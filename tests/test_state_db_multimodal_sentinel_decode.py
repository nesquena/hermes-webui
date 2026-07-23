"""Regression tests: Hermes Agent's structured-content sentinel must be decoded
at the WebUI's state.db read boundary.

Incident: WebUI sent a native multimodal user message (text + image_url data
URL). Hermes Agent persisted the content list to state.db via its
``\\x00json:`` sentinel encoding (``SessionDB._encode_content``). After context
compaction rewrote the session, the WebUI's direct state.db readers returned
that row verbatim — so the visible transcript rendered the raw internal JSON,
including a ~112KB base64 screenshot, as a user bubble. Because the raw string
also keyed differently from the clean sidecar row, sidecar/state.db dedup
failed and the leaked row was appended as a duplicate turn.
"""

from __future__ import annotations

import json

import api.models as models


SENTINEL = models._STATE_DB_CONTENT_JSON_PREFIX


def _encoded_multimodal(text: str) -> str:
    parts = [
        {"type": "text", "text": text},
        {
            "type": "image_url",
            "image_url": {"url": "data:image/png;base64," + ("A" * 4096)},
        },
    ]
    return SENTINEL + json.dumps(parts)


def test_sentinel_matches_hermes_agent_constant():
    # The constant is mirrored (not imported); it must stay byte-identical to
    # SessionDB._CONTENT_JSON_PREFIX in hermes-agent's hermes_state.py.
    assert SENTINEL == "\x00json:"


def test_plain_string_content_is_untouched():
    for value in ("hello", "", "json:[1,2]", "[{\"type\": \"text\"}]", None, 3, 2.5):
        assert models._decode_state_db_content(value) == value


def test_multimodal_content_flattens_to_text_only():
    decoded = models._decode_state_db_content(_encoded_multimodal("what is this?"))
    assert decoded == "what is this?"
    assert "base64" not in decoded


def test_multiple_text_parts_join_with_newline():
    parts = [
        {"type": "text", "text": "first"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
        {"type": "text", "text": "second"},
    ]
    decoded = models._decode_state_db_content(SENTINEL + json.dumps(parts))
    assert decoded == "first\nsecond"


def test_corrupt_sentinel_payload_returns_raw_value():
    raw = SENTINEL + "{not valid json"
    assert models._decode_state_db_content(raw) == raw


def test_non_list_json_payload_passes_through_decoded():
    raw = SENTINEL + json.dumps({"unexpected": "dict"})
    assert models._decode_state_db_content(raw) == {"unexpected": "dict"}


def test_decoded_row_keys_identically_to_clean_sidecar_row():
    # The core dedup property: a state.db multimodal row and the clean sidecar
    # row for the same turn must produce the same visible-identity key, so the
    # sidecar/state.db merge collapses them instead of appending a duplicate.
    text = "why does this keep happening again?"
    state_db_row = {
        "role": "user",
        "content": models._decode_state_db_content(_encoded_multimodal(text)),
        "tool_calls": None,
    }
    sidecar_row = {"role": "user", "content": text, "tool_calls": None}
    assert models._session_message_visible_key(
        state_db_row
    ) == models._session_message_visible_key(sidecar_row)


def test_get_state_db_session_messages_decodes_content(tmp_path, monkeypatch):
    # End-to-end through the real reader: a sentinel-encoded row in a real
    # sqlite file comes back flattened, plain rows come back untouched.
    import sqlite3

    db = tmp_path / "state.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE messages ("
        " id INTEGER PRIMARY KEY, session_id TEXT, role TEXT,"
        " content TEXT, timestamp REAL, tool_calls TEXT)"
    )
    conn.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY)")
    conn.execute("INSERT INTO sessions (id) VALUES ('sid1')")
    conn.execute(
        "INSERT INTO messages (session_id, role, content, timestamp) VALUES (?,?,?,?)",
        ("sid1", "user", _encoded_multimodal("attached a screenshot"), 100.0),
    )
    conn.execute(
        "INSERT INTO messages (session_id, role, content, timestamp) VALUES (?,?,?,?)",
        ("sid1", "assistant", "plain answer", 101.0),
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(models, "_active_state_db_path", lambda: db)

    msgs = models.get_state_db_session_messages("sid1", stitch_continuations=False)
    assert [m["content"] for m in msgs] == ["attached a screenshot", "plain answer"]
    assert all("base64" not in str(m["content"]) for m in msgs)
