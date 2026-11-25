"""#8748 (review 2026-11-23T13:16:43Z) — a folded carrier must survive settlement.

The Agent compressor folds the compaction summary into the first retained tail
row when role alternation demands it. That row keeps the authentic text, is
prefixed with ``[PRIOR CONTEXT ...]`` and reaches the summary behind the
delimiter, carrying ``_compressed_summary``. The broad
``is_context_compression_marker()`` matches that folded row, and
``_merge_display_messages_after_agent_result()`` drops markers from both the
previous display rows and the current-turn candidates, so the next turn's
settlement deletes the row from the saved transcript: a folded user carrier
takes the user's authentic text with it, and a folded assistant carrier takes
its ``tool_calls``, leaving an orphan ``tool`` row.

The merge must therefore keep folded rows (as stored, or rewritten to their
authentic text) and drop only standalone cards and carriers with no authentic
text. This module pins that behaviour for both merge branches (append-only and
compacted) and through ``Session.save`` / ``Session.load``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import api.models as models
from api.compression_anchor import (
    compaction_handoff_remainder,
    is_compaction_handoff_carrier,
    is_context_compression_marker,
    is_folded_compaction_carrier,
    is_standalone_compaction_card,
)
from api.models import Session
from api.streaming import _merge_display_messages_after_agent_result

HEADER = "[PRIOR CONTEXT — for reference only; not a new message]"
DELIMITER = "[END OF PRIOR CONTEXT — COMPACTION SUMMARY BELOW]"
END_MARKER = (
    "--- END OF CONTEXT SUMMARY — respond to the message below, "
    "not the summary above ---"
)
SUMMARY = (
    "[CONTEXT COMPACTION — REFERENCE ONLY] Earlier turns were compacted into the "
    "summary below.\n## Goal\nShip the carrier fix.\n## Active State\n"
    "- worktree wt-8748\n" + ("- padding detail line\n" * 30)
)
USER_AUTHENTIC = "Please keep the authored line in the saved transcript."
ASSISTANT_AUTHENTIC = "Reading the parser before editing it."
CARD_CONTENT = (
    "[context compaction — reference only] earlier turns were compacted.\n"
    "## Goal\nolder summary\n" + END_MARKER
)
CARRIER_TOOL_CALLS = [
    {
        "id": "call_1",
        "type": "function",
        "function": {"name": "read_file", "arguments": "{}"},
    }
]


def _folded(role, authentic, *, tool_calls=None, timestamp=3.0):
    message = {
        "role": role,
        "content": (
            HEADER + "\n" + authentic + "\n\n" + DELIMITER + "\n\n"
            + SUMMARY + "\n\n" + END_MARKER
        ),
        "_compressed_summary": True,
        "timestamp": timestamp,
    }
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    return message


def _stored_rows():
    """The durable display transcript before the next turn settles."""
    return [
        {"role": "user", "content": "first question", "timestamp": 1.0},
        {"role": "assistant", "content": "first answer", "timestamp": 2.0},
        _folded("user", USER_AUTHENTIC, timestamp=3.0),
        _folded(
            "assistant", ASSISTANT_AUTHENTIC,
            tool_calls=json.loads(json.dumps(CARRIER_TOOL_CALLS)),
            timestamp=4.0,
        ),
        {
            "role": "tool",
            "tool_call_id": "call_1",
            "content": "file body",
            "timestamp": 5.0,
        },
        {"role": "assistant", "content": "done", "timestamp": 6.0},
    ]


def _rows_containing(messages, text):
    return [
        m for m in messages
        if isinstance(m, dict) and text in str(m.get("content") or "")
    ]


def _tool_calls_of(message):
    tool_calls = message.get("tool_calls")
    if isinstance(tool_calls, str):
        return json.loads(tool_calls)
    return tool_calls or []


def _assert_folded_rows_survived(messages, label):
    user_rows = _rows_containing(messages, USER_AUTHENTIC)
    assert len(user_rows) == 1, (
        f"{label}: folded user carrier lost from the transcript "
        f"({len(user_rows)} rows carry its authentic text)"
    )
    assert user_rows[0]["role"] == "user"

    assistant_rows = _rows_containing(messages, ASSISTANT_AUTHENTIC)
    assert len(assistant_rows) == 1, (
        f"{label}: folded assistant carrier lost from the transcript "
        f"({len(assistant_rows)} rows carry its authentic text)"
    )
    assistant_row = assistant_rows[0]
    assert assistant_row["role"] == "assistant"
    calls = _tool_calls_of(assistant_row)
    assert [c.get("id") for c in calls] == ["call_1"], (
        f"{label}: folded assistant carrier lost its tool_calls"
    )

    tool_rows = [
        m for m in messages
        if isinstance(m, dict) and m.get("role") == "tool"
        and m.get("tool_call_id") == "call_1"
    ]
    assert len(tool_rows) == 1, (
        f"{label}: the tool result of a folded carrier became an orphan row"
    )


def _append_only_result(store):
    """Next turn in append-only mode: the stored context is still the prefix."""
    result = [dict(m) for m in store]
    result.append({"role": "user", "content": "next question", "timestamp": 7.0})
    result.append({"role": "assistant", "content": "next answer", "timestamp": 8.0})
    return result


def _compacted_result():
    """Next turn while the compressor fires: no shared prefix with the context."""
    return [
        {"role": "assistant", "content": CARD_CONTENT, "timestamp": 6.5},
        {"role": "user", "content": "next question", "timestamp": 7.0},
        {"role": "assistant", "content": "next answer", "timestamp": 8.0},
    ]


@pytest.fixture()
def session_store(tmp_path, monkeypatch):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    return session_dir


def test_folded_rows_are_markers_but_have_authentic_text():
    """The premise: the folded rows are markers *and* carry authentic text."""
    user_carrier = _folded("user", USER_AUTHENTIC)
    assistant_carrier = _folded("assistant", ASSISTANT_AUTHENTIC, tool_calls=CARRIER_TOOL_CALLS)

    for carrier, authentic in ((user_carrier, USER_AUTHENTIC), (assistant_carrier, ASSISTANT_AUTHENTIC)):
        assert is_context_compression_marker(carrier) is True
        assert is_compaction_handoff_carrier(carrier) is True
        assert compaction_handoff_remainder(carrier) == authentic


def test_settlement_keeps_folded_rows_append_only():
    store = _stored_rows()
    merged = _merge_display_messages_after_agent_result(
        [dict(m) for m in store],
        [dict(m) for m in store],
        _append_only_result(store),
        "next question",
    )

    _assert_folded_rows_survived(merged, "append-only merge")
    assert _rows_containing(merged, "next answer"), "the new turn was not appended"


def test_settlement_keeps_folded_rows_compacted():
    store = _stored_rows()
    merged = _merge_display_messages_after_agent_result(
        [dict(m) for m in store],
        [dict(m) for m in store],
        _compacted_result(),
        "next question",
    )

    _assert_folded_rows_survived(merged, "compacted merge")
    assert _rows_containing(merged, "next answer"), "the new turn was not appended"


def test_save_and_reload_keeps_folded_rows(session_store):
    store = _stored_rows()
    merged = _merge_display_messages_after_agent_result(
        [dict(m) for m in store],
        [dict(m) for m in store],
        _append_only_result(store),
        "next question",
    )

    session = Session(session_id="s8709fold", title="folded rows", messages=merged)
    session.save(skip_index=True)

    raw_path = Path(session_store) / "s8709fold.json"
    raw = json.loads(raw_path.read_text(encoding="utf-8"))
    _assert_folded_rows_survived(raw["messages"], "session file")

    loaded = Session.load("s8709fold")
    assert loaded is not None
    _assert_folded_rows_survived(loaded.messages, "reloaded session")


def test_standalone_cards_and_textless_carriers_are_still_dropped():
    """Control: the drop-filter must keep working for rows with nothing authored."""
    card = {"role": "assistant", "content": CARD_CONTENT, "timestamp": 2.0}
    pure_carrier = {
        "role": "assistant",
        "content": HEADER + "\n\n" + SUMMARY + "\n\n" + END_MARKER,
        "_compressed_summary": True,
        "timestamp": 4.0,
    }
    previous_display = [
        {"role": "user", "content": "first question", "timestamp": 1.0},
        card,
        pure_carrier,
    ]
    previous_context = [dict(m) for m in previous_display]

    for label, result in (
        (
            "append-only",
            [dict(m) for m in previous_context]
            + [
                {"role": "user", "content": "next question", "timestamp": 7.0},
                {"role": "assistant", "content": "next answer", "timestamp": 8.0},
            ],
        ),
        ("compacted", _compacted_result()),
    ):
        merged = _merge_display_messages_after_agent_result(
            [dict(m) for m in previous_display],
            [dict(m) for m in previous_context],
            result,
            "next question",
        )
        assert _rows_containing(merged, CARD_CONTENT) == [], f"{label}: standalone card leaked"
        assert _rows_containing(merged, "padding detail line") == [], (
            f"{label}: textless carrier leaked its synthetic summary"
        )
        assert _rows_containing(merged, "next answer"), f"{label}: the new turn was not appended"


# ── Structured content: the same folded rows stored as content blocks ─────────
# The Agent also stores a row's content as a list of blocks. The display
# projector preserves the authentic text of ``text``/``input_text``/``output_text``
# blocks (and of type-less ``text``/``content`` blocks), so the drop predicate has
# to reach those forms too: an empty result from a string-only parser is not proof
# that a structured row is synthetic (review 2026-11-25T06:27:58Z).

STRUCTURED_PART_TYPES = ("text", "input_text", "output_text")


def _carrier_text(authentic):
    return (
        HEADER + "\n" + authentic + "\n\n" + DELIMITER + "\n\n"
        + SUMMARY + "\n\n" + END_MARKER
    )


def _structured_folded(role, authentic, part_type, *, tool_calls=None, timestamp=3.0):
    message = {
        "role": role,
        "content": [{"type": part_type, "text": _carrier_text(authentic)}],
        "_compressed_summary": True,
        "timestamp": timestamp,
    }
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    return message


def _structured_stored_rows(part_type):
    """The durable display transcript, with both carriers stored as block lists."""
    return [
        {"role": "user", "content": "first question", "timestamp": 1.0},
        {"role": "assistant", "content": "first answer", "timestamp": 2.0},
        _structured_folded("user", USER_AUTHENTIC, part_type, timestamp=3.0),
        _structured_folded(
            "assistant", ASSISTANT_AUTHENTIC, part_type,
            tool_calls=json.loads(json.dumps(CARRIER_TOOL_CALLS)),
            timestamp=4.0,
        ),
        {
            "role": "tool",
            "tool_call_id": "call_1",
            "content": "file body",
            "timestamp": 5.0,
        },
        {"role": "assistant", "content": "done", "timestamp": 6.0},
    ]


@pytest.mark.parametrize("part_type", STRUCTURED_PART_TYPES)
def test_structured_folded_rows_are_preserved_by_the_drop_predicate(part_type):
    """Premise: a structured folded carrier is a marker but must not be a card."""
    for role, authentic in (("user", USER_AUTHENTIC), ("assistant", ASSISTANT_AUTHENTIC)):
        carrier = _structured_folded(role, authentic, part_type)
        assert is_context_compression_marker(carrier) is True
        assert is_folded_compaction_carrier(carrier) is True
        assert is_standalone_compaction_card(carrier) is False, (
            f"{part_type}/{role}: a structured folded carrier with authentic text "
            "was classified as a standalone card"
        )


@pytest.mark.parametrize("part_type", STRUCTURED_PART_TYPES)
@pytest.mark.parametrize("branch", ["append-only", "compacted"])
def test_structured_settlement_keeps_folded_rows(part_type, branch):
    store = _structured_stored_rows(part_type)
    result = _append_only_result(store) if branch == "append-only" else _compacted_result()
    merged = _merge_display_messages_after_agent_result(
        [dict(m) for m in store],
        [dict(m) for m in store],
        result,
        "next question",
    )

    _assert_folded_rows_survived(merged, f"{part_type}/{branch} merge")
    assert _rows_containing(merged, "next answer"), (
        f"{part_type}/{branch}: the new turn was not appended"
    )


@pytest.mark.parametrize("part_type", STRUCTURED_PART_TYPES)
def test_structured_save_and_reload_keeps_folded_rows(part_type, session_store):
    store = _structured_stored_rows(part_type)
    merged = _merge_display_messages_after_agent_result(
        [dict(m) for m in store],
        [dict(m) for m in store],
        _append_only_result(store),
        "next question",
    )

    session = Session(session_id="s8709foldblocks", title="folded blocks", messages=merged)
    session.save(skip_index=True)

    raw_path = Path(session_store) / "s8709foldblocks.json"
    raw = json.loads(raw_path.read_text(encoding="utf-8"))
    _assert_folded_rows_survived(raw["messages"], f"{part_type} session file")

    loaded = Session.load("s8709foldblocks")
    assert loaded is not None
    _assert_folded_rows_survived(loaded.messages, f"{part_type} reloaded session")


@pytest.mark.parametrize("part_type", STRUCTURED_PART_TYPES)
def test_structured_standalone_cards_and_textless_carriers_are_still_dropped(part_type):
    """Control: structured rows with nothing authored stay out of the transcript."""
    card = {
        "role": "assistant",
        "content": [{"type": part_type, "text": CARD_CONTENT}],
        "timestamp": 2.0,
    }
    pure_carrier = {
        "role": "assistant",
        "content": [{"type": part_type, "text": (
            HEADER + "\n\n" + DELIMITER + "\n\n" + SUMMARY + "\n\n" + END_MARKER
        )}],
        "_compressed_summary": True,
        "timestamp": 4.0,
    }
    # An author-less header shape without a delimiter is a card as well.
    bare_header = {
        "role": "assistant",
        "content": [{"type": part_type, "text": (
            HEADER + "\n\n" + SUMMARY + "\n\n" + END_MARKER
        )}],
        "_compressed_summary": True,
        "timestamp": 4.5,
    }
    previous_display = [
        {"role": "user", "content": "first question", "timestamp": 1.0},
        card,
        pure_carrier,
        bare_header,
    ]
    previous_context = [dict(m) for m in previous_display]

    for label, result in (
        (
            "append-only",
            [dict(m) for m in previous_context]
            + [
                {"role": "user", "content": "next question", "timestamp": 7.0},
                {"role": "assistant", "content": "next answer", "timestamp": 8.0},
            ],
        ),
        ("compacted", _compacted_result()),
    ):
        merged = _merge_display_messages_after_agent_result(
            [dict(m) for m in previous_display],
            [dict(m) for m in previous_context],
            result,
            "next question",
        )
        assert _rows_containing(merged, CARD_CONTENT) == [], (
            f"{part_type}/{label}: structured standalone card leaked"
        )
        assert _rows_containing(merged, "padding detail line") == [], (
            f"{part_type}/{label}: structured textless carrier leaked its summary"
        )
        assert _rows_containing(merged, "next answer"), (
            f"{part_type}/{label}: the new turn was not appended"
        )
