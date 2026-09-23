"""Trusted gateway wake provenance projection and delivery-id dedup."""

import ast
import inspect
import json
import sqlite3
import textwrap
import types

import pytest

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


def test_regeneration_tail_snapshot_preserves_durable_wakeup_provenance(
    tmp_path,
    monkeypatch,
):
    """The bounded reader must match the canonical state.db projection."""
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

    snapshot = models.get_state_db_regeneration_tail_snapshot("session-1", 0)

    assert snapshot is not None
    assert snapshot["tail"] == [_wake("delivery-1", timestamp=1.0)]


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

    assert merge_session_messages_append_only([sidecar], [state]) == [sidecar, state]
    assert sidecar == before
    assert state["_source"] == "process_wakeup"


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


def test_all_run_conversation_retries_forward_wakeup_provenance():
    """Credential self-heal retries must keep the original turn authority."""
    source = textwrap.dedent(inspect.getsource(streaming._run_agent_streaming))
    tree = ast.parse(source)
    builders = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_build_run_conversation_kwargs"
    ]

    assert len(builders) == 3
    for call in builders:
        keyword_names = {keyword.arg for keyword in call.keywords}
        assert "persist_user_display_kind" in keyword_names
        assert "persist_user_display_metadata" in keyword_names


# ---------------------------------------------------------------------------
# Mixed sidecar + state.db reconciliation through the real SQLite reader.
# Each store is covered on its own above; wake provenance must also survive the
# cross-store pairing without being laundered onto browser rows or consuming a
# distinct delivery.
# ---------------------------------------------------------------------------


def _write_state_db(db_path, rows):
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE messages ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "session_id TEXT, role TEXT, content TEXT, timestamp REAL, "
            "display_kind TEXT, display_metadata TEXT)"
        )
        for row in rows:
            metadata = row.get("display_metadata")
            conn.execute(
                "INSERT INTO messages "
                "(session_id, role, content, timestamp, display_kind, display_metadata) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    "session-1",
                    row["role"],
                    row["content"],
                    row["timestamp"],
                    row.get("display_kind"),
                    json.dumps(metadata) if metadata is not None else None,
                ),
            )


# Rows older than the first sidecar row are treated as compacted-out history
# and never resurrected, so every fixture starts from shared prior history.
HISTORY = (
    {"role": "user", "content": "hi", "timestamp": 50.0},
    {"role": "assistant", "content": "hello", "timestamp": 51.0},
)


def _reconcile(tmp_path, monkeypatch, sidecar, state_rows):
    sidecar = [dict(row) for row in HISTORY] + list(sidecar)
    state_rows = [dict(row) for row in HISTORY] + list(state_rows)
    db_path = tmp_path / "state.db"
    _write_state_db(db_path, state_rows)
    monkeypatch.setattr(models, "_active_state_db_path", lambda: db_path)
    session = types.SimpleNamespace(
        session_id="session-1",
        profile=None,
        messages=sidecar,
        truncation_watermark=None,
        truncation_boundary=None,
    )
    return models.reconciled_state_db_messages_for_session(session)


def _browser(content, timestamp):
    return {
        "role": "user",
        "content": content,
        "timestamp": timestamp,
        "_source": "webui",
    }


def _delivery_ids(messages):
    return [
        (msg.get("display_metadata") or {}).get("delivery_id")
        for msg in messages
        if msg.get("display_kind") == "process_wakeup"
    ]


def _assert_browser_row_untouched(messages, browser, before):
    rows = [msg for msg in messages if msg is browser]
    assert rows == [browser]
    assert browser == before
    assert browser.get("_source") == "webui"
    assert "display_kind" not in browser
    assert "display_metadata" not in browser
    assert "_wakeup_meta" not in browser


def test_mixed_exact_browser_lookalike_keeps_provenance_and_both_rows(
    tmp_path, monkeypatch
):
    browser = _browser(WAKE_TEXT, 200.0)
    before = dict(browser)

    merged = _reconcile(
        tmp_path,
        monkeypatch,
        [browser],
        [_wake("delivery-old", timestamp=100.0)],
    )

    _assert_browser_row_untouched(merged, browser, before)
    assert _delivery_ids(merged) == ["delivery-old"]
    assert [msg["timestamp"] for msg in merged] == [50.0, 51.0, 100.0, 200.0]
    assert merged[2]["_source"] == "process_wakeup"


