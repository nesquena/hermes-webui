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
