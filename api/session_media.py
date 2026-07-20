"""Private, file-backed storage for large native image content parts.

Large raster data URLs are stored as immutable, content-addressed files and
represented in session JSON by ``webui-media://`` references.  The private
store is owned by ``STATE_DIR/session-media`` and is deliberately separate
from the user-controlled attachment/archive extraction namespace.

References do not encode a filesystem root.  Moving the complete WebUI state
directory therefore moves their authority with it; changing only
``HERMES_WEBUI_ATTACHMENT_DIR`` does not.  Files written by the earlier
attachment-root layout are read through a strict compatibility path and
migrated into the state-owned store on first use.
"""
import base64
import binascii
import hashlib
import os
import re
import secrets
import stat
import threading
from contextlib import contextmanager
from pathlib import Path

from api.config import STATE_DIR


_MIN_EXTERNALIZED_BYTES = 64 * 1024
_MEDIA_SCHEME = "webui-media://"
_PRIVATE_ROOT_NAME = "session-media"
_REF_RE = re.compile(r"^webui-media://([a-f0-9]{64}\.(?:png|jpe?g|gif|webp|bmp))$")
_SAFE_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,120}$")
_MIME_EXTENSIONS = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/gif": "gif",
    "image/webp": "webp",
    "image/bmp": "bmp",
}
_EXTENSION_MIMES = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "webp": "image/webp",
    "bmp": "image/bmp",
}
_O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_MEDIA_IO_LOCK = threading.RLock()


class SessionMediaIntegrityError(ValueError):
    """A private reference cannot be proven safe and complete."""


def _validated_session_id(session_id: str) -> str:
    sid = str(session_id or "")
    if not _SAFE_SESSION_ID_RE.fullmatch(sid) or sid in {".", ".."}:
        raise SessionMediaIntegrityError("Invalid session id for private media")
    return sid


def _state_root() -> Path:
    root = Path(STATE_DIR).expanduser()
    root.mkdir(parents=True, exist_ok=True)
    return root.resolve()


