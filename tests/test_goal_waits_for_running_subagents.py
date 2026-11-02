"""A standing goal must not continue while the session's subagents are still running."""
import sqlite3

import pytest

from api import goals


def _db(home, rows):
    conn = sqlite3.connect(home / "state.db")
    conn.execute(
        "CREATE TABLE async_delegations (delegation_id TEXT PRIMARY KEY, origin_session TEXT,"
        " parent_session_id TEXT, origin_session_id TEXT, state TEXT)"
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
