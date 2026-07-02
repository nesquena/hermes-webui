"""
Hermes Web UI — Branding: user-uploaded logo/favicon management.

Uploaded logos are stored under STATE_DIR / 'branding' and served
via the /branding/<filename> route (see api/routes.py).
"""
import binascii
import re as _re
import struct
from pathlib import Path

from api.config import STATE_DIR
from api.helpers import j, bad

BRANDING_DIR = STATE_DIR / "branding"

# Allowed image formats and their MIME types
_ALLOWED_CONTENT_TYPES = {
    "image/png",
    "image/x-icon",
    "image/vnd.microsoft.icon",
}

_ALLOWED_EXTENSIONS = {".png", ".ico"}
_LEGACY_LOGO_EXTENSIONS = {".svg"}
_LOGO_FILE_EXTENSIONS_TO_DELETE = _ALLOWED_EXTENSIONS | _LEGACY_LOGO_EXTENSIONS

# 256x256 px at 4 bytes/pixel RGBA is roughly 262 KB uncompressed.
# 200 KB after PNG compression is generous headroom.
_MAX_LOGO_BYTES = 200 * 1024
_MAX_LOGO_MULTIPART_OVERHEAD_BYTES = 8 * 1024
_MAX_LOGO_UPLOAD_REQUEST_BYTES = _MAX_LOGO_BYTES + _MAX_LOGO_MULTIPART_OVERHEAD_BYTES

# Max pixel dimension for raster logo images.
_MAX_LOGO_DIMENSION = 256

# Valid mode values
_VALID_MODES = {"light", "dark"}
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

def _logo_path_from_settings_value(value: str) -> Path | None:
    """Resolve a stored logo filename if it is one of our canonical assets."""
    raw = str(value or "").strip()
    name = Path(raw).name
    if not name:
        return None
    if raw != name:
        return None
    if not _re.fullmatch(r"logo-(light|dark)\.(png|ico)", name):
        return None
    path = (BRANDING_DIR / name).resolve()
    try:
        path.relative_to(BRANDING_DIR.resolve())
    except ValueError:
        return None
    return path


def logo_version_for_settings_value(value: str) -> str:
    """Return the current cache-busting version for a stored logo filename."""
    path = _logo_path_from_settings_value(value)
    if not path or not path.exists() or not path.is_file():
        return ""
    return _branding_version(path)


def _branding_version(path: Path) -> str:
    """Return a stable cache-busting token for a mutable branding file."""
    try:
        return str(path.stat().st_mtime_ns)
    except OSError:
        return ""


def _png_dimensions(data: bytes) -> tuple[int, int]:
    """Validate PNG chunk structure and extract IHDR width and height.

    Returns (width, height) or raises ValueError if not a valid PNG.
    """
    if len(data) < 8 or data[:8] != _PNG_SIGNATURE:
        raise ValueError("Not a valid PNG file")

    pos = 8
    width = 0
    height = 0
    saw_ihdr = False
    saw_iend = False

    while pos < len(data):
        if len(data) - pos < 12:
            raise ValueError("Invalid PNG: truncated chunk")
        chunk_length = struct.unpack(">I", data[pos : pos + 4])[0]
        chunk_type = data[pos + 4 : pos + 8]
        chunk_start = pos + 8
        chunk_end = chunk_start + chunk_length
        crc_end = chunk_end + 4
        if chunk_end > len(data) or crc_end > len(data):
            raise ValueError("Invalid PNG: chunk extends beyond file")
        if not all((65 <= b <= 90) or (97 <= b <= 122) for b in chunk_type):
            raise ValueError("Invalid PNG: invalid chunk type")
        expected_crc = binascii.crc32(chunk_type + data[chunk_start:chunk_end]) & 0xFFFFFFFF
        actual_crc = struct.unpack(">I", data[chunk_end:crc_end])[0]
        if actual_crc != expected_crc:
            raise ValueError("Invalid PNG: bad chunk checksum")

        if not saw_ihdr:
            if chunk_type != b"IHDR" or chunk_length != 13:
                raise ValueError("Invalid PNG: missing IHDR")
            width = struct.unpack(">I", data[chunk_start : chunk_start + 4])[0]
            height = struct.unpack(">I", data[chunk_start + 4 : chunk_start + 8])[0]
            if width <= 0 or height <= 0:
                raise ValueError("Invalid PNG: invalid dimensions")
            saw_ihdr = True
        elif chunk_type == b"IHDR":
            raise ValueError("Invalid PNG: duplicate IHDR")

        if chunk_type == b"IEND":
            if chunk_length != 0:
                raise ValueError("Invalid PNG: invalid IEND")
            saw_iend = True
            pos = crc_end
            break

        pos = crc_end

    if not saw_ihdr or not saw_iend:
        raise ValueError("Invalid PNG: missing IEND")
    if pos != len(data):
        raise ValueError("Invalid PNG: trailing data after IEND")
    return width, height


