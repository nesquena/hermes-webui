"""Opt-in WebUI extension hooks.

This module intentionally provides a small, self-hosted extension surface:
configured same-origin script/style injection plus sandboxed static file serving.
It is disabled by default and never executes or fetches third-party URLs.
"""

import html
import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import unquote, urlsplit

from api.helpers import _security_headers, j

_log = logging.getLogger(__name__)

# Sane bound on configured URLs — real extensions ship 1-3 files. Higher values
# typically indicate a misconfiguration (one giant unsplit string, or a runaway
# generator script that wrote an env-var template without filtering). Capping
# avoids rendering tens of thousands of <script> tags into every page load.
_MAX_URL_LIST = 32

# Keep extension manifests small and auditable. The manifest is a convenience for
# bundling static assets, not a package manager or dependency lockfile.
_MAX_MANIFEST_BYTES = 64 * 1024

# Tracks rejected URL strings we've already warned about so a misconfigured env
# var doesn't spam the log on every request that re-reads it.
_warned_urls: set = set()


class _ManifestTooLarge(ValueError):
    pass


EXTENSION_ROUTE_PREFIX = "/extensions/"
_EXTENSION_DIR_ENV = "HERMES_WEBUI_EXTENSION_DIR"
_EXTENSION_SCRIPT_URLS_ENV = "HERMES_WEBUI_EXTENSION_SCRIPT_URLS"
_EXTENSION_STYLESHEET_URLS_ENV = "HERMES_WEBUI_EXTENSION_STYLESHEET_URLS"
_EXTENSION_MANIFEST_ENV = "HERMES_WEBUI_EXTENSION_MANIFEST"
_ALLOWED_ASSET_PREFIXES = ("/extensions/", "/static/")

_EXTENSION_MIME = {
    "css": "text/css",
    "js": "application/javascript",
    "html": "text/html",
    "svg": "image/svg+xml",
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "ico": "image/x-icon",
    "gif": "image/gif",
    "webp": "image/webp",
    "woff": "font/woff",
    "woff2": "font/woff2",
    "ttf": "font/ttf",
    "otf": "font/otf",
    "wasm": "application/wasm",
}
_TEXT_MIME_TYPES = {"text/css", "application/javascript", "text/html", "image/svg+xml", "text/plain"}


def _extension_root() -> Optional[Path]:
    """Return the configured extension directory, or None when disabled.

    A missing or non-directory path disables extensions instead of failing open.
    The startup docs encourage users to point this at a directory they control.
    """
    raw = os.getenv(_EXTENSION_DIR_ENV, "").strip()
    if not raw:
        return None
    root = Path(raw).expanduser().resolve()
    if not root.exists() or not root.is_dir():
        return None
    return root


def _extension_root_status() -> Tuple[Optional[Path], bool, bool]:
    """Return (root, configured, valid) without exposing the configured path."""
    raw = os.getenv(_EXTENSION_DIR_ENV, "").strip()
    if not raw:
        return None, False, False
    root = Path(raw).expanduser().resolve()
    if not root.exists() or not root.is_dir():
        return None, True, False
    return root, True, True


def _new_diagnostics() -> Dict[str, Any]:
    return {"warnings": []}


def _add_diagnostic_warning(
    diagnostics: Optional[Dict[str, Any]], code: str, source: str
) -> None:
    """Record a sanitized diagnostic warning.

    Warnings intentionally carry only stable codes and coarse sources. They never
    include filesystem paths, raw environment values, or rejected URL strings.
    """
    if diagnostics is None:
        return
    warnings = diagnostics.setdefault("warnings", [])
    if not isinstance(warnings, list):
        return
    warning = {"code": code, "source": source}
    if warning not in warnings:
        warnings.append(warning)


def _fully_unquote_path(path: str) -> str:
    """Decode percent-encoding until stable so encoded dot-segments cannot hide.

    Iterates up to 10 times so even quadruple-encoded inputs like
    ``%2525252e%2525252e`` collapse to literal ``..`` and are rejected by
    the segment-level safety check downstream. URL strings stabilize in
    fewer than 5 iterations in practice; the cap is defensive.
    """
    previous = path
    for _ in range(10):
        current = unquote(previous)
        if current == previous:
            return current
        previous = current
    return previous


def _is_safe_asset_url(value: str) -> bool:
    """Allow only same-origin extension/static asset URLs.

    External schemes, protocol-relative URLs, fragments, arbitrary API paths, and
    encoded traversal are rejected so enabling extensions does not require
    loosening the CSP.
    """
    if not value or any(ch in value for ch in ('\x00', '\r', '\n', '"', "'", "<", ">", "\\")):
        return False
    parsed = urlsplit(value)
    if parsed.scheme or parsed.netloc or parsed.fragment:
        return False

    decoded_path = _fully_unquote_path(parsed.path)
    if not any(decoded_path.startswith(prefix) for prefix in _ALLOWED_ASSET_PREFIXES):
        return False

    for prefix in _ALLOWED_ASSET_PREFIXES:
        if decoded_path.startswith(prefix):
            return _is_safe_relative_path(decoded_path[len(prefix) :])
    return False


