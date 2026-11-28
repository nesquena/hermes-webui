"""#8748 — carry the positive compaction envelope through the display path.

Validates that:
1. Server predicate: is_context_compression_marker requires positive envelope evidence
   for the [prior context ...] handoff header (closing ] plus delimiter, context
   compaction prefix, or truthy _compressed_summary), rejecting authored rows.
2. visible_messages_for_anchor keeps authored rows (2 rows in, 2 out) while skipping
   genuine merged carriers and flagged legacy carriers.
3. Browser mirror: _isContextCompactionText in static/ui.js follows the server rule,
   so _messageIsRenderable is true for authored rows, false for unprojected merged carriers,
   and standalone compaction cards remain classified as compaction messages.
4. Fallback: _handle_session_get's KeyError branch projects compaction carriers for display
   without mutating synth.messages in model-facing history.
5. Budget: proven pure synthetic carriers projected to empty content do not consume the
   visible-row budget, and trailing carriers are retained in the window without hiding answers.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from io import BytesIO
from pathlib import Path
from urllib.parse import urlparse

import pytest

import api.routes as routes
from api.compression_anchor import (
    is_context_compression_marker,
    project_compaction_carriers_for_display,
    visible_messages_for_anchor,
)
from api.models import Session

ROOT = Path(__file__).resolve().parents[1]
UI_JS = ROOT / "static" / "ui.js"
NODE = shutil.which("node")

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
    "- worktree wt-516-8709\n"
)
MERGED_CARRIER_CONTENT = (
    HEADER + "\n" + RETAINED_STEP + "\n\n" + DELIMITER + "\n\n" + SUMMARY + "\n\n" + END_MARKER
)
CARD_CONTENT = (
    "[context compaction — reference only] earlier turns were compacted.\n"
    "## Goal\nolder summary\n" + END_MARKER
)
PURE_CARRIER_CONTENT = (
    HEADER + "\n\n" + DELIMITER + "\n\n" + SUMMARY + "\n\n" + END_MARKER
)


class _MockHandler:
    def __init__(self):
        self.status = None
        self.headers = {}
        self.wfile = BytesIO()
        self.client_address = ("127.0.0.1", 1)

    def send_response(self, status):
        self.status = status

    def send_header(self, k, v):
        self.headers[k] = v

    def end_headers(self):
        pass

    def write(self, data):
        self.wfile.write(data)

    def payload(self):
        return json.loads(self.wfile.getvalue().decode("utf-8") or "{}")


def _extract_fn(source: str, name: str) -> str:
    marker = f"function {name}("
    start = source.find(marker)
    assert start >= 0, f"{name} not found in source"
    body_start = source.find("{", start)
    depth = 0
    for idx in range(body_start, len(source)):
        c = source[idx]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return source[start : idx + 1]
    raise AssertionError(f"unbalanced braces for {name}")


def _eval_node_script(script: str) -> dict:
    assert NODE is not None, "node executable is required"
    # Feed program through stdin to avoid WinError 206 (command line length limit)
    proc = subprocess.run(
        [NODE],
        input=script,
        text=True,
        capture_output=True,
        check=True,
    )
    return json.loads(proc.stdout)


# ── 1. Server predicate: is_context_compression_marker ─────────────────────────


def test_is_context_compression_marker_authored_rows_return_false():
    # User-authored note starting with [PRIOR CONTEXT]
    user_row = {"role": "user", "content": "[PRIOR CONTEXT] user-authored quote or notes"}
    assert is_context_compression_marker(user_row) is False

    # Assistant-authored note starting with [PRIOR CONTEXT]
    assistant_row = {"role": "assistant", "content": "[PRIOR CONTEXT] my own note"}
    assert is_context_compression_marker(assistant_row) is False

    # Mixed case / bracket form without envelope delimiter
    mixed_case = {"role": "assistant", "content": "[Prior context — my own note]"}
    assert is_context_compression_marker(mixed_case) is False

    # List-content text block of the same shape
    list_content_row = {
        "role": "user",
        "content": [{"type": "text", "text": "[PRIOR CONTEXT] user-authored quote or notes"}],
    }
    assert is_context_compression_marker(list_content_row) is False

    # Unclosed bracket
    unclosed = {"role": "assistant", "content": "[prior context unclosed quote"}
    assert is_context_compression_marker(unclosed) is False


def test_is_context_compression_marker_positive_envelope_evidence_returns_true():
    # Positive evidence (a): delimiter [end of prior context in the text
    row_with_delim = {
        "role": "assistant",
        "content": f"[PRIOR CONTEXT — notes]\nretained\n{DELIMITER}\nsummary",
    }
    assert is_context_compression_marker(row_with_delim) is True

    # Positive evidence (b): text after ] starting with [context compaction
    row_with_compaction_bracket = {
        "role": "assistant",
        "content": "[PRIOR CONTEXT — ref]\n[CONTEXT COMPACTION — ref] summary",
    }
    assert is_context_compression_marker(row_with_compaction_bracket) is True

    # Positive evidence (b): text after ] starting with context compaction
    row_with_compaction_unbracketed = {
        "role": "assistant",
        "content": "[PRIOR CONTEXT — ref]\ncontext compaction summary",
    }
    assert is_context_compression_marker(row_with_compaction_unbracketed) is True

    # Positive evidence (c): truthy _compressed_summary flag on the row
    row_with_flag = {
        "role": "assistant",
        "content": "[PRIOR CONTEXT] legacy carrier head",
        "_compressed_summary": True,
    }
    assert is_context_compression_marker(row_with_flag) is True

    # Standalone [context compaction ...] card still returns True
    standalone_card = {"role": "assistant", "content": CARD_CONTENT}
    assert is_context_compression_marker(standalone_card) is True


# ── 2. visible_messages_for_anchor ─────────────────────────────────────────────


def test_visible_messages_for_anchor_preserves_authored_rows():
    authored_user = {"role": "user", "content": "[PRIOR CONTEXT] user-authored quote or notes"}
    authored_assistant = {"role": "assistant", "content": "[PRIOR CONTEXT] my own note"}

    # 2 authored rows in -> 2 rows out
    result = visible_messages_for_anchor([authored_user, authored_assistant])
    assert len(result) == 2
    assert result[0] == authored_user
    assert result[1] == authored_assistant

    # Genuine merged carrier with delimiter is skipped
    merged_carrier = {
        "role": "assistant",
        "content": MERGED_CARRIER_CONTENT,
    }
    # Flagged legacy carrier is skipped
    flagged_carrier = {
        "role": "assistant",
        "content": "[PRIOR CONTEXT] legacy carrier head",
        "_compressed_summary": True,
    }

    transcript = [authored_user, merged_carrier, flagged_carrier, authored_assistant]
    filtered = visible_messages_for_anchor(transcript)
    assert len(filtered) == 2
    assert filtered == [authored_user, authored_assistant]


# ── 3. Browser mirror: static/ui.js evaluation with node ──────────────────────


@pytest.mark.skipif(NODE is None, reason="node is required for browser mirror tests")
def test_browser_mirror_extracts_real_functions_and_classifies_correctly():
    ui_source = UI_JS.read_text("utf-8")

    fns_to_extract = [
        "msgContent",
        "_isRecoveryControlMessageText",
        "_isRecoveryControlMessage",
        "_assistantAnchorSceneFinalAnswerText",
        "_messageHasReasoningPayload",
        "_isAssistantEmptyPlaceholderContent",
        "_assistantMessageHasVisibleContent",
        "_isPreservedCompressionTaskListMessage",
        "_isContextCompactionText",
        "_isContextCompactionMessage",
        "_messageIsRenderable",
    ]

    extracted_code = "\n".join(_extract_fn(ui_source, fn) for fn in fns_to_extract)

    test_harness = f"""
{extracted_code}

