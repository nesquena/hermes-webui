"""Idea-mode flow tests (spec t_14ad8e89 §3, task t_b789726a).

Note on eval() in the JS tests: the node harness evals ONLY function source
extracted from this repo's own static/*.js (same pattern as
tests/test_issue5231_default_kanban_dispatch.py); no user input is ever eval'd.

Covers the capture flow end-to-end at the unit level with NO microphone and
NO hermes-agent install:

* session-flag store: on/off/status, idle expiry, stats counters
* /idea command semantics (on|off|status + usage error)
* capture pipeline: JSON typed ideas + multipart voice clips through the
  upload multipart reader with ``transcribe_audio`` STUBBED (CI has no mic),
  whisper-hallucination rejection (🤷, no card — spec §2/§6.7)
* §5 idea-payload contract emitted to the filing stage (exact keys/values)
* HTTP handlers incl. routes.py dispatch lines (source assertion pattern)
* read-back suppression is structural: the filing double records that no
  agent-turn path (/api/chat/start) is ever invoked by capture
* JS: send() interception + captureIdea() behaviour via node (js_source_extract
  pattern from test_issue5231_default_kanban_dispatch.py)
"""

from __future__ import annotations

import io
import json
import shutil
import subprocess
import sys
import tempfile
import time
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import api.idea_mode as idea_mode  # noqa: E402


# ── Helpers ────────────────────────────────────────────────────────────────


def _fresh_store(monkeypatch):
    """Isolate the module-level session store per test."""
    monkeypatch.setattr(idea_mode, "_SESSIONS", {})
    return idea_mode


class FakeFilingResult:
    def __init__(self, ok=True, kind="filed", text="✅ t_abc123: Test idea",
                 card_ids=("t_abc123",)):
        self.ok = ok
        self.kind = kind
        self.text = text
        self.card_ids = list(card_ids)

    def to_payload(self):
        return {"ok": self.ok, "kind": self.kind, "text": self.text,
                "card_ids": self.card_ids}


class FakeFiler:
    """Records the payload the capture stage emits (the §5 contract)."""

    def __init__(self, result=None):
        self.calls: list[dict] = []
        self.result = result or FakeFilingResult()

    def __call__(self, payload):
        self.calls.append(payload)
        return self.result


def _install_fake_filing(monkeypatch, filer: FakeFiler | None = None):
    filer = filer or FakeFiler()
    fake_module = types.ModuleType("api.idea_filing")
    fake_module.file_ideas = filer
    monkeypatch.setitem(sys.modules, "api.idea_filing", fake_module)
    monkeypatch.setattr(idea_mode, "_FILING_MODULE", "api.idea_filing")
    return filer


def _install_fake_stt(monkeypatch, transcript="een idee over de toolbelt", hallucination=False,
                      error=None):
    """Stub tools.transcription_tools + tools.voice_mode_transcript (no mic, no agent)."""
    fake_tt = types.ModuleType("tools.transcription_tools")
    fake_tt.transcribe_audio = lambda path: (
        {"success": False, "error": error} if error
        else {"success": True, "transcript": transcript}
    )
    fake_vmt = types.ModuleType("tools.voice_mode_transcript")
    fake_vmt.is_whisper_hallucination = lambda s: hallucination
    tools_parent = sys.modules.get("tools") or types.ModuleType("tools")
    if "tools" not in sys.modules:
        monkeypatch.setitem(sys.modules, "tools", tools_parent)
    monkeypatch.setitem(sys.modules, "tools.transcription_tools", fake_tt)
    monkeypatch.setitem(sys.modules, "tools.voice_mode_transcript", fake_vmt)
    monkeypatch.setattr(tools_parent, "transcription_tools", fake_tt, raising=False)
    monkeypatch.setattr(tools_parent, "voice_mode_transcript", fake_vmt, raising=False)


class FakeHandler:
    """Minimal request handler double (pattern: tests/test_voice_transcribe_endpoint.py)."""

    def __init__(self, body: bytes, content_type: str):
        self.rfile = io.BytesIO(body)
        self.wfile = io.BytesIO()
        self.headers = {"Content-Type": content_type, "Content-Length": str(len(body))}
        self.status = None
        self.sent_headers = {}

    def send_response(self, status):
        self.status = status

    def send_header(self, key, value):
        self.sent_headers[key] = value

    def end_headers(self):
        pass

    def payload(self):
        return json.loads(self.wfile.getvalue().decode("utf-8"))


