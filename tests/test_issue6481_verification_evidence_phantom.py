"""Regression coverage for issue #6481 verification follow-up contracts."""

import types

from api import streaming


ATTEMPTED_ANSWER = "The failing test is fixed."
CORRECTIVE_ANSWER = "Verification failed. I fixed the parser and reran the tests."
VERIFICATION_EVIDENCE = {
    "status": "passed",
    "kind": "terminal",
    "scope": "workspace",
    "canonical_command": "pytest tests/test_app.py -q",
}


def _current_turn_prefix(*, include_attempted_answer):
    messages = [
        {
            "role": "user",
            "content": "Fix the failing test.",
            "_active_turn_token": "stream:turn",
        },
        {
            "role": "assistant",
            "content": [{"type": "tool_use", "name": "terminal", "input": {"cmd": "pytest"}}],
        },
        {
            "role": "tool",
            "tool_call_id": "terminal",
            "content": "Tests passed.",
            "verification_evidence": VERIFICATION_EVIDENCE,
        },
    ]
    if include_attempted_answer:
        messages.append({"role": "assistant", "content": ATTEMPTED_ANSWER})
    return messages


def _verification_followup(marker, corrective=CORRECTIVE_ANSWER):
    return [
        {"role": "user", "content": "[System: verify the workspace]", marker: True},
        {"role": "assistant", "content": corrective},
    ]


def _writeback_provenance(*, prompt, timestamp, current_turn_user_idx, turn_id="turn-current", token="stream:turn"):
    return {
        "verification_nudge_seen": True,
        "active_turn_identity": {
            "token": token,
            "text": prompt,
            "timestamp": timestamp,
            "source": "webui",
            "attachments": [],
            "current_turn_user_idx": current_turn_user_idx,
            "turn_id": turn_id,
        },
    }


def _merge(
    result_messages,
    previous=None,
    previous_context=None,
    msg_text="Fix the failing test.",
    writeback_provenance=None,
):
    previous = list(previous or [{"role": "user", "content": "Fix the failing test."}])
    previous_context = list(previous_context if previous_context is not None else previous)
    return streaming._merge_display_messages_after_agent_result(
        previous,
        previous_context,
        result_messages,
        msg_text,
        verification_nudge_provenance=writeback_provenance,
    )


def _role_content_sequence(messages):
    return [(message["role"], message.get("content")) for message in messages]


def _expected_current_turn(*, include_attempted_answer):
    expected = _current_turn_prefix(include_attempted_answer=include_attempted_answer)
    return expected + [{"role": "assistant", "content": CORRECTIVE_ANSWER}]


def test_corrective_followup_survives_after_attempted_answer_for_both_markers():
    for marker in ("_verification_stop_synthetic", "_pre_verify_synthetic"):
        previous = _current_turn_prefix(include_attempted_answer=True)
        merged = _merge(
            previous + _verification_followup(marker),
            previous=previous,
            writeback_provenance=_writeback_provenance(
                prompt="Fix the failing test.",
                timestamp=1.0,
                current_turn_user_idx=0,
            ),
        )

        assert _role_content_sequence(merged) == _role_content_sequence(
            _expected_current_turn(include_attempted_answer=True)
        )
        assert [message.get("content") for message in merged].count("Fix the failing test.") == 1
        assert all(not streaming._is_synthetic_control_message(message) for message in merged)


def test_delta_only_followup_preserves_corrective_answer():
    for marker in ("_verification_stop_synthetic", "_pre_verify_synthetic"):
        previous = _current_turn_prefix(include_attempted_answer=True)
        merged = _merge(
            _verification_followup(marker),
            previous=previous,
            writeback_provenance=_writeback_provenance(
                prompt="Fix the failing test.",
                timestamp=1.0,
                current_turn_user_idx=0,
            ),
        )

        assert _role_content_sequence(merged) == _role_content_sequence(
            _expected_current_turn(include_attempted_answer=True)
        )
        assert [message.get("content") for message in merged].count("Fix the failing test.") == 1


def test_first_post_nudge_answer_survives_when_no_attempted_answer_exists():
    for marker in ("_verification_stop_synthetic", "_pre_verify_synthetic"):
        previous = _current_turn_prefix(include_attempted_answer=False)
        merged = _merge(
            previous + _verification_followup(marker),
            previous=previous,
            writeback_provenance=_writeback_provenance(
                prompt="Fix the failing test.",
                timestamp=1.0,
                current_turn_user_idx=0,
            ),
        )

        assert _role_content_sequence(merged) == _role_content_sequence(
            _expected_current_turn(include_attempted_answer=False)
        )
        assert [message.get("content") for message in merged].count("Fix the failing test.") == 1


def test_unmarked_adjacent_assistant_rows_remain_unchanged():
    messages = [
        {"role": "user", "content": "Give me two alternatives."},
        {"role": "assistant", "content": "First alternative."},
        {"role": "assistant", "content": "Second alternative."},
    ]

    assert streaming._drop_synthetic_control_messages(messages) == messages


