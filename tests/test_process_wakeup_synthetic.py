"""Test coverage for source field threading on process-wakeup synthetic turns."""

import time
import types
from pathlib import Path

from api.models import Session, _append_recovered_pending_turn, _apply_core_sync_or_error_marker
from api.routes import _checkpoint_user_message_for_eager_session_save
from api.streaming import (
    _materialize_pending_user_turn_before_error,
    _merge_display_messages_after_agent_result,
    _normalize_user_text,
    _message_text,
)
from api import streaming


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


_DEFERRED_WAKEUP_TEXT = (
    "[IMPORTANT: Background process proc_deferred_1 completed (exit_code=3).\n"
    "Command: cargo test -q\n"
    "Output:\n"
    "build failed\n"
)
_DEFERRED_TOKEN = "ws-deferred:1700000000"
_DEFERRED_HISTORY = [
    {"role": "user", "content": "earlier question"},
    {"role": "assistant", "content": "earlier answer"},
]
_DEFERRED_WAKEUP_META = {
    "type": "completion",
    "task_id": "proc_deferred_1",
    "command": "cargo test -q",
    "exit_code": 3,
}


def _deferred_active_turn_identity(source):
    """Server-owned active-turn authority for a deferred-save settlement."""
    return {
        "session_id": "test-session-wakeup-deferred",
        "token": _DEFERRED_TOKEN,
        "text": _DEFERRED_WAKEUP_TEXT,
        "timestamp": 1700000000.0,
        "source": source,
        "attachments": [],
        "checkpoint": None,
        "current_turn_user_idx": len(_DEFERRED_HISTORY),
        "turn_id": "turn-deferred-1",
    }


def _run_deferred_settlement(
    source,
    *,
    result_messages=None,
    duplicate_token_row=False,
    authoritative_checkpoint=False,
    display_only_checkpoint=False,
):
    """Settle a retained token-owned current-turn row lacking provenance.

    The retained row shape mirrors deferred Agent/state.db reconciliation:
    role=user, ``_db_persisted=True``, matching ``_active_turn_token``, but no
    ``_source`` and no ``_wakeup_meta``. Display and context hold distinct row
    copies, as they do when reloaded from persisted state.
    """
    def retained_row():
        return {
            "role": "user",
            "content": _DEFERRED_WAKEUP_TEXT,
            "timestamp": 1700000000.0,
            "_db_persisted": True,
            "_active_turn_token": _DEFERRED_TOKEN,
        }

    def checkpoint_rows():
        retained = retained_row()
        if authoritative_checkpoint:
            retained["id"] = "user-2"
        rows = [retained]
        if duplicate_token_row:
            rows.insert(0, {
                "role": "user",
                "content": "historical same-token text",
                "_active_turn_token": _DEFERRED_TOKEN,
            })
        if authoritative_checkpoint:
            checkpoint = retained_row()
            checkpoint["_db_persisted"] = False
            rows.append(checkpoint)
        return rows

    previous_display = [dict(m) for m in _DEFERRED_HISTORY] + checkpoint_rows()
    previous_context = [dict(m) for m in _DEFERRED_HISTORY]
    if not display_only_checkpoint:
        previous_context += checkpoint_rows()
    active_turn_identity = _deferred_active_turn_identity(source)
    if authoritative_checkpoint:
        active_turn_identity["checkpoint"] = dict(previous_context[-1])
    if result_messages is None:
        result_messages = (
            [dict(m) for m in _DEFERRED_HISTORY]
            + [retained_row()]
            + [{"role": "assistant", "content": "The background job failed; here is why."}]
        )
    session = types.SimpleNamespace(
        messages=list(previous_display),
        context_messages=list(previous_context),
    )
    streaming._settle_result_messages(
        session,
        previous_display,
        previous_context,
        result_messages,
        _DEFERRED_WAKEUP_TEXT,
        source,
        active_turn_identity,
    )
    return session


def _current_turn_survivors(messages):
    same_token = [
        m for m in messages
        if isinstance(m, dict) and m.get("_active_turn_token") == _DEFERRED_TOKEN
    ]
    same_text = [
        m for m in messages
        if isinstance(m, dict)
        and m.get("role") == "user"
        and _normalize_user_text(_message_text(m.get("content")))
        == _normalize_user_text(_DEFERRED_WAKEUP_TEXT)
    ]
    return same_token, same_text


def test_deferred_settlement_restores_wakeup_provenance_on_retained_token_row():
    """A token-owned retained user row regains trusted wakeup provenance."""
    session = _run_deferred_settlement("process_wakeup")

    for label, messages in (
        ("display", session.messages),
        ("context", session.context_messages),
    ):
        same_token, same_text = _current_turn_survivors(messages)
        assert len(same_token) == 1, label
        assert len(same_text) == 1, label
        survivor = same_token[0]
        assert same_text[0] is survivor, label
        assert survivor["role"] == "user", label
        assert survivor["_db_persisted"] is True, label
        assert survivor["_source"] == "process_wakeup", label
        assert survivor["_wakeup_meta"] == _DEFERRED_WAKEUP_META, label


