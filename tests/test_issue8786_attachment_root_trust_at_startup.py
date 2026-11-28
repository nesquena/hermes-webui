"""#8786 items 1-2: uploads and the vision allow-check must trust the attachment
inbox as resolved at server startup, never re-resolve it by name.

#8780 fixed the public-share reader of the inbox root (#8590): shares now
trust the root through the startup-resolved helper and walk it no-follow.
The two other readers still resolve it by name on every call:

1. Uploads (``api/upload.py:_session_attachment_dir``) -- if something
   replaces the inbox, or one of its ancestors, with a symlink, later
   uploads land wherever the symlink points (the write-side twin of #8590).
   In the default layout the inbox sits under HOME, so anything able to
   write there can do it.
2. The vision allow-check (``api/streaming.py:_build_native_multimodal_message``)
   -- a redirected inbox widens which local files may be read into the
   model's context.

Both must use the startup-trusted root and fail closed: the upload is
refused instead of written through the redirect, and the vision check
stops allowing paths under the redirected location.  The control cases pin
that the normal paths keep working: an upload still lands in the real
inbox, an image inside the inbox still embeds, and an inbox configured
through a symlink on purpose still resolves (once, at startup).

Out of scope for this slice (separate follow-ups, see issue #8786):
hardlinked files (item 3), import-order pinning (item 4), and settings
that legitimately change at runtime (item 5).

Test patterns follow tests/test_issue8590_share_root_trust.py (real
directories, real symlinks, real redirect) and
tests/test_attachment_upload_toctou.py (real multipart body through
``handle_upload`` with a fake handler).
"""
from __future__ import annotations

import errno
import io
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

import api.upload as upload
from api.routes import _normalize_chat_attachments
from api.streaming import _build_native_multimodal_message
from api.upload import handle_upload, handle_upload_extract

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX symlink semantics")

PNG = (
    b"\x89PNG\r\n\x1a\n"
    b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde"
    b"\x00\x00\x00\x0bIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00\x05\x18\xd8N"
    b"\x00\x00\x00\x00IEND\xaeB`\x82"
)

LAYOUTS = ["override-root", "override-ancestor", "default-root", "default-ancestor"]


def _put(d: Path) -> None:
    d.mkdir(parents=True, exist_ok=True)
    (d / "img.png").write_bytes(PNG)


def _swap_to(path: Path, target: Path) -> None:
    os.rename(path, path.with_name(path.name + ".saved"))
    os.symlink(target, path)


def _layout(base: Path, monkeypatch, kind: str):
    """(attachment inbox, redirect target, swap) configured like production.

    The inbox is nested under a HOME-like workspace, reachable either through
    the override env var or the default ``STATE_DIR / "attachments"`` layout,
    and the swap redirects the inbox itself or one of its ancestors.
    """
    ws = base / "home"
    out = base / "outside" / "attachments"
    att = ws / ".hermes" / "webui" / "attachments"
    _put(att)
    if kind.startswith("default"):
        monkeypatch.delenv("HERMES_WEBUI_ATTACHMENT_DIR", raising=False)
        monkeypatch.setattr(upload, "STATE_DIR", att.parent)
    else:
        monkeypatch.setenv("HERMES_WEBUI_ATTACHMENT_DIR", str(att))
    if kind.endswith("ancestor"):
        # The swap renames the ancestor, so the real inbox ends up under
        # webui.saved/attachments.
        return (att, out, lambda: _swap_to(ws / ".hermes" / "webui", base / "outside"),
                ws / ".hermes" / "webui.saved" / "attachments")
    return (att, out, lambda: _swap_to(att, base / "outside" / "attachments"),
            att.with_name(att.name + ".saved"))


def _multipart_body(fields=None, files=None, boundary=b"8786boundary"):
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


def _upload(monkeypatch, session_id: str, filename: str, data: bytes) -> _FakeHandler:
    monkeypatch.setattr(
        upload, "get_session", lambda sid: SimpleNamespace(session_id=sid, profile=None)
    )
    monkeypatch.setattr(upload, "_get_active_profile_name", lambda: "default")
    body, content_type = _multipart_body(
        fields={"session_id": session_id},
        files={"file": (filename, data, "application/octet-stream")},
    )
    handler = _FakeHandler(body, content_type)
    handle_upload(handler)
    return handler


