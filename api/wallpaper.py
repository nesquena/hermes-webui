"""Bounded, signature-based validation for uploaded wallpaper images."""

from __future__ import annotations

import ctypes
from dataclasses import dataclass
import errno
import hashlib
import logging
import os
import re
import secrets
import stat
import struct
import sys
import zlib


MAX_ENCODED_BYTES = 10 * 1024 * 1024
MAX_IMAGE_SIDE = 16_384
MAX_IMAGE_PIXELS = 50_000_000


class WallpaperValidationError(ValueError):
    """An uploaded wallpaper is unsupported or structurally invalid."""


class WallpaperTooLargeError(WallpaperValidationError):
    """An uploaded wallpaper exceeds the encoded-size limit."""


class WallpaperNotFoundError(RuntimeError):
    """No valid active wallpaper exists for the requested mutation."""


class WallpaperCollisionError(RuntimeError):
    """A content destination exists but cannot be safely reused."""


class WallpaperUnavailableError(RuntimeError):
    """Secure wallpaper storage is unavailable or changed identity."""


class WallpaperPersistenceError(RuntimeError):
    """Wallpaper mutation failed with an explicit settings commit state."""

    NOT_COMMITTED = "NOT_COMMITTED"
    COMMITTED_OR_INDETERMINATE = "COMMITTED_OR_INDETERMINATE"

    def __init__(self, commit_state: str) -> None:
        if commit_state not in {
            self.NOT_COMMITTED,
            self.COMMITTED_OR_INDETERMINATE,
        }:
            raise ValueError("invalid wallpaper persistence commit state")
        self.commit_state = commit_state
        super().__init__(f"Wallpaper persistence failed ({commit_state})")


_FileIdentity = tuple[int, int]
_CONTENT_NAME = re.compile(r"wallpaper-[0-9a-f]{64}\.(?:jpg|png|webp)\Z")
_STAGE_NAME = re.compile(r"\.wallpaper-stage-[0-9a-f]{32}\.tmp\Z")
_WALLPAPER_DIRECTORY = "wallpaper"
_RENAME_NOREPLACE_RACE_HOOK = lambda: None
_ATOMIC_UNLINK_IF_IDENTITY_ADAPTER = None
logger = logging.getLogger(__name__)


def _probe_rename_noreplace_adapter():
    try:
        library = ctypes.CDLL(None, use_errno=True)
        if sys.platform.startswith("linux"):
            rename = library.renameat2
            rename.argtypes = (
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_uint,
            )
            rename.restype = ctypes.c_int

            def _linux_rename_noreplace(src: str, dst: str, dir_fd: int) -> bool:
                result = rename(
                    dir_fd,
                    os.fsencode(src),
                    dir_fd,
                    os.fsencode(dst),
                    1,  # RENAME_NOREPLACE
                )
                if result == 0:
                    return True
                error = ctypes.get_errno()
                if error == errno.EEXIST:
                    return False
                raise OSError(error, os.strerror(error))

            return _linux_rename_noreplace
        if sys.platform == "darwin":
            rename = library.renameatx_np
            rename.argtypes = (
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_uint,
            )
            rename.restype = ctypes.c_int

            def _macos_rename_noreplace(src: str, dst: str, dir_fd: int) -> bool:
                result = rename(
                    dir_fd,
                    os.fsencode(src),
                    dir_fd,
                    os.fsencode(dst),
                    0x00000004,  # RENAME_EXCL
                )
                if result == 0:
                    return True
                error = ctypes.get_errno()
                if error == errno.EEXIST:
                    return False
                raise OSError(error, os.strerror(error))

            return _macos_rename_noreplace
    except (AttributeError, OSError):
        return None
    return None


_RENAME_NOREPLACE_ADAPTER = _probe_rename_noreplace_adapter()


def _file_identity(metadata: os.stat_result) -> _FileIdentity:
    return metadata.st_dev, metadata.st_ino


def _valid_entry_name(name: object) -> bool:
    return isinstance(name, str) and bool(
        _CONTENT_NAME.fullmatch(name) or _STAGE_NAME.fullmatch(name)
    )


def _require_storage_capabilities(*, require_noreplace: bool = True) -> None:
    if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY"):
        raise WallpaperUnavailableError("required descriptor capability unavailable")
    if any(operation not in os.supports_dir_fd for operation in (os.open, os.mkdir, os.stat, os.unlink)):
        raise WallpaperUnavailableError("required dir_fd capability unavailable")
    if os.stat not in os.supports_follow_symlinks:
        raise WallpaperUnavailableError("required no-follow stat capability unavailable")
    if os.listdir not in os.supports_fd and os.scandir not in os.supports_fd:
        raise WallpaperUnavailableError("required descriptor enumeration unavailable")
    if require_noreplace and _RENAME_NOREPLACE_ADAPTER is None:
        raise WallpaperUnavailableError("required no-replace capability unavailable")


def _settings_lock_is_owned() -> bool:
    from api.config import _SETTINGS_WRITE_LOCK

    is_owned = getattr(_SETTINGS_WRITE_LOCK, "_is_owned", None)
    return bool(is_owned and is_owned())


@dataclass
class _WallpaperDir:
    fd: int
    state_fd: int
    state_identity: _FileIdentity
    identity: _FileIdentity

    @classmethod
    def open_locked(cls) -> _WallpaperDir:
        if not _settings_lock_is_owned():
            raise WallpaperUnavailableError("caller must own _SETTINGS_WRITE_LOCK")
        return cls.open_for_staging()

    @classmethod
    def open_for_cleanup(cls) -> _WallpaperDir:
        if not _settings_lock_is_owned():
            raise WallpaperUnavailableError("caller must own _SETTINGS_WRITE_LOCK")
        return cls.open_for_staging(require_noreplace=False)

    @classmethod
    def open_for_staging(cls, *, require_noreplace: bool = True) -> _WallpaperDir:
        _require_storage_capabilities(require_noreplace=require_noreplace)
        from api import config

        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        state_fd = -1
        wallpaper_fd = -1
        try:
            state_fd = os.open(config.STATE_DIR, flags)
            state_metadata = os.fstat(state_fd)
            if not stat.S_ISDIR(state_metadata.st_mode):
                raise WallpaperUnavailableError("state root is not a directory")
            try:
                os.mkdir(_WALLPAPER_DIRECTORY, mode=0o700, dir_fd=state_fd)
            except FileExistsError:
                pass
            wallpaper_fd = os.open(_WALLPAPER_DIRECTORY, flags, dir_fd=state_fd)
            wallpaper_metadata = os.fstat(wallpaper_fd)
            if not stat.S_ISDIR(wallpaper_metadata.st_mode):
                raise WallpaperUnavailableError("wallpaper root is not a directory")
            root = cls(
                fd=wallpaper_fd,
                state_fd=state_fd,
                state_identity=_file_identity(state_metadata),
                identity=_file_identity(wallpaper_metadata),
            )
            root._reverify_identity(require_lock=False)
            wallpaper_fd = -1
            state_fd = -1
            return root
        except WallpaperUnavailableError:
            raise
        except (OSError, TypeError, ValueError) as exc:
            raise WallpaperUnavailableError("wallpaper descriptor root unavailable") from exc
        finally:
            if wallpaper_fd >= 0:
                os.close(wallpaper_fd)
            if state_fd >= 0:
                os.close(state_fd)

    def reverify_locked(self) -> None:
        self._reverify_identity(require_lock=True)

    def _reverify_identity(self, *, require_lock: bool) -> None:
        if require_lock and not _settings_lock_is_owned():
            raise WallpaperUnavailableError("caller must own _SETTINGS_WRITE_LOCK")
        from api import config

        try:
            state_before = os.lstat(config.STATE_DIR)
            state_descriptor = os.fstat(self.state_fd)
            wallpaper_path = os.stat(
                _WALLPAPER_DIRECTORY,
                dir_fd=self.state_fd,
                follow_symlinks=False,
            )
            wallpaper_descriptor = os.fstat(self.fd)
            state_after = os.lstat(config.STATE_DIR)
        except (OSError, TypeError, ValueError) as exc:
            raise WallpaperUnavailableError("wallpaper root identity unavailable") from exc
        if (
            not stat.S_ISDIR(state_before.st_mode)
            or not stat.S_ISDIR(state_descriptor.st_mode)
            or not stat.S_ISDIR(wallpaper_path.st_mode)
            or not stat.S_ISDIR(wallpaper_descriptor.st_mode)
            or _file_identity(state_before) != self.state_identity
            or _file_identity(state_descriptor) != self.state_identity
            or _file_identity(state_after) != self.state_identity
            or _file_identity(state_before) != _file_identity(state_after)
            or _file_identity(wallpaper_path) != self.identity
            or _file_identity(wallpaper_descriptor) != self.identity
        ):
            raise WallpaperUnavailableError("wallpaper root identity changed")

    def listdir(self) -> list[str]:
        try:
            if os.listdir in os.supports_fd:
                return os.listdir(self.fd)
            if os.scandir in os.supports_fd:
                with os.scandir(self.fd) as entries:
                    return [entry.name for entry in entries]
        except (OSError, TypeError, ValueError) as exc:
            raise WallpaperUnavailableError("wallpaper enumeration unavailable") from exc
        raise WallpaperUnavailableError("wallpaper descriptor enumeration unavailable")

    def close(self) -> None:
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1
        if self.state_fd >= 0:
            os.close(self.state_fd)
            self.state_fd = -1

    def __enter__(self) -> _WallpaperDir:
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()


