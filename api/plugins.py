"""
Plugin discovery and static serving for Hermes Web UI.

Scans ~/.hermes/plugins/<name>/dashboard/ for manifest.json files,
matching the official Hermes dashboard plugin format.

Each plugin may have:
  dashboard/
    manifest.json   -- tab definition and entry point
    dist/
      index.js      -- plugin JS bundle (IIFE)
      style.css     -- optional plugin stylesheet
    plugin_api.py   -- optional backend API (not used in WebUI MVP)
"""
import json
import logging
import os
import re
from pathlib import Path

logger = logging.getLogger(__name__)

# Valid dashboard-plugin name: a safe slug (it becomes a URL path component and
# a settings key). Lowercase alnum + - / _, 1-64 chars, must start with a letter.
_VALID_PLUGIN_NAME = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")

# Valid tab.path: a clean same-origin absolute path. Must start with a single
# '/' (NOT '//' — a leading '//' is a protocol-relative URL that would resolve
# to a remote origin when assigned to iframe.src), then only safe path chars —
# no quotes, whitespace, control chars, query ('?') or fragment ('#').
_VALID_PLUGIN_TAB_PATH = re.compile(r"^/(?!/)[A-Za-z0-9._~/-]{0,255}$")

# plugin_name -> manifest dict (as loaded from manifest.json)
PLUGIN_MANIFESTS: dict[str, dict] = {}

# plugin_name -> resolved static root dir
_PLUGIN_STATIC_ROOTS: dict[str, Path] = {}


def _get_plugin_base() -> Path:
    return Path(os.environ.get("HERMES_WEBUI_PLUGINS_DIR", str(Path.home() / ".hermes" / "plugins")))


def load_plugins() -> None:
    """Scan plugin directories and load manifest.json for each dashboard plugin."""
    plugin_base = _get_plugin_base()
    if not plugin_base.is_dir():
        logger.debug("No plugins directory at %s", plugin_base)
        return

    for entry in sorted(plugin_base.iterdir()):
        if not entry.is_dir():
            continue
        manifest_path = entry / "dashboard" / "manifest.json"
        if not manifest_path.is_file():
            continue

        try:
            manifest = json.loads(manifest_path.read_text())
        except Exception:
            logger.exception("Failed to parse manifest for plugin %s", entry.name)
            continue

        name = manifest.get("name") or entry.name

        # Validate the plugin name: it becomes a URL path component
        # (/dashboard-plugins/<name>/...) and a settings key. Restrict to a safe
        # slug so a manifest like name:"../foo" can't make the URL-space ambiguous.
        if not _VALID_PLUGIN_NAME.match(str(name)):
            logger.warning("Skipping plugin with invalid name %r (must match %s)", name, _VALID_PLUGIN_NAME.pattern)
            continue

        tab = manifest.get("tab", {})
        tab_path = tab.get("path", f"/{name}")

        # Validate tab.path: it's a same-origin route the plugin page is served
        # at AND a value passed into client-side navigation. Require a clean
        # absolute path — no quotes/control chars/query/fragment — so a hostile
        # manifest can't shadow odd routes or inject via the path.
        if not _VALID_PLUGIN_TAB_PATH.match(str(tab_path)):
            logger.warning("Skipping plugin %s with invalid tab.path %r (must match %s)", name, tab_path, _VALID_PLUGIN_TAB_PATH.pattern)
            continue

        if name in PLUGIN_MANIFESTS:
            logger.warning("Duplicate plugin name skipped: %s (already loaded)", name)
            continue
        if tab_path in (m.get("tab", {}).get("path") for m in PLUGIN_MANIFESTS.values()):
            logger.warning("Plugin %s tab.path %r conflicts with another plugin; skipped", name, tab_path)
            continue

        PLUGIN_MANIFESTS[name] = manifest
        logger.info("Loaded dashboard plugin: %s (label=%s)", name, manifest.get("label", ""))

        # Pre-compute static root for fast serving (points to dashboard/)
        dashboard_dir = entry / "dashboard"
        if dashboard_dir.is_dir():
            _PLUGIN_STATIC_ROOTS[name] = dashboard_dir.resolve()