def _upload_extract(monkeypatch, session_id: str, filename: str, data: bytes) -> _FakeHandler:
    """The archive-upload route (/api/upload/extract), same driver shape."""
    monkeypatch.setattr(
        upload, "get_session", lambda sid: SimpleNamespace(session_id=sid, profile=None)
    )
    monkeypatch.setattr(upload, "_get_active_profile_name", lambda: "default")
    body, content_type = _multipart_body(
        fields={"session_id": session_id},
        files={"file": (filename, data, "application/zip")},
    )
    handler = _FakeHandler(body, content_type)
    handle_upload_extract(handler)
    return handler


def _embeds(path: Path, workspace: Path) -> bool:
    """True when the vision allow-check passes *path* into the model context."""
    atts = _normalize_chat_attachments(
        [{"name": "img.png", "path": str(path), "mime": "image/png",
          "size": path.stat().st_size, "is_image": True}]
    )
    return isinstance(
        _build_native_multimodal_message("", "describe", atts, str(workspace)), list
    )


# --- 1. Uploads must not follow a redirected inbox (write side) ---------------


@pytest.mark.parametrize("layout", LAYOUTS)
def test_upload_after_inbox_redirect_is_refused_and_writes_nowhere(tmp_path, monkeypatch, layout):
    att, out, swap, original = _layout(tmp_path, monkeypatch, layout)
    # Control: the server uploaded from this configuration before the symlink
    # appeared (non-vacuous: the file lands in the real inbox).
    h1 = _upload(monkeypatch, "sess-1", "first.txt", b"first")
    assert h1.status == 200, h1.payload()
    assert (att / "sess-1" / "first.txt").read_bytes() == b"first"

    swap()
    # The inbox (or an ancestor) now points outside.  The upload must be
    # refused -- fail closed -- and nothing may land under the redirect.
    h2 = _upload(monkeypatch, "sess-1", "payload.txt", b"OUTSIDE-PAYLOAD")
    assert h2.status == 400, h2.payload()
    assert not list(tmp_path.joinpath("outside").rglob("payload.txt"))
    # The original inbox (renamed aside by the swap) still holds only the
    # pre-redirect upload.
    assert (original / "sess-1" / "first.txt").read_bytes() == b"first"


@pytest.mark.parametrize("layout", LAYOUTS)
def test_upload_before_inbox_redirect_still_lands_in_inbox(tmp_path, monkeypatch, layout):
    """Control: the normal path is untouched (no false refusal)."""
    att, _out, _swap, _original = _layout(tmp_path, monkeypatch, layout)
    h = _upload(monkeypatch, "sess-2", "notes.txt", b"hello")
    assert h.status == 200, h.payload()
    assert (att / "sess-2" / "notes.txt").read_bytes() == b"hello"


def test_structural_refusal_during_root_resolution_stays_a_refusal(tmp_path, monkeypatch, capsys):
    """A root that can never be the inbox fails closed: refuse, never fall back.

    The fallback would re-introduce the bug, so None must mean "no upload",
    not "resolve by name again".  The injected fault is structural (ELOOP,
    a symlink loop): the refusal stays a client-side 400 with no server-error
    log -- the #9231 classification must not over-correct into a 500 here.
    """
    att = tmp_path / "attachments"
    _put(att)
    monkeypatch.setenv("HERMES_WEBUI_ATTACHMENT_DIR", str(att))
    # Trust is established at startup (first call), like the server's import.
    upload._trusted_attachment_root()
    # A fresh spelling (a rename in progress) so the cache cannot answer.
    moved = tmp_path / "attachments-renamed"
    os.rename(att, moved)
    real_resolve = Path.resolve

    def resolve(self, strict=False):
        if str(self) == str(moved):
            raise OSError(errno.ELOOP, os.strerror(errno.ELOOP))
        return real_resolve(self, strict=strict)

    monkeypatch.setenv("HERMES_WEBUI_ATTACHMENT_DIR", str(moved))
    monkeypatch.setattr(Path, "resolve", resolve)
    h = _upload(monkeypatch, "sess-3", "x.txt", b"x")
    assert h.status == 400, h.payload()
    assert "[webui] upload error:" not in capsys.readouterr().out
    assert not list(tmp_path.rglob("x.txt"))


def test_symlink_loop_inbox_refuses_upload(tmp_path, monkeypatch):
    loop = tmp_path / "loop"
    os.symlink(loop, loop)
    monkeypatch.setenv("HERMES_WEBUI_ATTACHMENT_DIR", str(loop))
    h = _upload(monkeypatch, "sess-4", "x.txt", b"x")
    assert h.status == 400, h.payload()
    assert not list(tmp_path.rglob("x.txt"))