@dataclass
class _StagedWallpaper:
    name: str
    fd: int

    def close(self) -> None:
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1

    def __enter__(self) -> _StagedWallpaper:
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()


def _write_all(fd: int, data: bytes) -> None:
    position = 0
    try:
        while position < len(data):
            written = os.write(fd, data[position:])
            if written <= 0:
                raise OSError("wallpaper stage write made no progress")
            position += written
    except (OSError, TypeError, ValueError) as exc:
        raise WallpaperUnavailableError("wallpaper stage write failed") from exc


def _write_stage(
    stage: _StagedWallpaper, root: _WallpaperDir, image: object
) -> ValidatedWallpaper:
    if stage.fd < 0:
        raise WallpaperUnavailableError("invalid wallpaper stage")
    try:
        data = bytes(_bounded_view(image))
        _write_all(stage.fd, data)
        os.fsync(stage.fd)
        return _validate_stage(stage, root.fd)
    except (WallpaperValidationError, WallpaperUnavailableError):
        raise
    except (OSError, TypeError, ValueError) as exc:
        raise WallpaperUnavailableError("wallpaper stage persistence failed") from exc


def _create_stage(dir_fd: int) -> _StagedWallpaper:
    for _ in range(16):
        name = f".wallpaper-stage-{secrets.token_hex(16)}.tmp"
        try:
            fd = os.open(
                name,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW,
                0o600,
                dir_fd=dir_fd,
            )
        except FileExistsError:
            continue
        except (OSError, TypeError, ValueError) as exc:
            raise WallpaperUnavailableError("wallpaper stage unavailable") from exc
        return _StagedWallpaper(name=name, fd=fd)
    raise WallpaperUnavailableError("wallpaper stage name unavailable")


def _safe_entry_stat(name: str, dir_fd: int) -> os.stat_result:
    if not _valid_entry_name(name):
        raise WallpaperUnavailableError("invalid wallpaper entry name")
    try:
        return os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except (OSError, TypeError, ValueError) as exc:
        raise WallpaperUnavailableError("wallpaper entry unavailable") from exc


def _verify_regular_identity(
    name: str,
    descriptor_metadata: os.stat_result,
    dir_fd: int,
    *,
    require_service_ownership: bool = False,
) -> None:
    path_metadata = _safe_entry_stat(name, dir_fd)
    if not stat.S_ISREG(descriptor_metadata.st_mode) or not stat.S_ISREG(path_metadata.st_mode):
        raise WallpaperUnavailableError("wallpaper entry is not a regular file")
    if _file_identity(descriptor_metadata) != _file_identity(path_metadata):
        raise WallpaperUnavailableError("wallpaper entry identity changed")
    if require_service_ownership:
        if descriptor_metadata.st_uid != os.geteuid():
            raise WallpaperUnavailableError("wallpaper stage ownership mismatch")
        if stat.S_IMODE(descriptor_metadata.st_mode) & ~0o600:
            raise WallpaperUnavailableError("wallpaper stage mode is too broad")


def _read_bounded_descriptor(fd: int) -> bytes:
    chunks = []
    remaining = MAX_ENCODED_BYTES + 1
    while remaining:
        chunk = os.read(fd, min(64 * 1024, remaining))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
        if remaining == 0:
            raise WallpaperUnavailableError("wallpaper content exceeds size limit")
    return b"".join(chunks)


def _read_verified_file(name: str, dir_fd: int) -> bytes:
    if not isinstance(name, str) or not _CONTENT_NAME.fullmatch(name):
        raise WallpaperUnavailableError("invalid wallpaper content name")
    initial_metadata = _safe_entry_stat(name, dir_fd)
    if not stat.S_ISREG(initial_metadata.st_mode):
        raise WallpaperUnavailableError("wallpaper entry is not a regular file")
    try:
        fd = os.open(
            name,
            os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW,
            dir_fd=dir_fd,
        )
    except (OSError, TypeError, ValueError) as exc:
        raise WallpaperUnavailableError("wallpaper content unavailable") from exc
    try:
        metadata = os.fstat(fd)
        if _file_identity(metadata) != _file_identity(initial_metadata):
            raise WallpaperUnavailableError("wallpaper entry identity changed")
        _verify_regular_identity(name, metadata, dir_fd)
        data = _read_bounded_descriptor(fd)
        final_metadata = os.fstat(fd)
        _verify_regular_identity(name, final_metadata, dir_fd)
        if _file_identity(final_metadata) != _file_identity(metadata):
            raise WallpaperUnavailableError("wallpaper entry identity changed")
        return data
    except WallpaperUnavailableError:
        raise
    except (OSError, TypeError, ValueError) as exc:
        raise WallpaperUnavailableError("wallpaper content unavailable") from exc
    finally:
        os.close(fd)


def _validate_stage(stage: _StagedWallpaper, dir_fd: int) -> ValidatedWallpaper:
    if stage.fd < 0 or not _STAGE_NAME.fullmatch(stage.name):
        raise WallpaperUnavailableError("invalid wallpaper stage")
    read_fd = -1
    try:
        retained_metadata = os.fstat(stage.fd)
        _verify_regular_identity(
            stage.name,
            retained_metadata,
            dir_fd,
            require_service_ownership=True,
        )
        read_fd = os.open(
            stage.name,
            os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW,
            dir_fd=dir_fd,
        )
        read_metadata = os.fstat(read_fd)
        if _file_identity(read_metadata) != _file_identity(retained_metadata):
            raise WallpaperUnavailableError("wallpaper stage identity changed")
        _verify_regular_identity(
            stage.name,
            read_metadata,
            dir_fd,
            require_service_ownership=True,
        )
        data = _read_bounded_descriptor(read_fd)
        final_read_metadata = os.fstat(read_fd)
        final_retained_metadata = os.fstat(stage.fd)
        if (
            _file_identity(final_read_metadata) != _file_identity(retained_metadata)
            or _file_identity(final_retained_metadata) != _file_identity(retained_metadata)
        ):
            raise WallpaperUnavailableError("wallpaper stage identity changed")
        _verify_regular_identity(
            stage.name,
            final_retained_metadata,
            dir_fd,
            require_service_ownership=True,
        )
        return validate_wallpaper(data)
    except (WallpaperUnavailableError, WallpaperValidationError):
        raise
    except (OSError, TypeError, ValueError) as exc:
        raise WallpaperUnavailableError("wallpaper stage unavailable") from exc
    finally:
        if read_fd >= 0:
            os.close(read_fd)


def _rename_noreplace(src: str, dst: str, dir_fd: int) -> bool:
    if (
        not isinstance(src, str)
        or not isinstance(dst, str)
        or not _STAGE_NAME.fullmatch(src)
        or not _CONTENT_NAME.fullmatch(dst)
    ):
        raise WallpaperUnavailableError("invalid wallpaper installation name")
    adapter = _RENAME_NOREPLACE_ADAPTER
    if adapter is None:
        raise WallpaperUnavailableError("required no-replace capability unavailable")
    try:
        _RENAME_NOREPLACE_RACE_HOOK()
        return bool(adapter(src, dst, dir_fd))
    except WallpaperUnavailableError:
        raise
    except OSError as exc:
        if exc.errno == errno.EEXIST:
            return False
        raise WallpaperUnavailableError("wallpaper no-replace install failed") from exc
    except (TypeError, ValueError) as exc:
        raise WallpaperUnavailableError("wallpaper no-replace install failed") from exc


