"""Behavioral tests for stale user-context contamination repair.

The agent's defensive `repair_message_sequence()` can concatenate a prior
context-tail user row with the current submitted turn as
``<previous tail user>\\n\\n<current user>``. WebUI must not persist that
polluted merged string as the current user turn in either visible
``messages`` or model-facing ``context_messages``.

These tests exercise the helpers in ``api.streaming`` directly so the
behaviour can be verified without a live chat/stream round-trip.
"""

import pytest


PRIOR_TAIL = "please use the larger context model"
CURRENT_TURN = "can you summarize the release blockers?"
POLLUTED = f"{PRIOR_TAIL}\n\n{CURRENT_TURN}"


def _text(value):
    """Extract plain text from a message's content field (str or list)."""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return " ".join(
            part.get("text", "")
            for part in value
            if isinstance(part, dict) and isinstance(part.get("text"), str)
        )
    return str(value or "")


def test_detect_stale_user_merge_matches_polluted_pair():
    """Detector must flag the polluted merge as a stale-prefixed current turn."""
    from api.streaming import _detect_stale_user_merge

    polluted_msg = {"role": "user", "content": POLLUTED}
    assert _detect_stale_user_merge(polluted_msg, CURRENT_TURN, PRIOR_TAIL) is True


def test_detect_stale_user_merge_does_not_match_clean_current():
    """A clean current turn that happens to mention the prior phrase stays intact."""
    from api.streaming import _detect_stale_user_merge

    clean_msg = {"role": "user", "content": CURRENT_TURN}
    assert _detect_stale_user_merge(clean_msg, CURRENT_TURN, PRIOR_TAIL) is False


def test_detect_stale_user_merge_does_not_match_when_tail_differs():
    """If the prior tail is different, the merge is not the repair pattern."""
    from api.streaming import _detect_stale_user_merge

    other_msg = {"role": "user", "content": f"other stale\n\n{CURRENT_TURN}"}
    assert _detect_stale_user_merge(other_msg, CURRENT_TURN, PRIOR_TAIL) is False


def test_detect_stale_user_merge_ignores_non_user_roles():
    """Only user rows are candidates for the repair-merge pattern."""
    from api.streaming import _detect_stale_user_merge

    assistant_msg = {"role": "assistant", "content": POLLUTED}
    assert _detect_stale_user_merge(assistant_msg, CURRENT_TURN, PRIOR_TAIL) is False


def test_detect_stale_user_merge_handles_workspace_prefixed_row():
    """Workspace-prefixed model rows still match when stripped."""
    from api.streaming import _detect_stale_user_merge

    prefixed = {
        "role": "user",
        "content": f"[Workspace::v1: /tmp/project]\n{POLLUTED}",
    }
    assert _detect_stale_user_merge(prefixed, CURRENT_TURN, PRIOR_TAIL) is True


def test_detect_stale_user_merge_handles_workspace_prefix_on_both_halves():
    """Repair can concatenate two separately workspace-prefixed user rows."""
    from api.streaming import _detect_stale_user_merge

    prefixed_both = {
        "role": "user",
        "content": (
            f"[Workspace::v1: /tmp/project]\n{PRIOR_TAIL}\n\n"
            f"[Workspace::v1: /tmp/project]\n{CURRENT_TURN}"
        ),
    }
    assert _detect_stale_user_merge(prefixed_both, CURRENT_TURN, PRIOR_TAIL) is True


def test_detect_stale_user_merge_does_not_match_single_newline_joined():
    """A single-\\n separator is not the repair shape and must not be flagged."""
    from api.streaming import _detect_stale_user_merge

    single_nl = {"role": "user", "content": f"{PRIOR_TAIL}\n{CURRENT_TURN}"}
    assert _detect_stale_user_merge(single_nl, CURRENT_TURN, PRIOR_TAIL) is False


def test_detect_stale_user_merge_does_not_match_space_joined():
    """A space-only separator is not the repair shape and must not be flagged."""
    from api.streaming import _detect_stale_user_merge

    space_joined = {"role": "user", "content": f"{PRIOR_TAIL} {CURRENT_TURN}"}
    assert _detect_stale_user_merge(space_joined, CURRENT_TURN, PRIOR_TAIL) is False