def test_inbox_configured_through_symlink_still_accepts_uploads(tmp_path, monkeypatch):
    """A symlinked inbox configured on purpose resolves once, at startup.

    The trusted root is resolved before the redirect is planted, so a
    deliberately symlinked configuration keeps working (no false refusal).
    """
    real = tmp_path / "data" / "attachments"
    _put(real)
    link = tmp_path / "attachments-link"
    os.symlink(real, link)
    monkeypatch.setenv("HERMES_WEBUI_ATTACHMENT_DIR", str(link))
    h = _upload(monkeypatch, "sess-5", "ok.txt", b"ok")
    assert h.status == 200, h.payload()
    assert (real / "sess-5" / "ok.txt").read_bytes() == b"ok"


# --- 2. The vision allow-check must not follow a redirected inbox -------------


@pytest.mark.parametrize("layout", LAYOUTS)
def test_vision_allow_check_after_inbox_redirect_refuses_outside(tmp_path, monkeypatch, layout):
    att, out, swap, _original = _layout(tmp_path, monkeypatch, layout)
    ws = tmp_path / "home"
    # Control: an image inside the real inbox embeds.
    assert _embeds(att / "img.png", ws)

    swap()
    _put(out)  # the redirect target now holds its own image
    # A path under the redirected inbox must not be read into the model.
    assert not _embeds(out / "img.png", ws)


@pytest.mark.parametrize("layout", LAYOUTS)
def test_vision_allow_check_before_inbox_redirect_still_allows(tmp_path, monkeypatch, layout):
    """Control: an image in the (not yet redirected) inbox still embeds."""
    att, _out, _swap, _original = _layout(tmp_path, monkeypatch, layout)
    ws = tmp_path / "home"
    assert _embeds(att / "img.png", ws)


def test_vision_allow_check_with_unresolvable_inbox_ignores_it(tmp_path, monkeypatch):
    """Fail closed: an unresolvable inbox is not an allowed root.

    The workspace root keeps working; only the attachment root is dropped.
    """
    loop = tmp_path / "loop"
    os.symlink(loop, loop)
    monkeypatch.setenv("HERMES_WEBUI_ATTACHMENT_DIR", str(loop))
    ws = tmp_path / "ws"
    _put(ws)
    assert _embeds(ws / "img.png", ws)          # workspace still allowed
    rogue = tmp_path / "rogue"
    _put(rogue)
    assert not _embeds(rogue / "img.png", ws)   # outside both roots


# --- 3. mkdir() errnos: refusal vs server-side storage fault -----------------


_STORAGE_FAULT_ERRNOS = [errno.ENOSPC, errno.EDQUOT, errno.EROFS, errno.EIO]
_INVALID_PATH_ERRNOS = [errno.ELOOP, errno.ENOTDIR]


