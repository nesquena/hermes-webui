"""#8826 follow-up: an archive member name is limited in BYTES, not characters.

``_create_archive_member_fd`` tells an over-long path component apart from a
genuine missing file by re-checking the member name: the anchored walk
(``open_anchored_create_fd``) converts the walk's ``OSError`` -- including
``ENAMETOOLONG`` -- into ``FileNotFoundError('Not found: ...')``, so the errno
is gone by the time the upload layer sees it. The re-check therefore has to
answer "is one component over the limit?" itself.

That limit is a byte limit: Linux caps one path component at 255 BYTES
(``NAME_MAX``), while macOS caps it at 255 CHARACTERS. The character-based
check (``len(part) > 255``) lets a multibyte component through -- 128 ``é``
characters are 256 UTF-8 bytes -- so on Linux such a member reaches the walk,
fails with ``ENAMETOOLONG``, and is re-raised unclassified as
``FileNotFoundError``. ``/api/upload/extract`` then answers 500 "Archive
extraction failed" and the workspace route answers a generic ``extract_error``
instead of the specific "name too long" reason.

The length must be measured the way the filesystem measures it:
``len(os.fsencode(part))``.

macOS cannot produce the failure naturally (its limit is per character), so
these tests drive a CONTROLLED filesystem stub: the stub raises the real
``ENAMETOOLONG`` (``errno.ENAMETOOLONG``) for any component whose encoded
length exceeds the limit, exactly like Linux. No archive sample is crafted to
trigger the old behaviour -- the stub decides what the filesystem does.

Three boundaries are pinned:
  - a component over the byte limit is classified as "name too long";
  - a component exactly AT the limit is not rejected for length;
  - a genuine missing path that is under the limit keeps its original
    (unclassified) handling.

The stub is deliberately narrow: it only replaces the filesystem calls the
anchored walk makes (``os.open`` / ``os.mkdir``) and delegates everything
else to the real filesystem, so normal extraction still lands real bytes.
"""
import errno
import io
import os
import tarfile
import warnings
import zipfile
from types import SimpleNamespace

import pytest

import api.upload as upload

# Linux NAME_MAX: one path component may not exceed 255 bytes.
BYTE_LIMIT = 255

# 128 "é" characters = 256 UTF-8 bytes: under the CHARACTER limit (255) and
# over the BYTE limit (255). This is the case the character check misses.
MULTIBYTE_DIR = "é" * 128
# Exactly at the byte limit: 85 "é" characters = 170 bytes; 255 ASCII chars
# = 255 bytes exactly.
AT_LIMIT_ASCII = "a" * BYTE_LIMIT
OVER_LIMIT_ASCII = "a" * (BYTE_LIMIT + 1)

TOO_LONG_FRAGMENT = "name too long"


def _make_zip(members):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        with warnings.catch_warnings():
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


def _byte_len(name: str) -> int:
    return len(os.fsencode(name))


class _ByteLimitFilesystem:
    """Controlled filesystem stub with Linux's per-component BYTE limit.

    ``os.open`` / ``os.mkdir`` raise the real ``ENAMETOOLONG`` for any path
    component whose encoded length exceeds ``limit`` bytes. Every other call
    goes to the real filesystem, so the extraction that follows writes real
    bytes to disk.

    ``limit`` is the byte limit; ``None`` disables the limit (the host's own
    limit applies).
    """

    def __init__(self, limit=None, enforce=True):
        self.limit = limit
        self.enforce = enforce
        self.checked = []

    def _over_limit(self, part: str) -> bool:
        if not self.enforce or self.limit is None:
            return False
        return _byte_len(part) > self.limit

    def install(self, monkeypatch):
        real_open = os.open
        real_mkdir = os.mkdir

        def fake_open(path, flags, *args, **kwargs):
            part = os.path.basename(os.fsdecode(path))
            self.checked.append(part)
            if self._over_limit(part):
                raise OSError(errno.ENAMETOOLONG, os.strerror(errno.ENAMETOOLONG), path)
            return real_open(path, flags, *args, **kwargs)

        def fake_mkdir(path, *args, **kwargs):
            part = os.path.basename(os.fsdecode(path))
            self.checked.append(part)
            if self._over_limit(part):
                raise OSError(errno.ENAMETOOLONG, os.strerror(errno.ENAMETOOLONG), path)
            return real_mkdir(path, *args, **kwargs)

        monkeypatch.setattr(upload.os, "open", fake_open)
        monkeypatch.setattr(upload.os, "mkdir", fake_mkdir)
        return self


