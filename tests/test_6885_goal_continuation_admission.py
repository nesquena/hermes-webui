"""#6885 admission correction: the goal-continuation marker must not
misclassify a genuine user/queued turn as goal-related.

#1932's PENDING_GOAL_CONTINUATION is a session-scoped marker: when
goal_continue fires, the streaming worker adds the session id, and
routes.py's consumer flips the NEXT /chat/start for that session into
goal_related. The marker carries no continuation text, so ANY next turn
— including a genuine user message typed before the browser's
auto-dispatch POST — is classified as goal-related. #6885 narrows the
consumer: only a turn whose text equals the pending continuation prompt
consumes the marker; anything else keeps normal user priority and
leaves the marker for the real continuation dispatch.

RED/GREEN evidence:
- pre-fix: the helper does not exist, collection errors (RED);
- post-fix: helper matches text; genuine user turn leaves the marker
  intact; whitespace-insensitive; fail-closed when prompt text is
  missing (GREEN).
"""
import re
from pathlib import Path

import pytest

from api.config import (
    PENDING_GOAL_CONTINUATION,
    PENDING_GOAL_CONTINUATION_PROMPTS,
)
from api.routes import _consume_pending_goal_continuation


@pytest.fixture(autouse=True)
def _clean_markers():
    PENDING_GOAL_CONTINUATION.clear()
    PENDING_GOAL_CONTINUATION_PROMPTS.clear()
    yield
    PENDING_GOAL_CONTINUATION.clear()
    PENDING_GOAL_CONTINUATION_PROMPTS.clear()


def test_marker_consumed_only_when_text_matches_prompt():
    """Browser auto-dispatch posts the continuation_prompt verbatim;
    that turn consumes the marker and becomes goal-related."""
    PENDING_GOAL_CONTINUATION.add("s1")
    PENDING_GOAL_CONTINUATION_PROMPTS["s1"] = "continue step 2"
    assert _consume_pending_goal_continuation("s1", "continue step 2") is True
    assert "s1" not in PENDING_GOAL_CONTINUATION
    assert "s1" not in PENDING_GOAL_CONTINUATION_PROMPTS


def test_genuine_user_turn_leaves_marker_intact():
    """A user-typed message with different text must keep normal priority:
    not goal-related, and the marker must survive for the browser's real
    continuation dispatch."""
    PENDING_GOAL_CONTINUATION.add("s1")
    PENDING_GOAL_CONTINUATION_PROMPTS["s1"] = "continue step 2"
    assert _consume_pending_goal_continuation("s1", "帮我总结一下当前进度") is False
    assert "s1" in PENDING_GOAL_CONTINUATION
    assert PENDING_GOAL_CONTINUATION_PROMPTS.get("s1") == "continue step 2"


def test_no_marker_returns_false():
    assert _consume_pending_goal_continuation("s9", "anything") is False


def test_marker_without_prompt_text_is_conservative():
    """A marker present without a recorded prompt (legacy/abnormal state)
    must fail closed: not consumed, marker left in place."""
    PENDING_GOAL_CONTINUATION.add("s1")
    assert _consume_pending_goal_continuation("s1", "continue step 2") is False
    assert "s1" in PENDING_GOAL_CONTINUATION


def test_match_is_whitespace_insensitive():
    PENDING_GOAL_CONTINUATION.add("s1")
    PENDING_GOAL_CONTINUATION_PROMPTS["s1"] = "  continue step 2  "
    assert _consume_pending_goal_continuation("s1", "continue step 2") is True
    assert "s1" not in PENDING_GOAL_CONTINUATION


def test_streaming_add_point_writes_prompt_adjacent():
    """The streaming worker must record the prompt next to the marker add so
    the consumer can match text. Without it the marker is ambiguous again."""
    src = Path(__file__).parents[1].joinpath("api", "streaming.py").read_text(encoding="utf-8")
    add_idx = src.find("PENDING_GOAL_CONTINUATION.add(session_id)")
    assert add_idx != -1, "streaming.py marker add not found"
    after = src[add_idx:add_idx + 400]
    assert "PENDING_GOAL_CONTINUATION_PROMPTS[session_id]" in after, (
        "streaming.py must write PENDING_GOAL_CONTINUATION_PROMPTS[session_id] "
        "adjacent to the marker add (goal_continue path)"
    )


def test_gateway_add_point_writes_prompt_adjacent():
    src = Path(__file__).parents[1].joinpath("api", "gateway_chat.py").read_text(encoding="utf-8")
    add_idx = src.find("PENDING_GOAL_CONTINUATION.add(session_id)")
    assert add_idx != -1, "gateway_chat.py marker add not found"
    after = src[add_idx:add_idx + 400]
    assert "PENDING_GOAL_CONTINUATION_PROMPTS[session_id]" in after, (
        "gateway_chat.py must write PENDING_GOAL_CONTINUATION_PROMPTS[session_id] "
        "adjacent to the marker add (goal_continue path)"
    )


def test_routes_consumer_routes_through_helper():
    """routes.py must consume via the helper (single atomic check+discard)
    and must not discard the marker anywhere else."""
    src = Path(__file__).parents[1].joinpath("api", "routes.py").read_text(encoding="utf-8")
    # The admission block calls the helper with session_id and msg.
    m = re.search(
        r"if not goal_related and _consume_pending_goal_continuation\(\s*s\.session_id,\s*msg\s*\):",
        src,
    )
    assert m is not None, (
        "routes.py admission must route through "
        "_consume_pending_goal_continuation(s.session_id, msg)"
    )
    # No stray direct discard outside the helper definition (helper keeps
    # check + discard atomic in one place).
    helper = re.search(
        r"def _consume_pending_goal_continuation\(.*?PENDING_GOAL_CONTINUATION\.discard",
        src,
        re.DOTALL,
    )
    assert helper is not None, "helper must contain the atomic discard"
    direct = re.findall(r"PENDING_GOAL_CONTINUATION\.discard", src)
    assert len(direct) == 1, (
        f"PENDING_GOAL_CONTINUATION.discard must appear exactly once in routes.py "
        f"(inside the helper), found {len(direct)}"
    )