def _install_stage_noreplace(
    stage: _StagedWallpaper, destination: str, dir_fd: int
) -> bool:
    if stage.fd < 0 or not _STAGE_NAME.fullmatch(stage.name):
        raise WallpaperUnavailableError("invalid wallpaper stage")
    try:
        retained_before = os.fstat(stage.fd)
        _verify_regular_identity(
            stage.name,
            retained_before,
            dir_fd,
            require_service_ownership=True,
        )
        if not _rename_noreplace(stage.name, destination, dir_fd):
            return False
        retained_after = os.fstat(stage.fd)
        installed = _safe_entry_stat(destination, dir_fd)
        if (
            not stat.S_ISREG(installed.st_mode)
            or _file_identity(retained_after) != _file_identity(retained_before)
            or _file_identity(installed) != _file_identity(retained_before)
        ):
            raise WallpaperUnavailableError("installed wallpaper identity changed")
        return True
    except WallpaperUnavailableError:
        raise
    except (OSError, TypeError, ValueError) as exc:
        raise WallpaperUnavailableError("wallpaper installation unavailable") from exc


def _unlink_if_identity(
    name: str, expected_identity: _FileIdentity, dir_fd: int
) -> bool:
    if not _valid_entry_name(name):
        return False
    if (
        not isinstance(expected_identity, tuple)
        or len(expected_identity) != 2
        or not all(isinstance(value, int) for value in expected_identity)
    ):
        return False
    adapter = _ATOMIC_UNLINK_IF_IDENTITY_ADAPTER
    if adapter is None:
        return False
    try:
        return bool(adapter(name, expected_identity, dir_fd))
    except (OSError, TypeError, ValueError):
        return False


@dataclass(frozen=True)
class ValidatedWallpaper:
    mime_type: str
    extension: str
    width: int
    height: int
    digest: str
    size: int


@dataclass(frozen=True)
class WallpaperInfo:
    has_wallpaper: bool
    opacity: float
    scope: str
    mime_type: str | None
    image_version: str | None


@dataclass
class WallpaperSnapshot:
    fd: int
    file: object
    size: int
    mime_type: str
    etag: str

    def close(self) -> None:
        if not self.file.closed:
            self.file.close()
        self.fd = -1

    def __enter__(self) -> WallpaperSnapshot:
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()


_ValidationCacheKey = tuple[int, int, int, int]
_VALIDATION_CACHE: tuple[_ValidationCacheKey, ValidatedWallpaper] | None = None


class _Cursor:
    __slots__ = ("data", "position", "size")

    def __init__(self, data: memoryview) -> None:
        self.data = data
        self.position = 0
        self.size = len(data)

    def remaining(self) -> int:
        return self.size - self.position

    def read_u8(self) -> int:
        if self.position >= self.size:
            _invalid("truncated image")
        value = self.data[self.position]
        self.position += 1
        return value

    def read_u16(self) -> int:
        if self.remaining() < 2:
            _invalid("truncated image")
        value = (self.data[self.position] << 8) | self.data[self.position + 1]
        self.position += 2
        return value

    def skip(self, count: int) -> None:
        if count < 0 or count > self.remaining():
            _invalid("truncated image")
        self.position += count


def _invalid(reason: str) -> None:
    raise WallpaperValidationError(reason)


def _bounded_view(image: object) -> memoryview:
    try:
        view = memoryview(image)
    except (TypeError, ValueError):
        _invalid("wallpaper must be encoded bytes")
    if view.ndim != 1 or view.itemsize != 1:
        _invalid("wallpaper must be encoded bytes")
    if len(view) == 0:
        _invalid("empty wallpaper")
    if len(view) > MAX_ENCODED_BYTES:
        raise WallpaperTooLargeError("wallpaper exceeds encoded size limit")
    try:
        view = view.cast("B")
    except (TypeError, ValueError):
        _invalid("wallpaper must be contiguous encoded bytes")
    if not isinstance(image, bytes):
        view = memoryview(bytes(view))
    return view


def validate_wallpaper(
    image: object, mime_type: str | None = None, filename: str | None = None
) -> ValidatedWallpaper:
    """Validate encoded bytes by signature and return trusted image metadata.

    ``mime_type`` and ``filename`` are accepted only for caller compatibility and
    deliberately do not participate in format detection or validation.
    """
    del mime_type, filename
    data = _bounded_view(image)
    if len(data) >= 2 and data[0] == 0xFF and data[1] == 0xD8:
        width, height = _validate_jpeg(data)
        trusted_mime = "image/jpeg"
        extension = "jpg"
    elif len(data) >= 8 and bytes(data[:8]) == _PNG_SIGNATURE:
        width, height = _validate_png(data)
        trusted_mime = "image/png"
        extension = "png"
    elif len(data) >= 4 and bytes(data[:4]) == b"RIFF":
        width, height = _validate_webp(data)
        trusted_mime = "image/webp"
        extension = "webp"
    else:
        _invalid("unsupported wallpaper format")

    # Hashing is intentionally last: invalid structures never consume hashing work.
    digest = hashlib.sha256(data).hexdigest()
    return ValidatedWallpaper(
        mime_type=trusted_mime,
        extension=extension,
        width=width,
        height=height,
        digest=digest,
        size=len(data),
    )


def _validation_cache_key(metadata: os.stat_result) -> _ValidationCacheKey:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
    )


def _absent_wallpaper_info() -> WallpaperInfo:
    return WallpaperInfo(
        has_wallpaper=False,
        opacity=0.8,
        scope="chat",
        mime_type=None,
        image_version=None,
    )


def _validated_active_descriptor_locked(
    name: str, root: _WallpaperDir
) -> tuple[int, ValidatedWallpaper]:
    global _VALIDATION_CACHE

    if not isinstance(name, str) or not _CONTENT_NAME.fullmatch(name):
        raise WallpaperUnavailableError("invalid wallpaper content name")
    initial_metadata = _safe_entry_stat(name, root.fd)
    if not stat.S_ISREG(initial_metadata.st_mode):
        raise WallpaperUnavailableError("wallpaper entry is not a regular file")

    fd = -1
    try:
        fd = os.open(
            name,
            os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW,
            dir_fd=root.fd,
        )
        descriptor_metadata = os.fstat(fd)
        if _file_identity(descriptor_metadata) != _file_identity(initial_metadata):
            raise WallpaperUnavailableError("wallpaper entry identity changed")
        _verify_regular_identity(name, descriptor_metadata, root.fd)
        cache_key = _validation_cache_key(descriptor_metadata)
        cached = _VALIDATION_CACHE
        if cached is not None and cached[0] == cache_key:
            validated = cached[1]
        else:
            data = _read_bounded_descriptor(fd)
            validated = validate_wallpaper(data)
            if validated.size != descriptor_metadata.st_size:
                raise WallpaperUnavailableError("wallpaper size changed during validation")

        final_metadata = os.fstat(fd)
        _verify_regular_identity(name, final_metadata, root.fd)
        root.reverify_locked()
        if _validation_cache_key(final_metadata) != cache_key:
            raise WallpaperUnavailableError("wallpaper entry changed during validation")
        expected_name = f"wallpaper-{validated.digest}.{validated.extension}"
        if name != expected_name:
            raise WallpaperValidationError("wallpaper digest does not match active name")
        _VALIDATION_CACHE = (cache_key, validated)
        os.lseek(fd, 0, os.SEEK_SET)
        retained_fd = fd
        fd = -1
        return retained_fd, validated
    except (WallpaperUnavailableError, WallpaperValidationError):
        raise
    except (OSError, TypeError, ValueError) as exc:
        raise WallpaperUnavailableError("wallpaper content unavailable") from exc
    finally:
        if fd >= 0:
            os.close(fd)


def _repair_invalid_active_locked() -> None:
    from api import config

    global _VALIDATION_CACHE
    _VALIDATION_CACHE = None
    try:
        config._save_wallpaper_settings_locked({"wallpaper_file": ""})
    except config.SettingsPersistenceError as exc:
        if (
            exc.commit_state
            == config.SettingsPersistenceError.COMMITTED_OR_INDETERMINATE
        ):
            config.load_settings()


def _open_authoritative_locked() -> tuple[WallpaperInfo, int | None]:
    from api import config

    settings = config.load_settings()
    name = settings["wallpaper_file"]
    if not name:
        return _absent_wallpaper_info(), None

    try:
        with _WallpaperDir.open_locked() as root:
            root.reverify_locked()
            fd, validated = _validated_active_descriptor_locked(name, root)
    except (WallpaperUnavailableError, WallpaperValidationError):
        _repair_invalid_active_locked()
        return _absent_wallpaper_info(), None

    return (
        WallpaperInfo(
            has_wallpaper=True,
            opacity=settings["wallpaper_opacity"],
            scope=settings["wallpaper_scope"],
            mime_type=validated.mime_type,
            image_version=validated.digest,
        ),
        fd,
    )


def get_wallpaper_info() -> WallpaperInfo:
    from api import config

    with config._SETTINGS_WRITE_LOCK:
        info, fd = _open_authoritative_locked()
        if fd is not None:
            os.close(fd)
        return info


