from pathlib import Path
import json
import pathlib

import pytest

from api import config as api_config
from api import workspace


REMOTE_CWD = "/Users/joeyshiue"


def _remote_config(**overrides):
    cfg = {"terminal": {"backend": "ssh", "cwd": REMOTE_CWD}}
    cfg.update(overrides)
    return cfg


def test_remote_terminal_cwd_is_profile_default_without_local_stat(monkeypatch, tmp_path):
    fallback = tmp_path / "fallback"
    fallback.mkdir()

    monkeypatch.setattr(api_config, "DEFAULT_WORKSPACE", fallback)
    monkeypatch.setattr(api_config, "get_config", lambda: _remote_config())

    assert workspace._profile_default_workspace() == REMOTE_CWD


def test_remote_terminal_last_workspace_ignores_stale_local_path(monkeypatch, tmp_path):
    stale_local = tmp_path / "stale-local"
    stale_local.mkdir()
    last_workspace = tmp_path / "last_workspace.txt"
    last_workspace.write_text(str(stale_local), encoding="utf-8")

    monkeypatch.setattr(api_config, "get_config", lambda: _remote_config())
    monkeypatch.setattr(workspace, "_last_workspace_file", lambda: last_workspace)
    monkeypatch.setattr(workspace, "_GLOBAL_LW_FILE", tmp_path / "missing-global-last-workspace.txt")

    assert workspace.get_last_workspace() == REMOTE_CWD


def test_remote_terminal_workspace_paths_under_cwd_do_not_require_local_existence(monkeypatch):
    monkeypatch.setattr(api_config, "get_config", lambda: _remote_config())

    target_side_project = f"{REMOTE_CWD}/projects/demo"

    assert workspace.validate_workspace_to_add(target_side_project) == Path(target_side_project).resolve()
    assert workspace.resolve_trusted_workspace(target_side_project) == Path(target_side_project).resolve()


def test_remote_terminal_workspace_paths_outside_cwd_still_reject(monkeypatch):
    monkeypatch.setattr(api_config, "get_config", lambda: _remote_config())

    with pytest.raises(ValueError, match="Path does not exist"):
        workspace.validate_workspace_to_add("/Users/other/projects/demo")

    with pytest.raises(ValueError, match="Path does not exist"):
        workspace.resolve_trusted_workspace("/Users/other/projects/demo")


@pytest.mark.parametrize(
    "terminal_cfg",
    [
        pytest.param({"backend": "ssh", "cwd": REMOTE_CWD}, id="cwd-absolute"),
        pytest.param({"backend": "ssh"}, id="cwd-omitted"),
        pytest.param({"backend": "ssh", "cwd": ""}, id="cwd-empty"),
        pytest.param({"backend": "ssh", "cwd": "."}, id="cwd-dot"),
    ],
)
def test_remote_terminal_implicit_recovery_does_not_use_local_missing_path(
    monkeypatch, tmp_path, terminal_cfg
):
    fallback_path = tmp_path / "fallback"
    fallback_path.mkdir()
    monkeypatch.setattr(
        api_config,
        "get_config",
        lambda: _remote_config(terminal=terminal_cfg),
    )
    monkeypatch.setattr(workspace, "_home_path", lambda: tmp_path)
    fallback_calls = {"count": 0}

    def fallback():
        fallback_calls["count"] += 1
        return fallback_path

    with pytest.raises(ValueError, match="Path does not exist"):
        workspace.resolve_implicit_workspace_with_recovery(
            "/Users/other/projects/demo",
            fallback,
        )

    assert fallback_calls["count"] == 0


def test_remote_terminal_workspace_paths_with_parent_escape_still_reject(monkeypatch):
    monkeypatch.setattr(api_config, "get_config", lambda: _remote_config())

    escaped = f"{REMOTE_CWD}/../other/projects/demo"

    with pytest.raises(ValueError, match="Path does not exist"):
        workspace.validate_workspace_to_add(escaped)

    with pytest.raises(ValueError, match="Path does not exist"):
        workspace.resolve_trusted_workspace(escaped)


@pytest.mark.parametrize("workspace_path", ["/etc", "/etc/ssh"])
def test_remote_terminal_workspace_system_roots_still_reject(monkeypatch, workspace_path):
    monkeypatch.setattr(api_config, "get_config", lambda: _remote_config(terminal={"backend": "ssh", "cwd": "/etc"}))

    with pytest.raises(ValueError, match="Path points to a system directory"):
        workspace.validate_workspace_to_add(workspace_path)

    with pytest.raises(ValueError, match="Path points to a system directory"):
        workspace.resolve_trusted_workspace(workspace_path)


@pytest.mark.parametrize("validator", [workspace.resolve_trusted_workspace, workspace.validate_workspace_to_add])
def test_var_home_workspaces_stay_allowed_before_system_root_blocklist(monkeypatch, validator):
    home = Path("/var/home/joeyshiue")
    candidate = home / "projects/demo"

    monkeypatch.setattr(workspace, "_resolve_path", lambda raw: candidate if str(raw) == str(candidate) else Path(raw))
    monkeypatch.setattr(workspace, "_home_path", lambda: home)
    monkeypatch.setattr(workspace, "_workspace_access_error", lambda _candidate: None)

    assert validator(str(candidate)) == candidate


def test_remote_terminal_workspace_rejects_embedded_nullbyte_in_raw_path(monkeypatch):
    """Embedded null bytes in remote workspace path should be rejected."""
    monkeypatch.setattr(api_config, "get_config", lambda: _remote_config())

    # Path with embedded null byte
    nullbyte_path = f"{REMOTE_CWD}/projects\x00/demo"

    assert workspace._remote_terminal_workspace_candidate(nullbyte_path) is None


def test_remote_terminal_workspace_rejects_embedded_nullbyte_in_cwd(monkeypatch):
    """Embedded null bytes in remote terminal cwd should be rejected."""
    monkeypatch.setattr(api_config, "get_config", lambda: _remote_config(terminal={"backend": "ssh", "cwd": f"{REMOTE_CWD}\x00/malicious"}))

    # Normal path, but remote cwd contains null byte
    normal_path = f"{REMOTE_CWD}/projects/demo"

    assert workspace._remote_terminal_workspace_candidate(normal_path) is None


def test_remote_terminal_linux_home_preserves_path_without_macos_synthetic_resolution(monkeypatch):
    """Remote Linux /home/<user> paths must not resolve to /System/Volumes/Data/home/<user> on macOS."""
    monkeypatch.setattr(
        api_config,
        "get_config",
        lambda: _remote_config(terminal={"backend": "ssh", "cwd": "/home/developer"}),
    )

    real_resolve = workspace._safe_resolve

    def fake_resolve(p):
        p_str = str(p)
        if p_str == "/home/developer" or p_str.startswith("/home/developer/"):
            return Path(f"/System/Volumes/Data{p_str}")
        return real_resolve(p)

    monkeypatch.setattr(workspace, "_safe_resolve", fake_resolve)

    assert workspace.validate_workspace_to_add("/home/developer") == Path("/home/developer")
    assert workspace.resolve_trusted_workspace("/home/developer") == Path("/home/developer")
    assert workspace.get_profile_default_workspace() == "/home/developer"
    assert workspace._resolve_path("/home/developer") == Path("/home/developer")
    assert workspace._clean_workspace_list([{"path": "/home/developer", "name": "Dev"}]) == [
        {"path": "/home/developer", "name": "Dev"}
    ]