class TestMemberNameLengthIsMeasuredInBytes:
    """The over-long-component classification must use the ENCODED length."""

    def test_multibyte_component_over_byte_limit_is_too_long(self, tmp_path, monkeypatch):
        # 128 "é" = 256 bytes: over the byte limit, under the character limit.
        assert len(MULTIBYTE_DIR) <= BYTE_LIMIT
        assert _byte_len(MULTIBYTE_DIR) > BYTE_LIMIT

        stub = _ByteLimitFilesystem(limit=BYTE_LIMIT).install(monkeypatch)

        message = _extract_error(
            _make_zip([(f"{MULTIBYTE_DIR}/f.txt", b"x")]), "mb.zip", tmp_path
        )

        assert TOO_LONG_FRAGMENT in message, message
        assert stub.checked, "the stub never saw a path component"
        assert not (tmp_path / "mb").exists()
        assert list(tmp_path.iterdir()) == []

    def test_multibyte_component_over_byte_limit_is_too_long_in_tar(self, tmp_path, monkeypatch):
        assert _byte_len(MULTIBYTE_DIR) > BYTE_LIMIT

        stub = _ByteLimitFilesystem(limit=BYTE_LIMIT).install(monkeypatch)

        message = _extract_error(
            _make_tar([(f"{MULTIBYTE_DIR}/f.txt", b"x")]), "mb.tar", tmp_path
        )

        assert TOO_LONG_FRAGMENT in message, message
        assert stub.checked
        assert not (tmp_path / "mb").exists()
        assert list(tmp_path.iterdir()) == []

    def test_component_exactly_at_byte_limit_is_not_rejected(self, tmp_path, monkeypatch):
        # Exactly at the limit (255 bytes): must NOT be refused for length.
        assert _byte_len(AT_LIMIT_ASCII) == BYTE_LIMIT

        stub = _ByteLimitFilesystem(limit=BYTE_LIMIT).install(monkeypatch)

        result = upload.extract_archive(
            _make_zip([(f"{AT_LIMIT_ASCII}/f.txt", b"ok")]), "atlimit.zip", tmp_path
        )

        assert result["extracted"] == 1
        dest = tmp_path / "atlimit"
        assert (dest / AT_LIMIT_ASCII / "f.txt").read_bytes() == b"ok"
        assert stub.checked

    def test_component_under_limit_with_multibyte_name_extracts(self, tmp_path, monkeypatch):
        # 84 "é" = 168 bytes: under the byte limit -> normal extraction.
        short_multibyte = "é" * 84
        assert _byte_len(short_multibyte) <= BYTE_LIMIT

        stub = _ByteLimitFilesystem(limit=BYTE_LIMIT).install(monkeypatch)

        result = upload.extract_archive(
            _make_zip([(f"{short_multibyte}/f.txt", b"ok")]), "short.zip", tmp_path
        )

        assert result["extracted"] == 1
        assert (tmp_path / "short" / short_multibyte / "f.txt").read_bytes() == b"ok"
        assert stub.checked

    def test_multibyte_leaf_over_byte_limit_is_too_long(self, tmp_path, monkeypatch):
        # The leaf create fails the same way (ENAMETOOLONG), so the leaf name
        # must be measured in bytes too.
        assert _byte_len(MULTIBYTE_DIR) > BYTE_LIMIT

        stub = _ByteLimitFilesystem(limit=BYTE_LIMIT).install(monkeypatch)

        message = _extract_error(_make_zip([(MULTIBYTE_DIR, b"x")]), "mbleaf.zip", tmp_path)

        assert TOO_LONG_FRAGMENT in message, message
        assert stub.checked
        assert list(tmp_path.iterdir()) == []


