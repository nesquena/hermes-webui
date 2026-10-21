"""Regression coverage for source-aware transcript merge ordering."""

import sqlite3
from types import SimpleNamespace

import api.models as models
import api.routes as routes


def _user(content, ts, **extra):
    return {"role": "user", "content": content, "timestamp": ts, **extra}


def _assistant(content, ts, **extra):
    return {"role": "assistant", "content": content, "timestamp": ts, **extra}


def _read_idless_state_rows(monkeypatch, tmp_path, rows):
    """Project the production shape from a schema with no durable row id."""
    db_path = tmp_path / "state.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE messages "
            "(session_id TEXT, role TEXT, content TEXT, timestamp REAL)"
        )
        conn.executemany(
            "INSERT INTO messages VALUES (?, ?, ?, ?)",
            [
                ("ordering-session", row["role"], row["content"], row["timestamp"])
                for row in rows
            ],
        )
    monkeypatch.setattr(models, "_active_state_db_path", lambda: db_path)
    return models.get_state_db_session_messages("ordering-session")


def test_idless_state_db_projection_keeps_older_row_in_chronological_slot(
    monkeypatch, tmp_path
):
    """The SQLite reader must not turn its row identity into a WebUI stable id."""
    sidecar = [
        _user("question one", 1000.0, id="parent-user"),
        _assistant("answer one", 1010.0, id="parent-answer"),
        _assistant("final conclusion", 1200.0, id="parent-final"),
    ]
    state = _read_idless_state_rows(
        monkeypatch,
        tmp_path,
        [_user("question two", 1100.0)],
    )

    assert state == [_user("question two", 1100.0)]

    session = SimpleNamespace(
        session_id="ordering-session",
        messages=sidecar,
        truncation_watermark=None,
        truncation_boundary=None,
    )
    merged = models.reconciled_state_db_messages_for_session(
        session,
        state_messages=state,
    )

    assert [message["content"] for message in merged] == [
        "question one",
        "answer one",
        "question two",
        "final conclusion",
    ]


def test_idless_state_db_projection_still_appends_genuinely_newest_row(
    monkeypatch, tmp_path
):
    sidecar = [
        _user("question one", 1000.0, id="parent-user"),
        _assistant("answer one", 1010.0, id="parent-answer"),
    ]
    state = _read_idless_state_rows(
        monkeypatch,
        tmp_path,
        [_user("late question", 2000.0)],
    )

    session = SimpleNamespace(
        session_id="ordering-session",
        messages=sidecar,
        truncation_watermark=None,
        truncation_boundary=None,
    )
    merged = models.reconciled_state_db_messages_for_session(
        session,
        state_messages=state,
    )

    assert [message["content"] for message in merged][-1] == "late question"


def test_private_state_db_provenance_can_reorder_terminal_conflict_row():
    """Only a private state.db row identity can authorize terminal reordering."""
    sidecar = [
        _user("question one", 1000.0, id="sidecar-user"),
        _user(
            "recovered question",
            1100.0,
            _state_db_row_id=848467,
            api_content="wire-sidecar",
        ),
        _assistant("final conclusion", 1200.0, id="sidecar-final"),
    ]
    state = [
        _user(
            "recovered question",
            1100.0,
            _state_db_row_id=848467,
            api_content="wire-state-db-conflict",
        )
    ]

    merged = models.merge_session_messages_append_only(
        sidecar,
        state,
        incoming_provenance="state_db",
    )

    assert [message["content"] for message in merged] == [
        "question one",
        "recovered question",
        "recovered question",
        "final conclusion",
    ]


def test_compression_child_stable_ids_remain_after_restamped_parent(monkeypatch):
    """Child-sidecar sequence is authoritative even when parent timestamps are later."""
    parent = SimpleNamespace(
        session_id="compression-parent",
        parent_session_id=None,
        session_source="webui",
        pre_compression_snapshot=True,
        truncation_watermark=None,
        truncation_boundary=None,
        messages=[
            _user("parent question", 1000.0, id="parent-user"),
            _assistant("parent answer", 1400.0, id="parent-answer"),
        ],
    )
    child = SimpleNamespace(
        session_id="compression-child",
        parent_session_id="compression-parent",
        session_source="webui",
        pre_compression_snapshot=False,
        truncation_watermark=None,
        truncation_boundary=None,
        messages=[
            _user("child continuation", 1100.0, id="child-user"),
            _assistant("child answer", 1200.0, id="child-answer"),
        ],
    )
    monkeypatch.setattr(
        routes.Session,
        "load",
        lambda session_id: parent if session_id == parent.session_id else None,
    )

    merged = routes._webui_sidecar_lineage_messages_for_display(child)

    assert [message["id"] for message in merged] == [
        "parent-user",
        "parent-answer",
        "child-user",
        "child-answer",
    ]