def _multipart_body(fields=None, files=None, boundary=b"ideaboundary"):
    fields = fields or {}
    files = files or {}
    body = b""
    for name, value in fields.items():
        body += b"--" + boundary + b"\r\n"
        body += f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode()
        body += str(value).encode() + b"\r\n"
    for name, (filename, data, content_type) in files.items():
        body += b"--" + boundary + b"\r\n"
        body += (
            f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'
            f"Content-Type: {content_type}\r\n\r\n"
        ).encode()
        body += data + b"\r\n"
    body += b"--" + boundary + b"--\r\n"
    return body, f"multipart/form-data; boundary={boundary.decode()}"


def _json_body(obj) -> tuple[bytes, str]:
    return json.dumps(obj).encode(), "application/json"


# ── Session-flag store ─────────────────────────────────────────────────────


def test_flag_starts_off_and_status_shape():
    state = idea_mode.get_state("s1")
    assert state["idea_mode"] is False
    assert state["stats"] == {"filed": 0, "dupes": 0, "failed": 0, "queued": 0}
    assert idea_mode.is_idea_mode("s1") is False


def test_flag_on_off_roundtrip(monkeypatch):
    m = _fresh_store(monkeypatch)
    m.set_idea_mode("s1", True)
    assert m.is_idea_mode("s1") is True
    state = m.set_idea_mode("s1", False)
    assert state["idea_mode"] is False
    assert m.is_idea_mode("s1") is False


def test_flag_idle_expiry(monkeypatch):
    m = _fresh_store(monkeypatch)
    m.set_idea_mode("s1", True)
    # Age the session past the 30-minute idle window.
    with m._LOCK:
        m._SESSIONS["s1"]["last_active"] = time.time() - (m.IDLE_TIMEOUT_S + 60)
    assert m.is_idea_mode("s1") is False


def test_stats_counters_bump_by_result_kind(monkeypatch):
    m = _fresh_store(monkeypatch)
    m.set_idea_mode("s1", True)
    filer = _install_fake_filing(monkeypatch, FakeFiler(FakeFilingResult(
        ok=True, kind="filed", card_ids=("t_1", "t_2"))))
    m.capture_idea("s1", "idea one")
    m.capture_idea("s1", "idea two")
    filer.result = FakeFilingResult(ok=True, kind="dupe", text="🔁 Already on the board: t_1", card_ids=[])
    m.capture_idea("s1", "idea one again")
    state = m.get_state("s1")
    assert state["stats"]["filed"] == 4  # two cards per filed utterance
    assert state["stats"]["dupes"] == 1
    assert state["stats"]["failed"] == 0


def test_summary_line_format(monkeypatch):
    m = _fresh_store(monkeypatch)
    m.set_idea_mode("s1", True)
    with m._LOCK:
        m._SESSIONS["s1"]["stats"] = {"filed": 3, "dupes": 1, "failed": 0, "queued": 0}
    state = m.set_idea_mode("s1", False)
    assert m.summary_line(state) == "3 filed, 1 dupe, 0 lost. Board: ideas."


# ── /idea command semantics ────────────────────────────────────────────────


def test_idea_command_on_off_status(monkeypatch):
    m = _fresh_store(monkeypatch)
    on = m.handle_idea_command("s1", "on")
    assert on["ok"] and on["idea_mode"] is True and "Idea mode on" in on["text"]
    status = m.handle_idea_command("s1", "status")
    assert status["ok"] and status["idea_mode"] is True and "1" not in status["text"].split(":")[0]
    off = m.handle_idea_command("s1", "off")
    assert off["ok"] and off["idea_mode"] is False
    assert off["text"] == "0 filed, 0 dupes, 0 lost. Board: ideas."


def test_idea_command_rejects_bad_arg():
    bad = idea_mode.handle_idea_command("s1", "banana")
    assert bad["ok"] is False
    assert "usage" in bad["error"]


