"""Shared test helpers for the server-side wakeup test suites.

Consolidates the ``_install_fake_start_session_turn`` / ``_wait_for_wakeup``
pair that ``test_session_channel_option_x.py`` and ``test_wakeup_defer_race.py``
both need so the two suites can't drift. Per Copilot review on PR #2971
(r3305700944).
"""
from __future__ import annotations

import threading


def install_fake_start_session_turn(monkeypatch, *, status: int = 200):
    """Patch ``api.routes.start_session_turn`` to record calls instead of
    running a real agent turn.

    The drain helper does ``from api.routes import start_session_turn``
    inside a daemon thread, so patching the attribute on the ``api.routes``
    module is what the thread resolves at call time.

    Returns a ``holder`` dict with ``calls`` (list of recorded call kwargs)
    and ``event`` (a ``threading.Event`` set on first call) — pair it with
    ``wait_for_wakeup`` below.
    """
    import api.routes as _routes

    holder = {"calls": [], "event": threading.Event()}

    def _fake(session_id, message, *, source="process_wakeup"):
        holder["calls"].append(
            {"session_id": session_id, "message": message, "source": source}
        )
        holder["event"].set()
        return {"stream_id": "fake-stream", "session_id": session_id, "_status": status}

    monkeypatch.setattr(_routes, "start_session_turn", _fake, raising=True)
    return holder


def wait_for_wakeup(holder, timeout: float = 3.0) -> bool:
    """Block until the server-side wakeup runner thread recorded a call.

    Returns True if the holder's event fired within ``timeout`` seconds.
    """
    return holder["event"].wait(timeout=timeout)
