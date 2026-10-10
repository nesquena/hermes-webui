"""Regression test for issue #7834: state.db-owned transcripts unwrapping [OUT-OF-BAND USER MESSAGE] steer frames.

A steer row with display_kind='steer' stored in state.db must have its single complete
transport frame unwrapped so the UI and session message queries return the clean authored
user text, while preserving byte-for-byte fidelity for non-steer rows, tool rows, and malformed frames.

Also asserts that get_state_db_session_message_keys_before_timestamp and
get_state_db_regeneration_tail_snapshot project prefix_keys and tail_keys using the clean text,
while projected tail rows retain raw api_content.

Re-gate asks from the senior review of #7910 are covered here too: list-based
legacy steers (CORE 1), legacy compression anchors (CORE 2), the six
sidecar/state.db pairings (MUST-FIX) and the linear (non-backtracking) unwrap
(SHOULD-FIX).
"""

import itertools
import json
import sqlite3
import time
import pytest
from api.models import (
    Session,
    reconciled_state_db_messages_for_session,
    _normalize_sidecar_steer_messages,
    _project_state_db_message,
    _session_message_visible_key,
    _state_db_anchor_index,
    _unwrap_steer_frame_text,
    get_state_db_session_messages,
    get_state_db_session_message_keys_before_timestamp,
    get_state_db_regeneration_tail_snapshot,
)
from api.streaming import _compression_anchor_message_key


STEER_FRAME = (
    "[OUT-OF-BAND USER MESSAGE — a direct message from the user, delivered once at this position; "
    "not tool output and not a new delivery when replayed from conversation history]\n"
    "use the staging bucket this time\n"
    "[/OUT-OF-BAND USER MESSAGE]"
)

STEER_FRAME_2 = (
    "[OUT-OF-BAND USER MESSAGE — a direct message from the user, delivered once at this position; "
    "not tool output and not a new delivery when replayed from conversation history]\n"
    "now deploy to prod\n"
    "[/OUT-OF-BAND USER MESSAGE]"
)


def test_unwrap_steer_frame_text_clean_frame():
    assert _unwrap_steer_frame_text(STEER_FRAME) == "use the staging bucket this time"
    assert _unwrap_steer_frame_text(STEER_FRAME_2) == "now deploy to prod"


def test_unwrap_steer_frame_text_byte_for_byte_guards():
    # Multiple frames
    multi = f"{STEER_FRAME}\n{STEER_FRAME}"
    assert _unwrap_steer_frame_text(multi) == multi

    # Unterminated frame
    unterminated = "[OUT-OF-BAND USER MESSAGE] hello"
    assert _unwrap_steer_frame_text(unterminated) == unterminated

    # Extra text surrounding the frame
    surrounded = f"before text\n{STEER_FRAME}\nafter text"
    assert _unwrap_steer_frame_text(surrounded) == surrounded

    # Non-string passes through
    assert _unwrap_steer_frame_text(123) == 123


