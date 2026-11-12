import io
import json
import sys
import types

import pytest

import api.upload as upload
from api.upload import (
    _stt_provider_capability_from_module,
    handle_transcribe,
    handle_transcribe_capability,
)


def _multipart_body(fields=None, files=None, boundary=b"voiceboundary"):
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
            f'Content-Type: {content_type}\r\n\r\n'
        ).encode()
        body += data + b"\r\n"
    body += b"--" + boundary + b"--\r\n"
    return body, f"multipart/form-data; boundary={boundary.decode()}"


class _FakeHandler:
    def __init__(self, body: bytes, content_type: str):
        self.rfile = io.BytesIO(body)
        self.wfile = io.BytesIO()
        self.headers = {
            "Content-Type": content_type,
            "Content-Length": str(len(body)),
        }
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


def _install_fake_transcription_tools(monkeypatch, fake_mod):
    monkeypatch.setitem(sys.modules, "tools.transcription_tools", fake_mod)
    tools_pkg = sys.modules.get("tools")
    if tools_pkg is not None:
        monkeypatch.setattr(tools_pkg, "transcription_tools", fake_mod, raising=False)


def test_handle_transcribe_requires_file_field():
    body, content_type = _multipart_body(fields={"note": "missing file"})
    handler = _FakeHandler(body, content_type)
    handle_transcribe(handler)
    assert handler.status == 400
    assert handler.payload()["error"] == "No file field in request"


def test_handle_transcribe_returns_transcript(monkeypatch):
    fake_mod = types.ModuleType("tools.transcription_tools")
    fake_mod.transcribe_audio = lambda path: {"success": True, "transcript": "hello from audio"}
    _install_fake_transcription_tools(monkeypatch, fake_mod)

    body, content_type = _multipart_body(
        files={"file": ("voice.webm", b"RIFFfakeaudio", "audio/webm")}
    )
    handler = _FakeHandler(body, content_type)
    handle_transcribe(handler)

    assert handler.status == 200
    assert handler.payload() == {"ok": True, "transcript": "hello from audio"}


def test_handle_transcribe_surfaces_provider_error(monkeypatch):
    fake_mod = types.ModuleType("tools.transcription_tools")
    fake_mod.transcribe_audio = lambda path: {"success": False, "error": "STT not configured"}
    _install_fake_transcription_tools(monkeypatch, fake_mod)

    body, content_type = _multipart_body(
        files={"file": ("voice.webm", b"RIFFfakeaudio", "audio/webm")}
    )
    handler = _FakeHandler(body, content_type)
    handle_transcribe(handler)

    assert handler.status == 503
    assert handler.payload()["error"] == "STT not configured"


def test_handle_transcribe_treats_no_speech_as_empty_success(monkeypatch):
    """A provider that exits cleanly with no transcript found no speech.

    transcription_command.py reports it with this message only after the exit
    code has been checked, so it cannot mean "the provider crashed". Answering
    400 made the composer report a failure for a silent clip.
    """
    fake_mod = types.ModuleType("tools.transcription_tools")
    fake_mod.transcribe_audio = lambda path: {
        "success": False,
        "error": (
            "Command STT provider wrote no output file at /tmp/stt-out/x.txt "
            "and produced no stdout"
        ),
    }
    _install_fake_transcription_tools(monkeypatch, fake_mod)

    body, content_type = _multipart_body(
        files={"file": ("voice.webm", b"RIFFfakeaudio", "audio/webm")}
    )
    handler = _FakeHandler(body, content_type)
    handle_transcribe(handler)

    assert handler.status == 200
    assert handler.payload() == {"ok": True, "transcript": "", "no_speech": True}


def test_handle_transcribe_keeps_provider_crash_as_error(monkeypatch):
    """A non-zero exit is a real failure, not silence: keep it a 4xx."""
    fake_mod = types.ModuleType("tools.transcription_tools")
    fake_mod.transcribe_audio = lambda path: {
        "success": False,
        "error": "STT command provider 'stt-gw' exited with code 1: boom",
    }
    _install_fake_transcription_tools(monkeypatch, fake_mod)

    body, content_type = _multipart_body(
        files={"file": ("voice.webm", b"RIFFfakeaudio", "audio/webm")}
    )
    handler = _FakeHandler(body, content_type)
    handle_transcribe(handler)

    assert handler.status == 400
    assert "exited with code 1" in handler.payload()["error"]


@pytest.mark.parametrize(
    "error",
    [
        # Review 5674873058: a real nonzero exit whose stderr happens to quote
        # the clean-run wording was reported as a successful silent clip.
        "STT command provider 'review-fixture' exited with code 7: stderr: produced no stdout",
        "STT command provider 'x' exited with code 7: stderr: wrote no output file",
        "Command provider crashed\nTraceback: produced no stdout",
        # Same words, but not the producer's clean-run message.
        "stderr: Command STT provider wrote no output file at /tmp/x.txt and produced no stdout",
        "warning: produced no stdout",
        "Command STT provider wrote no output file\nand produced no stdout",
    ],
)
def test_handle_transcribe_keeps_marker_collisions_as_errors(monkeypatch, error):
    fake_mod = types.ModuleType("tools.transcription_tools")
    fake_mod.transcribe_audio = lambda path: {"success": False, "error": error}
    _install_fake_transcription_tools(monkeypatch, fake_mod)

    body, content_type = _multipart_body(
        files={"file": ("voice.webm", b"RIFFfakeaudio", "audio/webm")}
    )
    handler = _FakeHandler(body, content_type)
    handle_transcribe(handler)

    assert handler.status == 400, "a crashed provider is not a silent clip"
    assert handler.payload().get("no_speech") is None
    assert handler.payload()["error"] == error


