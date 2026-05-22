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
import importlib
import json
import logging
import os
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

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

        tab = manifest.get("tab", {})
        tab_path = tab.get("path", f"/{name}")

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
    Serve a static file from a plugin's dist/ directory.

    Returns (file_bytes, content_type) on success, None on not found.
    """
    root = _PLUGIN_STATIC_ROOTS.get(plugin_name)
    if not root:
        return None

    safe = (root / rel_path.lstrip("/")).resolve()
    try:
        safe.relative_to(root)
    except ValueError:
        return None  # path traversal attempt

    if not safe.is_file():
        return None

    data = safe.read_bytes()
    ext = os.path.splitext(rel_path.lower())[1]
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
