"""Production-composed tests for the durable history-mutation authority.

PR #7075 review rework — the maintainers asked for
  * checkpoint restore + per-message delete to go through the Agent's
    transaction authority (SessionDB.rewind_to_message / replace_messages),
    not WebUI hand-rolled SQLite;
  * fail-closed dual-store publication (a durable commit whose sidecar publish
    fails must never report success — the correction is captured in a
    crash-safe resync marker and re-applied on the next load);
  * tests that exercise the REAL public paths instead of stub tables.

Everything here runs against a tmp state.db built with the real agent schema
and written through the same SessionDB API the agent uses.
"""
import json
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import api.config as config  # noqa: E402
import api.models as models  # noqa: E402
import api.session_ops as session_ops  # noqa: E402
from api.models import Session  # noqa: E402


def _seed_state_db(state_db, sid: str, rows):
    from hermes_state import SessionDB
    db = SessionDB(state_db)
    try:
        db.ensure_session(sid, source="webui-test")
        return [db.append_message(sid, role, content) for role, content in rows]
    finally:
        db.close()


def _active_ids(state_db, sid):
    conn = sqlite3.connect(state_db)
    try:
        return sorted(r[0] for r in conn.execute(
            "SELECT id FROM messages WHERE session_id=? "
            "AND (active IS NULL OR active != 0)", (sid,)).fetchall())
    finally:
        conn.close()


def _active_flag(state_db, row_id):
    conn = sqlite3.connect(state_db)
    try:
        row = conn.execute(
            "SELECT active FROM messages WHERE id=?", (row_id,)).fetchone()
        return None if row is None else row[0]
    finally:
        conn.close()


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """3-turn session, display rows stamped with the REAL durable ids."""
    sid = "durableauth01"
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    state_db = tmp_path / "state.db"

    u1, a1, u2, a2, u3, a3 = _seed_state_db(state_db, sid, [
        ("user", "first prompt"), ("assistant", "first reply"),
        ("user", "second prompt"), ("assistant", "second reply"),
        ("user", "third prompt"), ("assistant", "third reply"),
    ])
    ids = {"u1": u1, "a1": a1, "u2": u2, "a2": a2, "u3": u3, "a3": a3}

    s = Session(session_id=sid)
    s.title = "durable authority test"
    s.messages = [
        {"id": "u1", "role": "user", "content": "first prompt", "_row_id": u1, "timestamp": 1.0},
        {"id": "a1", "role": "assistant", "content": "first reply", "_row_id": a1, "timestamp": 1.5},
        {"id": "u2", "role": "user", "content": "second prompt", "_row_id": u2, "timestamp": 2.0},
        {"id": "a2", "role": "assistant", "content": "second reply", "_row_id": a2, "timestamp": 2.5},
        {"id": "u3", "role": "user", "content": "third prompt", "_row_id": u3, "timestamp": 3.0},
        {"id": "a3", "role": "assistant", "content": "third reply", "_row_id": a3, "timestamp": 3.5},
    ]
    s.context_messages = [dict(m) for m in s.messages]

    monkeypatch.setattr(config, "SESSION_DIR", str(sessions_dir))
    monkeypatch.setattr(models, "SESSION_DIR", sessions_dir)
    monkeypatch.setattr(models, "_active_state_db_path", lambda: state_db)
    monkeypatch.setattr(models, "_agent_state_db_path", lambda **kw: state_db)
    s.save()
    monkeypatch.setitem(models.SESSIONS, sid, s)
    return SimpleNamespace(sid=sid, session=s, state_db=state_db, ids=ids)


# --------------------------------------------------------------------------
# per-message delete: durable through the Agent's replace_messages
# --------------------------------------------------------------------------

def test_delete_single_removes_durable_row_and_reinserts_suffix(env):
    """Item #4: the delete is durable. The target row is soft-archived; the
    divergent suffix is re-inserted with NEW ids (replace_messages contract)."""
    res = session_ops.delete_message(env.sid, "u2", scope="single")
    assert res["durable_synced"] is True
    assert res["removed_message_ids"] == ["u2"]
    assert res["durable_removed_row_ids"] == [env.ids["u2"]]
    # display dropped exactly the target (single scope keeps the reply)
    assert [m["content"] for m in env.session.messages] == [
        "first prompt", "first reply", "second reply", "third prompt", "third reply"]
    active = _active_ids(env.state_db, env.sid)
    # kept prefix keeps its identity; the suffix got fresh ids
    assert active[:2] == [env.ids["u1"], env.ids["a1"]]
    assert len(active) == 5
    assert env.ids["u2"] not in active            # archived, not deleted
    assert _active_flag(env.state_db, env.ids["u2"]) == 0


def test_delete_pair_removes_both_durable_rows(env):
    res = session_ops.delete_message(env.sid, "u2", scope="pair")
    assert res["durable_synced"] is True
    assert res["removed_message_ids"] == ["a2", "u2"]
    assert res["durable_removed_row_ids"] == [env.ids["u2"], env.ids["a2"]]
    assert [m["content"] for m in env.session.messages] == [
        "first prompt", "first reply", "third prompt", "third reply"]
    active = _active_ids(env.state_db, env.sid)
    assert active[:2] == [env.ids["u1"], env.ids["a1"]]
    assert len(active) == 4
    assert _active_flag(env.state_db, env.ids["u2"]) == 0
    assert _active_flag(env.state_db, env.ids["a2"]) == 0


