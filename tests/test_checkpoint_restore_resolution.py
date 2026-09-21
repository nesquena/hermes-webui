"""Tests for display-addressed checkpoint restore (api/checkpoint_map.py +
session_ops.restore_checkpoint_to_display_message).

Covers the resolution contract that fixes the production failure
``row_id 500 not found in current session transcript; refusing to truncate to
a guessed index`` (stale sidecar stamps on legacy rows):

  * exact content match without any stamped ``_row_id``
  * stale stamp -> falls back to the active content twin
  * joined/duplicate display artifacts -> monotone anchor cut
  * compacted (older-than-live) targets -> anchor / anchor-next cuts
  * nothing durable attributable -> display-only plan
  * durable store unavailable -> empty plan (stamp-only fallback)
  * annotate_restore_targets copies rows, never mutates the transcript

Harness style mirrors tests/test_checkpoint_restore_path.py — tmp sidecar
store + tmp state.db, no sockets.
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
import api.checkpoint_map as checkpoint_map  # noqa: E402
import api.session_ops as session_ops  # noqa: E402
from api.models import Session  # noqa: E402


# --------------------------------------------------------------------------
# pure resolver tests (loader monkeypatched — no db needed)
# --------------------------------------------------------------------------

def _row(rid, content, ts=None, role="user"):
    return {"id": rid, "role": role, "ts": ts, "content": content, "api_content": None}


def _msg(content, role="user", stamp=None, ts=None, mid=None):
    m = {"role": role, "content": content}
    if stamp is not None:
        m["_row_id"] = stamp
    if ts is not None:
        m["timestamp"] = ts
    if mid is not None:
        m["id"] = mid
    return m


@pytest.fixture()
def plan_env(monkeypatch):
    store = {"rows": []}

    def fake_load(session):
        return store["rows"] if store["rows"] is not None else None

    monkeypatch.setattr(checkpoint_map, "load_active_user_rows", fake_load)
    return SimpleNamespace(store=store, session=SimpleNamespace(session_id="x"))


def test_exact_match_without_stamp(plan_env):
    plan_env.store["rows"] = [_row(11, "first prompt"), _row(13, "second prompt")]
    msgs = [_msg("first prompt"), _msg("assistant answer", role="assistant"),
            _msg("second prompt")]
    plan = checkpoint_map.build_restore_plan(plan_env.session, msgs)
    assert plan[0]["mode"] == "exact" and plan[0]["cut_row_id"] == 11
    assert plan[2]["mode"] == "exact" and plan[2]["cut_row_id"] == 13


def test_stale_stamp_falls_back_to_content_twin(plan_env):
    # Sidecar stamp points at a row that is no longer active (history rewrite);
    # the active twin carries the same content under a new id.
    plan_env.store["rows"] = [_row(500, "one"), _row(901, "rewritten prompt")]
    msgs = [_msg("rewritten prompt", stamp=402)]
    plan = checkpoint_map.build_restore_plan(plan_env.session, msgs)
    assert plan[0]["mode"] == "exact"
    assert plan[0]["cut_row_id"] == 901


def test_joined_display_row_gets_anchor_next_cut(plan_env):
    # 'A B' is a display-only join of two durable rows and matches nothing;
    # its window row (B) is older than the join, so the cut moves to the next
    # mapped twin (C) — everything after the join is archived.
    plan_env.store["rows"] = [_row(1, "A", ts=10), _row(2, "B", ts=50), _row(3, "C", ts=60)]
    msgs = [_msg("A", stamp=1, ts=10),
            _msg("A B", ts=100),
            _msg("C", stamp=3, ts=60)]
    plan = checkpoint_map.build_restore_plan(plan_env.session, msgs)
    assert plan[1]["mode"] == "anchor-next"
    assert plan[1]["cut_row_id"] == 3


def test_unmapped_row_anchor_cut_by_timestamp(plan_env):
    # Edited prompt: no exact twin, but the durable row sitting between the
    # previous and next mapped twins is NEWER than the target -> it (and
    # everything after) is the suffix to archive.
    plan_env.store["rows"] = [_row(1, "A", ts=10), _row(2, "X question", ts=199), _row(3, "C", ts=300)]
    msgs = [_msg("A", stamp=1, ts=10),
            _msg("X question v2", ts=200),
            _msg("C", stamp=3, ts=300)]
    plan = checkpoint_map.build_restore_plan(plan_env.session, msgs)
    assert plan[1]["mode"] == "anchor"
    assert plan[1]["cut_row_id"] == 2


def test_unmapped_trailing_row_is_display_only(plan_env):
    # Target is the newest display turn; the only durable row left in its
    # window is OLDER than the target (a previous turn's content) -> nothing
    # durable is attributable -> display-only.
    plan_env.store["rows"] = [_row(1, "A", ts=10), _row(2, "B", ts=100)]
    msgs = [_msg("A", stamp=1, ts=10), _msg("brand new unpersisted prompt", ts=999)]
    plan = checkpoint_map.build_restore_plan(plan_env.session, msgs)
    assert plan[1]["mode"] == "display-only"
    assert plan[1]["cut_row_id"] is None


def test_no_active_rows_all_display_only(plan_env):
    plan_env.store["rows"] = []
    msgs = [_msg("a"), _msg("b")]
    plan = checkpoint_map.build_restore_plan(plan_env.session, msgs)
    assert plan[0]["mode"] == "display-only"
    assert plan[1]["mode"] == "display-only"


def test_durable_unavailable_returns_empty_plan(plan_env):
    plan_env.store["rows"] = None  # loader: state.db unreadable
    plan = checkpoint_map.build_restore_plan(plan_env.session, [_msg("a")])
    assert plan == {}


def test_duplicate_text_matches_monotonically(plan_env):
    plan_env.store["rows"] = [_row(1, "kontynuuj"), _row(2, "kontynuuj"), _row(3, "done")]
    msgs = [_msg("kontynuuj"), _msg("kontynuuj"), _msg("done")]
    plan = checkpoint_map.build_restore_plan(plan_env.session, msgs)
    assert plan[0]["cut_row_id"] == 1
    assert plan[1]["cut_row_id"] == 2
    assert plan[2]["cut_row_id"] == 3


def test_workspace_banner_stripped_for_matching(plan_env):
    plan_env.store["rows"] = [_row(7, "Zrob to zadanie")]
    msgs = [_msg("[Workspace::v1: /tmp/x]\nZrob to zadanie", mid="u9")]
    plan = checkpoint_map.build_restore_plan(plan_env.session, msgs)
    assert plan[0]["mode"] == "exact" and plan[0]["cut_row_id"] == 7


def test_annotate_copies_and_never_mutates(plan_env):
    plan_env.store["rows"] = [_row(11, "hello")]
    original = _msg("hello", mid="u1")
    other = _msg("assistant", role="assistant", mid="a1")
    full = [original, other]
    served = checkpoint_map.annotate_restore_targets(plan_env.session, full, [original, other])
    assert served[0]["_restore_ready"] is True
    assert served[0]["_restore_row_id"] == 11
    assert served[0] is not original
    assert "_restore_ready" not in original          # original untouched
    assert served[1] is other                        # non-user passes through


# --------------------------------------------------------------------------
# session_ops end-to-end on a tmp sidecar store + tmp state.db
# --------------------------------------------------------------------------

@pytest.fixture()
def env(tmp_path, monkeypatch):
    """Legacy-style session: NO stamped _row_id anywhere (the production bug),
    ids are stable sidecar ids; state.db mirrors the same contents."""
    sid = "restoretest02"
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    state_db = tmp_path / "state.db"

    conn = sqlite3.connect(state_db)
    conn.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, "
                 "role TEXT, content TEXT, active INTEGER DEFAULT 1)")
    rows = [(11, sid, "user", "first prompt"), (12, sid, "assistant", "answer 1"),
            (13, sid, "user", "second prompt"), (14, sid, "assistant", "answer 2")]
    conn.executemany("INSERT INTO messages VALUES (?,?,?,?,1)", rows)
    conn.commit()
    conn.close()

    s = Session(session_id=sid)
    s.title = "display restore test"
    s.messages = [
        {"role": "user", "content": "first prompt", "id": "u1", "timestamp": 100.0},
        {"role": "assistant", "content": "answer 1", "id": "a1", "timestamp": 101.0},
        {"role": "user", "content": "second prompt", "id": "u2", "timestamp": 102.0},
        {"role": "assistant", "content": "answer 2", "id": "a2", "timestamp": 103.0},
    ]
    s.context_messages = [dict(m) for m in s.messages]

    monkeypatch.setattr(config, "SESSION_DIR", str(sessions_dir))
    monkeypatch.setattr(models, "SESSION_DIR", sessions_dir)
    monkeypatch.setattr(models, "_active_state_db_path", lambda: state_db)
    monkeypatch.setattr(models, "get_state_db_session_messages",
                        lambda session_id, **kw: _read_state_rows(state_db, session_id))
    s.save()
    monkeypatch.setitem(models.SESSIONS, sid, s)
    return SimpleNamespace(sid=sid, session=s, state_db=str(state_db))


def _read_state_rows(state_db, session_id):
    conn = sqlite3.connect(state_db)
    try:
        rows = conn.execute(
            "SELECT id, role, content FROM messages WHERE session_id=? "
            "AND (active IS NULL OR active != 0) ORDER BY id", (session_id,)).fetchall()
    finally:
        conn.close()
    return [{"id": rid, "role": role, "content": content, "active": 1}
            for rid, role, content in rows]


def _active_ids(state_db, sid):
    conn = sqlite3.connect(state_db)
    try:
        return sorted(r[0] for r in conn.execute(
            "SELECT id FROM messages WHERE session_id=? AND (active IS NULL OR active != 0)",
            (sid,)).fetchall())
    finally:
        conn.close()


def test_display_addressed_restore_by_message_id(env):
    res = session_ops.restore_checkpoint_to_display_message(env.sid, message_id="u2")
    assert res["restore_mode"] == "exact"
    assert res["restored_to_row_id"] == 13
    assert res["archived_state_row_ids"] == [13, 14]
    assert res["new_message_count"] == 2
    assert [m["content"] for m in env.session.messages] == ["first prompt", "answer 1"]
    assert _active_ids(env.state_db, env.sid) == [11, 12]
    # context_messages (model-facing transcript) aligned to the same prefix
    ctx = env.session.context_messages
    assert isinstance(ctx, list)
    assert len(ctx) == 2
    assert ctx[-1]["content"] == "answer 1"


def test_display_addressed_restore_by_msg_idx_with_ts(env):
    res = session_ops.restore_checkpoint_to_display_message(
        env.sid, msg_idx=2, message_ts=102.0)
    assert res["restored_to_row_id"] == 13
    assert res["new_message_count"] == 2


def test_display_addressed_restore_stale_view_refused(env):
    with pytest.raises(ValueError):
        session_ops.restore_checkpoint_to_display_message(
            env.sid, msg_idx=2, message_ts=999.0)
    assert len(env.session.messages) == 4  # untouched


def test_display_addressed_restore_unknown_message_id_refused(env):
    with pytest.raises(ValueError):
        session_ops.restore_checkpoint_to_display_message(env.sid, message_id="nope")
    assert len(env.session.messages) == 4


def test_display_addressed_restore_to_first_message_wipes_tail(env):
    res = session_ops.restore_checkpoint_to_display_message(env.sid, message_id="u1")
    assert res["restored_to_row_id"] == 11
    assert res["new_message_count"] == 0
    assert res["archived_state_row_ids"] == [11, 12, 13, 14]
    assert _active_ids(env.state_db, env.sid) == []


def test_display_addressed_restore_rejects_assistant_row(env):
    with pytest.raises(ValueError):
        session_ops.restore_checkpoint_to_display_message(env.sid, message_id="a1")
    assert len(env.session.messages) == 4


def test_display_addressed_restore_requires_some_address(env):
    with pytest.raises(ValueError):
        session_ops.restore_checkpoint_to_display_message(env.sid)


def test_display_addressed_restore_archives_before_sidecar(env, monkeypatch):
    """Dual-store fail-closed: durable failure aborts ahead of the sidecar."""
    def boom(*a, **k):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(session_ops, "_archive_state_db_suffix", boom)
    with pytest.raises(sqlite3.OperationalError):
        session_ops.restore_checkpoint_to_display_message(env.sid, message_id="u2")
    assert len(env.session.messages) == 4
    on_disk = json.loads(Path(env.session.path).read_text())
    assert len(on_disk["messages"]) == 4


def test_route_accepts_display_address(env):
    """The route block must dispatch display-address payloads to the new
    primitive (and keep the durable-only contract for row_id-only bodies)."""
    import inspect
    import api.routes as routes
    src = inspect.getsource(routes)
    assert "restore_checkpoint_to_display_message" in src
    assert 'body.get("message_id")' in src
    assert 'body.get("msg_idx")' in src
    assert "row_id, message_id or msg_idx is required" in src


def test_reader_exposes_ids_without_api_content(env):
    """Regression: the shared state.db reader hides durable ids on rows that
    carry no api_content replay sidecar (it exposes ``_state_db_row_id`` only
    alongside one), so the restore path must use its own id-exposing reader —
    otherwise anchor validation and suffix archiving silently no-op."""
    rows = session_ops._read_active_state_db_rows(env.sid)
    assert rows is not None
    assert [r["id"] for r in rows] == [11, 12, 13, 14]
    assert session_ops._state_db_active_rows_from(env.sid, 13) == [13, 14]
    assert session_ops._state_db_active_rows_after(env.sid, 13) == [13, 14]
    assert session_ops._state_db_active_row_by_id(env.sid, 13)["role"] == "user"
