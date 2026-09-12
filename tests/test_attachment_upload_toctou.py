"""TOCTOU hardening for chat-attachment uploads.

handle_upload deduped filenames with an exists() check and then wrote with
plain write_bytes — two concurrent uploads of the same name could both pass
the check and the last writer silently won. These tests pin the #3398-style
anchored O_CREAT|O_EXCL|O_NOFOLLOW creation semantics (mirrored from the
workspace upload path): a raced duplicate must 409 instead of overwriting,
and the non-raced happy path / dedup behavior must stay unchanged.

Test-pattern cribbed from tests/test_raw_audio_upload.py (real multipart body
through parse_multipart + fake handler) and
tests/test_session_active_profile_authorization.py (monkeypatched
get_session / _get_active_profile_name).
"""
import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import api.upload as upload
from api.upload import handle_upload


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


@pytest.fixture()
def attachment_env(tmp_path, monkeypatch):
    """Isolate the attachment inbox and stub the session lookup."""
    root = tmp_path / "attachments"
    monkeypatch.setenv("HERMES_WEBUI_ATTACHMENT_DIR", str(root))
    monkeypatch.setattr(
        upload,
        "get_session",
        lambda sid: SimpleNamespace(session_id=sid, profile=None),
    )
    monkeypatch.setattr(upload, "_get_active_profile_name", lambda: "default")
    return root


def test_raced_duplicate_returns_409_and_preserves_existing_bytes(attachment_env, monkeypatch):
    """An attacker winning the exists()-check race must not get its bytes written.

    Simulates the race deterministically: the destination file already exists
    with known content, and _upload_destination returns that exact path as if
    the dedup check had just passed. The anchored O_EXCL create must fail with
    FileExistsError -> 409, and the pre-existing bytes must be untouched.
    """
    session_id = "race-sess"
    dest_dir = upload._session_attachment_dir(session_id)
    dest_dir.mkdir(parents=True, exist_ok=True)
    target = dest_dir / "report.txt"
    target.write_bytes(b"ORIGINAL-CONTENT")

    monkeypatch.setattr(
        upload,
        "_upload_destination",
        lambda session_id, safe_name, dest_dir=None: target,
    )

    body, content_type = _multipart_body(
        fields={"session_id": session_id},
        files={"file": ("report.txt", b"ATTACKER-BYTES", "text/plain")},
    )
    handler = _FakeHandler(body, content_type)
    handle_upload(handler)

    assert handler.status == 409
    assert handler.payload() == {
        "error": "Upload destination already exists: report.txt"
    }
    assert target.read_bytes() == b"ORIGINAL-CONTENT"


def test_new_upload_happy_path_unchanged(attachment_env):
    """A normal upload of a fresh name keeps the exact response shape."""
    body, content_type = _multipart_body(
        fields={"session_id": "happy-sess"},
        files={"file": ("notes.txt", b"hello world", "text/plain")},
    )
    handler = _FakeHandler(body, content_type)
    handle_upload(handler)

    assert handler.status == 200
    payload = handler.payload()
    assert set(payload) == {"filename", "path", "size", "mime", "is_image"}
    assert payload["filename"] == "notes.txt"
    assert payload["mime"].startswith("text/")
    assert payload["is_image"] is False
    assert payload["size"] == len(b"hello world")

    dest = attachment_env / "happy-sess" / "notes.txt"
    assert Path(payload["path"]) == dest.resolve()
    assert dest.read_bytes() == b"hello world"


def test_non_raced_dedup_still_suffixed(attachment_env):
    """Uploading an existing name (no race) still picks the -1 suffixed name."""
    body1, ctype1 = _multipart_body(
        fields={"session_id": "dedup-sess"},
        files={"file": ("dup.txt", b"first", "text/plain")},
    )
    h1 = _FakeHandler(body1, ctype1)
    handle_upload(h1)
    assert h1.status == 200

    body2, ctype2 = _multipart_body(
        fields={"session_id": "dedup-sess"},
        files={"file": ("dup.txt", b"second", "text/plain")},
    )
    h2 = _FakeHandler(body2, ctype2)
    handle_upload(h2)
    assert h2.status == 200
    assert h2.payload()["filename"] == "dup-1.txt"

    dest_dir = upload._session_attachment_dir("dedup-sess")
    assert (dest_dir / "dup.txt").read_bytes() == b"first"
    assert (dest_dir / "dup-1.txt").read_bytes() == b"second"
