"""
Regression coverage for issue #1458 Bug #3 diagnostics.

The residual HTTP-unhealthy wedge can leave the process alive and the port
listening while request handling no longer advances. /health must expose enough
cheap diagnostics for a watchdog to distinguish process liveness from request
path liveness, and /health?deep=1 must exercise a little more of the request
path without hanging forever on shared locks.
"""
import json
import urllib.request

from tests._pytest_port import BASE


def _get(path):
    with urllib.request.urlopen(BASE + path, timeout=10) as r:
        return json.loads(r.read()), r.status


def test_health_exposes_request_heartbeat_counter(cleanup_test_sessions):
    first, status = _get("/health")
    assert status == 200
    assert first["status"] == "ok"
    heartbeat = first["request_heartbeat"]
    assert isinstance(heartbeat["count"], int)
    assert heartbeat["count"] >= 1
    assert isinstance(heartbeat["last_at"], float)
    assert heartbeat["last_age_seconds"] >= 0

    second, status = _get("/health")
    assert status == 200
    assert second["request_heartbeat"]["count"] > heartbeat["count"]


def test_deep_health_exercises_session_store_and_stream_lock(cleanup_test_sessions):
    data, status = _get("/health?deep=1")
    assert status == 200
    assert data["status"] == "ok"
    assert data["deep"] is True

    session_store = data["diagnostics"]["session_store"]
    assert session_store["ok"] is True
    assert session_store["path_exists"] is True
    assert isinstance(session_store["session_file_count"], int)
    assert "index_readable" in session_store

    streams_lock = data["diagnostics"]["streams_lock"]
    assert streams_lock["ok"] is True
    assert streams_lock["acquired"] is True
    assert streams_lock["active_streams"] == data["active_streams"]
    assert streams_lock["wait_ms"] >= 0


def test_deep_health_stream_lock_probe_is_bounded(cleanup_test_sessions):
    from api import config as cfg
    from api.routes import _read_active_streams_bounded

    assert cfg.STREAMS_LOCK.acquire(blocking=False)
    try:
        result = _read_active_streams_bounded(timeout_seconds=0.01)
    finally:
        cfg.STREAMS_LOCK.release()

    assert result["ok"] is False
    assert result["acquired"] is False
    assert result["active_streams"] is None
    assert result["wait_ms"] < 250
