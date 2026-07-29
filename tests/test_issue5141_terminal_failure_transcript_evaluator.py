"""Tests for #5141 terminal-failure transcript evaluator split."""

from __future__ import annotations

from unittest import mock

import api.streaming as streaming


def test_turn_evaluator_matches_merged_wrapper_without_replay_filter():
    previous_display = [{"role": "user", "content": "hello"}]
    previous_context = list(previous_display)
    result_messages = previous_context + [
        {"role": "user", "content": "follow up"},
    ]
    msg_text = "follow up"

    merged = streaming._merge_display_messages_after_agent_result(
        previous_display,
        previous_context,
        streaming._restore_reasoning_metadata(previous_display, result_messages),
        msg_text,
        source="webui",
    )
    direct = streaming._turn_transcript_lacks_final_assistant_answer(
        merged,
        previous_display,
        msg_text,
        source="webui",
        drop_replayed_assistant=False,
    )
    wrapped = streaming._merged_transcript_lacks_final_assistant_answer(
        previous_display,
        previous_context,
        result_messages,
        msg_text,
        source="webui",
        drop_replayed_assistant=False,
    )
    assert direct is wrapped
    assert direct is True


def test_turn_evaluator_matches_merged_wrapper_with_final_answer():
    previous_display = [{"role": "user", "content": "hello"}]
    previous_context = list(previous_display)
    result_messages = previous_context + [
        {"role": "user", "content": "follow up"},
        {"role": "assistant", "content": "done"},
    ]
    msg_text = "follow up"

    merged = streaming._merge_display_messages_after_agent_result(
        previous_display,
        previous_context,
        streaming._restore_reasoning_metadata(previous_display, result_messages),
        msg_text,
        source="webui",
    )
    direct = streaming._turn_transcript_lacks_final_assistant_answer(
        merged,
        previous_display,
        msg_text,
        source="webui",
        drop_replayed_assistant=False,
    )
    wrapped = streaming._merged_transcript_lacks_final_assistant_answer(
        previous_display,
        previous_context,
        result_messages,
        msg_text,
        source="webui",
        drop_replayed_assistant=False,
    )
    assert direct is wrapped
    assert direct is False


def test_turn_evaluator_materializes_pending_user_after_display_boundary():
    previous_display = [{"role": "user", "content": "older"}]
    merged = list(previous_display)
    msg_text = "new prompt"

    assert streaming._turn_transcript_lacks_final_assistant_answer(
        merged,
        previous_display,
        msg_text,
        source="webui",
        drop_replayed_assistant=False,
    ) is True


def test_merged_wrapper_delegates_to_turn_evaluator():
    calls = []

    def _fake_evaluator(merged_messages, previous_display, msg_text, source="webui", drop_replayed_assistant=False):
        calls.append(
            {
                "merged_len": len(list(merged_messages or [])),
                "previous_len": len(list(previous_display or [])),
                "msg_text": msg_text,
                "source": source,
                "drop_replayed_assistant": drop_replayed_assistant,
            }
        )
        return True

    previous_display = [{"role": "user", "content": "hello"}]
    with mock.patch.object(
        streaming,
        "_turn_transcript_lacks_final_assistant_answer",
        side_effect=_fake_evaluator,
    ):
        result = streaming._merged_transcript_lacks_final_assistant_answer(
            previous_display,
            previous_display,
            previous_display,
            "hello",
            source="cli",
            drop_replayed_assistant=True,
        )
    assert result is True
    assert len(calls) == 1
    assert calls[0]["previous_len"] == 1
    assert calls[0]["msg_text"] == "hello"
    assert calls[0]["source"] == "cli"
    assert calls[0]["drop_replayed_assistant"] is True
    assert calls[0]["merged_len"] >= 1


def test_checkpointed_current_user_accepts_new_result_answer_by_provenance():
    msg_text = "current prompt"
    previous_display = [
        {"role": "user", "content": "older prompt"},
        {"role": "assistant", "content": "older answer"},
        {"role": "user", "content": msg_text},
    ]
    result_messages = list(previous_display) + [
        {"role": "assistant", "content": "real final answer"},
    ]

    # The positional merged evaluator is intentionally conservative at this
    # checkpoint boundary, but result provenance proves the unique final row.
    assert streaming._settled_turn_answer_state(
        previous_display,
        previous_display,
        result_messages,
        msg_text,
        source="webui",
        drop_replayed_assistant=True,
    ) == (True, False)


def test_checkpointed_repeated_prompt_rejects_replayed_historical_answer():
    msg_text = "repeat this"
    stale_answer = {"id": 2, "role": "assistant", "content": "historical answer"}
    previous_display = [
        {"id": 1, "role": "user", "content": msg_text},
        dict(stale_answer),
        {"id": 3, "role": "user", "content": msg_text},
    ]
    result_messages = list(previous_display) + [dict(stale_answer)]

    assert streaming._settled_turn_answer_state(
        previous_display,
        previous_display,
        result_messages,
        msg_text,
        source="webui",
        drop_replayed_assistant=True,
    ) == (False, True)


def test_identical_new_answer_is_valid_when_it_has_new_row_identity():
    msg_text = "repeat this"
    previous_display = [
        {"id": 1, "role": "user", "content": msg_text},
        {"id": 2, "role": "assistant", "content": "same answer"},
        {"id": 3, "role": "user", "content": msg_text},
    ]
    result_messages = list(previous_display) + [
        {"id": 4, "role": "assistant", "content": "same answer"},
    ]

    assert streaming._settled_turn_answer_state(
        previous_display,
        previous_display,
        result_messages,
        msg_text,
        source="webui",
        drop_replayed_assistant=True,
    ) == (True, False)


def test_replay_filter_unions_display_and_context_row_ids():
    msg_text = "current prompt"
    previous_display = [
        {"id": 1, "role": "user", "content": msg_text},
    ]
    context_only_answer = {"id": 2, "role": "assistant", "content": "context answer"}
    previous_context = [*previous_display, context_only_answer]
    result_messages = [*previous_context, dict(context_only_answer)]

    assert streaming._assistant_reply_added_after_current_turn(
        result_messages,
        previous_context,
        msg_text,
        previous_display=previous_display,
        drop_replayed_assistant=True,
    ) is False


def test_structured_content_without_text_is_not_a_final_answer():
    previous = [{"id": 1, "role": "user", "content": "prompt"}]
    result_messages = [
        *previous,
        {"id": 2, "role": "assistant", "content": [{"type": "image"}]},
    ]

    assert streaming._assistant_reply_added_after_current_turn(
        result_messages,
        previous,
        "prompt",
        previous_display=previous,
        drop_replayed_assistant=True,
    ) is False


def test_legacy_idless_replay_with_freshly_minted_id_is_rejected():
    msg_text = "repeat this"
    previous_display = [
        {"role": "user", "content": msg_text},
        {"role": "assistant", "content": "legacy answer"},
        {"role": "user", "content": msg_text},
    ]
    # Settlement stamps an id on every id-less result row. A replayed legacy
    # assistant can therefore arrive with a fresh id even though its prior copy
    # had none; content fallback must still reject it.
    result_messages = [
        *previous_display,
        {"id": 4, "role": "assistant", "content": "legacy answer"},
    ]

    assert streaming._settled_turn_answer_state(
        previous_display,
        previous_display,
        result_messages,
        msg_text,
        source="webui",
        drop_replayed_assistant=True,
    ) == (False, True)
