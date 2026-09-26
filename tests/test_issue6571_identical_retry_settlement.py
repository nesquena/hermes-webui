"""Regression coverage for #6571 identical-prompt retry settlement."""

from __future__ import annotations

from api import streaming


PROMPT = "Apply the two requested configuration updates."


def _previous_turns():
    previous_display = [
        {"role": "user", "content": "Earlier task", "id": 1},
        {"role": "assistant", "content": "Earlier answer", "id": 2},
        {"role": "user", "content": PROMPT, "id": 10},
    ]
    return previous_display, previous_display[:2]


def _active_turn_identity():
    return {
        "token": "direct-stream:2",
        "text": PROMPT,
        "timestamp": 2.0,
        "source": "webui",
        "attachments": [],
        "current_turn_user_idx": 2,
        "turn_id": "turn-2",
    }


def _lacks_final_answer(result_messages):
    previous_display, previous_context = _previous_turns()
    return streaming._merged_transcript_lacks_final_assistant_answer(
        previous_display,
        previous_context,
        result_messages,
        PROMPT,
        source="webui",
        drop_replayed_assistant=False,
        active_turn_identity=_active_turn_identity(),
    )


def test_successful_identical_prompt_retry_keeps_terminal_answer():
    _previous_display, previous_context = _previous_turns()
    result_messages = previous_context + [
        {"role": "user", "content": PROMPT, "id": 11},
        {"role": "assistant", "content": "Working on it.", "id": 12},
        {
            "role": "tool",
            "content": "update succeeded",
            "tool_call_id": "tool-1",
            "id": 13,
        },
        {
            "role": "assistant",
            "content": "Both changes were applied and verified.",
            "id": 14,
        },
    ]

    assert _lacks_final_answer(result_messages) is False


def test_failed_identical_prompt_retry_still_lacks_terminal_answer():
    _previous_display, previous_context = _previous_turns()
    result_messages = previous_context + [
        {"role": "user", "content": PROMPT, "id": 11},
        {"role": "assistant", "content": "Working on it.", "id": 12},
        {
            "role": "tool",
            "content": "update failed",
            "tool_call_id": "tool-1",
            "id": 13,
        },
    ]

    assert _lacks_final_answer(result_messages) is True
