"""Behavioral regression tests for draft-save coalescing (#7839).

These drive the real `_draft_save_worker` against a real (isolated) session
store, not just source assertions:

1. A published intent is durably persisted to the session JSON.
2. A burst of N intents collapses to fewer full saves (latest-wins).
3. A missing session settles without saving and clears the worker state.
4. A bounded agent-lock timeout requeues the intent (no false ok:true / data
   loss edge of the kind that killed the earlier sidecar attempts #6011/#6252).
"""
import collections
import threading
import time

import pytest


@pytest.fixture
def isolated_session_env(monkeypatch, tmp_path):
    """Isolate SESSIONS-cache + session store globals onto a throwaway dir."""
    from api import config as _cfg
    from api import models as _models

    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    index_file = sessions_dir / "_index.json"

    monkeypatch.setattr(_cfg, "SESSION_DIR", sessions_dir)
    monkeypatch.setattr(_models, "SESSION_DIR", sessions_dir)
    monkeypatch.setattr(_cfg, "SESSION_INDEX_FILE", index_file)
    monkeypatch.setattr(_models, "SESSION_INDEX_FILE", index_file)
    lock = threading.Lock()
    monkeypatch.setattr(_cfg, "LOCK", lock)
    monkeypatch.setattr(_models, "LOCK", lock)
    sessions = collections.OrderedDict()
    monkeypatch.setattr(_cfg, "SESSIONS", sessions)
    monkeypatch.setattr(_models, "SESSIONS", sessions)
    yield sessions_dir


def _make_persisted_session(sid):
    from api.models import Session

    s = Session(
        session_id=sid,
        title=f"Session {sid}",
        messages=[
            {"role": "user", "content": "hello", "timestamp": time.time()},
            {"role": "assistant", "content": "reply", "timestamp": time.time()},
        ],
    )
    s.save()
    return s


def _wait_for_draft(sid, expected_text, timeout=10.0):
    from api.models import Session

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        s = Session.load(sid)
        draft = getattr(s, "composer_draft", {}) or {}
        if draft.get("text") == expected_text:
            return draft
        time.sleep(0.02)
    return None


def test_worker_persists_intent_durably(isolated_session_env):
    from api import routes
    from api.models import Session

    sid = "dwkr0001"
    _make_persisted_session(sid)
    routes._DRAFT_COALESCE.clear()

    with routes._DRAFT_COALESCE_LOCK:
        routes._DRAFT_COALESCE[sid] = {
            "pending": None,
            "published": routes._DraftSaveIntent(text="persisted draft", files=None),
            "worker_alive": threading.Event(),
            "worker_event": threading.Event(),
            "settled_unchanged": False,
        }
    routes._draft_save_worker(sid)

    draft = _wait_for_draft(sid, "persisted draft")
    assert draft is not None, "worker must durably persist the published intent"
    assert routes._DRAFT_COALESCE.get(sid) is None, "drained state must be evicted"
    # The saved session must not be a stub: messages survive the draft save.
    s = Session.load(sid)
    assert len(s.messages) == 2


def test_worker_settles_missing_session_without_saving(isolated_session_env):
    from api import routes

    sid = "dwkr0002"
    routes._DRAFT_COALESCE.clear()

    with routes._DRAFT_COALESCE_LOCK:
        routes._DRAFT_COALESCE[sid] = {
            "pending": None,
            "published": routes._DraftSaveIntent(text="x", files=None),
            "worker_alive": threading.Event(),
            "worker_event": threading.Event(),
            "settled_unchanged": False,
        }
    routes._draft_save_worker(sid)
    assert routes._DRAFT_COALESCE.get(sid) is None, "missing session must clear state"


def test_burst_coalesces_to_single_save(isolated_session_env):
    from api import routes
    from api.models import Session

    sid = "dwkr0003"
    _make_persisted_session(sid)
    routes._DRAFT_COALESCE.clear()

    saves = {"n": 0}
    real_save = Session.save

    def counting_save(self, *a, **kw):
        saves["n"] += 1
        return real_save(self, *a, **kw)

    Session.save = counting_save
    try:
        with routes._DRAFT_COALESCE_LOCK:
            routes._DRAFT_COALESCE[sid] = {
                "pending": None,
                "published": routes._DraftSaveIntent(text="v1", files=None),
                "worker_alive": threading.Event(),
                "worker_event": threading.Event(),
                "settled_unchanged": False,
            }
        # A second intent arrives while the worker is still draining v1: the
        # v2 payload must win and only one additional full save may run.
        with routes._DRAFT_COALESCE_LOCK:
            routes._DRAFT_COALESCE[sid]["pending"] = routes._DraftSaveIntent(text="v2", files=None)
        routes._draft_save_worker(sid)
    finally:
        Session.save = real_save

    assert saves["n"] == 1, f"burst must coalesce to one save, got {saves['n']}"
    draft = _wait_for_draft(sid, "v2")
    assert draft is not None, "latest payload must win"
    assert routes._DRAFT_COALESCE.get(sid) is None


