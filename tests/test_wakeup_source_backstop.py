"""Trusted gateway wake provenance projection and delivery-id dedup."""

import json
import sqlite3

import api.models as models
import api.streaming as streaming
from api.models import (
    _normalize_wakeup_rows_for_display,
    merge_session_messages_append_only,
)


WAKE_TEXT = (
    "[IMPORTANT: Background process proc_5b9fcce4cbff completed (exit_code=1).\n"
    "Command: make test\nOutput:\nfailed]"
)


def _wake(delivery_id, **extra):
    row = {
        "role": "user",
        "content": WAKE_TEXT,
        "display_kind": "process_wakeup",
        "display_metadata": {"delivery_id": delivery_id},
    }
    row.update(extra)
    return row


def test_state_db_reader_preserves_durable_wakeup_provenance(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE messages ("
            "session_id TEXT, role TEXT, content TEXT, timestamp REAL, "
            "display_kind TEXT, display_metadata TEXT)"
        )
        conn.execute(
            "INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?)",
            (
                "session-1",
                "user",
                WAKE_TEXT,
                1,
                "process_wakeup",
                json.dumps({"delivery_id": "delivery-1"}),
            ),
        )
    monkeypatch.setattr(models, "_active_state_db_path", lambda: db_path)

    assert models.get_state_db_session_messages("session-1") == [
        _wake("delivery-1", timestamp=1.0)
    ]


def test_trusted_wakeup_is_stamped_and_gets_display_metadata():
    row = _wake("delivery-1")
    assert _normalize_wakeup_rows_for_display([row]) == [row]
    assert row["_source"] == "process_wakeup"
    assert row["_wakeup_meta"]["task_id"] == "proc_5b9fcce4cbff"


def test_merge_deduplicates_only_the_same_delivery_id():
    first = _wake("delivery-1", timestamp=1)
    twin = _wake("delivery-1", timestamp=2)
    assert merge_session_messages_append_only([], [first, twin]) == [first]


def test_merge_projects_trusted_state_provenance_onto_sidecar_row():
    sidecar = {"role": "user", "content": WAKE_TEXT, "timestamp": 1}
    state = _wake("delivery-1", timestamp=1)
    distinct = _wake("delivery-2", timestamp=1)

    assert merge_session_messages_append_only([sidecar], [state, distinct]) == [
        sidecar,
        distinct,
    ]
    assert sidecar["display_kind"] == "process_wakeup"
    assert sidecar["display_metadata"] == {"delivery_id": "delivery-1"}
    assert sidecar["_source"] == "process_wakeup"
    assert distinct["_source"] == "process_wakeup"


def test_merge_never_combines_partial_provenance_into_trusted_pair():
    sidecar = {
        "role": "user",
        "content": WAKE_TEXT,
        "timestamp": 1,
        "display_kind": "process_wakeup",
    }
    before = dict(sidecar)
    state = _wake("delivery-1", timestamp=1)

    assert merge_session_messages_append_only([sidecar], [state]) == [sidecar]
    assert sidecar == before


def test_distinct_delivery_ids_never_deduplicate_even_with_identical_text():
    first = _wake("delivery-1", timestamp=1)
    second = _wake("delivery-2", timestamp=1)
    assert merge_session_messages_append_only([], [first, second]) == [first, second]


def test_user_typed_wakeup_shape_stays_byte_identical():
    row = {"role": "user", "content": WAKE_TEXT, "timestamp": 1}
    before = dict(row)
    assert _normalize_wakeup_rows_for_display([row]) == [row]
    assert row == before


def test_untrusted_or_incomplete_provenance_stays_byte_identical():
    rows = [
        _wake("", timestamp=1),
        {"role": "user", "content": WAKE_TEXT, "display_kind": "process_wakeup"},
        {
            "role": "user",
            "content": WAKE_TEXT,
            "display_kind": "other",
            "display_metadata": {"delivery_id": "delivery-1"},
        },
    ]
    before = [dict(row) for row in rows]
    assert _normalize_wakeup_rows_for_display(rows) == rows
    assert rows == before


def test_non_gateway_rows_are_byte_identical():
    rows = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hello"},
        {"role": "tool", "content": {"not": "coerced"}},
    ]
    before = [dict(row) for row in rows]
    assert _normalize_wakeup_rows_for_display(rows) == rows
    assert rows == before


def test_empty_and_non_list_passthrough():
    assert _normalize_wakeup_rows_for_display([]) == []
    assert _normalize_wakeup_rows_for_display(None) is None


def test_legacy_process_wakeup_turn_gets_durable_delivery_provenance():
    kind, metadata = streaming._trusted_turn_display_persistence(
        "process_wakeup",
        "stream-123",
    )

    assert kind == "process_wakeup"
    assert metadata == {"delivery_id": "stream-123"}


def test_browser_turn_cannot_self_classify_as_process_wakeup():
    assert streaming._trusted_turn_display_persistence(
        "webui",
        "stream-123",
    ) == (None, None)


def test_run_conversation_contract_forwards_supported_wakeup_provenance():
    class ModernAgent:
        def run_conversation(
            self,
            user_message,
            system_message,
            conversation_history,
            task_id,
            persist_user_message,
            persist_user_timestamp=None,
            persist_user_display_kind=None,
            persist_user_display_metadata=None,
        ):
            return None

    kind, metadata = streaming._trusted_turn_display_persistence(
        "process_wakeup",
        "stream-123",
    )
    kwargs = streaming._build_run_conversation_kwargs(
        ModernAgent().run_conversation,
        user_message="model-facing prompt",
        system_message="system",
        conversation_history=[],
        conversation_history_revision=None,
        task_id="session-1",
        persist_user_message=WAKE_TEXT,
        persist_user_timestamp=1.0,
        persist_user_display_kind=kind,
        persist_user_display_metadata=metadata,
    )

    assert kwargs["persist_user_display_kind"] == "process_wakeup"
    assert kwargs["persist_user_display_metadata"] == {
        "delivery_id": "stream-123"
    }


def test_run_conversation_contract_omits_wakeup_fields_for_older_agent():
    class LegacyAgent:
        def run_conversation(
            self,
            user_message,
            system_message,
            conversation_history,
            task_id,
            persist_user_message,
        ):
            return None

    kwargs = streaming._build_run_conversation_kwargs(
        LegacyAgent().run_conversation,
        user_message="model-facing prompt",
        system_message="system",
        conversation_history=[],
        conversation_history_revision=None,
        task_id="session-1",
        persist_user_message=WAKE_TEXT,
        persist_user_timestamp=1.0,
        persist_user_display_kind="process_wakeup",
        persist_user_display_metadata={"delivery_id": "stream-123"},
    )

    assert "persist_user_display_kind" not in kwargs
    assert "persist_user_display_metadata" not in kwargs
