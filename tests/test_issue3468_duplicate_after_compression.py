"""Regression: repeated/similar user turns must not replay the whole history
after context compression (#3468).

``_find_current_user_turn`` locates the index in the agent's ``result_messages``
from which the current turn's new messages begin; the merge then takes
``result_messages[idx:]`` as the turn candidates. After one or more context
compression cycles, ``result_messages`` carries the FULL conversation history.
When the user asks a similar (or identical) question across turns, a *first*-match
scan returns the OLD turn's index, so the candidate slice includes the entire
replayed history and the merged transcript accumulates duplicate messages
(observed: a 137-message session where 89 were duplicate replays).

The fix returns the *last* matching user turn, so the candidate slice begins at
the current turn and the replayed history is not re-appended. These tests pin
that behavior at both the unit level (``_find_current_user_turn``) and the
integration level (``_merge_display_messages_after_agent_result``).
"""

from __future__ import annotations

from api.streaming import (
    _find_current_user_turn,
    _merge_display_messages_after_agent_result,
)


def test_find_current_user_turn_returns_last_matching_turn():
    """With the same user text appearing in an earlier AND the current turn,
    the function must return the LAST occurrence, not the first."""
    messages = [
        {"role": "user", "content": "recommend a stock"},
        {"role": "assistant", "content": "Here is an older answer."},
        {"role": "user", "content": "something else"},
        {"role": "assistant", "content": "Sure."},
        {"role": "user", "content": "recommend a stock"},  # current turn
    ]
    idx = _find_current_user_turn(messages, "recommend a stock")
    assert idx == 4, f"expected last matching user turn at index 4, got {idx}"


def test_find_current_user_turn_first_match_would_replay_history():
    """Demonstrate the bug shape: first-match would point at index 0 and the
    slice result_messages[0:] would replay everything. Last-match points at the
    current turn so the slice is just the current turn forward."""
    messages = [
        {"role": "user", "content": "推荐一下股票"},
        {"role": "assistant", "content": "old recommendation"},
        {"role": "user", "content": "推荐一下股票"},  # current turn, identical text
    ]
    idx = _find_current_user_turn(messages, "推荐一下股票")
    assert idx == 2
    # The slice taken by the merge starts at the CURRENT turn, not the old one.
    assert messages[idx:] == [{"role": "user", "content": "推荐一下股票"}]


def test_find_current_user_turn_fallback_when_no_match():
    """When no user message matches the needle, fall back to the last user
    index (existing behavior), not None, so the merge still has an anchor."""
    messages = [
        {"role": "user", "content": "alpha"},
        {"role": "assistant", "content": "a"},
        {"role": "user", "content": "beta"},
        {"role": "assistant", "content": "b"},
    ]
    idx = _find_current_user_turn(messages, "totally different text")
    # fallback = last user index seen (2)
    assert idx == 2


def test_find_current_user_turn_none_when_no_user_messages():
    messages = [
        {"role": "assistant", "content": "a"},
        {"role": "tool", "content": "t"},
    ]
    assert _find_current_user_turn(messages, "anything") is None


def test_strong_match_beats_later_synthetic_weak_match():
    """A later synthetic continuation prompt (agent-injected role:'user' "Continue"
    turns — see conversation_loop.py) only WEAK-matches the submitted text. The
    real current turn (an exact / strong match) must win even though the synthetic
    turn comes later, otherwise the merge anchors PAST the real turn and drops the
    assistant/tool output produced in between. (Codex pre-release finding, #3468.)"""
    msg_text = "Continue"
    messages = [
        {"role": "user", "content": "Continue"},          # idx 0 — real turn (strong/exact)
        {"role": "assistant", "content": "Working on it"},  # idx 1 — must be preserved
        {"role": "tool", "content": "tool output"},         # idx 2 — must be preserved
        # idx 3 — synthetic continuation prompt: CONTAINS "Continue" (weak match only)
        {"role": "user", "content": "[System: Continue now and finish the task. Continue]"},
    ]
    idx = _find_current_user_turn(messages, msg_text)
    # Must anchor on the REAL turn (idx 0, strong), not the synthetic one (idx 3, weak).
    assert idx == 0, f"strong match at 0 must beat later weak match at 3, got {idx}"
    # The slice from the real turn preserves the assistant + tool output.
    assert {m["role"] for m in messages[idx:]} >= {"assistant", "tool"}


def test_weak_match_used_when_no_strong_match():
    """When there's no exact (strong) match, fall back to the LAST weak substring
    match (still better than first-match for the repeated-question replay bug)."""
    msg_text = "recommend a stock"
    messages = [
        # idx 0 — weak: needle is a substring of this older, longer turn
        {"role": "user", "content": "recommend a stock for me"},
        {"role": "assistant", "content": "old"},
        # idx 2 — weak: needle is a substring of this later, longer turn
        {"role": "user", "content": "recommend a stock today"},
    ]
    # No message equals msg_text exactly, so no strong match; both are weak
    # (needle in text). Must return the LAST weak match (idx 2), not the first.
    idx = _find_current_user_turn(messages, msg_text)
    assert idx == 2, f"expected last weak match at 2, got {idx}"


def test_merge_no_duplicate_replay_on_repeated_question_after_compression():
    """Integration: the user asks the same question in turn 1 and turn 3. After
    compression result_messages carries the full history. The merged transcript
    must NOT re-append the turn-1 history — duplicates must not accumulate."""
    # The visible transcript so far (what the browser already shows).
    previous_display = [
        {"role": "user", "content": "recommend a stock"},
        {"role": "assistant", "content": "Older: consider an index fund."},
        {"role": "user", "content": "what about bonds"},
        {"role": "assistant", "content": "Bonds are lower risk."},
    ]
    # Context is empty / does not prefix-match result_messages, forcing the
    # _find_current_user_turn path (the branch the fix touches).
    previous_context: list[dict] = []
    # After compression the agent returns the FULL history plus the new turn.
    result_messages = [
        {"role": "user", "content": "recommend a stock"},
        {"role": "assistant", "content": "Older: consider an index fund."},
        {"role": "user", "content": "what about bonds"},
        {"role": "assistant", "content": "Bonds are lower risk."},
        {"role": "user", "content": "recommend a stock"},  # current turn (repeat)
        {"role": "assistant", "content": "Newer: here is a fresh pick."},
    ]
    msg_text = "recommend a stock"

    merged = _merge_display_messages_after_agent_result(
        previous_display, previous_context, result_messages, msg_text
    )

    # The new assistant answer must be present exactly once.
    newer = [m for m in merged if m.get("content") == "Newer: here is a fresh pick."]
    assert len(newer) == 1, f"new answer should appear once, got {len(newer)}"

    # The OLD history must NOT be duplicated: each prior message appears once.
    older = [m for m in merged if m.get("content") == "Older: consider an index fund."]
    assert len(older) == 1, f"old answer must not be replayed, got {len(older)}"
    bonds = [m for m in merged if m.get("content") == "Bonds are lower risk."]
    assert len(bonds) == 1, f"intermediate turn must not be replayed, got {len(bonds)}"

    # The merged transcript must not balloon: no message identity appears twice
    # except the deliberately-repeated user question (which legitimately occurs
    # in both turn 1 and turn 3).
    from api.streaming import _message_identity

    identities = [_message_identity(m) for m in merged]
    repeated_user_key = _message_identity({"role": "user", "content": "recommend a stock"})
    for ident in set(identities):
        count = identities.count(ident)
        if ident == repeated_user_key:
            assert count <= 2, "the repeated user question may appear at most twice"
        else:
            assert count == 1, f"message {ident!r} duplicated {count}x (history replay)"
