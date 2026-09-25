"""Regression coverage for source-aware transcript merge ordering."""

import sqlite3
from types import SimpleNamespace

import pytest

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


def test_state_db_prefix_replay_keeps_anonymous_cross_second_rows():
    sidecar = _assistant("repeated", 100.0)
    replay = _assistant("repeated", 101.0)

    assert models.merge_session_messages_append_only(
        [sidecar], [replay], incoming_provenance="state_db",
    ) == [sidecar, replay]


def test_state_db_keeps_subsecond_legacy_collision_before_later_sidecar_row():
    sidecar = [
        _assistant("same", 100.25),
        _user("later", 102.0),
    ]
    state = [_assistant("same", 100.75)]

    merged = models.merge_session_messages_append_only(
        sidecar, state, incoming_provenance="state_db",
    )

    assert [(message["content"], message["timestamp"]) for message in merged] == [
        ("same", 100.25),
        ("same", 100.75),
        ("later", 102.0),
    ]


def test_state_db_keeps_older_legacy_collision_without_reordering_sidecar_rows():
    sidecar = [
        _assistant("same", 102.0),
        _user("later", 103.0),
    ]
    state = [_assistant("same", 100.75)]

    merged = models.merge_session_messages_append_only(
        sidecar, state, incoming_provenance="state_db",
    )

    assert [(message["content"], message["timestamp"]) for message in merged] == [
        ("same", 102.0),
        ("later", 103.0),
        ("same", 100.75),
    ]


def test_state_db_keeps_older_anonymous_user_with_unequal_timestamp():
    sidecar = [_user("same", 102.0), _assistant("later", 103.0)]
    state = [_user("same", 100.75)]

    merged = models.merge_session_messages_append_only(
        sidecar, state, incoming_provenance="state_db",
    )

    assert sorted(
        message["timestamp"] for message in merged if message["content"] == "same"
    ) == [100.75, 102.0]


def test_state_db_context_delta_requires_identity_for_restamped_prefix():
    sidecar_context = [_user("q", 99.0), _assistant("same", 100.0)]
    state = [
        _user("q", 199.0, _state_db_row_id=7001),
        _assistant("same", 200.0, _state_db_row_id=7002),
    ]

    assert models.state_db_delta_after_context(sidecar_context, state) == state

    shared_identity_context = [
        _user("q", 99.0, id="user-row", _state_db_row_id=7001),
        _assistant("same", 100.0, id="answer-row", _state_db_row_id=7002),
    ]
    shared_identity_state = [
        _user("q", 199.0, id="user-row", _state_db_row_id=7001),
        _assistant("same", 200.0, id="answer-row", _state_db_row_id=7002),
    ]

    assert models.state_db_delta_after_context(
        shared_identity_context, shared_identity_state,
    ) == []


@pytest.mark.parametrize(
    ("malformed_side", "timestamp"),
    [
        pytest.param("state", "not-a-timestamp", id="state-unparseable"),
        pytest.param("state", float("nan"), id="state-nan"),
        pytest.param("state", float("inf"), id="state-infinite"),
        pytest.param("sidecar", "not-a-timestamp", id="sidecar-unparseable"),
        pytest.param("sidecar", float("nan"), id="sidecar-nan"),
    ],
)
def test_state_db_context_delta_preserves_rows_with_malformed_timestamps(
    malformed_side, timestamp,
):
    sidecar_context = [
        _user("same question", 100.0),
        _assistant("same answer", 101.0),
    ]
    state = [
        _user("same question", 100.0),
        _assistant("same answer", 101.0),
    ]
    rows = state if malformed_side == "state" else sidecar_context
    rows[0]["timestamp"] = timestamp

    delta = models.state_db_delta_after_context(sidecar_context, state)

    assert [message["content"] for message in delta] == [
        "same question",
        "same answer",
    ]
    merged = models.merge_session_messages_append_only(
        sidecar_context, state, incoming_provenance="state_db",
    )
    assert [message["content"] for message in merged].count("same question") == 2
    assert [message["content"] for message in merged].count("same answer") == 1


def test_state_db_context_delta_keeps_idless_restamped_pending_user_collision():
    stream_id = "pending-context-stream"
    token = models.build_active_turn_token(stream_id, 99.0)
    sidecar_context = [
        _user("old", 10.0),
        _assistant("answer", 20.0),
        _user("q", 99.0, _active_turn_token=token),
    ]
    state = [
        _user("old", 10.0),
        _assistant("answer", 20.0),
        _user("q", 199.0),
    ]
    session = models.Session(
        messages=sidecar_context,
        context_messages=sidecar_context,
        active_stream_id=stream_id,
        pending_started_at=99.0,
        pending_user_message="q",
        pending_user_source="webui",
        _webui_pending_user_timestamp_identity=(stream_id, 99.0),
    )

    assert session._webui_pending_user_timestamp_identity == (stream_id, 99.0)
    assert models.state_db_delta_after_context(sidecar_context, state) == [state[-1]]
    reconciled = models.reconciled_state_db_messages_for_session(
        session,
        prefer_context=True,
        state_messages=state,
    )
    assert [
        (message["content"], message["timestamp"])
        for message in reconciled
        if message.get("role") == "user" and message["content"] == "q"
    ] == [("q", 99.0), ("q", 199.0)]


def test_state_db_prefix_replay_uses_full_precision_and_shared_identity():
    cases = [
        (_assistant("same", 100.0), _assistant("same", 100.0)),
        (_assistant("same", None), _assistant("same", 101.0)),
        (
            _assistant("same", 100.0, id="message-1"),
            _assistant("same", 101.0, id="message-1"),
        ),
        (
            _assistant("same", 100.0, _state_db_row_id=42),
            _assistant("same", 101.0, _state_db_row_id=42),
        ),
        (
            _user("same", 100.0, _state_db_row_id=43),
            _user("same", 101.0, _state_db_row_id=43),
        ),
        (
            _assistant("same", 100.0, _active_turn_token="turn:1"),
            _assistant("same", 101.0, _active_turn_token="turn:1"),
        ),
        (
            _user("same", 100.0, _active_turn_token="turn:2"),
            _user("same", 101.0, _active_turn_token="turn:2"),
        ),
    ]

    for sidecar, replay in cases:
        assert models.merge_session_messages_append_only(
            [sidecar], [replay], incoming_provenance="state_db",
        ) == [sidecar]

    sidecar = _assistant("same", 100.25)
    replay = _assistant("same", 100.75)
    assert models.merge_session_messages_append_only(
        [sidecar], [replay], incoming_provenance="state_db",
    ) == [sidecar, replay]


def test_unverified_prefix_replay_still_dedupes_nonuniform_restamps():
    sidecar = [
        _assistant("first", 100.0),
        _assistant("second", 101.0),
        _assistant("third", 102.0),
    ]
    restamped = [
        _assistant("first", 103.0),
        _assistant("second", 105.0),
        _assistant("third", 108.0),
    ]

    assert models.merge_session_messages_append_only(sidecar, restamped) == sidecar


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
