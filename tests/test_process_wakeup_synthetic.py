"""Test coverage for source field threading on process-wakeup synthetic turns."""

import time
from pathlib import Path

from api.models import Session, _append_recovered_pending_turn, _apply_core_sync_or_error_marker
from api.routes import _checkpoint_user_message_for_eager_session_save
from api.streaming import _materialize_pending_user_turn_before_error, _merge_display_messages_after_agent_result


def test_append_recovered_pending_turn_stamps_process_wakeup_source():
    """Verify _append_recovered_pending_turn stamps _source when pending_user_source is process_wakeup."""
    s = Session(
        session_id="test-session-1",
        pending_user_message="[IMPORTANT: Process completed]",
        pending_user_source="process_wakeup",
    )
    recovered = _append_recovered_pending_turn(s)
    assert recovered is not None
    assert recovered["role"] == "user"
    assert recovered["content"] == "[IMPORTANT: Process completed]"
    assert recovered["_source"] == "process_wakeup"
    assert recovered["_recovered"] is True


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


def test_checkpoint_user_message_stamps_process_wakeup_source():
    """Verify eager-path checkpoint stamps _source on message dict when source is process_wakeup."""
    s = Session(session_id="test-session-4")
    s.messages = []
    _checkpoint_user_message_for_eager_session_save(
        s,
        msg="[IMPORTANT: Wakeup prompt]",
        attachments=[],
        started_at=time.time(),
        source="process_wakeup",
    )
    assert len(s.messages) == 1
    user_msg = s.messages[0]
    assert user_msg["role"] == "user"
    assert user_msg["content"] == "[IMPORTANT: Wakeup prompt]"
    assert user_msg["_source"] == "process_wakeup"


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


def test_merge_display_materializes_missing_process_wakeup_user_turn():
    merged = _merge_display_messages_after_agent_result(
        [],
        [],
        [{"role": "assistant", "content": "done"}],
        "[IMPORTANT: Wakeup prompt]",
        source="process_wakeup",
    )

    assert merged[0]["role"] == "user"
    assert merged[0]["content"] == "[IMPORTANT: Wakeup prompt]"
    assert merged[0]["_source"] == "process_wakeup"
    assert merged[1]["role"] == "assistant"


def test_merge_display_stamps_process_wakeup_source_on_echoed_user_turn():
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

    assert merged[0]["role"] == "user"
    assert merged[0]["_source"] == "process_wakeup"
    assert merged[1]["role"] == "assistant"


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


def test_materialize_pending_user_turn_before_error_stamps_process_wakeup_source():
    s = Session(
        session_id="test-session-8",
        pending_user_message="[IMPORTANT: Wakeup prompt]",
        pending_user_source="process_wakeup",
    )
    s.messages = []

    assert _materialize_pending_user_turn_before_error(s) is True
    assert s.messages[0]["_source"] == "process_wakeup"


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