def test_unmarked_assistant_only_new_turn_materializes_current_user_row():
    previous = [
        {"role": "user", "content": "Earlier request."},
        {"role": "assistant", "content": "Earlier answer."},
    ]
    result = [{"role": "assistant", "content": "New answer."}]

    merged = _merge(result, previous=previous, msg_text="New request.")

    assert _role_content_sequence(merged) == [
        ("user", "Earlier request."),
        ("assistant", "Earlier answer."),
        ("user", "New request."),
        ("assistant", "New answer."),
    ]


def test_unmarked_repeated_identical_prompt_materializes_new_user_row():
    previous = [
        {"role": "user", "content": "Fix the failing test."},
        {"role": "assistant", "content": "The first attempt."},
    ]
    result = [{"role": "assistant", "content": "The second attempt."}]

    merged = _merge(result, previous=previous)

    assert _role_content_sequence(merged) == [
        ("user", "Fix the failing test."),
        ("assistant", "The first attempt."),
        ("user", "Fix the failing test."),
        ("assistant", "The second attempt."),
    ]


def test_marked_repeated_old_prompt_materializes_deferred_current_turn():
    prompt = "Fix the failing test."
    previous = [
        {"role": "user", "content": prompt, "timestamp": 1.0},
        {"role": "assistant", "content": "The earlier attempt is complete."},
    ]
    corrective = "Verification failed. I fixed the parser and reran the tests."

    for marker in ("_verification_stop_synthetic", "_pre_verify_synthetic"):
        merged = _merge(
            previous
            + [
                {"role": "user", "content": "[System: verify the workspace]", marker: True},
                {"role": "assistant", "content": corrective},
            ],
            previous=previous,
            msg_text=prompt,
            writeback_provenance=_writeback_provenance(
                prompt=prompt,
                timestamp=2.0,
                current_turn_user_idx=len(previous),
                token="direct-stream:2",
            ),
        )

        assert _role_content_sequence(merged) == [
            ("user", prompt),
            ("assistant", "The earlier attempt is complete."),
            ("user", prompt),
            ("assistant", corrective),
        ]
        assert [message.get("content") for message in merged].count(prompt) == 2


def test_context_only_exact_checkpoint_does_not_suppress_display_boundary():
    prompt = "Fix the failing test."
    previous_display = [
        {"role": "user", "content": prompt, "timestamp": 1.0},
        {"role": "assistant", "content": "The earlier attempt is complete."},
    ]
    previous_context = previous_display + [
        {
            "role": "user",
            "content": prompt,
            "timestamp": 2.0,
            "_source": "webui",
            "attachments": [],
            "_active_turn_token": "direct-stream:2",
        },
    ]
    corrective = "Verification failed. I fixed the parser and reran the tests."
    aligned_display, _ = streaming._align_current_turn_display(
        previous_display,
        previous_context,
        _writeback_provenance(
            prompt=prompt,
            timestamp=2.0,
            current_turn_user_idx=2,
            token="direct-stream:2",
        )["active_turn_identity"],
    )

    merged = _merge(
        [
            {"role": "user", "content": "[System: verify the workspace]", "_verification_stop_synthetic": True},
            {"role": "assistant", "content": corrective},
        ],
        previous=aligned_display,
        previous_context=previous_context,
        msg_text=prompt,
        writeback_provenance=_writeback_provenance(
            prompt=prompt,
            timestamp=2.0,
            current_turn_user_idx=2,
            token="direct-stream:2",
        ),
    )

    assert _role_content_sequence(merged) == [
        ("user", prompt),
        ("assistant", "The earlier attempt is complete."),
        ("user", prompt),
        ("assistant", corrective),
    ]


def test_divergent_context_index_does_not_relabel_same_text_display_row():
    prompt = "Fix the failing test."
    token = "direct-stream:2"
    previous_display = [
        {"role": "user", "content": prompt, "timestamp": 1.0},
        {"role": "assistant", "content": "The earlier attempt is complete."},
    ]
    previous_context = [
        {"role": "user", "content": prompt, "timestamp": 1.0, "attachments": []},
        {
            "role": "user",
            "content": prompt,
            "timestamp": 2.0,
            "attachments": [],
            "_active_turn_token": token,
        },
    ]

    aligned_display, _ = streaming._align_current_turn_display(
        previous_display,
        previous_context,
        _writeback_provenance(
            prompt=prompt,
            timestamp=2.0,
            current_turn_user_idx=0,
            token=token,
        )["active_turn_identity"],
    )

    assert _role_content_sequence(aligned_display) == [
        ("user", prompt),
        ("assistant", "The earlier attempt is complete."),
        ("user", prompt),
    ]
    assert aligned_display[0].get("_active_turn_token") is None
    assert aligned_display[-1]["_active_turn_token"] == token