def test_empty_result_aligns_display_checkpoint_into_context():
    """An exact display checkpoint is copied into an empty context projection."""
    session = _run_deferred_settlement(
        "process_wakeup",
        result_messages=[],
        display_only_checkpoint=True,
    )

    for label, messages in (
        ("display", session.messages),
        ("context", session.context_messages),
    ):
        same_token, same_text = _current_turn_survivors(messages)
        assert len(same_token) == 1, label
        assert len(same_text) == 1, label
        survivor = same_token[0]
        assert same_text[0] is survivor, label
        assert survivor["_db_persisted"] is True, label
        assert survivor["_source"] == "process_wakeup", label
        assert survivor["_wakeup_meta"] == _DEFERRED_WAKEUP_META, label


def test_prefix_mismatch_does_not_tail_append_display_checkpoint_to_context():
    """A failed non-empty boundary settlement must not move wakeup into context tail."""
    session = _run_deferred_settlement(
        "process_wakeup",
        result_messages=[
            {"role": "user", "content": "unrelated"},
            {"role": "assistant", "content": "answer"},
        ],
        display_only_checkpoint=True,
    )

    context_tokens = [
        message
        for message in session.context_messages
        if message.get("_active_turn_token") == _DEFERRED_TOKEN
    ]
    assert not context_tokens
    assert session.context_messages[-1]["role"] == "assistant"
    assert session.context_messages[-1]["content"] == "answer"

    same_token, same_text = _current_turn_survivors(session.messages)
    assert len(same_token) == 1
    assert len(same_text) == 1
    survivor = same_token[0]
    assert same_text[0] is survivor
    assert survivor["_db_persisted"] is True
    assert survivor["_source"] == "process_wakeup"
    assert survivor["_wakeup_meta"] == _DEFERRED_WAKEUP_META


def test_deferred_settlement_leaves_webui_canonical_lookalike_unstamped():
    """An ordinary webui turn with canonical-prompt-shaped text stays plain."""
    session = _run_deferred_settlement("webui")

    for label, messages in (
        ("display", session.messages),
        ("context", session.context_messages),
    ):
        same_token, same_text = _current_turn_survivors(messages)
        assert len(same_token) == 1, label
        assert len(same_text) == 1, label
        survivor = same_token[0]
        assert same_text[0] is survivor, label
        assert survivor["role"] == "user", label
        assert survivor["_db_persisted"] is True, label
        assert "_source" not in survivor, label
        assert "_wakeup_meta" not in survivor, label


def test_empty_result_settlement_restores_wakeup_provenance_in_context():
    """An empty Agent result still enriches the retained context checkpoint."""
    session = _run_deferred_settlement("process_wakeup", result_messages=[])

    for label, messages in (
        ("display", session.messages),
        ("context", session.context_messages),
    ):
        same_token, same_text = _current_turn_survivors(messages)
        assert len(same_token) == 1, label
        assert len(same_text) == 1, label
        survivor = same_token[0]
        assert same_text[0] is survivor, label
        assert survivor["_db_persisted"] is True, label
        assert survivor["_source"] == "process_wakeup", label
        assert survivor["_wakeup_meta"] == _DEFERRED_WAKEUP_META, label


def test_empty_result_collapses_duplicate_token_checkpoints_to_retained_row():
    """Same-token history cannot displace the retained canonical completion row."""
    session = _run_deferred_settlement(
        "process_wakeup",
        result_messages=[],
        duplicate_token_row=True,
    )

    for label, messages in (
        ("display", session.messages),
        ("context", session.context_messages),
    ):
        same_token, same_text = _current_turn_survivors(messages)
        assert len(same_token) == 1, label
        assert len(same_text) == 1, label
        survivor = same_token[0]
        assert same_text[0] is survivor, label
        assert survivor["content"] == _DEFERRED_WAKEUP_TEXT, label
        assert survivor["_db_persisted"] is True, label
        assert survivor["_source"] == "process_wakeup", label
        assert survivor["_wakeup_meta"] == _DEFERRED_WAKEUP_META, label


def test_empty_result_preserves_persisted_authoritative_checkpoint_replacement():
    """Replacing an authoritative checkpoint keeps durable persistence state."""
    session = _run_deferred_settlement(
        "process_wakeup",
        result_messages=[],
        authoritative_checkpoint=True,
    )

    for label, messages in (
        ("display", session.messages),
        ("context", session.context_messages),
    ):
        same_token, same_text = _current_turn_survivors(messages)
        assert len(same_token) == 1, label
        assert len(same_text) == 1, label
        survivor = same_token[0]
        assert same_text[0] is survivor, label
        assert survivor["_db_persisted"] is True, label
        assert survivor["id"] == "user-2", label
        assert survivor["_source"] == "process_wakeup", label
        assert survivor["_wakeup_meta"] == _DEFERRED_WAKEUP_META, label