const userAuthored = {{role: 'user', content: '[PRIOR CONTEXT] user-authored quote or notes'}};
const assistantAuthored = {{role: 'assistant', content: '[PRIOR CONTEXT] my own note'}};
const plainAssistant = {{role: 'assistant', content: 'Normal assistant answer'}};
const mergedCarrier = {{role: 'assistant', content: {json.dumps(MERGED_CARRIER_CONTENT)}}};
const standaloneCard = {{role: 'assistant', content: {json.dumps(CARD_CONTENT)}}};

const results = {{
  user_authored_compaction_text: _isContextCompactionText(userAuthored.content),
  user_authored_compaction_msg: _isContextCompactionMessage(userAuthored),
  user_authored_renderable: _messageIsRenderable(userAuthored),

  assistant_authored_compaction_text: _isContextCompactionText(assistantAuthored.content),
  assistant_authored_compaction_msg: _isContextCompactionMessage(assistantAuthored),
  assistant_authored_renderable: _messageIsRenderable(assistantAuthored),

  plain_assistant_renderable: _messageIsRenderable(plainAssistant),

  merged_carrier_compaction_msg: _isContextCompactionMessage(mergedCarrier),
  merged_carrier_renderable: _messageIsRenderable(mergedCarrier),

  standalone_card_compaction_msg: _isContextCompactionMessage(standaloneCard),
  standalone_card_renderable: _messageIsRenderable(standaloneCard),
}};

