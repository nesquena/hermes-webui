"""Server-side wakeups must retarget archived compression snapshots.

Reproduces the "Failed to migrate compressed-session ownership: <old>-><new>"
turn deaths seen in production logs: a background-process completion (or any
deferred-wakeup delivery) is keyed by the session id that was live when the
process was launched. If that session has since been rotated by context
compression, the wakeup used to start a turn on the sealed pre-compression
parent while the cached agent already owned the continuation id. At turn end
the stream then attempted a SECOND ownership migration onto the already
published continuation, which fails closed — the whole wakeup turn (including
any real agent work it had done) died with a RuntimeError surfaced to the UI.

The fix retargets start_session_turn to the durable compression continuation
before any turn state is built, so the turn runs on the live session and no
second migration is attempted.
"""
from __future__ import annotations

import collections
import sqlite3
from pathlib import Path

import pytest


class _FakeSession:
    def __init__(self, sid, *, parent=None, snap=False, profile="work", updated=100.0):
        self.session_id = sid
        self.profile = profile
        self.parent_session_id = parent
        self.pre_compression_snapshot = snap
        self.updated_at = updated
        self.created_at = updated
        self.model = "test-model"
        self.model_provider = "test-provider"
        self.workspace = "/tmp/ws-test"


@pytest.fixture
def _stub_routes(monkeypatch, tmp_path):
    from api import routes as routes_mod
    from api import config as config_mod

    parent = _FakeSession("parent000001", snap=True, updated=100.0)
    child = _FakeSession("child0000001", parent="parent000001", updated=200.0)
    live = _FakeSession("live00000001", updated=300.0)

    sessions = {s.session_id: s for s in (parent, child, live)}
    monkeypatch.setattr(routes_mod, "get_session", lambda sid: sessions[sid])

    # Resolver inputs: in-memory children only; no index, no sidecar scan.
    monkeypatch.setattr(
        routes_mod, "SESSIONS", collections.OrderedDict(sessions), raising=False
    )
    monkeypatch.setattr(routes_mod, "SESSION_DIR", tmp_path, raising=False)
    monkeypatch.setattr(config_mod, "SESSION_DIR", tmp_path, raising=False)
    monkeypatch.setattr(
        routes_mod, "SESSION_INDEX_FILE", tmp_path / "_index.json", raising=False
    )

    # Short-circuit everything between the retarget seam and _start_run.
    monkeypatch.setattr(
        routes_mod,
        "_resolve_chat_workspace_with_recovery",
        lambda _s, _req: "/tmp/ws-test",
    )
    monkeypatch.setattr(
        routes_mod,
        "_resolve_compatible_session_model_state",
        lambda model, provider, **kwargs: (model, provider, model),
    )

    started = []

    def _fake_start_run(s, **kwargs):
        started.append((s.session_id, kwargs.get("source")))
        return {"_status": 200, "stream_id": "stream-test", "session_id": s.session_id}

    monkeypatch.setattr(routes_mod, "_start_run", _fake_start_run)

    import api.background_process as bp_mod

    monkeypatch.setattr(bp_mod, "get_session_channel", lambda _sid: None)

    return routes_mod, started


def test_wakeup_on_archived_snapshot_retargets_to_continuation(_stub_routes):
    routes_mod, started = _stub_routes

    resp = routes_mod.start_session_turn(
        "parent000001", "background process finished", source="process_wakeup"
    )

    assert resp["_status"] == 200
    # The turn must run on the live continuation, NOT the sealed parent.
    # Before the fix this recorded "parent000001" and the turn later died in
    # the post-turn ownership migration.
    assert started == [("child0000001", "process_wakeup")]


def test_wakeup_on_live_session_is_untouched(_stub_routes):
    routes_mod, started = _stub_routes

    resp = routes_mod.start_session_turn(
        "live00000001", "background process finished", source="process_wakeup"
    )

    assert resp["_status"] == 200
    assert started == [("live00000001", "process_wakeup")]


def test_wakeup_on_snapshot_without_continuation_keeps_parent(_stub_routes):
    routes_mod, started = _stub_routes
    # Remove the continuation: the snapshot has no live descendant, so the
    # turn proceeds on the parent exactly as before (no crash, no retarget).
    orphan = _FakeSession("orphan000001", snap=True, updated=100.0)
    sessions = {orphan.session_id: orphan}
    routes_mod.get_session = lambda sid: sessions[sid]
    routes_mod.SESSIONS = collections.OrderedDict()

    resp = routes_mod.start_session_turn(
        "orphan000001", "background process finished", source="process_wakeup"
    )

    assert resp["_status"] == 200
    assert started == [("orphan000001", "process_wakeup")]