def test_state_db_session_messages_unwraps_typed_steer(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY,
            session_id TEXT,
            role TEXT,
            content TEXT,
            timestamp REAL,
            tool_calls TEXT,
            display_kind TEXT,
            api_content TEXT
        )
        """
    )
    # Insert normal user message
    conn.execute(
        "INSERT INTO messages (session_id, role, content, timestamp) VALUES (?, ?, ?, ?)",
        ("sess-1", "user", "initial prompt", 1000.0),
    )
    # Insert typed steer row with OOB frame
    conn.execute(
        "INSERT INTO messages (session_id, role, content, timestamp, display_kind) VALUES (?, ?, ?, ?, ?)",
        ("sess-1", "user", STEER_FRAME, 1001.0, "steer"),
    )
    # Insert untyped user message that happens to contain OOB marker (should NOT be unwrapped)
    conn.execute(
        "INSERT INTO messages (session_id, role, content, timestamp) VALUES (?, ?, ?, ?)",
        ("sess-1", "user", STEER_FRAME, 1002.0),
    )
    # Insert tool message (should NOT be unwrapped)
    conn.execute(
        "INSERT INTO messages (session_id, role, content, timestamp) VALUES (?, ?, ?, ?)",
        ("sess-1", "tool", STEER_FRAME, 1003.0),
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr("api.models._active_state_db_path", lambda: db_path)

    msgs = get_state_db_session_messages("sess-1")
    assert len(msgs) == 4

    # 1. Normal user message
    assert msgs[0]["content"] == "initial prompt"

    # 2. Typed steer row -> unwrapped clean content, original saved in api_content
    assert msgs[1]["content"] == "use the staging bucket this time"
    assert msgs[1]["display_kind"] == "steer"
    assert msgs[1]["api_content"] == STEER_FRAME

    # 3. Untyped user row -> preserved byte-for-byte
    assert msgs[2]["content"] == STEER_FRAME

    # 4. Tool row -> preserved byte-for-byte
    assert msgs[3]["content"] == STEER_FRAME

    # Check visible keys before timestamp
    keys = get_state_db_session_message_keys_before_timestamp("sess-1", 1001.5)
    assert keys is not None
    assert len(keys) == 2
    # The key for steer row must reflect the clean text, not the transport wrapper
    assert keys[1][1] == "use the staging bucket this time"


def test_state_db_regeneration_tail_snapshot_unwraps_steer(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY,
            session_id TEXT,
            role TEXT,
            content TEXT,
            timestamp REAL,
            tool_calls TEXT,
            display_kind TEXT,
            api_content TEXT
        )
        """
    )
    # Message before floor (ts=1000.0)
    conn.execute(
        "INSERT INTO messages (session_id, role, content, timestamp) VALUES (?, ?, ?, ?)",
        ("sess-regen", "user", "initial prompt", 1000.0),
    )
    # Steer row before floor (ts=1001.0 < floor 1002.0)
    conn.execute(
        "INSERT INTO messages (session_id, role, content, timestamp, display_kind) VALUES (?, ?, ?, ?, ?)",
        ("sess-regen", "user", STEER_FRAME, 1001.0, "steer"),
    )
    # Normal assistant reply at floor (ts=1002.0 >= floor 1002.0)
    conn.execute(
        "INSERT INTO messages (session_id, role, content, timestamp) VALUES (?, ?, ?, ?)",
        ("sess-regen", "assistant", "acknowledged staging", 1002.0),
    )
    # Steer row after floor (ts=1003.0 >= floor 1002.0)
    conn.execute(
        "INSERT INTO messages (session_id, role, content, timestamp, display_kind) VALUES (?, ?, ?, ?, ?)",
        ("sess-regen", "user", STEER_FRAME_2, 1003.0, "steer"),
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr("api.models._active_state_db_path", lambda: db_path)

    snapshot = get_state_db_regeneration_tail_snapshot("sess-regen", 1002.0)
    assert snapshot is not None

    # Assert prefix has 2 items (< 1002.0)
    assert snapshot["prefix"]["count"] == 2
    assert len(snapshot["prefix_keys"]) == 2

    # prefix_keys[1] must use clean unwrapped text
    assert snapshot["prefix_keys"][1][0] == "user"
    assert snapshot["prefix_keys"][1][1] == "use the staging bucket this time"

    # tail has 2 items (>= 1002.0)
    assert len(snapshot["tail"]) == 2
    assert len(snapshot["tail_keys"]) == 2

    # tail[1] is the steer message after floor
    steer_tail = snapshot["tail"][1]
    assert steer_tail["role"] == "user"
    assert steer_tail["content"] == "now deploy to prod"
    assert steer_tail["display_kind"] == "steer"
    assert steer_tail["api_content"] == STEER_FRAME_2

    # tail_keys[1] must use clean text
    assert snapshot["tail_keys"][1][0] == "user"
    assert snapshot["tail_keys"][1][1] == "now deploy to prod"


def test_legacy_sidecar_steer_collapses_with_state_db_on_reload_and_next_send(tmp_path, monkeypatch):
    """A legacy pre-#7600 sidecar holding raw OOB steer frame collapses with state.db steer row to 1 row on reload and next-send."""
    db_path = tmp_path / "state.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY,
            session_id TEXT,
            role TEXT,
            content TEXT,
            timestamp REAL,
            tool_calls TEXT,
            display_kind TEXT,
            api_content TEXT
        )
        """
    )
    # Insert normal user prompt
    conn.execute(
        "INSERT INTO messages (session_id, role, content, timestamp) VALUES (?, ?, ?, ?)",
        ("sess-legacy", "user", "initial prompt", 1000.0),
    )
    # Insert steer row in state.db
    conn.execute(
        "INSERT INTO messages (session_id, role, content, timestamp, display_kind) VALUES (?, ?, ?, ?, ?)",
        ("sess-legacy", "user", STEER_FRAME, 1001.0, "steer"),
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr("api.models._active_state_db_path", lambda: db_path)

    # Legacy sidecar with raw STEER_FRAME in both messages and context_messages
    raw_steer_row = {
        "role": "user",
        "display_kind": "steer",
        "content": STEER_FRAME,
        "timestamp": 1001.0,
    }
    legacy_session = Session(
        session_id="sess-legacy",
        messages=[
            {"role": "user", "content": "initial prompt", "timestamp": 1000.0},
            dict(raw_steer_row),
        ],
        context_messages=[
            {"role": "user", "content": "initial prompt", "timestamp": 1000.0},
            dict(raw_steer_row),
        ],
    )

    # 1. Reload (reconcile display messages)
    reconciled_display = reconciled_state_db_messages_for_session(legacy_session, prefer_context=False)
    # Must collapse to exactly 2 rows (initial prompt + 1 steer row), NOT duplicate the steer!
    assert len(reconciled_display) == 2
    assert reconciled_display[1]["role"] == "user"
    assert reconciled_display[1]["display_kind"] == "steer"
    assert reconciled_display[1]["content"] == "use the staging bucket this time"
    assert reconciled_display[1]["api_content"] == STEER_FRAME

    # 2. Next-send (reconcile context messages for provider)
    reconciled_context = reconciled_state_db_messages_for_session(legacy_session, prefer_context=True)
    # Must collapse to exactly 2 rows as well
    assert len(reconciled_context) == 2
    assert reconciled_context[1]["role"] == "user"
    assert reconciled_context[1]["display_kind"] == "steer"
    assert reconciled_context[1]["content"] == "use the staging bucket this time"
    assert reconciled_context[1]["api_content"] == STEER_FRAME

    # 3. Verify via Session.load from JSON file
    sess_file = tmp_path / "sess-legacy.json"
    sidecar_data = {
        "session_id": "sess-legacy",
        "messages": [
            {"role": "user", "content": "initial prompt", "timestamp": 1000.0},
            dict(raw_steer_row),
        ],
        "context_messages": [
            {"role": "user", "content": "initial prompt", "timestamp": 1000.0},
            dict(raw_steer_row),
        ],
    }
    sess_file.write_text(json.dumps(sidecar_data), encoding="utf-8")
    monkeypatch.setattr("api.models.SESSION_DIR", tmp_path)
    loaded_session = Session.load("sess-legacy")
    assert loaded_session is not None
    assert loaded_session.messages[1]["content"] == "use the staging bucket this time"
    assert loaded_session.messages[1]["api_content"] == STEER_FRAME
    assert loaded_session.context_messages[1]["content"] == "use the staging bucket this time"
    assert loaded_session.context_messages[1]["api_content"] == STEER_FRAME

    reloaded_display = reconciled_state_db_messages_for_session(loaded_session, prefer_context=False)
    assert len(reloaded_display) == 2
    assert reloaded_display[1]["content"] == "use the staging bucket this time"
    assert reloaded_display[1]["api_content"] == STEER_FRAME


# ---------------------------------------------------------------------------
# Shared fixtures for the re-gate asks (#7910).
# ---------------------------------------------------------------------------

CLEAN_STEER = "use the staging bucket this time"
SENTINEL = "\x00json:"


def _steer_list():
    """Fresh single-text-part list holding the raw frame (never share one)."""
    return [{"type": "text", "text": STEER_FRAME}]


def _steer_list_clean():
    return [{"type": "text", "text": CLEAN_STEER}]


def _write_state_db(db_path, rows):
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY,
            session_id TEXT,
            role TEXT,
            content TEXT,
            timestamp REAL,
            tool_calls TEXT,
            display_kind TEXT,
            api_content TEXT
        )
        """
    )
    for row in rows:
        cols = ["session_id", "role", "content", "timestamp"]
        vals = [row["session_id"], row["role"], row["content"], row["timestamp"]]
        for extra in ("display_kind", "api_content", "tool_calls"):
            if extra in row:
                cols.append(extra)
                vals.append(row[extra])
        conn.execute(
            f"INSERT INTO messages ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
            vals,
        )
    conn.commit()
    conn.close()