def test_is_no_speech_error_matches_only_the_producers_complete_message():
    # The exact message transcription_tools raises on a clean exit with no output.
    assert upload._is_no_speech_error(
        "Command STT provider wrote no output file at /tmp/stt-out/x.txt and produced no stdout"
    )
    assert upload._is_no_speech_error(
        "  Command STT provider wrote no output file at /tmp/stt-out/x.txt "
        "and produced no stdout.\n"
    )
    assert not upload._is_no_speech_error("")
    assert not upload._is_no_speech_error("STT not configured")
    assert not upload._is_no_speech_error(
        "exited with code 0: stderr: produced no stdout"
    )
    assert not upload._is_no_speech_error("produced no stdout")
    assert not upload._is_no_speech_error("wrote no output file")


def test_handle_transcribe_reports_clean_empty_run_as_no_speech(monkeypatch):
    # The positive side of the contract: the producer's own clean-run message is
    # still answered as a successful empty transcript.
    fake_mod = types.ModuleType("tools.transcription_tools")
    fake_mod.transcribe_audio = lambda path: {
        "success": False,
        "error": "Command STT provider wrote no output file at /tmp/stt-out/x.txt "
                 "and produced no stdout",
    }
    _install_fake_transcription_tools(monkeypatch, fake_mod)

    body, content_type = _multipart_body(
        files={"file": ("voice.webm", b"RIFFfakeaudio", "audio/webm")}
    )
    handler = _FakeHandler(body, content_type)
    handle_transcribe(handler)

    assert handler.status == 200
    assert handler.payload() == {"ok": True, "transcript": "", "no_speech": True}


def test_handle_transcribe_capability_reports_unavailable_without_provider(monkeypatch):
    monkeypatch.setattr(upload, "_stt_provider_capability", lambda: (False, "none"))

    handler = _FakeHandler(b"", "")
    handle_transcribe_capability(handler)

    assert handler.status == 200
    assert handler.payload()["ok"] is True
    assert handler.payload()["available"] is False

def test_handle_transcribe_capability_reports_available_provider(monkeypatch):
    monkeypatch.setattr(upload, "_stt_provider_capability", lambda: (True, "openai"))

    handler = _FakeHandler(b"", "")
    handle_transcribe_capability(handler)

    assert handler.status == 200
    assert handler.payload()["ok"] is True
    assert handler.payload()["available"] is True
    assert handler.payload()["provider"] == "openai"

def test_handle_transcribe_capability_requires_ffmpeg_for_local_command_webm(monkeypatch):
    fake_mod = types.ModuleType("tools.transcription_tools")
    fake_mod.__dict__.update(
        {
            "_load_stt_config": lambda: {"provider": "local_command"},
            "is_stt_enabled": lambda cfg=None: True,
            "_HAS_FASTER_WHISPER": False,
            "_HAS_OPENAI": False,
            "_HAS_MISTRAL": False,
            "_has_local_command": lambda: True,
            "_find_ffmpeg_binary": lambda: None,
        }
    )
    available, provider = _stt_provider_capability_from_module(fake_mod)

    assert available is False
    assert provider == "local_command"


def test_handle_transcribe_capability_reports_actual_local_command_fallback(monkeypatch):
    fake_mod = types.ModuleType("tools.transcription_tools")
    fake_mod.__dict__.update(
        {
            "_load_stt_config": lambda: {"provider": "local"},
            "is_stt_enabled": lambda cfg=None: True,
            "_HAS_FASTER_WHISPER": False,
            "_HAS_OPENAI": False,
            "_HAS_MISTRAL": False,
            "_has_local_command": lambda: True,
            "_find_ffmpeg_binary": lambda: "/usr/bin/ffmpeg",
        }
    )
    available, provider = _stt_provider_capability_from_module(fake_mod)

    assert available is True
    assert provider == "local_command"


def test_handle_transcribe_capability_reports_named_command_provider(monkeypatch):
    stt_config = {
        "provider": "elevenlabs-gemini",
        "providers": {
            "elevenlabs-gemini": {
                "type": "command",
                "command": "/home/user/.hermes/scripts/hermes-elevenlabs-stt.sh {input}",
            }
        },
    }
    fake_mod = types.ModuleType("tools.transcription_tools")
    fake_mod.__dict__.update(
        {
            "_load_stt_config": lambda: stt_config,
            "is_stt_enabled": lambda cfg=None: True,
            "_HAS_FASTER_WHISPER": False,
            "_HAS_OPENAI": False,
            "_HAS_MISTRAL": False,
            "_resolve_command_stt_provider_config": lambda provider, cfg: cfg["providers"].get(provider),
        }
    )

    available, provider = _stt_provider_capability_from_module(fake_mod)

    assert available is True
    assert provider == "elevenlabs-gemini"