def open_wallpaper_snapshot() -> WallpaperSnapshot | None:
    from api import config

    with config._SETTINGS_WRITE_LOCK:
        info, fd = _open_authoritative_locked()
        if fd is None:
            return None
        try:
            file = os.fdopen(fd, "rb", closefd=True)
        except Exception:
            os.close(fd)
            raise
        return WallpaperSnapshot(
            fd=fd,
            file=file,
            size=os.fstat(fd).st_size,
            mime_type=info.mime_type or "application/octet-stream",
            etag=f'"{info.image_version}"',
        )


def _fsync_wallpaper_directory(root: _WallpaperDir) -> None:
    try:
        os.fsync(root.fd)
    except (OSError, TypeError, ValueError) as exc:
        raise WallpaperUnavailableError("wallpaper directory sync failed") from exc


def _installed_identity(destination: str, stage: _StagedWallpaper, root: _WallpaperDir) -> _FileIdentity:
    try:
        retained = os.fstat(stage.fd)
        installed = _safe_entry_stat(destination, root.fd)
    except WallpaperUnavailableError:
        raise
    except (OSError, TypeError, ValueError) as exc:
        raise WallpaperUnavailableError("installed wallpaper identity unavailable") from exc
    if (
        not stat.S_ISREG(retained.st_mode)
        or not stat.S_ISREG(installed.st_mode)
        or _file_identity(retained) != _file_identity(installed)
    ):
        raise WallpaperUnavailableError("installed wallpaper identity changed")
    return _file_identity(retained)


def _validated_content_identity_locked(
    name: str, root: _WallpaperDir
) -> tuple[_FileIdentity, ValidatedWallpaper]:
    fd = -1
    try:
        fd, validated = _validated_active_descriptor_locked(name, root)
        return _file_identity(os.fstat(fd)), validated
    finally:
        if fd >= 0:
            os.close(fd)


def _active_identity_locked(
    name: str, root: _WallpaperDir
) -> tuple[_FileIdentity, ValidatedWallpaper] | None:
    if not name:
        return None
    try:
        return _validated_content_identity_locked(name, root)
    except WallpaperValidationError as exc:
        raise WallpaperUnavailableError("active wallpaper validation failed") from exc


def _decide_destination_locked(
    destination: str,
    expected: ValidatedWallpaper,
    prior_name: str,
    prior_active: tuple[_FileIdentity, ValidatedWallpaper] | None,
    root: _WallpaperDir,
) -> tuple[bool, _FileIdentity | None]:
    if destination == prior_name and prior_active is not None:
        identity, validated = prior_active
        if validated == expected:
            return True, identity
        raise WallpaperCollisionError("wallpaper content collision")

    try:
        os.stat(destination, dir_fd=root.fd, follow_symlinks=False)
    except FileNotFoundError:
        return False, None
    except (OSError, TypeError, ValueError) as exc:
        raise WallpaperUnavailableError("wallpaper destination unavailable") from exc

    try:
        identity, validated = _validated_content_identity_locked(destination, root)
    except (WallpaperUnavailableError, WallpaperValidationError) as exc:
        raise WallpaperCollisionError("wallpaper content collision") from exc
    if validated != expected:
        raise WallpaperCollisionError("wallpaper content collision")
    return True, identity


def _verify_destination_identity_locked(
    destination: str,
    expected_identity: _FileIdentity,
    root: _WallpaperDir,
) -> None:
    metadata = _safe_entry_stat(destination, root.fd)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or _file_identity(metadata) != expected_identity
    ):
        raise WallpaperUnavailableError("wallpaper destination identity changed")


def _rollback_installed(
    destination: str,
    installed_identity: _FileIdentity | None,
    prior_name: str,
    root: _WallpaperDir,
) -> None:
    if installed_identity is None or destination == prior_name:
        return
    try:
        if _unlink_if_identity(destination, installed_identity, root.fd):
            _fsync_wallpaper_directory(root)
    except Exception:
        logger.warning("Wallpaper rollback cleanup failed")


def _best_effort_remove(
    name: str,
    identity: _FileIdentity | None,
    root: _WallpaperDir,
) -> None:
    if not name or identity is None:
        return
    try:
        _unlink_if_identity(name, identity, root.fd)
    except Exception:
        logger.warning("Wallpaper post-commit cleanup failed")


def _best_effort_final_sync(root: _WallpaperDir) -> None:
    try:
        _fsync_wallpaper_directory(root)
    except Exception:
        logger.warning("Wallpaper post-commit directory sync failed")


def _wallpaper_info(
    validated: ValidatedWallpaper, opacity: float, scope: str
) -> WallpaperInfo:
    return WallpaperInfo(
        has_wallpaper=True,
        opacity=opacity,
        scope=scope,
        mime_type=validated.mime_type,
        image_version=validated.digest,
    )


def _raise_persistence(exc) -> None:
    raise WallpaperPersistenceError(exc.commit_state) from exc


def replace_wallpaper(
    image: object, *, opacity: float = 0.8, scope: str = "chat"
) -> WallpaperInfo:
    """Stage, validate, and durably install a content-addressed wallpaper."""
    from api import config

    config._validate_wallpaper_internal_update(
        {"wallpaper_opacity": opacity, "wallpaper_scope": scope}
    )
    with _WallpaperDir.open_for_staging() as root:
        with _create_stage(root.fd) as stage:
            validated = _write_stage(stage, root, image)
            destination = f"wallpaper-{validated.digest}.{validated.extension}"
            installed_identity: _FileIdentity | None = None
            prior_name = ""
            prior_identity: _FileIdentity | None = None
            with config._SETTINGS_WRITE_LOCK:
                root.reverify_locked()
                settings = config.load_settings()
                prior_name = settings["wallpaper_file"]
                prior_active = _active_identity_locked(prior_name, root)
                if prior_active is not None:
                    prior_identity = prior_active[0]
                reused, destination_identity = _decide_destination_locked(
                    destination,
                    validated,
                    prior_name,
                    prior_active,
                    root,
                )
                if not reused:
                    retained_stage_identity = _file_identity(os.fstat(stage.fd))
                    try:
                        installed = _install_stage_noreplace(
                            stage, destination, root.fd
                        )
                    except Exception:
                        _rollback_installed(
                            destination,
                            retained_stage_identity,
                            prior_name,
                            root,
                        )
                        raise
                    if not installed:
                        raise WallpaperCollisionError("wallpaper content collision")
                    installed_identity = retained_stage_identity
                    try:
                        installed_identity = _installed_identity(
                            destination, stage, root
                        )
                    except Exception:
                        _rollback_installed(
                            destination, installed_identity, prior_name, root
                        )
                        raise
                    try:
                        _fsync_wallpaper_directory(root)
                    except Exception as exc:
                        _rollback_installed(
                            destination, installed_identity, prior_name, root
                        )
                        raise WallpaperPersistenceError(
                            WallpaperPersistenceError.NOT_COMMITTED
                        ) from exc
                    destination_identity = installed_identity

                assert destination_identity is not None
                try:
                    _verify_destination_identity_locked(
                        destination, destination_identity, root
                    )
                except Exception:
                    _rollback_installed(
                        destination, installed_identity, prior_name, root
                    )
                    raise

                update = {
                    "wallpaper_file": destination,
                    "wallpaper_opacity": opacity,
                    "wallpaper_scope": scope,
                }
                try:
                    config._save_wallpaper_settings_locked(update)
                except config.SettingsPersistenceError as exc:
                    if exc.commit_state == exc.NOT_COMMITTED:
                        _rollback_installed(
                            destination, installed_identity, prior_name, root
                        )
                    else:
                        config.load_settings()
                    _raise_persistence(exc)

                global _VALIDATION_CACHE
                _VALIDATION_CACHE = None
                if prior_name and prior_name != destination:
                    _best_effort_remove(prior_name, prior_identity, root)
                _best_effort_final_sync(root)
                return _wallpaper_info(validated, opacity, scope)


def update_wallpaper_metadata(
    *, opacity: float | None = None, scope: str | None = None
) -> WallpaperInfo:
    """Persist metadata for a currently valid active wallpaper."""
    from api import config

    update = {}
    if opacity is not None:
        update["wallpaper_opacity"] = opacity
    if scope is not None:
        update["wallpaper_scope"] = scope
    config._validate_wallpaper_internal_update(update)
    with config._SETTINGS_WRITE_LOCK:
        settings = config.load_settings()
        name = settings["wallpaper_file"]
        if not name:
            raise WallpaperNotFoundError("active wallpaper not found")
        with _WallpaperDir.open_locked() as root:
            root.reverify_locked()
            active = _active_identity_locked(name, root)
            if active is None:
                raise WallpaperNotFoundError("active wallpaper not found")
            _, validated = active
            try:
                saved = config._save_wallpaper_settings_locked(update)
            except config.SettingsPersistenceError as exc:
                if exc.commit_state == exc.COMMITTED_OR_INDETERMINATE:
                    saved = config.load_settings()
                _raise_persistence(exc)
            return _wallpaper_info(
                validated,
                saved["wallpaper_opacity"],
                saved["wallpaper_scope"],
            )


