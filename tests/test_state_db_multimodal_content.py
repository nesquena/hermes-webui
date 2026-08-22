"""Regression coverage for Hermes Agent structured state.db content."""

import json
import sqlite3
from types import SimpleNamespace

import pytest

import api.models as models


pytestmark = pytest.mark.requires_agent_modules

SESSION_ID = "multimodal-state-db-test"
IMAGE_A = "data:image/png;base64,AA=="
IMAGE_B = "data:image/png;base64,AQ=="


def _rich_content(image_url=IMAGE_A, text="describe this image"):
    return [
        {"type": "text", "text": text},
        {"type": "image_url", "image_url": {"url": image_url}},
    ]


def _encoded_content(content):
    return "\x00json:" + json.dumps(content, separators=(",", ":"))


def _make_state_db(path, rows):
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            role TEXT,
            content TEXT,
            timestamp REAL,
            tool_calls TEXT
        )
        """
    )
    conn.executemany(
        """
        INSERT INTO messages (session_id, role, content, timestamp, tool_calls)
        VALUES (?, ?, ?, ?, ?)
        """,
        [
            (
                SESSION_ID,
                row["role"],
                row["content"],
                row.get("timestamp", 1000.0),
                row.get("tool_calls"),
            )
            for row in rows
        ],
    )
    conn.commit()
    conn.close()


def test_decode_state_db_content_accepts_only_json_lists():
    content = _rich_content()

    assert models._decode_state_db_content(_encoded_content(content)) == content
    assert models._message_content_text({"content": content}) == "describe this image"


@pytest.mark.parametrize(
    "raw",
    [
        "\x00json:{\"kind\":\"dict\"}",
        "\x00json:\"string root\"",
        "\x00json:42",
        "\x00json:{malformed",
    ],
)
def test_state_db_readers_keep_malformed_and_non_list_roots_as_strings(
    raw,
    tmp_path,
    monkeypatch,
):
    db = tmp_path / "state.db"
    _make_state_db(db, [{"role": "user", "content": raw}])
    monkeypatch.setattr(models, "_active_state_db_path", lambda: db)

    messages = models.get_state_db_session_messages(SESSION_ID)
    keys = models.get_state_db_session_message_keys_before_timestamp(
        SESSION_ID,
        1001.0,
    )
    expected_key = models._session_message_visible_key(
        {"role": "user", "content": raw},
        normalize_workspace_prefix=True,
    )

    assert models._decode_state_db_content(raw) == raw
    assert isinstance(messages[0]["content"], str)
    assert messages[0]["content"] == raw
    assert keys == [expected_key]


def test_decode_state_db_content_keeps_plain_content_byte_for_byte():
    raw = "plain content\nwith\tspacing"

    assert models._decode_state_db_content(raw) == raw


def test_get_state_db_session_messages_decodes_structured_content(tmp_path, monkeypatch):
    db = tmp_path / "state.db"
    content = _rich_content()
    _make_state_db(
        db,
        [{"role": "user", "content": _encoded_content(content)}],
    )
    monkeypatch.setattr(models, "_active_state_db_path", lambda: db)

    messages = models.get_state_db_session_messages(SESSION_ID)

    assert messages[0]["content"] == content
    assert "\x00json:" not in repr(messages[0]["content"])
    assert IMAGE_A not in models._message_content_text(messages[0])


def test_get_state_db_session_message_keys_use_decoded_content(tmp_path, monkeypatch):
    db = tmp_path / "state.db"
    content = _rich_content()
    _make_state_db(
        db,
        [
            {
                "role": "user",
                "content": _encoded_content(content),
                "timestamp": 1000.0,
            }
        ],
    )
    monkeypatch.setattr(models, "_active_state_db_path", lambda: db)

    keys = models.get_state_db_session_message_keys_before_timestamp(
        SESSION_ID,
        1001.0,
    )

    expected = models._session_message_visible_key(
        {"role": "user", "content": content},
        normalize_workspace_prefix=True,
    )
    raw_key = models._session_message_visible_key(
        {"role": "user", "content": _encoded_content(content)},
        normalize_workspace_prefix=True,
    )
    assert keys == [expected]
    assert keys != [raw_key]


def test_state_db_rich_sidecar_row_deduplicates_without_flattening(
    tmp_path,
    monkeypatch,
):
    db = tmp_path / "state.db"
    content = _rich_content()
    _make_state_db(
        db,
        [{"role": "user", "content": _encoded_content(content)}],
    )
    monkeypatch.setattr(models, "_active_state_db_path", lambda: db)
    sidecar = [{"role": "user", "content": content, "timestamp": 1000.0}]

    merged = models.merge_session_messages_append_only(
        sidecar,
        models.get_state_db_session_messages(SESSION_ID),
    )

    assert merged == sidecar


def test_prefixed_state_db_rich_row_matches_bare_sidecar(tmp_path, monkeypatch):
    db = tmp_path / "state.db"
    bare_content = _rich_content()
    prefixed_content = _rich_content(
        text="[Workspace::v1: /tmp/synthetic]\ndescribe this image",
    )
    _make_state_db(
        db,
        [{"role": "user", "content": _encoded_content(prefixed_content)}],
    )
    monkeypatch.setattr(models, "_active_state_db_path", lambda: db)
    sidecar = [{"role": "user", "content": bare_content, "timestamp": 1000.0}]

    state_messages = models.get_state_db_session_messages(SESSION_ID)
    merged = models.merge_session_messages_append_only(sidecar, state_messages)
    keys = models.get_state_db_session_message_keys_before_timestamp(
        SESSION_ID,
        1001.0,
    )
    expected_key = models._session_message_visible_key(
        sidecar[0],
        normalize_workspace_prefix=False,
    )

    assert merged == sidecar
    assert keys == [expected_key]


def test_same_text_different_image_turns_remain_distinct(tmp_path, monkeypatch):
    db = tmp_path / "state.db"
    first = _rich_content(IMAGE_A)
    second = _rich_content(IMAGE_B)
    _make_state_db(
        db,
        [
            {"role": "user", "content": _encoded_content(first), "timestamp": 1000.0},
            {"role": "user", "content": _encoded_content(second), "timestamp": 1001.0},
        ],
    )
    monkeypatch.setattr(models, "_active_state_db_path", lambda: db)
    sidecar = [{"role": "user", "content": first, "timestamp": 1000.0}]

    merged = models.merge_session_messages_append_only(
        sidecar,
        models.get_state_db_session_messages(SESSION_ID),
    )

    assert [message["content"] for message in merged] == [first, second]


def test_reconciled_model_context_preserves_structured_state_db_content(
    tmp_path,
    monkeypatch,
):
    db = tmp_path / "state.db"
    rich_content = _rich_content()
    _make_state_db(
        db,
        [
            {"role": "user", "content": "old user", "timestamp": 1000.0},
            {"role": "assistant", "content": "old answer", "timestamp": 1001.0},
            {
                "role": "user",
                "content": _encoded_content(rich_content),
                "timestamp": 1002.0,
            },
            {"role": "assistant", "content": "new answer", "timestamp": 1003.0},
        ],
    )
    monkeypatch.setattr(models, "_active_state_db_path", lambda: db)
    context = [
        {"role": "user", "content": "old user", "timestamp": 1000.0},
        {"role": "assistant", "content": "old answer", "timestamp": 1001.0},
    ]
    session = SimpleNamespace(
        session_id=SESSION_ID,
        messages=context,
        context_messages=context,
        profile=None,
    )

    reconciled = models.reconciled_state_db_messages_for_session(
        session,
        prefer_context=True,
    )
    from api.streaming import _sanitize_messages_for_agent

    assert reconciled[2]["content"] == rich_content
    assert models._message_content_text(reconciled[2]) == "describe this image"
    sanitized = _sanitize_messages_for_agent(reconciled)
    assert sanitized[2]["content"] == rich_content
