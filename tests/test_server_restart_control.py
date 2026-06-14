import json
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

from api import routes

REPO = Path(__file__).resolve().parents[1]


class CaptureHandler:
    def __init__(self, body=b"{}"):
        self.headers = {"Content-Length": str(len(body))}
        self.rfile = BytesIO(body)
        self.client_address = ("127.0.0.1", 12345)
        self.command = "POST"
        self.path = "/api/server/restart"
        self.responses = []
        self.sent_headers = []
        self.wfile = BytesIO()

    def send_response(self, status):
        self.status = status

    def send_header(self, key, value):
        self.sent_headers.append((key, value))

    def end_headers(self):
        pass


def _capture_json(monkeypatch):
    captured = {}

    def fake_j(handler, payload, status=200, extra_headers=None):
        captured["payload"] = payload
        captured["status"] = status
        captured["extra_headers"] = extra_headers or {}
        return True

    monkeypatch.setattr(routes, "j", fake_j)
    return captured


def test_restart_config_is_unavailable_without_env_command(monkeypatch, tmp_path):
    monkeypatch.delenv("HERMES_WEBUI_RESTART_COMMAND", raising=False)
    monkeypatch.setenv("HERMES_WEBUI_STATE_DIR", str(tmp_path))

    from api import server_restart

    cfg = server_restart.restart_config()

    assert cfg == {"enabled": False, "reason": "not_configured"}


