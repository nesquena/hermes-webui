"""Behavioral regression tests for draft-save coalescing (#7839).

Drives the real `_draft_save_worker` AND the real POST handler (via
`routes.handle_post` with a fake handler) against a real isolated session
store — including the concurrent-POST interleavings the gate review demanded:

1. A published intent is durably persisted to the session JSON.
2. A burst collapses: the queued latest intent wins, one save per payload.
3. A missing session settles honestly (404), never as ok:true.
4. A bounded agent-lock timeout requeues the intent (retryable 503, no data
   drop) and a bounded self-heal timer makes it durable without any further
   request; retries stop at the cap.
5. Two concurrent POSTs start exactly ONE worker; both settle 200 with the
   newest text durable.
6. A non-KeyError load exception settles as 503 and does not strand the
   worker: the next request recovers.
7. The 200 response carries the actual durable draft (including preserved
   files on a text-only POST).
"""
import collections
import io
import json
import threading
import time
import urllib.parse

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


@pytest.fixture(autouse=True)
def _clean_draft_registry():
    """Keep the module-global coalescing registry from leaking across tests."""
    from api import routes

    routes._DRAFT_COALESCE.clear()
    yield
    routes._DRAFT_COALESCE.clear()


def _make_persisted_session(sid, draft=None):
    from api.models import Session

    s = Session(
        session_id=sid,
        title=f"Session {sid}",
        messages=[
            {"role": "user", "content": "hello", "timestamp": time.time()},
            {"role": "assistant", "content": "reply", "timestamp": time.time()},
        ],
    )
    if draft is not None:
        s.composer_draft = draft
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


