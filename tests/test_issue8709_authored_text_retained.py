"""#8748 — authentic text that starts with the compaction vocabulary must survive.

Maintainer review 2026-11-23T12:21:44Z, findings 1 and 2 plus the browser mirror
note. The Agent folds the summary into the first retained tail row, so the
authentic text of that row sits BETWEEN the `[prior context` header and the
`[end of prior context` delimiter, and the summary comes AFTER the delimiter.
Two guarantees have to hold on that shape:

1. A retained row whose authored text begins with "Context compaction ..." is
   authentic: the display projection keeps it instead of emptying the row.
2. The model-facing deduper must not rewrite a folded carrier's `user` role to
   `assistant`; a standalone mis-roled summary card keeps being normalized.

The browser mirror (`_isContextCompactionText`) must not classify a projected
authored row as a compaction card, or the server-side retention stays invisible.
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
    _CARRIER_AUTHENTIC_REMAINDER,
    _CARRIER_NOT_A_CARRIER,
    _CARRIER_PROVEN_SYNTHETIC,
    _compaction_carrier_envelope_decision,
    project_compaction_carrier_for_display,
)
from api.models import Session
from api.streaming import _deduplicate_context_messages

ROOT = Path(__file__).resolve().parents[1]
UI_JS = ROOT / "static" / "ui.js"
NODE = shutil.which("node")

HEADER = "[PRIOR CONTEXT — for reference only; not a new message]"
DELIMITER = "[END OF PRIOR CONTEXT — COMPACTION SUMMARY BELOW]"
SUMMARY = (
    "[CONTEXT COMPACTION — REFERENCE ONLY] Earlier turns were compacted into the "
    "summary below.\n## Goal\nShip the carrier fix.\n"
)
AUTHORED_TEXT = "Context compaction made my chat lose the table, see above"
CARD_CONTENT = "[context compaction — reference only] earlier turns were compacted.\n## Goal\nolder summary\n"
FOLDED_AUTHORED_CONTENT = HEADER + "\n" + AUTHORED_TEXT + "\n\n" + DELIMITER + "\n\n" + SUMMARY
PURE_CONTENT = HEADER + "\n\n" + DELIMITER + "\n\n" + SUMMARY
PLAIN_RETAINED_CONTENT = (
    HEADER + "\nLet me check the failing assertion before editing the parser.\n\n" + DELIMITER + "\n\n" + SUMMARY
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
    proc = subprocess.run([NODE], input=script, text=True, capture_output=True, check=True)
    return json.loads(proc.stdout)


# ── 1. Envelope decision: non-empty authored head is authentic ───────────────


def test_envelope_decision_keeps_authored_text_that_starts_with_the_marker_prefix():
    decision, remainder = _compaction_carrier_envelope_decision(FOLDED_AUTHORED_CONTENT)
    assert decision == _CARRIER_AUTHENTIC_REMAINDER
    assert remainder == AUTHORED_TEXT

    # Control: an ordinary retained row is still authentic
    decision, remainder = _compaction_carrier_envelope_decision(PLAIN_RETAINED_CONTENT)
    assert (decision, remainder) == (
        _CARRIER_AUTHENTIC_REMAINDER,
        "Let me check the failing assertion before editing the parser.",
    )

    # Control: no authored head at all stays a proven synthetic carrier
    assert _compaction_carrier_envelope_decision(PURE_CONTENT) == (_CARRIER_PROVEN_SYNTHETIC, "")

    # Control: a plain row is not a carrier
    assert _compaction_carrier_envelope_decision("hello there") == (_CARRIER_NOT_A_CARRIER, "")


def test_projection_keeps_authored_user_row_content():
    raw = {
        "role": "user",
        "content": FOLDED_AUTHORED_CONTENT,
        "timestamp": 10.0,
        "_compressed_summary": True,
    }
    projected = project_compaction_carrier_for_display(raw)

    assert projected["content"] == AUTHORED_TEXT
    assert projected["role"] == "user"
    assert projected.get("_pure_compaction_carrier") is not True
    # Display copy never carries the handoff flag forward
    assert "_compressed_summary" not in projected
    # Input (model-facing history) untouched
    assert raw["content"] == FOLDED_AUTHORED_CONTENT
    assert raw["_compressed_summary"] is True


# ── 2. Route: GET /api/session keeps the authored text ───────────────────────


def test_fallback_session_get_keeps_authored_text_that_starts_with_prefix(monkeypatch):
    raw_carrier = {
        "role": "user",
        "content": FOLDED_AUTHORED_CONTENT,
        "timestamp": 100.0,
        "_compressed_summary": True,
    }
    answer = {"role": "assistant", "content": "Here is the table again", "timestamp": 101.0}

    synth_session = Session(
        session_id="synth_sid_8748n",
        workspace="/tmp/test_workspace",
        title="Synthesized CLI Session",
        model="hermes-3",
        messages=[raw_carrier, answer],
    )

    monkeypatch.setattr(
        routes,
        "get_session",
        lambda sid, *a, **kw: (_ for _ in ()).throw(KeyError(sid)),
    )
    monkeypatch.setattr(routes, "_lookup_cli_session_metadata", lambda sid: {"profile": "default"})
    monkeypatch.setattr(
        routes,
        "_claim_or_synthesize_cli_session",
        lambda sid, cli_meta: (synth_session, "foreign_found"),
    )

    handler = _MockHandler()
    parsed = urlparse("/api/session?session_id=synth_sid_8748n&messages=1")
    routes._handle_session_get(handler, parsed)
    assert handler.status == 200

    messages = handler.payload().get("session", {}).get("messages", [])
    carriers = [m for m in messages if m.get("role") == "user" and "Context compaction" in str(m.get("content"))]
    assert len(carriers) == 1, messages
    projected = carriers[0]

    assert projected["content"] == AUTHORED_TEXT
    assert HEADER not in projected["content"]
    assert DELIMITER not in projected["content"]
    assert "Ship the carrier fix" not in projected["content"]

    # Model-facing history keeps the full envelope
    assert synth_session.messages[0]["content"] == FOLDED_AUTHORED_CONTENT
    assert synth_session.messages[0]["_compressed_summary"] is True


# ── 3. Deduper: folded carrier keeps its user role ──────────────────────────


def test_dedup_keeps_folded_carrier_user_role_and_content():
    folded = {
        "role": "user",
        "content": FOLDED_AUTHORED_CONTENT,
        "timestamp": 5.0,
        "_compressed_summary": True,
    }
    result = _deduplicate_context_messages([folded])

    assert len(result) == 1
    assert result[0]["role"] == "user"
    assert result[0]["content"] == FOLDED_AUTHORED_CONTENT


def test_dedup_still_normalizes_a_standalone_misroled_card():
    misroled_card = {"role": "user", "content": CARD_CONTENT, "timestamp": 6.0}
    result = _deduplicate_context_messages([misroled_card])

    assert len(result) == 1
    assert result[0]["role"] == "assistant"
    assert result[0]["content"] == CARD_CONTENT


# ── 4. Browser mirror: projected authored text is not a compaction card ─────


@pytest.mark.skipif(NODE is None, reason="node is required for browser mirror tests")
def test_browser_mirror_does_not_swallow_projected_authored_text():
    ui_source = UI_JS.read_text("utf-8")
    extracted = "\n".join(
        _extract_fn(ui_source, name) for name in ("msgContent", "_isContextCompactionText")
    )

    script = f"""
{extracted}

const authored = {{role:'user', content:{json.dumps(AUTHORED_TEXT)}}};
const flaggedAuthored = {{role:'assistant', content:{json.dumps(AUTHORED_TEXT)}, _compressed_summary:true}};
const card = {{role:'assistant', content:{json.dumps(CARD_CONTENT)}}};
const plain = {{role:'assistant', content:'Normal answer'}};

process.stdout.write(JSON.stringify({{
  authored: _isContextCompactionText(authored.content, authored),
  flagged_authored: _isContextCompactionText(flaggedAuthored.content, flaggedAuthored),
  card: _isContextCompactionText(card.content, card),
  plain: _isContextCompactionText(plain.content, plain),
}}));
"""
    results = _eval_node_script(script)

    assert results["authored"] is False
    assert results["plain"] is False
    # Controls: the flagged legacy carrier and the bracketed card stay compaction text
    assert results["flagged_authored"] is True
    assert results["card"] is True
