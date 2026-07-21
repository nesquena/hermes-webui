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
# POSIX only exposes unlink/rmdir by a mutable directory entry.  It cannot
# retire the inode held by an open descriptor, so a replacement can always win
# between a final identity check and the destructive syscall.  Do not create a
# private reference until the storage backend has an identity-bound retirement
# primitive.  Existing references remain read-compatible and are migrated back
# to portable data URLs by Session.save().
_PRIVATE_MEDIA_EXTERNALIZATION_ENABLED = False
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
_DIR_FD_CAPABILITIES = {
    "open": os.open in getattr(os, "supports_dir_fd", set()),
    "mkdir": os.mkdir in getattr(os, "supports_dir_fd", set()),
    "stat": os.stat in getattr(os, "supports_dir_fd", set()),
    "unlink": os.unlink in getattr(os, "supports_dir_fd", set()),
    "rmdir": os.rmdir in getattr(os, "supports_dir_fd", set()),
    "link": os.link in getattr(os, "supports_dir_fd", set()),
    # CPython exposes os.replace(src_dir_fd=..., dst_dir_fd=...) through the
    # same platform primitive as os.rename, but only os.rename is listed in
    # supports_dir_fd. Key the replace capability to that authoritative entry.
    "replace": os.name != "nt" and os.rename in getattr(os, "supports_dir_fd", set()),
    "listdir": os.listdir in getattr(os, "supports_fd", set()),
}
_DIR_FD_OK = all(_DIR_FD_CAPABILITIES.values())
_MEDIA_IO_LOCK = threading.RLock()


class SessionMediaIntegrityError(ValueError):
    """A private reference cannot be proven safe and complete."""


def _unsupported_backend_error() -> SessionMediaIntegrityError:
    missing = ", ".join(
        name for name, supported in _DIR_FD_CAPABILITIES.items() if not supported
    ) or "anchored directory handles"
    return SessionMediaIntegrityError(
        "Private session media is unsupported on this filesystem backend "
        f"because safe anchored operations are unavailable ({missing})"
    )


def _validated_session_id(session_id: str) -> str:
    sid = str(session_id or "")
    if not _SAFE_SESSION_ID_RE.fullmatch(sid) or sid in {".", ".."}:
        raise SessionMediaIntegrityError("Invalid session id for private media")
    return sid


@contextmanager
def _session_media_authority(session_id: str):
    """Share the durable SID mutation boundary with sidecar publication."""
    from api.models import _session_publication_process_lock

    with _session_publication_process_lock(_validated_session_id(session_id)):
        yield


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


def session_media_destination_owners(session_id: str) -> list[str]:
    """Return durable private-media owners for *session_id* without mutating.

    A crash may leave a renamed ``.delete-<sid-hash>-...`` quarantine before a
    cleanup residual is persisted.  It is still private data for that SID and
    must block reuse just like the canonical directory does.
    """
    sid = _validated_session_id(session_id)
    root = _state_root() / _PRIVATE_ROOT_NAME
    try:
        root_stat = os.lstat(root)
    except FileNotFoundError:
        return []
    except OSError:
        return ["session_media_unknown"]
    if not stat.S_ISDIR(root_stat.st_mode):
        return ["session_media_unknown"]
    owners = []
    try:
        if _path_entry_exists(_session_media_dir(sid)):
            owners.append("session_media")
        prefix = _deletion_quarantine_prefix(sid)
        if any(path.name.startswith(prefix) for path in root.iterdir()):
            owners.append("session_media_quarantine")
    except OSError:
        return ["session_media_unknown"]
    return owners


def _path_entry_exists(path: Path) -> bool:
    try:
        os.lstat(path)
        return True
    except FileNotFoundError:
        return False


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


def _assert_regular_entry_still_names_fd(
    parent_fd: int,
    name: str,
    entry_fd: int,
) -> None:
    held = os.fstat(entry_fd)
    try:
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except OSError as exc:
        raise SessionMediaIntegrityError(
            "Private media file changed during operation"
        ) from exc
    if not stat.S_ISREG(current.st_mode) or (held.st_dev, held.st_ino) != (
        current.st_dev,
        current.st_ino,
    ):
        raise SessionMediaIntegrityError("Private media file changed during operation")


def _unlink_quarantined_regular_entry(
    parent_fd: int,
    name: str,
    entry_fd: int,
) -> None:
    """Retire only the regular inode held in private quarantine.

    POSIX exposes ``unlinkat`` only by mutable directory entry.  Even after an
    inode comparison, another writer can replace that entry before the unlink,
    so no portable Python/POSIX primitive can prove that unlink removes this
    held inode rather than the replacement.  Keep the quarantined entry as the
    durable retry authority instead of risking deletion of unrelated data.
    """
    _assert_regular_entry_still_names_fd(parent_fd, name, entry_fd)
    raise SessionMediaIntegrityError(
        "Exact private media file retirement is unavailable; retaining quarantine"
    )