def _warn_rejected_url(value: str, source: str) -> None:
    if value in _warned_urls:
        return
    _warned_urls.add(value)
    _log.warning(
        "Rejected extension URL %r from %s (not a same-origin "
        "/extensions/ or /static/ path, or contains unsafe chars)",
        value, source,
    )


def _append_safe_asset_url(
    urls: List[str],
    value: str,
    source: str,
    *,
    dedupe: bool = True,
    diagnostics: Optional[Dict[str, Any]] = None,
) -> bool:
    """Append a validated URL while preserving order and the global cap.

    Returns False when the caller should stop accumulating entries for this list.
    Manifest paths dedupe by default, while env-only lists preserve their legacy
    behavior unless they are appending after manifest-provided assets.
    """
    value = value.strip() if isinstance(value, str) else ""
    if not value:
        return True
    if not _is_safe_asset_url(value):
        _warn_rejected_url(value, source)
        _add_diagnostic_warning(diagnostics, "asset_url_rejected", source)
        return True
    if dedupe and value in urls:
        return True
    if len(urls) >= _MAX_URL_LIST:
        if source not in _warned_urls:
            _warned_urls.add(source)
            _log.warning(
                "Extension URL list %s truncated at %d entries",
                source, _MAX_URL_LIST,
            )
        _add_diagnostic_warning(diagnostics, "asset_url_list_truncated", source)
        return False
    urls.append(value)
    return True


def _read_url_list(
    env_name: str,
    existing: Optional[List[str]] = None,
    *,
    diagnostics: Optional[Dict[str, Any]] = None,
) -> List[str]:
    raw = os.getenv(env_name, "")
    urls = list(existing or [])
    # Preserve legacy env-only behavior: duplicate env URLs injected twice before
    # manifests existed. When a manifest seeds the list, dedupe appended env URLs
    # so bundle manifests and explicit overrides do not double-load an asset.
    dedupe = existing is not None
    for item in raw.split(","):
        if not _append_safe_asset_url(
            urls, item, env_name, dedupe=dedupe, diagnostics=diagnostics
        ):
            break
    return urls


def _manifest_path_with_status(root: Path) -> Tuple[Optional[Path], str]:
    raw = os.getenv(_EXTENSION_MANIFEST_ENV, "").strip()
    if not raw:
        return None, "not_configured"
    if raw.startswith(("/", "~")):
        _log.warning("Rejected extension manifest path from %s", _EXTENSION_MANIFEST_ENV)
        return None, "invalid_path"
    rel = _fully_unquote_path(raw)
    if not _is_safe_relative_path(rel):
        _log.warning("Rejected extension manifest path from %s", _EXTENSION_MANIFEST_ENV)
        return None, "invalid_path"
    manifest = (root / rel).resolve()
    try:
        manifest.relative_to(root)
    except ValueError:
        _log.warning("Rejected extension manifest path from %s", _EXTENSION_MANIFEST_ENV)
        return None, "invalid_path"
    return manifest, "configured"


def _manifest_path(root: Path) -> Optional[Path]:
    manifest, _ = _manifest_path_with_status(root)
    return manifest


def _manifest_asset_url(value: object) -> str:
    """Normalize a manifest asset entry to the existing same-origin URL format."""
    if not isinstance(value, str):
        return ""
    item = value.strip()
    if not item:
        return ""
    parsed = urlsplit(item)
    if parsed.scheme or parsed.netloc or item.startswith("//"):
        return item
    # Manifests are meant to make bundled local assets less noisy to list, so
    # bare relative paths resolve under /extensions/. Absolute same-origin paths
    # are still allowed and go through the same validator as env-configured URLs.
    if item.startswith("/"):
        return item
    return EXTENSION_ROUTE_PREFIX + item


def _iter_manifest_entries(manifest: object) -> List[Tuple[str, object]]:
    entries: List[Tuple[str, object]] = []
    extension_entries: object = []
    if isinstance(manifest, dict):
        entries.append(("manifest", manifest))
        extension_entries = manifest.get("extensions", [])
    elif isinstance(manifest, list):
        extension_entries = manifest
    if isinstance(extension_entries, list):
        for index, extension in enumerate(extension_entries):
            if not isinstance(extension, dict):
                continue
            if extension.get("enabled", True) is False:
                continue
            entries.append((f"manifest.extensions[{index}]", extension))
    return entries