def clear_wallpaper() -> WallpaperInfo:
    """Persist all wallpaper defaults before best-effort conditional cleanup."""
    from api import config

    defaults = {
        "wallpaper_file": "",
        "wallpaper_opacity": 0.8,
        "wallpaper_scope": "chat",
    }
    with config._SETTINGS_WRITE_LOCK:
        settings = config.load_settings()
        prior_name = settings["wallpaper_file"]
        try:
            config._save_wallpaper_settings_locked(defaults)
        except config.SettingsPersistenceError as exc:
            if exc.commit_state == exc.COMMITTED_OR_INDETERMINATE:
                config.load_settings()
            _raise_persistence(exc)

        if prior_name:
            try:
                with _WallpaperDir.open_for_cleanup() as root:
                    root.reverify_locked()
                    active = _active_identity_locked(prior_name, root)
                    prior_identity = active[0] if active is not None else None
                    _best_effort_remove(prior_name, prior_identity, root)
                    _best_effort_final_sync(root)
            except Exception:
                logger.warning("Wallpaper post-commit cleanup unavailable")
        global _VALIDATION_CACHE
        _VALIDATION_CACHE = None
        return _absent_wallpaper_info()


def _validated_cleanup_identity(
    name: str, root: _WallpaperDir, *, stage: bool
) -> _FileIdentity:
    """Validate one enumerated cleanup candidate and retain its exact identity."""
    pattern = _STAGE_NAME if stage else _CONTENT_NAME
    if not isinstance(name, str) or pattern.fullmatch(name) is None:
        raise WallpaperUnavailableError("invalid wallpaper cleanup entry")
    initial = _safe_entry_stat(name, root.fd)
    if not stat.S_ISREG(initial.st_mode):
        raise WallpaperUnavailableError("wallpaper cleanup entry is not regular")

    fd = -1
    try:
        fd = os.open(
            name,
            os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW,
            dir_fd=root.fd,
        )
        descriptor = os.fstat(fd)
        if _file_identity(descriptor) != _file_identity(initial):
            raise WallpaperUnavailableError("wallpaper cleanup identity changed")
        _verify_regular_identity(
            name,
            descriptor,
            root.fd,
            require_service_ownership=stage,
        )
        data = _read_bounded_descriptor(fd)
        validated = validate_wallpaper(data)
        final = os.fstat(fd)
        _verify_regular_identity(
            name,
            final,
            root.fd,
            require_service_ownership=stage,
        )
        if (
            _file_identity(final) != _file_identity(descriptor)
            or _validation_cache_key(final) != _validation_cache_key(descriptor)
            or validated.size != final.st_size
        ):
            raise WallpaperUnavailableError("wallpaper cleanup entry changed")
        if not stage:
            expected_name = f"wallpaper-{validated.digest}.{validated.extension}"
            if name != expected_name:
                raise WallpaperValidationError("wallpaper cleanup digest mismatch")
        root.reverify_locked()
        return _file_identity(final)
    except (WallpaperUnavailableError, WallpaperValidationError):
        raise
    except (OSError, TypeError, ValueError) as exc:
        raise WallpaperUnavailableError("wallpaper cleanup entry unavailable") from exc
    finally:
        if fd >= 0:
            os.close(fd)


def cleanup_wallpaper_orphans() -> None:
    """Best-effort cleanup of provably owned stages and validated content orphans.

    Physical removal is intentionally unavailable unless the platform supplies an
    atomic compare-identity-and-unlink adapter. A plain pathname unlink is never a
    fallback.
    """
    from api import config

    if _ATOMIC_UNLINK_IF_IDENTITY_ADAPTER is None:
        return
    try:
        with config._SETTINGS_WRITE_LOCK:
            with _WallpaperDir.open_for_cleanup() as root:
                root.reverify_locked()
                active_name = config.load_settings()["wallpaper_file"]
                entries = root.listdir()
                for name in entries:
                    is_stage = isinstance(name, str) and _STAGE_NAME.fullmatch(name)
                    is_content = isinstance(name, str) and _CONTENT_NAME.fullmatch(name)
                    if not is_stage and not is_content:
                        continue
                    if is_content and name == active_name:
                        continue
                    try:
                        identity = _validated_cleanup_identity(
                            name, root, stage=bool(is_stage)
                        )
                        # Reconfirm authoritative state under the shared lock directly
                        # before the adapter's atomic identity boundary.
                        if is_content and config.load_settings()["wallpaper_file"] == name:
                            continue
                        if _unlink_if_identity(name, identity, root.fd):
                            _fsync_wallpaper_directory(root)
                    except Exception:
                        logger.warning("Wallpaper startup cleanup skipped one entry")
    except WallpaperUnavailableError:
        logger.warning("Wallpaper startup cleanup unavailable")


_WEBP_VP8X_ICCP = 0x20
_WEBP_VP8X_ALPHA = 0x10
_WEBP_VP8X_EXIF = 0x08
_WEBP_VP8X_XMP = 0x04
_WEBP_VP8X_ANIMATION = 0x02
_WEBP_VP8X_ALLOWED_FLAGS = 0x3E


@dataclass(frozen=True)
class _WebpChunk:
    name: bytes
    payload: memoryview


