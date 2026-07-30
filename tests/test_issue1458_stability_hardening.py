"""Regression coverage for issue #1458 persistent-host hardening."""
import json
import urllib.request

from tests._pytest_port import BASE


def _get(path):
    with urllib.request.urlopen(BASE + path, timeout=10) as r:
        return json.loads(r.read()), r.status


def test_health_exposes_accept_loop_heartbeat():
    data, status = _get("/health")

    assert status == 200
    heartbeat = data.get("accept_loop")
    assert isinstance(heartbeat, dict)
    assert isinstance(heartbeat.get("requests_total"), int)
    assert heartbeat["requests_total"] >= 1
    assert isinstance(heartbeat.get("last_request_at"), (int, float))
    assert heartbeat["last_request_at"] > 0


def test_deep_health_exercises_session_project_and_sqlite_paths():
    data, status = _get("/health?deep=1")

    assert status == 200
    assert data["status"] == "ok"
    checks = data.get("checks")
    assert isinstance(checks, dict)
    assert checks["streams_lock"]["status"] == "ok"
    assert isinstance(checks["streams_lock"].get("active_streams"), int)
    assert checks["sessions"]["status"] == "ok"
    assert isinstance(checks["sessions"].get("count"), int)
    assert checks["projects"]["status"] == "ok"
    assert isinstance(checks["projects"].get("count"), int)
    # The isolated test home may not have a Hermes state.db yet. Deep health
    # should still report the state-db probe explicitly so watchdogs can tell
    # whether sqlite was checked or absent.
    assert checks["state_db"]["status"] in {"ok", "missing"}


def test_server_raises_fd_soft_limit_when_resource_allows(monkeypatch):
    import server

    calls = []

    class FakeResource:
        RLIMIT_NOFILE = object()

        @staticmethod
        def getrlimit(which):
            return (256, 8192)

        @staticmethod
        def setrlimit(which, limits):
            calls.append((which, limits))

    monkeypatch.setattr(server, "resource", FakeResource, raising=False)

    result = server._raise_fd_soft_limit(target=4096)

    assert result["status"] == "raised"
    assert result["soft"] == 4096
    assert calls == [(FakeResource.RLIMIT_NOFILE, (4096, 8192))]
