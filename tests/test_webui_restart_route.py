from __future__ import annotations

import io
import json
from urllib.parse import urlparse

from api import routes


class _FakeHandler:
    def __init__(self):
        self.status = None
        self.headers = {}
        self.wfile = io.BytesIO()

    def send_response(self, status):
        self.status = status

    def send_header(self, *_args):
        pass

    def end_headers(self):
        pass


def _payload(handler: _FakeHandler) -> dict:
    return json.loads(handler.wfile.getvalue().decode("utf-8"))


def _call_restart(monkeypatch):
    monkeypatch.setattr(routes, "_check_csrf", lambda _handler: True)
    handler = _FakeHandler()
    assert routes.handle_post(handler, urlparse("/api/restart")) is True
    return handler, _payload(handler)


def test_restart_route_rejects_unmanaged_webui_without_scheduling_helper(monkeypatch):
    monkeypatch.setattr(routes, "_is_ctl_managed_webui", lambda: False)
    scheduled = []
    monkeypatch.setattr(routes, "_schedule_webui_restart", lambda: scheduled.append(True))

    handler, payload = _call_restart(monkeypatch)

    assert handler.status == 409
    assert payload["status"] == "unsupported"
    assert scheduled == []


def test_restart_route_acknowledges_ctl_managed_webui_and_schedules_once(monkeypatch):
    monkeypatch.setattr(routes, "_is_ctl_managed_webui", lambda: True)
    scheduled = []
    monkeypatch.setattr(routes, "_schedule_webui_restart", lambda: scheduled.append(True))

    try:
        handler, payload = _call_restart(monkeypatch)
    finally:
        routes._WEBUI_RESTART_LOCK.release()

    assert handler.status == 200
    assert payload == {"status": "restarting"}
    assert scheduled == [True]


def test_restart_route_rejects_concurrent_restart_without_scheduling_helper(monkeypatch):
    monkeypatch.setattr(routes, "_is_ctl_managed_webui", lambda: True)
    scheduled = []
    monkeypatch.setattr(routes, "_schedule_webui_restart", lambda: scheduled.append(True))

    assert routes._WEBUI_RESTART_LOCK.acquire(blocking=False)
    try:
        handler, payload = _call_restart(monkeypatch)
    finally:
        routes._WEBUI_RESTART_LOCK.release()

    assert handler.status == 429
    assert payload["status"] == "busy"
    assert scheduled == []


def test_ctl_managed_webui_requires_current_process_pid_in_pid_file(monkeypatch, tmp_path):
    pid_file = tmp_path / "webui.pid"
    monkeypatch.setenv("HERMES_WEBUI_PID_FILE", str(pid_file))
    monkeypatch.setattr(routes.os, "getpid", lambda: 4242)

    pid_file.write_text("4242\n", encoding="utf-8")
    assert routes._is_ctl_managed_webui() is True

    pid_file.write_text("4243\n", encoding="utf-8")
    assert routes._is_ctl_managed_webui() is False

    pid_file.write_text("not-a-pid\n", encoding="utf-8")
    assert routes._is_ctl_managed_webui() is False
