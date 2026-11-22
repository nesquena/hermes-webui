"""#8523 release gate, fold 8: two range regressions the release delta gate found.

R1. With another profile's cookie, ``POST /api/chat/start`` on another client's session (read-only here) answered 409
with its read-only reason ("Continue in Desktop.") before the profile check, and the transport-configured path added a
403 "External session belongs to a different profile". A session the active profile cannot list now gets master's
answer at this route: master's own 403 for a read-only imported session (master refuses that before the profile check),
otherwise master's invalid-profile 400 or its ``_session_profile_mismatch_response``. One deliberate difference, accepted
as intended: master could retag an EMPTY external session of another profile to the requesting profile; it now keeps its
owner. A session the profile can open (its own, and unowned legacy ones under the root profile) keeps the PR's answer:
409 with the read-only reason. With a native session transport configured, a session the profile cannot see still gets
master's answer: no transport exemption, no transport 400, and no transcript refresh before the refusal.

R2. ``refreshSession`` (the offline/bfcache recovery refresh) replaced ``S.session`` without ``updateSendBtn()``, so a
session that turned read-only (or writable) kept the old composer lock, notice and placeholder until something else
redrew them.
"""
import json
import pathlib
import shutil
import subprocess
from collections import OrderedDict
from unittest.mock import MagicMock
from urllib.parse import urlparse

import pytest

from api import gateway_sessions, models, routes

REPO = pathlib.Path(__file__).resolve().parents[1]
UI_JS = (REPO / "static" / "ui.js").read_text(encoding="utf-8")
NODE = shutil.which("node")
REASON = "Read-only in WebUI. Continue in Desktop."
READ_ONLY_REPLY = (409, {"error": REASON, "code": "gateway_external_session_read_only"})
# Master's answers at this route (exp-v0.52.635), before #8523:
MASTER_INVALID_PROFILE = (400, "invalid profile")


@pytest.fixture
def chat_start(tmp_path, monkeypatch):
    state = tmp_path / "webui"
    state.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", state)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", state / "_index.json")
    cache = OrderedDict()
    monkeypatch.setattr(models, "SESSIONS", cache)
    monkeypatch.setattr(routes, "SESSIONS", cache)
    monkeypatch.setattr(routes, "_agent_runtime_barrier_response", lambda **kw: None)
    monkeypatch.setattr(routes, "_check_csrf", lambda *a: True)
    start_run = MagicMock(side_effect=AssertionError("must not start a turn"))
    monkeypatch.setattr(routes, "_start_run", start_run)
    refreshed = []
    monkeypatch.setattr(routes, "get_cli_session_messages", lambda sid, **kw: refreshed.append(sid) or [])
    replies = []
    monkeypatch.setattr(routes, "j", lambda h, body, status=200, **kw: replies.append((status, body)) or True)
    monkeypatch.setattr(routes, "bad", lambda h, text, status=400: replies.append((status, text)) or True)

    def run(*, owner, active, source="desktop", read_only_reason=REASON, imported_flag=False, requested=None,
            empty=False, transport=False, extra=None, cli=False):
        sid = "external-1"
        session = models.Session(session_id=sid, profile=owner, workspace=str(tmp_path), source_tag=source,
                                 session_source="other", read_only=imported_flag)
        if cli:
            session.is_cli_session = True
        if not empty:
            session.messages = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}]
        session.save()
        cache[sid] = session
        original = session.path.read_bytes()
        monkeypatch.setattr(routes, "gateway_session_chat_read_only_reason",
                            lambda row, config_data=None: read_only_reason)
        monkeypatch.setattr(routes, "_get_active_profile_name", lambda: active)
        # Native session transport on or off (a configured transport exempts a listed session from read-only).
        monkeypatch.setattr(routes, "webui_gateway_chat_enabled", lambda *a, **k: transport)
        monkeypatch.setattr(gateway_sessions, "configured", lambda *a, **k: transport)
        refreshed.clear()
        body = {"session_id": sid, "message": "hello", **(extra or {})}
        if requested:
            body["profile"] = requested
        monkeypatch.setattr(routes, "read_body", lambda *a: dict(body))
        routes.handle_post(MagicMock(), urlparse("/api/chat/start"))
        got = replies[-1]
        replies.clear()
        routes._session_profile_mismatch_response(MagicMock(), sid, owner)
        mismatch = replies[-1]
        replies.clear()
        assert session.profile == owner and session.path.read_bytes() == original
        return got, mismatch

    run.refreshed = refreshed
    return run