def test_missing_exact_checkpoint_materializes_current_turn_without_retokening_history():
    prompt = "Fix the failing test."
    token = "direct-stream:1.9"
    previous_display = [
        {"role": "user", "content": prompt, "timestamp": 1.1},
        {"role": "assistant", "content": "The earlier attempt is complete."},
    ]
    previous_context = [
        {"role": "user", "content": prompt, "timestamp": 1.1, "attachments": []},
        {"role": "assistant", "content": "The earlier attempt is complete."},
    ]

    aligned_display, aligned_context = streaming._align_current_turn_display(
        previous_display,
        previous_context,
        _writeback_provenance(
            prompt=prompt,
            timestamp=1.9,
            current_turn_user_idx=0,
            token=token,
        )["active_turn_identity"],
    )

    expected = [
        ("user", prompt),
        ("assistant", "The earlier attempt is complete."),
        ("user", prompt),
    ]
    assert _role_content_sequence(aligned_display) == expected
    assert _role_content_sequence(aligned_context) == expected
    assert aligned_display[0].get("_active_turn_token") is None
    assert aligned_context[0].get("_active_turn_token") is None
    assert aligned_display[-1]["_active_turn_token"] == token
    assert aligned_context[-1]["_active_turn_token"] == token


def test_shared_settlement_owner_contract():
    prompt = "Fix the failing test."
    previous = [
        {"role": "user", "content": prompt, "timestamp": 1.1},
        {"role": "assistant", "content": "The earlier attempt is complete."},
    ]
    result_messages = previous + [
        {"role": "user", "content": "[System: verify the workspace]", "_verification_stop_synthetic": True},
        {"role": "assistant", "content": CORRECTIVE_ANSWER},
    ]
    session = types.SimpleNamespace(messages=list(previous), context_messages=list(previous))

    streaming._settle_result_messages(
        session,
        previous,
        previous,
        result_messages,
        prompt,
        "webui",
        _writeback_provenance(
            prompt=prompt,
            timestamp=1.9,
            current_turn_user_idx=len(previous),
            token="direct-stream:1.9",
        )["active_turn_identity"],
    )

    expected = [
        ("user", prompt),
        ("assistant", "The earlier attempt is complete."),
        ("user", prompt),
        ("assistant", CORRECTIVE_ANSWER),
    ]
    assert _role_content_sequence(session.messages) == expected
    assert _role_content_sequence(session.context_messages) == expected


def test_untokened_legacy_row_fails_closed():
    prompt = "Fix the failing test."
    previous_display = [
        {"role": "user", "content": prompt, "timestamp": 1.1},
        {"role": "assistant", "content": "The earlier attempt is complete."},
    ]
    previous_context = previous_display + [
        {
            "role": "user",
            "content": prompt,
            "timestamp": 1.9,
            "_source": "webui",
            "attachments": [],
        },
    ]

    merged = _merge(
        [
            {"role": "user", "content": "[System: verify the workspace]", "_verification_stop_synthetic": True},
            {"role": "assistant", "content": CORRECTIVE_ANSWER},
        ],
        previous=previous_display,
        previous_context=previous_context,
        msg_text=prompt,
        writeback_provenance=_writeback_provenance(
            prompt=prompt,
            timestamp=1.9,
            current_turn_user_idx=2,
            token="direct-stream:1.9",
        ),
    )

    assert _role_content_sequence(merged) == [
        ("user", prompt),
        ("assistant", "The earlier attempt is complete."),
        ("user", prompt),
        ("assistant", CORRECTIVE_ANSWER),
    ]


def test_materialized_webui_user_omits_source():
    message = streaming._materialize_active_turn_user(
        _writeback_provenance(
            prompt="Fix the failing test.",
            timestamp=1.9,
            current_turn_user_idx=2,
            token="direct-stream:1.9",
        )["active_turn_identity"],
        "Fix the failing test.",
        "webui",
    )

    assert message["role"] == "user"
    assert message["content"] == "Fix the failing test."
    assert message["_active_turn_token"] == "direct-stream:1.9"
    assert "_source" not in message


def test_materialized_process_wakeup_user_has_wakeup_meta():
    wakeup_text = (
        "[IMPORTANT: Background process bg-1 completed (exit_code=0).\n"
        "Command: python worker.py\n"
        "Output:\n"
        "done]"
    )
    message = streaming._materialize_active_turn_user(
        {
            "token": "wakeup-stream:1.9",
            "text": wakeup_text,
            "timestamp": 1.9,
            "source": "process_wakeup",
            "attachments": [],
            "current_turn_user_idx": 0,
            "turn_id": "turn-wakeup",
        },
        wakeup_text,
        "process_wakeup",
    )

    assert message["_source"] == "process_wakeup"
    assert message["_active_turn_token"] == "wakeup-stream:1.9"
    assert message["_wakeup_meta"] == {
        "type": "completion",
        "task_id": "bg-1",
        "command": "python worker.py",
        "exit_code": 0,
    }