def test_strip_stale_user_merge_from_messages_replaces_polluted_row():
    """Normalizer replaces a polluted current row with a clean copy of msg_text."""
    from api.streaming import _strip_stale_user_merge_from_messages

    messages = [
        {"role": "user", "content": POLLUTED},
        {"role": "assistant", "content": "Sure, checking that now."},
    ]

    cleaned = _strip_stale_user_merge_from_messages(messages, CURRENT_TURN, PRIOR_TAIL)

    assert cleaned[0]["content"] == CURRENT_TURN
    assert cleaned[0]["role"] == "user"
    assert cleaned[1] == messages[1]


def test_strip_stale_user_merge_handles_list_content_row():
    """Normalizer also handles OpenAI-style list content payloads."""
    from api.streaming import _strip_stale_user_merge_from_messages

    messages = [
        {
            "role": "user",
            "content": [{"type": "text", "text": POLLUTED}],
        },
        {"role": "assistant", "content": "pushing now"},
    ]

    cleaned = _strip_stale_user_merge_from_messages(messages, CURRENT_TURN, PRIOR_TAIL)

    assert cleaned[0]["content"] == CURRENT_TURN
    assert cleaned[1] == messages[1]


def test_strip_stale_user_merge_does_not_touch_clean_rows():
    """Clean rows, assistant rows, and tool rows must pass through untouched."""
    from api.streaming import _strip_stale_user_merge_from_messages

    messages = [
        {"role": "user", "content": PRIOR_TAIL},
        {"role": "assistant", "content": "ok noted"},
        {"role": "user", "content": CURRENT_TURN},
        {"role": "tool", "content": "result", "tool_call_id": "x"},
    ]

    cleaned = _strip_stale_user_merge_from_messages(messages, CURRENT_TURN, PRIOR_TAIL)

    assert [m["content"] for m in cleaned] == [
        PRIOR_TAIL,
        "ok noted",
        CURRENT_TURN,
        "result",
    ]


def test_deduplicate_context_messages_cleans_polluted_current_user_in_result():
    """Result-normalization must leave no polluted current user row behind.

    This is the end-to-end assertion the spec asks for: the post-merge
    `context_messages` must contain a clean current user turn, not the
    stale-merged pair.
    """
    from api.streaming import (
        _deduplicate_context_messages,
        _dedupe_replayed_context_messages,
    )

    previous_context = [
        {"role": "user", "content": "are we ready?"},
        {"role": "assistant", "content": "almost, hold on"},
        {"role": "user", "content": PRIOR_TAIL},
    ]
    result_messages = [
        *previous_context,
        {"role": "user", "content": POLLUTED},
        {"role": "assistant", "content": "pushing now"},
    ]

    cleaned = _dedupe_replayed_context_messages(
        previous_context,
        result_messages,
        CURRENT_TURN,
    )
    cleaned = _deduplicate_context_messages(cleaned)

    polluted_rows = [
        m
        for m in cleaned
        if isinstance(m, dict)
        and m.get("role") == "user"
        and POLLUTED in _text(m.get("content", ""))
    ]
    assert not polluted_rows, (
        f"No user content should equal or start with the polluted pair; got: "
        f"{[m.get('content') for m in cleaned]}"
    )

    current_rows = [
        m
        for m in cleaned
        if isinstance(m, dict)
        and m.get("role") == "user"
        and _text(m.get("content", "")).strip() == CURRENT_TURN
    ]
    assert current_rows, (
        f"Persisted context should contain the clean current user turn; got: "
        f"{[m.get('content') for m in cleaned]}"
    )


def test_dedupe_replayed_context_handles_repair_replaced_tail_user_row():
    """Context merge handles repaired rows that replace the prior tail user row.

    Some repair paths return the shared prefix only through the item before the
    prior user tail, then put the repair-merged current user row at the old tail
    position. WebUI should preserve the old tail and append the clean current
    turn, never persist the polluted joined content.
    """
    from api.streaming import _dedupe_replayed_context_messages

    previous_context = [
        {"role": "user", "content": "are we ready?"},
        {"role": "assistant", "content": "almost, hold on"},
        {"role": "user", "content": PRIOR_TAIL},
    ]
    result_messages = [
        previous_context[0],
        previous_context[1],
        {"role": "user", "content": POLLUTED},
        {"role": "assistant", "content": "pushing now"},
    ]

    cleaned = _dedupe_replayed_context_messages(
        previous_context,
        result_messages,
        CURRENT_TURN,
    )

    assert [m.get("content") for m in cleaned] == [
        "are we ready?",
        "almost, hold on",
        PRIOR_TAIL,
        CURRENT_TURN,
        "pushing now",
    ]
    assert not any(
        isinstance(m, dict) and POLLUTED in _text(m.get("content", ""))
        for m in cleaned
    )