def _rmdir_quarantined_directory_entry(
    parent_fd: int,
    name: str,
    entry_fd: int,
) -> None:
    """Retire only the empty directory inode held in private quarantine.

    See :func:`_unlink_quarantined_regular_entry`: ``rmdir`` has the same
    pathname-only final operation, so retaining the already-hidden quarantine
    is the only fail-closed behavior on this backend.
    """
    _assert_entry_still_names_fd(parent_fd, name, entry_fd)
    raise SessionMediaIntegrityError(
        "Exact private media directory retirement is unavailable; retaining quarantine"
    )


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
    if not _DIR_FD_OK:
        raise _unsupported_backend_error()
    temp_name = None
    published = False
    sid = _validated_session_id(session_id)
    with _session_media_authority(sid), _MEDIA_IO_LOCK:
        with _open_private_session(sid, create=True) as (
            state_fd,
            media_fd,
            directory_fd,
            session_created,
        ):
            try:
                try:
                    _read_and_verify_at(directory_fd, filename)
                except FileNotFoundError:
                    pass
                except SessionMediaIntegrityError:
                    # A corrupt same-name target is replaced only after the new
                    # bytes are fully written and synced.
                    pass
                else:
                    _assert_private_handles(
                        state_fd,
                        media_fd,
                        directory_fd,
                        sid,
                    )
                    return _MEDIA_SCHEME + filename
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
    if not _DIR_FD_OK:
        raise _unsupported_backend_error()
    try:
        with _session_media_authority(session_id), _open_private_session(
            session_id,
            create=False,
        ) as (
            state_fd,
            media_fd,
            directory_fd,
            _,
        ):
            result = _read_and_verify_at(directory_fd, filename)
            _assert_private_handles(
                state_fd,
                media_fd,
                directory_fd,
                _validated_session_id(session_id),
            )
            return result
    except FileNotFoundError as private_missing:
        for legacy_path in _legacy_session_media_dirs(session_id):
            try:
                with _open_legacy_session(legacy_path) as legacy_fd:
                    mime, raw = _read_and_verify_at(legacy_fd, filename)
                # Compatibility is deliberately read-only. Publishing a new
                # private copy would recreate the retirement problem this
                # module now fails closed on; persisted migration requires an
                # explicit offline tool with a safe retirement backend.
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


def verify_session_media_references(value, session_id: str) -> int:
    """Fail closed unless every retained private reference is owned and intact."""
    filenames = _collect_reference_filenames(value)
    for filename in filenames:
        _read_verified_media_reference(session_id, filename)
    return len(filenames)


def verify_serialized_session_media_payload(payload: dict, session_id: str) -> int:
    """Validate private refs across the complete sidecar schema.

    Compact references are sanctioned only inside the two transcript fields,
    and only in canonical ``type=image_url`` parts. Every other serialized
    field must remain portable and free of private ownership handles.
    """
    if not isinstance(payload, dict):
        raise SessionMediaIntegrityError("Session payload must be an object")
    filenames = set()

    def reject_private_refs(value) -> None:
        assert_no_session_media_references(
            value,
            context="session transcript outside an image part",
        )

    for field in ("messages", "context_messages"):
        transcript = payload.get(field, [])
        if not isinstance(transcript, list):
            reject_private_refs(transcript)
            continue
        for message in transcript:
            if not isinstance(message, dict):
                reject_private_refs(message)
                continue
            reject_private_refs(
                {key: value for key, value in message.items() if key != "content"}
            )
            content = message.get("content")
            if not isinstance(content, list):
                reject_private_refs(content)
                continue
            for part in content:
                image = part.get("image_url") if isinstance(part, dict) else None
                if (
                    isinstance(part, dict)
                    and part.get("type") == "image_url"
                    and isinstance(image, dict)
                ):
                    url = image.get("url")
                    if isinstance(url, str) and url.startswith(_MEDIA_SCHEME):
                        filenames.add(_reference_filename(url))
                    reject_private_refs(
                        {
                            **{key: value for key, value in part.items() if key != "image_url"},
                            "image_url": {
                                key: value for key, value in image.items() if key != "url"
                            },
                        }
                    )
                    continue
                reject_private_refs(part)

    outside = {
        key: value
        for key, value in payload.items()
        if key not in {"messages", "context_messages"}
    }
    assert_no_session_media_references(
        outside,
        context="session payload outside messages/context_messages",
    )
    for filename in sorted(filenames):
        _read_verified_media_reference(session_id, filename)
    return len(filenames)