# ── Capture: §5 payload contract (the exit criterion) ─────────────────────


def test_capture_emits_exact_spec5_payload(monkeypatch):
    m = _fresh_store(monkeypatch)
    m.set_idea_mode("s1", True)
    filer = _install_fake_filing(monkeypatch)
    m.capture_idea("s1", "transit layer voor earth-monitor", audio_sha16="ab12cd34ef567890",
                   attempts=0, ts=1792200000.0)
    assert len(filer.calls) == 1
    payload = filer.calls[0]
    assert payload == {
        "session_id": "s1",
        "source": "webui",
        "transcript": "transit layer voor earth-monitor",
        "ts": 1792200000.0,
        "audio_sha16": "ab12cd34ef567890",
        "attempts": 0,
    }
    assert list(payload.keys()) == ["session_id", "source", "transcript", "ts",
                                    "audio_sha16", "attempts"]


def test_capture_requires_mode_on(monkeypatch):
    m = _fresh_store(monkeypatch)
    filer = _install_fake_filing(monkeypatch)
    result = m.capture_idea("s1", "an idea")
    assert result["kind"] == "mode_off"
    assert filer.calls == []


def test_capture_never_calls_chat_start(monkeypatch):
    """Structural no-read-back: the capture path has no reference to the agent
    turn route at all — assert the module source never mentions it."""
    src = (REPO_ROOT / "api" / "idea_mode.py").read_text(encoding="utf-8")
    assert "/api/chat/start" not in src
    messages_js = (REPO_ROOT / "static" / "messages.js").read_text(encoding="utf-8")
    capture_fn = messages_js[messages_js.find("async function captureIdea"):]
    capture_fn = capture_fn[: capture_fn.find("\nasync function send(")]
    assert "/api/chat/start" not in capture_fn
    assert "/api/idea/capture" in capture_fn


# ── Capture: voice path with stubbed STT ──────────────────────────────────


def test_capture_voice_clip_transcribes_and_files(monkeypatch):
    m = _fresh_store(monkeypatch)
    m.set_idea_mode("s1", True)
    _install_fake_stt(monkeypatch, transcript="wifi scanner met live discovery")
    filer = _install_fake_filing(monkeypatch)
    result = m.capture_idea("s1", audio_bytes=b"RIFFfakeaudio-bytes")
    assert result["ok"] is True
    assert result["kind"] == "filed"
    assert result["idea"]["transcript"] == "wifi scanner met live discovery"
    assert result["idea"]["audio_sha16"]  # sha256[:16] of the clip bytes
    assert len(result["idea"]["audio_sha16"]) == 16
    assert filer.calls[0]["transcript"] == "wifi scanner met live discovery"


def test_capture_voice_clip_rejects_whisper_hallucination(monkeypatch):
    """Spec §2/§6.7: hallucination strings must flow to filing as EMPTY —
    🤷 line, no card, no crash."""
    m = _fresh_store(monkeypatch)
    m.set_idea_mode("s1", True)
    _install_fake_stt(monkeypatch, transcript="Thank you for watching!",
                      hallucination=True)
    filer = _install_fake_filing(monkeypatch, FakeFiler(FakeFilingResult(
        ok=False, kind="nothing_heard", text="🤷 Nothing heard — try again.", card_ids=())))
    result = m.capture_idea("s1", audio_bytes=b"silence-bytes")
    assert result["kind"] == "nothing_heard"
    assert result["text"].startswith("🤷")
    assert filer.calls[0]["transcript"] == ""  # rejected as empty, not filed
    assert result["idea"]["transcript"] == ""


def test_capture_voice_clip_stt_error_surfaces(monkeypatch):
    m = _fresh_store(monkeypatch)
    m.set_idea_mode("s1", True)
    _install_fake_stt(monkeypatch, error="STT not configured")
    filer = _install_fake_filing(monkeypatch)
    result = m.capture_idea("s1", audio_bytes=b"bytes")
    assert result["ok"] is False
    assert result["kind"] == "stt_error"
    assert filer.calls == []
    assert m.get_state("s1")["stats"]["failed"] == 1


