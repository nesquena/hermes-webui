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
CHAIN_TAIL_A = "could you re-check the deployment checklist"
CHAIN_TAIL_B = "also, ignore the earlier correction request"
CHAIN_CURRENT = "start from the latest verified state only"
CHAIN_POLLUTED = f"{CHAIN_TAIL_A}\n\n{CHAIN_TAIL_B}\n\n{CHAIN_CURRENT}"


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


def test_detect_stale_user_merge_prefers_context_over_mismatched_tail_fallback():
    """When previous_context is supplied, a stale row must be supported by that context."""
    from api.streaming import _detect_stale_user_merge

    polluted_msg = {"role": "user", "content": POLLUTED}
    previous_context = [{"role": "user", "content": "different prior context"}]

    assert _detect_stale_user_merge(
        polluted_msg,
        CURRENT_TURN,
        PRIOR_TAIL,
        previous_context=previous_context,
    ) is False


def test_detect_stale_user_merge_matches_multihop_chain_with_context_history():
    """Multi-hop stale merges match when historical user context is contiguous."""
    from api.streaming import _detect_stale_user_merge

    previous_context = [
        {"role": "user", "content": CHAIN_TAIL_A},
        {"role": "assistant", "content": "acknowledged"},
        {"role": "user", "content": CHAIN_TAIL_B},
    ]
    polluted_msg = {"role": "user", "content": CHAIN_POLLUTED}

    assert _detect_stale_user_merge(
        polluted_msg,
        CHAIN_CURRENT,
        CHAIN_TAIL_B,
        previous_context=previous_context,
    ) is True


def test_detect_stale_user_merge_rejects_multihop_chain_without_matching_history():
    """A matching current row is not enough when stale segments do not align."""
    from api.streaming import _detect_stale_user_merge

    previous_context = [
        {"role": "user", "content": "different first tail"},
        {"role": "assistant", "content": "skip"},
        {"role": "user", "content": CHAIN_TAIL_B},
    ]
    polluted_msg = {"role": "user", "content": CHAIN_POLLUTED}

    assert _detect_stale_user_merge(
        polluted_msg,
        CHAIN_CURRENT,
        CHAIN_TAIL_B,
        previous_context=previous_context,
    ) is False


def test_detect_stale_user_merge_preserves_legitimate_multisection_current_turn():
    """A user-authored multi-section current prompt is not the stale repair shape."""
    from api.streaming import _detect_stale_user_merge

    previous_context = [
        {"role": "user", "content": CHAIN_TAIL_A},
        {"role": "assistant", "content": "acknowledged"},
        {"role": "user", "content": CHAIN_TAIL_B},
    ]
    current_prompt = f"{CHAIN_TAIL_A}\n\n{CHAIN_TAIL_B}\n\n{CHAIN_CURRENT}"
    current_msg = {"role": "user", "content": current_prompt}

    assert _detect_stale_user_merge(
        current_msg,
        current_prompt,
        CHAIN_TAIL_B,
        previous_context=previous_context,
    ) is False


def test_detect_stale_user_merge_handles_single_hop_current_turn_with_paragraphs():
    """One-hop stale repair still matches when the submitted turn has paragraphs."""
    from api.streaming import _detect_stale_user_merge

    current_prompt = "summarize this first\n\nthen list the risks"
    polluted_msg = {"role": "user", "content": f"{PRIOR_TAIL}\n\n{current_prompt}"}

    assert _detect_stale_user_merge(
        polluted_msg,
        current_prompt,
        PRIOR_TAIL,
    ) is True


def test_detect_stale_user_merge_handles_multihop_current_turn_with_paragraphs():
    """Multi-hop stale repair uses the full current-turn suffix, not the last paragraph."""
    from api.streaming import _detect_stale_user_merge

    previous_context = [
        {"role": "user", "content": CHAIN_TAIL_A},
        {"role": "assistant", "content": "acknowledged"},
        {"role": "user", "content": CHAIN_TAIL_B},
    ]
    current_prompt = "summarize this first\n\nthen list the risks"
    polluted_msg = {
        "role": "user",
        "content": f"{CHAIN_TAIL_A}\n\n{CHAIN_TAIL_B}\n\n{current_prompt}",
    }

    assert _detect_stale_user_merge(
        polluted_msg,
        current_prompt,
        CHAIN_TAIL_B,
        previous_context=previous_context,
    ) is True