class TestGenuineMissingPathStaysUnclassified:
    """A real missing path under the byte limit keeps the original handling."""

    def test_missing_path_under_limit_is_not_a_name_problem(self, tmp_path, monkeypatch):
        def missing(_root, _path):
            raise FileNotFoundError("genuinely missing")

        monkeypatch.setattr(upload, "open_anchored_create_fd", missing)

        with pytest.raises(FileNotFoundError) as excinfo:
            upload.extract_archive(_make_zip([("a.txt", b"x")]), "missing.zip", tmp_path)

        assert TOO_LONG_FRAGMENT not in str(excinfo.value)
        assert not (tmp_path / "missing").exists()

    def test_walk_not_found_without_over_long_component_stays_unclassified(
        self, tmp_path, monkeypatch
    ):
        # The walk's "Not found:" marker WITHOUT an over-long component: a
        # raced-out component, not a name problem.
        def walk_missing(_root, path):
            raise FileNotFoundError(f"Not found: {path}")

        monkeypatch.setattr(upload, "open_anchored_create_fd", walk_missing)

        with pytest.raises(FileNotFoundError) as excinfo:
            upload.extract_archive(_make_zip([("a.txt", b"x")]), "walk.zip", tmp_path)

        assert TOO_LONG_FRAGMENT not in str(excinfo.value)

    def test_walk_not_found_with_multibyte_component_under_byte_limit_stays_unclassified(
        self, tmp_path, monkeypatch
    ):
        # Multibyte but UNDER the byte limit: still not a name problem.
        short_multibyte = "é" * 84
        assert _byte_len(short_multibyte) <= BYTE_LIMIT

        def walk_missing(_root, path):
            raise FileNotFoundError(f"Not found: {path}")

        monkeypatch.setattr(upload, "open_anchored_create_fd", walk_missing)

        with pytest.raises(FileNotFoundError) as excinfo:
            upload.extract_archive(
                _make_zip([(f"{short_multibyte}/f.txt", b"x")]), "walkmb.zip", tmp_path
            )

        assert TOO_LONG_FRAGMENT not in str(excinfo.value)


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
        import json

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


class TestRouteContractsForByteLimitedNames:
    """Both callers keep their error contract for a byte-over-long name."""

    def test_upload_extract_returns_400_with_reason(self, tmp_path, monkeypatch):
        stub = _ByteLimitFilesystem(limit=BYTE_LIMIT).install(monkeypatch)

        handler, _workspace, attachments = _drive_route(
            "upload_extract", "mb.zip", _make_zip([(f"{MULTIBYTE_DIR}/f.txt", b"x")]),
            tmp_path, monkeypatch,
        )

        body = handler.json_body()
        assert handler.status == 400, f"expected 400, got {handler.status}: {body}"
        assert TOO_LONG_FRAGMENT in body["error"], body
        assert stub.checked
        assert list((attachments / "session-8826").iterdir()) == []

    def test_workspace_upload_reports_extract_error_with_reason(self, tmp_path, monkeypatch):
        stub = _ByteLimitFilesystem(limit=BYTE_LIMIT).install(monkeypatch)

        handler, workspace, _attachments = _drive_route(
            "workspace_upload", "mb.zip", _make_zip([(f"{MULTIBYTE_DIR}/f.txt", b"x")]),
            tmp_path, monkeypatch,
        )

        body = handler.json_body()
        assert handler.status == 200, body
        assert TOO_LONG_FRAGMENT in body["extract_error"], body
        assert stub.checked
        assert list(workspace.iterdir()) == []
