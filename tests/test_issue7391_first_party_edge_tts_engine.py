"""G7394-1: first-party Edge callers must send engine:'edge' explicitly.

The Settings selector writes localStorage immediately, then saves the server
setting through a 350ms debounced autosave. During that window (or if the
save fails) the WebUI dispatches the Edge branch while the server still holds
ElevenLabs/OpenAI. Omitting engine lets the persisted-engine fallback silently
play the stale provider.

These tests execute the production ui.js / boot.js callers with a local Edge
selection and assert the /api/tts request body carries the explicit override.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.js_source_extract import extract_function


ROOT = Path(__file__).resolve().parents[1]
UI_JS = (ROOT / "static" / "ui.js").read_text(encoding="utf-8")
BOOT_JS = (ROOT / "static" / "boot.js").read_text(encoding="utf-8")
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")


def _run_node(script: str) -> list[dict]:
    result = subprocess.run(
        [NODE, "-e", script],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, (
        f"node subprocess failed:\n--- stdout ---\n{result.stdout}\n"
        f"--- stderr ---\n{result.stderr}"
    )
    return json.loads(result.stdout.strip().splitlines()[-1])


_UI_HARNESS = r"""
const requests = [];
const store = {
  'hermes-tts-engine': 'edge',
  'hermes-tts-voice': 'en-US-AriaNeural',
};
globalThis.window = globalThis;
globalThis.speechSynthesis = { cancel() {}, getVoices() { return []; } };
globalThis.localStorage = {
  getItem: (k) => Object.prototype.hasOwnProperty.call(store, k) ? store[k] : null,
};
globalThis.document = {
  baseURI: 'http://localhost:8787/',
  querySelectorAll: () => [],
};
globalThis.location = { href: 'http://localhost:8787/' };
globalThis.fetch = (url, opts) => {
  requests.push({ url: String(url), body: JSON.parse(opts.body) });
  return new Promise(() => {});
};
let _ttsSpeaking = false;
let _playingEdgeAudio = null;
let _ttsCurrentUtterance = null;
let _ttsChunkQueue = [];
let _ttsChunkIndex = 0;
let _ttsActiveBtn = null;
__UI_FNS__
const row = { dataset: { rawText: 'Hello from Edge' } };
const btn = { dataset: { speaking: '0' }, closest: () => row };
speakMessage(btn);
console.log(JSON.stringify(requests));
"""


_BOOT_HARNESS = r"""
const requests = [];
const store = {
  'hermes-tts-engine': 'edge',
  'hermes-tts-voice': 'en-US-AriaNeural',
};
globalThis.window = globalThis;
globalThis.localStorage = {
  getItem: (k) => Object.prototype.hasOwnProperty.call(store, k) ? store[k] : null,
};
globalThis.document = {
  baseURI: 'http://localhost:8787/',
  querySelectorAll: () => [{ dataset: { rawText: 'Hello from Edge' } }],
};
globalThis.location = { href: 'http://localhost:8787/' };
globalThis.fetch = (url, opts) => {
  requests.push({ url: String(url), body: JSON.parse(opts.body) });
  return new Promise(() => {});
};
const S = { session: { session_id: 'sid-edge' } };
let _voiceModeActive = true;
let _voiceModeThinkingSid = null;
let _ttsSpeaking = false;
let _playingEdgeAudio = null;
function _setState() {}
function _startListening() {}
__BOOT_FNS__
_speakResponse();
console.log(JSON.stringify(requests));
"""


def test_play_edge_tts_chunked_sends_explicit_engine_when_server_still_elevenlabs():
    """Local Edge selection (Listen button) must override persisted ElevenLabs."""
    media_ref = re.search(r"^const MEDIA_REF_CLASS\s*=.*?;$", UI_JS, re.M).group(0)
    fns = "\n".join(
        [
            media_ref,
            extract_function(UI_JS, "_stripForTTS"),
            extract_function(UI_JS, "_splitForTTS"),
            extract_function(UI_JS, "_playEdgeTtsChunked"),
            extract_function(UI_JS, "stopTTS"),
            extract_function(UI_JS, "speakMessage"),
        ]
    )
    requests = _run_node(_UI_HARNESS.replace("__UI_FNS__", fns))

    assert len(requests) == 1
    assert requests[0]["url"].endswith("/api/tts")
    assert requests[0]["body"]["engine"] == "edge"
    assert requests[0]["body"]["text"] == "Hello from Edge"
    assert requests[0]["body"]["voice"] == "en-US-AriaNeural"


def test_voice_mode_edge_branch_sends_explicit_engine_when_server_still_openai():
    """Local Edge selection (voice-mode auto-read) must override persisted OpenAI."""
    fns = extract_function(BOOT_JS, "_speakResponse")
    requests = _run_node(_BOOT_HARNESS.replace("__BOOT_FNS__", fns))

    assert len(requests) == 1
    assert requests[0]["url"].endswith("/api/tts")
    assert requests[0]["body"]["engine"] == "edge"
    assert requests[0]["body"]["text"] == "Hello from Edge"
    assert requests[0]["body"]["voice"] == "en-US-AriaNeural"
