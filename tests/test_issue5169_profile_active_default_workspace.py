"""Regression test for #5169 — GET /api/profile/active must return a
profile-scoped ``default_workspace``.

Bug: a blank new-chat page showed the wrong workspace (or "No workspace") for a
named profile on cold load. Boot only ever sourced ``S._profileDefaultWorkspace``
from GET /api/settings, which returns the GLOBAL default — never the active
profile's configured workspace. The composer workspace chip therefore diverged
from the workspace a new chat would actually inherit for that profile.

Fix: GET /api/profile/active now includes ``default_workspace``, resolved with
the SAME profile-scoped priority used by POST /api/profile/switch — reusing
``api.workspace.get_last_workspace()`` (which is already keyed off the
per-request hermes_profile cookie via the thread-local):
    {profile_home}/webui_state/last_workspace.txt
      -> config.yaml workspace / default_workspace
      -> terminal.cwd
      -> process default

These tests cover both the wiring (the endpoint surfaces the resolver's value)
and the real filesystem resolution from a named profile's last_workspace.txt.
"""

from __future__ import annotations

from types import SimpleNamespace
from urllib.parse import urlparse

import api.profiles as profiles
import api.routes as routes
import api.workspace as workspace
import api.config as config_mod


def _capture_j(monkeypatch):
    """Patch routes.j to capture the JSON payload instead of writing a response."""
    captured = {}

    def fake_j(_handler, payload, status=200, **_kwargs):
        captured["status"] = status
        captured["payload"] = payload
        return captured

    monkeypatch.setattr(routes, "j", fake_j)
    return captured


def test_profile_active_includes_default_workspace_from_resolver(monkeypatch):
    """The endpoint must surface whatever the profile-scoped resolver returns.

    This pins the wiring: ``default_workspace`` is populated from
    ``api.workspace.get_last_workspace()`` (imported into routes at module level),
    the very helper POST /api/profile/switch resolution is built on — not from a
    duplicated, divergent code path.
    """
    captured = _capture_j(monkeypatch)
    monkeypatch.setattr(profiles, "get_active_profile_name", lambda: "work")
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: "/home/u/.hermes/profiles/work")
    monkeypatch.setattr(profiles, "_is_root_profile", lambda name: name in ("default", ""))
    # The resolver is the single source of truth for the workspace value.
    monkeypatch.setattr(routes, "get_profile_default_workspace", lambda: "/srv/projects/work")

    routes.handle_get(SimpleNamespace(), urlparse("/api/profile/active"))

    assert captured["status"] == 200
    payload = captured["payload"]
    assert payload["name"] == "work"
    assert payload["is_default"] is False
    assert payload["default_workspace"] == "/srv/projects/work", (
        "GET /api/profile/active must surface the profile-scoped workspace from "
        "get_profile_default_workspace() so the blank new-chat composer chip shows it (#5169)"
    )


def test_profile_active_default_workspace_resolves_from_named_profile(monkeypatch, tmp_path):
    """End-to-end: a named profile's last_workspace.txt drives default_workspace.

    Mirrors the real boot path — the per-request hermes_profile cookie sets the
    thread-local profile (set_request_profile), so get_last_workspace() reads the
    target profile's {home}/webui_state/last_workspace.txt rather than the global
    default.
    """
    # ── Single-user layout: base ~/.hermes with a named 'work' profile ──
    base_home = tmp_path / ".hermes"
    profile_home = base_home / "profiles" / "work"
    for subdir in ("memories", "sessions", "skills", "webui_state"):
        (profile_home / subdir).mkdir(parents=True, exist_ok=True)

    # The profile's previously-chosen workspace (top resolution priority).
    workspace_dir = tmp_path / "code" / "work-project"
    workspace_dir.mkdir(parents=True, exist_ok=True)
    resolved_ws = str(workspace_dir.resolve())
    (profile_home / "webui_state" / "last_workspace.txt").write_text(
        resolved_ws, encoding="utf-8"
    )

    captured = _capture_j(monkeypatch)

    # Point the profile machinery at our temp base home and force normal
    # multi-profile mode (no isolated opt-in) so the named profile resolves
    # to {base}/profiles/work.
    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", base_home)
    monkeypatch.delenv("HERMES_WEBUI_ISOLATED_PROFILE", raising=False)
    monkeypatch.setattr(profiles, "_INITIAL_ISOLATED_PROFILE_OPT_IN", "")
    # Defensive: keep resolution hermetic w.r.t. the test runner's real config —
    # a remote terminal backend would otherwise reject the local last_workspace.
    monkeypatch.setattr(workspace, "_remote_terminal_cwd", lambda: None)

    # Simulate the request carrying a hermes_profile=work cookie.
    profiles.set_request_profile("work")
    try:
        routes.handle_get(SimpleNamespace(), urlparse("/api/profile/active"))
    finally:
        profiles.clear_request_profile()

    payload = captured["payload"]
    assert payload["name"] == "work"
    assert payload["is_default"] is False
    assert payload["default_workspace"] == resolved_ws, (
        "default_workspace must resolve from the active profile's "
        "webui_state/last_workspace.txt, not the global/default workspace (#5169)"
    )