def clone_session_media_references(value, source_session_id: str, destination_session_id: str) -> int:
    """Transactionally give a new session verified ownership of all references."""
    if source_session_id == destination_session_id:
        return 0
    filenames = _collect_reference_filenames(value)
    if not filenames:
        return 0

    if not _PRIVATE_MEDIA_EXTERNALIZATION_ENABLED:
        raise SessionMediaIntegrityError(
            "Private session media creation is disabled; hydrate legacy references first"
        )

    if not _DIR_FD_OK:
        raise _unsupported_backend_error()

    # Preflight every source before creating the destination namespace.  A bad
    # later reference therefore cannot leave an earlier destination blob behind.
    payloads = {
        filename: _read_verified_media_reference(source_session_id, filename)
        for filename in filenames
    }
    staged = {}
    created = []
    destination_sid = _validated_session_id(destination_session_id)
    with _session_media_authority(destination_sid), _MEDIA_IO_LOCK:
        with _open_private_session(destination_sid, create=True) as (
            state_fd,
            media_fd,
            directory_fd,
            session_created,
        ):
            try:
                _assert_private_handles(
                    state_fd,
                    media_fd,
                    directory_fd,
                    destination_sid,
                )
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
    # Portable session JSON is the only enabled write format.  Keep this
    # no-op as the centralized save hook so callers cannot accidentally revive
    # compact reference creation while exact retirement is unavailable.
    if not _PRIVATE_MEDIA_EXTERNALIZATION_ENABLED:
        return 0
    # Unsupported platforms keep portable data URLs inline. No compact private
    # reference is ever persisted unless every operation needed to read, clone,
    # commit, roll back, and remove it has an anchored handle implementation.
    if not _DIR_FD_OK:
        return 0
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


def _remove_tree_at(parent_fd: int, name: str, *, expected_fd: int | None = None) -> None:
    """Remove exactly one held child tree without following replacements."""
    child_fd = expected_fd
    owns_fd = child_fd is None
    if child_fd is None:
        try:
            child_fd = os.open(name, os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW, dir_fd=parent_fd)
        except FileNotFoundError:
            return
    try:
        for entry_name in os.listdir(child_fd):
            try:
                entry_fd = os.open(
                    entry_name,
                    os.O_RDONLY | _O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0),
                    dir_fd=child_fd,
                )
            except OSError as exc:
                raise SessionMediaIntegrityError(
                    "Could not hold private media entry for removal"
                ) from exc
            held = os.fstat(entry_fd)
            quarantine_name = f".remove-{secrets.token_hex(16)}"
            try:
                if not (stat.S_ISDIR(held.st_mode) or stat.S_ISREG(held.st_mode)):
                    raise SessionMediaIntegrityError(
                        "Unexpected entry in private session media directory"
                    )
                os.replace(
                    entry_name,
                    quarantine_name,
                    src_dir_fd=child_fd,
                    dst_dir_fd=child_fd,
                )
                _fsync_dir(child_fd)
                if stat.S_ISDIR(held.st_mode):
                    _assert_entry_still_names_fd(
                        child_fd,
                        quarantine_name,
                        entry_fd,
                    )
                    _remove_tree_at(
                        child_fd,
                        quarantine_name,
                        expected_fd=entry_fd,
                    )
                else:
                    _assert_regular_entry_still_names_fd(
                        child_fd,
                        quarantine_name,
                        entry_fd,
                    )
                    _unlink_quarantined_regular_entry(
                        child_fd,
                        quarantine_name,
                        entry_fd,
                    )
            finally:
                os.close(entry_fd)
        # Keep the authoritative handle open through the last identity check
        # and rmdir. In particular, never close it and then resolve ``name``
        # again as the old implementation did.
        _assert_entry_still_names_fd(parent_fd, name, child_fd)
        _rmdir_quarantined_directory_entry(parent_fd, name, child_fd)
    finally:
        if owns_fd and child_fd is not None:
            os.close(child_fd)


def _deletion_quarantine_prefix(session_id: str) -> str:
    # Hashing makes the ownership prefix unambiguous: SID ``a`` must never
    # match a quarantine belonging to sibling SID ``a-b`` during retry.
    return f".delete-{hashlib.sha256(session_id.encode('utf-8')).hexdigest()}-"


