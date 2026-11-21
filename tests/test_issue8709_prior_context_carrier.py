"""#8709 — a compaction handoff folded into a retained row must not render as chat.

The Agent compressor folds the summary into the first retained tail row when
role alternation demands it. That row keeps the authentic tail text, is
prefixed with ``[PRIOR CONTEXT ...]``, ends with the compaction summary, and
carries ``_compressed_summary=1``. Before this fix the WebUI only knew the
``[context compaction`` prefix and the state.db reader did not select the flag,
so the whole handoff blob (tens of KB) rendered as a normal assistant message.

The rule owner is ``api/compression_anchor.py``; ``api/models.py`` and
``static/ui.js`` mirror it instead of keeping their own prefix copies.
"""

from __future__ import annotations

import inspect
import json
import sqlite3
from pathlib import Path

import pytest

import api.models as models
from api.compression_anchor import (
    compaction_handoff_remainder,
    is_compaction_handoff_carrier,
    is_context_compression_marker,
    is_prompt_compaction_marker,
    project_compaction_carrier_for_display,
    project_compaction_carriers_for_display,
)

ROOT = Path(__file__).resolve().parents[1]
UI_JS = ROOT / "static" / "ui.js"

HEADER = "[PRIOR CONTEXT — for reference only; not a new message]"
DELIMITER = "[END OF PRIOR CONTEXT — COMPACTION SUMMARY BELOW]"
END_MARKER = (
    "--- END OF CONTEXT SUMMARY — respond to the message below, "
    "not the summary above ---"
)
RETAINED_STEP = "Let me check the failing assertion before editing the parser."
SUMMARY = (
    "[CONTEXT COMPACTION — REFERENCE ONLY] Earlier turns were compacted into the "
    "summary below.\n## Goal\nShip the carrier fix.\n## Active State\n"
    "- worktree wt-516-8709\n" + ("- padding detail line\n" * 40)
)
CARRIER_CONTENT = (
    HEADER + "\n" + RETAINED_STEP + "\n\n" + DELIMITER + "\n\n" + SUMMARY + "\n\n" + END_MARKER
)
CARD_CONTENT = (
    "[context compaction — reference only] earlier turns were compacted.\n"
    "## Goal\nolder summary\n" + END_MARKER
)
NORMAL_REPLY = "Done — parser fixed."
NORMAL_TOOL_CALLS = (
    '[{"id": "call_2", "type": "function", '
    '"function": {"name": "apply_patch", "arguments": "{}"}}]'
)
CARRIER_TOOL_CALLS = (
    '[{"id": "call_1", "type": "function", '
    '"function": {"name": "read_file", "arguments": "{}"}}]'
)

SCHEMA = """
CREATE TABLE messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT,
    tool_call_id TEXT,
    tool_calls TEXT,
    tool_name TEXT,
    timestamp REAL NOT NULL,
    reasoning TEXT,
    _compressed_summary INTEGER NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1,
    api_content TEXT
)
"""


def _make_state_db(path: Path, *, with_flag_column: bool = True) -> None:
    conn = sqlite3.connect(str(path))
    if with_flag_column:
        conn.execute(SCHEMA)
        insert = (
            "INSERT INTO messages (session_id, role, content, timestamp,"
            " _compressed_summary, tool_calls) VALUES ('sid', ?, ?, ?, ?, ?)"
        )
        rows = [
            ("user", "please fix the parser", 1.0, 0, None),
            ("assistant", CARD_CONTENT, 2.0, 0, None),
            ("user", "carry on", 3.0, 0, None),
            ("assistant", CARRIER_CONTENT, 4.0, 1, CARRIER_TOOL_CALLS),
            ("tool", "file body", 5.0, 0, None),
            ("assistant", NORMAL_REPLY, 6.0, 0, NORMAL_TOOL_CALLS),
        ]
    else:  # legacy schema predating the Agent flag column
        conn.execute(
            """
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT,
                tool_calls TEXT,
                timestamp REAL NOT NULL
            )
            """
        )
        insert = (
            "INSERT INTO messages (session_id, role, content, timestamp, tool_calls)"
            " VALUES ('sid', ?, ?, ?, ?)"
        )
        rows = [
            ("assistant", CARRIER_CONTENT, 4.0, 0, CARRIER_TOOL_CALLS),
            ("assistant", NORMAL_REPLY, 6.0, 0, NORMAL_TOOL_CALLS),
        ]
    for role, content, ts, flag, tool_calls in rows:
        conn.execute(insert, (role, content, ts, flag, tool_calls) if with_flag_column
                     else (role, content, ts, tool_calls))
    conn.commit()
    conn.close()


@pytest.fixture()
def state_db(tmp_path, monkeypatch):
    db = tmp_path / "state.db"
    _make_state_db(db)
    monkeypatch.setattr(models, "_active_state_db_path", lambda: db)
    return db