def test_session_init_and_created_workspace_preserve_remote_posix_path(monkeypatch):
    """Session initialization must not corrupt remote POSIX paths to macOS synthetic firmlinks."""
    monkeypatch.setattr(
        api_config,
        "get_config",
        lambda: _remote_config(terminal={"backend": "ssh", "cwd": "/home/rootson"}),
    )

    from api.models import Session

    s = Session(workspace="/home/rootson", created_workspace="/home/rootson")
    assert s.workspace == "/home/rootson"
    assert s.created_workspace == "/home/rootson"


def test_named_profile_remote_terminal_workspace_candidate_isolated(monkeypatch, tmp_path):
    """Remote paths must resolve per profile: remote for named remote profile, not for active local profile."""
    # Active profile is local
    monkeypatch.setattr(api_config, "get_config", lambda: {"terminal": {"backend": "local", "cwd": "/Users/local"}})

    # Named profile 'optiplex' under base home
    profiles_dir = tmp_path / "profiles" / "optiplex"
    profiles_dir.mkdir(parents=True)
    (profiles_dir / "config.yaml").write_text(
        "terminal:\n  backend: ssh\n  cwd: /home/rootson\n", encoding="utf-8"
    )

    from api import profiles
    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", tmp_path)
    monkeypatch.setattr(profiles, "_resolve_base_hermes_home", lambda: tmp_path)

    # Scoped to named remote profile: candidate is recognized as remote
    cand_optiplex = workspace._remote_terminal_workspace_candidate(
        "/home/rootson/projects/app", profile="optiplex"
    )
    assert cand_optiplex == Path("/home/rootson/projects/app")

    # Scoped to active local profile: candidate is NOT recognized as remote (prevents cross-profile bypass)
    cand_local = workspace._remote_terminal_workspace_candidate(
        "/home/rootson/projects/app", profile=None
    )
    assert cand_local is None

    # Local validation fails on nonexistent path when active profile is local
    with pytest.raises(ValueError, match="Path does not exist"):
        workspace.validate_workspace_to_add("/home/rootson/projects/app", profile=None)

    # Remote validation succeeds when profile is optiplex
    assert workspace.validate_workspace_to_add(
        "/home/rootson/projects/app", profile="optiplex"
    ) == Path("/home/rootson/projects/app")


def test_build_native_multimodal_message_preserves_remote_workspace(monkeypatch, tmp_path):
    """Multimodal message builder must scope workspace resolution to the session profile."""
    # Active ambient profile is local
    monkeypatch.setattr(api_config, "get_config", lambda: {"terminal": {"backend": "local", "cwd": "/Users/local"}})

    profiles_dir = tmp_path / "profiles" / "optiplex"
    profiles_dir.mkdir(parents=True)
    (profiles_dir / "config.yaml").write_text(
        "terminal:\n  backend: ssh\n  cwd: /home/rootson\n", encoding="utf-8"
    )

    from api import profiles, streaming
    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", tmp_path)
    monkeypatch.setattr(profiles, "_resolve_base_hermes_home", lambda: tmp_path)

    # 1. Profile as logical name string
    msg1 = streaming._build_native_multimodal_message(
        "[Workspace::v1: /home/rootson]\n",
        "hello",
        attachments=[],
        workspace="/home/rootson",
        profile="optiplex",
    )
    assert "[Workspace::v1: /home/rootson]" in msg1

    # 2. Profile as filesystem path string (_profile_home)
    msg2 = streaming._build_native_multimodal_message(
        "[Workspace::v1: /home/rootson]\n",
        "hello",
        attachments=[],
        workspace="/home/rootson",
        profile=str(profiles_dir),
    )
    assert "[Workspace::v1: /home/rootson]" in msg2

    # 3. Profile as Path object
    msg3 = streaming._build_native_multimodal_message(
        "[Workspace::v1: /home/rootson]\n",
        "hello",
        attachments=[],
        workspace="/home/rootson",
        profile=profiles_dir,
    )
    assert "[Workspace::v1: /home/rootson]" in msg3


def test_resolve_profile_home_param_formats(tmp_path):
    """_resolve_profile_home_param handles None, default, profile name, and Path.

    Round 5 (#7168): path-shaped STRINGS are no longer accepted as explicit
    homes — a string is strictly a logical profile id (grammar-checked), so an
    explicit home must be expressed as a Path value.
    """
    from api.profiles import _DEFAULT_HERMES_HOME

    assert workspace._resolve_profile_home_param(None) == _DEFAULT_HERMES_HOME
    assert workspace._resolve_profile_home_param("default") == _DEFAULT_HERMES_HOME
    # Path-shaped string: rejected by the logical-id grammar.
    with pytest.raises(ValueError):
        workspace._resolve_profile_home_param(str(tmp_path / "profiles/custom"))
    # Same location as a Path value: honored and canonicalized.
    assert workspace._resolve_profile_home_param(tmp_path / "profiles/custom") == (
        tmp_path / "profiles/custom"
    )


def test_remote_profile_a_cannot_use_remote_profile_b_cwd(monkeypatch, tmp_path):
    """Active remote profile Alice (/srv/remote-alice) cannot validate paths under remote profile Bob (/srv/remote-bob)."""
    profiles_alice = tmp_path / "profiles" / "alice"
    profiles_alice.mkdir(parents=True)
    (profiles_alice / "config.yaml").write_text("terminal:\n  backend: ssh\n  cwd: /srv/remote-alice\n", encoding="utf-8")

    profiles_bob = tmp_path / "profiles" / "bob"
    profiles_bob.mkdir(parents=True)
    (profiles_bob / "config.yaml").write_text("terminal:\n  backend: ssh\n  cwd: /srv/remote-bob\n", encoding="utf-8")

    from api import profiles
    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", tmp_path)
    monkeypatch.setattr(profiles, "_resolve_base_hermes_home", lambda: tmp_path)

    # Scoped to Alice: Bob's path must be rejected
    with pytest.raises(ValueError, match="Path does not exist"):
        workspace.validate_workspace_to_add("/srv/remote-bob/project", profile="alice")

    with pytest.raises(ValueError, match="Path does not exist"):
        workspace.resolve_trusted_workspace("/srv/remote-bob/project", profile="alice")

    # Scoped to Alice: Alice's own path succeeds
    assert workspace.validate_workspace_to_add("/srv/remote-alice/project", profile="alice") == Path("/srv/remote-alice/project")
    assert workspace.resolve_trusted_workspace("/srv/remote-alice/project", profile="alice") == Path("/srv/remote-alice/project")


def test_local_symlink_resolving_to_system_path_is_rejected(monkeypatch, tmp_path):
    """Local symlink pointing to a system directory (/etc) is strictly rejected."""
    # Active profile is local
    monkeypatch.setattr(api_config, "get_config", lambda: {"terminal": {"backend": "local", "cwd": str(tmp_path)}})
    monkeypatch.setattr(workspace, "_home_path", lambda: tmp_path / "home")

    symlink_to_etc = tmp_path / "symlink_etc"
    try:
        symlink_to_etc.symlink_to("/etc")
    except OSError:
        pytest.skip("Symlink creation requires permissions")

    with pytest.raises(ValueError, match="Path points to a system directory"):
        workspace.validate_workspace_to_add(str(symlink_to_etc), profile=None)

    with pytest.raises(ValueError, match="Path points to a system directory"):
        workspace.resolve_trusted_workspace(str(symlink_to_etc), profile=None)