def test_profile_active_default_workspace_falls_back_to_config_workspace(monkeypatch, tmp_path):
    """With no last_workspace.txt, config.yaml `workspace` drives the value.

    This exercises the second tier of the shared resolution priority used by
    POST /api/profile/switch.
    """
    base_home = tmp_path / ".hermes"
    profile_home = base_home / "profiles" / "work"
    (profile_home / "webui_state").mkdir(parents=True, exist_ok=True)

    cfg_workspace = tmp_path / "cfg-workspace"
    cfg_workspace.mkdir(parents=True, exist_ok=True)
    resolved_cfg_ws = str(cfg_workspace.resolve())

    captured = _capture_j(monkeypatch)
    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", base_home)
    monkeypatch.delenv("HERMES_WEBUI_ISOLATED_PROFILE", raising=False)
    monkeypatch.setattr(profiles, "_INITIAL_ISOLATED_PROFILE_OPT_IN", "")
    monkeypatch.setattr(workspace, "_remote_terminal_cwd", lambda: None)
    # Neutralize the global last_workspace.txt fallback so this test exercises the
    # config.yaml tier deterministically regardless of the runner's real state dir.
    monkeypatch.setattr(workspace, "_GLOBAL_LW_FILE", tmp_path / "nonexistent-global-lw.txt")
    # No last_workspace.txt on disk -> resolver consults the profile config.yaml.
    # _profile_default_workspace() does `from api.config import get_config`, so the
    # patch must land on api.config (the lookup happens at call time).
    monkeypatch.setattr(config_mod, "get_config", lambda: {"workspace": resolved_cfg_ws})

    profiles.set_request_profile("work")
    try:
        routes.handle_get(SimpleNamespace(), urlparse("/api/profile/active"))
    finally:
        profiles.clear_request_profile()

    payload = captured["payload"]
    assert payload["default_workspace"] == resolved_cfg_ws, (
        "default_workspace must fall back to config.yaml `workspace` when no "
        "last_workspace.txt exists (mirrors POST /api/profile/switch priority, #5169)"
    )


def test_profile_active_default_workspace_ignores_global_last_workspace(monkeypatch, tmp_path):
    """The #5169 regression Codex flagged: a GLOBAL last_workspace.txt must NOT
    leak into a named profile's default_workspace.

    get_last_workspace() falls back to the global _GLOBAL_LW_FILE before reaching
    the profile config.yaml — so a named profile with NO own last_workspace.txt
    but WITH a config.yaml `workspace` would have returned the global path,
    re-introducing exactly the wrong-workspace bug. /api/profile/active now uses
    get_profile_default_workspace(), which skips the global file: profile-scoped
    last_workspace.txt -> config.yaml -> terminal.cwd -> default.
    """
    base_home = tmp_path / ".hermes"
    profile_home = base_home / "profiles" / "work"
    (profile_home / "webui_state").mkdir(parents=True, exist_ok=True)

    # A GLOBAL last_workspace.txt pointing somewhere the named profile must NOT use.
    global_ws = tmp_path / "global-workspace"
    global_ws.mkdir(parents=True, exist_ok=True)
    global_lw = tmp_path / "global-last_workspace.txt"
    global_lw.write_text(str(global_ws.resolve()), encoding="utf-8")

    # The named profile's OWN configured workspace (config.yaml), which must win.
    cfg_workspace = tmp_path / "work-config-workspace"
    cfg_workspace.mkdir(parents=True, exist_ok=True)
    resolved_cfg_ws = str(cfg_workspace.resolve())

    captured = _capture_j(monkeypatch)
    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", base_home)
    monkeypatch.delenv("HERMES_WEBUI_ISOLATED_PROFILE", raising=False)
    monkeypatch.setattr(profiles, "_INITIAL_ISOLATED_PROFILE_OPT_IN", "")
    monkeypatch.setattr(workspace, "_remote_terminal_cwd", lambda: None)
    # The global last-workspace file DOES exist (and is valid) — the named-profile
    # resolver must still ignore it.
    monkeypatch.setattr(workspace, "_GLOBAL_LW_FILE", global_lw)
    # No profile-scoped last_workspace.txt on disk -> resolver consults config.yaml.
    monkeypatch.setattr(config_mod, "get_config", lambda: {"workspace": resolved_cfg_ws})

    profiles.set_request_profile("work")
    try:
        routes.handle_get(SimpleNamespace(), urlparse("/api/profile/active"))
    finally:
        profiles.clear_request_profile()

    payload = captured["payload"]
    assert payload["default_workspace"] == resolved_cfg_ws, (
        "default_workspace must resolve from the named profile's config.yaml workspace, "
        "NOT the global last_workspace.txt — the #5169 regression guard"
    )
    assert payload["default_workspace"] != str(global_ws.resolve()), (
        "the global last-workspace path must never leak into a named profile's default_workspace"
    )