def _steer_rows(messages):
    """Rows carrying the steer turn, in either of its two persisted shapes."""
    return [
        m for m in messages
        if isinstance(m, dict) and m.get("content") in (CLEAN_STEER, STEER_FRAME)
    ]


def _prompt_and_steer(steer_factory):
    return [
        {"role": "user", "content": "initial prompt", "timestamp": 1000.0},
        steer_factory(),
    ]


# ---------------------------------------------------------------------------
# CORE 1 -- list-based legacy steers duplicate on reload and next-send.
# ---------------------------------------------------------------------------


def test_project_state_db_message_normalizes_list_based_steer():
    """The SQLite projection unwraps the same list shape the sidecar already does."""
    row = {
        "role": "user",
        "content": SENTINEL + json.dumps(_steer_list()),
        "timestamp": 1001.0,
        "id": 7,
        "display_kind": "steer",
    }
    msg = _project_state_db_message(
        row,
        available={"id", "display_kind", "api_content"},
        id_col=True,
        optional=("display_kind", "api_content"),
    )
    assert msg["content"] == _steer_list_clean()
    assert msg["api_content"] == STEER_FRAME


def test_legacy_sidecar_list_steer_collapses_with_state_db(tmp_path, monkeypatch):
    """A cold sidecar holding a list-based steer resolves to 1 row, as master does."""
    db_path = tmp_path / "state.db"
    _write_state_db(db_path, [
        {"session_id": "sess-list", "role": "user", "content": "initial prompt", "timestamp": 1000.0},
        {
            "session_id": "sess-list",
            "role": "user",
            "content": SENTINEL + json.dumps(_steer_list()),
            "timestamp": 1001.0,
            "display_kind": "steer",
        },
    ])
    monkeypatch.setattr("api.models._active_state_db_path", lambda: db_path)

    session = Session(
        session_id="sess-list",
        messages=_prompt_and_steer(lambda: {
            "role": "user", "content": _steer_list(), "timestamp": 1001.0,
            "display_kind": "steer",
        }),
        context_messages=_prompt_and_steer(lambda: {
            "role": "user", "content": _steer_list(), "timestamp": 1001.0,
            "display_kind": "steer",
        }),
    )

    for prefer_context in (False, True):
        rows = reconciled_state_db_messages_for_session(session, prefer_context=prefer_context)
        steer = [
            m for m in rows
            if isinstance(m.get("content"), list) and m["content"] == _steer_list_clean()
        ]
        assert len(rows) == 2, (prefer_context, [m.get("content") for m in rows])
        assert len(steer) == 1, (prefer_context, [m.get("content") for m in rows])
        assert steer[0]["api_content"] == STEER_FRAME


