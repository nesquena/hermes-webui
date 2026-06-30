"""Test coverage for source field threading on process-wakeup synthetic turns."""

import time
from pathlib import Path

from api.models import Session, _append_recovered_pending_turn, _apply_core_sync_or_error_marker, filter_hidden_internal_messages
from api.routes import _checkpoint_user_message_for_eager_session_save, _message_counts_as_renderable_for_window, _message_summary, _message_window_for_display
from api.streaming import _materialize_pending_user_turn_before_error, _merge_display_messages_after_agent_result, _merged_transcript_lacks_final_assistant_answer


def test_append_recovered_pending_turn_skips_process_wakeup_source():
    """process_wakeup prompts are internal wakeups, not visible recovered user turns."""
    s = Session(
        session_id="test-session-1",
        pending_user_message="[IMPORTANT: Process completed]",
        pending_user_source="process_wakeup",
    )
    recovered = _append_recovered_pending_turn(s)
    assert recovered is None
    assert s.messages == []


def test_append_recovered_pending_turn_skips_webui_source():
    """Verify _append_recovered_pending_turn does NOT stamp _source when source is webui (default)."""
    s = Session(
        session_id="test-session-2",
        pending_user_message="Normal user message",
        pending_user_source="webui",
    )
    recovered = _append_recovered_pending_turn(s)
    assert recovered is not None
    assert recovered["role"] == "user"
    assert recovered["content"] == "Normal user message"
    assert "_source" not in recovered
    assert recovered["_recovered"] is True


def test_append_recovered_pending_turn_defaults_to_no_source():
    """Verify _append_recovered_pending_turn does NOT stamp _source when pending_user_source is None."""
    s = Session(
        session_id="test-session-3",
        pending_user_message="Another user message",
        pending_user_source=None,
    )
    recovered = _append_recovered_pending_turn(s)
    assert recovered is not None
    assert recovered["role"] == "user"
    assert "_source" not in recovered


def test_checkpoint_user_message_skips_process_wakeup_source():
    """Eager persistence must not save process wakeups as human user bubbles."""
    s = Session(session_id="test-session-4")
    s.messages = []
    _checkpoint_user_message_for_eager_session_save(
        s,
        msg="[IMPORTANT: Wakeup prompt]",
        attachments=[],
        started_at=time.time(),
        source="process_wakeup",
    )
    assert s.messages == []


def test_checkpoint_user_message_skips_webui_source():
    """Verify eager-path checkpoint does NOT stamp _source when source is webui (default)."""
    s = Session(session_id="test-session-5")
    s.messages = []
    _checkpoint_user_message_for_eager_session_save(
        s,
        msg="Normal user message",
        attachments=[],
        started_at=time.time(),
        source="webui",
    )
    assert len(s.messages) == 1
    user_msg = s.messages[0]
    assert user_msg["role"] == "user"
    assert user_msg["content"] == "Normal user message"
    assert "_source" not in user_msg


def test_checkpoint_user_message_defaults_to_no_source():
    """Verify eager-path checkpoint does NOT stamp _source when source is omitted (defaults to webui)."""
    s = Session(session_id="test-session-6")
    s.messages = []
    _checkpoint_user_message_for_eager_session_save(
        s,
        msg="Another user message",
        attachments=[],
        started_at=time.time(),
    )
    assert len(s.messages) == 1
    user_msg = s.messages[0]
    assert user_msg["role"] == "user"
    assert user_msg["content"] == "Another user message"
    assert "_source" not in user_msg


def test_session_pending_user_source_persisted():
    """Verify pending_user_source survives serialization and deserialization."""
    s = Session(
        session_id="test-session-7",
        pending_user_message="Test message",
        pending_user_source="process_wakeup",
    )
    s_dict = {k: getattr(s, k, None) for k in [
        'session_id', 'pending_user_message', 'pending_user_source'
    ]}
    assert s_dict["pending_user_source"] == "process_wakeup"

    # Reconstruct from dict
    s2 = Session(**s_dict)
    assert s2.pending_user_source == "process_wakeup"


def test_merge_display_hides_missing_process_wakeup_user_turn():
    merged = _merge_display_messages_after_agent_result(
        [],
        [],
        [{"role": "assistant", "content": "done"}],
        "[IMPORTANT: Wakeup prompt]",
        source="process_wakeup",
    )

    assert merged == [{"role": "assistant", "content": "done"}]


def test_merge_display_drops_echoed_process_wakeup_user_turn():
    merged = _merge_display_messages_after_agent_result(
        [],
        [],
        [
            {"role": "user", "content": "[IMPORTANT: Wakeup prompt]"},
            {"role": "assistant", "content": "done"},
        ],
        "[IMPORTANT: Wakeup prompt]",
        source="process_wakeup",
    )

    assert merged == [{"role": "assistant", "content": "done"}]


def test_merge_display_leaves_webui_user_turn_unmarked():
    merged = _merge_display_messages_after_agent_result(
        [],
        [],
        [{"role": "assistant", "content": "done"}],
        "Normal user message",
        source="webui",
    )

    assert merged[0]["role"] == "user"
    assert "_source" not in merged[0]


def test_materialize_pending_user_turn_before_error_skips_process_wakeup_source():
    s = Session(
        session_id="test-session-8",
        pending_user_message="[IMPORTANT: Wakeup prompt]",
        pending_user_source="process_wakeup",
    )
    s.messages = []

    assert _materialize_pending_user_turn_before_error(s) is False
    assert s.messages == []


