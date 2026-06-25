"""Opt-in WebUI extension hooks.

This module intentionally provides a small, self-hosted extension surface:
configured same-origin script/style injection plus sandboxed static file serving.
It is disabled by default and never executes or fetches third-party URLs.
"""

import html
import json
import logging
import os
import re
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import unquote, urlsplit
import hashlib
import io
import time
import zipfile
from urllib.request import HTTPRedirectHandler, build_opener, urlopen

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


class ExtensionToggleError(Exception):
    """Sanitized extension mutation error safe to return to the browser."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


class ExtensionInstallError(Exception):
    """Sanitized extension install/uninstall error safe to return to the browser."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


EXTENSION_ROUTE_PREFIX = "/extensions/"
_EXTENSION_DIR_ENV = "HERMES_WEBUI_EXTENSION_DIR"
_EXTENSION_SCRIPT_URLS_ENV = "HERMES_WEBUI_EXTENSION_SCRIPT_URLS"
_EXTENSION_STYLESHEET_URLS_ENV = "HERMES_WEBUI_EXTENSION_STYLESHEET_URLS"
_EXTENSION_MANIFEST_ENV = "HERMES_WEBUI_EXTENSION_MANIFEST"
_ALLOWED_ASSET_PREFIXES = ("/extensions/", "/static/")
_SIDECAR_WARNING_SOURCE = "manifest:sidecars"
_DEFAULT_SIDECAR_HEALTH_PATH = "/health"
_LOOPBACK_SIDECAR_HOSTS = {"127.0.0.1", "localhost", "::1"}
_EXTENSION_STATE_FILENAME = "extension-overrides.json"
_MAX_EXTENSION_STATE_BYTES = 32 * 1024
_MAX_DISABLED_EXTENSION_IDS = 512
_EXTENSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_EXTENSION_STATE_WARNING_SOURCE = "extension_state"
_EXTENSION_STATE_LOCK = threading.Lock()

_GALLERY_INSTALL_STATE_FILENAME = "extension-install-manifest.json"
_MAX_INSTALL_MANIFEST_BYTES = 128 * 1024
_MAX_GALLERY_INSTALLED_IDS = 256
_MAX_ZIP_DOWNLOAD_BYTES = 32 * 1024 * 1024
_REGISTRY_URL = "https://hermes-webui.github.io/hermes-webui-extensions/registry.json"
_REGISTRY_ALLOWED_DOWNLOAD_HOSTS = frozenset({"hermes-webui.github.io"})
_REGISTRY_CACHE: dict = {}
_REGISTRY_LOCK = threading.Lock()
_REGISTRY_TTL_SECONDS = 300


class _AllowlistRedirectHandler(HTTPRedirectHandler):
    """Reject redirects to hosts not in the download allowlist."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parsed = urlsplit(newurl)
        if parsed.scheme != "https" or parsed.hostname not in _REGISTRY_ALLOWED_DOWNLOAD_HOSTS:
            raise ExtensionInstallError("Download redirected to disallowed host")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _safe_download(url: str, max_bytes: int, timeout: int = 30) -> bytes:
    """Download from an allowlisted host, rejecting cross-host redirects."""
    opener = build_opener(_AllowlistRedirectHandler)
    resp = opener.open(url, timeout=timeout)
    try:
        return resp.read(max_bytes + 1)
    finally:
        resp.close()


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


def _valid_extension_id(value: object) -> bool:
    return isinstance(value, str) and bool(_EXTENSION_ID_RE.fullmatch(value.strip()))


def _extension_state_dir() -> Path:
    """Return the WebUI-managed state directory for extension overrides."""
    try:
        from api.config import STATE_DIR

        return Path(STATE_DIR)
    except Exception:
        return Path(os.getenv("HERMES_WEBUI_STATE_DIR", str(Path.home() / ".hermes" / "webui"))).expanduser()


def _extension_state_file() -> Path:
    return _extension_state_dir() / _EXTENSION_STATE_FILENAME


def _empty_extension_state() -> Dict[str, Any]:
    return {"version": 1, "disabled_extensions": []}


def _load_extension_state(diagnostics: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Load UI-managed extension overrides, failing safe without path leaks."""
    state_file = _extension_state_file()
    try:
        if not state_file.exists() or not state_file.is_file():
            return _empty_extension_state()
        with state_file.open("rb") as fh:
            raw = fh.read(_MAX_EXTENSION_STATE_BYTES + 1)
        if len(raw) > _MAX_EXTENSION_STATE_BYTES:
            _add_diagnostic_warning(
                diagnostics, "extension_state_oversized", _EXTENSION_STATE_WARNING_SOURCE
            )
            return _empty_extension_state()
        parsed = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        _add_diagnostic_warning(
            diagnostics, "extension_state_unreadable", _EXTENSION_STATE_WARNING_SOURCE
        )
        return _empty_extension_state()
    if not isinstance(parsed, dict):
        _add_diagnostic_warning(
            diagnostics, "extension_state_invalid", _EXTENSION_STATE_WARNING_SOURCE
        )
        return _empty_extension_state()
    disabled_raw = parsed.get("disabled_extensions", [])
    if not isinstance(disabled_raw, list):
        _add_diagnostic_warning(
            diagnostics, "extension_state_invalid", _EXTENSION_STATE_WARNING_SOURCE
        )
        return _empty_extension_state()
    disabled: List[str] = []
    seen: Set[str] = set()
    invalid = False
    for value in disabled_raw:
        if not _valid_extension_id(value):
            invalid = True
            continue
        ext_id = str(value).strip()
        if ext_id in seen:
            continue
        seen.add(ext_id)
        disabled.append(ext_id)
        if len(disabled) >= _MAX_DISABLED_EXTENSION_IDS:
            _add_diagnostic_warning(
                diagnostics, "extension_state_truncated", _EXTENSION_STATE_WARNING_SOURCE
            )
            break
    if invalid:
        _add_diagnostic_warning(
            diagnostics, "extension_state_invalid_entries", _EXTENSION_STATE_WARNING_SOURCE
        )
    return {"version": 1, "disabled_extensions": disabled}