def test_merge_display_drops_polluted_current_when_eager_checkpoint_clean():
    """Display merge must not append a polluted row next to a clean eager checkpoint.

    The visible transcript should keep exactly one clean current user row and
    append only the assistant response — not produce two adjacent user rows.
    """
    from api.streaming import _merge_display_messages_after_agent_result

    previous_display = [
        {"role": "user", "content": "are we ready?"},
        {"role": "assistant", "content": "almost, hold on"},
        {"role": "user", "content": CURRENT_TURN},  # eager checkpoint
    ]
    previous_context = [
        {"role": "user", "content": "are we ready?"},
        {"role": "assistant", "content": "almost, hold on"},
        {"role": "user", "content": PRIOR_TAIL},
    ]
    result_messages = [
        *previous_context,
        {"role": "user", "content": POLLUTED},  # polluted merge from repair
        {"role": "assistant", "content": "pushing now"},
    ]

    merged = _merge_display_messages_after_agent_result(
        previous_display,
        previous_context,
        result_messages,
        CURRENT_TURN,
    )

    user_texts = [
        _text(m.get("content", "")).strip()
        for m in merged
        if isinstance(m, dict) and m.get("role") == "user"
    ]
    assert user_texts.count(CURRENT_TURN) == 1, (
        f"Should keep exactly one clean current user row; got user rows: {user_texts}"
    )
    assert not any(POLLUTED in t for t in user_texts), (
        f"Polluted merged user row must not appear in display; got: {user_texts}"
    )
    assert any(
        isinstance(m, dict)
        and m.get("role") == "assistant"
        and "pushing now" in _text(m.get("content", ""))
        for m in merged
    ), "Assistant response must still be appended"


def test_merge_display_does_not_overstrip_when_current_already_clean():
    """A legitimately new current turn that mentions the prior phrase stays intact."""
    from api.streaming import _merge_display_messages_after_agent_result

    previous_display = [
        {"role": "user", "content": "are we ready?"},
        {"role": "assistant", "content": "almost, hold on"},
    ]
    previous_context = [
        {"role": "user", "content": "are we ready?"},
        {"role": "assistant", "content": "almost, hold on"},
        {"role": "user", "content": PRIOR_TAIL},
    ]
    new_turn = f"re: {PRIOR_TAIL} — that helps, thanks"
    result_messages = [
        *previous_context,
        {"role": "user", "content": new_turn},
        {"role": "assistant", "content": "glad it did"},
    ]

    merged = _merge_display_messages_after_agent_result(
        previous_display,
        previous_context,
        result_messages,
        new_turn,
    )

    user_texts = [
        _text(m.get("content", "")).strip()
        for m in merged
        if isinstance(m, dict) and m.get("role") == "user"
    ]
    assert new_turn in user_texts, (
        f"Genuine current turn must remain intact; got: {user_texts}"
    )
    assert PRIOR_TAIL in user_texts, (
        f"Original prior-tail user row must remain in display; got: {user_texts}"
    )


def test_merge_display_passes_through_when_prior_tail_text_differs():
    """Detector skips when the prior-tail text does not match PRIOR_TAIL — no over-strip."""
    from api.streaming import _merge_display_messages_after_agent_result

    previous_display = [
        {"role": "user", "content": "first question"},
        {"role": "assistant", "content": "first answer"},
    ]
    previous_context = [
        {"role": "user", "content": "first question"},
        {"role": "assistant", "content": "first answer"},
    ]
    # _last_user_row finds "first question" as the prior tail, but its
    # normalized text does not match PRIOR_TAIL, so _detect_stale_user_merge
    # correctly returns False and the polluted row is not cleaned.
    polluted_msg = {"role": "user", "content": POLLUTED}
    result_messages = [
        *previous_context,
        polluted_msg,
        {"role": "assistant", "content": "answer"},
    ]

    merged = _merge_display_messages_after_agent_result(
        previous_display,
        previous_context,
        result_messages,
        CURRENT_TURN,
    )

    user_texts = [
        _text(m.get("content", "")).strip()
        for m in merged
        if isinstance(m, dict) and m.get("role") == "user"
    ]
    assert CURRENT_TURN in user_texts, (
        f"Current turn must be normalized to the clean text; got: {user_texts}"
    )
    assert PRIOR_TAIL not in user_texts, (
        f"Unmatched prior-tail text must not be synthesized as its own user row; got: {user_texts}"
    )