def test_restart_config_is_enabled_from_env_without_exposing_raw_command(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_WEBUI_RESTART_COMMAND", "python -c 'print(42)'")
    monkeypatch.setenv("HERMES_WEBUI_STATE_DIR", str(tmp_path))

    from api import server_restart

    cfg = server_restart.restart_config()

    assert cfg["enabled"] is True
    assert cfg["strategy"] == "command"
    assert "command" not in cfg
    assert "python" not in json.dumps(cfg).lower()


def test_start_restart_refuses_when_not_configured(monkeypatch, tmp_path):
    monkeypatch.delenv("HERMES_WEBUI_RESTART_COMMAND", raising=False)
    monkeypatch.setenv("HERMES_WEBUI_STATE_DIR", str(tmp_path))

    from api import server_restart

    result = server_restart.start_guarded_restart()

    assert result["ok"] is False
    assert result["status"] == "unavailable"
    assert result["reason"] == "not_configured"


def test_start_restart_waits_for_active_runs_before_command(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_WEBUI_RESTART_COMMAND", "echo restart")
    monkeypatch.setenv("HERMES_WEBUI_STATE_DIR", str(tmp_path))

    from api import server_restart

    monkeypatch.setattr(server_restart, "restart_safety_snapshot", lambda: {"active_runs": 1, "active_streams": 0})
    spawned = []
    monkeypatch.setattr(server_restart, "_spawn_restart_worker", lambda status_id: spawned.append(status_id))

    result = server_restart.start_guarded_restart()

    assert result["ok"] is True
    assert result["status"] == "waiting_idle"
    assert result["active_runs"] == 1
    assert spawned == [result["restart_id"]]


def test_start_restart_persists_semantic_status_and_spawns_worker(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_WEBUI_RESTART_COMMAND", "echo restart")
    monkeypatch.setenv("HERMES_WEBUI_STATE_DIR", str(tmp_path))

    from api import server_restart

    monkeypatch.setattr(server_restart, "restart_safety_snapshot", lambda: {"active_runs": 0, "active_streams": 0})
    spawned = []
    monkeypatch.setattr(server_restart, "_spawn_restart_worker", lambda status_id: spawned.append(status_id))

    result = server_restart.start_guarded_restart(reason="settings")
    status = server_restart.read_restart_status(result["restart_id"])

    assert result["ok"] is True
    assert result["status"] == "restarting"
    assert result["restart_id"]
    assert spawned == [result["restart_id"]]
    assert status["status"] == "restarting"
    assert status["reason"] == "settings"
    assert "elapsed_seconds" in status


def test_waiting_worker_starts_command_after_idle(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_WEBUI_RESTART_COMMAND", "echo restart")
    monkeypatch.setenv("HERMES_WEBUI_STATE_DIR", str(tmp_path))

    from api import server_restart

    snapshots = iter([
        {"active_runs": 1, "active_streams": 0},
        {"active_runs": 0, "active_streams": 0},
    ])
    monkeypatch.setattr(server_restart, "restart_safety_snapshot", lambda: next(snapshots))
    monkeypatch.setattr(server_restart.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(server_restart, "_spawn_restart_worker", lambda _restart_id: None)
    commands = []
    monkeypatch.setattr(server_restart, "_run_restart_command", lambda restart_id: commands.append(restart_id))

    result = server_restart.start_guarded_restart(reason="settings")
    status_id = result["restart_id"]
    server_restart._write_status(
        {"ok": True, "restart_id": status_id, "status": "waiting_idle", "started_at": server_restart._now()},
        restart_id=status_id,
    )
    server_restart._restart_worker(status_id)
    status = server_restart.read_restart_status(status_id)

    assert result["status"] == "waiting_idle"
    assert commands == [status_id]
    assert status["status"] == "restarting"


def test_restart_config_treats_invalid_command_as_unavailable(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_WEBUI_RESTART_COMMAND", "python -c '")
    monkeypatch.setenv("HERMES_WEBUI_STATE_DIR", str(tmp_path))

    from api import server_restart

    assert server_restart.restart_config() == {"enabled": False, "reason": "not_configured"}
    assert server_restart.start_guarded_restart()["status"] == "unavailable"


def test_restart_command_nonzero_marks_failed(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_WEBUI_RESTART_COMMAND", "false")
    monkeypatch.setenv("HERMES_WEBUI_STATE_DIR", str(tmp_path))

    from api import server_restart

    server_restart._write_status(
        {"ok": True, "restart_id": "nonzero", "status": "restarting", "started_at": server_restart._BOOT_TIME + 1},
        restart_id="nonzero",
    )
    monkeypatch.setattr(server_restart.subprocess, "run", lambda *_args, **_kwargs: SimpleNamespace(returncode=42))

    server_restart._run_restart_command("nonzero")
    status = server_restart.read_restart_status("nonzero")

    assert status["status"] == "failed"
    assert status["ok"] is False
    assert status["exit_code"] == 42


def test_restart_command_stays_restarting_while_command_runs(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_WEBUI_RESTART_COMMAND", "echo restart")
    monkeypatch.setenv("HERMES_WEBUI_STATE_DIR", str(tmp_path))

    from api import server_restart

    server_restart._write_status(
        {"ok": True, "restart_id": "running-command", "status": "restarting", "started_at": server_restart._BOOT_TIME + 1},
        restart_id="running-command",
    )
    observed_statuses = []

    def fake_run(*_args, **_kwargs):
        observed_statuses.append(server_restart.read_restart_status("running-command")["status"])
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(server_restart.subprocess, "run", fake_run)

    server_restart._run_restart_command("running-command")
    status = server_restart.read_restart_status("running-command")

    assert observed_statuses == ["restarting"]
    assert status["status"] == "checking_health"
    assert "command_started_at" in status
    assert "checking_started_at" in status


def test_restart_command_success_waits_for_new_process_before_succeeded(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_WEBUI_RESTART_COMMAND", "echo restart")
    monkeypatch.setenv("HERMES_WEBUI_STATE_DIR", str(tmp_path))

    from api import server_restart

    server_restart._write_status(
        {"ok": True, "restart_id": "same-process", "status": "restarting", "started_at": server_restart._BOOT_TIME + 1},
        restart_id="same-process",
    )
    monkeypatch.setattr(server_restart.subprocess, "run", lambda *_args, **_kwargs: SimpleNamespace(returncode=0))

    server_restart._run_restart_command("same-process")
    status = server_restart.read_restart_status("same-process")

    assert status["status"] == "checking_health"
    assert status["exit_code"] == 0
    assert "checking_started_at" in status


def test_checking_health_times_out_when_restart_not_observed(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_WEBUI_STATE_DIR", str(tmp_path))

    from api import server_restart

    server_restart._write_status(
        {
            "ok": True,
            "restart_id": "stale-check",
            "status": "checking_health",
            "started_at": server_restart._BOOT_TIME + 1,
            "checking_started_at": server_restart._now() - server_restart._HEALTH_CONFIRM_TIMEOUT_SECONDS - 1,
        },
        restart_id="stale-check",
    )

    status = server_restart.read_restart_status("stale-check")

    assert status["status"] == "failed"
    assert status["ok"] is False
    assert status["error"] == "restart_not_observed"


def test_single_flight_reuses_existing_in_progress_restart(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_WEBUI_RESTART_COMMAND", "echo restart")
    monkeypatch.setenv("HERMES_WEBUI_STATE_DIR", str(tmp_path))

    from api import server_restart

    monkeypatch.setattr(server_restart, "restart_safety_snapshot", lambda: {"active_runs": 0, "active_streams": 0})
    spawned = []
    monkeypatch.setattr(server_restart, "_spawn_restart_worker", lambda status_id: spawned.append(status_id))

    first = server_restart.start_guarded_restart(reason="settings")
    second = server_restart.start_guarded_restart(reason="settings")

    assert first["status"] == "restarting"
    assert second["restart_id"] == first["restart_id"]
    assert second["already_in_progress"] is True
    assert spawned == [first["restart_id"]]


def test_status_writes_are_atomic_and_leave_no_temp_files(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_WEBUI_STATE_DIR", str(tmp_path))

    from api import server_restart

    server_restart._write_status({"restart_id": "atomic", "status": "idle"}, restart_id="atomic")

    status_dir = tmp_path / "server_restart"
    assert (status_dir / "atomic.json").exists()
    assert (status_dir / "latest.json").exists()
    assert list(status_dir.glob("*.tmp")) == []


def test_read_restart_status_only_promotes_status_from_previous_process(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_WEBUI_STATE_DIR", str(tmp_path))

    from api import server_restart

    current = server_restart._write_status(
        {"ok": True, "restart_id": "current", "status": "checking_health", "started_at": server_restart._BOOT_TIME + 1},
        restart_id="current",
    )
    assert server_restart.read_restart_status("current")["status"] == "checking_health"

    previous = server_restart._write_status(
        {"ok": True, "restart_id": "previous", "status": "checking_health", "started_at": server_restart._BOOT_TIME - 1},
        restart_id="previous",
    )
    assert current["restart_id"] == "current"
    assert previous["restart_id"] == "previous"
    assert server_restart.read_restart_status("previous")["status"] == "succeeded"


def test_stale_waiting_idle_from_previous_process_becomes_terminal(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_WEBUI_RESTART_COMMAND", "echo restart")
    monkeypatch.setenv("HERMES_WEBUI_STATE_DIR", str(tmp_path))

    from api import server_restart

    server_restart._write_status(
        {"ok": True, "restart_id": "stale-wait", "status": "waiting_idle", "started_at": server_restart._BOOT_TIME - 1},
        restart_id="stale-wait",
    )

    status = server_restart.read_restart_status("stale-wait")
    assert status["status"] == "failed"
    assert status["ok"] is False
    assert status["error"] == "restart_interrupted_before_idle"
    spawned = []
    monkeypatch.setattr(server_restart, "restart_safety_snapshot", lambda: {"active_runs": 0, "active_streams": 0})
    monkeypatch.setattr(server_restart, "_spawn_restart_worker", lambda status_id: spawned.append(status_id))
    restarted = server_restart.start_guarded_restart(reason="settings")
    assert restarted["status"] == "restarting"
    assert restarted["restart_id"] != "stale-wait"
    assert spawned == [restarted["restart_id"]]


def test_restart_routes_are_wired(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_WEBUI_RESTART_COMMAND", "echo restart")
    monkeypatch.setenv("HERMES_WEBUI_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(routes, "_check_csrf", lambda _handler: True)
    from api import server_restart
    monkeypatch.setattr(server_restart, "_spawn_restart_worker", lambda _restart_id: None)
    captured = _capture_json(monkeypatch)

    assert routes.handle_post(CaptureHandler(), SimpleNamespace(path="/api/server/restart")) is True

    assert captured["status"] == 200
    assert captured["payload"]["ok"] is True
    assert captured["payload"]["status"] in {"restarting", "checking_health", "succeeded", "waiting_idle"}


def test_restart_post_is_csrf_protected(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_WEBUI_RESTART_COMMAND", "echo restart")
    monkeypatch.setenv("HERMES_WEBUI_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(routes, "_check_csrf", lambda _handler: False)
    monkeypatch.setattr(routes, "_csrf_rejection_error", lambda _handler: "Cross-origin request rejected")
    captured = _capture_json(monkeypatch)

    assert routes.handle_post(CaptureHandler(), SimpleNamespace(path="/api/server/restart")) is True

    assert captured["status"] == 403
    assert captured["payload"] == {"error": "Cross-origin request rejected"}


def test_settings_restart_ui_uses_i18n_and_status_hooks():
    html = (REPO / "static" / "index.html").read_text(encoding="utf-8")
    boot = (REPO / "static" / "boot.js").read_text(encoding="utf-8")
    i18n = (REPO / "static" / "i18n.js").read_text(encoding="utf-8")

    assert 'id="serverControlsBlock"' in html
    assert 'id="btnRestartServer"' in html
    assert 'id="restartServerStatus"' in html
    assert 'onclick="restartServer()"' in html
    assert 'data-i18n="settings_label_server_controls"' in html
    assert 'data-i18n="settings_btn_restart_server"' in html
    assert 'data-i18n="settings_desc_restart_server"' in html
    assert 'data-i18n="settings_restart_status_idle"' in html

    assert "function restartServer(" in boot
    assert "/api/server/restart" in boot
    assert "/api/server/restart/status" in boot
    assert "settings_restart_status_" in boot
    poll_slice = boot[boot.index("async function _pollRestartStatus("):boot.index("async function restartServer()")]
    terminal_slice = poll_slice[poll_slice.index("status.status === 'failed'"):poll_slice.index("} catch (_)")]
    assert "btnRestartServer" in terminal_slice
    assert "btn.disabled = false" in terminal_slice

    locale_count = i18n.count("sign_out:")
    assert locale_count > 0
    for key in (
        "settings_label_server_controls",
        "settings_desc_restart_server",
        "settings_btn_restart_server",
        "settings_restart_status_idle",
        "settings_restart_status_waiting_idle",
        "settings_restart_status_restarting",
        "settings_restart_status_checking_health",
        "settings_restart_status_succeeded",
        "settings_restart_status_failed",
        "settings_restart_status_unavailable",
        "settings_restart_not_configured",
    ):
        assert i18n.count(f"{key}:") == locale_count

    server_block = html[html.index('id="serverControlsBlock"'):html.index('id="passkeysSettingsBlock"')]
    restart_slice = boot[boot.index('function _restartText('):boot.index('async function shutdownServer()')]
    touched = "\n".join([server_block, restart_slice])
    assert "/home/manfred" not in touched
    assert "tars" not in touched.lower()
    assert "systemctl" not in touched.lower()
