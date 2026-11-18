"""Compose messaging reset compatibility with independent reset lineage (#7179)."""

import json

import pytest

from api import agent_sessions
from tests.test_session_lineage_report import _ensure_state_db, _insert_message, _insert_state_row


@pytest.fixture(scope="session", autouse=True)
def test_server():
    """All readers use disposable SQLite fixtures, not the shared HTTP server."""


@pytest.mark.parametrize(
    "parent_fields,child_fields,reset",
    [
        ({}, {}, True),
        ({}, {"session_source": "fork"}, False),
        ({}, {"session_source": "fork", "model_config": {"_reset_from": "parent"}}, False),
        ({}, {"source": "tool", "model_config": {"_reset_from": "parent"}}, False),
        ({}, {"source": "webui"}, False),
        ({}, {"source": "discord"}, False),
        ({}, {"source": "webui", "model_config": {"_reset_from": "parent"}}, False),
        ({}, {"source": "discord", "model_config": {"_reset_from": "parent"}}, False),
        ({}, {"source": "", "model_config": {"_reset_from": "parent"}}, False),
        ({}, {"session_key": "other"}, False),
        ({}, {"model_config": {"_branched_from": "parent"}}, False),
        ({}, {"model_config": {"_delegate_from": "parent"}}, False),
        ({}, {"model_config": {"_branched_from": "ancestor"}}, True),
        ({}, {"model_config": {"_delegate_from": "ancestor"}}, True),
        ({}, {"model_config": {"_reset_from": "other-parent"}}, False),
        ({}, {"model_config": "{"}, False),
        ({}, {"model_config": {"_branched_from": ""}}, False),
        ({}, {"model_config": {"_delegate_from": None}}, False),
        ({}, {"model_config": {"_reset_from": None}}, False),
        ({}, {"model_config": {"_reset_from": "parent", "_branched_from": "parent"}}, False),
        ({}, {"model_config": {"_reset_from": "parent", "_branched_from": "ancestor"}}, True),
        ({"end_reason": None, "ended_at": None}, {"model_config": {"_reset_from": "parent"}}, True),
        ({"end_reason": None, "ended_at": None},
         {"model_config": {"_reset_from": "parent", "_delegate_from": "ancestor"}}, True),
        ({"end_reason": "compression"}, {"model_config": {"_reset_from": "parent"}}, True),
        ({"end_reason": "session_switch"}, {}, True),
        ({"source": "cli"}, {"source": "cli"}, True),
        ({}, {"started_at": 150}, False),
        ({"session_key": None, "chat_id": "chat", "user_id": "user", "thread_id": "thread"},
         {"session_key": None, "chat_id": "chat", "user_id": "user", "thread_id": "thread"}, True),
        ({"session_key": None, "origin_chat_id": "chat", "origin_user_id": "user"},
         {"session_key": None, "origin_chat_id": "chat", "origin_user_id": "user"}, True),
        ({"session_key": None, "chat_id": "chat", "user_id": "user"},
         {"origin_chat_id": "chat", "origin_user_id": "user"}, True),
        ({"session_key": None, "chat_id": "chat", "user_id": "user", "thread_id": "one"},
         {"session_key": None, "chat_id": "chat", "user_id": "user", "thread_id": "two"}, False),
        ({"session_key": None}, {"session_key": None}, False),
    ],
)
def test_reset_classification_agrees_across_readers(tmp_path, parent_fields, child_fields, reset):
    db_path = tmp_path / "state.db"
    conn = _ensure_state_db(db_path)
    try:
        for column in ("chat_id", "user_id", "thread_id", "origin_chat_id", "origin_user_id"):
            conn.execute(f"ALTER TABLE sessions ADD COLUMN {column} TEXT")
        _insert_state_row(conn, "parent", source="telegram", started_at=100,
                          ended_at=200, end_reason="session_reset", session_key="same-key")
        _insert_state_row(conn, "child", source="telegram", started_at=210,
                          parent="parent", session_key="same-key")
        for sid, fields in (("parent", parent_fields), ("child", child_fields)):
            for column, value in fields.items():
                if isinstance(value, dict):
                    value = json.dumps(value)
                conn.execute(f"UPDATE sessions SET {column} = ? WHERE id = ?", (value, sid))
            _insert_message(conn, sid, timestamp=220)
        conn.commit()
    finally:
        conn.close()

    projected = {row["id"]: row for row in agent_sessions.read_importable_agent_session_rows(
        db_path, limit=None, exclude_sources=None,
    )}
    # The metadata reader re-fetches descendants after its ancestor lookup;
    # both SELECT lists must retain every optional identity column by name.
    metadata = agent_sessions.read_session_lineage_metadata(db_path, ["child"])
    for rows in (projected, metadata):
        child = rows["child"]
        assert child["parent_session_id"] == "parent"
        assert child["relationship_type"] == ("reset_successor" if reset else "child_session")
        if reset:
            assert child["_lineage_root_id"] == "child"
            assert "parent_title" not in child
        assert "model_config" not in child
    parent_report = agent_sessions.read_session_lineage_report(db_path, "parent")
    assert parent_report["found"] is True
    assert [row["session_id"] for row in parent_report["children"]] == ([] if reset else ["child"])
    child_report = agent_sessions.read_session_lineage_report(db_path, "child")
    assert child_report["lineage_key"] == "child"
    assert child_report["total_segments"] == 1


