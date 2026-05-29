import logging
import types
import threading


def test_server_shutdown_audit_logs_active_stream_context(monkeypatch, caplog):
    import server
    from api import models

    monkeypatch.setattr(server, "_SHUTDOWN_AUDIT_LOGGED", False)
    monkeypatch.setitem(
        models.SESSIONS,
        "session-1\nforged",
        types.SimpleNamespace(active_stream_id="stream-1\rforged", pending_user_message="hello"),
    )
    monkeypatch.setitem(
        models.SESSIONS,
        "session-2",
        types.SimpleNamespace(active_stream_id=None, pending_user_message=None),
    )

    caplog.set_level(logging.INFO, logger="server")
    server._log_shutdown_audit(reason="test-exit")

    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert "[shutdown-audit]" in logged
    assert "reason=test-exit" in logged
    assert "sid=session-1?forged stream=stream-1?forged pending=True" in logged
    assert "session-1\nforged" not in logged
    assert "stream-1\rforged" not in logged
    assert "session-2" not in logged


def test_shutdown_route_logs_request_context_without_starting_real_shutdown(monkeypatch, caplog):
    from api import routes

    responses = []
    monkeypatch.setattr(routes, "j", lambda handler, payload, **kw: responses.append(payload) or True)

    started_threads = []

    class FakeThread:
        def __init__(self, target, daemon=False):
            self.target = target
            self.daemon = daemon

        def start(self):
            started_threads.append((self.target, self.daemon))

    monkeypatch.setattr(threading, "Thread", FakeThread)

    handler = types.SimpleNamespace(
        client_address=("127.0.0.1", 12345),
        command="POST",
        path="/api/shutdown\nforged",
        headers={"User-Agent": "pytest-agent\r\nforged"},
    )

    caplog.set_level(logging.INFO, logger="api.routes")
    assert routes._handle_shutdown(handler) is True

    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert "[shutdown-request]" in logged
    assert "remote=127.0.0.1" in logged
    assert "method=POST" in logged
    assert "path=/api/shutdown?forged" in logged
    assert "ua=pytest-agent?forged" in logged
    assert "/api/shutdown\nforged" not in logged
    assert "pytest-agent\r\nforged" not in logged
    assert responses == [{"status": "shutting_down"}]
    assert started_threads and started_threads[0][1] is True