def test_wakeup_with_unloadable_continuation_fails_closed(_stub_routes):
    routes_mod, started = _stub_routes
    # The lineage NAMES a continuation (it is visible in SESSIONS) but its
    # row cannot be loaded. The sealed parent must not be reopened: the start
    # is refused so the caller's 404 branch clears queued wakeup state rather
    # than resurrecting archived history.
    parent = routes_mod.SESSIONS["parent000001"]

    def _get(sid):
        if sid == "parent000001":
            return parent
        raise KeyError(sid)

    routes_mod.get_session = _get

    resp = routes_mod.start_session_turn(
        "parent000001", "background process finished", source="process_wakeup"
    )

    assert resp == {"error": "Session not found", "_status": 404}
    assert started == []


def test_wakeup_requeue_follows_continuation_key(_stub_routes, monkeypatch):
    """A 409 re-defer must park the prompt under the LIVE continuation key.

    Before the fix the re-defer used the sealed parent's key; the
    continuation's teardown drain only reads its own key, so the prompt
    stayed behind with nothing left to drain it (Greptile finding on #9380).
    """
    import time

    routes_mod, started = _stub_routes
    import api.background_process as bp_mod
    from api import config as config_mod

    def _busy_start(sid, *args, **kwargs):
        started.append((sid, kwargs.get("source")))
        return {"_status": 409, "error": "session already has an active stream"}

    monkeypatch.setattr(routes_mod, "start_session_turn", _busy_start)

    with config_mod.DEFERRED_PROCESS_WAKEUPS_LOCK:
        config_mod.DEFERRED_PROCESS_WAKEUPS.clear()

    bp_mod._start_server_side_wakeup_turn(
        "parent000001",
        "[IMPORTANT: Background process proc-x completed (exit_code=0).]",
        process_id="proc-x",
    )

    child_entries, parent_entries = [], []
    deadline = time.time() + 5
    while time.time() < deadline:
        with config_mod.DEFERRED_PROCESS_WAKEUPS_LOCK:
            child_entries = list(
                config_mod.DEFERRED_PROCESS_WAKEUPS.get("child0000001") or []
            )
            parent_entries = list(
                config_mod.DEFERRED_PROCESS_WAKEUPS.get("parent000001") or []
            )
        if child_entries:
            break
        time.sleep(0.05)

    assert child_entries, "re-deferred prompt must be keyed to the live continuation"
    assert not parent_entries, "nothing may be parked under the sealed parent's key"
    # The turn start itself was also attempted under the continuation id.
    assert started == [("child0000001", "process_wakeup")]


def test_multi_completion_drain_rekeys_survivors_to_continuation(_stub_routes):
    """Entries 2..N of a multi-completion drain survive under the LIVE key.

    Maintainer finding on #9380: ``drain_deferred_wakeups_for_session``
    re-defers entries 2..N under the ORIGINAL (sealed-parent) key before
    starting the first wakeup. The first wakeup retargets to the
    continuation, but the continuation's teardown only drains its own key,
    so the survivors were stranded on the archived parent forever.
    """
    import time

    routes_mod, started = _stub_routes
    import api.background_process as bp_mod
    from api import config as config_mod

    with config_mod.DEFERRED_PROCESS_WAKEUPS_LOCK:
        config_mod.DEFERRED_PROCESS_WAKEUPS.clear()
    assert bp_mod.record_deferred_wakeup("parent000001", "proc-a", "prompt A")
    assert bp_mod.record_deferred_wakeup(
        "parent000001",
        "proc-b",
        "prompt B",
        consumed_claimed_by_dispatch=True,
    )

    assert bp_mod.drain_deferred_wakeups_for_session("parent000001") == 1

    deadline = time.time() + 5
    while time.time() < deadline and not started:
        time.sleep(0.05)
    assert started == [("child0000001", "process_wakeup")]

    with config_mod.DEFERRED_PROCESS_WAKEUPS_LOCK:
        parent_entries = list(
            config_mod.DEFERRED_PROCESS_WAKEUPS.get("parent000001") or []
        )
        child_entries = list(
            config_mod.DEFERRED_PROCESS_WAKEUPS.get("child0000001") or []
        )
    assert not parent_entries, "survivors must not stay on the sealed parent"
    assert [e["wakeup_prompt"] for e in child_entries] == ["prompt B"]
    assert (
        child_entries[0].get("consumed_claimed_by_dispatch") is True
    ), "dispatch-claim provenance must survive the re-key"

    # Subsequent delivery: the continuation's own teardown drains its key.
    assert bp_mod.drain_deferred_wakeups_for_session("child0000001") == 1
    deadline = time.time() + 5
    while time.time() < deadline and len(started) < 2:
        time.sleep(0.05)
    assert started == [
        ("child0000001", "process_wakeup"),
        ("child0000001", "process_wakeup"),
    ]
    with config_mod.DEFERRED_PROCESS_WAKEUPS_LOCK:
        assert not config_mod.DEFERRED_PROCESS_WAKEUPS.get("child0000001")
    with config_mod.DEFERRED_PROCESS_WAKEUPS_LOCK:
        config_mod.DEFERRED_PROCESS_WAKEUPS.clear()


