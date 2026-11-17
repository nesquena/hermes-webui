"""The Gateway runs path keeps the context the Agent compacted mid-turn.

``/v1/runs`` uses a caller-supplied ``conversation_history`` as-is. When the
Agent compacts the conversation in place during a turn, its committed result
lives only in ``state.db``; the WebUI success writeback kept appending to its
own pre-compaction ``context_messages``, so every later turn re-sent the whole
history, was rejected for size, and was compacted again.

Each test drives the real ``_run_gateway_chat_streaming`` against the fake
``/v1/runs`` gateway of ``test_gateway_cancel_reconciliation`` and a real
``state.db`` holding the rows an in-place compaction leaves active.
"""
from __future__ import annotations

import os
import sqlite3
import time
from unittest.mock import patch

import pytest

import api.config as cfg
import api.models as models
from api.models import get_session
from tests.test_gateway_cancel_reconciliation import (
    CAPTURED_BODIES,
    GATEWAY_STORE,
    fake_urlopen,
    history_rows,
    make_session,
)

SUMMARY = (
    "[CONTEXT COMPACTION — REFERENCE ONLY] Earlier turns were compacted into the summary below.\n"
    "The user asked two questions and both were answered."
)
OLD_CONTEXT = [
    {"role": "user", "content": "old question one", "timestamp": 10.0},
    {"role": "assistant", "content": "old answer one", "timestamp": 11.0},
    {"role": "user", "content": "old question two", "timestamp": 12.0},
    {"role": "assistant", "content": "old answer two", "timestamp": 13.0},
]


@pytest.fixture(autouse=True)
def _clean_gateway_state():
    del CAPTURED_BODIES[:]
    GATEWAY_STORE.clear()
    yield
    with cfg.STREAMS_LOCK:
        for key in ("STREAMS", "CANCEL_FLAGS", "STREAM_PARTIAL_TEXT", "STREAM_REASONING_TEXT",
                    "STREAM_LIVE_TOOL_CALLS", "STREAM_LAST_EVENT_ID"):
            getattr(cfg, key).clear()
    cfg.ACTIVE_RUNS.clear()


def _write_agent_rows(session_id, rows):
    """Replace the session's state.db rows; each row is (role, content, timestamp, active)."""
    db_path = models._active_state_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS messages (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "session_id TEXT NOT NULL, role TEXT NOT NULL, content TEXT, timestamp REAL NOT NULL, "
            "active INTEGER NOT NULL DEFAULT 1)"
        )
        conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
        conn.executemany(
            "INSERT INTO messages (session_id, role, content, timestamp, active) VALUES (?, ?, ?, ?, ?)",
            [(session_id, role, content, ts, active) for role, content, ts, active in rows],
        )
        conn.commit()
    finally:
        conn.close()


def _start_turn(session, stream_id, prompt):
    session.active_stream_id = stream_id
    session.pending_user_message = prompt
    session.pending_started_at = time.time()
    session.pending_attachments = []
    session.pending_user_source = "webui"
    session.save()
    return session.pending_started_at


def _run_turn(session_id, prompt, stream_id):
    import api.gateway_chat as gateway_chat

    with cfg.STREAMS_LOCK:
        cfg.STREAMS[stream_id] = cfg.create_stream_channel()
    env = {"HERMES_WEBUI_CHAT_BACKEND": "gateway", "HERMES_WEBUI_GATEWAY_USE_RUNS_API": "1"}
    with patch.dict(os.environ, env), \
            patch.object(gateway_chat, "gateway_supports_approval", lambda *a, **k: True), \
            patch.object(gateway_chat.urllib.request, "urlopen", side_effect=fake_urlopen):
        gateway_chat._run_gateway_chat_streaming(session_id, prompt, "test-model", "/tmp", stream_id, [])


def _texts(rows):
    return [row["content"] for row in rows]


def _compacted_rows(started, prompt, *, summary_at=None):
    """What an in-place compaction leaves in state.db: archived old rows, then the live set."""
    summary_at = started + 1 if summary_at is None else summary_at
    return [
        ("user", "old question one", 10.0, 0),
        ("assistant", "old answer one", 11.0, 0),
        ("user", "old question two", 12.0, 0),
        ("assistant", "old answer two", 13.0, 0),
        ("user", SUMMARY, summary_at, 1),
        ("assistant", "old answer two", summary_at, 1),
        ("user", prompt, summary_at, 1),
        ("assistant", "turn answer", summary_at + 1, 1),
    ]