def test_capture_missing_stt_module_is_503_not_crash(monkeypatch):
    m = _fresh_store(monkeypatch)
    m.set_idea_mode("s1", True)
    monkeypatch.setitem(sys.modules, "tools.transcription_tools", None)
    result = m.capture_idea("s1", audio_bytes=b"bytes")
    assert result["kind"] == "stt_error"
    assert "unavailable" in result["error"]


def test_capture_missing_filing_module_is_clean_error(monkeypatch):
    m = _fresh_store(monkeypatch)
    m.set_idea_mode("s1", True)
    _install_fake_stt(monkeypatch)
    # Point the lazy loader at a module that does not exist and drop any
    # cached import — this is the "only one of the two PRs deployed" case.
    monkeypatch.setattr(idea_mode, "_FILING_MODULE", "api.idea_filing_absent")
    monkeypatch.delitem(sys.modules, "api.idea_filing", raising=False)
    result = m.capture_idea("s1", "typed idea")
    assert result["kind"] == "filing_unavailable"
    assert "unavailable" in result["error"]


# ── HTTP handler: multipart + JSON ─────────────────────────────────────────


def _fake_get_session(monkeypatch, exists=True):
    session = SimpleNamespace(profile=None)
    def get_session(sid):
        if not exists:
            raise KeyError(sid)
        return session
    import api.models as models
    monkeypatch.setattr(models, "get_session", get_session)
    # idea_mode imported get_session by value — patch its reference too
    monkeypatch.setattr(idea_mode, "get_session", get_session)


def test_handle_capture_multipart_voice(monkeypatch):
    m = _fresh_store(monkeypatch)
    m.set_idea_mode("sess-voice", True)
    _install_fake_stt(monkeypatch, transcript="dashboard widgets in de toolbelt")
    _install_fake_filing(monkeypatch)
    _fake_get_session(monkeypatch)

    body, ct = _multipart_body(
        fields={"session_id": "sess-voice"},
        files={"file": ("voice.webm", b"RIFFfake-opus-bytes", "audio/webm")},
    )
    handler = FakeHandler(body, ct)
    m.handle_idea_capture(handler)
    assert handler.status == 200
    payload = handler.payload()
    assert payload["ok"] is True
    assert payload["kind"] == "filed"
    assert payload["idea"]["transcript"] == "dashboard widgets in de toolbelt"


def test_handle_capture_json_typed(monkeypatch):
    m = _fresh_store(monkeypatch)
    m.set_idea_mode("sess-typed", True)
    _install_fake_filing(monkeypatch)
    _fake_get_session(monkeypatch)

    body, ct = _json_body({"session_id": "sess-typed",
                           "transcript": "worldwide transit layer met 9292 data"})
    handler = FakeHandler(body, ct)
    m.handle_idea_capture(handler)
    assert handler.status == 200
    payload = handler.payload()
    assert payload["idea"]["transcript"] == "worldwide transit layer met 9292 data"
    assert payload["idea"]["source"] == "webui"


def test_handle_capture_requires_session_id(monkeypatch):
    body, ct = _json_body({"transcript": "idea"})
    handler = FakeHandler(body, ct)
    idea_mode.handle_idea_capture(handler)
    assert handler.status == 400
    assert "session_id" in handler.payload()["error"]


def test_handle_capture_unknown_session_404(monkeypatch):
    m = _fresh_store(monkeypatch)
    _fake_get_session(monkeypatch, exists=False)
    body, ct = _json_body({"session_id": "ghost", "transcript": "idea"})
    handler = FakeHandler(body, ct)
    m.handle_idea_capture(handler)
    assert handler.status == 404


def test_handle_capture_mode_off_is_409(monkeypatch):
    m = _fresh_store(monkeypatch)
    _fake_get_session(monkeypatch)
    _install_fake_filing(monkeypatch)
    body, ct = _json_body({"session_id": "sess-off", "transcript": "idea"})
    handler = FakeHandler(body, ct)
    m.handle_idea_capture(handler)
    assert handler.status == 409
    assert handler.payload()["idea_mode"] is False


