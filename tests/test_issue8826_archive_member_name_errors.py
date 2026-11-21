"""#8826: duplicate, empty and over-long archive member names get a clear 400.

They used to escape ``extract_archive()`` as FileExistsError / OSError /
FileNotFoundError, so ``/api/upload/extract`` answered 500 "Archive extraction
failed" and the workspace upload showed the same generic ``extract_error``.
"""
import io
import json
import tarfile
import zipfile
from types import SimpleNamespace

import pytest

import api.upload as upload


def _zip(names):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name in names:
            zf.writestr(name, "x")
    return buf.getvalue()


def _tar(names):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        for name in names:
            info = tarfile.TarInfo(name)
            info.size = 1
            tf.addfile(info, io.BytesIO(b"x"))
    return buf.getvalue()


CASES = [
    ("dup.zip", lambda: _zip(["a.txt", "a.txt"]), "Archive contains a duplicate file name: a.txt"),
    ("long.zip", lambda: _zip(["a" * 305]), "Archive member name too long: "),
    ("longdir.zip", lambda: _zip(["d" * 260 + "/f.txt"]), "Archive member name too long: "),
    ("empty.tar", lambda: _tar(["", "ok.txt"]), "Archive member has an empty name"),
    ("long.tar", lambda: _tar(["a" * 305]), "Archive member name too long: "),
]


@pytest.fixture(autouse=True)
def _quiet_duplicate_zip_warning():
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        yield


@pytest.mark.parametrize("filename, make, message", CASES)
def test_extract_archive_raises_a_clear_value_error(tmp_path, filename, make, message):
    with pytest.raises(ValueError) as exc:
        upload.extract_archive(make(), filename, tmp_path)
    assert str(exc.value).startswith(message)
    assert len(str(exc.value)) < 140, "long names are shortened in the message"
    assert list(tmp_path.iterdir()) == [], "partial extraction is still cleaned up"


def test_good_archives_and_long_but_legal_names_still_extract(tmp_path):
    name = "n" * 255  # exactly at the usual limit
    result = upload.extract_archive(_zip(["a.txt", "dir/b.txt", name]), "ok.zip", tmp_path)
    assert result["extracted"] == 3
    result = upload.extract_archive(_tar(["a.txt", "dir/b.txt"]), "ok.tar", tmp_path)
    assert result["extracted"] == 2


class _Handler:
    def __init__(self):
        self.status = None
        self.rfile = io.BytesIO()
        self.wfile = io.BytesIO()
        self.headers = {"Content-Type": "multipart/form-data; boundary=t", "Content-Length": "0"}

    def send_response(self, status):
        self.status = status

    def send_header(self, *_args):
        pass

    def end_headers(self):
        pass

    def body(self):
        return json.loads(self.wfile.getvalue().decode("utf-8"))


def _payload(monkeypatch, filename, data, fields=None):
    monkeypatch.setattr(
        upload,
        "parse_multipart",
        lambda *_a, **_k: (fields or {"session_id": "s1"}, {"file": (filename, data)}),
    )


@pytest.mark.parametrize("filename, make, message", CASES)
def test_upload_extract_route_returns_400_with_the_reason(monkeypatch, tmp_path, filename, make, message):
    attach = tmp_path / "attach"
    monkeypatch.setattr(upload, "get_session", lambda _sid: SimpleNamespace(profile="default"))
    monkeypatch.setattr(upload, "_reject_invisible_session", lambda *_a: False)
    monkeypatch.setattr(upload, "_session_attachment_dir", lambda _sid: attach)
    _payload(monkeypatch, filename, make())

    handler = _Handler()
    upload.handle_upload_extract(handler)

    assert handler.status == 400
    assert handler.body()["error"].startswith(message)
    assert list(attach.iterdir()) == []


@pytest.mark.parametrize("filename, make, message", CASES)
def test_workspace_upload_reports_the_reason(monkeypatch, tmp_path, filename, make, message):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    session = SimpleNamespace(workspace=workspace, profile="default")
    monkeypatch.setattr(upload, "get_session", lambda _sid: session)
    monkeypatch.setattr(upload, "_reject_invisible_session", lambda *_a: False)
    monkeypatch.setattr(upload, "resolve_trusted_workspace", lambda path, **_kw: path)
    _payload(monkeypatch, filename, make(), {"session_id": "s1", "path": "", "extract": "1"})

    handler = _Handler()
    upload.handle_workspace_upload(handler)

    body = handler.body()
    entry = body["files"][0] if "files" in body else body
    assert entry["extracted"] is False
    assert entry["extract_error"].startswith(message)
    assert list(workspace.iterdir()) == [], "archive and partial folder are removed"