class _WebpBitReader:
    __slots__ = ("data", "position")

    def __init__(self, data: memoryview) -> None:
        self.data = data
        self.position = 0

    def read(self, count: int) -> int:
        if count < 0 or self.position + count > len(self.data) * 8:
            _invalid("truncated WebP lossless stream")
        value = 0
        for offset in range(count):
            value |= (
                (self.data[(self.position + offset) // 8]
                 >> ((self.position + offset) % 8))
                & 1
            ) << offset
        self.position += count
        return value

    def finish(self) -> None:
        byte_position, bit_position = divmod(self.position, 8)
        if byte_position >= len(self.data):
            if byte_position != len(self.data) or bit_position:
                _invalid("invalid WebP lossless stream boundary")
            return
        if byte_position != len(self.data) - 1:
            _invalid("trailing WebP lossless bytes")
        unused_mask = 0xFF ^ ((1 << bit_position) - 1)
        if bit_position == 0 or self.data[byte_position] & unused_mask:
            _invalid("nonzero trailing WebP lossless bits")


def _webp_u24(data: memoryview, position: int) -> int:
    if position < 0 or position + 3 > len(data):
        _invalid("truncated WebP field")
    return int.from_bytes(data[position : position + 3], "little")


def _parse_webp_chunks(data: memoryview) -> list[_WebpChunk]:
    if len(data) < 12 or bytes(data[:4]) != b"RIFF" or bytes(data[8:12]) != b"WEBP":
        _invalid("invalid WebP RIFF header")
    riff_size = int.from_bytes(data[4:8], "little")
    if riff_size != len(data) - 8:
        _invalid("WebP RIFF size does not match complete file")

    chunks = []
    position = 12
    while position < len(data):
        if len(data) - position < 8:
            _invalid("truncated WebP chunk header")
        name = bytes(data[position : position + 4])
        payload_size = int.from_bytes(data[position + 4 : position + 8], "little")
        payload_start = position + 8
        payload_end = payload_start + payload_size
        padded_end = payload_end + (payload_size & 1)
        if payload_end < payload_start or padded_end > len(data):
            _invalid("truncated WebP chunk")
        if payload_size & 1 and data[payload_end] != 0:
            _invalid("WebP chunk padding must be zero")
        if len(chunks) >= 6:
            _invalid("WebP exceeds static chunk cardinality")
        chunks.append(_WebpChunk(name, data[payload_start:payload_end]))
        position = padded_end
    if position != len(data):
        _invalid("invalid WebP RIFF boundary")
    return chunks


def _validate_vp8(payload: memoryview) -> tuple[int, int, bool]:
    if len(payload) <= 10:
        _invalid("truncated VP8 key frame")
    frame_tag = int.from_bytes(payload[:3], "little")
    if frame_tag & 1:
        _invalid("WebP VP8 image is not a key frame")
    if ((frame_tag >> 1) & 0x07) > 3:
        _invalid("unsupported WebP VP8 version")
    if not ((frame_tag >> 4) & 1):
        _invalid("hidden WebP VP8 frame")
    partition_length = frame_tag >> 5
    if partition_length == 0 or partition_length > len(payload) - 10:
        _invalid("invalid WebP VP8 first partition")
    if bytes(payload[3:6]) != b"\x9d\x01\x2a":
        _invalid("invalid WebP VP8 start code")
    horizontal = int.from_bytes(payload[6:8], "little")
    vertical = int.from_bytes(payload[8:10], "little")
    if horizontal >> 14 or vertical >> 14:
        _invalid("scaled WebP VP8 frames are unsupported")
    width = horizontal & 0x3FFF
    height = vertical & 0x3FFF
    _check_dimensions(width, height)
    return width, height, False


def _read_webp_simple_huffman_symbol(reader: _WebpBitReader) -> int:
    if reader.read(1) != 1 or reader.read(1) != 0:
        _invalid("unsupported WebP lossless Huffman tree")
    if reader.read(1) != 1:
        _invalid("WebP lossless subset requires one-symbol Huffman trees")
    return reader.read(8)


def _validate_vp8l(payload: memoryview) -> tuple[int, int, bool]:
    if len(payload) < 6 or payload[0] != 0x2F:
        _invalid("invalid WebP lossless signature or truncated stream")
    header = int.from_bytes(payload[1:5], "little")
    width = (header & 0x3FFF) + 1
    height = ((header >> 14) & 0x3FFF) + 1
    alpha = bool((header >> 28) & 1)
    if header >> 29:
        _invalid("unsupported WebP lossless version")
    _check_dimensions(width, height)

    reader = _WebpBitReader(payload[5:])
    if reader.read(1):
        _invalid("WebP lossless transforms are unsupported")
    if reader.read(1):
        _invalid("WebP lossless color cache is unsupported")
    if reader.read(1):
        _invalid("WebP lossless meta Huffman groups are unsupported")
    symbols = tuple(_read_webp_simple_huffman_symbol(reader) for _ in range(5))
    if symbols[4] != 0:
        _invalid("unsupported WebP lossless distance tree")
    reader.finish()
    return width, height, alpha


def _validate_webp_image(chunk: _WebpChunk) -> tuple[int, int, bool]:
    if chunk.name == b"VP8 ":
        return _validate_vp8(chunk.payload)
    if chunk.name == b"VP8L":
        return _validate_vp8l(chunk.payload)
    _invalid("WebP is missing its image chunk")


def _validate_webp_extended(chunks: list[_WebpChunk]) -> tuple[int, int]:
    if not chunks or chunks[0].name != b"VP8X" or len(chunks[0].payload) != 10:
        _invalid("WebP VP8X must be one 10-byte first chunk")
    header = chunks[0].payload
    flags = header[0]
    if flags & ~_WEBP_VP8X_ALLOWED_FLAGS or any(header[1:4]):
        _invalid("WebP VP8X reserved fields must be zero")
    if flags & _WEBP_VP8X_ANIMATION:
        _invalid("animated WebP is unsupported")
    canvas_width = _webp_u24(header, 4) + 1
    canvas_height = _webp_u24(header, 7) + 1
    _check_dimensions(canvas_width, canvas_height)

    expected = [b"VP8X"]
    if flags & _WEBP_VP8X_ICCP:
        expected.append(b"ICCP")
    alpha_flag = bool(flags & _WEBP_VP8X_ALPHA)
    image_position = 1 + bool(flags & _WEBP_VP8X_ICCP)
    if image_position >= len(chunks):
        _invalid("WebP is missing its image chunk")
    if chunks[image_position].name == b"ALPH":
        expected.append(b"ALPH")
        image_position += 1
    if image_position >= len(chunks) or chunks[image_position].name not in (
        b"VP8 ",
        b"VP8L",
    ):
        _invalid("invalid WebP extended chunk order")
    image_name = chunks[image_position].name
    expected.append(image_name)
    if flags & _WEBP_VP8X_EXIF:
        expected.append(b"EXIF")
    if flags & _WEBP_VP8X_XMP:
        expected.append(b"XMP ")
    if [chunk.name for chunk in chunks] != expected:
        _invalid("invalid WebP extended chunk order or flags")

    width, height, lossless_alpha = _validate_webp_image(chunks[image_position])
    if (width, height) != (canvas_width, canvas_height):
        _invalid("WebP image dimensions do not match its canvas")
    has_alph = b"ALPH" in expected
    if image_name == b"VP8L" and has_alph:
        _invalid("WebP ALPH is forbidden with VP8L")
    if alpha_flag != (lossless_alpha if image_name == b"VP8L" else has_alph):
        _invalid("WebP alpha flag does not match image data")
    if has_alph:
        payload = chunks[expected.index(b"ALPH")].payload
        if not payload:
            _invalid("empty WebP ALPH chunk")
        header_byte = payload[0]
        compression = header_byte & 0x03
        preprocessing = (header_byte >> 4) & 0x03
        if header_byte & 0xC0 or compression != 0:
            _invalid("unsupported WebP ALPH coding")
        if preprocessing > 1:
            _invalid("invalid WebP ALPH header")
        if len(payload) - 1 != canvas_width * canvas_height:
            _invalid("WebP ALPH pixel count does not match canvas")
    return canvas_width, canvas_height


def _validate_webp(data: memoryview) -> tuple[int, int]:
    chunks = _parse_webp_chunks(data)
    if any(chunk.name in (b"ANIM", b"ANMF") for chunk in chunks):
        _invalid("animated WebP is unsupported")
    if chunks and chunks[0].name == b"VP8X":
        return _validate_webp_extended(chunks)
    if len(chunks) != 1 or chunks[0].name not in (b"VP8 ", b"VP8L"):
        _invalid("invalid simple WebP chunk cardinality")
    width, height, _ = _validate_webp_image(chunks[0])
    return width, height


_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_PNG_LAYOUTS = {
    0: ({1, 2, 4, 8, 16}, 1),
    2: ({8, 16}, 3),
    3: ({1, 2, 4, 8}, 1),
    4: ({8, 16}, 2),
    6: ({8, 16}, 4),
}
_PNG_ALLOWED_ANCILLARY = {b"sRGB", b"gAMA", b"pHYs", b"tRNS"}
_PNG_APNG_CHUNKS = {b"acTL", b"fcTL", b"fdAT"}


def _png_u32(data: memoryview, position: int) -> int:
    return int.from_bytes(data[position : position + 4], "big")


def _validate_png_chunk_name(name: bytes) -> None:
    if len(name) != 4 or any(not (65 <= byte <= 90 or 97 <= byte <= 122) for byte in name):
        _invalid("illegal PNG chunk name")
    if name[2] & 0x20:
        _invalid("PNG reserved chunk-name bit must be zero")


def _validate_png_trns(
    payload: memoryview,
    color_type: int,
    bit_depth: int,
    palette_entries: int | None,
) -> None:
    if color_type in (4, 6):
        _invalid("PNG tRNS is forbidden for alpha color types")
    sample_limit = 1 << bit_depth
    if color_type == 0:
        if len(payload) != 2 or int.from_bytes(payload, "big") >= sample_limit:
            _invalid("invalid grayscale PNG tRNS")
    elif color_type == 2:
        if len(payload) != 6:
            _invalid("invalid truecolor PNG tRNS")
        if any(
            int.from_bytes(payload[position : position + 2], "big") >= sample_limit
            for position in (0, 2, 4)
        ):
            _invalid("PNG tRNS sample exceeds bit depth")
    elif color_type == 3:
        if palette_entries is None:
            _invalid("indexed PNG tRNS precedes PLTE")
        if not (1 <= len(payload) <= palette_entries):
            _invalid("invalid indexed PNG tRNS cardinality")
    else:
        _invalid("invalid PNG tRNS color type")


@dataclass
class _PngInflater:
    inflater: zlib.Decompress
    expected_size: int
    stride: int
    height: int
    output_size: int = 0

    def feed(self, payload: memoryview) -> None:
        if self.inflater.eof and len(payload):
            _invalid("compressed PNG bytes follow zlib EOF")
        position = 0
        try:
            while position < len(payload):
                pending = payload[position : position + 64 * 1024]
                position += len(pending)
                while len(pending):
                    allowance = min(64 * 1024, self.expected_size + 1 - self.output_size)
                    if allowance <= 0:
                        _invalid("inflated PNG data exceeds expected size")
                    output = self.inflater.decompress(pending, allowance)
                    for offset, value in enumerate(output, self.output_size):
                        if offset % self.stride == 0 and value > 4:
                            _invalid("unsupported PNG row filter")
                    self.output_size += len(output)
                    pending = self.inflater.unconsumed_tail
                    if self.inflater.unused_data:
                        _invalid("PNG contains trailing or concatenated zlib data")
                if self.inflater.eof and position < len(payload):
                    _invalid("compressed PNG bytes follow zlib EOF")
        except zlib.error:
            _invalid("invalid PNG zlib stream")

    def finish(self) -> None:
        if self.output_size != self.expected_size:
            _invalid("unexpected inflated PNG size")
        if (
            not self.inflater.eof
            or self.inflater.unused_data
            or self.inflater.unconsumed_tail
        ):
            _invalid("incomplete or overlong PNG zlib stream")
        if self.expected_size != self.height * self.stride:
            _invalid("invalid PNG row layout")


def _validate_png(data: memoryview) -> tuple[int, int]:
    if len(data) < len(_PNG_SIGNATURE) or bytes(data[:8]) != _PNG_SIGNATURE:
        _invalid("invalid PNG signature")

    position = 8
    chunk_index = 0
    width = height = bit_depth = color_type = channels = 0
    palette_entries: int | None = None
    seen_plte = False
    seen_idat = False
    idat_ended = False
    seen_iend = False
    seen_ancillary: set[bytes] = set()
    png_inflater: _PngInflater | None = None

    while position < len(data):
        if len(data) - position < 12:
            _invalid("truncated PNG chunk")
        length = _png_u32(data, position)
        if length > 0x7FFFFFFF:
            _invalid("invalid PNG chunk length")
        name = bytes(data[position + 4 : position + 8])
        _validate_png_chunk_name(name)
        payload_start = position + 8
        payload_end = payload_start + length
        chunk_end = payload_end + 4
        if payload_end < payload_start or chunk_end > len(data):
            _invalid("truncated PNG chunk")
        payload = data[payload_start:payload_end]
        stored_crc = _png_u32(data, payload_end)
        if zlib.crc32(payload, zlib.crc32(name)) != stored_crc:
            _invalid("invalid PNG chunk CRC")
        position = chunk_end

        if chunk_index == 0 and name != b"IHDR":
            _invalid("PNG IHDR must be first")
        if seen_iend:
            _invalid("bytes after PNG IEND")

        if name == b"IHDR":
            if chunk_index != 0 or length != 13:
                _invalid("invalid or duplicate PNG IHDR")
            width, height, bit_depth, color_type, compression, filter_method, interlace = struct.unpack(
                ">IIBBBBB", payload
            )
            _check_dimensions(width, height)
            layout = _PNG_LAYOUTS.get(color_type)
            if layout is None or bit_depth not in layout[0]:
                _invalid("unsupported PNG color type and bit depth")
            channels = layout[1]
            if compression != 0 or filter_method != 0 or interlace != 0:
                _invalid("unsupported PNG coding method")
            row_bytes = (width * channels * bit_depth + 7) // 8
            stride = 1 + row_bytes
            png_inflater = _PngInflater(
                inflater=zlib.decompressobj(),
                expected_size=height * stride,
                stride=stride,
                height=height,
            )

        elif name == b"PLTE":
            if seen_plte or seen_idat or color_type in (0, 4):
                _invalid("invalid PNG PLTE placement")
            if length < 3 or length > 768 or length % 3:
                _invalid("invalid PNG PLTE cardinality")
            palette_entries = length // 3
            if color_type == 3 and palette_entries > (1 << bit_depth):
                _invalid("PNG palette exceeds bit depth")
            seen_plte = True

        elif name == b"IDAT":
            if idat_ended:
                _invalid("PNG IDAT chunks must be consecutive")
            if color_type == 3 and not seen_plte:
                _invalid("indexed PNG requires PLTE before IDAT")
            assert png_inflater is not None
            seen_idat = True
            png_inflater.feed(payload)

        elif name == b"IEND":
            if length != 0 or not seen_idat or position != len(data):
                _invalid("invalid terminal PNG IEND")
            seen_iend = True

        else:
            if seen_idat:
                idat_ended = True
            if name in _PNG_APNG_CHUNKS:
                _invalid("APNG is unsupported")
            if not (name[0] & 0x20):
                _invalid("unknown PNG critical chunk")
            if name not in _PNG_ALLOWED_ANCILLARY:
                _invalid("unsupported PNG ancillary chunk")
            if name in seen_ancillary:
                _invalid("duplicate PNG ancillary chunk")
            seen_ancillary.add(name)

            if name in (b"sRGB", b"gAMA") and (seen_plte or seen_idat):
                _invalid("late PNG color metadata")
            if name == b"sRGB":
                if length != 1 or payload[0] > 3:
                    _invalid("invalid PNG sRGB")
            elif name == b"gAMA":
                if length != 4 or _png_u32(payload, 0) == 0:
                    _invalid("invalid PNG gAMA")
            elif name == b"pHYs":
                if seen_idat or length != 9 or payload[8] not in (0, 1):
                    _invalid("invalid PNG pHYs")
            else:
                if seen_idat:
                    _invalid("late PNG tRNS")
                _validate_png_trns(payload, color_type, bit_depth, palette_entries)

        chunk_index += 1

    if not seen_iend:
        _invalid("PNG missing IEND")
    assert png_inflater is not None
    png_inflater.finish()
    return width, height


_SOF_MARKERS = {
    0xC0,
    0xC1,
    0xC2,
    0xC3,
    0xC5,
    0xC6,
    0xC7,
    0xC9,
    0xCA,
    0xCB,
    0xCD,
    0xCE,
    0xCF,
}
_SUPPORTED_SOF_MARKERS = {0xC0, 0xC2}
_APP_MARKERS = set(range(0xE0, 0xF0))


@dataclass
class _JpegFrame:
    progressive: bool
    width: int
    height: int
    component_ids: tuple[int, ...]


def _read_marker(cursor: _Cursor) -> int:
    if cursor.read_u8() != 0xFF:
        _invalid("expected JPEG marker")
    marker = cursor.read_u8()
    while marker == 0xFF:
        marker = cursor.read_u8()
    if marker == 0x00:
        _invalid("stuffed byte outside JPEG entropy data")
    return marker


def _segment_end(cursor: _Cursor) -> int:
    length = cursor.read_u16()
    if length < 2:
        _invalid("invalid JPEG segment length")
    payload_size = length - 2
    if payload_size > cursor.remaining():
        _invalid("truncated JPEG segment")
    return cursor.position + payload_size


def _finish_segment(cursor: _Cursor, end: int) -> None:
    if cursor.position != end:
        _invalid("invalid JPEG segment cardinality")


def _skip_segment(cursor: _Cursor) -> None:
    end = _segment_end(cursor)
    cursor.position = end


def _parse_dqt(cursor: _Cursor) -> None:
    end = _segment_end(cursor)
    if cursor.position == end:
        _invalid("empty JPEG quantization table segment")
    while cursor.position < end:
        descriptor = cursor.read_u8()
        precision = descriptor >> 4
        table_id = descriptor & 0x0F
        if precision not in (0, 1) or table_id > 3:
            _invalid("invalid JPEG quantization table")
        value_count = 64
        value_bytes = value_count * (precision + 1)
        if value_bytes > end - cursor.position:
            _invalid("truncated JPEG quantization table")
        for _ in range(value_count):
            value = cursor.read_u8() if precision == 0 else cursor.read_u16()
            if value == 0:
                _invalid("zero JPEG quantization value")
    _finish_segment(cursor, end)


def _parse_dht(cursor: _Cursor) -> tuple[int, ...]:
    end = _segment_end(cursor)
    if cursor.position == end:
        _invalid("empty JPEG Huffman table segment")
    progressive_only_ac_symbols = []
    while cursor.position < end:
        descriptor = cursor.read_u8()
        table_class = descriptor >> 4
        table_id = descriptor & 0x0F
        if table_class > 1 or table_id > 3:
            _invalid("invalid JPEG Huffman table selector")
        if end - cursor.position < 16:
            _invalid("truncated JPEG Huffman counts")
        symbols_count = 0
        available_codes = 1
        for _ in range(16):
            count = cursor.read_u8()
            symbols_count += count
            available_codes = available_codes * 2 - count
            if available_codes < 0:
                _invalid("oversubscribed JPEG Huffman table")
        if available_codes == 0:
            _invalid("JPEG Huffman table uses reserved all-ones code")
        if symbols_count == 0 or symbols_count > 256:
            _invalid("invalid JPEG Huffman table cardinality")
        if symbols_count > end - cursor.position:
            _invalid("truncated JPEG Huffman symbols")
        symbols_end = cursor.position + symbols_count
        while cursor.position < symbols_end:
            symbol = cursor.read_u8()
            if table_class == 0:
                if symbol > 11:
                    _invalid("invalid JPEG DC Huffman symbol")
            else:
                run = symbol >> 4
                size = symbol & 0x0F
                if size > 10:
                    _invalid("invalid JPEG AC Huffman symbol")
                if size == 0 and run not in (0, 15):
                    progressive_only_ac_symbols.append(symbol)
    _finish_segment(cursor, end)
    return tuple(progressive_only_ac_symbols)


def _validate_dht_frame_mode(
    progressive_only_ac_symbols: tuple[int, ...], frame: _JpegFrame
) -> None:
    if progressive_only_ac_symbols and not frame.progressive:
        _invalid("progressive JPEG AC Huffman symbol in baseline frame")


def _parse_dri(cursor: _Cursor) -> int:
    end = _segment_end(cursor)
    if end - cursor.position != 2:
        _invalid("invalid JPEG restart interval segment")
    interval = cursor.read_u16()
    _finish_segment(cursor, end)
    return interval


def _check_dimensions(width: int, height: int) -> None:
    if width <= 0 or height <= 0:
        _invalid("invalid wallpaper dimensions")
    if width > MAX_IMAGE_SIDE or height > MAX_IMAGE_SIDE:
        _invalid("wallpaper exceeds side limit")
    if width * height > MAX_IMAGE_PIXELS:
        _invalid("wallpaper exceeds pixel limit")


def _parse_sof(cursor: _Cursor, marker: int) -> _JpegFrame:
    if marker not in _SUPPORTED_SOF_MARKERS:
        _invalid("unsupported JPEG frame coding")
    end = _segment_end(cursor)
    if end - cursor.position < 6:
        _invalid("truncated JPEG frame header")
    precision = cursor.read_u8()
    height = cursor.read_u16()
    width = cursor.read_u16()
    component_count = cursor.read_u8()
    if precision != 8 or component_count not in (1, 3):
        _invalid("unsupported JPEG frame layout")
    if end - cursor.position != component_count * 3:
        _invalid("invalid JPEG frame component count")
    _check_dimensions(width, height)
    component_ids = []
    for _ in range(component_count):
        component_id = cursor.read_u8()
        sampling = cursor.read_u8()
        quant_table = cursor.read_u8()
        horizontal = sampling >> 4
        vertical = sampling & 0x0F
        if component_id in component_ids:
            _invalid("duplicate JPEG frame component")
        if not (1 <= horizontal <= 4 and 1 <= vertical <= 4):
            _invalid("invalid JPEG sampling factor")
        if quant_table > 3:
            _invalid("invalid JPEG quantization selector")
        component_ids.append(component_id)
    _finish_segment(cursor, end)
    return _JpegFrame(
        progressive=marker == 0xC2,
        width=width,
        height=height,
        component_ids=tuple(component_ids),
    )


def _parse_sos(cursor: _Cursor, frame: _JpegFrame) -> tuple[tuple[int, ...], int, int, int, int]:
    end = _segment_end(cursor)
    if end - cursor.position < 1:
        _invalid("truncated JPEG scan header")
    component_count = cursor.read_u8()
    if component_count < 1 or component_count > len(frame.component_ids):
        _invalid("invalid JPEG scan component count")
    if end - cursor.position != component_count * 2 + 3:
        _invalid("invalid JPEG scan header length")
    component_ids = []
    for _ in range(component_count):
        component_id = cursor.read_u8()
        table_selectors = cursor.read_u8()
        if component_id not in frame.component_ids:
            _invalid("unknown JPEG scan component")
        if component_id in component_ids:
            _invalid("duplicate JPEG scan component")
        if (table_selectors >> 4) > 3 or (table_selectors & 0x0F) > 3:
            _invalid("invalid JPEG scan table selector")
        component_ids.append(component_id)
    ss = cursor.read_u8()
    se = cursor.read_u8()
    approximation = cursor.read_u8()
    ah = approximation >> 4
    al = approximation & 0x0F
    _finish_segment(cursor, end)
    return tuple(component_ids), ss, se, ah, al


def _record_baseline_scan(
    scan: tuple[tuple[int, ...], int, int, int, int], seen: set[int]
) -> None:
    component_ids, ss, se, ah, al = scan
    if (ss, se, ah, al) != (0, 63, 0, 0):
        _invalid("invalid baseline JPEG scan parameters")
    if any(component_id in seen for component_id in component_ids):
        _invalid("duplicate baseline JPEG scan component")
    seen.update(component_ids)


def _record_progressive_scan(
    scan: tuple[tuple[int, ...], int, int, int, int],
    coefficients: dict[int, list[int | None]],
) -> None:
    component_ids, ss, se, ah, al = scan
    if ah > 13 or al > 13:
        _invalid("invalid progressive JPEG approximation")
    if ss == 0:
        if se != 0:
            _invalid("invalid progressive JPEG DC band")
    elif not (1 <= ss <= se <= 63) or len(component_ids) != 1:
        _invalid("invalid progressive JPEG AC band")

    for component_id in component_ids:
        states = coefficients[component_id]
        band = states[ss : se + 1]
        if ah == 0:
            if any(value is not None for value in band):
                _invalid("overlapping progressive JPEG first scan")
        else:
            if al != ah - 1 or any(value != ah for value in band):
                _invalid("invalid progressive JPEG refinement")
        for coefficient in range(ss, se + 1):
            states[coefficient] = al


def _scan_entropy(cursor: _Cursor, restart_interval: int) -> int:
    expected_restart = 0
    while cursor.position < cursor.size:
        value = cursor.read_u8()
        if value != 0xFF:
            continue
        marker = cursor.read_u8()
        saw_fill = False
        while marker == 0xFF:
            saw_fill = True
            marker = cursor.read_u8()
        if marker == 0x00:
            if saw_fill:
                _invalid("invalid JPEG stuffed-byte sequence")
            continue
        if 0xD0 <= marker <= 0xD7:
            if restart_interval == 0 or marker != 0xD0 + expected_restart:
                _invalid("invalid JPEG restart sequence")
            expected_restart = (expected_restart + 1) % 8
            continue
        return marker
    _invalid("JPEG scan missing terminal marker")


def _validate_jpeg(data: memoryview) -> tuple[int, int]:
    cursor = _Cursor(data)
    if cursor.read_u8() != 0xFF or cursor.read_u8() != 0xD8:
        _invalid("JPEG SOI must be first")

    frame: _JpegFrame | None = None
    restart_interval = 0
    scan_count = 0
    baseline_seen: set[int] = set()
    progressive_coefficients: dict[int, list[int | None]] | None = None
    pending_progressive_ac_symbols: list[int] = []
    pending_marker: int | None = None
    after_scan = False

    while True:
        marker = pending_marker if pending_marker is not None else _read_marker(cursor)
        pending_marker = None

        if marker == 0xD9:
            if cursor.position != cursor.size:
                _invalid("trailing bytes after JPEG EOI")
            if frame is None or scan_count == 0:
                _invalid("incomplete JPEG image")
            if frame.progressive:
                assert progressive_coefficients is not None
                if any(
                    value is None
                    for states in progressive_coefficients.values()
                    for value in states
                ):
                    _invalid("incomplete progressive JPEG coefficients")
            elif set(frame.component_ids) != baseline_seen:
                _invalid("incomplete baseline JPEG components")
            return frame.width, frame.height

        if marker in _SOF_MARKERS:
            if after_scan or frame is not None:
                _invalid("duplicate JPEG frame header")
            frame = _parse_sof(cursor, marker)
            _validate_dht_frame_mode(tuple(pending_progressive_ac_symbols), frame)
            pending_progressive_ac_symbols.clear()
            if frame.progressive:
                progressive_coefficients = {
                    component_id: [None] * 64 for component_id in frame.component_ids
                }
            continue

        if marker == 0xDA:
            if frame is None:
                _invalid("JPEG scan precedes frame header")
            scan = _parse_sos(cursor, frame)
            if frame.progressive:
                assert progressive_coefficients is not None
                _record_progressive_scan(scan, progressive_coefficients)
            else:
                _record_baseline_scan(scan, baseline_seen)
            scan_count += 1
            pending_marker = _scan_entropy(cursor, restart_interval)
            after_scan = True
            continue

        if marker == 0xDB:
            _parse_dqt(cursor)
        elif marker == 0xC4:
            progressive_symbols = _parse_dht(cursor)
            if frame is None:
                pending_progressive_ac_symbols.extend(progressive_symbols)
            else:
                _validate_dht_frame_mode(progressive_symbols, frame)
        elif marker == 0xDD:
            restart_interval = _parse_dri(cursor)
        elif marker in _APP_MARKERS or marker == 0xFE:
            _skip_segment(cursor)
        elif marker == 0xCC:
            _invalid("arithmetic JPEG coding is unsupported")
        else:
            _invalid("illegal or unsupported JPEG marker")
