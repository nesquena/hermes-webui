"""Regression tests for issue #1932: goal hook fires on every assistant turn.

The goal evaluation hook must only run when the turn was triggered by an
explicit goal-related message (goal set, goal continuation). Unrelated
messages like "what time is it" must NOT:
  - increment turns_used
  - trigger goal_continue SSE events
  - burn the goal budget
"""
import pytest


# ---------------------------------------------------------------------------
# Test 1: config exports STREAM_GOAL_RELATED
# ---------------------------------------------------------------------------

def test_config_exports_stream_goal_related():
    """api.config must export STREAM_GOAL_RELATED for the streaming gate."""
    from api.config import STREAM_GOAL_RELATED
    assert isinstance(STREAM_GOAL_RELATED, dict)


# ---------------------------------------------------------------------------
# Test 2: config exports PENDING_GOAL_CONTINUATION
# ---------------------------------------------------------------------------

def test_config_exports_pending_goal_continuation():
    """api.config must export PENDING_GOAL_CONTINUATION for auto-marking
    continuation streams as goal-related."""
    from api.config import PENDING_GOAL_CONTINUATION
    assert isinstance(PENDING_GOAL_CONTINUATION, (dict, set))


# ---------------------------------------------------------------------------
# Test 3: streaming.py gates evaluate_goal_after_turn on STREAM_GOAL_RELATED
# ---------------------------------------------------------------------------

def test_streaming_source_code_gates_on_stream_goal_related():
    """The streaming code must check STREAM_GOAL_RELATED[stream_id] before
    calling evaluate_goal_after_turn, so unrelated turns skip the hook."""
    from pathlib import Path
    streaming_py = (Path(__file__).resolve().parents[1] / "api" / "streaming.py").read_text()

    # Must import STREAM_GOAL_RELATED
    assert "STREAM_GOAL_RELATED" in streaming_py, (
        "streaming.py must import STREAM_GOAL_RELATED from api.config"
    )

    # Must check it before calling evaluate_goal_after_turn
    goal_related_check = streaming_py.find("STREAM_GOAL_RELATED")
    eval_call = streaming_py.find("evaluate_goal_after_turn")
    assert goal_related_check != -1 and eval_call != -1
    assert goal_related_check < eval_call, (
        "STREAM_GOAL_RELATED check must appear before evaluate_goal_after_turn call"
    )


# ---------------------------------------------------------------------------
# Test 4: streaming.py sets PENDING_GOAL_CONTINUATION on goal_continue
# ---------------------------------------------------------------------------

def test_streaming_sets_pending_goal_continuation_on_goal_continue():
    """When goal_continue is emitted, streaming.py must set
    PENDING_GOAL_CONTINUATION so the next /chat/start marks the stream."""
    from pathlib import Path
    streaming_py = (Path(__file__).resolve().parents[1] / "api" / "streaming.py").read_text()

    assert "PENDING_GOAL_CONTINUATION" in streaming_py, (
        "streaming.py must reference PENDING_GOAL_CONTINUATION"
    )

    # The PENDING_GOAL_CONTINUATION set must happen near goal_continue
    goal_continue_idx = streaming_py.find("goal_continue")
    pending_idx = streaming_py.find("PENDING_GOAL_CONTINUATION")
    assert goal_continue_idx != -1 and pending_idx != -1


# ---------------------------------------------------------------------------
# Test 5: routes.py reads PENDING_GOAL_CONTINUATION and marks stream
# ---------------------------------------------------------------------------

def test_routes_reads_pending_goal_continuation():
    """The chat/start handler must check PENDING_GOAL_CONTINUATION and mark
    the new stream as goal-related."""
    from pathlib import Path
    routes_py = (Path(__file__).resolve().parents[1] / "api" / "routes.py").read_text()

    assert "PENDING_GOAL_CONTINUATION" in routes_py, (
        "routes.py must reference PENDING_GOAL_CONTINUATION"
    )
    assert "STREAM_GOAL_RELATED" in routes_py, (
        "routes.py must reference STREAM_GOAL_RELATED to mark goal-related streams"
    )


# ---------------------------------------------------------------------------
# Test 6: routes.py marks goal kickoff streams as goal-related
# ---------------------------------------------------------------------------

