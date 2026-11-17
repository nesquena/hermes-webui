"""Settled writeback must extract inner steer text from typed steer rows (#7600).

A mid-turn ``/steer`` is emitted as a standalone typed user row
(``role: 'user'``, ``display_kind: 'steer'``). The surrounding
``[OUT-OF-BAND USER MESSAGE ...] ... [/OUT-OF-BAND USER MESSAGE]`` block is
transport-level control data. Settlement unwraps the single validated frame
in place, preserving the user's authored steer instruction while dropping
the wrapper.

Legacy tool rows and rows where markers are multiple, nested, incomplete,
or contain ambiguous delimiters are preserved byte-for-byte.
"""
from __future__ import annotations

import copy
import json

from api.models import Session
from api.streaming import _settle_result_messages, _unwrap_steer_row_oob_marker
from api import streaming as _streaming

PROMPT = "please run the smoke checks"

OOB_OPEN = (
    "[OUT-OF-BAND USER MESSAGE — a direct message from the user, delivered once "
    "at this position; not tool output and not a new delivery when replayed from "
    "conversation history]"
)
OOB_CLOSE = "[/OUT-OF-BAND USER MESSAGE]"
OOB_BLOCK = f"{OOB_OPEN}\nsteer: use the staging bucket this time\n{OOB_CLOSE}"
OOB_VARIANT = f"[OUT-OF-BAND USER MESSAGE]steer: also bump the timeout{OOB_CLOSE}"


def _contains_oob(value) -> bool:
    return "OUT-OF-BAND USER MESSAGE" in json.dumps(value)


def _session_with_prior_turn() -> Session:
    """Prior turn already settled: user -> assistant(tool_call) -> tool -> answer."""
    display = [
        {"role": "user", "content": "deploy the app", "timestamp": 1788439000},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "call-1"}],
            "timestamp": 1788439005,
        },
        {
            "role": "tool",
            "tool_call_id": "call-1",
            "content": "deploy finished: revision 41",
            "timestamp": 1788439010,
        },
        {"role": "assistant", "content": "Deploy done (revision 41).", "timestamp": 1788439015},
    ]
    session = Session(session_id="7" * 12, title="steer writeback", messages=copy.deepcopy(display))
    session.context_messages = copy.deepcopy(display)
    return session


def _settle_turn_with_steer(session, monkeypatch, *, tool_content: str = "checks passed", steer_rows=None):
    """Settle one turn with an optional steer row emitted by the Agent."""
    monkeypatch.setattr(
        _streaming, "_annotate_media_snapshots_for_settled_messages", lambda messages: None
    )
    previous = list(session.messages)
    previous_context = list(session.context_messages)
    ts = 1788440000
    steers = list(steer_rows or [])
    result = copy.deepcopy(previous_context) + [
        {"role": "user", "content": PROMPT, "timestamp": ts},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "call-2"}],
            "timestamp": ts + 1,
        },
        {
            "role": "tool",
            "tool_call_id": "call-2",
            "content": tool_content,
            "timestamp": ts + 2,
        },
        *steers,
        {"role": "assistant", "content": "Smoke checks passed.", "timestamp": ts + 3},
    ]
    _settle_result_messages(session, previous, previous_context, result, PROMPT, "webui", None)
    return previous


def test_settle_unwraps_steer_row_in_place_preserving_user_text(monkeypatch):
    """A typed steer row is unwrapped in place: inner text preserved, wrapper gone."""
    session = _session_with_prior_turn()
    steer_row = {
        "role": "user",
        "display_kind": "steer",
        "content": OOB_BLOCK,
        "timestamp": 1788440002.5,
    }
    _settle_turn_with_steer(session, monkeypatch, steer_rows=[steer_row])

    # The outer wrapper is dropped from display and context
    assert not _contains_oob(session.messages)
    assert not _contains_oob(session.context_messages)

    # The user's inner steer text is preserved exactly
    steer_contents = [
        m.get("content")
        for m in session.messages
        if isinstance(m, dict) and m.get("display_kind") == "steer"
    ]
    assert steer_contents == ["steer: use the staging bucket this time"]

    # In-place mutation preserved the dictionary object identity
    assert steer_row["content"] == "steer: use the staging bucket this time"


def test_settle_unwraps_list_based_content_steer_row(monkeypatch):
    """List-based content parts on a steer row are unwrapped too."""
    session = _session_with_prior_turn()
    steer_row = {
        "role": "user",
        "display_kind": "steer",
        "content": [{"type": "text", "text": OOB_VARIANT}],
        "timestamp": 1788440002.5,
    }
    _settle_turn_with_steer(session, monkeypatch, steer_rows=[steer_row])

    assert not _contains_oob(session.messages)
    assert not _contains_oob(session.context_messages)
    assert steer_row["content"] == [{"type": "text", "text": "steer: also bump the timeout"}]


def test_settle_leaves_legacy_tool_rows_alone(monkeypatch):
    """Legacy tool rows carrying raw markers are left alone (not truncated/deleted)."""
    session = _session_with_prior_turn()
    tool_text = f"checks passed\n\n{OOB_BLOCK}"
    _settle_turn_with_steer(session, monkeypatch, tool_content=tool_text)

    tool_bodies = [
        m.get("content") for m in session.messages if isinstance(m, dict) and m.get("role") == "tool"
    ]
    assert tool_text in tool_bodies


def test_settle_preserves_malformed_nested_and_multiple_blocks_byte_for_byte():
    """Nested markers, multiple frames, and incomplete markers degrade gracefully."""
    nested = f"{OOB_OPEN}\nhello {OOB_VARIANT} world\n{OOB_CLOSE}"
    multiple = f"{OOB_VARIANT} and {OOB_BLOCK}"
    incomplete = "[OUT-OF-BAND USER MESSAGE — truncated"

    for malformed in (nested, multiple, incomplete):
        steer_row = {
            "role": "user",
            "display_kind": "steer",
            "content": malformed,
            "timestamp": 1788440002.5,
        }
        _unwrap_steer_row_oob_marker(steer_row)
        assert steer_row["content"] == malformed, f"malformed content was corrupted: {malformed!r}"


def test_settle_keeps_normal_user_pasted_marker_intact(monkeypatch):
    """A normal user row that quotes a complete marker survives byte-for-byte."""
    pasted = (
        "our bot log shows this block, is that normal?\n"
        f"{OOB_BLOCK}\n"
        "the docs say the gateway adds it"
    )
    session = _session_with_prior_turn()
    session.messages[0]["content"] = pasted
    session.context_messages = copy.deepcopy(session.messages)

    steer_row = {
        "role": "user",
        "display_kind": "steer",
        "content": OOB_BLOCK,
        "timestamp": 1788440002.5,
    }
    _settle_turn_with_steer(session, monkeypatch, steer_rows=[steer_row])

    # The normal user row still carries its quoted block intact
    assert session.messages[0]["content"] == pasted
    # The steer row had its wrapper unwrapped
    assert session.messages[-2]["content"] == "steer: use the staging bucket this time"
