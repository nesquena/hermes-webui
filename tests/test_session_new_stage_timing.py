"""POST /api/session/new emits one per-stage [SLOW] line only when slow."""

from __future__ import annotations

import time as _real_time
from urllib.parse import urlparse

import api.routes as routes

STAGES = (
    "total", "prev_check", "workspace", "worktree", "prep",
    "new_session", "publish", "projection", "json_write",
)


class _Session:
    session_id = "stagetiming"
    profile = None
    messages: list = []

    def compact(self):
        return {"session_id": self.session_id}


class _Clock:
    """Delegates to ``time`` but advances monotonic() by ``step`` per call."""

    def __init__(self, step):
        self._now, self._step = 0.0, step

    def monotonic(self):
        self._now += self._step
        return self._now

    def __getattr__(self, name):
        return getattr(_real_time, name)


def _drive(monkeypatch, step):
    printed, cap = [], {}
    handler = type("_H", (), {})()
    handler.command = "POST"
    handler.headers = {}
    handler._safe_webui_print = lambda msg, *a, **k: printed.append(msg)
    monkeypatch.setattr(routes, "read_body", lambda _h: {"worktree": False})
    monkeypatch.setattr(routes, "_check_csrf", lambda _h: True)
    monkeypatch.setattr(routes, "_csrf_exempt_path", lambda _p: False)
    monkeypatch.setattr(routes, "_guard_request_session_visibility", lambda *a, **k: True)
    monkeypatch.setattr(routes, "_resolve_new_session_workspace", lambda *a, **k: None)
    monkeypatch.setattr(routes, "new_session", lambda **kw: _Session())
    monkeypatch.setattr(routes, "j", lambda _h, payload, *a, **k: cap.setdefault("ok", payload) or True)
    monkeypatch.setattr(routes, "bad", lambda _h, msg, *a, **k: cap.setdefault("bad", msg) or True)
    if step is not None:
        monkeypatch.setattr(routes, "time", _Clock(step))
    routes.handle_post(handler, urlparse("/api/session/new"))
    assert "bad" not in cap and "ok" in cap
    return [m for m in printed if "[SLOW]" in str(m)]


def test_slow_session_new_logs_every_stage(monkeypatch):
    lines = _drive(monkeypatch, step=1.0)  # every stage "takes" 1s
    assert len(lines) == 1, lines
    line = lines[0]
    assert line.startswith("[SLOW] session_new ")
    for key in STAGES:
        assert f" {key}=" in line, (key, line)


def test_fast_session_new_logs_nothing(monkeypatch):
    assert _drive(monkeypatch, step=None) == []