def test_prefix_key_projections_normalize_list_based_steer(tmp_path, monkeypatch):
    """Both prefix-key projections must apply the sidecar normalization, not a string-only copy."""
    db_path = tmp_path / "state.db"
    _write_state_db(db_path, [
        {"session_id": "sess-pfx", "role": "user", "content": "initial prompt", "timestamp": 1000.0},
        {
            "session_id": "sess-pfx",
            "role": "user",
            "content": SENTINEL + json.dumps(_steer_list()),
            "timestamp": 1001.0,
            "display_kind": "steer",
        },
    ])
    monkeypatch.setattr("api.models._active_state_db_path", lambda: db_path)

    sidecar_steer = {
        "role": "user", "content": _steer_list(), "timestamp": 1001.0,
        "display_kind": "steer",
    }
    _normalize_sidecar_steer_messages([sidecar_steer])
    expected = _session_message_visible_key(sidecar_steer, normalize_workspace_prefix=True)

    keys = get_state_db_session_message_keys_before_timestamp("sess-pfx", 1002.0)
    assert keys is not None
    assert keys[1] == expected

    snapshot = get_state_db_regeneration_tail_snapshot("sess-pfx", 1002.0)
    assert snapshot is not None
    assert snapshot["prefix_keys"][1] == expected


