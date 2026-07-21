import logging
from pathlib import Path

import pytest


class _ServerSentinel(RuntimeError):
    pass


def _prepare_main(monkeypatch, tmp_path: Path, cleanup):
    import api.auth
    import api.background_process
    import api.config
    import api.crash_visibility
    import api.gateway_watcher
    import api.plugins
    import api.session_lifecycle
    import api.session_recovery
    import server

    state = tmp_path / "state"
    sessions = state / "sessions"
    workspace = tmp_path / "workspace"
    monkeypatch.setattr(server, "STATE_DIR", state)
    monkeypatch.setattr(server, "SESSION_DIR", sessions)
    monkeypatch.setattr(server, "DEFAULT_WORKSPACE", workspace)
    monkeypatch.setattr(api.config, "STATE_DIR", state)
    monkeypatch.setattr(server, "install_crash_visibility", lambda: None)
    monkeypatch.setattr(server, "fix_credential_permissions", lambda: None)
    monkeypatch.setattr(server, "_raise_fd_soft_limit", lambda: {"status": "unchanged"})
    monkeypatch.setattr(server, "_abort_if_already_serving", lambda *args: None)
    monkeypatch.setattr(api.config, "print_startup_config", lambda: None)
    monkeypatch.setattr(api.config, "verify_hermes_imports", lambda: (True, [], {}))
    monkeypatch.setattr(api.auth, "is_auth_enabled", lambda: True)
    monkeypatch.setattr(api.auth, "get_oidc_startup_warning", lambda: None)
    monkeypatch.setattr(api.session_recovery, "recover_all_sessions_on_startup", lambda *a, **k: {})
    monkeypatch.setattr(api.gateway_watcher, "start_watcher", lambda: None)
    monkeypatch.setattr(api.background_process, "start_drain_thread", lambda: False)
    monkeypatch.setattr(api.background_process, "start_session_channel_reaper", lambda: False)
    monkeypatch.setattr(api.plugins, "load_plugins", lambda: None)
    monkeypatch.setattr(api.gateway_watcher, "stop_watcher", lambda: None)
    monkeypatch.setattr(api.session_lifecycle, "drain_all_on_shutdown", lambda: None)
    monkeypatch.setattr(api.background_process, "stop_drain_thread", lambda: None)
    monkeypatch.setattr(api.background_process, "stop_session_channel_reaper", lambda: None)
    monkeypatch.setattr(server, "_log_shutdown_audit", lambda *args, **kwargs: None)
    monkeypatch.setattr("api.wallpaper.cleanup_wallpaper_orphans", cleanup)
    return server, state, sessions, workspace


def test_wallpaper_startup_cleanup_runs_after_state_setup_before_server(
    monkeypatch, tmp_path: Path
) -> None:
    events = []

    def _cleanup():
        assert state.is_dir()
        assert sessions.is_dir()
        assert workspace.is_dir()
        events.append("cleanup")

    server, state, sessions, workspace = _prepare_main(monkeypatch, tmp_path, _cleanup)

    def _server(*args, **kwargs):
        events.append("server")
        raise _ServerSentinel

    monkeypatch.setattr(server, "QuietHTTPServer", _server)

    with pytest.raises(_ServerSentinel):
        server.main()

    assert events == ["cleanup", "server"]


@pytest.mark.parametrize("cleanup_fails", [False, True])
def test_wallpaper_startup_cleanup_precedes_construction_and_reaches_serve_forever(
    monkeypatch, tmp_path: Path, cleanup_fails: bool
) -> None:
    events = []

    def _cleanup():
        events.append("cleanup")
        if cleanup_fails:
            raise OSError("cleanup failed")

    server, _, _, _ = _prepare_main(monkeypatch, tmp_path, _cleanup)

    class _ServingSentinel:
        def __init__(self, *args, **kwargs):
            events.append("construct")

        def serve_forever(self):
            events.append("serve_forever")
            raise _ServerSentinel

        def server_close(self):
            events.append("server_close")

    monkeypatch.setattr(server, "QuietHTTPServer", _ServingSentinel)

    with pytest.raises(_ServerSentinel):
        server.main()

    assert events == ["cleanup", "construct", "serve_forever", "server_close"]


def test_wallpaper_startup_cleanup_failure_logs_path_free_and_continues(
    monkeypatch, tmp_path: Path, caplog
) -> None:
    secret_path = tmp_path / "must-not-log"

    def _cleanup():
        raise OSError(f"failed at {secret_path}")

    server, _, _, _ = _prepare_main(monkeypatch, tmp_path, _cleanup)
    monkeypatch.setattr(
        server,
        "QuietHTTPServer",
        lambda *args, **kwargs: (_ for _ in ()).throw(_ServerSentinel()),
    )

    with caplog.at_level(logging.WARNING), pytest.raises(_ServerSentinel):
        server.main()

    assert "Wallpaper startup cleanup failed" in caplog.text
    assert str(secret_path) not in caplog.text
