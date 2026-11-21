"""#8826: hostile archive member names must be a specific 400, not a generic 500.

``extract_archive()`` promises ``ValueError`` for bad archives, and the upload
handlers already turn ``ValueError`` into a 400 / ``extract_error`` carrying the
message. But the per-member anchored creates raise raw filesystem exceptions:

  - ``FileExistsError`` (errno 17) for a duplicate member name, or an EMPTY
    member name (it resolves to the destination directory itself);
  - ``ENAMETOOLONG`` for an over-long member name (errno 63 on macOS, 36 on
    Linux -- production classification uses ``errno.ENAMETOOLONG``, never a
    literal);
  - a path-walk ``FileNotFoundError`` when an over-long DIRECTORY component
    makes the anchored walk fail: ``open_anchored_create_fd`` converts that
    ``OSError`` to ``FileNotFoundError("Not found: ...")``.

``/api/upload/extract`` answered 500 "Archive extraction failed" (logging a
traceback) and the workspace route answered a generic ``extract_error``.

These tests pin the specific ``ValueError`` messages on both routes, assert a
failed extraction leaves nothing behind, and keep a genuine missing-file
``FileNotFoundError`` unclassified (also a ``"Not found:"`` walk error without
an over-long component) so the mapping cannot swallow real failures. Controls:
normal archives still extract byte-identically, zip-slip stays blocked, and
unsupported formats stay rejected.

Measured on this machine (macOS): the over-long inputs below really do raise
``ENAMETOOLONG`` (errno 63) / the walk ``FileNotFoundError``; Linux CI reaches
the same branches with errno 36. The genuine-missing-file cases are injected
(``monkeypatch``) -- they are not producible by a hostile archive.
"""
import io
import json
import tarfile
import warnings
import zipfile
from types import SimpleNamespace

import pytest

import api.upload as upload

# A 305-char leaf fails the anchored leaf create with ENAMETOOLONG; a 260-char
# directory component fails inside the anchored walk (FileNotFoundError).
OVER_LONG_NAME = "x" * 305
OVER_LONG_DIR = "d" * 260

DUPLICATE_FRAGMENT = "duplicate file name"
TOO_LONG_FRAGMENT = "name too long"
EMPTY_NAME_FRAGMENT = "empty name"


def _make_zip(members):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        with warnings.catch_warnings():
            # Duplicate member names are exactly what the duplicate-name case
            # needs; zipfile's UserWarning is expected, not a defect.
            warnings.simplefilter("ignore", UserWarning)
            for name, data in members:
                zf.writestr(name, data)
    return buf.getvalue()


def _make_tar(members):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        for name, data in members:
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _extract_error(data, filename, workspace):
    with pytest.raises(ValueError) as excinfo:
        upload.extract_archive(data, filename, workspace)
    return str(excinfo.value)


class TestExtractArchiveClassifiesMemberFailures:
    """One case per failure row in the issue, straight through extract_archive."""

    def test_zip_duplicate_member_name(self, tmp_path):
        data = _make_zip([("a.txt", b"first"), ("a.txt", b"second")])

        message = _extract_error(data, "dup.zip", tmp_path)

        assert DUPLICATE_FRAGMENT in message
        assert "a.txt" in message
        assert not (tmp_path / "dup").exists()
        assert list(tmp_path.iterdir()) == []

    def test_zip_over_long_member_name(self, tmp_path):
        data = _make_zip([(OVER_LONG_NAME, b"x")])

        message = _extract_error(data, "long.zip", tmp_path)

        assert TOO_LONG_FRAGMENT in message
        assert not (tmp_path / "long").exists()
        assert list(tmp_path.iterdir()) == []

    def test_zip_over_long_directory_component(self, tmp_path):
        data = _make_zip([(f"{OVER_LONG_DIR}/f.txt", b"x")])

        message = _extract_error(data, "longdir.zip", tmp_path)

        assert TOO_LONG_FRAGMENT in message
        assert not (tmp_path / "longdir").exists()
        assert list(tmp_path.iterdir()) == []

    def test_tar_empty_member_name(self, tmp_path):
        data = _make_tar([("", b""), ("ok.txt", b"ok")])

        message = _extract_error(data, "empty.tar", tmp_path)

        assert EMPTY_NAME_FRAGMENT in message
        assert not (tmp_path / "empty").exists()
        assert list(tmp_path.iterdir()) == []

    def test_tar_over_long_member_name(self, tmp_path):
        data = _make_tar([(OVER_LONG_NAME, b"x")])

        message = _extract_error(data, "long.tar", tmp_path)

        assert TOO_LONG_FRAGMENT in message
        assert not (tmp_path / "long").exists()
        assert list(tmp_path.iterdir()) == []