def test_merge_display_workspace_prefixed_polluted_row_is_cleaned():
    """The polluted row may arrive with a workspace sentinel; cleaning still works."""
    from api.streaming import _merge_display_messages_after_agent_result

    previous_display = [
        {"role": "user", "content": "are we ready?"},
        {"role": "assistant", "content": "almost, hold on"},
    ]
    previous_context = [
        {"role": "user", "content": "are we ready?"},
        {"role": "assistant", "content": "almost, hold on"},
        {"role": "user", "content": PRIOR_TAIL},
    ]
    prefixed_polluted = {
        "role": "user",
        "content": f"[Workspace::v1: /tmp/project]\n{POLLUTED}",
    }
    result_messages = [
        *previous_context,
        prefixed_polluted,
        {"role": "assistant", "content": "pushing now"},
    ]

    merged = _merge_display_messages_after_agent_result(
        previous_display,
        previous_context,
        result_messages,
        CURRENT_TURN,
    )

    user_texts = [
        _text(m.get("content", "")).strip()
        for m in merged
        if isinstance(m, dict) and m.get("role") == "user"
    ]
    assert user_texts.count(CURRENT_TURN) == 1, (
        f"Should keep exactly one clean current user row; got: {user_texts}"
    )
    assert not any(POLLUTED in t for t in user_texts), (
        f"Polluted row must be normalized away; got: {user_texts}"
    )


def test_dedupe_replayed_context_preserves_historical_row_with_merge_shape():
    """A historical user row shaped like <previous_user_tail>\\n\\n<msg_text> must not be rewritten.

    If a prior conversation turn happens to have content exactly matching the
    stale-merge pattern, _dedupe_replayed_context_messages must not rewrite it:
    only the new-turn boundary/candidate slice is eligible for stale-merge cleanup.
    """
    from api.streaming import _dedupe_replayed_context_messages

    HISTORICAL_SHAPE = f"{PRIOR_TAIL}\n\n{CURRENT_TURN}"

    previous_context = [
        {"role": "user", "content": HISTORICAL_SHAPE},  # legitimate old turn
        {"role": "assistant", "content": "summary from that turn"},
        {"role": "user", "content": PRIOR_TAIL},         # becomes previous_user_tail
    ]
    result_messages = [
        *previous_context,
        {"role": "user", "content": CURRENT_TURN},       # clean current turn
        {"role": "assistant", "content": "new response"},
    ]

    cleaned = _dedupe_replayed_context_messages(
        previous_context,
        result_messages,
        CURRENT_TURN,
    )

    assert any(
        isinstance(m, dict) and m.get("content") == HISTORICAL_SHAPE
        for m in cleaned
    ), (
        "Historical user row shaped like the stale merge must remain unchanged; "
        f"got: {[m.get('content') for m in cleaned]}"
    )
    assert any(
        isinstance(m, dict) and m.get("role") == "user" and m.get("content") == CURRENT_TURN
        for m in cleaned
    ), (
        "Clean current user turn must appear in returned context; "
        f"got: {[m.get('content') for m in cleaned]}"
    )


def test_stale_tail_candidate_returns_normalized_text():
    """_stale_user_tail_candidate normalizes whitespace and strips workspace prefix."""
    from api.streaming import _stale_user_tail_candidate

    msg = {
        "role": "user",
        "content": "[Workspace::v1: /tmp/project]\n  please  use  the larger context model  ",
    }
    assert _stale_user_tail_candidate(msg) == "please use the larger context model"

    non_user = {"role": "assistant", "content": "please use the larger context model"}
    assert _stale_user_tail_candidate(non_user) is None

    empty = {"role": "user", "content": ""}
    assert _stale_user_tail_candidate(empty) is None


def test_last_user_row_returns_trailing_user_message():
    """_last_user_row returns the most recent user message in the list."""
    from api.streaming import _last_user_row

    messages = [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "ok"},
        {"role": "user", "content": "second"},
    ]
    assert _last_user_row(messages) == {"role": "user", "content": "second"}
    assert _last_user_row([]) is None
    assert _last_user_row([{"role": "assistant", "content": "no user"}]) is None


# --- helpers ----------------------------------------------------------------


def _stale_tail_for(messages):
    """Re-derive the normalized prior-tail text used by the production call sites."""
    from api.streaming import _last_user_row, _stale_user_tail_candidate

    return _stale_user_tail_candidate(_last_user_row(messages))


if __name__ == "__main__":  # pragma: no cover - allow `python tests/...py`
    raise SystemExit(pytest.main([__file__, "-q"]))
