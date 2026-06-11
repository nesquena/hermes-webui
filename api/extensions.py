"""Opt-in WebUI extension hooks.

This module intentionally provides a small, self-hosted extension surface:
configured same-origin script/style injection plus sandboxed static file serving.
It is disabled by default and never executes or fetches third-party URLs.
"""

import html
import logging
import os
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import unquote, urlsplit

from api.helpers import _security_headers, j

_log = logging.getLogger(__name__)

# Sane bound on configured URLs — real extensions ship 1-3 files. Higher values
# typically indicate a misconfiguration (one giant unsplit string, or a runaway
# generator script that wrote an env-var template without filtering). Capping
# avoids rendering tens of thousands of <script> tags into every page load.
_MAX_URL_LIST = 32

# Tracks rejected URL strings we've already warned about so a misconfigured env
# var doesn't spam the log on every request that re-reads it.
_warned_urls: set = set()

EXTENSION_ROUTE_PREFIX = "/extensions/"
_EXTENSION_DIR_ENV = "HERMES_WEBUI_EXTENSION_DIR"
_EXTENSION_SCRIPT_URLS_ENV = "HERMES_WEBUI_EXTENSION_SCRIPT_URLS"
_EXTENSION_STYLESHEET_URLS_ENV = "HERMES_WEBUI_EXTENSION_STYLESHEET_URLS"
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


def _read_url_list(env_name: str) -> List[str]:
    raw = os.getenv(env_name, "")
    urls = []
    for item in raw.split(","):
        value = item.strip()
        if not value:
            continue
        if _is_safe_asset_url(value):
            urls.append(value)
            if len(urls) >= _MAX_URL_LIST:
                # Stop accumulating after the cap. Anything past this point
                # would be silently dropped anyway; logging once makes the
                # truncation visible to a confused operator.
                if env_name not in _warned_urls:
                    _warned_urls.add(env_name)
                    _log.warning(
                        "Extension URL list %s truncated at %d entries",
                        env_name, _MAX_URL_LIST,
                    )
                break
        elif value not in _warned_urls:
            # First-time-seen invalid URL: log once per process so a typo
            # in HERMES_WEBUI_EXTENSION_*_URLS doesn't disappear silently.
            _warned_urls.add(value)
            _log.warning(
                "Rejected extension URL %r from %s (not a same-origin "
                "/extensions/ or /static/ path, or contains unsafe chars)",
                value, env_name,
            )
    return urls


def get_extension_config() -> Dict[str, object]:
    """Return public extension config without exposing filesystem paths."""
    enabled = _extension_root() is not None
    if not enabled:
        return {"enabled": False, "script_urls": [], "stylesheet_urls": []}
    return {
        "enabled": True,
        "script_urls": _read_url_list(_EXTENSION_SCRIPT_URLS_ENV),
        "stylesheet_urls": _read_url_list(_EXTENSION_STYLESHEET_URLS_ENV),
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