def test_sealed_parent_with_closed_continuation_is_refused(_stub_routes, monkeypatch, tmp_path):
    """A durably sealed parent whose tip was explicitly closed is refused.

    Maintainer finding on #9380: ``durable_compression_continuation``
    returns ``(True, None)`` when the tip row ended with an explicit
    ``session_reset``. The optional-id helper loses the sealed flag, and
    admission fell through to a recording launch on the archived parent
    (200). Neither sealed row is a safe write target — fail closed.
    """
    state = pytest.importorskip("hermes_state")
    from api import profiles

    monkeypatch.syspath_prepend(str(Path(state.__file__).parent))
    db = state.SessionDB(tmp_path / "state.db")
    db.create_session("sealedparent", source="webui")
    db.create_session("closedchild", source="tui", parent_session_id="sealedparent")
    db.end_session("sealedparent", "compression")
    db.end_session("closedchild", "idle_timeout")
    conn = sqlite3.connect(tmp_path / "state.db")
    conn.execute(
        "UPDATE sessions SET end_reason='session_reset' WHERE id='closedchild'"
    )
    conn.commit()
    monkeypatch.setattr(
        profiles, "_resolve_profile_home_for_name", lambda name: str(tmp_path)
    )

    routes_mod, started = _stub_routes
    parent = _FakeSession("sealedparent", snap=True, profile="default", updated=100.0)
    sessions = {parent.session_id: parent}
    routes_mod.get_session = lambda sid: sessions[sid]
    routes_mod.SESSIONS = collections.OrderedDict(sessions)

    resp = routes_mod.start_session_turn(
        "sealedparent", "background process finished", source="process_wakeup"
    )

    assert resp == {"error": "Session not found", "_status": 404}
    assert started == []

    conn.close()
    db.close()


class _ChurnReader:
    """Fake persistence reader whose tip read churns between the two calls.

    Reproduces the transient ``(True, None)`` the Greptile P1 on #9380 called
    out: the seal is real but the tip could not be confirmed on this pass
    (a newer rotation landed mid-read). The same shape covers a read that
    raises after the seal was found.
    """

    def __init__(self, *, churn=True, raises=False):
        self._tip_calls = 0
        self._churn = churn
        self._raises = raises

    def get_session(self, sid):
        if sid == "sealedparent":
            return {
                "id": sid,
                "end_reason": "compression",
                "profile_name": "default",
            }
        return {
            "id": sid,
            "end_reason": None,
            "profile_name": "default",
            "source": "tui",
        }

    def get_compression_tip(self, sid):
        self._tip_calls += 1
        if self._raises:
            raise RuntimeError("database is locked")
        if self._churn and self._tip_calls > 1:
            return "childtwo"
        return "childone"

    def close(self):
        # Churn is per-pass, not per-connection: every seal_state call must
        # see the same first-read/second-read divergence.
        self._tip_calls = 0


def _seal_state_for(reader, monkeypatch, tmp_path):
    from api import compression_continuation as cc
    from api import profiles

    (tmp_path / "state.db").touch()
    monkeypatch.setattr(
        profiles, "_resolve_profile_home_for_name", lambda name: str(tmp_path)
    )
    monkeypatch.setattr(cc, "open_compression_reads", lambda _p: reader)
    return cc.compression_seal_state(
        _FakeSession("sealedparent", profile="default")
    )


