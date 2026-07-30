"""Shared test helpers for the server-side wakeup test suites.

Consolidates the ``_install_fake_start_session_turn`` / ``_wait_for_wakeup``
pair that ``test_session_channel_option_x.py`` and ``test_wakeup_defer_race.py``
both need so the two suites can't drift. Per Copilot review on PR #2971
(r3305700944).
"""
from __future__ import annotations

import queue
import sys
import threading
import types


class FakeProcessRegistry:
    """Minimal stand-in for ``tools.process_registry.process_registry``.

    Consolidated from the verbatim ``_FakeProcessRegistry`` copies that lived
    in ``test_bg_task_complete_wakeup.py``, ``test_bg_task_complete_throttle.py``
    and ``test_bg_task_complete_ab_coexistence.py`` (Greptile review on PR #2979).
    Keeping one definition prevents the three suites from drifting to subtly
    different stub shapes.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._completion_consumed: set[str] = set()
        self.completion_queue: queue.Queue = queue.Queue()
        self._procs: dict[str, types.SimpleNamespace] = {}

    def register(self, process_id: str, session_key: str) -> None:
        self._procs[process_id] = types.SimpleNamespace(session_key=session_key)

    def get(self, process_id: str):
        return self._procs.get(process_id)

    def is_completion_consumed(self, process_id: str) -> bool:
        with self._lock:
            return process_id in self._completion_consumed


def install_fake_registry(monkeypatch, fake) -> None:
    """Inject ``fake`` under ``tools.process_registry`` for the test.

    IMPORTANT (rebase isolation): uses ONLY ``monkeypatch.setitem`` so both
    ``sys.modules`` entries are restored to their real/absent state on
    teardown. A prior implementation used ``sys.modules.setdefault("tools", …)``
    which is an UNTRACKED mutation — when the real ``tools`` package was not yet
    imported it permanently leaked a non-package fake ``tools`` into
    ``sys.modules``, breaking any later test doing
    ``from tools.process_registry import …``. Both setitem calls below are
    monkeypatch-tracked: on teardown each key is restored to its prior value,
    or deleted if it was absent — no leak.
    """
    mod = types.ModuleType("tools.process_registry")
    mod.process_registry = fake  # type: ignore[attr-defined]
    tools_mod = types.ModuleType("tools")
    tools_mod.process_registry = mod  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "tools", tools_mod)
    monkeypatch.setitem(sys.modules, "tools.process_registry", mod)


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