def serve_plugin_static(plugin_name: str, rel_path: str) -> tuple[bytes, str] | None:
    """
    Serve a built static asset from a plugin's dashboard/dist/ (or static/) dir.

    Returns (file_bytes, content_type) on success, None on not found.

    Security: _PLUGIN_STATIC_ROOTS points at the plugin's whole dashboard/ dir
    (the page route needs that), but the asset route must NOT expose plugin
    source/config — e.g. dashboard/plugin_api.py, manifest.json, .env. So we
    constrain served files to the built-asset subtrees (dist/ or static/), reject
    dotfiles, and require a known static extension.
    """
    root = _PLUGIN_STATIC_ROOTS.get(plugin_name)
    if not root:
        return None

    safe = (root / rel_path.lstrip("/")).resolve()
    try:
        safe.relative_to(root)
    except ValueError:
        return None  # path traversal attempt

    # Only built-asset subtrees are servable (not the dashboard root itself,
    # which holds plugin_api.py / manifest.json / config).
    rel = safe.relative_to(root)
    if not rel.parts or rel.parts[0] not in ("dist", "static"):
        return None
    # No dotfiles (.env, .git, etc.) anywhere in the path.
    if any(part.startswith(".") for part in rel.parts):
        return None

    if not safe.is_file():
        return None

    # Allowlist of static asset extensions — refuse source/config (.py, .json,
    # .toml, .env, .sh, ...) even if somehow placed under dist/.
    ext = os.path.splitext(rel_path.lower())[1]
    _STATIC_EXTS = {
        ".js", ".css", ".html", ".png", ".jpg", ".jpeg", ".gif", ".svg",
        ".ico", ".webp", ".woff", ".woff2", ".ttf", ".otf", ".map", ".txt",
    }
    if ext not in _STATIC_EXTS:
        return None

    data = safe.read_bytes()
    content_type = {
        ".js": "application/javascript; charset=utf-8",
        ".css": "text/css; charset=utf-8",
        ".html": "text/html; charset=utf-8",
        ".json": "application/json; charset=utf-8",
        ".png": "image/png",
        ".svg": "image/svg+xml",
        ".ico": "image/x-icon",
    }.get(ext, "application/octet-stream")

    return data, content_type


def get_plugin_metadata() -> list[dict]:
    """
    Return a list of plugin metadata suitable for the Settings → Plugins tab.
    Each entry includes name, key, version, description, and tab info for linking.

    Per-plugin enabled state is stored in settings.json under `dashboard_plugins`.
    A plugin is enabled only if the user has explicitly toggled it on (default off).
    """
    from api.config import load_settings

    plugin_settings = load_settings().get("dashboard_plugins", {})
    plugins = []
    for name, manifest in sorted(PLUGIN_MANIFESTS.items()):
        tab = manifest.get("tab", {})
        path = tab.get("path", f"/{name}")
        plugins.append({
            "name": manifest.get("label") or manifest.get("name") or name,
            "key": name,
            "version": manifest.get("version", "0.0.0"),
            "description": manifest.get("description", ""),
            "tab": {
                "path": path,
                "label": tab.get("label") or manifest.get("label") or name,
            },
            "enabled": bool(plugin_settings.get(name, False)),
            "hooks": [],
        })
    return plugins


# ── Dashboard plugin backends (plugin_api.py) ──────────────────────────────
# A dashboard plugin may declare an optional Python backend via manifest "api"
# (e.g. "plugin_api.py") that exposes a FastAPI ``APIRouter`` named ``router``.
# The WebUI serves its GET routes (read-only) under /api/plugins/<name>/<route>
# so the plugin's built frontend can fetch its own data. Without this, such a
# dashboard loads its assets but every data fetch 404s (blank panel).
#
# Requires FastAPI to be importable so the plugin's ``@router.get(...)`` routes
# are registered; if it is not installed, plugin backends are skipped gracefully
# (the panel still loads, just without live data).

