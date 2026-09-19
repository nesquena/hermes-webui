"""Settled writeback must drop consumed OUT-OF-BAND steer wrappers (#7600).

A mid-turn ``/steer`` is delivered to the agent as an
``[OUT-OF-BAND USER MESSAGE ...] ... [/OUT-OF-BAND USER MESSAGE]`` block appended
to the turn's last tool result. The model-facing path strips that wrapper
(``_strip_oob_blocks`` in the gateway history builder), but the writeback in
``_prepare_marker_clean_writeback`` kept the raw text, so the settled display
transcript (``session.messages``) and the persisted context
(``session.context_messages``) both stored the wrapper and the frontend rendered
it verbatim.

These tests drive the real settle path (``_settle_result_messages``) with the
production message shape and assert on what actually gets persisted.
"""
from __future__ import annotations

import copy
import json

from api.models import Session
from api.streaming import _settle_result_messages
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


def _settle_turn_with_oob(session, monkeypatch, *, tool_content: str, steer_row=None):
    """Settle one turn whose last tool result carries the consumed steer block."""
    monkeypatch.setattr(
        _streaming, "_annotate_media_snapshots_for_settled_messages", lambda messages: None
    )
    previous = list(session.messages)
    previous_context = list(session.context_messages)
    ts = 1788440000
    steer_rows = list(steer_row or [])
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
        *steer_rows,
        {"role": "assistant", "content": "Smoke checks passed.", "timestamp": ts + 3},
    ]
    _settle_result_messages(session, previous, previous_context, result, PROMPT, "webui", None)
    return previous


def test_settle_drops_oob_wrapper_from_display_and_context(monkeypatch):
    """The consumed steer wrapper must not survive into either written copy."""
    session = _session_with_prior_turn()
    _settle_turn_with_oob(
        session, monkeypatch, tool_content=f"checks passed\n\n{OOB_BLOCK}"
    )

    assert not _contains_oob(session.messages), (
        "raw [OUT-OF-BAND USER MESSAGE] wrapper leaked into the display transcript"
    )
    assert not _contains_oob(session.context_messages), (
        "raw [OUT-OF-BAND USER MESSAGE] wrapper leaked into the model context"
    )
    # The tool result it was appended to keeps its own output.
    tool_bodies = [
        m.get("content") for m in session.messages if isinstance(m, dict) and m.get("role") == "tool"
    ]
    assert "checks passed" in json.dumps(tool_bodies)
    assert session.messages[-1]["content"] == "Smoke checks passed."


def test_settle_drops_oob_wrapper_in_list_content_parts(monkeypatch):
    """List-based content parts are stripped too (the helper already supports them)."""
    session = _session_with_prior_turn()
    _settle_turn_with_oob(
        session,
        monkeypatch,
        tool_content=[
            {"type": "text", "text": f"checks passed\n{OOB_BLOCK}\nrevision 41"},
            {"type": "text", "text": "no marker here"},
        ],
    )

    assert not _contains_oob(session.messages)
    assert not _contains_oob(session.context_messages)
    assert "checks passed" in json.dumps(session.messages)
    assert "no marker here" in json.dumps(session.messages)


def test_settle_drops_every_oob_block_in_one_row(monkeypatch):
    session = _session_with_prior_turn()
    _settle_turn_with_oob(
        session, monkeypatch, tool_content=f"before {OOB_BLOCK} middle {OOB_VARIANT} end"
    )

    assert not _contains_oob(session.messages)
    assert not _contains_oob(session.context_messages)
    joined = json.dumps(session.messages)
    assert "before" in joined and "middle" in joined and "end" in joined


def test_settle_keeps_transcript_without_markers_intact(monkeypatch):
    """No marker: every previously settled row keeps its exact content."""
    session = _session_with_prior_turn()
    previous = _settle_turn_with_oob(
        session, monkeypatch, tool_content="checks passed\nrevision 41"
    )

    assert not _contains_oob(session.messages)
    assert [m.get("content") for m in session.messages[: len(previous)]] == [
        m.get("content") for m in previous
    ]
    assert [m.get("content") for m in session.context_messages[: len(previous)]] == [
        m.get("content") for m in previous
    ]


def test_settle_keeps_prose_that_merely_names_the_marker(monkeypatch):
    """A user row that *talks about* the marker (no brackets) must not be trimmed."""
    session = _session_with_prior_turn()
    prose = "the docs describe the OUT-OF-BAND USER MESSAGE wrapper used by /steer"
    previous = list(session.messages)
    previous_context = list(session.context_messages)
    monkeypatch.setattr(
        _streaming, "_annotate_media_snapshots_for_settled_messages", lambda messages: None
    )
    result = copy.deepcopy(previous_context) + [
        {"role": "user", "content": PROMPT, "timestamp": 1788440000},
        {"role": "assistant", "content": prose, "timestamp": 1788440001},
    ]

    _settle_result_messages(session, previous, previous_context, result, PROMPT, "webui", None)

    assert prose in json.dumps(session.messages)
    assert [m.get("content") for m in session.messages[: len(previous)]] == [
        m.get("content") for m in previous
    ]