def test_detect_stale_user_merge_handles_replayed_prefix_from_old_polluted_row():
    """Already-contaminated sessions can replay an old prefix after newer clean turns."""
    from api.streaming import _detect_stale_user_merge

    stable_prefix = f"{CHAIN_TAIL_A}\n\n{CHAIN_TAIL_B}"
    previous_context = [
        {"role": "user", "content": f"{stable_prefix}\n\nold follow-up"},
        {"role": "assistant", "content": "answered the old follow-up"},
        {"role": "user", "content": "newer clean question after the polluted row"},
        {"role": "assistant", "content": "answered the newer clean question"},
    ]
    current_turn = "latest question that should stand alone"
    polluted_msg = {"role": "user", "content": f"{stable_prefix}\n\n{current_turn}"}

    assert _detect_stale_user_merge(
        polluted_msg,
        current_turn,
        "newer clean question after the polluted row",
        previous_context=previous_context,
    ) is True


def test_detect_stale_user_merge_handles_segments_replayed_from_multiple_old_rows():
    """Stale paragraphs can be assembled from earlier polluted rows, not only one row."""
    from api.streaming import _detect_stale_user_merge

    old_correction = "remove the reassurance sentence"
    attachment_request = "send that with the attached image"
    thread_check = "confirm it was sent in the right thread"
    salary_question = "what is the average salary there"
    current_turn = "should we mention the prior product by name?"
    previous_context = [
        {"role": "user", "content": f"{old_correction}\n\n{attachment_request}\n\nold current"},
        {"role": "assistant", "content": "answered the old current"},
        {"role": "user", "content": f"{thread_check}\n\n{salary_question}"},
        {"role": "assistant", "content": "answered salary"},
        {"role": "user", "content": "newer clean question"},
    ]
    polluted_msg = {
        "role": "user",
        "content": (
            f"{old_correction}\n\n{attachment_request}\n\n{thread_check}\n\n"
            f"{salary_question}\n\n{current_turn}"
        ),
    }

    assert _detect_stale_user_merge(
        polluted_msg,
        current_turn,
        "newer clean question",
        previous_context=previous_context,
    ) is True


def test_detect_stale_user_merge_rejects_replayed_segments_in_wrong_order():
    """Prior user substrings must explain stale paragraphs in chronological order."""
    from api.streaming import _detect_stale_user_merge

    first = "first stale paragraph"
    second = "second stale paragraph"
    current_turn = "latest clean question"
    previous_context = [
        {"role": "user", "content": second},
        {"role": "assistant", "content": "answered second"},
        {"role": "user", "content": first},
    ]
    polluted_msg = {"role": "user", "content": f"{first}\n\n{second}\n\n{current_turn}"}

    assert _detect_stale_user_merge(
        polluted_msg,
        current_turn,
        first,
        previous_context=previous_context,
    ) is False


def test_detect_stale_user_merge_handles_prior_row_starting_with_stale_prefix():
    """A stable stale prefix may be a leading subset of one older polluted row."""
    from api.streaming import _detect_stale_user_merge

    stable_prefix = f"{CHAIN_TAIL_A}\n\n{CHAIN_TAIL_B}"
    current_turn = "latest question that should stand alone"
    previous_context = [
        {"role": "user", "content": f"{stable_prefix}\n\nolder different follow-up"},
        {"role": "assistant", "content": "answered older follow-up"},
        {"role": "user", "content": "newer clean question"},
    ]
    polluted_msg = {"role": "user", "content": f"{stable_prefix}\n\n{current_turn}"}

    assert _detect_stale_user_merge(
        polluted_msg,
        current_turn,
        "newer clean question",
        previous_context=previous_context,
    ) is True


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


def test_strip_stale_user_merge_from_messages_replaces_multihop_polluted_row():
    """Cleaner should replace a multi-hop polluted row with the clean current text."""
    from api.streaming import _strip_stale_user_merge_from_messages

    previous_context = [
        {"role": "user", "content": CHAIN_TAIL_A},
        {"role": "assistant", "content": "done"},
        {"role": "user", "content": CHAIN_TAIL_B},
    ]
    messages = [
        {"role": "user", "content": CHAIN_POLLUTED},
        {"role": "assistant", "content": "pushing now"},
    ]

    cleaned = _strip_stale_user_merge_from_messages(
        messages,
        CHAIN_CURRENT,
        CHAIN_TAIL_B,
        previous_context=previous_context,
    )

    assert cleaned[0]["content"] == CHAIN_CURRENT
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