@pytest.mark.parametrize("owner", [None, "default", "josue"])
@pytest.mark.parametrize("requested", [None, "work"])
def test_hidden_read_only_session_gets_masters_response(chat_start, owner, requested):
    got, mismatch = chat_start(owner=owner, active="work", requested=requested)
    assert got == mismatch
    assert "Desktop" not in json.dumps(got)


@pytest.mark.parametrize("owner", [None, "default", "josue"])
@pytest.mark.parametrize("requested", [None, "work", "bad!"])
def test_hidden_imported_read_only_session_gets_masters_answer(chat_start, owner, requested):
    # Since #8853 (exp-v0.52.644) master refuses a session this profile can't see before any load, ahead of the
    # imported-session 403 it gave before: the imported flag makes no difference, as on master.
    got, mismatch = chat_start(owner=owner, active="work", imported_flag=True, requested=requested)
    assert got == (MASTER_INVALID_PROFILE if requested == "bad!" else mismatch)


@pytest.mark.parametrize("owner", [None, "josue"])
def test_hidden_read_only_session_with_malformed_profile_gets_masters_400(chat_start, owner):
    got, _ = chat_start(owner=owner, active="work", requested="bad!")
    assert got == MASTER_INVALID_PROFILE


@pytest.mark.parametrize("owner", [None, "default"])
def test_hidden_external_session_without_reason_gets_masters_response(chat_start, owner):
    got, mismatch = chat_start(owner=owner, active="work", read_only_reason="", requested="work")
    assert got == mismatch


@pytest.mark.parametrize("reason", [REASON, ""])
def test_hidden_empty_external_session_is_not_retagged(chat_start, reason):
    # The one deliberate difference from master: master retagged this session to the requesting profile.
    got, mismatch = chat_start(owner="josue", active="work", read_only_reason=reason, requested="work", empty=True)
    assert got == mismatch


@pytest.mark.parametrize("owner,active", [("work", "work"), (None, "default"), ("default", "default")])
@pytest.mark.parametrize("imported_flag", [False, True])
@pytest.mark.parametrize("requested", [None, "active", "bad!"])
@pytest.mark.parametrize("empty", [False, True])
def test_listed_read_only_session_still_says_why(chat_start, owner, active, imported_flag, requested, empty):
    requested = active if requested == "active" else requested
    got, _ = chat_start(owner=owner, active=active, imported_flag=imported_flag, requested=requested, empty=empty)
    assert got == READ_ONLY_REPLY


@pytest.mark.parametrize("owner", [None, "default", "josue"])
@pytest.mark.parametrize("requested", [None, "work", "bad!"])
def test_hidden_imported_session_with_native_transport_gets_masters_answer_before_any_refresh(chat_start, owner,
                                                                                               requested):
    # Codex r1105j: a configured transport exempted the session from master's refusal (and refreshed its cached
    # transcript first) even though the requesting profile cannot list it. Since #8853 (exp-v0.52.644) master's
    # refusal is its pre-load profile answer.
    got, mismatch = chat_start(owner=owner, active="work", read_only_reason="", imported_flag=True,
                               requested=requested, transport=True, cli=True)
    assert got == (MASTER_INVALID_PROFILE if requested == "bad!" else mismatch)
    assert chat_start.refreshed == []


@pytest.mark.parametrize("extra", [{"attachments": [{"name": "a.txt"}]}, {"regenerate": True}])
@pytest.mark.parametrize("owner", [None, "josue"])
def test_hidden_session_with_native_transport_gets_masters_profile_answer_not_a_transport_400(chat_start, extra,
                                                                                              owner):
    got, mismatch = chat_start(owner=owner, active="work", read_only_reason="", transport=True, extra=extra)
    assert got == mismatch


def test_listed_session_with_native_transport_keeps_the_transport_check(chat_start):
    got, _ = chat_start(owner="work", active="work", read_only_reason="", transport=True,
                        extra={"attachments": [{"name": "a.txt"}]})
    assert got[0] == 400 and "Gateway session continuation" in got[1]