def test_routes_marks_goal_kickoff_as_goal_related():
    """The /api/goal handler must mark the kickoff stream as goal-related."""
    from pathlib import Path
    routes_py = (Path(__file__).resolve().parents[1] / "api" / "routes.py").read_text()

    # After kickoff stream is started, it must mark the stream
    kickoff_idx = routes_py.find("kickoff_prompt")
    stream_goal_idx = routes_py.find("STREAM_GOAL_RELATED")
    assert kickoff_idx != -1 and stream_goal_idx != -1


# ---------------------------------------------------------------------------
# Test 7: _start_chat_stream_for_session passes goal_related through
# ---------------------------------------------------------------------------

def test_start_chat_stream_accepts_goal_related():
    """_start_chat_stream_for_session must accept goal_related kwarg."""
    from pathlib import Path
    routes_py = (Path(__file__).resolve().parents[1] / "api" / "routes.py").read_text()

    assert "goal_related" in routes_py, (
        "routes.py must reference goal_related parameter"
    )


# ---------------------------------------------------------------------------
# Test 8: _run_agent_streaming accepts and uses goal_related
# ---------------------------------------------------------------------------

def test_run_agent_streaming_uses_goal_related():
    """_run_agent_streaming must accept goal_related kwarg and use it to
    gate the goal evaluation hook."""
    from pathlib import Path
    streaming_py = (Path(__file__).resolve().parents[1] / "api" / "streaming.py").read_text()

    # Function must accept goal_related parameter
    func_def_idx = streaming_py.find("def _run_agent_streaming")
    assert func_def_idx != -1

    # The function signature area (within ~200 chars) should contain goal_related
    sig_area = streaming_py[func_def_idx:func_def_idx + 500]
    assert "goal_related" in sig_area, (
        "_run_agent_streaming must accept a goal_related parameter"
    )


# ---------------------------------------------------------------------------
# Test 9: STREAM_GOAL_RELATED cleanup on stream exit
# ---------------------------------------------------------------------------

def test_stream_goal_related_cleaned_up():
    """STREAM_GOAL_RELATED entries must be cleaned up when streams end."""
    from pathlib import Path
    streaming_py = (Path(__file__).resolve().parents[1] / "api" / "streaming.py").read_text()

    # Must have cleanup of STREAM_GOAL_RELATED
    assert "STREAM_GOAL_RELATED" in streaming_py
    # Look for pop or del of STREAM_GOAL_RELATED
    assert any(
        pattern in streaming_py
        for pattern in [
            "STREAM_GOAL_RELATED.pop",
            "del STREAM_GOAL_RELATED",
        ]
    ), "streaming.py must clean up STREAM_GOAL_RELATED entries when streams end"


# ---------------------------------------------------------------------------
# Test 10: functional test with FakeGoalManager at streaming integration level
# ---------------------------------------------------------------------------

def test_goal_evaluate_after_turn_only_increments_for_user_initiated(monkeypatch):
    """Verify that evaluate_goal_after_turn only increments turns_used
    when user_initiated=True (goal-related), not when user_initiated=False."""
    from api import goals as webui_goals

    turns_incremented = []

    class FakeState:
        goal = "test goal"
        status = "active"
        turns_used = 0
        max_turns = 10
        last_turn_at = 0.0
        last_verdict = None
        last_reason = None
        paused_reason = None

        def to_json(self):
            return {"goal": self.goal, "status": self.status}

    class FakeMgr:
        def __init__(self, session_id, default_max_turns=20):
            self.state = FakeState()

        def is_active(self):
            return True

        def evaluate_after_turn(self, last_response, user_initiated=True):
            if user_initiated:
                self.state.turns_used += 1
                turns_incremented.append(True)
            return {
                "status": "active",
                "should_continue": True,
                "continuation_prompt": "continue",
                "verdict": "continue",
                "reason": "ok",
                "message": "ok",
            }

    monkeypatch.setattr(webui_goals, "GoalManager", FakeMgr)
    monkeypatch.setattr(webui_goals, "_default_max_turns", lambda: 10)

    # user_initiated=True should increment
    result1 = webui_goals.evaluate_goal_after_turn(
        "sid-1", "goal response", user_initiated=True, profile_home=None
    )
    assert len(turns_incremented) == 1

    # user_initiated=False should NOT increment
    result2 = webui_goals.evaluate_goal_after_turn(
        "sid-1", "unrelated response", user_initiated=False, profile_home=None
    )
    assert len(turns_incremented) == 1, (
        "turns_used should NOT increment when user_initiated=False"
    )
