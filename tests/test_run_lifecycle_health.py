"""Regression coverage for restart-safety run lifecycle reporting."""

import json
import threading
import time
import urllib.error
import urllib.request


class _TestPermit:
    def __init__(self):
        self.acquired = True

    def release(self):
        self.acquired = False


def test_health_counts_active_runs_even_when_no_sse_streams():
    """A worker run can outlive its SSE channel; health must expose the run."""
    from api import config, routes

    with config.STREAMS_LOCK:
        config.STREAMS.clear()
    with config.ACTIVE_RUNS_LOCK:
        config.ACTIVE_RUNS.clear()
        config.ACTIVE_RUNS["stream-1"] = {
            "stream_id": "stream-1",
            "session_id": "session-1",
            "workspace": "/private/workspace",
            "started_at": time.time() - 42,
            "phase": "running",
        }

    try:
        stream_check = routes._streams_lock_health()
        run_check = routes._run_lifecycle_health()

        assert stream_check["active_streams"] == 0
        assert run_check["active_runs"] == 1
        assert run_check["oldest_run_age_seconds"] >= 40
        run = run_check["runs"][0]
        assert set(run) == {"started_at", "phase", "age_seconds"}
        assert run["phase"] == "running"
        json.dumps(run)
    finally:
        with config.ACTIVE_RUNS_LOCK:
            config.ACTIVE_RUNS.clear()


def test_health_http_stays_serializable_while_turn_admission_is_registered():
    """A parked admitted turn must not make the real /health route return 500."""
    from api import config
    from api.session_lineage import TurnAdmission, release_turn_admission
    from server import Handler, QuietHTTPServer

    stream_id = "health-admitted-stream"
    permit = _TestPermit()
    admission = TurnAdmission.create_for_test(
        stream_id=stream_id,
        root_session_id="health-root",
        delivery_session_id="health-tip",
        permit=permit,
    )
    admission.admitted.set()
    config.register_stream_owner(stream_id, admission.delivery_session_id)
    with config.ACTIVE_RUNS_LOCK:
        config.ACTIVE_RUNS[stream_id] = {
            "stream_id": stream_id,
            "lineage_id": admission.root_session_id,
            "delivery_session_id": admission.delivery_session_id,
            "owner_token": admission.owner_token,
            "admission": admission,
            "permit": admission.permit,
            "session_id": admission.delivery_session_id,
            "started_at": time.time() - 2,
            "phase": "parked",
            "workspace": "/private/workspace",
            "model": "test/model",
            "provider": "test-provider",
            "ephemeral": False,
            "backend": "local",
            "latest_tool": "terminal",
        }
        assert config.ACTIVE_RUNS[stream_id]["admission"] is admission

    httpd = QuietHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{httpd.server_port}/health"
        try:
            with urllib.request.urlopen(url, timeout=10) as response:
                status = response.status
                payload = json.loads(response.read())
        except urllib.error.HTTPError as exc:
            status = exc.code
            payload = json.loads(exc.read())

        assert status == 200
        assert payload["status"] == "ok"
        assert payload["active_runs"] == 1
        assert payload["oldest_run_age_seconds"] >= 1
        assert set(payload["runs"][0]) == {
            "started_at",
            "phase",
            "model",
            "provider",
            "ephemeral",
            "backend",
            "latest_tool",
            "age_seconds",
        }
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)
        release_turn_admission(admission)


def test_run_lifecycle_health_uses_an_explicit_public_field_allowlist():
    """Future internal row objects cannot become health response fields."""
    from api import config, routes

    with config.ACTIVE_RUNS_LOCK:
        config.ACTIVE_RUNS.clear()
        config.ACTIVE_RUNS["stream-private-fields"] = {
            "stream_id": "stream-private-fields",
            "session_id": "private-session",
            "workspace": "/private/workspace",
            "lineage_id": "private-root",
            "delivery_session_id": "private-tip",
            "owner_token": "private-owner-token",
            "admission": object(),
            "permit": object(),
            "future_private_value": object(),
            "started_at": time.time(),
            "phase": "running",
            "model": "test/model",
            "provider": "test-provider",
            "ephemeral": True,
            "backend": "gateway",
            "latest_tool": "terminal",
        }

    try:
        run = routes._run_lifecycle_health()["runs"][0]

        assert set(run) == {
            "started_at",
            "phase",
            "model",
            "provider",
            "ephemeral",
            "backend",
            "latest_tool",
            "age_seconds",
        }
        json.dumps(run)
    finally:
        with config.ACTIVE_RUNS_LOCK:
            config.ACTIVE_RUNS.clear()


def test_run_registry_unregister_records_last_finished_time():
    """Guards need a grace window after the last real worker exits."""
    from api import config

    with config.ACTIVE_RUNS_LOCK:
        config.ACTIVE_RUNS.clear()
        config.LAST_RUN_FINISHED_AT = None
    config.register_stream_owner("stream-2", "session-2")

    config.register_active_run("stream-2", session_id="session-2", phase="starting")
    with config.ACTIVE_RUNS_LOCK:
        assert "stream-2" in config.ACTIVE_RUNS
    assert config.stream_owner_session_id("stream-2") == "session-2"

    config.unregister_active_run("stream-2")

    with config.ACTIVE_RUNS_LOCK:
        assert "stream-2" not in config.ACTIVE_RUNS
        assert isinstance(config.LAST_RUN_FINISHED_AT, float)
    assert config.stream_owner_session_id("stream-2") is None
