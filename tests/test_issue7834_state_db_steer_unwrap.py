"""Regression test for issue #7834: state.db-owned transcripts unwrapping [OUT-OF-BAND USER MESSAGE] steer frames.

A steer row with display_kind='steer' stored in state.db must have its single complete
transport frame unwrapped so the UI and session message queries return the clean authored
user text, while preserving byte-for-byte fidelity for non-steer rows, tool rows, and malformed frames.

Also asserts that get_state_db_session_message_keys_before_timestamp and
get_state_db_regeneration_tail_snapshot project prefix_keys and tail_keys using the clean text,
while projected tail rows retain raw api_content.
"""

import json
import sqlite3
from api.models import (
    Session,
    reconciled_state_db_messages_for_session,
    _unwrap_steer_row_oob_marker,
    get_state_db_session_messages,
    get_state_db_session_message_keys_before_timestamp,
    get_state_db_regeneration_tail_snapshot,
)


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


def test_unwrap_steer_row_oob_marker_clean_frame():
    assert _unwrap_steer_row_oob_marker(STEER_FRAME) == "use the staging bucket this time"
    assert _unwrap_steer_row_oob_marker(STEER_FRAME_2) == "now deploy to prod"


def test_unwrap_steer_row_oob_marker_byte_for_byte_guards():
    # Multiple frames
    multi = f"{STEER_FRAME}\n{STEER_FRAME}"
    assert _unwrap_steer_row_oob_marker(multi) == multi

    # Unterminated frame
    unterminated = "[OUT-OF-BAND USER MESSAGE] hello"
    assert _unwrap_steer_row_oob_marker(unterminated) == unterminated

    # Extra text surrounding the frame
    surrounded = f"before text\n{STEER_FRAME}\nafter text"
    assert _unwrap_steer_row_oob_marker(surrounded) == surrounded

    # Non-string passes through
    assert _unwrap_steer_row_oob_marker(123) == 123


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