# plugin_name -> imported plugin_api module (or None if absent/failed)
_PLUGIN_API_MODULES: dict[str, object] = {}


def _load_plugin_api(plugin_name: str):
    if plugin_name in _PLUGIN_API_MODULES:
        return _PLUGIN_API_MODULES[plugin_name]
    module = None
    manifest = PLUGIN_MANIFESTS.get(plugin_name)
    root = _PLUGIN_STATIC_ROOTS.get(plugin_name)
    api_file = (manifest or {}).get("api")
    if manifest and root and api_file:
        try:
            api_path = (root / api_file).resolve()
            api_path.relative_to(root)  # reject traversal outside dashboard/
            if api_path.is_file():
                import importlib.util
                import sys
                spec = importlib.util.spec_from_file_location(f"_hermes_plugin_api_{plugin_name}", api_path)
                module = importlib.util.module_from_spec(spec)
                sys.modules[spec.name] = module
                spec.loader.exec_module(module)
        except Exception:
            logger.exception("Failed to import plugin_api for plugin %s", plugin_name)
            module = None
    _PLUGIN_API_MODULES[plugin_name] = module
    return module


def _coerce_endpoint_kwargs(endpoint, query: dict) -> dict:
    """Build call kwargs for a router endpoint from the query string.

    Coerces present query values to the parameter's annotation, and unwraps a
    FastAPI ``Query(...)``/``Param(...)`` default object to its plain default for
    absent params (so the endpoint isn't called with the marker object).
    """
    import inspect
    kwargs: dict = {}
    for name, p in inspect.signature(endpoint).parameters.items():
        if name in query and query[name]:
            raw = query[name][0]
            # annotation may be a real type or a string (PEP 563 / from __future__ import annotations)
            ann = p.annotation
            ann_name = ann if isinstance(ann, str) else getattr(ann, "__name__", ann)
            try:
                if ann_name == "int":
                    kwargs[name] = int(raw)
                elif ann_name == "float":
                    kwargs[name] = float(raw)
                elif ann_name == "bool":
                    kwargs[name] = raw.strip().lower() in ("1", "true", "yes", "on")
                else:
                    kwargs[name] = raw
            except (ValueError, TypeError):
                pass  # uncoercible → fall back to the endpoint's own default
        else:
            d = p.default
            if d is not inspect.Parameter.empty and type(d).__module__.split(".")[0] == "fastapi":
                inner = getattr(d, "default", inspect.Parameter.empty)
                if inner is not inspect.Parameter.empty and inner is not Ellipsis:
                    kwargs[name] = inner
    return kwargs


def dispatch_plugin_api(plugin_name: str, sub_path: str, query: dict):
    """Dispatch a GET request to a dashboard plugin's plugin_api router.

    Returns (status_code, payload) on a matched route, or None if there is no
    such plugin/route (caller should 404). Read-only: only GET routes served.
    """
    if not _VALID_PLUGIN_NAME.match(plugin_name or ""):
        return None
    module = _load_plugin_api(plugin_name)
    if module is None:
        return None
    router = getattr(module, "router", None)
    routes = getattr(router, "routes", None) or []
    want = "/" + (sub_path or "").strip("/")
    for route in routes:
        if getattr(route, "path", None) != want:
            continue
        if "GET" not in (getattr(route, "methods", None) or set()):
            continue
        endpoint = getattr(route, "endpoint", None)
        if endpoint is None:
            continue
        try:
            return (200, endpoint(**_coerce_endpoint_kwargs(endpoint, query)))
        except Exception:
            logger.exception("plugin_api %s %s raised", plugin_name, want)
            return (500, {"error": "plugin backend error"})
    return None
