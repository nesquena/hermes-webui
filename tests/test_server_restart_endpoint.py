from io import BytesIO
from pathlib import Path
from types import SimpleNamespace


def _restart_blocker_snapshot(active_streams=0, active_runs=0):
    return {
        "active_streams": active_streams,
        "active_runs": active_runs,
        "blocking_stream_ids": ["stream-1"] if active_streams else [],
        "blocking_run_ids": ["run-1"] if active_runs else [],
        "restart_blocked": bool(active_streams or active_runs),
    }


def test_restart_endpoint_returns_status_and_schedules_restart(monkeypatch):
    import api.routes as routes

    calls = []
    responses = []

    monkeypatch.setattr(
        routes,
        "j",
        lambda handler, payload, status=200, **_: responses.append((status, payload)) or True,
    )
    monkeypatch.setattr(
        routes,
        "_check_csrf",
        lambda handler: True,
    )
    monkeypatch.setattr("api.updates._restart_blocker_snapshot", lambda: _restart_blocker_snapshot())

    def fake_schedule_restart(delay=2.0, force=False):
        calls.append((delay, force))

    monkeypatch.setattr(routes, "_schedule_server_restart", fake_schedule_restart, raising=False)

    handler = SimpleNamespace(headers={}, client_address=("127.0.0.1", 12345), command="POST", path="/api/restart")
    handled = routes.handle_post(handler, SimpleNamespace(path="/api/restart", query=""))

    assert handled is True
    assert calls == [(0.3, False)]
    assert responses == [(200, {"status": "restarting", "forced": False})]


def test_restart_endpoint_blocks_when_chat_stream_is_active(monkeypatch):
    import api.routes as routes

    calls = []
    responses = []

    monkeypatch.setattr(
        routes,
        "j",
        lambda handler, payload, status=200, **_: responses.append((status, payload)) or True,
    )
    monkeypatch.setattr(
        routes,
        "_check_csrf",
        lambda handler: True,
    )
    monkeypatch.setattr("api.updates._restart_blocker_snapshot", lambda: _restart_blocker_snapshot(active_streams=2))
    monkeypatch.setattr(routes, "_schedule_server_restart", lambda delay=2.0, force=False: calls.append((delay, force)))

    handler = SimpleNamespace(headers={}, client_address=("127.0.0.1", 12345), command="POST", path="/api/restart")
    handled = routes.handle_post(handler, SimpleNamespace(path="/api/restart", query=""))

    assert handled is True
    assert calls == []
    assert responses[0][0] == 409
    assert responses[0][1]["requires_confirmation"] is True
    assert responses[0][1]["restart_blocked"] is True
    assert responses[0][1]["active_streams"] == 2


def test_restart_endpoint_blocks_when_agent_run_is_active(monkeypatch):
    import api.routes as routes

    calls = []
    responses = []

    monkeypatch.setattr(
        routes,
        "j",
        lambda handler, payload, status=200, **_: responses.append((status, payload)) or True,
    )
    monkeypatch.setattr(
        routes,
        "_check_csrf",
        lambda handler: True,
    )
    monkeypatch.setattr("api.updates._restart_blocker_snapshot", lambda: _restart_blocker_snapshot(active_runs=1))
    monkeypatch.setattr(routes, "_schedule_server_restart", lambda delay=2.0, force=False: calls.append((delay, force)))

    handler = SimpleNamespace(headers={}, client_address=("127.0.0.1", 12345), command="POST", path="/api/restart")
    handled = routes.handle_post(handler, SimpleNamespace(path="/api/restart", query=""))

    assert handled is True
    assert calls == []
    assert responses[0][0] == 409
    assert responses[0][1]["requires_confirmation"] is True
    assert responses[0][1]["restart_blocked"] is True
    assert responses[0][1]["active_runs"] == 1


def test_restart_endpoint_force_schedules_restart_with_active_work(monkeypatch):
    import api.routes as routes

    calls = []
    responses = []

    monkeypatch.setattr(
        routes,
        "j",
        lambda handler, payload, status=200, **_: responses.append((status, payload)) or True,
    )
    monkeypatch.setattr(
        routes,
        "_check_csrf",
        lambda handler: True,
    )
    monkeypatch.setattr("api.updates._restart_blocker_snapshot", lambda: _restart_blocker_snapshot(active_streams=1, active_runs=1))
    monkeypatch.setattr(routes, "_schedule_server_restart", lambda delay=2.0, force=False: calls.append((delay, force)))

    body = b'{"force": true}'
    handler = SimpleNamespace(
        headers={"Content-Length": str(len(body))},
        rfile=BytesIO(body),
        client_address=("127.0.0.1", 12345),
        command="POST",
        path="/api/restart",
    )
    handled = routes.handle_post(handler, SimpleNamespace(path="/api/restart", query=""))

    assert handled is True
    assert calls == [(0.3, True)]
    assert responses == [(200, {"status": "restarting", "forced": True})]


def test_schedule_restart_has_real_force_path():
    src = Path(__file__).resolve().parents[1].joinpath("api", "updates.py").read_text(encoding="utf-8")

    assert "def _schedule_restart(delay: float = 2.0, force: bool = False)" in src
    assert "if force:" in src
    assert "_wait_until_restart_safe()" in src
    assert "forcing WebUI restart without waiting for active work" in src
