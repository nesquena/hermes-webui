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