@pytest.mark.parametrize("canonical", [False, True])
@pytest.mark.parametrize("inherited", [None, "_branched_from", "_delegate_from"])
def test_optional_identity_columns_and_missing_parent(tmp_path, canonical, inherited):
    """Old schemas still classify keyed resets; canonical intent survives a missing parent."""
    db_path = tmp_path / "state.db"
    conn = _ensure_state_db(db_path)
    try:
        if not canonical:
            _insert_state_row(conn, "parent", source="telegram", started_at=100,
                              ended_at=200, end_reason="session_reset", session_key="same-key")
        config = {"_reset_from": "parent"} if canonical else {}
        if inherited:
            config[inherited] = "ancestor"
        _insert_state_row(conn, "child", source="telegram", started_at=210,
                          parent="parent", session_key="same-key",
                          model_config=config or None)
        _insert_message(conn, "child", timestamp=220)
    finally:
        conn.close()
    projected = agent_sessions.read_importable_agent_session_rows(db_path, exclude_sources=None)
    metadata = agent_sessions.read_session_lineage_metadata(db_path, ["child"])
    assert projected[0]["relationship_type"] == "reset_successor"
    assert metadata["child"]["relationship_type"] == "reset_successor"
    assert metadata["child"]["_lineage_root_id"] == "child"
    assert agent_sessions.read_session_lineage_report(db_path, "child")["lineage_key"] == "child"


def test_canonical_reset_cannot_override_unknown_messaging_source(tmp_path):
    """NULL-source rows are not listable, but metadata must also refuse promotion."""
    db_path = tmp_path / "state.db"
    conn = _ensure_state_db(db_path)
    try:
        _insert_state_row(conn, "parent", source="telegram", started_at=100,
                          ended_at=200, end_reason="session_reset", session_key="same-key")
        _insert_state_row(conn, "child", source=None, started_at=210, parent="parent",
                          session_key="same-key", model_config={"_reset_from": "parent"})
    finally:
        conn.close()
    metadata = agent_sessions.read_session_lineage_metadata(db_path, ["child"])
    assert metadata["child"]["relationship_type"] == "child_session"
    report = agent_sessions.read_session_lineage_report(db_path, "parent")
    assert [row["session_id"] for row in report["children"]] == ["child"]


@pytest.mark.parametrize("identity,reset", [("direct", True), ("origin", True), (None, False)])
def test_legacy_schema_without_key_or_model_config(tmp_path, identity, reset):
    db_path = tmp_path / "state.db"
    conn = _ensure_state_db(db_path)
    try:
        _insert_state_row(conn, "parent", source="telegram", started_at=100,
                          ended_at=200, end_reason="session_reset")
        _insert_state_row(conn, "child", source="telegram", started_at=210, parent="parent")
        _insert_message(conn, "parent", timestamp=101)
        _insert_message(conn, "child", timestamp=211)
        conn.execute("ALTER TABLE sessions DROP COLUMN session_key")
        conn.execute("ALTER TABLE sessions DROP COLUMN model_config")
        if identity:
            prefix = "origin_" if identity == "origin" else ""
            for field in ("chat_id", "user_id"):
                conn.execute(f"ALTER TABLE sessions ADD COLUMN {prefix}{field} TEXT")
                conn.execute(f"UPDATE sessions SET {prefix}{field} = ?", (field,))
        conn.commit()
    finally:
        conn.close()
    projected = {row["id"]: row for row in agent_sessions.read_importable_agent_session_rows(
        db_path, exclude_sources=None,
    )}
    metadata = agent_sessions.read_session_lineage_metadata(db_path, ["child"])
    for rows in (projected, metadata):
        assert rows["child"]["relationship_type"] == ("reset_successor" if reset else "child_session")
    report = agent_sessions.read_session_lineage_report(db_path, "parent")
    assert report["found"] is True
    assert [row["session_id"] for row in report["children"]] == ([] if reset else ["child"])