def _dib_dimensions(data: bytes) -> tuple[int, int]:
    """Parse a bounded ICO DIB/BMP payload enough to validate image dimensions."""
    if len(data) < 16:
        raise ValueError("Invalid ICO: embedded bitmap is truncated")
    header_size = struct.unpack("<I", data[:4])[0]
    if header_size == 12:
        if len(data) < 12:
            raise ValueError("Invalid ICO: embedded bitmap header is truncated")
        w = struct.unpack("<H", data[4:6])[0]
        stored_h = struct.unpack("<H", data[6:8])[0]
        h = stored_h // 2 if stored_h > 1 else stored_h
        return w, h
    if header_size < 40 or header_size > len(data):
        raise ValueError("Invalid ICO: embedded bitmap header is invalid")
    w = abs(struct.unpack("<i", data[4:8])[0])
    stored_h = abs(struct.unpack("<i", data[8:12])[0])
    planes = struct.unpack("<H", data[12:14])[0]
    bit_count = struct.unpack("<H", data[14:16])[0]
    if w <= 0 or stored_h <= 0 or planes != 1 or bit_count not in {1, 4, 8, 16, 24, 32}:
        raise ValueError("Invalid ICO: embedded bitmap header is invalid")
    return w, stored_h // 2 if stored_h > 1 else stored_h


def _ico_dimensions(data: bytes) -> tuple[int, int]:
    """Parse ICO directory entries and validate each bounded payload."""
    if len(data) < 22 or data[:4] != b"\x00\x00\x01\x00":
        raise ValueError("Not a valid ICO file")
    count = struct.unpack("<H", data[4:6])[0]
    if count < 1:
        raise ValueError("Invalid ICO: no image entries")
    if len(data) < 6 + (count * 16):
        raise ValueError("Invalid ICO: truncated directory")

    max_w = 0
    max_h = 0
    ranges: list[tuple[int, int]] = []
    for idx in range(count):
        entry = data[6 + (idx * 16) : 6 + ((idx + 1) * 16)]
        w = entry[0] or 256
        h = entry[1] or 256
        size = struct.unpack("<I", entry[8:12])[0]
        offset = struct.unpack("<I", entry[12:16])[0]
        if size <= 0 or offset <= 0 or offset + size > len(data):
            raise ValueError("Invalid ICO: image entry points outside file")
        ranges.append((offset, offset + size))
        image = data[offset : offset + size]
        if image[:8] == _PNG_SIGNATURE:
            try:
                w, h = _png_dimensions(image)
            except ValueError as err:
                raise ValueError("Invalid ICO: invalid embedded PNG") from err
        else:
            try:
                dib_w, dib_h = _dib_dimensions(image)
            except ValueError as err:
                raise ValueError("Invalid ICO: invalid embedded bitmap") from err
            if dib_w != w or dib_h != h:
                raise ValueError("Invalid ICO: bitmap dimensions do not match directory")
        max_w = max(max_w, w)
        max_h = max(max_h, h)

    covered = bytearray(len(data))
    covered[: 6 + (count * 16)] = b"\x01" * (6 + (count * 16))
    for start, end in ranges:
        if any(covered[start:end]):
            raise ValueError("Invalid ICO: overlapping image entries")
        covered[start:end] = b"\x01" * (end - start)
    if not all(covered):
        raise ValueError("Invalid ICO: trailing or unreferenced data")
    return max_w, max_h


def _logo_requirements() -> str:
    return (
        f"Logo must be PNG or ICO, max {_MAX_LOGO_DIMENSION}x"
        f"{_MAX_LOGO_DIMENSION} px and {_MAX_LOGO_BYTES // 1024} KB."
    )


def _format_kb(size: int) -> str:
    return f"{(size + 1023) // 1024} KB"


def _raise_logo_requirement_error(issues: list[str]) -> None:
    suffix = f" Your file: {'; '.join(issues)}." if issues else ""
    raise ValueError(_logo_requirements() + suffix)