def test_seal_state_splits_confirmed_closure_from_transient(tmp_path, monkeypatch):
    """compression_seal_state must not conflate churn/read-failure with closure."""
    sealed, tip, state = _seal_state_for(_ChurnReader(churn=False), monkeypatch, tmp_path)
    assert (sealed, tip, state) == (True, "childone", "open")

    sealed, tip, state = _seal_state_for(_ChurnReader(churn=True), monkeypatch, tmp_path)
    assert (sealed, tip, state) == (True, None, "unconfirmed")

    sealed, tip, state = _seal_state_for(_ChurnReader(raises=True), monkeypatch, tmp_path)
    assert (sealed, tip, state) == (True, None, "unconfirmed")

    # The 2-tuple wrapper keeps its exact previous semantics for old callers.
    from api.compression_continuation import durable_compression_continuation

    assert durable_compression_continuation(
        _FakeSession("sealedparent", profile="default")
    ) == (True, None)


def test_sealed_parent_with_unconfirmable_tip_refuses_retryably(
    _stub_routes, monkeypatch, tmp_path
):
    """Greptile P1 on #9380: a transient null tip must not become a 404.

    A 404 at admission is terminal for the wakeup callers — they drop the
    prompt AND clear every queued wakeup for the session. When the tip merely
    could not be confirmed on this read the refusal is a 409 so the prompt
    survives for the bounded retry drain.
    """
    _seal_state_for(_ChurnReader(churn=True), monkeypatch, tmp_path)

    routes_mod, started = _stub_routes
    parent = _FakeSession("sealedparent", snap=True, profile="default", updated=100.0)
    sessions = {parent.session_id: parent}
    routes_mod.get_session = lambda sid: sessions[sid]
    routes_mod.SESSIONS = collections.OrderedDict(sessions)

    resp = routes_mod.start_session_turn(
        "sealedparent", "background process finished", source="process_wakeup"
    )

    assert resp == {
        "error": "compression_continuation_unconfirmed",
        "_status": 409,
    }
    assert started == []


def test_unconfirmed_refusal_requeues_prompt_and_schedules_bounded_retry(
    _stub_routes, monkeypatch
):
    """The wakeup caller must re-defer AND arm one bounded retry drain.

    A sealed parent never gets another turn teardown, so a plain re-defer
    would park the prompt on a key nothing drains again. The 409 branch
    schedules the #7680 one-shot retry timer — but only for the first
    attempt: the retry drain runs with retry_attempt=1 and must not
    reschedule, so a persistently unconfirmable tip parks instead of looping.
    """
    import time

    routes_mod, started = _stub_routes
    import api.background_process as bp_mod
    from api import config as config_mod

    def _unconfirmed_start(sid, *args, **kwargs):
        started.append((sid, kwargs.get("source")))
        return {
            "_status": 409,
            "error": "compression_continuation_unconfirmed",
        }

    scheduled = []
    monkeypatch.setattr(routes_mod, "start_session_turn", _unconfirmed_start)
    monkeypatch.setattr(
        routes_mod,
        "_schedule_deferred_wakeup_retry",
        lambda sid, **kwargs: scheduled.append(sid),
    )

    with config_mod.DEFERRED_PROCESS_WAKEUPS_LOCK:
        config_mod.DEFERRED_PROCESS_WAKEUPS.clear()

    bp_mod._start_server_side_wakeup_turn(
        "parent000001",
        "[IMPORTANT: Background process proc-y completed (exit_code=0).]",
        process_id="proc-y",
    )

    def _wait_for(predicate):
        deadline = time.time() + 5
        while time.time() < deadline and not predicate():
            time.sleep(0.05)

    def _child_entries():
        with config_mod.DEFERRED_PROCESS_WAKEUPS_LOCK:
            return list(config_mod.DEFERRED_PROCESS_WAKEUPS.get("child0000001") or [])

    _wait_for(lambda: scheduled or _child_entries())
    assert scheduled == ["child0000001"], (
        "the retry timer must be armed once, keyed to the live continuation"
    )
    entries = _child_entries()
    assert entries and entries[0]["wakeup_prompt"].startswith("[IMPORTANT:"), (
        "the prompt must be re-deferred under the continuation key, not dropped"
    )

    # Bounded: the retry drain's own delivery (retry_attempt=1) must NOT
    # schedule another timer.
    scheduled.clear()
    with config_mod.DEFERRED_PROCESS_WAKEUPS_LOCK:
        config_mod.DEFERRED_PROCESS_WAKEUPS.clear()
    bp_mod._start_server_side_wakeup_turn(
        "parent000001",
        "[IMPORTANT: Background process proc-z completed (exit_code=0).]",
        process_id="proc-z",
        retry_attempt=1,
    )
    _wait_for(_child_entries)
    assert scheduled == [], "the retry drain must not reschedule itself"
    with config_mod.DEFERRED_PROCESS_WAKEUPS_LOCK:
        config_mod.DEFERRED_PROCESS_WAKEUPS.clear()
