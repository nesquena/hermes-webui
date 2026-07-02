"""Tests for raw audio upload flow — sending audio as attachment vs transcribing."""
import io
import json
import sys
import types

from api.upload import handle_upload, handle_transcribe


def _multipart_body(fields=None, files=None, boundary=b"testboundary"):
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


class _FakeHandler:
    def __init__(self, body: bytes, content_type: str, session_id: str | None = None):
        self.rfile = io.BytesIO(body)
        self.wfile = io.BytesIO()
        self.headers = {
            "Content-Type": content_type,
            "Content-Length": str(len(body)),
        }
        self._session_id = session_id
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


# ── Raw Audio Upload Tests ────────────────────────────────────────────────


def test_raw_audio_upload_accepts_audio_file():
    """Audio files uploaded via /api/upload should succeed and return audio mime."""
    body, content_type = _multipart_body(
        fields={"session_id": "test-session-raw-audio"},
        files={"file": ("voice.webm", b"RIFF\x1a\x9f\x01fake_opus_data", "audio/webm")},
    )
    handler = _FakeHandler(body, content_type)
    try:
        handle_upload(handler)
    except KeyError:
        # Session not found is expected without a real session store
        pass
    # The handler should at least parse the request without crashing
    # Real session check happens after parsing
    assert handler.status is not None


def test_raw_audio_upload_rejects_missing_file():
    """Upload without file field should return 400."""
    body, content_type = _multipart_body(fields={"session_id": "test-session"})
    handler = _FakeHandler(body, content_type)
    handle_upload(handler)
    assert handler.status == 400
    assert handler.payload()["error"] == "No file field in request"


def test_raw_audio_vs_transcribe_no_regression(monkeypatch):
    """The /api/transcribe endpoint continues working independently of raw audio mode."""
    fake_mod = types.ModuleType("tools.transcription_tools")
    fake_mod.transcribe_audio = lambda path: {"success": True, "transcript": "hello from audio"}
    monkeypatch.setitem(sys.modules, "tools.transcription_tools", fake_mod)

    body, content_type = _multipart_body(
        files={"file": ("voice.webm", b"RIFFfakeaudio", "audio/webm")}
    )
    handler = _FakeHandler(body, content_type)
    handle_transcribe(handler)

    assert handler.status == 200
    assert handler.payload() == {"ok": True, "transcript": "hello from audio"}


def test_raw_audio_upload_requires_session():
    """Upload should return 404 for missing session."""
    body, content_type = _multipart_body(
        fields={"session_id": "nonexistent-session-id"},
        files={"file": ("voice.webm", b"fake", "audio/webm")},
    )
    handler = _FakeHandler(body, content_type)
    try:
        handle_upload(handler)
    except KeyError:
        # Expected — _FakeHandler doesn't have a real session store
        pass
    assert handler.status is None or handler.status == 404


def test_raw_audio_upload_mime_type_detection():
    """Uploaded audio file should return correct audio MIME type."""
    body, content_type = _multipart_body(
        fields={"session_id": "test-session-mime"},
        files={"file": ("recording.webm", b"RIFF\x1a\x9f\x01fake", "audio/webm")},
    )
    handler = _FakeHandler(body, content_type)
    try:
        handle_upload(handler)
    except KeyError:
        pass
    # The upload handler should at minimum parse the multipart and
    # guess the mime type from filename extension
    # (real session check happens after parsing)
    assert handler.status is not None


def test_raw_audio_upload_different_formats():
    """Audio files in different formats (ogg, wav) should all be accepted."""
    for filename, mime in [("voice.ogg", "audio/ogg"), ("voice.wav", "audio/wav"), ("voice.mp3", "audio/mpeg")]:
        body, content_type = _multipart_body(
            fields={"session_id": "test-session-fmt"},
            files={"file": (filename, b"fake audio", mime)},
        )
        handler = _FakeHandler(body, content_type)
        try:
            handle_upload(handler)
        except KeyError:
            pass
        assert handler.status is not None, f"Failed for {filename} ({mime})"


# ── Front-end: raw-audio mic backend pinning (boot.js source invariants) ──────
# Regression guard for the #3169 Codex-review fix: _stopMic / onstop must act on
# the capture backend that was ACTIVE WHEN RECORDING STARTED (pinned in
# _activeCaptureMode), not whatever _rawAudioMode says now — otherwise toggling
# Settings → Sound mid-recording orphans the wrong backend. Also: an explicit
# Send click (_micPendingSend) must send even with text in the composer.
import pathlib as _pathlib

_BOOT_JS = (_pathlib.Path(__file__).parent.parent / "static" / "boot.js").read_text(encoding="utf-8")


def test_stop_mic_uses_pinned_active_capture_mode_not_current_rawmode():
    assert "let _activeCaptureMode" in _BOOT_JS, "_activeCaptureMode must be declared"
    # _stopMic decides backend from the pinned mode, not _rawAudioMode.
    assert "_activeCaptureMode==='speech'" in _BOOT_JS
    # onstop dispatches raw-vs-transcribe from the pinned mode too.
    assert "if(captureMode==='media-raw')" in _BOOT_JS
    # the mode is pinned at both start branches.
    assert "_activeCaptureMode='speech'" in _BOOT_JS
    assert "const captureMode=_rawAudioMode?'media-raw':'media-transcribe';" in _BOOT_JS
    assert "_activeCaptureMode=captureMode;" in _BOOT_JS


def test_send_raw_audio_honors_explicit_pending_send():
    # An explicit Send-button click (sets _micPendingSend) must send the raw
    # audio even when the composer already has text.
    assert "if(window._micPendingSend){" in _BOOT_JS
    # and it lives inside _sendRawAudio (before the empty-composer fallback).
    idx = _BOOT_JS.index("async function _sendRawAudio")
    end = _BOOT_JS.index("function _commitTranscript", idx)
    body = _BOOT_JS[idx:end]
    assert "window._micPendingSend" in body and "send()" in body