def _validate_upload(body: bytes, filename: str = "") -> tuple[bytes, str]:
    """Validate uploaded logo data by detecting format from magic bytes.

    Returns (data, extension) on success.  Raises ValueError on rejection.
    """
    if not body:
        raise ValueError("Empty file")

    size_issue = None
    if len(body) > _MAX_LOGO_BYTES:
        size_issue = f"{_format_kb(len(body))}"

    # Auto-detect format from magic bytes
    if body[:8] == b"\x89PNG\r\n\x1a\n":
        # PNG
        try:
            w, h = _png_dimensions(body)
        except ValueError as err:
            raise ValueError("Invalid PNG: could not parse dimensions") from err
        issues = []
        if size_issue:
            issues.append(f"{size_issue}")
        if w > _MAX_LOGO_DIMENSION or h > _MAX_LOGO_DIMENSION:
            issues.append(f"{w}x{h}px")
        if issues:
            _raise_logo_requirement_error(issues)
        return body, ".png"

    if body[:4] == b"\x00\x00\x01\x00":
        # ICO file
        try:
            w, h = _ico_dimensions(body)
        except ValueError as err:
            raise ValueError("Invalid ICO: could not parse dimensions") from err
        issues = []
        if size_issue:
            issues.append(f"{size_issue}")
        if w > _MAX_LOGO_DIMENSION or h > _MAX_LOGO_DIMENSION:
            issues.append(f"{w}x{h}px")
        if issues:
            _raise_logo_requirement_error(issues)
        return body, ".ico"

    # Fallback: detect by filename extension
    ext = Path(filename).suffix.lower() if filename else ""
    if ext in _ALLOWED_EXTENSIONS:
        if ext == ".png":
            try:
                w, h = _png_dimensions(body)
            except ValueError as err:
                raise ValueError("Invalid PNG: could not parse dimensions") from err
            issues = []
            if size_issue:
                issues.append(f"{size_issue}")
            if w > _MAX_LOGO_DIMENSION or h > _MAX_LOGO_DIMENSION:
                issues.append(f"{w}x{h}px")
            if issues:
                _raise_logo_requirement_error(issues)
            return body, ".png"
        if ext == ".ico":
            try:
                w, h = _ico_dimensions(body)
            except ValueError as err:
                raise ValueError("Invalid ICO: could not parse dimensions") from err
            issues = []
            if size_issue:
                issues.append(f"{size_issue}")
            if w > _MAX_LOGO_DIMENSION or h > _MAX_LOGO_DIMENSION:
                issues.append(f"{w}x{h}px")
            if issues:
                _raise_logo_requirement_error(issues)
            return body, ".ico"

    raise ValueError(_logo_requirements())


def _delete_logo_files_for_mode(mode: str) -> list[str]:
    """Delete every canonical logo file for a mode, regardless of extension."""
    deleted: list[str] = []
    for ext in _LOGO_FILE_EXTENSIONS_TO_DELETE:
        path = BRANDING_DIR / f"logo-{mode}{ext}"
        try:
            path.relative_to(BRANDING_DIR)
        except ValueError:
            continue
        if path.exists() and path.is_file():
            path.unlink()
            deleted.append(path.name)
    return deleted


def handle_logo_upload(handler) -> bool:
    """POST /api/settings/upload-logo

    Expects multipart/form-data with:
      - file: the image file (PNG or ICO)
      - mode: "light" or "dark"

    Saves to BRANDING_DIR/logo-{mode}.{ext} and returns the
    relative path so the client can store it in settings.
    """
    from api.upload import parse_multipart

    content_type = handler.headers.get("Content-Type", "")
    try:
        content_length = int(handler.headers.get("Content-Length", "0") or 0)
    except (TypeError, ValueError):
        return bad(handler, "Invalid Content-Length")

    if not content_type.startswith("multipart/form-data"):
        return bad(handler, "Expected multipart/form-data")

    if content_length > _MAX_LOGO_UPLOAD_REQUEST_BYTES:
        return j(handler, {"error": _logo_requirements()}, status=413)

    try:
        fields, files = parse_multipart(handler.rfile, content_type, content_length)
    except ValueError as e:
        return bad(handler, str(e))

    mode = (fields.get("mode") or "").strip()
    if mode not in _VALID_MODES:
        return bad(handler, f"Invalid mode '{mode}'. Must be 'light' or 'dark'")

    file_name, file_data = files.get("file", (None, None))
    if not file_data:
        return bad(handler, "No file uploaded")

    try:
        validated_data, ext = _validate_upload(file_data, file_name or "")
    except ValueError as e:
        return bad(handler, str(e))

    # Keep the original validated extension on the canonical path.
    BRANDING_DIR.mkdir(parents=True, exist_ok=True)
    deleted = _delete_logo_files_for_mode(mode)
    dest = BRANDING_DIR / f"logo-{mode}{ext}"
    dest.write_bytes(validated_data)

    filename = dest.name
    version = _branding_version(dest)

    return j(handler, {
        "ok": True,
        "path": filename,
        "version": version,
        "size": len(validated_data),
        "deleted": deleted,
    })


def handle_logo_delete(handler) -> bool:
    """POST /api/settings/delete-logo

    Expects JSON body: {"mode": "light" | "dark"}

    Deletes the corresponding logo file and returns ok.
    """
    from api.helpers import read_body

    try:
        body = read_body(handler)
    except ValueError as exc:
        return bad(handler, str(exc))

    mode = (body.get("mode") or "").strip()
    if mode not in _VALID_MODES:
        return bad(handler, f"Invalid mode '{mode}'. Must be 'light' or 'dark'")

    deleted = _delete_logo_files_for_mode(mode)

    return j(handler, {"ok": True, "deleted": bool(deleted)})
