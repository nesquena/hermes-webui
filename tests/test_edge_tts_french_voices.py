"""French Edge TTS voices: server allowlist and voice-picker parity.

Each French voice offered by the Edge voice picker in ``static/panels.js``
must be accepted by the server-side allowlist in ``_handle_tts`` and reach
synthesis (HTTP 200) through the real ``/api/tts`` handler, with ``edge_tts``
stubbed so no network call happens.

The picker side is checked by executing the real ``window._populateTtsVoices``
population logic under Node against a minimal ``<select>`` stub and asserting
the option values it renders — not by grepping the source.
"""
import io
import json
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
PANELS_JS = (ROOT / "static" / "panels.js").read_text(encoding="utf-8")

VOICES = [
    "fr-FR-RemyMultilingualNeural",
    "fr-FR-VivienneMultilingualNeural",
    "fr-FR-DeniseNeural",
    "fr-FR-EloiseNeural",
    "fr-FR-HenriNeural",
    "fr-CA-AntoineNeural",
    "fr-CA-JeanNeural",
    "fr-CA-SylvieNeural",
    "fr-CA-ThierryNeural",
]


def _synthesize(monkeypatch, voice, client):
    """Drive the real ``_handle_tts`` for ``voice`` from ``client``.

    Returns ``(status_codes, voices_passed_to_edge_tts, response_body)``.
    """
    from api import auth, routes

    monkeypatch.setattr(auth, "is_auth_enabled", lambda: False)
    captured = []

    class Communicate:
        def __init__(self, text, voice, **kwargs):
            captured.append(voice)

        def stream_sync(self):
            yield {"type": "audio", "data": b"test"}

    monkeypatch.setitem(sys.modules, "edge_tts", SimpleNamespace(Communicate=Communicate))
    raw = json.dumps({"text": "Bonjour", "voice": voice}).encode()
    status = []
    handler = SimpleNamespace(
        command="POST",
        rfile=io.BytesIO(raw),
        wfile=io.BytesIO(),
        headers={"Content-Length": str(len(raw))},
        client_address=(client, 1234),
        send_response=status.append,
        send_header=lambda *a: None,
        end_headers=lambda: None,
    )
    routes._handle_tts(handler, None)
    return status, captured, handler.wfile.getvalue()


def _extract_balanced_block(src, marker):
    assert src.count(marker) == 1, f"Expected exactly one {marker!r} in panels.js"
    start = src.index(marker)
    brace = src.index("{", start)
    depth = 0
    end = None
    for idx in range(brace, len(src)):
        ch = src[idx]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = idx + 1
                break
    assert end is not None, f"Unbalanced block for {marker!r}"
    return src[start:end]


def _render_edge_picker_options(current_voice):
    """Run the real ``_populateTtsVoices`` under Node with engine=edge.

    Returns the ``<option>`` elements appended to the ``#settingsTtsVoice``
    stub as ``[{"value", "label", "selected"}, ...]`` in render order.
    """
    populate_fn = _extract_balanced_block(PANELS_JS, "window._populateTtsVoices=function(){")
    script = f"""
const window = {{}};
const localStorage = {{
  store: new Map([
    ['hermes-tts-engine', 'edge'],
    ['hermes-tts-voice', {json.dumps(current_voice)}],
  ]),
  getItem(key) {{ return this.store.has(key) ? this.store.get(key) : null; }},
  setItem(key, value) {{ this.store.set(key, String(value)); }},
}};
function _speechSetting(settingKey, storageKey, fallback) {{
  const cached = localStorage.getItem(storageKey);
  return cached === null ? fallback : cached;
}}
function _syncSpeechPreferenceCache() {{}}
const document = {{
  createElement(tag) {{ return {{tagName: tag, value: '', textContent: '', selected: false}}; }},
}};
const ttsVoiceSel = {{
  options: [],
  set innerHTML(html) {{ this.options = []; }},
  appendChild(el) {{ this.options.push(el); return el; }},
}};
{populate_fn};
window._populateTtsVoices();
process.stdout.write(JSON.stringify(ttsVoiceSel.options.map(function (o) {{
  return {{value: o.value, label: o.textContent, selected: o.selected}};
}})));
"""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required to execute the real picker population logic")
    proc = subprocess.run([node, "-e", script], check=True, capture_output=True, text=True)
    return json.loads(proc.stdout)


@pytest.mark.parametrize("voice", VOICES)
def test_french_voice_reaches_synthesis(monkeypatch, voice):
    from api import routes

    if hasattr(routes._handle_tts, "_tts_limiter"):
        del routes._handle_tts._tts_limiter
    # Unique client per voice so the per-client rate limiter never throttles
    # successive parametrized runs.
    client = f"10.98.0.{VOICES.index(voice) + 1}"
    status, captured, body = _synthesize(monkeypatch, voice, client)
    assert status == [200], body
    assert captured == [voice]


def test_edge_picker_renders_exactly_the_french_voices_and_server_accepts_each(monkeypatch):
    from api import routes

    selected_voice = "fr-FR-VivienneMultilingualNeural"
    options = _render_edge_picker_options(selected_voice)
    values = [o["value"] for o in options]

    assert len(values) == len(set(values)), f"duplicate picker options: {values}"
    french = [v for v in values if v.startswith("fr-")]
    assert sorted(french) == sorted(VOICES)
    # The real population logic ran: the current voice is the one marked selected.
    assert [o["value"] for o in options if o["selected"]] == [selected_voice]
    for option in options:
        assert option["label"], f"empty label for {option['value']}"

    # Every French voice the picker actually renders is accepted by the server
    # allowlist and reaches synthesis through the real handler.
    if hasattr(routes._handle_tts, "_tts_limiter"):
        del routes._handle_tts._tts_limiter
    for idx, voice in enumerate(french, start=1):
        status, captured, body = _synthesize(monkeypatch, voice, f"10.98.1.{idx}")
        assert status == [200], (voice, body)
        assert captured == [voice]


# The Edge voices that shipped before the French entries, in their original
# picker order. The French voices are appended after them so the blank
# "Default (Xiaoxiao)" option stays next to its own voice family and existing
# users see the list they already know.
PRE_EXISTING_EDGE_VOICES = [
    "zh-CN-XiaoxiaoNeural",
    "zh-CN-XiaoyiNeural",
    "zh-CN-YunxiNeural",
    "zh-CN-YunjianNeural",
    "zh-CN-YunyangNeural",
    "en-US-AriaNeural",
    "en-US-GuyNeural",
    "id-ID-GadisNeural",
]


def test_edge_picker_appends_french_voices_after_existing_entries():
    # The stub <select> resets on innerHTML, so the blank "Default (Xiaoxiao)"
    # placeholder is not in this list; only the edgeVoices options are.
    values = [o["value"] for o in _render_edge_picker_options("")]
    assert values == PRE_EXISTING_EDGE_VOICES + VOICES
