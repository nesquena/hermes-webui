"""Re-gate round 6 (#7168): the logical "default" name must honor isolated-mode clamping.

Maintainer round-6 re-gate CORE (one remaining instance of the isolation
class): ``_resolve_profile_home_param("default")`` short-circuited to
``_DEFAULT_HERMES_HOME`` BEFORE delegating to
``api.profiles.get_hermes_home_for_profile()``, so it never reached the
isolated-mode clamp in ``_resolve_profile_home_for_name`` (which pins every
lookup to ``_INITIAL_HERMES_HOME`` when ``HERMES_WEBUI_ISOLATED_PROFILE`` is
enabled). In an isolated deployment pinned at ``<base>/profiles/default``, a
session created with ``profile="default"`` therefore used the BASE root
home's workspace + config instead of the pinned one.

Fix under test: the literal shortcut is gone — the logical string ``"default"``
flows through ``get_hermes_home_for_profile()`` like every other id, so the
clamp applies. Literal-default routing to the global state files is RETAINED
(canonical ``_is_default_profile_home`` identity), asserted by the fallback
tests below.
"""

import pytest

import api.workspace as workspace
from api import profiles


@pytest.fixture
def isolated_default_pinned(tmp_path, monkeypatch):
    """Isolated-mode deployment pinned at <base>/profiles/default.

    Mirrors the gate reproduction: HERMES_WEBUI_ISOLATED_PROFILE=1 with a
    profile-shaped _INITIAL_HERMES_HOME whose directory NAME is literally
    'default' (the exact shape where base and pin diverge). Base home gets a
    DISTINCT config/workspace marker so any leak of the root home is visible.
    """
    base = tmp_path / ".hermes"
    pinned = base / "profiles" / "default"
    pinned.mkdir(parents=True)
    (pinned / "webui_state").mkdir()

    # Distinct base-vs-pinned configs: only the PINNED one carries the marker.
    (pinned / "config.yaml").write_text(
        "workspace: /srv/pinned-workspace\n", encoding="utf-8"
    )

    monkeypatch.setenv("HERMES_WEBUI_ISOLATED_PROFILE", "1")
    monkeypatch.setattr(profiles, "_INITIAL_ISOLATED_PROFILE_OPT_IN", "1")
    monkeypatch.setattr(profiles, "_INITIAL_HERMES_HOME", str(pinned))
    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", base)
    monkeypatch.setattr(profiles, "_LIST_PROFILES_CACHE", None)
    # Hermetic w.r.t. the runner's real state dir / remote terminal config.
    monkeypatch.setattr(workspace, "_remote_terminal_cwd", lambda profile=None: None)
    return {"base": base, "pinned": pinned, "tmp": tmp_path}