def remove_session_media(session_id: str) -> None:
    """Delete the state-owned private media namespace for one session."""
    sid = _validated_session_id(session_id)
    if not _PRIVATE_MEDIA_EXTERNALIZATION_ENABLED:
        owners = session_media_destination_owners(sid)
        if owners:
            raise SessionMediaIntegrityError(
                "Private session media predates disabled externalization; "
                "automatic retirement is unavailable"
            )
        return
    if not _DIR_FD_OK:
        # A platform that never had the capability cannot create this tree via
        # this implementation. Absence is therefore a safe no-op; presence is
        # not deleted through a mutable pathname because that would reintroduce
        # the check-then-use parent-swap vulnerability this guard prevents.
        try:
            os.lstat(_state_root() / _PRIVATE_ROOT_NAME)
        except FileNotFoundError:
            return
        raise _unsupported_backend_error()
    with _session_media_authority(sid), _MEDIA_IO_LOCK:
        try:
            state_fd = _open_root_fd(_state_root())
            try:
                media_fd, _ = _open_child_dir(state_fd, _PRIVATE_ROOT_NAME, create=False)
                try:
                    quarantine_prefix = _deletion_quarantine_prefix(sid)
                    existing_quarantines = [
                        entry_name
                        for entry_name in os.listdir(media_fd)
                        if entry_name.startswith(quarantine_prefix)
                    ]
                    if existing_quarantines:
                        # A prior call already detached this SID from its
                        # public namespace. Retrying a pathname deletion of
                        # that held quarantine would reintroduce the same
                        # identity race (and recursively create more
                        # quarantines). Preserve the single durable retry
                        # authority and make the incomplete cleanup explicit.
                        raise SessionMediaIntegrityError(
                            "Exact private media retirement is unavailable; retaining quarantine"
                        )
                    try:
                        session_fd = os.open(
                            sid,
                            os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW,
                            dir_fd=media_fd,
                        )
                    except FileNotFoundError:
                        # Retry the durability barrier even when a prior call
                        # already completed the rename/remove operations.
                        _fsync_dir(media_fd)
                        return
                    quarantine = f"{quarantine_prefix}{secrets.token_hex(16)}"
                    try:
                        os.replace(
                            sid,
                            quarantine,
                            src_dir_fd=media_fd,
                            dst_dir_fd=media_fd,
                        )
                        _fsync_dir(media_fd)
                        _assert_entry_still_names_fd(media_fd, quarantine, session_fd)
                        _remove_tree_at(
                            media_fd,
                            quarantine,
                            expected_fd=session_fd,
                        )
                    finally:
                        os.close(session_fd)
                finally:
                    os.close(media_fd)
            finally:
                os.close(state_fd)
        except FileNotFoundError:
            return


def _retained_media_filenames(values) -> set[str]:
    retained: set[str] = set()

    def visit(node) -> None:
        if isinstance(node, dict):
            for child in node.values():
                visit(child)
        elif isinstance(node, list):
            for child in node:
                visit(child)
        elif isinstance(node, str) and node.startswith(_MEDIA_SCHEME):
            retained.add(_reference_filename(node))

    visit(values)
    return retained


def prune_session_media(session_id: str, retained_values) -> int:
    """Durably remove blobs unreachable from current and recovery payloads."""
    sid = _validated_session_id(session_id)
    if not _PRIVATE_MEDIA_EXTERNALIZATION_ENABLED:
        owners = session_media_destination_owners(sid)
        if owners:
            raise SessionMediaIntegrityError(
                "Private session media predates disabled externalization; "
                "automatic retirement is unavailable"
            )
        return 0
    retained = _retained_media_filenames(retained_values)
    if not _DIR_FD_OK:
        try:
            os.lstat(_state_root() / _PRIVATE_ROOT_NAME)
        except FileNotFoundError:
            return 0
        raise _unsupported_backend_error()
    removed = 0
    with _session_media_authority(sid), _MEDIA_IO_LOCK:
        try:
            with _open_private_session(sid, create=False) as handles:
                directory_fd = handles[2]
                for filename in os.listdir(directory_fd):
                    try:
                        entry_fd = os.open(
                            filename,
                            os.O_RDONLY
                            | _O_NOFOLLOW
                            | getattr(os, "O_NONBLOCK", 0),
                            dir_fd=directory_fd,
                        )
                    except OSError as exc:
                        raise SessionMediaIntegrityError(
                            "Could not hold private media entry for pruning"
                        ) from exc
                    try:
                        held = os.fstat(entry_fd)
                        if not stat.S_ISREG(held.st_mode):
                            raise SessionMediaIntegrityError(
                                "Unexpected entry in private session media directory"
                            )
                        if filename not in retained:
                            quarantine = f".prune-{secrets.token_hex(16)}"
                            os.replace(
                                filename,
                                quarantine,
                                src_dir_fd=directory_fd,
                                dst_dir_fd=directory_fd,
                            )
                            _fsync_dir(directory_fd)
                            _assert_regular_entry_still_names_fd(
                                directory_fd,
                                quarantine,
                                entry_fd,
                            )
                            _unlink_quarantined_regular_entry(
                                directory_fd,
                                quarantine,
                                entry_fd,
                            )
                            removed += 1
                    finally:
                        os.close(entry_fd)
                _fsync_dir(directory_fd)
        except FileNotFoundError:
            return 0
    return removed