# ---------------------------------------------------------------------------
# CORE 2 -- a legacy raw-frame compression anchor must still resolve.
# ---------------------------------------------------------------------------


def test_legacy_raw_frame_anchor_keeps_later_state_db_history(tmp_path, monkeypatch):
    """A stale anchor made reconciliation return context-only and drop later SQLite rows."""
    db_path = tmp_path / "state.db"
    _write_state_db(db_path, [
        {"session_id": "sess-anchor", "role": "user", "content": "initial prompt", "timestamp": 1000.0},
        {
            "session_id": "sess-anchor", "role": "user", "content": STEER_FRAME,
            "timestamp": 1001.0, "display_kind": "steer",
        },
        {
            "session_id": "sess-anchor", "role": "user",
            "content": "gateway prompt after compaction", "timestamp": 1003.0,
        },
        {
            "session_id": "sess-anchor", "role": "assistant",
            "content": "answer after compaction", "timestamp": 1004.0,
        },
    ])
    monkeypatch.setattr("api.models._active_state_db_path", lambda: db_path)

    # The anchor is persisted by the real builder while the row still carried
    # the raw transport frame (pre-#7600 sidecar / pre-scrub session).
    anchor = _compression_anchor_message_key(
        {"role": "user", "content": STEER_FRAME, "timestamp": 1001.0}
    )
    assert anchor is not None
    assert anchor["role"] == "user"

    compressed_context = [
        {"role": "user", "content": "initial prompt", "timestamp": 1000.0},
        {"role": "user", "content": STEER_FRAME, "timestamp": 1001.0, "display_kind": "steer"},
        {
            "role": "user", "content": "[context compaction summary of the earlier turns]",
            "timestamp": 1002.0,
        },
    ]
    session = Session(
        session_id="sess-anchor",
        messages=[dict(m) for m in compressed_context],
        context_messages=[dict(m) for m in compressed_context],
        compression_anchor_message_key=anchor,
    )

    # The anchor must resolve against the projected steer row (clean content
    # plus the raw frame preserved in api_content).
    assert _state_db_anchor_index(get_state_db_session_messages("sess-anchor"), anchor) is not None

    ctx = reconciled_state_db_messages_for_session(session, prefer_context=True)
    texts = [m.get("content") for m in ctx]
    assert "gateway prompt after compaction" in texts, texts
    assert "answer after compaction" in texts, texts


# ---------------------------------------------------------------------------
# MUST-FIX -- all six sidecar x state.db steer pairings collapse to one row.
# ---------------------------------------------------------------------------