def test_context_follows_the_agents_in_place_compaction():
    session = make_session("compaction-adopt", OLD_CONTEXT)
    started = _start_turn(session, "stream-adopt-1", "new question")
    _write_agent_rows(session.session_id, _compacted_rows(started, "new question"))

    _run_turn(session.session_id, "new question", "stream-adopt-1")

    saved = get_session(session.session_id)
    assert _texts(saved.context_messages) == [SUMMARY, "old answer two", "new question", "turn answer"]
    # The visible transcript keeps every turn and never shows the summary.
    assert _texts(saved.messages) == [
        "old question one", "old answer one", "old question two", "old answer two",
        "new question", "turn answer",
    ]


def test_the_next_turn_sends_the_compacted_history_not_the_old_one():
    session = make_session("compaction-next", OLD_CONTEXT)
    started = _start_turn(session, "stream-next-1", "new question")
    _write_agent_rows(session.session_id, _compacted_rows(started, "new question"))
    _run_turn(session.session_id, "new question", "stream-next-1")

    session = get_session(session.session_id)
    _start_turn(session, "stream-next-2", "follow up")
    _run_turn(session.session_id, "follow up", "stream-next-2")

    assert history_rows(CAPTURED_BODIES[-1]) == [
        ("user", SUMMARY),
        ("assistant", "old answer two"),
        ("user", "new question"),
        ("assistant", "turn answer"),
    ]
    saved = get_session(session.session_id)
    # Adopted rows are model context only: no summary and no second copy of a kept turn.
    shown = _texts(saved.messages)
    assert SUMMARY not in shown
    assert shown.count("old answer two") == 1
    assert shown[:6] == [
        "old question one", "old answer one", "old question two", "old answer two",
        "new question", "turn answer",
    ]
    assert "follow up" in shown


def test_a_prompt_the_agent_folded_into_the_previous_user_row_is_split_off():
    session = make_session("compaction-folded", OLD_CONTEXT)
    started = _start_turn(session, "stream-fold-1", "new question")
    _write_agent_rows(session.session_id, [
        ("user", SUMMARY, started + 1, 1),
        ("assistant", "old answer two", started + 1, 1),
        ("user", "a delegated result\n\nnew question", started + 1, 1),
        ("assistant", "turn answer", started + 2, 1),
    ])

    _run_turn(session.session_id, "new question", "stream-fold-1")

    saved = get_session(session.session_id)
    assert _texts(saved.context_messages) == [
        SUMMARY, "old answer two", "a delegated result", "new question", "turn answer",
    ]


@pytest.mark.parametrize(
    "rows_for",
    [
        # A compaction from before this turn: the WebUI context already reflects it, or the
        # user changed it since (truncate, undo), so the stored rows must not replace it.
        lambda started: _compacted_rows(started, "new question", summary_at=started - 60),
        # No compaction at all.
        lambda started: [
            ("user", "old question one", 10.0, 1),
            ("assistant", "old answer one", 11.0, 1),
            ("user", "new question", started + 1, 1),
            ("assistant", "turn answer", started + 2, 1),
        ],
        # A compaction, but this turn's prompt is not in the stored rows.
        lambda started: [
            ("user", SUMMARY, started + 1, 1),
            ("assistant", "old answer two", started + 1, 1),
        ],
        # The compacted rows are archived, not active.
        lambda started: [(role, text, ts, 0) for role, text, ts, _ in _compacted_rows(started, "new question")],
    ],
    ids=["older-compaction", "no-compaction", "prompt-missing", "archived-only"],
)
def test_the_context_is_left_alone_without_a_compaction_from_this_turn(rows_for):
    session = make_session("compaction-none", OLD_CONTEXT)
    started = _start_turn(session, "stream-none-1", "new question")
    _write_agent_rows(session.session_id, rows_for(started))

    _run_turn(session.session_id, "new question", "stream-none-1")

    saved = get_session(session.session_id)
    assert _texts(saved.context_messages) == [
        "old question one", "old answer one", "old question two", "old answer two",
        "new question", "turn answer",
    ]


def test_the_context_is_left_alone_when_state_db_is_missing():
    session = make_session("compaction-nodb", OLD_CONTEXT)
    _start_turn(session, "stream-nodb-1", "new question")

    _run_turn(session.session_id, "new question", "stream-nodb-1")

    saved = get_session(session.session_id)
    assert _texts(saved.context_messages) == [
        "old question one", "old answer one", "old question two", "old answer two",
        "new question", "turn answer",
    ]