def _attachment_root() -> Path:
    """Return the legacy upload-inbox root for read compatibility only."""
    override = os.getenv("HERMES_WEBUI_ATTACHMENT_DIR", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return (_state_root() / "attachments").resolve()


def _session_media_dir(session_id: str) -> Path:
    """Return the state-owned private media path (diagnostics/tests only)."""
    return _state_root() / _PRIVATE_ROOT_NAME / _validated_session_id(session_id)


def _legacy_session_media_dirs(session_id: str) -> list[Path]:
    sid = _validated_session_id(session_id)
    roots = [_attachment_root(), (_state_root() / "attachments").resolve()]
    out = []
    for root in roots:
        candidate = root / sid / _PRIVATE_ROOT_NAME
        if candidate not in out:
            out.append(candidate)
    return out


def _fsync_dir(fd: int) -> None:
    os.fsync(fd)


def _open_root_fd(path: Path) -> int:
    return os.open(str(path), os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW)


def _open_child_dir(parent_fd: int, name: str, *, create: bool) -> tuple[int, bool]:
    try:
        return os.open(name, os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW, dir_fd=parent_fd), False
    except FileNotFoundError:
        if not create:
            raise
    try:
        os.mkdir(name, 0o700, dir_fd=parent_fd)
        created = True
        try:
            _fsync_dir(parent_fd)
        except BaseException:
            try:
                os.rmdir(name, dir_fd=parent_fd)
            except OSError:
                pass
            raise
    except FileExistsError:
        created = False
    return os.open(name, os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW, dir_fd=parent_fd), created


def _assert_entry_still_names_fd(parent_fd: int, name: str, child_fd: int) -> None:
    held = os.fstat(child_fd)
    try:
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except OSError as exc:
        raise SessionMediaIntegrityError(
            "Private media directory changed during operation"
        ) from exc
    if not stat.S_ISDIR(current.st_mode) or (held.st_dev, held.st_ino) != (
        current.st_dev,
        current.st_ino,
    ):
        raise SessionMediaIntegrityError("Private media directory changed during operation")


def _assert_private_handles(state_fd: int, media_fd: int, session_fd: int, sid: str) -> None:
    _assert_entry_still_names_fd(state_fd, _PRIVATE_ROOT_NAME, media_fd)
    _assert_entry_still_names_fd(media_fd, sid, session_fd)
    try:
        current_state = os.stat(_state_root(), follow_symlinks=True)
    except OSError as exc:
        raise SessionMediaIntegrityError(
            "WebUI state directory changed during operation"
        ) from exc
    held_state = os.fstat(state_fd)
    if (current_state.st_dev, current_state.st_ino) != (held_state.st_dev, held_state.st_ino):
        raise SessionMediaIntegrityError("WebUI state directory changed during operation")


@contextmanager
def _open_private_session(session_id: str, *, create: bool):
    sid = _validated_session_id(session_id)
    state_fd = media_fd = session_fd = None
    state_root = _state_root()
    try:
        state_fd = _open_root_fd(state_root)
        media_fd, _ = _open_child_dir(state_fd, _PRIVATE_ROOT_NAME, create=create)
        session_fd, session_created = _open_child_dir(media_fd, sid, create=create)
        yield state_fd, media_fd, session_fd, session_created
        _assert_private_handles(state_fd, media_fd, session_fd, sid)
    finally:
        for fd in (session_fd, media_fd, state_fd):
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass


@contextmanager
def _open_legacy_session(path: Path):
    """Open legacy ``<attachment-root>/<sid>/session-media`` without symlinks."""
    root = path.parents[1]
    sid = path.parent.name
    root_fd = session_fd = media_fd = None
    try:
        root_fd = _open_root_fd(root)
        session_fd, _ = _open_child_dir(root_fd, sid, create=False)
        media_fd, _ = _open_child_dir(session_fd, _PRIVATE_ROOT_NAME, create=False)
        yield media_fd
        _assert_entry_still_names_fd(root_fd, sid, session_fd)
        _assert_entry_still_names_fd(session_fd, _PRIVATE_ROOT_NAME, media_fd)
    finally:
        for fd in (media_fd, session_fd, root_fd):
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass


def _is_expected_raster_bytes(mime: str, raw: bytes) -> bool:
    if mime == "image/png":
        return raw.startswith(b"\x89PNG\r\n\x1a\n")
    if mime == "image/jpeg":
        return raw.startswith(b"\xff\xd8\xff")
    if mime == "image/gif":
        return raw.startswith((b"GIF87a", b"GIF89a"))
    if mime == "image/webp":
        return len(raw) >= 12 and raw[:4] == b"RIFF" and raw[8:12] == b"WEBP"
    if mime == "image/bmp":
        return raw.startswith(b"BM")
    return False


def _decode_raster_data_uri(url, *, min_bytes: int = 0):
    """Return ``(mime, bytes)`` for a valid raster base64 data URI, else None."""
    if not isinstance(url, str) or not url[:5].lower() == "data:":
        return None
    try:
        header, encoded = url.split(",", 1)
    except ValueError:
        return None
    tokens = header[5:].split(";")
    if not tokens or "base64" not in {token.strip().lower() for token in tokens[1:]}:
        return None
    mime = tokens[0].strip().lower()
    if mime not in _MIME_EXTENSIONS:
        return None
    if min_bytes and len(encoded) < 4 * ((min_bytes + 2) // 3):
        return None
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        return None
    if not _is_expected_raster_bytes(mime, raw):
        return None
    return mime, raw


def _reference_filename(url: str) -> str:
    match = _REF_RE.fullmatch(url) if isinstance(url, str) else None
    if not match:
        raise SessionMediaIntegrityError("Invalid private session media reference")
    return match.group(1)


def _verify_media_bytes(filename: str, raw: bytes) -> tuple[str, bytes]:
    _reference_filename(_MEDIA_SCHEME + filename)
    mime = _EXTENSION_MIMES.get(filename.rsplit(".", 1)[-1].lower())
    if not mime or not _is_expected_raster_bytes(mime, raw):
        raise SessionMediaIntegrityError(
            f"Stored session image has an invalid type: {filename}. Restore it or re-attach the image."
        )
    if hashlib.sha256(raw).hexdigest() != filename.split(".", 1)[0]:
        raise SessionMediaIntegrityError(
            f"Stored session image failed digest verification: {filename}. Restore it or re-attach the image."
        )
    return mime, raw


def _read_file_at(directory_fd: int, filename: str) -> bytes:
    fd = os.open(filename, os.O_RDONLY | _O_NOFOLLOW, dir_fd=directory_fd)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise SessionMediaIntegrityError("Stored session image is not a regular file")
        chunks = []
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)
    finally:
        os.close(fd)


def _read_and_verify_at(directory_fd: int, filename: str) -> tuple[str, bytes]:
    return _verify_media_bytes(filename, _read_file_at(directory_fd, filename))


def _new_temp_name(filename: str) -> str:
    return f".{filename}.{secrets.token_hex(16)}.tmp"


def _write_temp_at(directory_fd: int, filename: str, raw: bytes) -> str:
    temp_name = _new_temp_name(filename)
    fd = os.open(
        temp_name,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_NOFOLLOW,
        0o600,
        dir_fd=directory_fd,
    )
    try:
        view = memoryview(raw)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("Could not write private session media")
            view = view[written:]
        os.fsync(fd)
    except BaseException:
        try:
            os.unlink(temp_name, dir_fd=directory_fd)
        except OSError:
            pass
        raise
    finally:
        os.close(fd)
    return temp_name


def _write_media(session_id: str, mime: str, raw: bytes) -> str:
    """Durably persist immutable media and return its opaque reference URL."""
    if mime not in _MIME_EXTENSIONS or not _is_expected_raster_bytes(mime, raw):
        raise SessionMediaIntegrityError("Refusing to store unverified session media")
    digest = hashlib.sha256(raw).hexdigest()
    filename = f"{digest}.{_MIME_EXTENSIONS[mime]}"
    temp_name = None
    published = False
    with _MEDIA_IO_LOCK:
        sid = _validated_session_id(session_id)
        with _open_private_session(sid, create=True) as (
            state_fd,
            media_fd,
            directory_fd,
            session_created,
        ):
            try:
                try:
                    _read_and_verify_at(directory_fd, filename)
                    return _MEDIA_SCHEME + filename
                except FileNotFoundError:
                    pass
                except SessionMediaIntegrityError:
                    # A corrupt same-name target is replaced only after the new
                    # bytes are fully written and synced.
                    pass
                temp_name = _write_temp_at(directory_fd, filename, raw)
                os.replace(
                    temp_name,
                    filename,
                    src_dir_fd=directory_fd,
                    dst_dir_fd=directory_fd,
                )
                temp_name = None
                published = True
                _fsync_dir(directory_fd)
                _read_and_verify_at(directory_fd, filename)
                _assert_private_handles(state_fd, media_fd, directory_fd, sid)
            except BaseException:
                if temp_name is not None:
                    try:
                        os.unlink(temp_name, dir_fd=directory_fd)
                    except OSError:
                        pass
                    temp_name = None
                if published:
                    try:
                        os.unlink(filename, dir_fd=directory_fd)
                        _fsync_dir(directory_fd)
                    except OSError:
                        pass
                if session_created:
                    try:
                        os.rmdir(sid, dir_fd=media_fd)
                        _fsync_dir(media_fd)
                    except OSError:
                        pass
                raise
            finally:
                if temp_name is not None:
                    try:
                        os.unlink(temp_name, dir_fd=directory_fd)
                    except OSError:
                        pass
    return _MEDIA_SCHEME + filename


def _read_verified_media_reference(session_id: str, filename: str) -> tuple[str, bytes]:
    """Read through one anchored handle and verify type, magic and digest."""
    _reference_filename(_MEDIA_SCHEME + filename)
    try:
        with _open_private_session(session_id, create=False) as (_, _, directory_fd, _):
            return _read_and_verify_at(directory_fd, filename)
    except FileNotFoundError as private_missing:
        for legacy_path in _legacy_session_media_dirs(session_id):
            try:
                with _open_legacy_session(legacy_path) as legacy_fd:
                    mime, raw = _read_and_verify_at(legacy_fd, filename)
                # Migration is part of the successful compatibility read.  Do
                # not keep serving from an attachment root that may move later.
                _write_media(session_id, mime, raw)
                return mime, raw
            except FileNotFoundError:
                continue
        raise SessionMediaIntegrityError(
            f"Stored session image is missing: {filename}. Restore it or re-attach the image."
        ) from private_missing
    except OSError as exc:
        raise SessionMediaIntegrityError(
            f"Stored session image could not be read safely: {filename}. Restore it or re-attach the image."
        ) from exc


def _collect_reference_filenames(value) -> list[str]:
    filenames = set()

    def visit(node):
        if isinstance(node, list):
            for item in node:
                visit(item)
            return
        if not isinstance(node, dict):
            if isinstance(node, str) and node.startswith(_MEDIA_SCHEME):
                raise SessionMediaIntegrityError("Private media reference is outside an image part")
            return
        image = node.get("image_url")
        if node.get("type") == "image_url" and isinstance(image, dict):
            url = image.get("url")
            if isinstance(url, str) and url.startswith(_MEDIA_SCHEME):
                filenames.add(_reference_filename(url))
            for key, child in node.items():
                if key != "image_url":
                    visit(child)
            for key, child in image.items():
                if key != "url":
                    visit(child)
            return
        for child in node.values():
            visit(child)

    visit(value)
    return sorted(filenames)


def clone_session_media_references(value, source_session_id: str, destination_session_id: str) -> int:
    """Transactionally give a new session verified ownership of all references."""
    if source_session_id == destination_session_id:
        return 0
    filenames = _collect_reference_filenames(value)
    if not filenames:
        return 0

    # Preflight every source before creating the destination namespace.  A bad
    # later reference therefore cannot leave an earlier destination blob behind.
    payloads = {
        filename: _read_verified_media_reference(source_session_id, filename)
        for filename in filenames
    }
    staged = {}
    created = []
    with _MEDIA_IO_LOCK:
        destination_sid = _validated_session_id(destination_session_id)
        with _open_private_session(destination_sid, create=True) as (
            state_fd,
            media_fd,
            directory_fd,
            session_created,
        ):
            try:
                for filename, (mime, raw) in payloads.items():
                    expected = f"{hashlib.sha256(raw).hexdigest()}.{_MIME_EXTENSIONS[mime]}"
                    if expected != filename:
                        raise SessionMediaIntegrityError("Session media identity changed while cloning")
                    try:
                        _read_and_verify_at(directory_fd, filename)
                        continue
                    except FileNotFoundError:
                        staged[filename] = _write_temp_at(directory_fd, filename, raw)
                    except SessionMediaIntegrityError as exc:
                        raise SessionMediaIntegrityError(
                            f"Destination already contains corrupt session media: {filename}"
                        ) from exc

                # Hard-link fully-synced staging files into their immutable names.
                # Link publication is exclusive, so a check-then-use race cannot
                # overwrite a different writer.
                for filename, temp_name in staged.items():
                    try:
                        os.link(
                            temp_name,
                            filename,
                            src_dir_fd=directory_fd,
                            dst_dir_fd=directory_fd,
                            follow_symlinks=False,
                        )
                        created.append(filename)
                    except FileExistsError:
                        _read_and_verify_at(directory_fd, filename)
                for temp_name in staged.values():
                    os.unlink(temp_name, dir_fd=directory_fd)
                staged.clear()
                _fsync_dir(directory_fd)
                for filename in filenames:
                    _read_and_verify_at(directory_fd, filename)
                _assert_private_handles(
                    state_fd,
                    media_fd,
                    directory_fd,
                    destination_sid,
                )
            except BaseException:
                for filename in reversed(created):
                    try:
                        os.unlink(filename, dir_fd=directory_fd)
                    except OSError:
                        pass
                for temp_name in staged.values():
                    try:
                        os.unlink(temp_name, dir_fd=directory_fd)
                    except OSError:
                        pass
                try:
                    _fsync_dir(directory_fd)
                except OSError:
                    pass
                if session_created:
                    try:
                        os.rmdir(destination_sid, dir_fd=media_fd)
                        _fsync_dir(media_fd)
                    except OSError:
                        pass
                raise
    return len(filenames)


def externalize_large_session_media(
    value,
    session_id: str,
    *,
    min_bytes: int = _MIN_EXTERNALIZED_BYTES,
) -> int:
    """Replace large structured raster data URLs in *value* in place."""
    changed = 0

    def visit(node):
        nonlocal changed
        if isinstance(node, list):
            for item in node:
                visit(item)
            return
        if not isinstance(node, dict):
            return
        image = node.get("image_url")
        if node.get("type") == "image_url" and isinstance(image, dict):
            decoded = _decode_raster_data_uri(image.get("url"), min_bytes=min_bytes)
            if decoded is not None:
                mime, raw = decoded
                if len(raw) >= min_bytes:
                    # Mutate only after durable publication succeeds.
                    reference = _write_media(session_id, mime, raw)
                    image["url"] = reference
                    changed += 1
                    return
        for child in node.values():
            visit(child)

    visit(value)
    return changed


def assert_no_session_media_references(value, *, context: str = "outbound payload") -> None:
    """Recursively reject any private URI that survived a serialization boundary."""
    def visit(node):
        if isinstance(node, dict):
            for child in node.values():
                visit(child)
        elif isinstance(node, list):
            for child in node:
                visit(child)
        elif isinstance(node, str) and node.startswith(_MEDIA_SCHEME):
            raise SessionMediaIntegrityError(
                f"Private session media reference remained in {context}"
            )

    visit(value)


def hydrate_session_media_urls(value, session_id: str):
    """Return a deep copy with every private image strictly verified and expanded."""
    import copy

    hydrated = copy.deepcopy(value)

    def visit(node):
        if isinstance(node, list):
            for item in node:
                visit(item)
            return
        if not isinstance(node, dict):
            return
        image = node.get("image_url")
        if node.get("type") == "image_url" and isinstance(image, dict):
            url = image.get("url")
            if isinstance(url, str) and url.startswith(_MEDIA_SCHEME):
                filename = _reference_filename(url)
                mime, raw = _read_verified_media_reference(session_id, filename)
                image["url"] = "data:%s;base64,%s" % (
                    mime,
                    base64.b64encode(raw).decode("ascii"),
                )
                return
        for child in node.values():
            visit(child)

    visit(hydrated)
    assert_no_session_media_references(hydrated, context="hydrated session history")
    return hydrated


def _remove_tree_at(parent_fd: int, name: str) -> None:
    """Remove one child tree without following symlinks."""
    try:
        child_fd = os.open(name, os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW, dir_fd=parent_fd)
    except FileNotFoundError:
        return
    try:
        with os.scandir(child_fd) as entries:
            for entry in entries:
                info = entry.stat(follow_symlinks=False)
                if stat.S_ISDIR(info.st_mode):
                    _remove_tree_at(child_fd, entry.name)
                else:
                    os.unlink(entry.name, dir_fd=child_fd)
    finally:
        os.close(child_fd)
    os.rmdir(name, dir_fd=parent_fd)
    _fsync_dir(parent_fd)


def remove_session_media(session_id: str) -> None:
    """Delete the state-owned private media namespace for one session."""
    sid = _validated_session_id(session_id)
    try:
        state_fd = _open_root_fd(_state_root())
        try:
            media_fd, _ = _open_child_dir(state_fd, _PRIVATE_ROOT_NAME, create=False)
            try:
                _remove_tree_at(media_fd, sid)
            finally:
                os.close(media_fd)
        finally:
            os.close(state_fd)
    except FileNotFoundError:
        return
