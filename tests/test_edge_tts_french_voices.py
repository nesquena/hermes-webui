"""French Edge TTS voices: server allowlist and voice-picker parity.

Each French voice offered by the Edge voice picker in ``static/panels.js``
must be accepted by the server-side allowlist in ``_handle_tts`` and reach
synthesis (HTTP 200) through the real ``/api/tts`` handler, with ``edge_tts``
stubbed so no network call happens.
"""
import io
import json
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


@pytest.mark.parametrize("voice", VOICES)
def test_french_voice_reaches_synthesis(monkeypatch, voice):
    from api import auth, routes

    monkeypatch.setattr(auth, "is_auth_enabled", lambda: False)
    if hasattr(routes._handle_tts, "_tts_limiter"):
        del routes._handle_tts._tts_limiter
    captured = []

    class Communicate:
        def __init__(self, text, voice, **kwargs):
            captured.append(voice)

        def stream_sync(self):
            yield {"type": "audio", "data": b"test"}

    monkeypatch.setitem(sys.modules, "edge_tts", SimpleNamespace(Communicate=Communicate))
    raw = json.dumps({"text": "Bonjour", "voice": voice}).encode()
    status = []
    # Unique client per voice so the per-client rate limiter never throttles
    # successive parametrized runs.
    client = f"10.98.0.{VOICES.index(voice) + 1}"
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
    assert status == [200], handler.wfile.getvalue()
    assert captured == [voice]


@pytest.mark.parametrize("voice", VOICES)
def test_french_voice_listed_in_edge_picker(voice):
    assert f"value:'{voice}'" in PANELS_JS
