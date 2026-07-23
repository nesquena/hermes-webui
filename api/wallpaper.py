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


def _probe_png_dimensions(data: memoryview) -> tuple[int, int]:
    if len(data) < 33 or bytes(data[:8]) != _PNG_SIGNATURE:
        _invalid("truncated PNG header")
    if int.from_bytes(data[8:12], "big") != 13 or bytes(data[12:16]) != b"IHDR":
        _invalid("PNG IHDR must be first")
    payload = data[16:29]
    stored_crc = int.from_bytes(data[29:33], "big")
    if zlib.crc32(payload, zlib.crc32(b"IHDR")) != stored_crc:
        _invalid("invalid PNG IHDR checksum")
    width = int.from_bytes(payload[:4], "big")
    height = int.from_bytes(payload[4:8], "big")
    bit_depth, color_type, compression, filter_method, interlace = payload[8:13]
    layouts = {
        0: {1, 2, 4, 8, 16},
        2: {8, 16},
        3: {1, 2, 4, 8},
        4: {8, 16},
        6: {8, 16},
    }
    if bit_depth not in layouts.get(color_type, set()):
        _invalid("unsupported PNG color layout")
    if compression != 0 or filter_method != 0 or interlace not in (0, 1):
        _invalid("unsupported PNG coding method")
    _check_dimensions(width, height)
    return width, height


def _probe_jpeg_dimensions(data: memoryview) -> tuple[int, int]:
    position = 2
    frame_markers = {
        *range(0xC0, 0xC4),
        *range(0xC5, 0xC8),
        *range(0xC9, 0xCC),
        *range(0xCD, 0xD0),
    }
    while position < len(data):
        if data[position] != 0xFF:
            _invalid("invalid JPEG marker")
        while position < len(data) and data[position] == 0xFF:
            position += 1
        if position >= len(data):
            _invalid("truncated JPEG marker")
        marker = data[position]
        position += 1
        if marker == 0x01:
            continue
        if marker in (0x00, 0xD8) or 0xD0 <= marker <= 0xD7:
            _invalid("invalid JPEG header marker")
        if marker in (0xD9, 0xDA):
            _invalid("JPEG is missing a frame header")
        if position + 2 > len(data):
            _invalid("truncated JPEG segment")
        segment_length = int.from_bytes(data[position : position + 2], "big")
        if segment_length < 2 or position + segment_length > len(data):
            _invalid("truncated JPEG segment")
        if marker in frame_markers:
            if segment_length < 8:
                _invalid("truncated JPEG frame header")
            component_count = data[position + 7]
            if component_count == 0 or segment_length != 8 + component_count * 3:
                _invalid("invalid JPEG frame header")
            height = int.from_bytes(data[position + 3 : position + 5], "big")
            width = int.from_bytes(data[position + 5 : position + 7], "big")
            _check_dimensions(width, height)
            return width, height
        position += segment_length
    _invalid("JPEG is missing a frame header")


def _probe_webp_image_dimensions(
    name: bytes, payload: memoryview
) -> tuple[int, int]:
    if name == b"VP8L":
        if len(payload) < 5 or payload[0] != 0x2F:
            _invalid("invalid WebP lossless header")
        header = int.from_bytes(payload[1:5], "little")
        if header >> 29:
            _invalid("unsupported WebP lossless version")
        width = (header & 0x3FFF) + 1
        height = ((header >> 14) & 0x3FFF) + 1
    elif name == b"VP8 ":
        if len(payload) < 10 or bytes(payload[3:6]) != b"\x9d\x01\x2a":
            _invalid("invalid WebP VP8 frame header")
        frame_tag = int.from_bytes(payload[:3], "little")
        horizontal = int.from_bytes(payload[6:8], "little")
        vertical = int.from_bytes(payload[8:10], "little")
        if frame_tag & 1 or ((frame_tag >> 1) & 0x07) > 3 or not ((frame_tag >> 4) & 1):
            _invalid("unsupported WebP VP8 frame header")
        if horizontal >> 14 or vertical >> 14:
            _invalid("unsupported WebP VP8 scaling")
        width = horizontal & 0x3FFF
        height = vertical & 0x3FFF
    else:
        _invalid("WebP is missing its image header")
    _check_dimensions(width, height)
    return width, height


def _webp_chunks(data: memoryview):
    position = 12
    while position < len(data):
        if len(data) - position < 8:
            _invalid("truncated WebP chunk header")
        name = bytes(data[position : position + 4])
        size = int.from_bytes(data[position + 4 : position + 8], "little")
        payload_start = position + 8
        payload_end = payload_start + size
        padded_end = payload_end + (size & 1)
        if padded_end > len(data):
            _invalid("truncated WebP chunk")
        if size & 1 and data[payload_end] != 0:
            _invalid("invalid WebP chunk padding")
        yield name, data[payload_start:payload_end]
        position = padded_end


def _probe_webp_dimensions(data: memoryview) -> tuple[int, int]:
    if len(data) < 20 or bytes(data[:4]) != b"RIFF" or bytes(data[8:12]) != b"WEBP":
        _invalid("invalid WebP RIFF header")
    if int.from_bytes(data[4:8], "little") != len(data) - 8:
        _invalid("invalid WebP RIFF size")

    chunks = _webp_chunks(data)
    try:
        first_name, first_payload = next(chunks)
    except StopIteration:
        _invalid("WebP is missing its image header")
    if first_name != b"VP8X":
        dimensions = _probe_webp_image_dimensions(first_name, first_payload)
        for name, _ in chunks:
            if name in (b"VP8 ", b"VP8L", b"VP8X", b"ANIM", b"ANMF"):
                _invalid("invalid WebP image cardinality")
        return dimensions
    if len(first_payload) != 10:
        _invalid("invalid WebP VP8X header")
    flags = first_payload[0]
    if flags & 0xC3 or flags & 0x02 or any(first_payload[1:4]):
        _invalid("unsupported WebP VP8X header")
    canvas = (
        int.from_bytes(first_payload[4:7], "little") + 1,
        int.from_bytes(first_payload[7:10], "little") + 1,
    )
    _check_dimensions(*canvas)
    dimensions = None
    for name, payload in chunks:
        if name in (b"ANIM", b"ANMF"):
            _invalid("animated WebP is unsupported")
        if name not in (b"VP8 ", b"VP8L"):
            continue
        if dimensions is not None:
            _invalid("invalid WebP image cardinality")
        dimensions = _probe_webp_image_dimensions(name, payload)
        if dimensions != canvas:
            _invalid("WebP image dimensions do not match its canvas")
    if dimensions is None:
        _invalid("WebP is missing its image header")
    return dimensions


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
        width, height = _probe_jpeg_dimensions(data)
        trusted_mime = "image/jpeg"
        extension = "jpg"
    elif len(data) >= 8 and bytes(data[:8]) == _PNG_SIGNATURE:
        width, height = _probe_png_dimensions(data)
        trusted_mime = "image/png"
        extension = "png"
    elif len(data) >= 4 and bytes(data[:4]) == b"RIFF":
        width, height = _probe_webp_dimensions(data)
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


_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _check_dimensions(width: int, height: int) -> None:
    if width <= 0 or height <= 0:
        _invalid("invalid wallpaper dimensions")
    if width > MAX_IMAGE_SIDE or height > MAX_IMAGE_SIDE:
        _invalid("wallpaper exceeds side limit")
    if width * height > MAX_IMAGE_PIXELS:
        _invalid("wallpaper exceeds pixel limit")