class TestGenuineMissingFileIsNotMisclassified:
    """The classification must not turn a real missing-file failure into a
    400 blaming the member name."""

    def test_plain_file_not_found_propagates(self, tmp_path, monkeypatch):
        def missing(_root, _path):
            raise FileNotFoundError("genuinely missing")

        monkeypatch.setattr(upload, "open_anchored_create_fd", missing)

        with pytest.raises(FileNotFoundError):
            upload.extract_archive(_make_zip([("a.txt", b"x")]), "missing.zip", tmp_path)

    def test_walk_not_found_without_over_long_component_propagates(self, tmp_path, monkeypatch):
        # open_anchored_create_fd's walk marker without an over-long component:
        # a raced-out component, not a name problem -- must stay unclassified.
        def walk_missing(_root, path):
            raise FileNotFoundError(f"Not found: {path}")

        monkeypatch.setattr(upload, "open_anchored_create_fd", walk_missing)

        with pytest.raises(FileNotFoundError):
            upload.extract_archive(_make_zip([("a.txt", b"x")]), "walk.zip", tmp_path)


class TestExtractionControlsUnchanged:
    """Normal extraction, zip-slip and unsupported-format behaviour is identical."""

    def test_normal_zip_extracts_byte_identically(self, tmp_path):
        members = [
            ("top.txt", b"top"),
            ("d1/a.txt", b"alpha"),
            ("d1/d2/b.bin", bytes(range(256))),
        ]

        result = upload.extract_archive(_make_zip(members), "normal.zip", tmp_path)

        dest = tmp_path / "normal"
        assert result["extracted"] == 3
        assert sorted(result["files"]) == [
            "normal/d1/a.txt",
            "normal/d1/d2/b.bin",
            "normal/top.txt",
        ]
        for name, content in members:
            assert (dest / name).read_bytes() == content

    def test_normal_tar_extracts_byte_identically(self, tmp_path):
        members = [
            ("top.txt", b"top"),
            ("d1/a.txt", b"alpha"),
            ("d1/d2/b.bin", bytes(range(256))),
        ]

        result = upload.extract_archive(_make_tar(members), "normal.tar", tmp_path)

        dest = tmp_path / "normal"
        assert result["extracted"] == 3
        assert sorted(result["files"]) == [
            "normal/d1/a.txt",
            "normal/d1/d2/b.bin",
            "normal/top.txt",
        ]
        for name, content in members:
            assert (dest / name).read_bytes() == content

    def test_zip_slip_still_rejected(self, tmp_path):
        with pytest.raises(ValueError) as excinfo:
            upload.extract_archive(_make_zip([("../evil.txt", b"evil")]), "slip.zip", tmp_path)

        assert "Zip-slip blocked" in str(excinfo.value)
        assert not (tmp_path / "evil.txt").exists()
        assert not (tmp_path / "slip").exists()

    def test_unsupported_format_still_rejected(self, tmp_path):
        with pytest.raises(ValueError) as excinfo:
            upload.extract_archive(b"not really an archive", "thing.rar", tmp_path)

        assert "Unsupported archive format" in str(excinfo.value)
        assert list(tmp_path.iterdir()) == []