def test_handle_mode_and_status_handlers(monkeypatch):
    m = _fresh_store(monkeypatch)
    handler = FakeHandler(*_json_body({"session_id": "s9", "on": True}))
    m.handle_idea_mode(handler, json.loads(handler.rfile.read().decode()))
    assert handler.status == 200
    assert handler.payload()["idea_mode"] is True

    parsed = SimpleNamespace(path="/api/idea/status", query="session_id=s9")
    handler2 = FakeHandler(b"", "")
    m.handle_idea_status(handler2, parsed)
    assert handler2.status == 200
    assert handler2.payload()["idea_mode"] is True
    assert handler2.payload()["stats"] == {"filed": 0, "dupes": 0, "failed": 0, "queued": 0}


def test_handle_status_requires_session_id():
    handler = FakeHandler(b"", "")
    idea_mode.handle_idea_status(handler, SimpleNamespace(path="/api/idea/status", query=""))
    assert handler.status == 400


# ── routes.py dispatch (source assertions — existing gate pattern) ─────────


def test_routes_dispatch_idea_endpoints():
    src = (REPO_ROOT / "api" / "routes.py").read_text(encoding="utf-8")
    assert 'parsed.path == "/api/idea/capture"' in src
    assert "handle_idea_capture(handler)" in src
    assert 'parsed.path == "/api/idea/mode"' in src
    assert "handle_idea_mode(handler, body)" in src
    assert 'parsed.path == "/api/idea/status"' in src
    assert "handle_idea_status(handler, parsed)" in src


def test_capture_route_registered_before_read_body():
    """The capture route accepts multipart, so it must dispatch in the
    pre-read_body block (with /api/upload, /api/transcribe), not after it."""
    src = (REPO_ROOT / "api" / "routes.py").read_text(encoding="utf-8")
    capture_pos = src.find('parsed.path == "/api/idea/capture"')
    read_body_pos = src.find("body = read_body(handler)")
    assert 0 < capture_pos < read_body_pos


# ── JS behaviour via node (pattern: test_issue5231_default_kanban_dispatch) ─


NODE = shutil.which("node")
pytestmark_js = pytest.mark.skipif(NODE is None, reason="node not on PATH")


def _run_node(js: str) -> dict:
    with tempfile.NamedTemporaryFile("w", suffix=".cjs", encoding="utf-8",
                                     dir=REPO_ROOT, delete=False) as script:
        script.write(js)
        script_path = Path(script.name)
    try:
        proc = subprocess.run([NODE, str(script_path)], cwd=REPO_ROOT,
                              capture_output=True, text=True, timeout=30)
    finally:
        script_path.unlink(missing_ok=True)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip())
    return json.loads(proc.stdout.strip())


def _extract_messages_function(name: str, prefix: str = "async function") -> str:
    from tests.js_source_extract import extract_function

    js = (REPO_ROOT / "static" / "messages.js").read_text(encoding="utf-8")
    return extract_function(js, name, prefix=prefix)