def _carrier_row(messages):
    return [
        m for m in messages
        if isinstance(m.get("content"), str) and m["content"].startswith("[PRIOR CONTEXT")
    ]


def test_merged_handoff_carrier_is_not_rendered_as_chat(state_db):
    messages = models.get_state_db_session_messages("sid")

    # Raw/model-facing history keeps the complete handoff (the Agent replays it).
    carriers = _carrier_row(messages)
    assert len(carriers) == 1
    carrier = carriers[0]
    assert DELIMITER in carrier["content"] and SUMMARY.splitlines()[0] in carrier["content"]

    # The state.db reader now selects the Agent flag, so the marker predicate
    # receives the signal instead of relying on the content prefix alone.
    assert carrier["_compressed_summary"] is True
    assert is_context_compression_marker(carrier) is True
    assert is_compaction_handoff_carrier(carrier) is True

    # Display projection (what GET /api/session serves) drops the handoff.
    displayed = project_compaction_carriers_for_display(messages)
    assert _carrier_row(displayed) == []
    shown = [m for m in displayed if m.get("tool_calls") and m.get("content") == RETAINED_STEP]
    assert len(shown) == 1
    projected = shown[0]
    assert projected["content"] == RETAINED_STEP
    assert "PRIOR CONTEXT" not in projected["content"]
    assert "END OF PRIOR CONTEXT" not in projected["content"]
    assert SUMMARY.splitlines()[0] not in projected["content"]
    # The retained row's real tool-call step and linkage survive.
    assert projected["tool_calls"] == json.loads(CARRIER_TOOL_CALLS)
    assert projected["role"] == "assistant"
    assert projected["timestamp"] == 4.0
    # Original list is never mutated.
    assert _carrier_row(messages), "display projection must copy, not mutate"


def test_pure_handoff_carrier_projects_to_empty_content():
    pure = {"role": "assistant", "content": HEADER + "\n\n" + SUMMARY + "\n\n" + END_MARKER,
            "_compressed_summary": True}
    assert compaction_handoff_remainder(pure) == ""
    assert project_compaction_carrier_for_display(pure)["content"] == ""


def test_context_compaction_card_behaviour_preserved(state_db):
    messages = models.get_state_db_session_messages("sid")
    card = next(m for m in messages if str(m.get("content", "")).startswith("[context compaction"))

    assert is_context_compression_marker(card) is True
    assert is_prompt_compaction_marker(card) is True
    # The card row keeps its summary text: the renderer needs it for the card.
    assert project_compaction_carrier_for_display(card) is card
    assert project_compaction_carrier_for_display(card)["content"] == CARD_CONTENT


def test_normal_assistant_tool_call_row_still_renders(state_db):
    messages = models.get_state_db_session_messages("sid")
    normal = next(m for m in messages if m.get("content") == NORMAL_REPLY)

    assert is_context_compression_marker(normal) is False
    assert is_compaction_handoff_carrier(normal) is False
    projected = project_compaction_carrier_for_display(normal)
    assert projected is normal
    assert projected["content"] == NORMAL_REPLY
    assert projected["tool_calls"] == json.loads(NORMAL_TOOL_CALLS)


def test_legacy_schema_without_flag_column_still_strips_carrier(tmp_path, monkeypatch):
    db = tmp_path / "state.db"
    _make_state_db(db, with_flag_column=False)
    monkeypatch.setattr(models, "_active_state_db_path", lambda: db)

    messages = models.get_state_db_session_messages("sid")
    assert all("_compressed_summary" not in m for m in messages)
    displayed = project_compaction_carriers_for_display(messages)
    carrier = next(m for m in displayed if m.get("tool_calls") and m["content"] == RETAINED_STEP)
    assert carrier["timestamp"] == 4.0


def test_python_marker_paths_agree_on_the_same_rule():
    card = {"role": "assistant", "content": CARD_CONTENT}
    carrier = {"role": "assistant", "content": CARRIER_CONTENT, "_compressed_summary": True}
    arc = {"role": "user", "content": "[session arc summary] earlier work"}

    # api/models.py delegates to the owner instead of keeping a second copy.
    assert models._context_messages_include_compression_marker([card]) is True
    assert models._context_messages_include_compression_marker([carrier]) is False
    assert models._context_messages_include_compression_marker([arc]) is False
    for message in (card, carrier, arc):
        assert models._context_messages_include_compression_marker([message]) is is_prompt_compaction_marker(message)


def test_browser_mirror_accepts_the_same_prefixes():
    source = UI_JS.read_text(encoding="utf-8")
    start = source.index("function _isContextCompactionText(")
    body = source[start:source.index("function _compactionSummarySegment(", start)]
    assert "context compaction" in body
    assert "prior context" in body


def test_session_get_display_path_applies_the_projection():
    from api import routes

    assert routes.project_compaction_carriers_for_display is project_compaction_carriers_for_display
    source = inspect.getsource(routes._handle_session_get)
    assert "project_compaction_carriers_for_display(_all_msgs)" in source