class _Handler:
    """Minimal BaseHTTPRequestHandler stand-in for in-process handler calls."""

    def __init__(self):
        self.status = None
        self.headers = {
            "Content-Type": "multipart/form-data; boundary=x8826",
            "Content-Length": "1",
        }
        self.rfile = io.BytesIO(b"")
        self.wfile = io.BytesIO()
        self.command = "POST"
        self.path = "/"
        self.client_address = ("127.0.0.1", 1)

    def send_response(self, status):
        self.status = status

    def send_header(self, *_args):
        pass

    def end_headers(self):
        pass

    def json_body(self):
        return json.loads(self.wfile.getvalue().decode("utf-8"))


def _drive_route(route, filename, data, tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    attachments = tmp_path / "attachments"
    attachments.mkdir()
    session = SimpleNamespace(workspace=workspace, profile="default")

    monkeypatch.setattr(
        upload,
        "parse_multipart",
        lambda *_args, **_kwargs: (
            {"session_id": "session-8826", "path": ""},
            {"file": (filename, data)},
        ),
    )
    monkeypatch.setattr(upload, "get_session", lambda _sid: session)
    monkeypatch.setattr(upload, "_reject_invisible_session", lambda *_args: False)
    monkeypatch.setattr(upload, "_reject_invisible_session_before_load", lambda *_args: False)
    monkeypatch.setattr(upload, "_attachment_root", lambda: attachments.resolve())
    monkeypatch.setattr(
        upload, "resolve_trusted_workspace", lambda *_args, **_kwargs: workspace.resolve()
    )

    handler = _Handler()
    if route == "upload_extract":
        upload.handle_upload_extract(handler)
    else:
        upload.handle_workspace_upload(handler)
    return handler, workspace, attachments


FAILURE_CASES = [
    pytest.param(
        "dup.zip",
        _make_zip([("a.txt", b"1"), ("a.txt", b"2")]),
        DUPLICATE_FRAGMENT,
        id="zip-duplicate-member",
    ),
    pytest.param(
        "long.zip", _make_zip([(OVER_LONG_NAME, b"x")]), TOO_LONG_FRAGMENT, id="zip-long-name"
    ),
    pytest.param(
        "longdir.zip",
        _make_zip([(f"{OVER_LONG_DIR}/f.txt", b"x")]),
        TOO_LONG_FRAGMENT,
        id="zip-long-dir-component",
    ),
    pytest.param(
        "empty.tar",
        _make_tar([("", b""), ("ok.txt", b"ok")]),
        EMPTY_NAME_FRAGMENT,
        id="tar-empty-name",
    ),
    pytest.param(
        "long.tar", _make_tar([(OVER_LONG_NAME, b"x")]), TOO_LONG_FRAGMENT, id="tar-long-name"
    ),
]


class TestUploadExtractRoute:
    """POST /api/upload/extract: a specific 400, no leftover files."""

    @pytest.mark.parametrize("filename, data, fragment", FAILURE_CASES)
    def test_returns_400_with_specific_message(self, filename, data, fragment, tmp_path, monkeypatch):
        handler, _workspace, attachments = _drive_route(
            "upload_extract", filename, data, tmp_path, monkeypatch
        )

        body = handler.json_body()
        assert handler.status == 400, f"expected 400, got {handler.status}: {body}"
        assert fragment in body["error"], body
        assert list((attachments / "session-8826").iterdir()) == []


class TestWorkspaceUploadRoute:
    """Workspace upload: a specific extract_error, archive removed, no leftovers."""

    @pytest.mark.parametrize("filename, data, fragment", FAILURE_CASES)
    def test_reports_specific_extract_error(self, filename, data, fragment, tmp_path, monkeypatch):
        handler, workspace, _attachments = _drive_route(
            "workspace_upload", filename, data, tmp_path, monkeypatch
        )

        body = handler.json_body()
        assert handler.status == 200, body
        assert fragment in body["extract_error"], body
        assert list(workspace.iterdir()) == []