def test_mixed_exact_browser_lookalike_with_state_mirror_of_browser_row(
    tmp_path, monkeypatch
):
    """state.db also mirrors the browser turn without provenance."""
    browser = _browser(WAKE_TEXT, 200.0)
    before = dict(browser)

    merged = _reconcile(
        tmp_path,
        monkeypatch,
        [browser],
        [
            _wake("delivery-old", timestamp=100.0),
            {"role": "assistant", "content": "noted", "timestamp": 101.0},
            {"role": "user", "content": WAKE_TEXT, "timestamp": 200.0},
        ],
    )

    _assert_browser_row_untouched(merged, browser, before)
    assert _delivery_ids(merged) == ["delivery-old"]
    assert [msg["content"] for msg in merged].count(WAKE_TEXT) == 2


def test_mixed_quoted_browser_lookalike_keeps_provenance_and_both_rows(
    tmp_path, monkeypatch
):
    browser = _browser(f"Why did this fail?\n\n> {WAKE_TEXT}\n\nPlease retry.", 200.0)
    before = dict(browser)

    merged = _reconcile(
        tmp_path,
        monkeypatch,
        [browser],
        [_wake("delivery-old", timestamp=100.0)],
    )

    _assert_browser_row_untouched(merged, browser, before)
    assert _delivery_ids(merged) == ["delivery-old"]
    assert len(merged) == 4


def test_mixed_same_text_distinct_deliveries_both_survive(tmp_path, monkeypatch):
    sidecar_a = _wake("delivery-a", timestamp=100.0)

    merged = _reconcile(
        tmp_path,
        monkeypatch,
        [sidecar_a],
        [_wake("delivery-b", timestamp=200.0)],
    )

    assert _delivery_ids(merged) == ["delivery-a", "delivery-b"]
    assert sidecar_a["display_metadata"] == {"delivery_id": "delivery-a"}


def test_mixed_same_text_same_timestamp_distinct_deliveries_both_survive(
    tmp_path, monkeypatch
):
    sidecar_a = _wake("delivery-a", timestamp=100.0)

    merged = _reconcile(
        tmp_path,
        monkeypatch,
        [sidecar_a],
        [
            _wake("delivery-a", timestamp=100.0),
            _wake("delivery-b", timestamp=100.0),
        ],
    )

    assert _delivery_ids(merged) == ["delivery-a", "delivery-b"]
    assert sidecar_a["display_metadata"] == {"delivery_id": "delivery-a"}


def test_mixed_exact_role_content_timestamp_match_receives_provenance(
    tmp_path, monkeypatch
):
    """The legitimate same-turn pairing still projects durable provenance."""
    sidecar = {
        "role": "user",
        "content": WAKE_TEXT,
        "timestamp": 100.25,
        "_source": "process_wakeup",
    }

    merged = _reconcile(
        tmp_path,
        monkeypatch,
        [sidecar],
        [_wake("delivery-1", timestamp=100.25)],
    )

    assert merged[2:] == [sidecar]
    assert len(merged) == 3
    assert sidecar["display_metadata"] == {"delivery_id": "delivery-1"}
    assert sidecar["_source"] == "process_wakeup"


def test_mixed_sub_second_timestamp_mismatch_does_not_transfer_provenance(
    tmp_path, monkeypatch
):
    browser = _browser(WAKE_TEXT, 100.5)
    before = dict(browser)

    merged = _reconcile(
        tmp_path,
        monkeypatch,
        [browser],
        [_wake("delivery-1", timestamp=100.25)],
    )

    _assert_browser_row_untouched(merged, browser, before)
    assert _delivery_ids(merged) == ["delivery-1"]


@pytest.mark.parametrize(
    "existing_provenance",
    [
        {"display_kind": "process_wakeup"},
        {
            "display_kind": "other",
            "display_metadata": {"delivery_id": "sidecar-conflict"},
        },
    ],
    ids=("partial", "conflicting"),
)
def test_mixed_rejected_provenance_transfer_keeps_authoritative_wake(
    tmp_path,
    monkeypatch,
    existing_provenance,
):
    """A rejected exact-pair transfer must fail closed without dropping state.db."""
    sidecar = {
        "role": "user",
        "content": WAKE_TEXT,
        "timestamp": 100.25,
        **existing_provenance,
    }
    before = json.loads(json.dumps(sidecar))

    merged = _reconcile(
        tmp_path,
        monkeypatch,
        [sidecar],
        [_wake("delivery-authoritative", timestamp=100.25)],
    )

    assert len(merged) == 4
    assert merged[2] is sidecar
    assert sidecar == before
    authoritative = [
        msg
        for msg in merged
        if models._trusted_wakeup_delivery_id(msg) == "delivery-authoritative"
    ]
    assert len(authoritative) == 1
    assert authoritative[0]["_source"] == "process_wakeup"