SIDECAR_STEER_SHAPES = {
    # What import_cli_session persists from master's projection: no display_kind.
    "untyped_raw": lambda: {"role": "user", "content": STEER_FRAME, "timestamp": 1001.0},
    # Legacy pre-#7600 sidecar: typed, still holding the transport frame.
    "typed_raw": lambda: {
        "role": "user", "content": STEER_FRAME, "timestamp": 1001.0,
        "display_kind": "steer",
    },
    # Post-#7600 sidecar: typed and already normalized.
    "typed_clean": lambda: {
        "role": "user", "content": CLEAN_STEER, "timestamp": 1001.0,
        "display_kind": "steer", "api_content": STEER_FRAME,
    },
}

STATE_STEER_SHAPES = {
    "typed": {"role": "user", "content": STEER_FRAME, "timestamp": 1001.0, "display_kind": "steer"},
    "untyped": {"role": "user", "content": STEER_FRAME, "timestamp": 1001.0},
}


@pytest.mark.parametrize(
    "sidecar_shape,state_shape",
    list(itertools.product(SIDECAR_STEER_SHAPES, STATE_STEER_SHAPES)),
)
def test_six_sidecar_state_pairings_collapse_to_one_steer(
    tmp_path, monkeypatch, sidecar_shape, state_shape
):
    sid = f"sess-{sidecar_shape}-{state_shape}"
    db_path = tmp_path / "state.db"
    _write_state_db(db_path, [
        {"session_id": sid, "role": "user", "content": "initial prompt", "timestamp": 1000.0},
        {"session_id": sid, **STATE_STEER_SHAPES[state_shape]},
    ])
    monkeypatch.setattr("api.models._active_state_db_path", lambda: db_path)

    session = Session(
        session_id=sid,
        messages=_prompt_and_steer(SIDECAR_STEER_SHAPES[sidecar_shape]),
        context_messages=_prompt_and_steer(SIDECAR_STEER_SHAPES[sidecar_shape]),
    )

    for prefer_context in (False, True):
        rows = reconciled_state_db_messages_for_session(session, prefer_context=prefer_context)
        shapes = (sidecar_shape, state_shape, prefer_context, [m.get("content") for m in rows])
        assert len(rows) == 2, shapes
        assert len(_steer_rows(rows)) == 1, shapes

    # Regeneration prefix check (api/routes.py:10299-10309) must keep passing.
    state_keys = get_state_db_session_message_keys_before_timestamp(sid, 1002.0)
    assert state_keys is not None
    sidecar_keys = [
        _session_message_visible_key(msg)
        for msg, ts in zip(session.messages, (1000.0, 1001.0), strict=True)
        if ts < 1002.0
    ]
    assert state_keys == sidecar_keys, (sidecar_shape, state_shape, state_keys, sidecar_keys)


# ---------------------------------------------------------------------------
# SHOULD-FIX -- the unwrap must stay linear, not backtrack quadratically.
# ---------------------------------------------------------------------------


def test_unwrap_steer_frame_does_not_backtrack_quadratically():
    """Count passes but the close marker is not at the end: the anchored regex
    walked every split of a 32k whitespace run (seconds); finditer is linear."""
    content = (
        "[OUT-OF-BAND USER MESSAGE]\n"
        "x" + " " * 32000 +
        "\n[/OUT-OF-BAND USER MESSAGE]\ntrailing"
    )
    lower = content.lower()
    assert lower.count("[out-of-band user message") == 1
    assert lower.count("[/out-of-band user message]") == 1

    started = time.perf_counter()
    result = _unwrap_steer_frame_text(content)
    elapsed = time.perf_counter() - started

    # Close marker is not at the end, so the row is preserved byte-for-byte.
    assert result == content
    assert elapsed < 0.4, f"unwrap took {elapsed:.3f}s; expected a linear finditer unwrap"