def test_lock_timeout_requeues_intent_instead_of_dropping(isolated_session_env, monkeypatch):
    from api import routes

    sid = "dwkr0004"
    _make_persisted_session(sid)
    routes._DRAFT_COALESCE.clear()

    class BusyLock:
        def acquire(self, timeout=None):
            return False

        def release(self):
            pass

    monkeypatch.setattr(routes, "_get_session_agent_lock", lambda _sid: BusyLock())
    monkeypatch.setattr(routes, "_DRAFT_SAVE_LOCK_WAIT", 0.05)

    with routes._DRAFT_COALESCE_LOCK:
        routes._DRAFT_COALESCE[sid] = {
            "pending": None,
            "published": routes._DraftSaveIntent(text="must survive", files=None),
            "worker_alive": threading.Event(),
            "worker_event": threading.Event(),
            "settled_unchanged": False,
        }
    routes._draft_save_worker(sid)

    with routes._DRAFT_COALESCE_LOCK:
        state = routes._DRAFT_COALESCE.get(sid)
        assert state is not None, "failed save must keep the state for retry"
        assert state["pending"] is not None and state["pending"].text == "must survive", (
            "lock timeout must requeue the intent (no silent drop / false ok:true)"
        )
        assert state["published"] is None
        assert not state["worker_alive"].is_set()


def test_failed_settle_self_heals_once_lock_frees(isolated_session_env, monkeypatch):
    """A queued intent must become durable even if no further request comes.

    The pre-coalescing code waited synchronously on the agent lock and always
    saved; after a failed settle the coalescing worker must therefore respawn
    itself and persist the intent (bounded retry timer), not silently lose it.
    """
    from api import routes

    sid = "dwkr0005"
    _make_persisted_session(sid)
    routes._DRAFT_COALESCE.clear()

    class FlakyLock:
        def __init__(self):
            self.calls = 0

        def acquire(self, timeout=None):
            self.calls += 1
            return self.calls > 2

        def release(self):
            pass

    flaky = FlakyLock()
    monkeypatch.setattr(routes, "_get_session_agent_lock", lambda _sid: flaky)
    monkeypatch.setattr(routes, "_DRAFT_SAVE_LOCK_WAIT", 0.05)
    monkeypatch.setattr(routes, "_DRAFT_SAVE_RETRY_DELAY", 0.2)

    with routes._DRAFT_COALESCE_LOCK:
        routes._DRAFT_COALESCE[sid] = {
            "pending": None,
            "published": routes._DraftSaveIntent(text="self healed", files=None),
            "worker_alive": threading.Event(),
            "worker_event": threading.Event(),
            "settled_unchanged": False,
        }
    routes._draft_save_worker(sid)  # fails, requeues, schedules the retry timer

    draft = _wait_for_draft(sid, "self healed", timeout=10.0)
    assert draft is not None, "bounded self-heal retry must persist the queued intent"
    with routes._DRAFT_COALESCE_LOCK:
        assert routes._DRAFT_COALESCE.get(sid) is None, "drained state must be evicted after self-heal"


def test_self_heal_retries_are_bounded(isolated_session_env, monkeypatch):
    """A persistent failure must stop retrying instead of spinning forever."""
    import time as _time

    from api import routes

    sid = "dwkr0006"
    _make_persisted_session(sid)
    routes._DRAFT_COALESCE.clear()

    class BusyLock:
        def acquire(self, timeout=None):
            return False

        def release(self):
            pass

    monkeypatch.setattr(routes, "_get_session_agent_lock", lambda _sid: BusyLock())
    monkeypatch.setattr(routes, "_DRAFT_SAVE_LOCK_WAIT", 0.02)
    monkeypatch.setattr(routes, "_DRAFT_SAVE_RETRY_DELAY", 0.05)
    monkeypatch.setattr(routes, "_DRAFT_SAVE_MAX_RETRIES", 2)

    with routes._DRAFT_COALESCE_LOCK:
        routes._DRAFT_COALESCE[sid] = {
            "pending": None,
            "published": routes._DraftSaveIntent(text="never lands", files=None),
            "worker_alive": threading.Event(),
            "worker_event": threading.Event(),
            "settled_unchanged": False,
        }
    routes._draft_save_worker(sid)
    deadline = _time.monotonic() + 5.0
    while _time.monotonic() < deadline:
        with routes._DRAFT_COALESCE_LOCK:
            state = routes._DRAFT_COALESCE.get(sid)
            if state and state.get("retries", 0) >= 3:
                break
        _time.sleep(0.05)
    _time.sleep(0.5)  # would-be fourth retry window
    with routes._DRAFT_COALESCE_LOCK:
        state = routes._DRAFT_COALESCE.get(sid)
        assert state is not None, "state must remain for the next real request"
        assert state.get("retries", 0) == 3, (
            f"initial run + {routes._DRAFT_SAVE_MAX_RETRIES} retry spawns, then stop; got {state.get('retries')}"
        )
        assert state["pending"] is not None and state["pending"].text == "never lands"
        assert not state["worker_alive"].is_set()