class TestRound6DefaultHonorsIsolationClamp:
    """The logical "default" name resolves through the clamped delegated path."""

    def test_resolver_pins_default_in_isolated_mode(self, isolated_default_pinned):
        env = isolated_default_pinned
        assert profiles._is_isolated_profile_mode() is True
        assert (
            profiles.get_hermes_home_for_profile("default") == env["pinned"]
        )  # pre-existing correct behavior (the clamp)
        got = workspace._resolve_profile_home_param("default")
        assert got == env["pinned"].resolve(), (
            f"_resolve_profile_home_param('default') must reach the isolated-mode "
            f"clamp and return the PINNED home {env['pinned']}, got {got}"
        )
        assert got != env["base"], "must not resolve to the base/root home"

    def test_get_last_workspace_uses_global_state_not_base(self, isolated_default_pinned, monkeypatch):
        """Round 7 contract: 'default' STATE lives in the GLOBAL files only.

        Round-6 follow-up: once the resolver pins config/path resolution at
        the isolated home, the canonical-home check in _profile_state_dir
        split explicit profile="default" state I/O onto {pinned}/webui_state/
        while ambient calls kept the global dir. Round 7 restores ONE state
        authority: literal "default" reads/writes the global files, so this
        test now asserts the global binding WINS and the BASE home's
        webui_state is never consulted.
        """
        env = isolated_default_pinned
        # The authoritative GLOBAL binding...
        global_ws = env["tmp"] / "srv" / "global-ws"
        global_ws.mkdir(parents=True)
        global_lw = env["tmp"] / "global-state"
        global_lw.mkdir(parents=True)
        # Both global state files must move TOGETHER: in production
        # _GLOBAL_WS_FILE and _GLOBAL_LW_FILE share one state dir.
        monkeypatch.setattr(workspace, "_GLOBAL_WS_FILE", global_lw / "workspaces.json")
        monkeypatch.setattr(workspace, "_GLOBAL_LW_FILE", global_lw / "last_workspace.txt")
        (global_lw / "last_workspace.txt").write_text(str(global_ws), encoding="utf-8")

        # ...and a POISONED base binding that must NOT win. Point the base
        # home's own webui_state at a foreign path to prove no base read.
        poisoned_ws = env["tmp"] / "srv" / "poisoned-base-ws"
        poisoned_ws.mkdir(parents=True)
        base_state = env["base"] / "webui_state"
        base_state.mkdir(parents=True)
        (base_state / "last_workspace.txt").write_text(str(poisoned_ws), encoding="utf-8")

        got = workspace.get_last_workspace(profile="default")
        assert str(got) == str(global_ws.resolve()), (
            f"profile='default' in isolated mode must read the GLOBAL state "
            f"authority ({global_lw / 'last_workspace.txt'}), got {got!r}"
        )

    def test_get_last_workspace_global_fallback_only_when_canonically_default(
        self, isolated_default_pinned, monkeypatch
    ):
        """No profile-local state + non-pinned layout → legacy global fallback.

        Literal-default routing to _GLOBAL_LW_FILE is retained OUTSIDE isolated
        mode: when the resolved default home IS canonically the default home,
        get_last_workspace(profile="default") still reads the global file
        (historical behavior, re-gate round 3 contract).
        """
        env = isolated_default_pinned
        # Disable isolation so 'default' resolves canonically to the base home.
        monkeypatch.delenv("HERMES_WEBUI_ISOLATED_PROFILE", raising=False)
        monkeypatch.setattr(profiles, "_INITIAL_ISOLATED_PROFILE_OPT_IN", "")
        monkeypatch.setattr(profiles, "_INITIAL_HERMES_HOME", str(env["base"]))
        global_lw = env["tmp"] / "global-state"
        global_lw.mkdir(parents=True)
        monkeypatch.setattr(workspace, "_GLOBAL_LW_FILE", global_lw / "last_workspace.txt")

        shared_ws = env["tmp"] / "srv" / "shared-ws"
        shared_ws.mkdir(parents=True)
        (global_lw / "last_workspace.txt").write_text(str(shared_ws), encoding="utf-8")

        # The conftest autouse cleanup fixture writes TEST_STATE_DIR /
        # last_workspace.txt at every teardown; for a canonically-default
        # profile that IS the profile-local tier and would shadow the global
        # file under test. Pin the profile-local tier to an absent path so the
        # GLOBAL fallback is what actually gets exercised.
        monkeypatch.setattr(
            workspace,
            "_last_workspace_file_for_profile",
            lambda p=None: env["tmp"] / "absent-webui_state" / "last_workspace.txt",
        )

        got = workspace.get_last_workspace(profile="default")
        assert str(got) == str(shared_ws), (
            "outside isolated mode, profile='default' keeps the legacy global "
            "fallback (canonical identity retained after the round-6 fix)"
        )

    def test_new_session_binds_global_state_workspace(self, isolated_default_pinned, monkeypatch):
        """Session creation with profile='default' honors the global state
        authority and never leaks onto the base home (round-7 contract)."""
        import api.models as models

        env = isolated_default_pinned
        bound_ws = env["tmp"] / "srv" / "bound-session-ws"
        bound_ws.mkdir(parents=True)
        global_lw = env["tmp"] / "global-state"
        global_lw.mkdir(parents=True)
        # Both global state files must move TOGETHER: in production
        # _GLOBAL_WS_FILE and _GLOBAL_LW_FILE share one state dir.
        monkeypatch.setattr(workspace, "_GLOBAL_WS_FILE", global_lw / "workspaces.json")
        monkeypatch.setattr(workspace, "_GLOBAL_LW_FILE", global_lw / "last_workspace.txt")
        (global_lw / "last_workspace.txt").write_text(str(bound_ws), encoding="utf-8")

        # POISONED base-home binding must not win.
        poisoned_ws = env["tmp"] / "srv" / "poisoned-base-ws"
        poisoned_ws.mkdir(parents=True)
        base_state = env["base"] / "webui_state"
        base_state.mkdir(parents=True)
        (base_state / "last_workspace.txt").write_text(str(poisoned_ws), encoding="utf-8")

        s = models.new_session(profile="default")
        assert s.profile == "default"
        assert str(s.workspace) == str(bound_ws.resolve()), (
            f"new_session(profile='default') in isolated mode must bind the "
            f"GLOBAL state authority's workspace, got {s.workspace!r}"
        )
        assert str(s.workspace) != str(poisoned_ws.resolve()), (
            "must never leak onto the BASE home's webui_state binding"
        )

    def test_session_config_reads_pinned_config(
        self, isolated_default_pinned, monkeypatch
    ):
        """Config for the resolved 'default' home comes from the PINNED home.

        In isolated mode the pinned home IS the active home, so
        get_config_for_profile_home takes the ambient-match branch — which is
        exactly the authority we must verify: the ambient resolver (and its
        HERMES_CONFIG_PATH) must be anchored at the PINNED home, never at the
        base/root one. Point the authoritative config INSIDE the pinned home;
        had the resolver leaked to the base home, this config would not win.
        """
        from api import config as cfg_mod
        from api.config import get_config_for_profile_home

        env = isolated_default_pinned
        monkeypatch.setenv("HERMES_CONFIG_PATH", str(env["pinned"] / "config.yaml"))
        cfg_mod.reload_config()

        home = workspace._resolve_profile_home_param("default")
        assert home == env["pinned"].resolve()
        got = get_config_for_profile_home(home)
        assert got.get("workspace") == "/srv/pinned-workspace", (
            "config for the resolved 'default' home must be anchored at the "
            "PINNED profile's home/config, never the base root's"
        )
        # The BASE root has no config.yaml — a leak onto the base home would
        # have produced the defaults-only dict (no 'workspace' key).


