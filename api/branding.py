"""
Hermes Web UI — Branding: user-uploaded logo/favicon management.

Uploaded logos are stored under STATE_DIR / 'branding' and served
via the /branding/<filename> route (see api/routes.py).
"""
import re as _re
import struct
from pathlib import Path

from api.config import STATE_DIR
from api.helpers import j, bad

BRANDING_DIR = STATE_DIR / "branding"

# Allowed image formats and their MIME types
_ALLOWED_CONTENT_TYPES = {
    "image/png",
    "image/svg+xml",
    "image/x-icon",
    "image/vnd.microsoft.icon",
}

_ALLOWED_EXTENSIONS = {".png", ".svg", ".ico"}

# 256x256 px at 4 bytes/pixel RGBA is roughly 262 KB uncompressed.
# 200 KB after PNG compression is generous headroom.
_MAX_LOGO_BYTES = 200 * 1024

# Max pixel dimension for raster logo images.
_MAX_LOGO_DIMENSION = 256

# Valid mode values
_VALID_MODES = {"light", "dark"}

# Sanitize file stem: keep only [a-zA-Z0-9._-], cap at 100 chars
_SAFE_STEM_RE = _re.compile(r"[^\w.\-]")


def _resolve_logo_path(mode: str) -> Path:
    """Return the canonical save path for a logo mode (light / dark)."""
    return BRANDING_DIR / f"logo-{mode}.png"


def _logo_path_from_settings_value(value: str) -> Path | None:
    """Resolve a stored logo filename if it is one of our canonical assets."""
    raw = str(value or "").strip()
    name = Path(raw).name
    if not name:
        return None
    if raw != name:
        return None
    if not _re.fullmatch(r"logo-(light|dark)\.(png|svg|ico)", name):
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
    """Parse PNG IHDR chunk to extract width and height.

    Returns (width, height) or raises ValueError if not a valid PNG.
    """
    # PNG signature: 8 bytes
    if len(data) < 33 or data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("Not a valid PNG file")
    # IHDR chunk starts at byte 8: 4 bytes length, 4 bytes 'IHDR',
    # then 4 bytes width (big-endian), 4 bytes height (big-endian).
    w = struct.unpack(">I", data[16:20])[0]
    h = struct.unpack(">I", data[20:24])[0]
    return w, h


def _logo_requirements() -> str:
    return (
        f"Logo must be PNG, SVG, or ICO, max {_MAX_LOGO_DIMENSION}x"
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
        if size_issue:
            _raise_logo_requirement_error([size_issue])
        return body, ".ico"

    # Try SVG detection (text-based)
    text = body.decode("utf-8", errors="replace")[:4096].strip()
    if text and ("<svg" in text[:200] or "<SVG" in text[:200]):
        if size_issue:
            _raise_logo_requirement_error([size_issue])
        return body, ".svg"

    # Fallback: detect by filename extension
    ext = Path(filename).suffix.lower() if filename else ""
    if ext in _ALLOWED_EXTENSIONS:
        if ext == ".svg":
            text = body.decode("utf-8", errors="replace")[:4096]
            if "<svg" not in text and "<SVG" not in text:
                raise ValueError("Invalid SVG: missing <svg> element")
            if size_issue:
                _raise_logo_requirement_error([size_issue])
            return body, ".svg"
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
            if size_issue:
                _raise_logo_requirement_error([size_issue])
            return body, ".ico"

    raise ValueError(_logo_requirements())


def _delete_logo_files_for_mode(mode: str) -> list[str]:
    """Delete every canonical logo file for a mode, regardless of extension."""
    deleted: list[str] = []
    for ext in _ALLOWED_EXTENSIONS:
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
      - file: the image file (PNG, SVG, or ICO)
      - mode: "light" or "dark"

    Saves to BRANDING_DIR/logo-{mode}.{ext} and returns the
    relative path so the client can store it in settings.
    """
    from api.upload import parse_multipart

    content_type = handler.headers.get("Content-Type", "")
    content_length = int(handler.headers.get("Content-Length", "0"))

    if not content_type.startswith("multipart/form-data"):
        return bad(handler, "Expected multipart/form-data")

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

    # Convert SVG/ICO to PNG naming for consistency? No — keep original ext
    # but save to the canonical path. For PNG we use .png, SVG .svg, ICO .ico.
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
    import json as _json

    content_length = int(handler.headers.get("Content-Length", "0"))
    body_raw = handler.rfile.read(content_length) if content_length else b"{}"
    try:
        body = _json.loads(body_raw)
    except (ValueError, TypeError):
        return bad(handler, "Invalid JSON body")

    mode = (body.get("mode") or "").strip()
    if mode not in _VALID_MODES:
        return bad(handler, f"Invalid mode '{mode}'. Must be 'light' or 'dark'")

    deleted = _delete_logo_files_for_mode(mode)

    return j(handler, {"ok": True, "deleted": bool(deleted)})