def _entry_asset_values(entry: Dict[str, object], key: str) -> List[object]:
    values = entry.get(key, [])
    return values if isinstance(values, list) else []


def _read_manifest_text(manifest_file: Path) -> str:
    with manifest_file.open("rb") as fh:
        data = fh.read(_MAX_MANIFEST_BYTES + 1)
    if len(data) > _MAX_MANIFEST_BYTES:
        raise _ManifestTooLarge("manifest too large")
    return data.decode("utf-8")


def _read_manifest_urls_with_diagnostics(
    root: Path, diagnostics: Optional[Dict[str, Any]] = None
) -> Tuple[List[str], List[str], Dict[str, Any]]:
    manifest_file, path_status = _manifest_path_with_status(root)
    manifest_status: Dict[str, Any] = {
        "configured": path_status != "not_configured",
        "loaded": False,
        "status": path_status,
        "entry_count": 0,
        "script_count": 0,
        "stylesheet_count": 0,
    }
    if manifest_file is None:
        if path_status == "invalid_path":
            _add_diagnostic_warning(diagnostics, "manifest_invalid_path", "manifest")
        return [], [], manifest_status
    try:
        if not manifest_file.exists() or not manifest_file.is_file():
            _log.warning("Configured extension manifest was not found")
            manifest_status["status"] = "missing"
            _add_diagnostic_warning(diagnostics, "manifest_missing", "manifest")
            return [], [], manifest_status
        manifest = json.loads(_read_manifest_text(manifest_file))
    except _ManifestTooLarge:
        _log.warning("Configured extension manifest exceeds %d bytes", _MAX_MANIFEST_BYTES)
        manifest_status["status"] = "oversized"
        _add_diagnostic_warning(diagnostics, "manifest_oversized", "manifest")
        return [], [], manifest_status
    except json.JSONDecodeError:
        _log.warning("Configured extension manifest is not valid JSON")
        manifest_status["status"] = "malformed"
        _add_diagnostic_warning(diagnostics, "manifest_malformed", "manifest")
        return [], [], manifest_status
    except RecursionError:
        # A <=64KB but deeply-nested manifest makes json.loads exceed the
        # interpreter recursion limit. Without this, the RecursionError escapes
        # into the app-shell route and every page load 503s. Fail safe.
        _log.warning("Configured extension manifest is too deeply nested")
        manifest_status["status"] = "too_deeply_nested"
        _add_diagnostic_warning(diagnostics, "manifest_too_deeply_nested", "manifest")
        return [], [], manifest_status
    except (OSError, UnicodeDecodeError):
        _log.warning("Configured extension manifest could not be read")
        manifest_status["status"] = "unreadable"
        _add_diagnostic_warning(diagnostics, "manifest_unreadable", "manifest")
        return [], [], manifest_status

    scripts: List[str] = []
    stylesheets: List[str] = []
    entries = _iter_manifest_entries(manifest)
    manifest_status["entry_count"] = len(entries)
    scripts_full = False
    stylesheets_full = False
    for _source, entry in entries:
        if not isinstance(entry, dict):
            continue
        script_source = "manifest:scripts"
        stylesheet_source = "manifest:stylesheets"
        if not scripts_full:
            for value in _entry_asset_values(entry, "scripts"):
                if not _append_safe_asset_url(
                    scripts,
                    _manifest_asset_url(value),
                    script_source,
                    diagnostics=diagnostics,
                ):
                    scripts_full = True
                    break
        if not stylesheets_full:
            for value in _entry_asset_values(entry, "stylesheets"):
                if not _append_safe_asset_url(
                    stylesheets,
                    _manifest_asset_url(value),
                    stylesheet_source,
                    diagnostics=diagnostics,
                ):
                    stylesheets_full = True
                    break
        if scripts_full and stylesheets_full:
            break
    manifest_status.update(
        {
            "loaded": True,
            "status": "loaded",
            "script_count": len(scripts),
            "stylesheet_count": len(stylesheets),
        }
    )
    return scripts, stylesheets, manifest_status


def _read_manifest_urls(root: Path) -> Tuple[List[str], List[str]]:
    scripts, stylesheets, _ = _read_manifest_urls_with_diagnostics(root)
    return scripts, stylesheets


def get_extension_config() -> Dict[str, Any]:
    """Return public extension config without exposing filesystem paths."""
    root = _extension_root()
    if root is None:
        return {"enabled": False, "script_urls": [], "stylesheet_urls": []}
    manifest_scripts, manifest_stylesheets = _read_manifest_urls(root)
    return {
        "enabled": True,
        "script_urls": _read_url_list(
            _EXTENSION_SCRIPT_URLS_ENV, manifest_scripts or None
        ),
        "stylesheet_urls": _read_url_list(
            _EXTENSION_STYLESHEET_URLS_ENV, manifest_stylesheets or None
        ),
    }