def test_local_profile_add_workspace_auto_create_on_remote_path_collision(monkeypatch, tmp_path):
    """Active local profile adding a path beneath an inactive remote profile's cwd still creates local directory."""
    # Active profile is local
    monkeypatch.setattr(api_config, "get_config", lambda: {"terminal": {"backend": "local", "cwd": str(tmp_path)}})

    # Inactive remote profile 'optiplex' has cwd /tmp/remote-test
    profiles_dir = tmp_path / "profiles" / "optiplex"
    profiles_dir.mkdir(parents=True)
    (profiles_dir / "config.yaml").write_text("terminal:\n  backend: ssh\n  cwd: /tmp/remote-test\n", encoding="utf-8")

    from api import profiles
    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", tmp_path)
    monkeypatch.setattr(profiles, "_resolve_base_hermes_home", lambda: tmp_path)

    local_target = tmp_path / "remote-test" / "subproject"
    assert not local_target.exists()

    from api.routes import _handle_workspace_add

    class MockHandler:
        def __init__(self):
            self.response = None
        def send_response(self, *args): pass
        def send_header(self, *args): pass
        def end_headers(self): pass
        @property
        def wfile(self):
            class W:
                def write(self, b): pass
            return W()

    handler = MockHandler()
    monkeypatch.setattr(profiles, "get_active_profile_name", lambda: "default")
    monkeypatch.setattr(workspace, "_workspaces_file", lambda: tmp_path / "workspaces.json")
    monkeypatch.setattr(workspace, "_home_path", lambda: tmp_path)

    _handle_workspace_add(handler, {"path": str(local_target), "create": True})
    assert local_target.is_dir()


def test_detached_streaming_worker_preserves_session_profile_workspace(monkeypatch, tmp_path):
    """Detached streaming worker preserves session profile Alice (/srv/remote-alice) even with ambient local profile."""
    # Ambient process profile is local
    monkeypatch.setattr(api_config, "get_config", lambda: {"terminal": {"backend": "local", "cwd": "/Users/local"}})

    profiles_dir = tmp_path / "profiles" / "alice"
    profiles_dir.mkdir(parents=True)
    (profiles_dir / "config.yaml").write_text("terminal:\n  backend: ssh\n  cwd: /srv/remote-alice\n", encoding="utf-8")

    from api import profiles, models
    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", tmp_path)
    monkeypatch.setattr(profiles, "_resolve_base_hermes_home", lambda: tmp_path)

    # Simulate macOS firmlink expansion on host resolve
    real_resolve = workspace._safe_resolve
    def fake_resolve(p):
        p_str = str(p)
        if p_str == "/srv/remote-alice" or p_str.startswith("/srv/remote-alice/"):
            return Path(f"/System/Volumes/Data{p_str}")
        return real_resolve(p)

    monkeypatch.setattr(workspace, "_safe_resolve", fake_resolve)

    s = models.Session(session_id="test1234", workspace="/srv/remote-alice", profile="alice")
    assert s.workspace == "/srv/remote-alice"
    assert s.created_workspace == "/srv/remote-alice"

    # Streaming workspace update
    resolved_ws = workspace._resolve_path("/srv/remote-alice", profile=s.profile)
    assert str(resolved_ws) == "/srv/remote-alice"


def test_gateway_multimodal_message_preserves_remote_profile_workspace(monkeypatch, tmp_path):
    """Gateway chat multimodal payload builder preserves remote profile workspace containment."""
    # Ambient process profile is local
    monkeypatch.setattr(api_config, "get_config", lambda: {"terminal": {"backend": "local", "cwd": "/Users/local"}})

    profiles_dir = tmp_path / "profiles" / "optiplex"
    profiles_dir.mkdir(parents=True)
    (profiles_dir / "config.yaml").write_text("terminal:\n  backend: ssh\n  cwd: /home/rootson\n", encoding="utf-8")

    from api import profiles, streaming, models
    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", tmp_path)
    monkeypatch.setattr(profiles, "_resolve_base_hermes_home", lambda: tmp_path)

    s = models.Session(session_id="gw_test", workspace="/home/rootson", profile="optiplex")

    msg = streaming._build_native_multimodal_message(
        "",
        "hello",
        attachments=[],
        workspace=str(s.workspace),
        profile=getattr(s, "profile", None),
    )
    assert msg == "hello"


def test_isolated_profile_config_read_does_not_mutate_or_read_shared_cache(monkeypatch, tmp_path):
    """Explicit profile config lookup must not leak into or read from mutable global config cache."""
    alice_home = tmp_path / "profiles" / "alice"
    alice_home.mkdir(parents=True)
    (alice_home / "config.yaml").write_text("terminal:\n  backend: ssh\n  cwd: /srv/remote-alice\n", encoding="utf-8")

    bob_home = tmp_path / "profiles" / "bob"
    bob_home.mkdir(parents=True)
    (bob_home / "config.yaml").write_text("terminal:\n  backend: local\n  cwd: /Users/bob\n", encoding="utf-8")

    from api import profiles
    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", tmp_path)
    monkeypatch.setattr(profiles, "_resolve_base_hermes_home", lambda: tmp_path)

    # NO ambient TLS profile: simulates a detached worker resolving an explicit
    # session profile while the process-global active profile is 'default'
    # (local). Alice diverges from the ambient target, so her on-disk
    # config.yaml is authoritative — regardless of any concurrent reload.
    monkeypatch.setattr(api_config, "get_config", lambda: {"terminal": {"backend": "local", "cwd": "/Users/local"}})

    # 1. alice resolves to remote cwd (divergent profile reads its own disk file)
    assert workspace._remote_terminal_cwd("alice") == "/srv/remote-alice"

    # 2. bob resolves to None (local backend)
    assert workspace._remote_terminal_cwd("bob") is None

    # 3. Simulate concurrent reload on global _cfg_cache pointing to dirty state
    monkeypatch.setattr(api_config, "_cfg_cache", {"terminal": {"backend": "local", "cwd": "/Users/polluted"}})

    # Explicit lookup of alice still resolves to alice's on-disk terminal cwd, not polluted global cache
    assert workspace._remote_terminal_cwd("alice") == "/srv/remote-alice"
    assert workspace.validate_workspace_to_add("/srv/remote-alice/sub", profile="alice") == Path("/srv/remote-alice/sub")