process.stdout.write(JSON.stringify(results));
"""

    results = _eval_node_script(test_harness)

    # Authored rows are not compaction markers and stay renderable
    assert results["user_authored_compaction_text"] is False
    assert results["user_authored_compaction_msg"] is False
    assert results["user_authored_renderable"] is True

    assert results["assistant_authored_compaction_text"] is False
    assert results["assistant_authored_compaction_msg"] is False
    assert results["assistant_authored_renderable"] is True

    # Control: plain assistant answer is renderable
    assert results["plain_assistant_renderable"] is True

    # Merged carrier before projection is a compaction message and not renderable
    assert results["merged_carrier_compaction_msg"] is True
    assert results["merged_carrier_renderable"] is False

    # Standalone compaction card keeps its current classification
    assert results["standalone_card_compaction_msg"] is True
    assert results["standalone_card_renderable"] is False


# ── 4. Fallback: _handle_session_get's KeyError branch ──────────────────────────


def test_fallback_handle_session_get_returns_projected_display_copy(monkeypatch):
    carrier_tool_calls = [
        {"id": "call_1", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}
    ]
    raw_carrier = {
        "role": "assistant",
        "content": MERGED_CARRIER_CONTENT,
        "tool_calls": carrier_tool_calls,
        "timestamp": 100.0,
        "_compressed_summary": True,
    }
    user_msg = {
        "role": "user",
        "content": "Please inspect this repo",
        "timestamp": 90.0,
    }

    synth_session = Session(
        session_id="synth_sid_8748",
        workspace="/tmp/test_workspace",
        title="Synthesized CLI Session",
        model="hermes-3",
        messages=[user_msg, raw_carrier],
    )

    # Force KeyError in get_session to enter the foreign/synthesized fallback branch
    monkeypatch.setattr(
        routes,
        "get_session",
        lambda sid, *a, **kw: (_ for _ in ()).throw(KeyError(sid)),
    )
    monkeypatch.setattr(
        routes,
        "_lookup_cli_session_metadata",
        lambda sid: {"profile": "default"},
    )
    monkeypatch.setattr(
        routes,
        "_claim_or_synthesize_cli_session",
        lambda sid, cli_meta: (synth_session, "foreign_found"),
    )

    handler = _MockHandler()
    parsed = urlparse("/api/session?session_id=synth_sid_8748&messages=1")
    routes._handle_session_get(handler, parsed)
    assert handler.status == 200

    payload = handler.payload()
    session_data = payload.get("session", {})
    messages = session_data.get("messages", [])

    assert len(messages) == 2
    projected_carrier = messages[1]

    # Handoff header, delimiter and summary are gone; retained step text survives
    assert projected_carrier["content"] == RETAINED_STEP
    assert HEADER not in projected_carrier["content"]
    assert DELIMITER not in projected_carrier["content"]
    assert "Ship the carrier fix" not in projected_carrier["content"]

    # tool_calls survive
    assert projected_carrier.get("tool_calls") == carrier_tool_calls

    # Flag was removed from display copy
    assert "_compressed_summary" not in projected_carrier

    # Crucial: synth.messages was NOT mutated (raw/model-facing history keeps the carrier)
    assert synth_session.messages[1]["content"] == MERGED_CARRIER_CONTENT
    assert synth_session.messages[1].get("_compressed_summary") is True
    assert synth_session.messages[1].get("tool_calls") == carrier_tool_calls


# ── 5. Budget: proven pure synthetic carrier does not consume budget ───────────


def test_budget_pure_synthetic_carrier_does_not_consume_visible_budget():
    user_msg = {"role": "user", "content": "user question", "timestamp": 1.0}
    answer_msg = {"role": "assistant", "content": "real answer", "timestamp": 2.0}
    pure_carrier_raw = {
        "role": "assistant",
        "content": PURE_CARRIER_CONTENT,
        "timestamp": 3.0,
        "_compressed_summary": True,
    }

    source = [user_msg, answer_msg, pure_carrier_raw]
    projected = project_compaction_carriers_for_display(source)

    # Empty content after projection
    assert projected[2]["content"] == ""

    # Predicate: pure carrier does not count as renderable
    assert routes._message_counts_as_renderable_for_window(projected[0]) is True
    assert routes._message_counts_as_renderable_for_window(projected[1]) is True
    assert routes._message_counts_as_renderable_for_window(projected[2]) is False

    # Windowing with msg_limit=1 over [question, answer, pure carrier]
    window, offset = routes._message_window_for_display(projected, msg_limit=1)

    # Returns 2 rows: the answer and the carrier, offset = 1 (answer index)
    assert len(window) == 2
    assert window[0]["content"] == "real answer"
    assert window[1]["content"] == ""
    assert offset == 1


def test_budget_projected_row_with_anchors_keeps_counting():
    # A projected row that still carries tool_calls must keep counting
    row_with_tool_calls = {
        "role": "assistant",
        "content": "",
        "tool_calls": [{"id": "call_1", "function": {"name": "test"}}],
    }
    assert routes._message_counts_as_renderable_for_window(row_with_tool_calls) is True

    # A projected row that still carries attachments must keep counting
    row_with_attachments = {
        "role": "assistant",
        "content": "",
        "attachments": [{"url": "http://example.com/img.png"}],
    }
    assert routes._message_counts_as_renderable_for_window(row_with_attachments) is True

    # A projected row that still carries reasoning must keep counting
    row_with_reasoning = {
        "role": "assistant",
        "content": "",
        "reasoning": "thought process details",
    }
    assert routes._message_counts_as_renderable_for_window(row_with_reasoning) is True

    # A projected row that carries non-text content blocks must keep counting
    row_with_non_text = {
        "role": "assistant",
        "content": [{"type": "image", "source": "xyz"}],
    }
    assert routes._message_counts_as_renderable_for_window(row_with_non_text) is True