def test_dedupe_replayed_context_handles_multihop_repair_replaced_tail_row():
    """When repair replaces last tail row with a multi-hop merge, history is preserved."""
    from api.streaming import _dedupe_replayed_context_messages

    previous_context = [
        {"role": "user", "content": CHAIN_TAIL_A},
        {"role": "assistant", "content": "done"},
        {"role": "user", "content": CHAIN_TAIL_B},
    ]
    result_messages = [
        previous_context[0],
        previous_context[1],
        {"role": "user", "content": CHAIN_POLLUTED},
        {"role": "assistant", "content": "pushing now"},
    ]

    cleaned = _dedupe_replayed_context_messages(
        previous_context,
        result_messages,
        CHAIN_CURRENT,
    )

    assert [m.get("content") for m in cleaned] == [
        CHAIN_TAIL_A,
        "done",
        CHAIN_TAIL_B,
        CHAIN_CURRENT,
        "pushing now",
    ]
    assert not any(
        isinstance(m, dict) and CHAIN_POLLUTED in _text(m.get("content", ""))
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


def test_merge_display_drops_multihop_polluted_current_when_eager_checkpoint_clean():
    """Multi-hop stale shape is normalized before eager checkpoint dedupe logic."""
    from api.streaming import _merge_display_messages_after_agent_result

    previous_display = [
        {"role": "user", "content": CHAIN_TAIL_A},
        {"role": "assistant", "content": "done"},
        {"role": "user", "content": CHAIN_CURRENT},  # eager checkpoint
    ]
    previous_context = [
        {"role": "user", "content": CHAIN_TAIL_A},
        {"role": "assistant", "content": "done"},
        {"role": "user", "content": CHAIN_TAIL_B},
    ]
    result_messages = [
        *previous_context,
        {"role": "user", "content": CHAIN_POLLUTED},
        {"role": "assistant", "content": "pushing now"},
    ]

    merged = _merge_display_messages_after_agent_result(
        previous_display,
        previous_context,
        result_messages,
        CHAIN_CURRENT,
    )

    user_texts = [
        _text(m.get("content", "")).strip()
        for m in merged
        if isinstance(m, dict) and m.get("role") == "user"
    ]
    assert user_texts.count(CHAIN_CURRENT) == 1, (
        f"Should keep exactly one clean current user row; got user rows: {user_texts}"
    )
    assert not any(CHAIN_POLLUTED in t for t in user_texts), (
        f"Multi-hop polluted row must be removed from display; got: {user_texts}"
    )


def test_merge_display_drops_replayed_old_prefix_after_newer_clean_turn():
    """Visible transcript drops stale prefixes replayed from older polluted rows."""
    from api.streaming import _merge_display_messages_after_agent_result

    stable_prefix = f"{CHAIN_TAIL_A}\n\n{CHAIN_TAIL_B}"
    newer_clean = "newer clean question after the polluted row"
    current_turn = "latest question that should stand alone"
    polluted_current = f"{stable_prefix}\n\n{current_turn}"
    previous_display = [
        {"role": "user", "content": f"{stable_prefix}\n\nold follow-up"},
        {"role": "assistant", "content": "answered old follow-up"},
        {"role": "user", "content": newer_clean},
        {"role": "assistant", "content": "answered newer clean question"},
        {"role": "user", "content": current_turn},  # eager checkpoint
    ]
    previous_context = previous_display[:-1]
    result_messages = [
        *previous_context,
        {"role": "user", "content": polluted_current},
        {"role": "assistant", "content": "latest answer"},
    ]

    merged = _merge_display_messages_after_agent_result(
        previous_display,
        previous_context,
        result_messages,
        current_turn,
    )

    user_texts = [
        _text(m.get("content", "")).strip()
        for m in merged
        if isinstance(m, dict) and m.get("role") == "user"
    ]
    assert user_texts.count(current_turn) == 1, (
        f"Should keep one clean current user row; got user rows: {user_texts}"
    )
    assert polluted_current not in user_texts, (
        f"Replayed stale prefix must not be displayed as a user row; got: {user_texts}"
    )
    assert any(
        isinstance(m, dict)
        and m.get("role") == "assistant"
        and "latest answer" in _text(m.get("content", ""))
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
