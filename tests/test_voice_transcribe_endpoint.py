import io
import json
import sys
import types

from api.upload import handle_transcribe


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
            f"Content-Type: {content_type}\r\n\r\n"
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


def test_handle_transcribe_requires_file_field():
    body, content_type = _multipart_body(fields={"note": "missing file"})
    handler = _FakeHandler(body, content_type)
    handle_transcribe(handler)
    assert handler.status == 400
    assert handler.payload()["error"] == "No file field in request"


def test_handle_transcribe_returns_transcript(monkeypatch):
    fake_mod = types.ModuleType("tools.transcription_tools")
    fake_mod.transcribe_audio = lambda path: {
        "success": True,
        "transcript": "hello from audio",
    }
    monkeypatch.setitem(sys.modules, "tools.transcription_tools", fake_mod)

    body, content_type = _multipart_body(
        files={"file": ("voice.webm", b"RIFFfakeaudio", "audio/webm")}
    )
    handler = _FakeHandler(body, content_type)
    handle_transcribe(handler)

    assert handler.status == 200
    assert handler.payload() == {"ok": True, "transcript": "hello from audio"}


def test_handle_transcribe_surfaces_provider_error(monkeypatch):
    fake_mod = types.ModuleType("tools.transcription_tools")
    fake_mod.transcribe_audio = lambda path: {
        "success": False,
        "error": "STT not configured",
    }
    monkeypatch.setitem(sys.modules, "tools.transcription_tools", fake_mod)

    body, content_type = _multipart_body(
        files={"file": ("voice.webm", b"RIFFfakeaudio", "audio/webm")}
    )
    handler = _FakeHandler(body, content_type)
    handle_transcribe(handler)

    assert handler.status == 503
    assert handler.payload()["error"] == "STT not configured"