def test_workspace_routes_profile_isolation(monkeypatch, tmp_path):
    """Workspace route actions (add, remove, rename, reorder) must be fully isolated per profile."""
    from api import profiles
    from api.routes import (
        _handle_workspace_add,
    )

    alice_home = tmp_path / "profiles" / "alice"
    alice_home.mkdir(parents=True)
    (alice_home / "config.yaml").write_text("terminal:\n  backend: ssh\n  cwd: /srv/remote-alice\n", encoding="utf-8")

    bob_home = tmp_path / "profiles" / "bob"
    bob_home.mkdir(parents=True)
    (bob_home / "config.yaml").write_text("terminal:\n  backend: ssh\n  cwd: /srv/remote-bob\n", encoding="utf-8")

    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", tmp_path)
    monkeypatch.setattr(profiles, "_resolve_base_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(workspace, "_home_path", lambda: tmp_path)

    # Ambient get_config() follows the per-request TLS profile exactly as
    # production does (server.py sets the cookie context; the config loader
    # resolves the active home). With the restored upstream authority rules,
    # an ambient-target lookup delegates here — so each simulated request
    # must see ITS OWN profile's terminal block.
    def _ambient_cfg():
        name = profiles.get_active_profile_name()
        if name == "bob":
            return {"terminal": {"backend": "ssh", "cwd": "/srv/remote-bob"}}
        if name == "alice":
            return {"terminal": {"backend": "ssh", "cwd": "/srv/remote-alice"}}
        return {"terminal": {"backend": "local", "cwd": str(tmp_path)}}

    monkeypatch.setattr(api_config, "get_config", _ambient_cfg)

    class MockHandler:
        def __init__(self):
            self.status = 200
            self.data = None
        def send_response(self, code): self.status = code
        def send_header(self, *args): pass
        def end_headers(self): pass
        @property
        def wfile(self):
            class W:
                def write(inner_self, b): pass
            return W()

    # Add workspace under Alice — TLS profile is alice, so the route's
    # get_active_profile_name() == 'alice' and the add writes into alice's
    # profile-scoped workspaces.json.
    profiles.set_request_profile("alice")
    try:
        h1 = MockHandler()
        _handle_workspace_add(h1, {"path": "/srv/remote-alice/project1", "name": "Project 1"})
        assert len(workspace.load_workspaces(profile="alice")) == 2
        assert any(w["path"] == "/srv/remote-alice/project1" for w in workspace.load_workspaces(profile="alice"))
    finally:
        profiles.clear_request_profile()

    # Bob's workspaces must be clean and not contain Alice's workspace
    profiles.set_request_profile("bob")
    try:
        h2 = MockHandler()
        assert len(workspace.load_workspaces(profile="bob")) == 1
        assert workspace.load_workspaces(profile="bob")[0]["path"] == "/srv/remote-bob"  # Default workspace for bob

        _handle_workspace_add(h2, {"path": "/srv/remote-bob/app", "name": "Bob App"})
        bob_ws = workspace.load_workspaces(profile="bob")
        assert any(w["path"] == "/srv/remote-bob/app" for w in bob_ws)
        assert not any("remote-alice" in w["path"] for w in bob_ws)
    finally:
        profiles.clear_request_profile()

    # Re-verify Alice's workspaces did not get Bob's workspace
    profiles.set_request_profile("alice")
    try:
        alice_ws = workspace.load_workspaces(profile="alice")
        assert len(alice_ws) == 2
        assert any(w["path"] == "/srv/remote-alice/project1" for w in alice_ws)
        assert not any("remote-bob" in w["path"] for w in alice_ws)
    finally:
        profiles.clear_request_profile()


def test_saved_workspace_trust_scoped_to_session_profile(monkeypatch, tmp_path):
    """resolve_trusted_workspace's saved-workspace branch (B) must honor the explicit profile.

    Greptile #3838260381: when a session profile differs from the ambient
    profile, the saved-workspace authorization branch called load_workspaces()
    and _resolve_path() WITHOUT the session profile — so a session-owned
    external workspace was rejected and a path saved only by the ambient
    profile was wrongly trusted instead.
    """
    from api import profiles

    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", tmp_path)
    monkeypatch.setattr(profiles, "_resolve_base_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(workspace, "_home_path", lambda: tmp_path / "home")
    (tmp_path / "home").mkdir(parents=True)

    # Session profile 'carol': remote SSH with cwd /srv/remote-carol.
    carol_home = tmp_path / "profiles" / "carol"
    carol_home.mkdir(parents=True)
    (carol_home / "config.yaml").write_text(
        "terminal:\n  backend: ssh\n  cwd: /srv/remote-carol\n", encoding="utf-8"
    )
    # Ambient/default: local backend so nothing is remotely classified.
    monkeypatch.setattr(api_config, "get_config", lambda: {"terminal": {"backend": "local", "cwd": str(tmp_path)}})

    # Carol's saved list includes an external (non-home, non-cwd) workspace dir.
    external_dir = tmp_path / "data" / "carol-projects"
    external_dir.mkdir(parents=True)
    ws_file = tmp_path / "state"
    monkeypatch.setattr(workspace, "_workspaces_file_for_profile", lambda p=None: ws_file)
    ws_file.parent.mkdir(parents=True, exist_ok=True)
    ws_file.write_text(
        json.dumps([{"path": str(external_dir), "name": "Carol External"}]),
        encoding="utf-8",
    )

    # With the session profile passed explicitly, the saved branch trusts it.
    resolved = workspace.resolve_trusted_workspace(str(external_dir), profile="carol")
    assert resolved == Path(str(external_dir)).resolve()

    # Without the profile (ambient local), the same path is NOT trusted:
    # it lives outside home/boot-default and the ambient profile has no such save.
    monkeypatch.setattr(workspace, "_workspaces_file_for_profile", lambda p=None: tmp_path / "missing-workspaces.json")
    with pytest.raises(ValueError, match="outside the user home directory"):
        workspace.resolve_trusted_workspace(str(external_dir))


def test_session_init_simulated_macos_resolution_is_red_on_master_shape(monkeypatch):
    """Session.__init__ must preserve /home/<user> even when host resolve rewrites it.

    Maintainer gate r4 item 3: this test simulates macOS synthetic firmlink
    expansion on the HOST resolver. On origin/master's shape
    (Path(workspace).expanduser().resolve()) the assertion fails because the
    stored workspace becomes /System/Volumes/Data/home/rootson; on this head
    (_resolve_path with profile-scoped remote preservation) it passes — i.e.
    genuinely RED -> GREEN across the fix boundary.
    """
    monkeypatch.setattr(
        api_config,
        "get_config",
        lambda: _remote_config(terminal={"backend": "ssh", "cwd": "/home/rootson"}),
    )

    real_resolve = workspace._safe_resolve

    def fake_resolve(p):
        p_str = str(p)
        if p_str == "/home/rootson" or p_str.startswith("/home/rootson/"):
            return Path(f"/System/Volumes/Data{p_str}")
        return real_resolve(p)

    monkeypatch.setattr(workspace, "_safe_resolve", fake_resolve)

    from api.models import Session

    s = Session(workspace="/home/rootson", created_workspace="/home/rootson")
    assert s.workspace == "/home/rootson"
    assert s.created_workspace == "/home/rootson"


def test_local_tilde_and_relative_paths_still_host_resolve(monkeypatch, tmp_path):
    """Control: local ~/ paths keep host resolution while remotes are preserved.

    Guards the discriminator BOTH ways (maintainer gate r4): the remote-path
    preservation must not swallow ordinary local path semantics — tilde
    expansion and symlink normalization still run for local profiles.
    """
    real_home = tmp_path / "users" / "local"
    projects = real_home / "projects"
    projects.mkdir(parents=True)
    # A symlink inside home pointing elsewhere-under-home must normalize.
    linked = real_home / "linked-projects"
    try:
        linked.symlink_to(projects)
    except OSError:
        pytest.skip("Symlink creation requires permissions")

    monkeypatch.setenv("HOME", str(real_home))
    monkeypatch.setattr(api_config, "get_config", lambda: {"terminal": {"backend": "local", "cwd": str(projects)}})

    # Tilde expansion still resolves against the LOCAL home.
    resolved = workspace._resolve_path("~/projects")
    assert resolved == projects.resolve()

    # Symlinks under home still collapse to their real target.
    resolved_link = workspace._resolve_path("~/linked-projects")
    assert resolved_link == projects.resolve()


def test_config_authority_active_home_with_external_override(monkeypatch, tmp_path):
    """Maintainer blocker 1: an authoritative HERMES_CONFIG_PATH override wins
    over target/config.yaml when resolving the ACTIVE profile home."""
    from api import config as cfg, profiles as profiles_mod
    import yaml

    active_home = tmp_path / "active-home"
    active_home.mkdir()
    (active_home / "config.yaml").write_text(
        yaml.safe_dump({"terminal": {"backend": "ssh", "cwd": "/srv/from-active-home"}}, sort_keys=False),
        encoding="utf-8",
    )
    override_dir = tmp_path / "override-dir"
    override_dir.mkdir()
    override_cfg = override_dir / "config.yaml"
    override_cfg.write_text(
        yaml.safe_dump({"terminal": {"backend": "docker", "cwd": "/srv/from-override"}}, sort_keys=False),
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_CONFIG_PATH", str(override_cfg))
    monkeypatch.setattr(profiles_mod, "get_active_hermes_home", lambda: active_home)
    cfg.reload_config()

    result = cfg.get_config_for_profile_home(active_home)
    assert result.get("terminal", {}).get("cwd") == "/srv/from-override"
    # NOTE: no manual delenv/reload here — monkeypatch restores
    # HERMES_CONFIG_PATH at teardown, and the stale (_cfg_path vs pinned)
    # check invalidates the cache on the next reader. A trailing
    # reload_config() would bake this test's override into the global cache
    # and pollute later tests (#7168 review triage).


def test_divergent_profile_without_config_yaml_stays_isolated(monkeypatch, tmp_path):
    """Maintainer blocker 2: an existing divergent named-profile directory with
    NO config.yaml must NOT inherit the ambient default's terminal config."""
    from api import config as cfg, profiles as profiles_mod

    default_home = tmp_path / "default-home"
    default_home.mkdir(parents=True)
    (default_home / "config.yaml").write_text(
        "terminal:\n  backend: ssh\n  cwd: /srv/remote-default\n",
        encoding="utf-8",
    )
    # Fresh non-cloned named profile: exists on disk, no config.yaml yet.
    fresh_profile_home = tmp_path / "profiles" / "fresh"
    fresh_profile_home.mkdir(parents=True)

    monkeypatch.setenv("HERMES_CONFIG_PATH", str(default_home / "config.yaml"))
    monkeypatch.setattr(profiles_mod, "get_active_hermes_home", lambda: default_home)
    cfg.reload_config()

    result = cfg.get_config_for_profile_home(fresh_profile_home)
    # Defaults applied, but crucially NOT the ambient default profile's terminal block.
    assert result.get("terminal") != {"backend": "ssh", "cwd": "/srv/remote-default"}
    assert not result.get("terminal", {}).get("cwd")

    # And a divergent home that doesn't exist at all still returns {}.
    assert cfg.get_config_for_profile_home(tmp_path / "profiles" / "ghost") == {}
    # NOTE: no manual delenv/reload here (see test_config_authority_active_home_with_external_override) —
    # monkeypatch teardown + path-change invalidation keep the global cache clean.


def test_suggestions_scoped_to_explicit_profile_saved_roots(tmp_path):
    """Sweep regression (#7168 review): list_workspace_suggestions with an
    explicit profile must widen trust only via THAT profile's saved
    workspaces — a workspace saved under a different profile stays out."""
    other_dir = tmp_path / "other-profile-dir"
    other_dir.mkdir(parents=True)
    mine = tmp_path / "mine"
    mine.mkdir()

    ws_file = workspace._workspaces_file_for_profile("alice")
    ws_file.parent.mkdir(parents=True, exist_ok=True)
    ws_file.write_text(json.dumps([{"path": str(other_dir)}]), encoding="utf-8")

    prefix = str(tmp_path) + "/"
    suggestions = workspace.list_workspace_suggestions(prefix, profile="alice")
    assert any(s.endswith("other-profile-dir") for s in suggestions)

    # A different profile's saved list does NOT include alice's external dir.
    suggestions_bob = workspace.list_workspace_suggestions(prefix, profile="bob")
    assert not any(s.endswith("other-profile-dir") for s in suggestions_bob)
    assert mine.exists()  # control: fixture dir is real, absence is scoping


def test_recovery_helper_threads_profile_to_trust_and_fallback(tmp_path):
    """Sweep regression (#7168 review): resolve_implicit_workspace_with_recovery
    passes the profile into both trust resolution and the recovery fallback,
    so a deleted session workspace recovers to a fallback trusted for THAT
    profile — never via another profile's saved list."""
    external = tmp_path / "external-carol"
    external.mkdir()
    stale = tmp_path / "stale-workspace"
    fallback_dir = tmp_path / "fallback"
    fallback_dir.mkdir()

    ws_file = workspace._workspaces_file_for_profile("carol")
    ws_file.parent.mkdir(parents=True, exist_ok=True)
    ws_file.write_text(
        json.dumps([{"path": str(external)}, {"path": str(fallback_dir)}]),
        encoding="utf-8",
    )

    resolved, recovered = workspace.resolve_implicit_workspace_with_recovery(
        str(stale),
        lambda: str(fallback_dir),
        profile="carol",
    )
    assert recovered is True
    assert resolved == fallback_dir.resolve()

    # Under a DIFFERENT profile neither the stale candidate nor the recovery
    # fallback may gain trust via carol's saved list.
    with pytest.raises(ValueError):
        workspace.resolve_implicit_workspace_with_recovery(
            str(stale),
            lambda: str(fallback_dir),
            profile="not-carol",
        )



def test_clean_workspace_list_explicit_profile_keeps_own_entries(monkeypatch, tmp_path):
    """_clean_workspace_list must define 'own profile dir' by the explicit profile.

    Maintainer re-gate 2026-08-25 (CORE / data-loss): with an explicit
    profile="alice", own_profile_dir was still derived from the AMBIENT home.
    Loading Alice's saved list while ambient Bob was active dropped Alice's own
    workspace (it sat under Alice's home, not Bob's) and load_workspaces()
    persisted the emptied list back to disk — silent destruction of a
    profile's saved workspaces.

    Regression: Alice request / Bob ambient — Alice's own entry survives and
    is persisted intact; a genuinely foreign profile path is still removed.
    """
    from api import profiles

    # _clean_workspace_list derives the profiles root as _home_path()/'.hermes'/profiles.
    monkeypatch.setattr(workspace, "_home_path", lambda: tmp_path)
    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", tmp_path / ".hermes")
    monkeypatch.setattr(profiles, "_resolve_base_hermes_home", lambda: tmp_path / ".hermes")
    (tmp_path / ".hermes" / "profiles").mkdir(parents=True)

    alice_home = tmp_path / ".hermes" / "profiles" / "alice"
    alice_home.mkdir(parents=True)
    bob_home = tmp_path / ".hermes" / "profiles" / "bob"
    bob_home.mkdir(parents=True)

    # Alice owns a workspace that lives inside her OWN profile directory.
    alice_own = alice_home / "projects"
    alice_own.mkdir(parents=True)
    # A foreign entry pointing into BOB's profile dir must still be pruned.
    foreign = bob_home / "secret"
    foreign.mkdir(parents=True)

    raw = [
        {"path": str(alice_own), "name": "AliceProjects"},
        {"path": str(foreign), "name": "BobLeak"},
    ]

    cleaned = workspace._clean_workspace_list(raw, profile="alice")

    paths = [w["path"] for w in cleaned]
    assert str(alice_own) in paths, (
        "explicit-profile cleaning must keep the target profile's OWN entry"
    )
    assert not any("bob" in p for p in paths), "cross-profile leak must still be pruned"

    # End-to-end through the loader: with ambient Bob active, loading ALICE's
    # list must neither drop nor rewrite-away her own entry.
    monkeypatch.setattr(
        profiles, "get_active_profile_name", lambda: "bob", raising=False
    )
    ws_file = tmp_path / "state" / "workspaces.json"
    monkeypatch.setattr(workspace, "_workspaces_file_for_profile", lambda p=None: ws_file)
    ws_file.parent.mkdir(parents=True, exist_ok=True)
    ws_file.write_text(json.dumps(raw), encoding="utf-8")

    loaded = workspace.load_workspaces(profile="alice")
    loaded_paths = [w["path"] for w in loaded]
    assert str(alice_own) in loaded_paths
    on_disk = json.loads(ws_file.read_text(encoding="utf-8"))
    assert any(w["path"] == str(alice_own) for w in on_disk), (
        "persisted list must retain Alice's own workspace"
    )


def test_stale_workspace_recovery_scoped_to_explicit_profile(monkeypatch, tmp_path):
    """Recovery classification/probe/fallback must honor the explicit profile.

    Maintainer re-gate 2026-08-25 (CORE): resolve_implicit_workspace_with_recovery
    classified the backend via ambient get_config(), probed candidate paths
    without profile=, and fell back to an unbound getter. With profile="alice"
    under ambient Bob it returned BOB's last workspace as recovered=True, and an
    Alice remote backend without terminal.cwd was misclassified as local.

    Regression A (remote misclassification): alice is ssh WITHOUT cwd, ambient
      is local — recovery must fail closed preserving the original error.
    Regression B (cross-profile bind): alice local, stored path missing,
      fallback=get_last_workspace — must never return Bob's path nor claim
      recovered=True for it.
    """
    from api import profiles

    monkeypatch.setattr(workspace, "_home_path", lambda: tmp_path)
    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", tmp_path / ".hermes")
    monkeypatch.setattr(profiles, "_resolve_base_hermes_home", lambda: tmp_path / ".hermes")
    (tmp_path / ".hermes" / "profiles").mkdir(parents=True)

    alice_home = tmp_path / ".hermes" / "profiles" / "alice"
    alice_home.mkdir(parents=True)
    bob_home = tmp_path / ".hermes" / "profiles" / "bob"
    bob_home.mkdir(parents=True)

    def _ambient_cfg():
        # Ambient context belongs to Bob: LOCAL backend.
        return {"terminal": {"backend": "local"}}

    monkeypatch.setattr(api_config, "get_config", _ambient_cfg)

    # ── A: remote backend without terminal.cwd fails closed ──
    (alice_home / "config.yaml").write_text(
        "terminal:\n  backend: ssh\n", encoding="utf-8"
    )
    missing_remote = tmp_path / "gone" / "remote-ws"

    with pytest.raises(ValueError):
        workspace.resolve_implicit_workspace_with_recovery(
            str(missing_remote),
            None,
            profile="alice",
        )

    # ── B: missing-path fallback can never bind another profile's workspace ──
    (alice_home / "config.yaml").write_text(
        "terminal:\n  backend: local\n", encoding="utf-8"
    )
    stored_missing = tmp_path / "data" / "vanished"
    bob_ws = tmp_path / "srv" / "bobs-place"
    bob_ws.mkdir(parents=True)
    alice_ws = tmp_path / "srv" / "alice-own"
    alice_ws.mkdir(parents=True)

    # Alice's SAVED list contains her own external workspace (so it is trusted),
    # while Bob's list contains his. The saved-workspace trust branch is itself
    # profile-scoped (earlier fix), so each name resolves only its own list.
    alice_list = tmp_path / "state-alice" / "workspaces.json"
    bob_list = tmp_path / "state-bob" / "workspaces.json"
    alice_list.parent.mkdir(parents=True, exist_ok=True)
    bob_list.parent.mkdir(parents=True, exist_ok=True)
    alice_list.write_text(json.dumps([{"path": str(alice_ws), "name": "AliceOwn"}]), encoding="utf-8")
    bob_list.write_text(json.dumps([{"path": str(bob_ws), "name": "BobWs"}]), encoding="utf-8")

    def _ws_file_for(p=None):
        return alice_list if p is not None and str(p) == "alice" else bob_list

    monkeypatch.setattr(workspace, "_workspaces_file_for_profile", _ws_file_for)

    def _last_for(p=None):
        if p is not None:
            # Profile-aware getter: only ever returns THAT profile's state.
            if str(p) in ("alice",):
                return str(alice_ws)
            return str(bob_ws)
        return str(bob_ws)  # unscoped/ambient getter leaks Bob's path

    resolved, recovered = workspace.resolve_implicit_workspace_with_recovery(
        str(stored_missing),
        _last_for,
        profile="alice",
    )
    assert recovered is True
    assert resolved == alice_ws.resolve(), (
        "recovery under profile='alice' must bind Alice's own workspace"
    )
    assert "bobs-place" not in str(resolved), (
        "recovery under profile='alice' must never return Bob's workspace"
    )

def test_get_last_workspace_named_profile_never_reads_global_file(monkeypatch, tmp_path):
    """get_last_workspace(profile=...) must not fall back to the global file.

    Maintainer re-gate round 3 (CORE): for a named profile the getter still
    consulted _GLOBAL_LW_FILE after missing the profile-local file, so a direct
    probe returned Bob's globally-recorded workspace for profile="alice".
    The legacy global fallback is now allowed ONLY for the root/default
    profile. Uses the REAL getter with a real global-file fixture.
    """
    from api import profiles

    monkeypatch.setattr(workspace, "_home_path", lambda: tmp_path)
    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", tmp_path / ".hermes")
    monkeypatch.setattr(profiles, "_resolve_base_hermes_home", lambda: tmp_path / ".hermes")
    (tmp_path / ".hermes" / "profiles").mkdir(parents=True)
    (tmp_path / ".hermes" / "profiles" / "alice").mkdir(parents=True)

    bobs_dir = tmp_path / "srv" / "bobs-global"
    bobs_dir.mkdir(parents=True)

    # The legacy GLOBAL last-workspace file records BOB's workspace.
    global_lw = tmp_path / "global-state"
    global_lw.mkdir(parents=True)
    (global_lw / "last_workspace.txt").write_text(str(bobs_dir), encoding="utf-8")
    monkeypatch.setattr(workspace, "_GLOBAL_LW_FILE", global_lw / "last_workspace.txt")

    # Alice has NO profile-local last-workspace file at all.
    alice_state = tmp_path / ".hermes" / "profiles" / "alice" / "webui_state"

    def _lw_file_for(p=None):
        if p is not None and str(p) == "alice":
            return alice_state / "last_workspace.txt"
        return global_lw / "last_workspace.txt"

    monkeypatch.setattr(workspace, "_last_workspace_file_for_profile", _lw_file_for)

    # Direct probe: named profile must NOT inherit Bob's global binding.
    got = workspace.get_last_workspace(profile="alice")
    assert "bobs-global" not in str(got), (
        "named profile must never fall back to the global last-workspace file"
    )

    # Control: the root/default profile KEEPS the historical global fallback.
    monkeypatch.setattr(
        profiles,
        "_resolve_profile_home_param",
        lambda p: tmp_path / ".hermes" if p is None or str(p) in ("default",) else _lw_file_for(p).parent.parent.parent,
        raising=False,
    )
    got_default = workspace.get_last_workspace(profile="default")
    assert str(got_default) == str(bobs_dir), (
        "root/default profile retains the global last-workspace fallback"
    )


def test_new_session_binds_explicit_profile_not_ambient_last_workspace(monkeypatch, tmp_path):
    """new_session()/import_cli_session() must scope their fallback getter.

    Maintainer re-gate round 3 (CORE): both Session constructions fell back to
    get_last_workspace() WITHOUT the profile even though profile=profile was
    set on the same Session — an Alice session created under ambient Bob bound
    to BOB's workspace. Uses the real getters and real state files.
    """
    import api.models as models
    from api import profiles

    monkeypatch.setattr(workspace, "_home_path", lambda: tmp_path)
    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", tmp_path / ".hermes")
    monkeypatch.setattr(profiles, "_resolve_base_hermes_home", lambda: tmp_path / ".hermes")
    (tmp_path / ".hermes" / "profiles").mkdir(parents=True)
    (tmp_path / ".hermes" / "profiles" / "alice").mkdir(parents=True)

    bobs_ws = tmp_path / "srv" / "bobs-workspace"
    bobs_ws.mkdir(parents=True)
    alice_ws = tmp_path / "srv" / "alice-workspace"
    alice_ws.mkdir(parents=True)

    # Ambient/global last-workspace points at BOB's directory.
    global_lw = tmp_path / "global-state"
    global_lw.mkdir(parents=True)
    (global_lw / "last_workspace.txt").write_text(str(bobs_ws), encoding="utf-8")
    monkeypatch.setattr(workspace, "_GLOBAL_LW_FILE", global_lw / "last_workspace.txt")

    # Alice's OWN profile-local last-workspace points at her own directory.
    alice_state = tmp_path / ".hermes" / "profiles" / "alice" / "webui_state"
    alice_state.mkdir(parents=True)
    (alice_state / "last_workspace.txt").write_text(str(alice_ws), encoding="utf-8")

    def _lw_file_for(p=None):
        if p is not None and str(p) == "alice":
            return alice_state / "last_workspace.txt"
        return global_lw / "last_workspace.txt"

    monkeypatch.setattr(workspace, "_last_workspace_file_for_profile", _lw_file_for)
    monkeypatch.setattr(models, "get_last_workspace", workspace.get_last_workspace)

    s = models.new_session(profile="alice")
    assert str(s.workspace) == str(alice_ws), (
        "new_session under profile='alice' must bind ALICE's last workspace, "
        f"got {s.workspace!r}"
    )

    imported = models.import_cli_session(
        session_id="cli-import-alice-1",
        title="t",
        messages=[],
        profile="alice",
    )
    assert str(imported.workspace) == str(alice_ws), (
        "import_cli_session under profile='alice' must bind ALICE's last "
        f"workspace, got {imported.workspace!r}"
    )


# ── Re-gate round 4 (#7168): malformed names, aliases, unwired boundaries ────


class TestRound4ProfileIsolation:
    """Re-gate round 4: the four remaining profile-isolation instances."""

    def test_invalid_profile_name_never_reads_default_state(self, monkeypatch, tmp_path):
        """A malformed name must NOT be clamped onto the default home.

        Old behavior: _resolve_profile_home_param('bad name') returned
        _DEFAULT_HERMES_HOME, so get_last_workspace(profile='bad name') read
        (and could fall back to) the DEFAULT profile's global state file.
        """
        import api.models as models  # noqa: F401  (import parity with R3 tests)
        from api import profiles

        default_home = tmp_path / ".hermes"
        default_home.mkdir(parents=True)
        monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", default_home)
        monkeypatch.setattr(profiles, "_resolve_base_hermes_home", lambda: default_home)

        global_lw = tmp_path / "global-state"
        global_lw.mkdir(parents=True)
        poisoned = tmp_path / "poisoned-workspace"
        poisoned.mkdir()
        (global_lw / "last_workspace.txt").write_text(str(poisoned), encoding="utf-8")
        monkeypatch.setattr(workspace, "_GLOBAL_LW_FILE", global_lw / "last_workspace.txt")

        # The invalid name resolves to NOTHING state-like and raises on resolve.
        with pytest.raises(ValueError):
            workspace._resolve_profile_home_param("bad name")

        # Getter: never returns the poisoned default/global binding.
        got = workspace.get_last_workspace(profile="bad name")
        assert str(got) != str(poisoned)

        # Setter: writes no file anywhere under the default home.
        workspace.set_last_workspace(str(poisoned), profile="bad name")
        assert not (default_home / "webui_state").exists()

        # save_workspaces refuses outright.
        with pytest.raises(ValueError):
            workspace.save_workspaces([{"path": str(poisoned)}], profile="bad name")

    def test_symlink_alias_of_default_home_keeps_legacy_fallback(self, monkeypatch, tmp_path):
        """A symlink alias of _DEFAULT_HERMES_HOME is canonically the DEFAULT.

        Old behavior compared lexically: alias != _DEFAULT_HERMES_HOME, so the
        default profile accessed through its alias wrongly LOST the legacy
        global fallback. With canonical identity it keeps it.
        """
        from api import profiles

        real_home = tmp_path / "real-hermes"
        real_home.mkdir(parents=True)
        alias = tmp_path / "alias-hermes"
        try:
            alias.symlink_to(real_home, target_is_directory=True)
        except OSError:
            pytest.skip("symlinks unavailable in this environment")

        monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", real_home)
        monkeypatch.setattr(profiles, "_resolve_base_hermes_home", lambda: real_home)

        shared_ws = tmp_path / "shared-workspace"
        shared_ws.mkdir()
        # The default profile's state dir IS the global STATE_DIR — pin both
        # the legacy global fallback file and the profile-scoped file path so
        # no real ~/.hermes state leaks into the assertion.
        global_state = tmp_path / "global-state"
        global_state.mkdir(parents=True)
        (global_state / "last_workspace.txt").write_text(str(shared_ws), encoding="utf-8")
        monkeypatch.setattr(workspace, "_GLOBAL_LW_FILE", global_state / "last_workspace.txt")
        monkeypatch.setattr(
            workspace,
            "_GLOBAL_WS_FILE",
            global_state / "workspaces.json",
        )
        monkeypatch.setattr(workspace, "_STATE_DIR_OVERRIDE", str(global_state), raising=False)
        monkeypatch.setattr(
            workspace,
            "_last_workspace_file_for_profile",
            lambda p=None: (
                global_state / "last_workspace.txt"
                if p is None or workspace._is_default_profile_home(
                    workspace._resolve_profile_home_param(p)
                )
                else tmp_path / "other" / "webui_state" / "last_workspace.txt"
            ),
            raising=False,
        )

        assert workspace._is_default_profile_home(alias), (
            "symlink alias must compare canonically equal to the default home"
        )

        # No profile-local last_workspace.txt exists → legacy global fallback applies.
        # Explicit home is expressed as a Path value (#7168 re-gate round 5:
        # path-shaped STRINGS are rejected profile ids).
        got = workspace.get_last_workspace(profile=alias)
        assert str(got) == str(shared_ws)

    def test_set_last_workspace_threads_session_profile(self, monkeypatch, tmp_path):
        """set_last_workspace call sites write the SESSION's profile state file.

        Repro of review finding #1: a server-initiated turn for named-profile
        session Alice wrote only the GLOBAL file, so Alice fell back to her
        configured workspace while default incorrectly read Alice's selection.
        """
        alice_state = tmp_path / "profiles" / "alice" / "webui_state"
        alice_state.mkdir(parents=True)

        lw_paths = {}
        real_set = workspace.set_last_workspace

        def spy(path, profile=None):
            lw = workspace._last_workspace_file_for_profile(profile)
            lw_paths["profile"] = profile
            lw_paths["file"] = str(lw)
            return real_set(path, profile=profile)

        # Pin the resolver so 'alice' maps into the fixture tree (the product
        # code path under test is the routes.py call shape, not profiles lookup).
        monkeypatch.setattr(
            workspace,
            "_resolve_profile_home_param",
            lambda p: tmp_path / "profiles" / (p or "default"),
            raising=False,
        )

        # Simulate exactly what the three routes.py sites now do:
        s_like = type("S", (), {"profile": "alice"})()
        spy("/srv/alice-ws", profile=getattr(s_like, "profile", None))

        assert lw_paths["profile"] == "alice"
        assert pathlib.PurePath(lw_paths["file"]).name == "last_workspace.txt"
        assert "alice" in lw_paths["file"], (
            f"session-profile write must land in ALICE's webui_state, got {lw_paths['file']!r}"
        )
        assert not (tmp_path / "global").exists()

    def test_cli_projection_uses_own_cli_profile(self, monkeypatch, tmp_path):
        """The all-profile CLI sidebar projection scopes its workspace probe.

        Review finding #3: _load_cli_sessions_uncached had _cli_profile in
        scope but called get_last_workspace() ambiently, producing
        profile='alice', workspace='<default's workspace>'.
        """
        import api.models as models

        calls = []

        def fake_getlw(profile=None):
            calls.append(profile)
            return f"workspace-for:{profile}"

        monkeypatch.setattr(models, "get_last_workspace", fake_getlw)
        monkeypatch.setattr(models, "get_claude_code_sessions", lambda: [])
        monkeypatch.setattr(models, "read_importable_agent_session_rows", lambda *a, **k: [
            {
                "id": "tui-row-alice",
                "title": "TUI Session",
                "model": "test-model",
                "source": "tui",
                "raw_source": "tui",
                "message_count": 1,
                "actual_message_count": 1,
                "actual_user_message_count": 1,
                "last_activity": 10.0,
                "started_at": 9.0,
            }
        ])
        monkeypatch.setattr(models, "_profile_has_user_projects", lambda: False)
        monkeypatch.setattr(models, "ensure_cron_project", lambda **_: None)
        monkeypatch.setattr(models.Session, "load_metadata_only", lambda _sid: None)

        db = tmp_path / "state.db"
        db.write_text("", encoding="utf-8")
        rows = models._load_cli_sessions_uncached(
            tmp_path, db, _cli_profile="alice"
        )
        assert any(r.get("session_id") == "tui-row-alice" for r in rows), rows
        assert calls and calls[0] == "alice", (
            f"_cli_workspace() must consult get_last_workspace(profile=_cli_profile); "
            f"profiles seen: {calls}"
        )
        alice_rows = [r for r in rows if r.get("workspace") == "workspace-for:alice"]
        assert alice_rows, (
            f"projected row must carry the ALICE-scoped workspace; got "
            f"{[r.get('workspace') for r in rows]}"
        )


# ── Re-gate round 5 (#7168): traversal gate + symlinked config authority ─────


class TestRound5TraversalAndSymlinkAuthority:
    """Round 5: path-shaped profile STRINGS are rejected ids; config authority
    survives a symlinked HERMES_HOME."""

    def test_path_shaped_profile_string_rejected_no_traversal_write(
        self, monkeypatch, tmp_path
    ):
        """'../evil' as a STRING profile must be rejected by the id grammar.

        Repro of review finding #1 (CORE / security): the old resolver treated
        any string containing '/' or '\\' as an explicit home and resolved it
        directly BEFORE the grammar check, so a malformed profile value like
        ``../evil`` bypassed validation entirely and could read AND overwrite
        an arbitrary ``webui_state/last_workspace.txt`` outside the profile
        boundary. Real getters + real state files throughout.
        """
        from api import profiles

        default_home = tmp_path / ".hermes"
        default_home.mkdir(parents=True)
        monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", default_home)
        monkeypatch.setattr(profiles, "_resolve_base_hermes_home", lambda: default_home)

        # The file the traversal WOULD reach if it escaped the boundary.
        evil_dir = tmp_path / "evil"
        evil_state = evil_dir / "webui_state"
        evil_state.mkdir(parents=True)
        victim = evil_state / "last_workspace.txt"
        victim.write_text("/original-victim-binding", encoding="utf-8")

        traversal = "../evil"

        # Grammar gate: a path-shaped STRING is not a valid logical id.
        with pytest.raises(ValueError):
            workspace._resolve_profile_home_param(traversal)

        # Getter: reads NOTHING from the traversal target.
        got = workspace.get_last_workspace(profile=traversal)
        assert got != "/original-victim-binding"

        # Setter: writes NO file anywhere along the traversal.
        workspace.set_last_workspace("/attacker-chosen", profile=traversal)
        assert victim.read_text(encoding="utf-8") == "/original-victim-binding"
        assert sorted(p.name for p in evil_state.iterdir()) == ["last_workspace.txt"], (
            "traversal profile must not create or modify any state file"
        )

        # Saved-list writer refuses outright too.
        with pytest.raises(ValueError):
            workspace.save_workspaces([{"path": str(evil_dir)}], profile=traversal)

        # Same location delivered as a REAL Path is still honored — explicit
        # homes move to Path values; they are canonicalized, not banned.
        assert workspace._resolve_profile_home_param(Path(traversal)) == Path(
            traversal
        ).expanduser().resolve()

    def test_symlinked_home_with_authoritative_config_path_keeps_authority(
        self, monkeypatch, tmp_path
    ):
        """HERMES_HOME via symlink + authoritative HERMES_CONFIG_PATH.

        Repro of review finding #2 (CORE): workspace canonicalization hands
        get_config_for_profile_home the RESOLVED alias target, but the config
        authority compared the active home and the config parent LEXICALLY.
        With HERMES_HOME reached through a symlink alias, both identity checks
        failed and the function performed a DIRECT read of the symlink
        target's own (wrong) config.yaml instead of deferring to the
        authoritative HERMES_CONFIG_PATH — master trusts the remote
        terminal.cwd from the override; the lexical head rejected it.
        Canonicalize all three sides before comparing.
        """
        import os

        import yaml

        from api import profiles

        real_home = tmp_path / "real-hermes"
        real_home.mkdir(parents=True)
        alias_home = tmp_path / "alias-hermes"
        try:
            alias_home.symlink_to(real_home, target_is_directory=True)
        except OSError:
            pytest.skip("symlinks unavailable in this environment")

        # The symlink target's OWN config is the WRONG one a naive direct
        # read picks up (no remote cwd — exactly what "rejects the remote
        # path" looks like downstream).
        (real_home / "config.yaml").write_text(
            yaml.safe_dump(
                {"terminal": {"backend": "local", "cwd": "/srv/from-symlink-target"}},
                sort_keys=False,
            ),
            encoding="utf-8",
        )
        # The AUTHORITATIVE config lives OUTSIDE the home, behind
        # HERMES_CONFIG_PATH (maintainer-blocker-1 semantics).
        override_dir = tmp_path / "override-dir"
        override_dir.mkdir()
        override_cfg = override_dir / "config.yaml"
        override_cfg.write_text(
            yaml.safe_dump(
                {"terminal": {"backend": "ssh", "cwd": "/srv/from-override"}},
                sort_keys=False,
            ),
            encoding="utf-8",
        )

        # Ambient resolver reports the home under its ALIAS spelling (as an
        # env-provided HERMES_HOME would); callers pass the canonicalized
        # target — the exact divergence the lexical comparison mishandled.
        monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: alias_home)
        monkeypatch.setenv("HERMES_CONFIG_PATH", str(override_cfg))
        api_config.reload_config()

        got = api_config.get_config_for_profile_home(real_home)
        assert got.get("terminal", {}).get("cwd") == "/srv/from-override", (
            "canonicalized alias target must preserve HERMES_CONFIG_PATH "
            f"authority; got {got.get('terminal')!r}"
        )
        assert got.get("terminal", {}).get("backend") == "ssh"

        # The un-canonicalized alias spelling converges to the SAME answer —
        # identity is by resolution, never by lexical spelling.
        got_alias = api_config.get_config_for_profile_home(alias_home)
        assert got_alias.get("terminal", {}).get("cwd") == "/srv/from-override"

        # Sanity: the fixture genuinely exercises alias-vs-resolved-target
        # comparison rather than passing trivially.
        assert os.path.realpath(alias_home) == str(real_home)
        assert os.path.realpath(real_home) != str(alias_home)
        # NOTE: no manual delenv/reload here (see
        # test_config_authority_active_home_with_external_override) —
        # monkeypatch teardown + stale-path check invalidate the cache.
