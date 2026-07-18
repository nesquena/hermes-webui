"""Hermes-agent plugin lifecycle actions (install/update/remove) for the
WebUI's Settings -> Plugins panel.

Delegates to hermes_cli.plugins_cmd's ``dashboard_*`` functions in
hermes-agent -- the SAME functions ``hermes plugins install/update/remove``
and the native dashboard use -- so installs/updates/removals go through one
implementation of git-clone / path-sanitization / bundled-plugin protection,
not a second copy in the WebUI. hermes_cli is an optional runtime dependency
here (see api/commands.py's MoA resolver for the same graceful-degrade
pattern); callers get a clear, catchable error when it isn't installed.

HIGHEST RISK surface in the WebUI Settings area: installing a plugin clones
and imports arbitrary Python from a Git repository into the running Hermes
agent process. Every write action here MUST be gated by the caller
(HERMES_WEBUI_ALLOW_PLUGIN_WRITE, checked in api/routes.py) -- this module
does not gate on its own, matching api/ops_actions.py's separation of
"mechanism" (this module) from "policy" (the route's env check).
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


class PluginLifecycleUnavailable(RuntimeError):
    """hermes-agent's plugins_cmd module isn't importable in this environment."""


def _plugins_cmd():
    try:
        from hermes_cli import plugins_cmd
    except ImportError as exc:
        raise PluginLifecycleUnavailable(
            "Plugin management requires hermes-agent (hermes_cli.plugins_cmd), "
            "which isn't installed in this environment."
        ) from exc
    return plugins_cmd


def list_installed_plugins() -> dict:
    """Every plugin the loader can see, annotated for lifecycle actions.

    Reads the same disk-scan (``_discover_all_plugins``) that
    ``dashboard_remove_user_plugin``/``dashboard_update_user_plugin``
    themselves consult to decide what's removable/updatable, so this list can
    never advertise an action the write endpoint would then reject.
    """
    try:
        cmd = _plugins_cmd()
    except PluginLifecycleUnavailable as exc:
        return {"plugins": [], "unavailable": True, "error": str(exc)}

    try:
        entries = cmd._discover_all_plugins()
    except Exception:
        logger.exception("Plugin discovery failed")
        return {"plugins": [], "unavailable": True, "error": "Plugin discovery failed."}

    plugins = []
    for name, version, description, source, _path, key in entries:
        source = str(source or "")
        plugins.append({
            "name": str(name or ""),
            "key": str(key or name or ""),
            "version": str(version or ""),
            "description": str(description or ""),
            # bundled | user | git | entrypoint -- "git" is a user install
            # that also has a .git dir (see plugins_cmd._scan_level), the
            # only source `dashboard_update_user_plugin` can `git pull`.
            "source": source,
            "removable": source in ("user", "git"),
            "updatable": source == "git",
        })
    plugins.sort(key=lambda p: p["key"].lower())
    return {"plugins": plugins, "unavailable": False}


def resolve_plugin_source(identifier: str) -> dict:
    """Preview what an install would clone from -- no network/filesystem writes.

    Pure URL parsing (``hermes_cli.plugins_cmd._resolve_git_url``), so it's
    safe to expose even while the write gate is closed: an operator can
    sanity-check a source (catch a typo'd owner/repo, an unexpected redirect
    target, an insecure scheme) before ever flipping
    HERMES_WEBUI_ALLOW_PLUGIN_WRITE on. The plugin's manifest itself is only
    knowable after cloning, so the install confirmation dialog can show this
    resolved source up front but shows the manifest (name/warnings/missing
    env) only in the post-install result.
    """
    identifier = str(identifier or "").strip()
    if not identifier:
        raise ValueError("identifier is required")
    cmd = _plugins_cmd()
    git_url, subdir = cmd._resolve_git_url(identifier)
    return {
        "identifier": identifier,
        "git_url": git_url,
        "subdir": subdir or "",
        "insecure_scheme": git_url.startswith(("http://", "file://")),
    }


def install_plugin(identifier: str, *, force: bool = False, enable: bool = True) -> dict:
    identifier = str(identifier or "").strip()
    if not identifier:
        raise ValueError("identifier is required")
    cmd = _plugins_cmd()
    return cmd.dashboard_install_plugin(identifier, force=bool(force), enable=bool(enable))


def update_plugin(name: str) -> dict:
    name = str(name or "").strip()
    if not name:
        raise ValueError("name is required")
    cmd = _plugins_cmd()
    return cmd.dashboard_update_user_plugin(name)


def remove_plugin(name: str) -> dict:
    name = str(name or "").strip()
    if not name:
        raise ValueError("name is required")
    cmd = _plugins_cmd()
    return cmd.dashboard_remove_user_plugin(name)