def _inject_mkdir_errno(monkeypatch, att: Path, err: int) -> Path:
    """Make the session-directory mkdir() in the inbox fail with *err*.

    Injected at the real creation seam -- the only mkdir() that names a
    session directory under the trusted root -- so ``handle_upload`` runs its
    production path end to end.
    """
    target = (att / "sess-errno").resolve()
    real_mkdir = Path.mkdir

    def mkdir(self, *args, **kwargs):
        if self == target:
            raise OSError(err, os.strerror(err))
        return real_mkdir(self, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", mkdir)
    return target


@pytest.mark.parametrize(
    "err", _STORAGE_FAULT_ERRNOS, ids=lambda e: errno.errorcode[e]
)
def test_storage_faults_from_mkdir_are_server_errors(tmp_path, monkeypatch, capsys, err):
    """A full disk, a quota, a read-only fs or an I/O fault is a 500.

    Those are server-side storage failures: they must reach handle_upload's
    HTTP500 branch and its error log instead of being reported as a client
    mistake (a 400 would also hide the fault from the operator).
    """
    att = tmp_path / "attachments"
    _put(att)
    monkeypatch.setenv("HERMES_WEBUI_ATTACHMENT_DIR", str(att))
    upload._trusted_attachment_root()
    target = _inject_mkdir_errno(monkeypatch, att, err)

    h = _upload(monkeypatch, "sess-errno", "x.txt", b"x")

    assert h.status == 500, h.payload()
    assert "[webui] upload error:" in capsys.readouterr().out
    assert not target.exists()


@pytest.mark.parametrize(
    "err", _INVALID_PATH_ERRNOS, ids=lambda e: errno.errorcode[e]
)
def test_invalid_path_errnos_from_mkdir_are_upload_refusals(tmp_path, monkeypatch, capsys, err):
    """A symlink loop or a non-directory component stays a refusal (400).

    The path can never become the inbox, so the upload is denied -- and no
    server-error log is written, because nothing server-side failed.
    """
    att = tmp_path / "attachments"
    _put(att)
    monkeypatch.setenv("HERMES_WEBUI_ATTACHMENT_DIR", str(att))
    upload._trusted_attachment_root()
    target = _inject_mkdir_errno(monkeypatch, att, err)

    h = _upload(monkeypatch, "sess-errno", "x.txt", b"x")

    assert h.status == 400, h.payload()
    assert "[webui] upload error:" not in capsys.readouterr().out
    assert not target.exists()


# --- 4. #9231 gate: a startup storage fault is a logged 500, not a silent 400
#
# The startup trust decision (resolve + usability probe) used to cache a bare
# None on ANY failure, and _session_attachment_dir() turned that into
# ValueError('Attachment inbox is unavailable') -- both upload routes then
# answered HTTP 400 with no operator log, hiding a server/storage fault as a
# client mistake.  The fix classifies the cached failure: structural refusals
# (ELOOP/ENOTDIR/RuntimeError/ValueError) keep the fail-closed 400, storage
# faults (EIO, EACCES, ENOSPC, EDQUOT, EROFS, ...) become a logged 500
# carrying the errno and a restart diagnostic.  No automatic re-trust: the
# cache is not re-resolved mid-process, so a fault repaired after startup is
# still reported until restart (the gate's "recovered-storage" cases).

_STARTUP_STORAGE_FAULTS = [errno.EIO, errno.EACCES]


def _inject_resolve_errno(monkeypatch, target: Path, err: int) -> None:
    """Make the startup by-name resolve() of *target* fail with *err*.

    *monkeypatch* must be a SCOPED patcher (``monkeypatch.context()``): the
    fault has to exist only while the startup trust decision runs, so the
    request that follows sees a recovered filesystem.
    """
    real_resolve = Path.resolve

    def resolve(self, strict=False):
        if str(self) == str(target):
            raise OSError(err, os.strerror(err))
        return real_resolve(self, strict=strict)

    monkeypatch.setattr(Path, "resolve", resolve)


def _inject_stat_errno(monkeypatch, target: Path, err: int) -> None:
    """Make the startup usability stat() of *target* fail with *err*.

    *monkeypatch* must be a SCOPED patcher (``monkeypatch.context()``): the
    fault has to exist only while the startup trust decision runs, so the
    request that follows sees a recovered filesystem.
    """
    real_stat = os.stat

    def stat(path, *args, **kwargs):
        if str(path) == str(target):
            raise OSError(err, os.strerror(err))
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(upload.os, "stat", stat)


def _assert_storage_recovered(att: Path) -> None:
    """Assert no injected startup fault is live anymore.

    The gate's recovered-storage cases only mean something while the
    filesystem is healthy again: if a patch leaked past the startup call,
    the request would fail for the wrong reason and the test would no
    longer distinguish "the cached failure is reported" from "the fault is
    still there".
    """
    for label, call in (
        ("resolve", lambda: Path(att).resolve()),
        ("stat", lambda: os.stat(att)),
    ):
        try:
            call()
        except OSError as exc:
            raise AssertionError(
                f"the injected startup fault is still live ({label}: {exc})"
            ) from exc


@pytest.mark.parametrize("err", _STARTUP_STORAGE_FAULTS, ids=lambda e: errno.errorcode[e])
@pytest.mark.parametrize("seam", ["resolve", "stat"], ids=lambda s: f"seam-{s}")
def test_startup_storage_fault_is_logged_500_with_restart_diagnostic(
    tmp_path, monkeypatch, capsys, err, seam
):
    """A storage fault during root initialization is a server error, not a 400.

    The fault exists only while the startup call runs; the request below
    sees a healthy filesystem (the gate's recovered-storage cases).  The
    cached failure must still surface: HTTP 500, an operator error log, and
    a restart diagnostic naming the errno.  Nothing is written anywhere.
    """
    att = tmp_path / "attachments"
    _put(att)
    monkeypatch.setenv("HERMES_WEBUI_ATTACHMENT_DIR", str(att))
    # The fault exists only while the startup call runs; the request below
    # sees a healthy filesystem (the gate's recovered-storage cases).  The
    # patch is therefore scoped to the startup call: a fault still live at
    # request time would make the request fail for the wrong reason, and a
    # regression that re-resolved the root after startup would go unnoticed.
    with monkeypatch.context() as fault:
        if seam == "resolve":
            _inject_resolve_errno(fault, att, err)
        else:
            _inject_stat_errno(fault, att, err)
        # The startup trust decision under the injected fault (the same call
        # the server makes at import), after which the filesystem is healthy
        # again.
        upload._trusted_attachment_root()
    _assert_storage_recovered(att)

    h = _upload(monkeypatch, "sess-9231", "x.txt", b"x")

    assert h.status == 500, h.payload()
    out = capsys.readouterr().out
    assert "[webui] upload error:" in out
    assert "Attachment inbox is unavailable" in out
    assert f"[Errno {err}]" in out
    assert "restart the server" in out
    assert not list(tmp_path.rglob("x.txt"))


@pytest.mark.parametrize("err", _STARTUP_STORAGE_FAULTS, ids=lambda e: errno.errorcode[e])
def test_startup_storage_fault_on_extract_route_is_logged_500(
    tmp_path, monkeypatch, capsys, err
):
    """The archive-upload route reports the same fault the same way.

    /api/upload/extract shares _session_attachment_dir(), so a startup
    storage fault must reach its HTTP500 branch and error log too -- the
    gate's reproduced silent 400 was on both routes.
    """
    att = tmp_path / "attachments"
    _put(att)
    monkeypatch.setenv("HERMES_WEBUI_ATTACHMENT_DIR", str(att))
    # Same scoping as the plain-upload case: the fault lives only during the
    # startup trust decision, so the request below sees a recovered
    # filesystem and exercises the cached failure, not the live fault.
    with monkeypatch.context() as fault:
        _inject_stat_errno(fault, att, err)
        upload._trusted_attachment_root()
    _assert_storage_recovered(att)

    h = _upload_extract(monkeypatch, "sess-9231x", "bundle.zip", b"PK\x03\x04payload")

    assert h.status == 500, h.payload()
    out = capsys.readouterr().out
    assert "[webui] upload extract error:" in out
    assert "Attachment inbox is unavailable" in out
    assert f"[Errno {err}]" in out
    assert "restart the server" in out
    assert not list(tmp_path.rglob("bundle"))


def test_root_failure_classification_split():
    """The classification itself: structural refusals vs storage faults."""
    loop = upload._classify_root_failure(OSError(errno.ELOOP, os.strerror(errno.ELOOP)))
    assert loop.kind == "refusal" and loop.errno == errno.ELOOP
    notdir = upload._classify_root_failure(OSError(errno.ENOTDIR, os.strerror(errno.ENOTDIR)))
    assert notdir.kind == "refusal" and notdir.errno == errno.ENOTDIR
    # macOS resolve() raises RuntimeError for a symlink loop; a spelling the
    # OS rejects outright raises ValueError.  Both are structural.
    assert upload._classify_root_failure(RuntimeError("loop")).kind == "refusal"
    assert upload._classify_root_failure(ValueError("embedded null byte")).kind == "refusal"
    for err in (errno.EIO, errno.EACCES, errno.ENOSPC, errno.EDQUOT, errno.EROFS):
        fault = upload._classify_root_failure(OSError(err, os.strerror(err)))
        assert fault.kind == "storage", err
        assert fault.errno == err


def test_failure_query_never_disagrees_with_the_root(tmp_path, monkeypatch):
    """_trusted_attachment_root_failure() mirrors the root's availability.

    Healthy spelling: a root and no failure.  Storage-faulted spelling: no
    root and a classified failure.  The two readers share one cache entry,
    so they cannot drift.
    """
    att = tmp_path / "attachments"
    _put(att)
    monkeypatch.setenv("HERMES_WEBUI_ATTACHMENT_DIR", str(att))
    assert upload._trusted_attachment_root() == att.resolve()
    assert upload._trusted_attachment_root_failure() is None

    moved = tmp_path / "attachments-renamed"
    os.rename(att, moved)
    _inject_stat_errno(monkeypatch, moved, errno.EIO)
    monkeypatch.setenv("HERMES_WEBUI_ATTACHMENT_DIR", str(moved))
    assert upload._trusted_attachment_root() is None
    failure = upload._trusted_attachment_root_failure()
    assert failure is not None
    assert failure.kind == "storage" and failure.errno == errno.EIO