@pytest.mark.parametrize("requested", [None, "work"])
def test_stored_profile_less_claude_code_sidecar_gets_masters_answer_like_its_detail_load(chat_start, requested):
    # A stored sidecar is judged by its own profile, as the session detail load's stored branch judges it (it 404s
    # there under a named profile), so another profile gets master's answer, not the reason.
    got, mismatch = chat_start(owner=None, active="work", source="claude_code", requested=requested,
                               read_only_reason="Read-only in WebUI. Continue in Claude Code.")
    assert got == mismatch == (404, "Session not found")


def _function_source(src: str, name: str) -> str:
    for head in (f"async function {name}(", f"function {name}("):
        if head in src:
            start = src.index(head)
            break
    else:
        raise AssertionError(name)
    depth, i = 0, src.index("{", src.index(")", start))
    while True:
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[start:i + 1]
        i += 1


_REFRESH_HARNESS = r"""
class El {
  constructor(id){ this.id=id; this.dataset={}; this.style={}; this.hidden=true; this.textContent=''; this.placeholder='Message Hermes\u2026';
    this.readOnly=false; this.disabled=false; this.title=''; this.attrs={}; this._cls=new Set(); const self=this;
    this.classList={toggle(c,on){ if(on===undefined?!self._cls.has(c):on) self._cls.add(c); else self._cls.delete(c); },
      add(c){self._cls.add(c);}, remove(c){self._cls.delete(c);}, contains(c){return self._cls.has(c);}}; }
  setAttribute(k,v){ this.attrs[k]=String(v); } getAttribute(k){ return k in this.attrs ? this.attrs[k] : null; }
  removeAttribute(k){ delete this.attrs[k]; }
}
const els={};
const $=(id)=>els[id]||(els[id]=new El(id));
let S={session:{session_id:'s1'}, messages:[], busy:false, activeStreamId:null};
let _messagesTruncated=false, _oldestIdx=0, _compressionPlaceholderSaved=null;
const window={};
let NEXT=null;
const api=async()=>({session:NEXT});
const t=(k)=>k==='composer_placeholder_read_only'?'Read-only in WebUI':k;
const dismissReconnect=()=>{}, syncTopbar=()=>{}, _renderMessagesWithScrollSnapshot=()=>{}, showToast=()=>{}, setStatus=(m)=>{ throw new Error(m); };
const _mergePendingSessionMessage=()=>{}, _adoptRegenerationRevision=()=>{};
const _composerHasContent=()=>false, _setComposerPrimaryButtonIcon=()=>{}, assistantDisplayName=()=>'Hermes';
const requestAnimationFrame=(f)=>f();
SOURCE
function snap(){ const m=$('msg'); return {readOnly:m.readOnly, placeholder:m.placeholder, notice:!$('composerChatReadOnlyNotice').hidden,
  sendDisabled:$('btnSend').disabled}; }
(async()=>{
  const out={};
  NEXT={session_id:'s1', messages:[], chat_read_only_reason:'Read-only in WebUI. Continue in Desktop.'};
  await refreshSession(); out.readOnly=snap();
  NEXT={session_id:'s1', messages:[]};
  await refreshSession(); out.writable=snap();
  process.stdout.write(JSON.stringify(out));
})().catch(e=>{ process.stdout.write(JSON.stringify({error:String(e)})); });
"""


def _send_button_id() -> str:
    body = _function_source(UI_JS, "updateSendBtn")
    start = body.index("$('", body.index("const btn"))
    return body[start + 3:body.index("'", start + 3)]


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_recovery_refresh_redraws_the_composer_both_ways():
    names = ("refreshSession", "updateSendBtn", "getComposerPrimaryAction", "_applyBusyComposerPlaceholder")
    source = "\n".join(_function_source(UI_JS, n) for n in names)
    harness = _REFRESH_HARNESS.replace("SOURCE", source).replace("$('btnSend')", f"$('{_send_button_id()}')")
    out = subprocess.run([NODE or "node", "-e", harness], capture_output=True, text=True, timeout=30, check=True)
    result = json.loads(out.stdout)
    assert "error" not in result, result
    assert result["readOnly"] == {"readOnly": True, "placeholder": "Read-only in WebUI", "notice": True,
                                  "sendDisabled": True}
    assert result["writable"]["readOnly"] is False
    assert result["writable"]["notice"] is False
    assert result["writable"]["placeholder"] == "Message Hermes\u2026"