def _write_extension_state(state: Dict[str, Any]) -> None:
    """Persist extension overrides with an atomic same-directory replace."""
    disabled_raw = state.get("disabled_extensions", [])
    disabled: List[str] = []
    seen: Set[str] = set()
    if isinstance(disabled_raw, list):
        for value in disabled_raw:
            if not _valid_extension_id(value):
                continue
            ext_id = str(value).strip()
            if ext_id in seen:
                continue
            seen.add(ext_id)
            disabled.append(ext_id)
            if len(disabled) >= _MAX_DISABLED_EXTENSION_IDS:
                break
    payload = {"version": 1, "disabled_extensions": disabled}
    target = _extension_state_file()
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    data = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    try:
        with tmp.open("wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, target)
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass


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


def _manifest_asset_url(value: object, asset_base: str = "") -> str:
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
    base = asset_base.strip("/")
    rel = f"{base}/{item}" if base else item
    return EXTENSION_ROUTE_PREFIX + rel


def _manifest_asset_value_with_base(value: object, asset_base: str) -> object:
    """Rewrite a manifest asset value so it remains relative to its manifest file."""
    if not isinstance(value, str):
        return value
    item = value.strip()
    if not item:
        return item
    parsed = urlsplit(item)
    if parsed.scheme or parsed.netloc or item.startswith("//") or item.startswith("/"):
        return item
    base = asset_base.strip("/")
    return f"{base}/{item}" if base else item


def _copy_manifest_entry_with_asset_base(entry: Dict[str, object], asset_base: str) -> Dict[str, object]:
    copied = dict(entry)
    for key in ("scripts", "stylesheets"):
        values = copied.get(key)
        if isinstance(values, list):
            copied[key] = [_manifest_asset_value_with_base(value, asset_base) for value in values]
    return copied


def _manifest_entry_text(entry: Dict[str, object], key: str) -> str:
    value = entry.get(key)
    if not isinstance(value, str):
        return ""
    return value.strip()


def _normalize_loopback_sidecar_origin(value: object) -> Optional[str]:
    """Return a canonical loopback origin or None when unsafe.

    Only browser-addressable loopback HTTP(S) origins are accepted. The returned
    value is rebuilt from parsed components so rejected raw input is never echoed
    into diagnostics.
    """
    if not isinstance(value, str):
        return None
    origin = value.strip()
    if not origin or any(
        ch in origin for ch in ("\x00", "\r", "\n", '"', "'", "<", ">", "\\")
    ):
        return None
    parsed = urlsplit(origin)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return None
    if parsed.username or parsed.password:
        return None
    if parsed.path or parsed.query or parsed.fragment:
        return None
    host = (parsed.hostname or "").lower()
    if host not in _LOOPBACK_SIDECAR_HOSTS:
        return None
    try:
        port = parsed.port
    except ValueError:
        return None
    display_host = f"[{host}]" if ":" in host else host
    return f"{parsed.scheme}://{display_host}{':' + str(port) if port is not None else ''}"


def _normalize_sidecar_health_path(value: object) -> Optional[str]:
    """Return a safe sidecar health path, or None when unsafe.

    Health paths are same-origin paths relative to the validated sidecar origin.
    Queries are rejected even though they are not cross-origin: health checks are
    diagnostics, and query strings often accidentally carry tokens.
    """
    if not isinstance(value, str):
        return None
    path = value.strip()
    if not path or not path.startswith("/") or path.startswith("//"):
        return None
    if any(ch in path for ch in ("\x00", "\r", "\n", '"', "'", "<", ">", "\\")):
        return None
    parsed = urlsplit(path)
    if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment:
        return None
    decoded_path = _fully_unquote_path(parsed.path)
    if any(ch in decoded_path for ch in ("\x00", "\r", "\n", '"', "'", "<", ">", "\\")):
        return None
    # #4612 (Codex gate): the raw query/fragment ban above runs BEFORE percent-
    # decoding, so an encoded delimiter (e.g. "/health%3Ftoken=abc" -> "?token=abc"
    # or "/health%23frag" -> "#frag") would survive into the probed URL despite the
    # documented query/fragment ban. Re-reject "?" and "#" on the decoded path.
    if any(ch in decoded_path for ch in ("?", "#")):
        return None
    if any(ch.isspace() for ch in decoded_path):
        return None
    if not decoded_path.startswith("/") or decoded_path.startswith("//"):
        return None
    segments = decoded_path.split("/")[1:]
    if not segments:
        return None
    for segment in segments:
        if not segment or segment in (".", ".."):
            return None
    return decoded_path


def _sidecar_from_manifest_entry(
    entry: Dict[str, object], diagnostics: Optional[Dict[str, Any]] = None
) -> Optional[Dict[str, str]]:
    raw = entry.get("sidecar")
    if raw is None:
        return None
    if not isinstance(raw, dict):
        _add_diagnostic_warning(diagnostics, "sidecar_invalid", _SIDECAR_WARNING_SOURCE)
        return None
    if raw.get("type") != "loopback":
        _add_diagnostic_warning(
            diagnostics, "sidecar_type_unsupported", _SIDECAR_WARNING_SOURCE
        )
        return None
    origin = _normalize_loopback_sidecar_origin(raw.get("origin"))
    if origin is None:
        _add_diagnostic_warning(
            diagnostics, "sidecar_origin_rejected", _SIDECAR_WARNING_SOURCE
        )
        return None
    if "health_path" in raw:
        health_path = _normalize_sidecar_health_path(raw.get("health_path"))
        if health_path is None:
            # Missing health_path defaults to /health; an explicitly invalid path
            # rejects the sidecar so the browser does not probe a declaration the
            # administrator needs to fix.
            _add_diagnostic_warning(
                diagnostics, "sidecar_health_path_rejected", _SIDECAR_WARNING_SOURCE
            )
            return None
    else:
        health_path = _DEFAULT_SIDECAR_HEALTH_PATH
    sidecar_id = _manifest_entry_text(entry, "id")
    name = _manifest_entry_text(entry, "name")
    return {
        "id": sidecar_id,
        "name": name,
        "type": "loopback",
        "origin": origin,
        "health_path": health_path,
        "health_url": f"{origin}{health_path}",
    }


def _manifest_extension_entries(manifest: object) -> List[Tuple[str, int, Dict[str, object]]]:
    extension_entries: object = []
    if isinstance(manifest, dict):
        extension_entries = manifest.get("extensions", [])
    elif isinstance(manifest, list):
        extension_entries = manifest
    entries: List[Tuple[str, int, Dict[str, object]]] = []
    if isinstance(extension_entries, list):
        for index, extension in enumerate(extension_entries):
            if isinstance(extension, dict):
                entries.append((f"manifest.extensions[{index}]", index, extension))
    return entries


def _iter_manifest_entries(
    manifest: object, disabled_ids: Optional[Set[str]] = None
) -> List[Tuple[str, object]]:
    disabled_ids = disabled_ids or set()
    entries: List[Tuple[str, object]] = []
    if isinstance(manifest, dict):
        entries.append(("manifest", manifest))
    for source, _index, extension in _manifest_extension_entries(manifest):
        if extension.get("enabled", True) is False:
            continue
        ext_id = _manifest_entry_text(extension, "id")
        if _valid_extension_id(ext_id) and ext_id in disabled_ids:
            continue
        entries.append((source, extension))
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


def _empty_manifest_status(path_status: str) -> Dict[str, Any]:
    return {
        "configured": path_status != "not_configured",
        "loaded": False,
        "status": path_status,
        "_asset_base": "",
        "entry_count": 0,
        "script_count": 0,
        "stylesheet_count": 0,
        "sidecar_count": 0,
    }


def _manifest_asset_base(root: Path, manifest_file: Path) -> str:
    try:
        rel_parent = manifest_file.parent.relative_to(root).as_posix()
    except ValueError:
        return ""
    return "" if rel_parent == "." else rel_parent


def _gallery_installed_runtime_manifest(
    root: Path, diagnostics: Optional[Dict[str, Any]] = None
) -> Optional[Dict[str, object]]:
    """Build a runtime manifest from gallery-installed extension manifests."""
    install_manifest = _load_install_manifest()
    installed = install_manifest.get("installed", {})
    if not isinstance(installed, dict):
        return None
    entries: List[Dict[str, object]] = []
    for ext_id in sorted(installed):
        if not _valid_extension_id(ext_id):
            continue
        manifest_file = root / ext_id / "manifest.json"
        try:
            if not manifest_file.exists() or not manifest_file.is_file():
                _add_diagnostic_warning(diagnostics, "gallery_manifest_missing", "gallery")
                continue
            manifest = json.loads(_read_manifest_text(manifest_file))
        except _ManifestTooLarge:
            _add_diagnostic_warning(diagnostics, "gallery_manifest_oversized", "gallery")
            continue
        except json.JSONDecodeError:
            _add_diagnostic_warning(diagnostics, "gallery_manifest_malformed", "gallery")
            continue
        except RecursionError:
            _add_diagnostic_warning(diagnostics, "gallery_manifest_too_deeply_nested", "gallery")
            continue
        except (OSError, UnicodeDecodeError):
            _add_diagnostic_warning(diagnostics, "gallery_manifest_unreadable", "gallery")
            continue
        asset_base = ext_id
        if isinstance(manifest, dict):
            top_entry: Dict[str, object] = {"id": ext_id}
            for key in ("name", "enabled", "scripts", "stylesheets", "sidecar"):
                if key in manifest:
                    top_entry[key] = manifest[key]
            if any(key in top_entry for key in ("scripts", "stylesheets", "sidecar")):
                entries.append(_copy_manifest_entry_with_asset_base(top_entry, asset_base))
        for _source, _index, entry in _manifest_extension_entries(manifest):
            copied = _copy_manifest_entry_with_asset_base(entry, asset_base)
            if not _valid_extension_id(copied.get("id")):
                copied["id"] = ext_id
            entries.append(copied)
    if not entries:
        return None
    return {"extensions": entries}


def _load_manifest_with_status(
    root: Path, diagnostics: Optional[Dict[str, Any]] = None
) -> Tuple[Optional[object], Dict[str, Any]]:
    """Load the configured manifest once, returning sanitized status/warnings."""
    manifest_file, path_status = _manifest_path_with_status(root)
    manifest_status = _empty_manifest_status(path_status)
    if manifest_file is None:
        if path_status == "invalid_path":
            _add_diagnostic_warning(diagnostics, "manifest_invalid_path", "manifest")
        elif path_status == "not_configured":
            manifest = _gallery_installed_runtime_manifest(root, diagnostics)
            if manifest is not None:
                manifest_status.update(
                    {
                        "loaded": True,
                        "status": "gallery_installed",
                        "_asset_base": "",
                    }
                )
                return manifest, manifest_status
        return None, manifest_status
    try:
        if not manifest_file.exists() or not manifest_file.is_file():
            _log.warning("Configured extension manifest was not found")
            manifest_status["status"] = "missing"
            _add_diagnostic_warning(diagnostics, "manifest_missing", "manifest")
            return None, manifest_status
        manifest = json.loads(_read_manifest_text(manifest_file))
        manifest_status.update(
            {
                "loaded": True,
                "status": "loaded",
                "_asset_base": _manifest_asset_base(root, manifest_file),
            }
        )
        return manifest, manifest_status
    except _ManifestTooLarge:
        _log.warning("Configured extension manifest exceeds %d bytes", _MAX_MANIFEST_BYTES)
        manifest_status["status"] = "oversized"
        _add_diagnostic_warning(diagnostics, "manifest_oversized", "manifest")
    except json.JSONDecodeError:
        _log.warning("Configured extension manifest is not valid JSON")
        manifest_status["status"] = "malformed"
        _add_diagnostic_warning(diagnostics, "manifest_malformed", "manifest")
    except RecursionError:
        # A <=64KB but deeply-nested manifest makes json.loads exceed the
        # interpreter recursion limit. Without this, the RecursionError escapes
        # into the app-shell route and every page load 503s. Fail safe.
        _log.warning("Configured extension manifest is too deeply nested")
        manifest_status["status"] = "too_deeply_nested"
        _add_diagnostic_warning(diagnostics, "manifest_too_deeply_nested", "manifest")
    except (OSError, UnicodeDecodeError):
        _log.warning("Configured extension manifest could not be read")
        manifest_status["status"] = "unreadable"
        _add_diagnostic_warning(diagnostics, "manifest_unreadable", "manifest")
    return None, manifest_status


def _manifest_extension_state(
    manifest: object, disabled_ids: Set[str], diagnostics: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """Return sanitized per-extension state for manifest extension entries."""
    extension_entries: List[Dict[str, Any]] = []
    known_ids: Set[str] = set()
    manifest_disabled_ids: Set[str] = set()
    seen_ids: Set[str] = set()
    invalid_seen = False
    duplicate_seen = False
    for _source, _index, entry in _manifest_extension_entries(manifest):
        raw_id = _manifest_entry_text(entry, "id")
        if not _valid_extension_id(raw_id):
            invalid_seen = True
            continue
        ext_id = raw_id.strip()
        if ext_id in seen_ids:
            duplicate_seen = True
            continue
        seen_ids.add(ext_id)
        known_ids.add(ext_id)
        name = _manifest_entry_text(entry, "name")
        manifest_enabled = entry.get("enabled", True) is not False
        user_disabled = ext_id in disabled_ids
        can_toggle = manifest_enabled
        effective_enabled = manifest_enabled and not user_disabled
        if not manifest_enabled:
            manifest_disabled_ids.add(ext_id)
        extension_entries.append(
            {
                "id": ext_id,
                "name": name or ext_id,
                "manifest_enabled": manifest_enabled,
                "user_enabled": (not user_disabled) if can_toggle else False,
                "user_disabled": user_disabled,
                "effective_enabled": effective_enabled,
                "can_toggle": can_toggle,
                "reload_required": True,
                "status": (
                    "manifest_disabled"
                    if not manifest_enabled
                    else ("user_disabled" if user_disabled else "enabled")
                ),
            }
        )
    if invalid_seen:
        _add_diagnostic_warning(diagnostics, "manifest_extension_id_invalid", "manifest:extensions")
    if duplicate_seen:
        _add_diagnostic_warning(diagnostics, "manifest_extension_id_duplicate", "manifest:extensions")
    stale_ids = sorted(disabled_ids - known_ids)
    if stale_ids:
        _add_diagnostic_warning(diagnostics, "extension_state_unknown_ids", _EXTENSION_STATE_WARNING_SOURCE)
    return {
        "extensions": extension_entries,
        "known_ids": known_ids,
        "manifest_disabled_ids": manifest_disabled_ids,
    }


def _read_manifest_urls_with_diagnostics(
    root: Path,
    diagnostics: Optional[Dict[str, Any]] = None,
    disabled_ids: Optional[Set[str]] = None,
    manifest: Optional[object] = None,
    manifest_status: Optional[Dict[str, Any]] = None,
) -> Tuple[List[str], List[str], List[Dict[str, str]], Dict[str, Any]]:
    disabled_ids = disabled_ids or set()
    if manifest is None or manifest_status is None:
        manifest, manifest_status = _load_manifest_with_status(root, diagnostics)
    if manifest is None:
        return [], [], [], manifest_status

    scripts: List[str] = []
    stylesheets: List[str] = []
    sidecars: List[Dict[str, str]] = []
    asset_base = str(manifest_status.get("_asset_base", "") or "")
    entries = _iter_manifest_entries(manifest, disabled_ids=disabled_ids)
    manifest_status["entry_count"] = len(entries)
    scripts_full = False
    stylesheets_full = False
    for _source, entry in entries:
        if not isinstance(entry, dict):
            continue
        if _source.startswith("manifest.extensions["):
            sidecar = _sidecar_from_manifest_entry(entry, diagnostics)
            if sidecar is not None:
                if len(sidecars) < _MAX_URL_LIST:
                    sidecars.append(sidecar)
                else:
                    _add_diagnostic_warning(
                        diagnostics, "sidecar_list_truncated", _SIDECAR_WARNING_SOURCE
                    )
        script_source = "manifest:scripts"
        stylesheet_source = "manifest:stylesheets"
        if not scripts_full:
            for value in _entry_asset_values(entry, "scripts"):
                if not _append_safe_asset_url(
                    scripts,
                    _manifest_asset_url(value, asset_base),
                    script_source,
                    diagnostics=diagnostics,
                ):
                    scripts_full = True
                    break
        if not stylesheets_full:
            for value in _entry_asset_values(entry, "stylesheets"):
                if not _append_safe_asset_url(
                    stylesheets,
                    _manifest_asset_url(value, asset_base),
                    stylesheet_source,
                    diagnostics=diagnostics,
                ):
                    stylesheets_full = True
                    break
    manifest_status.update(
        {
            "loaded": True,
            "status": manifest_status.get("status") or "loaded",
            "script_count": len(scripts),
            "stylesheet_count": len(stylesheets),
            "sidecar_count": len(sidecars),
        }
    )
    return scripts, stylesheets, sidecars, manifest_status


def _read_manifest_urls(
    root: Path, disabled_ids: Optional[Set[str]] = None
) -> Tuple[List[str], List[str]]:
    scripts, stylesheets, _, _ = _read_manifest_urls_with_diagnostics(
        root, disabled_ids=disabled_ids
    )
    return scripts, stylesheets


def get_extension_config() -> Dict[str, Any]:
    """Return public extension config without exposing filesystem paths."""
    root = _extension_root()
    if root is None:
        return {"enabled": False, "script_urls": [], "stylesheet_urls": []}
    state = _load_extension_state()
    disabled_ids = set(state.get("disabled_extensions") or [])
    manifest_scripts, manifest_stylesheets = _read_manifest_urls(root, disabled_ids=disabled_ids)
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
    """Return sanitized extension diagnostics for administrators."""
    diagnostics = _new_diagnostics()
    root, dir_configured, dir_valid = _extension_root_status()
    state = _load_extension_state(diagnostics)
    disabled_ids = set(state.get("disabled_extensions") or [])
    manifest_configured = bool(os.getenv(_EXTENSION_MANIFEST_ENV, "").strip())
    manifest_status: Dict[str, Any] = {
        "configured": manifest_configured,
        "loaded": False,
        "status": "extension_disabled" if manifest_configured else "not_configured",
        "entry_count": 0,
        "script_count": 0,
        "stylesheet_count": 0,
        "sidecar_count": 0,
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
            "sidecars": [],
            "counts": {
                "script_urls": 0,
                "stylesheet_urls": 0,
                "sidecars": 0,
                "manifest_extensions": 0,
                "user_disabled": 0,
            },
            "manifest": manifest_status,
            "extensions": [],
            "warnings": diagnostics["warnings"],
        }

    manifest, manifest_status = _load_manifest_with_status(root, diagnostics)
    extension_state = _manifest_extension_state(manifest, disabled_ids, diagnostics) if manifest is not None else {
        "extensions": [],
        "known_ids": set(),
        "manifest_disabled_ids": set(),
    }
    manifest_scripts, manifest_stylesheets, sidecars, manifest_status = _read_manifest_urls_with_diagnostics(
        root,
        diagnostics,
        disabled_ids=disabled_ids,
        manifest=manifest,
        manifest_status=manifest_status,
    )
    extensions = extension_state["extensions"]
    known_ids = extension_state["known_ids"]
    user_disabled_count = len(disabled_ids & known_ids)
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
    public_manifest_status = {
        key: value for key, value in manifest_status.items() if not key.startswith("_")
    }
    return {
        "enabled": True,
        "extension_dir_configured": True,
        "extension_dir_valid": True,
        "script_urls": script_urls,
        "stylesheet_urls": stylesheet_urls,
        "sidecars": sidecars,
        "counts": {
            "script_urls": len(script_urls),
            "stylesheet_urls": len(stylesheet_urls),
            "sidecars": len(sidecars),
            "manifest_extensions": len(extensions),
            "user_disabled": user_disabled_count,
        },
        "manifest": public_manifest_status,
        "extensions": extensions,
        "gallery_installed": _load_install_manifest().get("installed", {}),
        "warnings": diagnostics["warnings"],
    }


def set_extension_user_enabled(extension_id: object, enabled: object) -> Dict[str, Any]:
    """Set the UI-managed enabled override for an installed manifest extension."""
    if not _valid_extension_id(extension_id):
        raise ExtensionToggleError("Invalid extension id", status=400)
    ext_id = str(extension_id).strip()
    if not isinstance(enabled, bool):
        raise ExtensionToggleError("enabled must be a boolean", status=400)
    root = _extension_root()
    if root is None:
        raise ExtensionToggleError("Extensions are not configured", status=404)
    with _EXTENSION_STATE_LOCK:
        diagnostics = _new_diagnostics()
        state = _load_extension_state(diagnostics)
        disabled_ids = set(state.get("disabled_extensions") or [])
        manifest, manifest_status = _load_manifest_with_status(root, diagnostics)
        if manifest is None or not manifest_status.get("loaded", False):
            raise ExtensionToggleError("Extension manifest is not loaded", status=409)
        extension_state = _manifest_extension_state(manifest, disabled_ids, diagnostics)
        known_ids: Set[str] = extension_state["known_ids"]
        manifest_disabled_ids: Set[str] = extension_state["manifest_disabled_ids"]
        if ext_id not in known_ids:
            raise ExtensionToggleError("Extension not found", status=404)
        if ext_id in manifest_disabled_ids:
            raise ExtensionToggleError("Extension is disabled by its manifest", status=409)
        if enabled:
            disabled_ids.discard(ext_id)
        else:
            disabled_ids.add(ext_id)
        _write_extension_state({"disabled_extensions": sorted(disabled_ids)})
    # Return a fresh status snapshot after the atomic write is visible. Keeping
    # the readback outside the lock avoids doing the full manifest/status parse
    # while blocking other toggles; a concurrent toggle may be reflected too,
    # which is fine because the UI re-renders from the current effective state.
    return get_extension_status()


def _install_manifest_file() -> Path:
    return _extension_state_dir() / _GALLERY_INSTALL_STATE_FILENAME


def _empty_install_manifest() -> Dict[str, Any]:
    return {"version": 1, "installed": {}}


def _load_install_manifest() -> Dict[str, Any]:
    """Load gallery install manifest, failing safe on any error."""
    mfile = _install_manifest_file()
    try:
        if not mfile.exists() or not mfile.is_file():
            return _empty_install_manifest()
        with mfile.open("rb") as fh:
            raw = fh.read(_MAX_INSTALL_MANIFEST_BYTES + 1)
        if len(raw) > _MAX_INSTALL_MANIFEST_BYTES:
            return _empty_install_manifest()
        parsed = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        return _empty_install_manifest()
    if not isinstance(parsed, dict) or not isinstance(parsed.get("installed"), dict):
        return _empty_install_manifest()
    installed: Dict[str, Any] = {}
    for ext_id, entry in parsed["installed"].items():
        if not _valid_extension_id(ext_id):
            continue
        if not isinstance(entry, dict):
            continue
        files = entry.get("files", [])
        if not isinstance(files, list):
            continue
        installed[ext_id] = {
            "version": str(entry.get("version", "unknown")),
            "files": [f for f in files if isinstance(f, str)],
            "installed_at": str(entry.get("installed_at", "")),
        }
        if len(installed) >= _MAX_GALLERY_INSTALLED_IDS:
            break
    return {"version": 1, "installed": installed}


def _write_install_manifest(manifest: Dict[str, Any]) -> None:
    """Persist install manifest with atomic same-directory replace."""
    target = _install_manifest_file()
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    data = json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")
    try:
        with tmp.open("wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, target)
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass


def install_extension(id: object, download_url: object, sha256: object) -> Dict[str, Any]:
    """Download, verify, and extract a gallery extension."""
    if not _valid_extension_id(id):
        raise ExtensionInstallError("Invalid extension id")
    ext_id = str(id).strip()
    if not isinstance(download_url, str) or not download_url.startswith("https://"):
        raise ExtensionInstallError("Invalid download URL")
    parsed_url = urlsplit(download_url)
    if parsed_url.hostname not in _REGISTRY_ALLOWED_DOWNLOAD_HOSTS:
        raise ExtensionInstallError("Invalid download URL")
    if not isinstance(sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", sha256):
        raise ExtensionInstallError("Invalid sha256")
    root = _extension_root()
    if root is None:
        raise ExtensionInstallError("Extensions not configured", 404)
    try:
        raw_data = _safe_download(download_url, _MAX_ZIP_DOWNLOAD_BYTES)
    except ExtensionInstallError:
        raise
    except Exception as exc:
        raise ExtensionInstallError("Download failed", 502) from exc
    if len(raw_data) > _MAX_ZIP_DOWNLOAD_BYTES:
        raise ExtensionInstallError("Download too large")
    if hashlib.sha256(raw_data).hexdigest() != sha256:
        raise ExtensionInstallError("SHA-256 mismatch")
    try:
        zf = zipfile.ZipFile(io.BytesIO(raw_data))
    except zipfile.BadZipFile as exc:
        raise ExtensionInstallError("Invalid zip archive") from exc
    ext_dir = root / ext_id
    member_names = zf.namelist()
    total_uncompressed = sum(info.file_size for info in zf.infolist() if not info.is_dir())
    if total_uncompressed > _MAX_ZIP_DOWNLOAD_BYTES * 10:
        raise ExtensionInstallError("Archive uncompressed size exceeds limit")
    file_members = [n for n in member_names if n and not n.endswith("/")]
    if len(file_members) > 1024:
        raise ExtensionInstallError("Archive contains too many files")
    # Detect and strip a single top-level directory prefix matching the extension id.
    # Registry artifacts root files under <id>/ (e.g. desktop-companion/manifest.json).
    strip_prefix = ""
    candidate = ext_id + "/"
    if all(n.startswith(candidate) for n in file_members):
        strip_prefix = candidate
    def _stripped(name: str) -> str:
        if strip_prefix and name.startswith(strip_prefix):
            return name[len(strip_prefix):]
        return name
    root_resolved = root.resolve()
    ext_dir_resolved = ext_dir.resolve()
    for member_name in file_members:
        decoded = _fully_unquote_path(_stripped(member_name))
        if not decoded or not _is_safe_relative_path(decoded):
            raise ExtensionInstallError("Unsafe archive member")
        resolved = (ext_dir / decoded).resolve()
        try:
            resolved.relative_to(root_resolved)
        except ValueError as exc:
            raise ExtensionInstallError("Zip-slip detected") from exc
        try:
            resolved.relative_to(ext_dir_resolved)
        except ValueError as exc:
            raise ExtensionInstallError("Zip-slip detected") from exc
    # Determine version from extension.json or manifest.json in zip
    version = "unknown"
    for vfile in ("extension.json", "manifest.json"):
        candidate_name = strip_prefix + vfile if strip_prefix else vfile
        if candidate_name in member_names:
            try:
                mdata = json.loads(zf.read(candidate_name).decode("utf-8"))
                if isinstance(mdata, dict) and isinstance(mdata.get("version"), str):
                    version = mdata["version"]
                    break
            except Exception:
                pass
    with _EXTENSION_STATE_LOCK:
        ext_dir.mkdir(parents=True, exist_ok=True)
        if ext_dir.is_symlink():
            raise ExtensionInstallError("Extension directory is a symlink", 400)
        rollback: List[Path] = []
        try:
            for member_name in file_members:
                decoded = _fully_unquote_path(_stripped(member_name))
                dest = (ext_dir / decoded).resolve()
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(zf.read(member_name))
                rollback.append(dest)
        except Exception as exc:
            for path in rollback:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
            try:
                if ext_dir.exists() and not any(ext_dir.iterdir()):
                    ext_dir.rmdir()
            except OSError:
                pass
            raise ExtensionInstallError("Extraction failed", 500) from exc
        try:
            manifest = _load_install_manifest()
            from datetime import datetime, timezone
            rel_files = [p.relative_to(ext_dir_resolved).as_posix() for p in rollback]
            manifest["installed"][ext_id] = {
                "version": version,
                "files": rel_files,
                "installed_at": datetime.now(timezone.utc).isoformat(),
            }
            encoded = json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")
            if len(encoded) > _MAX_INSTALL_MANIFEST_BYTES:
                raise ExtensionInstallError("Install manifest would exceed size limit")
            _write_install_manifest(manifest)
        except ExtensionInstallError:
            for path in rollback:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
            try:
                if ext_dir.exists() and not any(ext_dir.iterdir()):
                    ext_dir.rmdir()
            except OSError:
                pass
            raise
        except Exception as exc:
            for path in rollback:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
            try:
                if ext_dir.exists() and not any(ext_dir.iterdir()):
                    ext_dir.rmdir()
            except OSError:
                pass
            raise ExtensionInstallError("Failed to record install", 500) from exc
    return {"installed": True, "id": ext_id, "version": version}


def uninstall_extension(id: object) -> Dict[str, Any]:
    """Remove a gallery-installed extension's files and manifest entry."""
    if not _valid_extension_id(id):
        raise ExtensionInstallError("Invalid extension id")
    ext_id = str(id).strip()
    root = _extension_root()
    if root is None:
        raise ExtensionInstallError("Extensions not configured", 404)
    with _EXTENSION_STATE_LOCK:
        manifest = _load_install_manifest()
        entry = manifest["installed"].get(ext_id)
        if entry is None:
            raise ExtensionInstallError("Extension not installed", 404)
        ext_dir = root / ext_id
        for rel_path in entry.get("files", []):
            if not _is_safe_relative_path(rel_path):
                continue
            target = (ext_dir / rel_path).resolve()
            try:
                target.relative_to(ext_dir.resolve())
            except ValueError:
                continue
            try:
                target.unlink(missing_ok=True)
            except OSError:
                pass
        # Remove empty directories bottom-up
        if ext_dir.exists():
            for dirpath in sorted(
                (d for d in ext_dir.rglob("*") if d.is_dir()),
                key=lambda p: len(p.parts),
                reverse=True,
            ):
                try:
                    if not any(dirpath.iterdir()):
                        dirpath.rmdir()
                except OSError:
                    pass
            try:
                if not any(ext_dir.iterdir()):
                    ext_dir.rmdir()
            except OSError:
                pass
        del manifest["installed"][ext_id]
        _write_install_manifest(manifest)
    return {"uninstalled": True, "id": ext_id}


def get_extension_registry() -> Dict[str, Any]:
    """Fetch the extension registry with a 5-minute TTL cache."""
    with _REGISTRY_LOCK:
        now = time.monotonic()
        cached = _REGISTRY_CACHE.get("data")
        cached_at = _REGISTRY_CACHE.get("fetched_at", 0.0)
        if cached is not None and (now - cached_at) < _REGISTRY_TTL_SECONDS:
            return {"entries": cached}
        try:
            raw = urlopen(_REGISTRY_URL, timeout=10).read(2 * 1024 * 1024)
            data = json.loads(raw.decode("utf-8"))
            if isinstance(data, list):
                entries = data
            elif isinstance(data, dict):
                entries = data.get("extensions") or data.get("entries") or []
            else:
                entries = []
            if not isinstance(entries, list):
                entries = []
            _REGISTRY_CACHE["data"] = entries
            _REGISTRY_CACHE["fetched_at"] = now
            return {"entries": entries}
        except Exception:
            return {"entries": [], "error": "registry_unavailable"}


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