def test_delete_strips_invalidated_display_stamps(env):
    """replace_messages re-inserts the suffix under new ids — display rows that
    point at the archived ids must not keep the stale stamps."""
    session_ops.delete_message(env.sid, "u2", scope="pair")
    stamps = {m["content"]: m.get("_row_id") for m in env.session.messages}
    assert stamps["first prompt"] == env.ids["u1"]      # kept identity
    assert stamps["first reply"] == env.ids["a1"]
    assert stamps["third prompt"] is None               # re-inserted: re-derive
    assert stamps["third reply"] is None


def test_delete_publish_failure_marks_and_next_load_corrects(env, monkeypatch):
    """Fail-closed delete: durable commit lands, sidecar publish fails ->
    flag + resync marker; the next load drops exactly the removed display rows."""
    import api.durable_sync as durable_sync

    original_save = models.Session.save

    def flaky_save(self, *a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(models.Session, "save", flaky_save)
    with pytest.raises(RuntimeError):
        session_ops.delete_message(env.sid, "u2", scope="pair")
    marker = durable_sync.read_resync_marker(env.sid)
    assert marker and marker["kind"] == "delete"
    assert sorted(marker["drop_display_ids"]) == ["a2", "u2"]
    # durable commit landed...
    active = _active_ids(env.state_db, env.sid)
    assert active[:2] == [env.ids["u1"], env.ids["a1"]]
    assert len(active) == 4
    # ...on-disk sidecar still has all 6 rows...
    on_disk = json.loads(Path(env.session.path).read_text())
    assert len(on_disk["messages"]) == 6
    # ...and the next load re-applies the drop + clears the marker.
    monkeypatch.setattr(models.Session, "save", original_save)
    fresh = models.Session.load(env.sid)
    assert [m["content"] for m in fresh.messages] == [
        "first prompt", "first reply", "third prompt", "third reply"]
    assert durable_sync.read_resync_marker(env.sid) is None


def test_delete_unattributable_target_fails_closed(env, monkeypatch):
    """A display row with no durable attribution must refuse (409-class stale),
    never guess — and the sidecar stays untouched."""
    env.session.messages.append(
        {"id": "u4", "role": "user", "content": "brand new unpersisted prompt", "timestamp": 9.0})
    with pytest.raises(session_ops.CheckpointStaleError):
        session_ops.delete_message(env.sid, "u4", scope="single")
    assert len(env.session.messages) == 7
    assert _active_ids(env.state_db, env.sid) == [
        env.ids["u1"], env.ids["a1"], env.ids["u2"],
        env.ids["a2"], env.ids["u3"], env.ids["a3"]]


def test_delete_without_durable_session_is_sidecar_only(tmp_path, monkeypatch, env):
    """A pre-persistence session (no durable rows) keeps the historic
    sidecar-only splice — nothing durable to update, durable_synced False."""
    sid = "delete-nodurable"
    s = Session(session_id=sid)
    s.messages = [
        {"id": "u1", "role": "user", "content": "hi", "timestamp": 1.0},
        {"id": "a1", "role": "assistant", "content": "hello", "timestamp": 1.5},
    ]
    s.context_messages = [dict(m) for m in s.messages]
    s.save()
    monkeypatch.setitem(models.SESSIONS, sid, s)
    res = session_ops.delete_message(sid, "u1", scope="pair")
    assert res["durable_synced"] is False
    assert res["durable_removed_row_ids"] == []
    assert s.messages == []


def test_delete_busy_lease_refused_via_authority(env):
    """A real cross-process turn lease refuses the durable delete in-txn."""
    from hermes_state import SessionDB
    db = SessionDB(env.state_db)
    try:
        assert db.try_acquire_session_turn_lease(
            env.sid, "webui-test-holder", ttl_seconds=120.0)
    finally:
        db.close()
    with pytest.raises(session_ops.CheckpointBusyError):
        session_ops.delete_message(env.sid, "u2", scope="pair")
    assert len(env.session.messages) == 6
    assert _active_ids(env.state_db, env.sid) == [
        env.ids["u1"], env.ids["a1"], env.ids["u2"],
        env.ids["a2"], env.ids["u3"], env.ids["a3"]]


# --------------------------------------------------------------------------
# source-level guards for the review contract
# --------------------------------------------------------------------------

def test_routes_map_durable_refusals_to_409_and_failures_to_500():
    import inspect
    import api.routes as routes
    src = inspect.getsource(routes)
    assert "except CheckpointBusyError as e:" in src     # -> 409 (restore + delete)
    assert "except CheckpointStaleError as e:" in src    # -> 409
    assert "Message delete failed" in src                # -> 500, never fake ok
    assert '"durable_synced"' in src                     # response carries the flag


def test_no_handrolled_durable_writes_in_session_ops():
    import inspect
    src = inspect.getsource(session_ops)
    assert "UPDATE messages SET active=0" not in src
    assert "_archive_state_db_suffix" not in src
    assert "rewind_to_message" in src
    assert "replace_messages" in src
