"""A standing goal must not continue while the session's subagents are still running."""
import sqlite3

import pytest

from api import goals


def _db(home, rows):
    conn = sqlite3.connect(home / "state.db")
    conn.execute(
        "CREATE TABLE async_delegations (delegation_id TEXT PRIMARY KEY, origin_session TEXT,"
        " parent_session_id TEXT, origin_ui_session_id TEXT, state TEXT)"
    )
    conn.executemany("INSERT INTO async_delegations VALUES (?,?,?,?,?)", rows)
    conn.commit()
    conn.close()


CONTINUE = {"should_continue": True, "continuation_prompt": "go on", "status": "active", "verdict": "continue"}


@pytest.mark.parametrize("state", ["running", "stalling", "finalizing"])
def test_continuation_is_held_while_a_subagent_runs(tmp_path, state):
    _db(tmp_path, [("d1", "k", "sid", "sid", state)])
    out = goals._hold_while_delegations_run(dict(CONTINUE), "sid", tmp_path)
    assert out["should_continue"] is False
    assert out["continuation_prompt"] is None
    assert out["verdict"] == "wait"


def test_continuation_proceeds_once_subagents_finished(tmp_path):
    _db(tmp_path, [("d1", "k", "sid", "sid", "completed"), ("d2", "k", "other", "other", "running")])
    assert goals._hold_while_delegations_run(dict(CONTINUE), "sid", tmp_path) == CONTINUE


def test_missing_state_db_never_blocks(tmp_path):
    assert goals._hold_while_delegations_run(dict(CONTINUE), "sid", tmp_path) == CONTINUE


def test_evaluate_goal_after_turn_holds_with_live_delegation(tmp_path, monkeypatch):
    _db(tmp_path, [("d1", "k", "sid", "sid", "running")])

    seen = []

    class Mgr:
        state = None

        def is_active(self):
            return True

        def evaluate_after_turn(self, last_response, **kw):
            seen.append(kw.get("active_delegations", 0))
            return dict(CONTINUE)

    monkeypatch.setattr(goals, "_manager", lambda sid, profile_home=None: Mgr())
    out = goals.evaluate_goal_after_turn("sid", "status report", profile_home=tmp_path)
    # Asserted here, not in the evaluator: production swallows evaluator exceptions.
    assert seen and seen[0] >= 1
    assert out["verdict"] == "wait"
    assert out["should_continue"] is False


def test_gather_background_state_falls_through_to_state_db_without_agent_cli(tmp_path, monkeypatch):
    """No importable hermes_cli.goals must not hide delegations recorded in state.db."""
    import sys

    _db(tmp_path, [("d1", "k", "sid", "sid", "running")])
    monkeypatch.setitem(sys.modules, "hermes_cli.goals", None)  # forces ImportError
    bg, active = goals._gather_goal_turn_background_state("sid", profile_home=tmp_path)
    assert bg is None
    assert active == 1


@pytest.mark.parametrize(
    "row",
    [
        ("d1", "sid", None, None, "running"),  # origin_session only
        ("d1", None, None, "sid", "running"),  # origin_ui_session_id only
        ("d1", None, "sid", None, "running"),  # parent_session_id only
    ],
    ids=["origin_session", "origin_ui_session_id", "parent_session_id"],
)
def test_every_ledger_session_selector_holds_the_continuation(tmp_path, row):
    """Same selectors as api.delegation_status: any one matching column is a live subagent."""
    _db(tmp_path, [row])
    assert goals._state_db_live_delegations("sid", tmp_path) == 1
    assert goals._hold_while_delegations_run(dict(CONTINUE), "sid", tmp_path)["verdict"] == "wait"


def _real_schema_db(home, rows):
    """async_delegations with the Agent's real column names (persistence/sqlite/_schema_ddl.py)."""
    conn = sqlite3.connect(home / "state.db")
    conn.execute(
        "CREATE TABLE async_delegations (delegation_id TEXT PRIMARY KEY, origin_session TEXT NOT NULL,"
        " origin_ui_session_id TEXT NOT NULL DEFAULT '', parent_session_id TEXT, state TEXT NOT NULL,"
        " dispatched_at REAL NOT NULL, completed_at REAL, updated_at REAL NOT NULL,"
        " delivery_state TEXT NOT NULL DEFAULT 'pending', origin_session_id TEXT NOT NULL DEFAULT '')"
    )
    conn.executemany(
        "INSERT INTO async_delegations (delegation_id, origin_session, origin_ui_session_id,"
        " parent_session_id, state, dispatched_at, updated_at) VALUES (?,?,?,?,?,1,1)",
        rows,
    )
    conn.commit()
    conn.close()


def test_held_goal_resumes_when_last_completion_lands_inside_an_ordinary_turn(tmp_path, monkeypatch):
    """A held continuation is released by the next settled turn once no subagent is live."""
    sid = "sid-held-ordinary-turn"
    _real_schema_db(tmp_path, [("d1", sid, sid, sid, "running")])
    held = goals._hold_while_delegations_run(dict(CONTINUE), sid, tmp_path, remember=True)
    assert held["verdict"] == "wait"
    started = []
    monkeypatch.setattr(goals, "has_active_goal", lambda *_a, **_kw: True)
    monkeypatch.setattr(
        goals, "start_detached_goal_continuation", lambda *a, **k: started.append(a)
    )
    try:
        # Still running: an ordinary turn's teardown keeps the hold.
        goals.settle_goal_continuation_after_turn(sid, "", "", cancelled=False)
        assert started == [] and goals.peek_pending_goal_continuation_id(sid) is None
        # A Stop never releases it either.
        conn = sqlite3.connect(tmp_path / "state.db")
        conn.execute("UPDATE async_delegations SET state='completed'")
        conn.commit()
        conn.close()
        goals.settle_goal_continuation_after_turn(sid, "", "", cancelled=True)
        assert started == []
        # The ordinary turn that folded the last completion releases it exactly once.
        goals.settle_goal_continuation_after_turn(sid, "", "", cancelled=False)
        cid = goals.peek_pending_goal_continuation_id(sid)
        assert cid and started == [(sid, "go on", cid)]
        # Later teardowns only retry that same record (admission consumes it once).
        goals.settle_goal_continuation_after_turn(sid, "", "", cancelled=False)
        assert {a[2] for a in started} == {cid}
        assert sid not in goals._HELD_GOAL_CONTINUATIONS
    finally:
        goals.clear_pending_goal_continuation(sid)