class TestRound7DefaultStateRouting:
    """Explicit and ambient "default" STATE I/O share ONE authority (round 7).

    Round-6 CORE follow-up: routing the literal "default" name through the
    delegated resolver made the canonical-home check in
    ``_profile_state_dir`` send EXPLICIT ``profile="default"`` state
    reads/writes to ``{pinned}/webui_state/`` while AMBIENT calls kept using
    the global state dir — splitting saved workspaces between two
    authorities and hiding the pre-upgrade list. Fix under test:
    literal-"default" STATE routing stays on the global files, while config
    and workspace PATH resolution keep the pinned home.
    """

    def _seed_global_state(self, env, monkeypatch):
        """Point both global state files at an isolated tmp dir."""
        g = env["tmp"] / "global-state"
        g.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(workspace, "_GLOBAL_WS_FILE", g / "workspaces.json")
        monkeypatch.setattr(workspace, "_GLOBAL_LW_FILE", g / "last_workspace.txt")
        return g

    def test_explicit_default_state_dir_is_global(self, isolated_default_pinned, monkeypatch):
        env = isolated_default_pinned
        g = self._seed_global_state(env, monkeypatch)
        assert workspace._profile_state_dir(profile="default") == g, (
            "explicit profile='default' must route STATE to the global dir "
            "(same authority as ambient), not {pinned}/webui_state/"
        )
        assert workspace._workspaces_file("default") == g / "workspaces.json"
        assert workspace._last_workspace_file("default") == g / "last_workspace.txt"

    def test_explicit_and_ambient_share_workspaces_authority(
        self, isolated_default_pinned, monkeypatch
    ):
        import json

        env = isolated_default_pinned
        g = self._seed_global_state(env, monkeypatch)

        # Pre-upgrade saved list lives ONLY in the global file.
        pre_dir = env["tmp"] / "srv" / "pre-upgrade"
        pre_dir.mkdir(parents=True)
        pre = [{"path": str(pre_dir), "name": "Pre"}]
        (g / "workspaces.json").write_text(
            json.dumps(pre, ensure_ascii=False), encoding="utf-8"
        )

        # Explicit profile="default" reads the pre-upgrade list...
        assert workspace.load_workspaces(profile="default") == pre

        # ...and explicit writes land in the SAME global file.
        added = env["tmp"] / "srv" / "added"
        added.mkdir(parents=True)
        updated = pre + [{"path": str(added), "name": "Added"}]
        workspace.save_workspaces(updated, profile="default")
        assert json.loads((g / "workspaces.json").read_text(encoding="utf-8")) == updated

        # Ambient calls see the identical authority — no split.
        assert workspace._profile_state_dir(None) == g
        assert workspace.load_workspaces() == updated

        # No divergent webui_state authority may appear under the pinned home.
        assert not (env["pinned"] / "webui_state" / "workspaces.json").exists()
        assert not (env["pinned"] / "webui_state" / "last_workspace.txt").exists()

    def test_last_workspace_round_trip_shared_explicit_ambient(
        self, isolated_default_pinned, monkeypatch
    ):
        env = isolated_default_pinned
        g = self._seed_global_state(env, monkeypatch)

        ws = env["tmp"] / "srv" / "bound-ws"
        ws.mkdir(parents=True)

        workspace.set_last_workspace(str(ws), profile="default")
        assert (g / "last_workspace.txt").exists(), (
            "set_last_workspace('default') must write the GLOBAL file"
        )
        assert not (env["pinned"] / "webui_state" / "last_workspace.txt").exists()

        assert workspace.get_last_workspace(profile="default") == str(ws)
        # Ambient (isolated mode's active profile is literally "default")
        # resolves to the same binding.
        assert workspace.get_last_workspace() == str(ws)
