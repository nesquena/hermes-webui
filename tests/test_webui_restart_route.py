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
    monkeypatch.setattr(routes, "_ctl_can_restart_webui", lambda _pid: False)
    scheduled = []
    monkeypatch.setattr(routes, "_schedule_webui_restart", lambda: scheduled.append(True))

    handler, payload = _call_restart(monkeypatch)

    assert handler.status == 409
    assert payload["status"] == "unsupported"
    assert scheduled == []


def test_restart_route_acknowledges_ctl_managed_webui_and_schedules_once(monkeypatch):
    monkeypatch.setattr(routes, "_ctl_can_restart_webui", lambda _pid: True)
    monkeypatch.setattr(routes.os, "getpid", lambda: 4242)
    scheduled = []

    def schedule(pid):
        scheduled.append(pid)
        return True

    monkeypatch.setattr(routes, "_schedule_webui_restart", schedule)

    try:
        handler, payload = _call_restart(monkeypatch)
    finally:
        routes._WEBUI_RESTART_LOCK.release()

    assert handler.status == 200
    assert payload == {"status": "restart_scheduled"}
    assert scheduled == [4242]


def test_restart_route_rejects_concurrent_restart_without_scheduling_helper(monkeypatch):
    monkeypatch.setattr(routes, "_ctl_can_restart_webui", lambda _pid: True)
    scheduled = []
    monkeypatch.setattr(routes, "_schedule_webui_restart", lambda _pid: scheduled.append(True))

    assert routes._WEBUI_RESTART_LOCK.acquire(blocking=False)
    try:
        handler, payload = _call_restart(monkeypatch)
    finally:
        routes._WEBUI_RESTART_LOCK.release()

    assert handler.status == 429
    assert payload["status"] == "busy"
    assert scheduled == []


def test_restart_route_refuses_windows_handoff_before_it_can_stop_the_server(monkeypatch):
    monkeypatch.setattr(routes, "_is_windows_platform", lambda: True)
    monkeypatch.setattr(routes, "_ctl_can_restart_webui", lambda _pid: True)
    scheduled = []
    monkeypatch.setattr(routes, "_schedule_webui_restart", lambda _pid: scheduled.append(True) or True)

    handler, payload = _call_restart(monkeypatch)

    assert handler.status == 409
    assert payload["status"] == "unsupported"
    assert scheduled == []


def test_restart_helper_releases_lock_when_ctl_cannot_be_spawned(monkeypatch):
    monkeypatch.setattr(routes.subprocess, "Popen", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("no bash")))

    assert routes._WEBUI_RESTART_LOCK.acquire(blocking=False)
    assert routes._schedule_webui_restart(4242) is False

    assert routes._WEBUI_RESTART_LOCK.acquire(blocking=False), "failed helper must not permanently block retries"
    routes._WEBUI_RESTART_LOCK.release()


def test_restart_helper_passes_expected_pid_and_releases_lock_after_nonzero_exit(monkeypatch):
    launched = {}

    class _Process:
        def wait(self):
            return 7

    class _InlineThread:
        def __init__(self, *, target, **_kwargs):
            self.target = target

        def start(self):
            self.target()

    monkeypatch.setattr(routes.threading, "Thread", _InlineThread)
    monkeypatch.setattr(routes.subprocess, "Popen", lambda args, **kwargs: launched.update(args=args, kwargs=kwargs) or _Process())
    monkeypatch.setattr(routes.os, "getpid", lambda: 4242)
    monkeypatch.setattr(routes.time, "sleep", lambda _seconds: None)

    assert routes._WEBUI_RESTART_LOCK.acquire(blocking=False)
    assert routes._schedule_webui_restart(4242) is True

    assert launched["args"][-2:] == ["--expected-pid", "4242"]
    assert routes._WEBUI_RESTART_LOCK.acquire(blocking=False), "nonzero helper must not permanently block retries"
    routes._WEBUI_RESTART_LOCK.release()
