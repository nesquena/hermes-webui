from pathlib import Path

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
    """_resolve_profile_home_param handles None, default, profile name, Path, and path string."""
    from api.profiles import _DEFAULT_HERMES_HOME

    assert workspace._resolve_profile_home_param(None) == _DEFAULT_HERMES_HOME
    assert workspace._resolve_profile_home_param("default") == _DEFAULT_HERMES_HOME
    assert workspace._resolve_profile_home_param(str(tmp_path / "profiles/custom")) == (tmp_path / "profiles/custom")
    assert workspace._resolve_profile_home_param(tmp_path / "profiles/custom") == (tmp_path / "profiles/custom")


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