def test_settle_keeps_unclosed_marker_as_is(monkeypatch):
    """Unclosed wrappers stay untouched — stripping only complete, consumed blocks.

    This is the contract ``_strip_oob_blocks`` already documents (a half-written
    marker means the block was never completed), and the settle path must not
    invent a different one.
    """
    session = _session_with_prior_turn()
    _settle_turn_with_oob(
        session, monkeypatch, tool_content="checks passed\n[OUT-OF-BAND USER MESSAGE — truncated"
    )

    assert session.messages[-1]["content"] == "Smoke checks passed."
    assert "truncated" in json.dumps(session.messages)


def test_settle_heals_a_transcript_that_already_stored_the_wrapper(monkeypatch):
    """A transcript polluted by an earlier settle is scrubbed on the next one.

    The display merge carries earlier rows across turns verbatim, so without a
    scrub on the merged transcript the wrapper written before this fix would stay
    visible for the rest of the session (and the agent replays it with the
    wrapper still in place).
    """
    session = _session_with_prior_turn()
    # Turn settled before the guard existed: the tool row kept the raw wrapper.
    session.messages[2]["content"] = f"deploy finished: revision 41\n{OOB_BLOCK}"
    session.context_messages = copy.deepcopy(session.messages)
    monkeypatch.setattr(
        _streaming, "_annotate_media_snapshots_for_settled_messages", lambda messages: None
    )
    previous = list(session.messages)
    previous_context = list(session.context_messages)
    result = copy.deepcopy(previous_context) + [
        {"role": "user", "content": PROMPT, "timestamp": 1788440000},
        {"role": "assistant", "content": "Nothing else changed.", "timestamp": 1788440001},
    ]

    _settle_result_messages(session, previous, previous_context, result, PROMPT, "webui", None)

    assert not _contains_oob(session.messages)
    assert not _contains_oob(session.context_messages)
    assert sum(1 for m in session.messages if m.get("role") == "tool") == 1
    assert "deploy finished: revision 41" in json.dumps(session.messages)


def test_settle_keeps_literal_markers_in_user_text_intact_and_unduplicated(monkeypatch):
    """A complete marker inside user text is content, not transport control data.

    The scrub may only touch the tool row the transport appended the wrapper to.
    A user row that quotes a *complete* marker — a pasted log excerpt, or the very
    prompt asking about this feature — is user-visible content: it must survive
    byte-for-byte and exactly once in both written copies. Before the role gate,
    the pre-match scrub also shortened the current turn's own prompt row, which
    broke active-turn identity matching and duplicated the turn.
    """
    pasted = (
        "our bot log shows this block, is that normal?\n"
        f"{OOB_BLOCK}\n"
        "the docs say the gateway adds it"
    )
    asked = f"is this wrapper expected?\n{OOB_VARIANT}\nit showed up mid-turn"
    session = _session_with_prior_turn()
    # Prior turn, already settled: its user row quotes the wrapper as an example.
    session.messages[0]["content"] = pasted
    session.context_messages = copy.deepcopy(session.messages)
    previous = list(session.messages)
    previous_context = list(session.context_messages)
    monkeypatch.setattr(
        _streaming, "_annotate_media_snapshots_for_settled_messages", lambda messages: None
    )

    ts = 1788440000
    token = "direct-stream:2"
    identity = {
        "token": token,
        "text": asked,
        "timestamp": float(ts),
        "source": "webui",
        "attachments": [],
        "current_turn_user_idx": len(previous_context),
        "turn_id": "turn-2",
        "agent_turn_boundary_resolved": True,
    }
    result = copy.deepcopy(previous_context) + [
        {"role": "user", "content": asked, "timestamp": ts, "_active_turn_token": token},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "call-2"}],
            "timestamp": ts + 1,
        },
        {
            "role": "tool",
            "tool_call_id": "call-2",
            "content": f"checks passed\n\n{OOB_BLOCK}",
            "timestamp": ts + 2,
        },
        {"role": "assistant", "content": "Smoke checks passed.", "timestamp": ts + 3},
    ]

    _settle_result_messages(
        session, previous, previous_context, result, asked, "webui", identity
    )

    expected_user_rows = [pasted, asked]
    for written in (session.messages, session.context_messages):
        user_texts = [
            m.get("content")
            for m in written
            if isinstance(m, dict) and m.get("role") == "user"
        ]
        assert user_texts == expected_user_rows, (
            "a literal marker in user text must survive intact and exactly once, "
            f"got {user_texts!r}"
        )
    assert session.messages[-1]["content"] == "Smoke checks passed."
    # The consumed steer wrapper on the carrier tool row is still dropped.
    tool_bodies = [
        m.get("content") for m in session.messages if isinstance(m, dict) and m.get("role") == "tool"
    ]
    assert not _contains_oob(tool_bodies), "wrapper leaked into a tool row"
    assert "checks passed" in json.dumps(tool_bodies)