def _wait_until(predicate, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


class _FakeHandler:
    """Minimal handler shim for routes.handle_post (pattern of test_465)."""

    def __init__(self, payload):
        raw = json.dumps(payload).encode("utf-8")
        self.status = None
        self.response = b""
        self.headers = {"Content-Type": "application/json", "Content-Length": str(len(raw))}
        self.rfile = io.BytesIO(raw)
        self.wfile = io.BytesIO()
        self.command = "POST"
        self.path = "/api/session/draft"
        self.client_address = ("127.0.0.1", 12345)

    def send_response(self, status):
        self.status = status

    def send_header(self, key, value):
        self.headers[key] = value

    def end_headers(self):
        pass

    def _safe_webui_print(self, *_args, **_kwargs):
        pass


@pytest.fixture
def draft_responses(monkeypatch):
    """Capture j()/bad() responses issued by the draft handler."""
    from api import routes

    def _j(handler, obj, *args, **kwargs):
        handler.response = obj
        handler.status = kwargs.get("status", 200)
        return True

    def _bad(handler, msg, code=400):
        handler.response = {"error": msg}
        handler.status = code
        return True

    monkeypatch.setattr(routes, "j", _j)
    monkeypatch.setattr(routes, "bad", _bad)


def _post_draft(sid, text, files=None):
    from api import routes

    handler = _FakeHandler({"session_id": sid, "text": text, "files": files})
    parsed = urllib.parse.urlparse("/api/session/draft")
    routes.handle_post(handler, parsed)
    return handler


def test_worker_persists_intent_durably(isolated_session_env):
    from api import routes
    from api.models import Session

    sid = "dwkr0001"
    _make_persisted_session(sid)

    state = routes._DraftSaveState()
    state.generation = 1
    state.pending = routes._DraftSaveIntent(text="persisted draft", files=None)
    with routes._DRAFT_CV:
        routes._DRAFT_COALESCE[sid] = state
    routes._draft_save_worker(sid)

    assert _wait_for_draft(sid, "persisted draft") is not None, (
        "worker must durably persist the published intent"
    )
    with routes._DRAFT_CV:
        assert routes._DRAFT_COALESCE.get(sid) is None, "drained state must be evicted"
    s = Session.load(sid)
    assert len(s.messages) == 2, "draft save must not touch messages"


def test_worker_coalesces_pending_to_latest_payload(isolated_session_env):
    from api import routes
    from api.models import Session

    sid = "dwkr0003"
    _make_persisted_session(sid)

    saves = {"n": 0}
    real_save = Session.save

    def counting_save(self, *a, **kw):
        saves["n"] += 1
        return real_save(self, *a, **kw)

    Session.save = counting_save
    try:
        state = routes._DraftSaveState()
        state.generation = 2
        state.published = routes._DraftSaveIntent(text="v1", files=None)
        state.published_gen = 1
        state.pending = routes._DraftSaveIntent(text="v2", files=None)
        with routes._DRAFT_CV:
            routes._DRAFT_COALESCE[sid] = state
        routes._draft_save_worker(sid)
    finally:
        Session.save = real_save

    assert saves["n"] == 1, f"queued burst must collapse to one save, got {saves['n']}"
    assert _wait_for_draft(sid, "v2") is not None, "latest queued payload must win"
    with routes._DRAFT_CV:
        assert routes._DRAFT_COALESCE.get(sid) is None


def test_missing_session_settles_missing(isolated_session_env, monkeypatch, draft_responses):
    """The WORKER's load 404s after a passing preflight: settle honestly (404).

    Regression guard for the Greptile P2: the previous version of this test
    never created the session, so the handler 404'd in its existence check and
    the post-preflight missing path stayed untested."""
    from api import routes

    sid = "dwkr-deleted"
    _make_persisted_session(sid)  # preflight must pass
    real_get = routes.get_session

    def get_session_stub(_sid, metadata_only=False):
        if metadata_only:
            # Handler preflight / subagent check: session still exists here.
            return real_get(_sid, metadata_only=True)
        # Every FULL load fails: the session vanished after the preflight.
        # (The subagent check swallows this inside its own try/except; the
        # worker must see it and settle "missing".)
        raise KeyError(_sid)

    monkeypatch.setattr(routes, "get_session", get_session_stub)

    handler = _post_draft(sid, "never lands")
    assert handler.status == 404, (
        f"a post-preflight missing session must 404, got {handler.status} {handler.response}"
    )
    with routes._DRAFT_CV:
        assert routes._DRAFT_COALESCE.get(sid) is None, "missing settle must clear state"


def test_lock_timeout_requeues_and_reports_failure(isolated_session_env, monkeypatch, draft_responses):
    from api import routes

    sid = "dwkr0004"
    _make_persisted_session(sid)

    class BusyLock:
        def acquire(self, timeout=None):
            return False

        def release(self):
            pass

    monkeypatch.setattr(routes, "_get_session_agent_lock", lambda _sid: BusyLock())
    monkeypatch.setattr(routes, "_DRAFT_SAVE_LOCK_WAIT", 0.05)
    monkeypatch.setattr(routes, "_DRAFT_SAVE_RETRY_DELAY", 3600.0)  # no self-heal in test

    handler = _post_draft(sid, "must survive")
    assert handler.status == 503, (
        f"failed settle must be 503 (no false ok:true), got {handler.status} {handler.response}"
    )
    with routes._DRAFT_CV:
        state = routes._DRAFT_COALESCE.get(sid)
        assert state is not None, "failed save must keep the state for retry"
        assert state.pending is not None and state.pending.text == "must survive"
        assert not state.owner_alive, "worker must retire after failure"


def test_failed_settle_self_heals_once_lock_frees(isolated_session_env, monkeypatch, draft_responses):
    from api import routes

    sid = "dwkr0005"
    _make_persisted_session(sid)

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

    handler = _post_draft(sid, "self healed")
    # The handler may 503 (its own wait expired) or 200; either way the queued
    # intent must become durable via the bounded self-heal timer.
    assert _wait_for_draft(sid, "self healed", timeout=10.0) is not None, (
        "bounded self-heal retry must persist the queued intent without a further request"
    )
    assert _wait_until(
        lambda: routes._DRAFT_COALESCE.get(sid) is None
    ), "drained state must be evicted after self-heal"


def test_self_heal_retries_are_bounded(isolated_session_env, monkeypatch, draft_responses):
    from api import routes

    sid = "dwkr0006"
    _make_persisted_session(sid)

    class BusyLock:
        def acquire(self, timeout=None):
            return False

        def release(self):
            pass

    monkeypatch.setattr(routes, "_get_session_agent_lock", lambda _sid: BusyLock())
    monkeypatch.setattr(routes, "_DRAFT_SAVE_LOCK_WAIT", 0.02)
    monkeypatch.setattr(routes, "_DRAFT_SAVE_RETRY_DELAY", 0.05)
    monkeypatch.setattr(routes, "_DRAFT_SAVE_MAX_RETRIES", 2)

    _post_draft(sid, "never lands")
    assert _wait_until(
        lambda: (routes._DRAFT_COALESCE.get(sid) is not None
                 and routes._DRAFT_COALESCE.get(sid).retries >= 3),
        timeout=10.0,
    ), "worker must run initial + 2 retry spawns"
    time.sleep(0.5)  # would-be fourth retry window
    with routes._DRAFT_CV:
        state = routes._DRAFT_COALESCE.get(sid)
        assert state is not None, "state must remain for the next real request"
        assert state.retries == 3, (
            f"initial run + {routes._DRAFT_SAVE_MAX_RETRIES} retry spawns, then stop; got {state.retries}"
        )
        assert state.pending is not None and state.pending.text == "never lands"
        assert not state.owner_alive


def test_load_exception_settles_and_next_request_recovers(isolated_session_env, monkeypatch, draft_responses):
    """A non-KeyError load failure must 503 and must not strand the worker."""
    from api import routes
    from api.models import Session

    sid = "dwkr0007"
    _make_persisted_session(sid)
    full_loads = {"n": 0}
    real_get = routes.get_session

    def get_session_stub(_sid, metadata_only=False):
        if metadata_only:
            return real_get(_sid, metadata_only=True)  # handler paths OK
        full_loads["n"] += 1
        # Full load #1 = _session_is_subagent_view_only check (inside its own
        # try/except), #2 = the worker's load — that one must fail, then heal.
        if full_loads["n"] == 2:
            raise RuntimeError("disk hiccup")
        return real_get(_sid, metadata_only=False)

    monkeypatch.setattr(routes, "get_session", get_session_stub)
    monkeypatch.setattr(routes, "_DRAFT_SAVE_RETRY_DELAY", 3600.0)

    handler = _post_draft(sid, "first try")
    assert handler.status == 503, f"load failure must settle 503, got {handler.status}"

    # Next request must be able to claim a fresh worker and succeed.
    def get_session_ok(_sid, metadata_only=False):
        return real_get(_sid, metadata_only=metadata_only)

    monkeypatch.setattr(routes, "get_session", get_session_ok)
    handler2 = _post_draft(sid, "second try")
    assert handler2.status == 200, (
        f"next request must recover after a load failure, got {handler2.status} {handler2.response}"
    )
    assert _wait_for_draft(sid, "second try") is not None


def test_response_carries_durable_draft_with_files(isolated_session_env, draft_responses):
    """A text-only POST on a session with stored attachments must answer with
    the durable draft (files preserved), not the request fields (#7840 gate)."""
    sid = "dwkr0008"
    _make_persisted_session(sid, draft={"text": "old", "files": ["upload-1.png"]})

    handler = _post_draft(sid, "new text", files=None)
    assert handler.status == 200, f"expected 200, got {handler.status} {handler.response}"
    draft = handler.response.get("draft") or {}
    assert draft.get("text") == "new text"
    assert draft.get("files") == ["upload-1.png"], (
        f"response must carry the durable draft's files, got {draft}"
    )


def test_earlier_success_not_flipped_by_later_failure(isolated_session_env, monkeypatch, draft_responses):
    """Gate P1: A (gen 1) and B (gen 2) overlap; A's save succeeds, B's fails.
    A must still answer 200 (its generation settled ok) and B must 503 —
    the earlier success must not be flipped by the later failure."""
    from api import routes
    from api.models import Session

    sid = "dwkr0010"
    _make_persisted_session(sid)

    class FirstOkThenFailLock:
        """Lock free for the first acquire, busy afterwards."""

        def __init__(self):
            self.runs = 0

        def acquire(self, timeout=None):
            self.runs += 1
            return self.runs <= 1

        def release(self):
            pass

    lock_shim = FirstOkThenFailLock()  # ONE instance: acquire calls count across workers
    monkeypatch.setattr(routes, "_get_session_agent_lock", lambda _sid: lock_shim)
    monkeypatch.setattr(routes, "_DRAFT_SAVE_LOCK_WAIT", 0.05)
    monkeypatch.setattr(routes, "_DRAFT_SAVE_RETRY_DELAY", 3600.0)  # no self-heal

    real_save = Session.save

    def slow_first_save(self, *a, **kw):
        # Keep the worker busy so B queues while gen 1 is still saving —
        # both requests are then in flight when the outcomes are written.
        slow_first_save.calls += 1
        if slow_first_save.calls == 1:
            time.sleep(0.4)
        return real_save(self, *a, **kw)

    slow_first_save.calls = 0
    Session.save = slow_first_save
    try:
        results = {}

        def run(key, text):
            results[key] = _post_draft(sid, text)

        t1 = threading.Thread(target=run, args=("a", "first-ok"))
        t2 = threading.Thread(target=run, args=("b", "second-fails"))
        t1.start()
        time.sleep(0.1)  # A is inside its slow save when B publishes
        t2.start()
        t1.join(30)
        t2.join(30)

        assert results["a"].status == 200, (
            f"A's generation settled ok; must not be flipped to 503 by B's failure, got {results['a'].status} {results['a'].response}"
        )
        assert results["b"].status == 503, (
            f"B's failed save must be 503, got {results['b'].status} {results['b'].response}"
        )
    finally:
        Session.save = real_save

    from api.models import Session as _S
    s = _S.load(sid)
    assert (getattr(s, "composer_draft", {}) or {}).get("text") == "first-ok", (
        "A's durable draft must survive B's failure"
    )


def test_two_concurrent_posts_spawn_one_worker_and_both_settle(isolated_session_env, draft_responses):
    """The exact handler-level interleaving the gate reproduced: two POSTs at
    the ownership decision must yield ONE worker; both requests settle 200 and
    the newest text is durable."""
    from api.models import Session

    sid = "dwkr0009"
    # A save heavy enough (~0.4s) that the two overlapping requests are both
    # still in flight while the worker owns the state.
    big = [
        {"role": "user", "content": "x" * 40000, "timestamp": time.time()},
        {"role": "assistant", "content": "y" * 40000, "timestamp": time.time()},
    ] * 30
    s = Session(
        session_id=sid,
        title=f"Session {sid}",
        messages=big,
    )
    s.save()

    worker_counts = []
    stop = {"flag": False}

    def sample_workers():
        while not stop["flag"]:
            n = sum(1 for t in threading.enumerate() if t.name == f"draft-save-{sid}")
            worker_counts.append(n)
            time.sleep(0.005)

    sampler = threading.Thread(target=sample_workers, daemon=True)
    sampler.start()

    results = {}

    def run(key, text):
        results[key] = _post_draft(sid, text)

    t1 = threading.Thread(target=run, args=("a", "concurrent-one"))
    t2 = threading.Thread(target=run, args=("b", "concurrent-two"))
    t1.start()
    time.sleep(0.05)  # let A reach the publish point first (its gen is 1)
    t2.start()
    t1.join(60)
    t2.join(60)
    stop["flag"] = True
    sampler.join(5)

    assert results["a"].status == 200, f"A: {results['a'].status} {results['a'].response}"
    assert results["b"].status == 200, f"B: {results['b'].status} {results['b'].response}"
    max_workers = max(worker_counts) if worker_counts else 0
    assert max_workers == 1, (
        f"concurrent POSTs must never spawn more than one worker, saw {max_workers}"
    )
    assert _wait_for_draft(sid, "concurrent-two") is not None, "the newest text must be durable"