def get_extension_status() -> Dict[str, Any]:
    """Return sanitized read-only extension diagnostics for administrators."""
    diagnostics = _new_diagnostics()
    root, dir_configured, dir_valid = _extension_root_status()
    manifest_configured = bool(os.getenv(_EXTENSION_MANIFEST_ENV, "").strip())
    manifest_status: Dict[str, Any] = {
        "configured": manifest_configured,
        "loaded": False,
        "status": "extension_disabled" if manifest_configured else "not_configured",
        "entry_count": 0,
        "script_count": 0,
        "stylesheet_count": 0,
    }
    if dir_configured and not dir_valid:
        _add_diagnostic_warning(diagnostics, "extension_dir_unavailable", "extension_dir")

    if root is None:
        return {
            "enabled": False,
            "extension_dir_configured": dir_configured,
            "extension_dir_valid": False,
            "script_urls": [],
            "stylesheet_urls": [],
            "counts": {"script_urls": 0, "stylesheet_urls": 0},
            "manifest": manifest_status,
            "warnings": diagnostics["warnings"],
        }

    manifest_scripts, manifest_stylesheets, manifest_status = _read_manifest_urls_with_diagnostics(
        root, diagnostics
    )
    script_urls = _read_url_list(
        _EXTENSION_SCRIPT_URLS_ENV,
        manifest_scripts or None,
        diagnostics=diagnostics,
    )
    stylesheet_urls = _read_url_list(
        _EXTENSION_STYLESHEET_URLS_ENV,
        manifest_stylesheets or None,
        diagnostics=diagnostics,
    )
    return {
        "enabled": True,
        "extension_dir_configured": True,
        "extension_dir_valid": True,
        "script_urls": script_urls,
        "stylesheet_urls": stylesheet_urls,
        "counts": {
            "script_urls": len(script_urls),
            "stylesheet_urls": len(stylesheet_urls),
        },
        "manifest": manifest_status,
        "warnings": diagnostics["warnings"],
    }


def inject_extension_tags(index_html: str) -> str:
    """Inject configured extension tags into the app shell.

    Tags are inserted only when the extension directory is enabled. URLs are
    escaped even though they are already validated, keeping the renderer robust
    if validation rules evolve later.
    """
    config = get_extension_config()
    if not config["enabled"]:
        return index_html

    result = index_html
    stylesheet_tags = [
        '<link rel="stylesheet" href="{}">'.format(html.escape(url, quote=True))
        for url in config["stylesheet_urls"]
    ]
    script_tags = [
        '<script src="{}" defer></script>'.format(html.escape(url, quote=True))
        for url in config["script_urls"]
    ]

    if stylesheet_tags:
        head_marker = "</head>"
        block = "\n".join(stylesheet_tags) + "\n"
        if head_marker in result:
            result = result.replace(head_marker, block + head_marker, 1)
        else:
            result = block + result

    if script_tags:
        body_marker = "</body>"
        block = "\n".join(script_tags) + "\n"
        if body_marker in result:
            result = result.replace(body_marker, block + body_marker, 1)
        else:
            result = result + "\n" + block

    return result


def _is_safe_relative_path(rel: str) -> bool:
    if not rel or "\x00" in rel or "\\" in rel:
        return False
    for segment in rel.split("/"):
        if not segment or segment in (".", "..") or segment.startswith("."):
            return False
    return True


def _not_found(handler) -> bool:
    j(handler, {"error": "not found"}, status=404)
    return True


def serve_extension_static(handler, parsed) -> bool:
    """Serve a file from the configured extension directory.

    The function always returns True for /extensions/* requests: either a file
    response or a 404. It never reveals why a request failed, which avoids
    leaking local paths or extension configuration details.
    """
    root = _extension_root()
    if root is None:
        return _not_found(handler)

    rel = unquote(parsed.path[len(EXTENSION_ROUTE_PREFIX) :])
    if not _is_safe_relative_path(rel):
        return _not_found(handler)

    static_file = (root / rel).resolve()
    try:
        static_file.relative_to(root)
    except ValueError:
        return _not_found(handler)

    if not static_file.exists() or not static_file.is_file():
        return _not_found(handler)

    ct = _EXTENSION_MIME.get(static_file.suffix.lower().lstrip("."), "text/plain")
    ct_header = "{}; charset=utf-8".format(ct) if ct in _TEXT_MIME_TYPES else ct
    try:
        raw = static_file.read_bytes()
    except OSError:
        return _not_found(handler)

    handler.send_response(200)
    handler.send_header("Content-Type", ct_header)
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Content-Length", str(len(raw)))
    _security_headers(handler)
    handler.end_headers()
    handler.wfile.write(raw)
    return True