def test_apply_core_sync_or_error_marker_clears_pending_user_source(tmp_path):
    s = Session(
        session_id="test-session-9",
        pending_user_message="[IMPORTANT: Wakeup prompt]",
        pending_user_source="process_wakeup",
    )
    s.messages = [{"role": "assistant", "content": "done"}]
    s.pending_started_at = time.time()
    s.save = lambda *args, **kwargs: None

    assert _apply_core_sync_or_error_marker(s, Path(tmp_path / "missing-core.json")) is True
    assert s.pending_user_source is None


def test_process_wakeup_user_rows_are_not_renderable_or_counted():
    hidden = {
        "role": "user",
        "content": "[IMPORTANT: Background process done]",
        "_source": "process_wakeup",
        "timestamp": 123.0,
    }
    visible = {"role": "assistant", "content": "done", "timestamp": 124.0}

    assert _message_counts_as_renderable_for_window(hidden) is False
    assert _message_counts_as_renderable_for_window(visible) is True
    assert _message_summary([hidden, visible]) == {
        "message_count": 1,
        "last_message_at": 124.0,
    }


def test_filter_hidden_internal_messages_removes_legacy_process_wakeup_rows():
    hidden = {
        "role": "User",
        "content": "[IMPORTANT: Background process done]",
        "_source": "process_wakeup",
        "timestamp": 123.0,
    }
    visible = {"role": "user", "content": "real user", "timestamp": 124.0}

    assert filter_hidden_internal_messages([hidden, visible]) == [visible]


def test_message_window_excludes_process_wakeup_rows_from_visible_budget():
    messages = [
        {"role": "assistant", "content": "older"},
        {
            "role": "user",
            "content": "[IMPORTANT: Background process done]",
            "_source": "process_wakeup",
        },
        {"role": "assistant", "content": "newer"},
    ]

    window, offset = _message_window_for_display(messages, msg_limit=2)

    assert window == [messages[0], messages[1], messages[2]]
    assert offset == 0
    assert sum(1 for msg in window if _message_counts_as_renderable_for_window(msg)) == 2


def test_process_wakeup_assistant_answer_satisfies_settlement():
    assert _merged_transcript_lacks_final_assistant_answer(
        [],
        [],
        [{"role": "assistant", "content": "done"}],
        "[IMPORTANT: Background process done]",
        source="process_wakeup",
    ) is False


def test_process_wakeup_without_visible_assistant_still_lacks_final_answer():
    assert _merged_transcript_lacks_final_assistant_answer(
        [],
        [],
        [],
        "[IMPORTANT: Background process done]",
        source="process_wakeup",
    ) is True


def test_process_wakeup_settlement_with_existing_history_and_fresh_answer():
    previous_display = [
        {"role": "user", "content": "older user"},
        {"role": "assistant", "content": "older answer"},
    ]

    assert _merged_transcript_lacks_final_assistant_answer(
        previous_display,
        [],
        [{"role": "assistant", "content": "fresh wakeup answer"}],
        "[IMPORTANT: Background process done]",
        source="process_wakeup",
    ) is False


def test_process_wakeup_settlement_ignores_hidden_rows_and_markers_before_tail():
    previous_display = [
        {"role": "assistant", "content": "older answer"},
        {"role": "user", "content": "hidden wakeup", "_source": "process_wakeup"},
        {"role": "assistant", "content": "context compressed", "_context_compression_marker": True},
    ]

    assert _merged_transcript_lacks_final_assistant_answer(
        previous_display,
        [],
        [{"role": "assistant", "content": "fresh wakeup answer"}],
        "[IMPORTANT: Background process done]",
        source="process_wakeup",
    ) is False


def test_process_wakeup_settlement_still_fails_without_new_answer_after_history():
    previous_display = [
        {"role": "user", "content": "older user"},
        {"role": "assistant", "content": "older answer"},
    ]

    assert _merged_transcript_lacks_final_assistant_answer(
        previous_display,
        [],
        [],
        "[IMPORTANT: Background process done]",
        source="process_wakeup",
    ) is True


def test_process_wakeup_drop_replayed_stable_assistant_still_requires_fresh_answer():
    previous_display = [
        {"role": "assistant", "content": "old", "id": "assistant-1"},
    ]

    assert _merged_transcript_lacks_final_assistant_answer(
        previous_display,
        [],
        [{"role": "assistant", "content": "old", "id": "assistant-1"}],
        "[IMPORTANT: Background process done]",
        source="process_wakeup",
        drop_replayed_assistant=True,
    ) is True


def test_process_wakeup_drop_replayed_keeps_fresh_answer_after_replay():
    previous_display = [
        {"role": "assistant", "content": "old answer", "id": "assistant-1"},
    ]

    assert _merged_transcript_lacks_final_assistant_answer(
        previous_display,
        [],
        [
            {"role": "assistant", "content": "old answer", "id": "assistant-1"},
            {"role": "assistant", "content": "fresh wakeup answer", "id": "assistant-2"},
        ],
        "[IMPORTANT: Background process done]",
        source="process_wakeup",
        drop_replayed_assistant=True,
    ) is False


def test_merge_display_does_not_backfill_hidden_process_wakeup_context_tail():
    previous_display = [
        {"role": "user", "content": "real user"},
        {"role": "assistant", "content": "real answer"},
    ]
    previous_context = [
        *previous_display,
        {
            "role": "user",
            "content": "[IMPORTANT: Background process completed]",
            "_source": "process_wakeup",
        },
    ]

    merged = _merge_display_messages_after_agent_result(
        previous_display,
        previous_context,
        [*previous_context, {"role": "assistant", "content": "fresh answer"}],
        "[IMPORTANT: Background process completed]",
        source="process_wakeup",
    )

    assert all(not (m.get("role") == "user" and m.get("_source") == "process_wakeup") for m in merged)
    assert merged[-1] == {"role": "assistant", "content": "fresh answer"}