def _capture_js_prelude():
    return """
let _posted = [];
let _pushed = [];
const window = {_ideaModeOn: true, _ideaModeSid: 's-js'};
const S = {session: {session_id: 's-js'}, messages: []};
const composer = {value: 'spoken idea via mic'};
function $(id){ return composer; }
function autoResize(){}
function renderMessages(){ _pushed.push(S.messages.slice()); }
function showToast(_m, _t){}
async function api(path, opts){
  _posted.push({path, opts});
  if(path.startsWith('/api/idea/status')) return {idea_mode: true, stats: {filed: 1, dupes: 0}};
  return {ok: true, kind: 'filed', text: '✅ t_js01: Spoken idea via mic',
          card_ids: ['t_js01'], idea: {session_id: 's-js', source: 'webui'}};
}
function setComposerStatus(_s){}
function t(key, ...args){ return key + (args.length ? ':' + args.join(',') : ''); }
function _pushIdeaLine(text){ S.messages.push({role:'assistant',content:String(text),_ts:1,_idea:true}); }
"""


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_js_capture_idea_posts_to_capture_endpoint_and_renders_confirmation():
    fn = _extract_messages_function("captureIdea")
    out = _run_node(_capture_js_prelude() + f"""
const fnSource = {json.dumps(fn)};
eval(fnSource);
(async () => {{
  await captureIdea('spoken idea via mic');
  const post = _posted.find(p => p.path === '/api/idea/capture');
  if (!post) throw new Error('captureIdea never POSTed /api/idea/capture');
  const body = JSON.parse(post.opts.body);
  if (body.session_id !== 's-js' || body.transcript !== 'spoken idea via mic')
    throw new Error('bad payload: ' + JSON.stringify(body));
  if (post.opts.body && _posted.some(p => p.path === '/api/chat/start'))
    throw new Error('captureIdea must never start an agent turn');
  const last = S.messages[S.messages.length - 1];
  if (last.role !== 'assistant' || !last.content.startsWith('✅'))
    throw new Error('no confirmation line rendered: ' + JSON.stringify(last));
  console.log(JSON.stringify({{posted: _posted.map(p => p.path),
                                confirmation: last.content}}));
}})().catch(e => {{ console.error(String(e)); process.exit(1); }});
""")
    assert "/api/idea/capture" in out["posted"]
    assert "/api/chat/start" not in out["posted"]
    assert out["confirmation"].startswith("✅")


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_js_capture_idea_restores_composer_on_failure():
    fn = _extract_messages_function("captureIdea")
    out = _run_node(_capture_js_prelude().replace(
        "return {ok: true, kind: 'filed'",
        "throw new Error('server down'); return {ok: false, kind: 'x'",
    ) + f"""
const fnSource = {json.dumps(fn)};
eval(fnSource);
(async () => {{
  await captureIdea('dutch idea that must not be lost');
  if (composer.value !== 'dutch idea that must not be lost')
    throw new Error('composer not restored: ' + JSON.stringify(composer.value));
  const last = S.messages[S.messages.length - 1];
  if (last.role !== 'assistant' || !last.content.startsWith('idea_capture_failed'))
    throw new Error('failure line missing: ' + JSON.stringify(last));
  console.log(JSON.stringify({{restored: composer.value, last: last.content}}));
}})().catch(e => {{ console.error(String(e)); process.exit(1); }});
""")
    assert out["restored"] == "dutch idea that must not be lost"


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_js_send_intercepts_while_idea_mode_on():
    """Extract send()'s idea-mode intercept marker and execute the guard block
    semantics: with the flag on and non-slash text, the chat-start route is
    never called. (send() itself is too entangled to eval; the intercept is
    verified by executing captureIdea + asserting the intercept's structural
    markers exist in send().)"""
    messages_js = (REPO_ROOT / "static" / "messages.js").read_text(encoding="utf-8")
    assert "window._ideaModeOn" in messages_js
    assert "captureIdea(_ideaText)" in messages_js
    assert "// ── Idea-mode capture intercept (spec t_14ad8e89 §3.2) ──" in messages_js
    # The intercept must sit AFTER the slash-command block (so /idea off works)
    # and BEFORE the first /api/chat/start call in send().
    intercept_pos = messages_js.find("Idea-mode capture intercept")
    slash_block = messages_js.find("Slash command intercept")
    chat_start = messages_js.find("api('/api/chat/start'")
    assert 0 < slash_block < intercept_pos < chat_start


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_js_commands_idea_registered_and_toggle_calls_mode_route():
    commands_js = (REPO_ROOT / "static" / "commands.js").read_text(encoding="utf-8")
    assert "{name:'idea'" in commands_js
    assert "async function cmdIdea" in commands_js
    assert "/api/idea/mode" in commands_js
    assert "/api/idea/status" in commands_js


def test_i18n_keys_present_in_en():
    i18n = (REPO_ROOT / "static" / "i18n.js").read_text(encoding="utf-8")
    for key in ("cmd_idea", "idea_usage", "idea_status_on", "idea_status_off",
                "idea_capturing", "idea_capture_failed", "idea_mode_on_toast",
                "idea_toggle_failed"):
        assert re_search(i18n, key), f"i18n key missing: {key}"


def re_search(haystack: str, key: str) -> bool:
    import re

    return re.search(rf"\b{re.escape(key)}\s*:", haystack) is not None
