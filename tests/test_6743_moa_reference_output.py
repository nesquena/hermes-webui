"""Regression tests for #6743 — MoA reference outputs silently dropped in WebUI.

Root cause: ``api/streaming.py``'s ``on_tool`` handled ``reasoning.available``
but had no branch for the MoA display-event family (``moa.reference`` /
``moa.progress`` / ``moa.phase`` / ``moa.aggregating``) that the Hermes Agent
relays through ``tool_progress_callback``. The events fell through every guard
and were never ``put()`` onto the SSE queue, so the WebUI showed nothing during
the reference fan-out — only the aggregator's final answer. The CLI/TUI render
each reference as a labelled block.

Fix (this PR):
  1. Backend — ``on_tool`` forwards each ``moa.*`` event as its own SSE event
     with a payload shaped like the gateway's (label/text/index/count for
     references; refs_done/refs_total for progress; phase/aggregator for
     phase/aggregating).
  2. Frontend — ``static/messages.js`` registers ``moa.reference`` /
     ``moa.progress`` / ``moa.phase`` / ``moa.aggregating`` SSE listeners and
     renders each reference as a labelled, collapsible ``thinking-card`` block
     (``_upsertMoaReference``) plus a shared status line (``_updateMoaStatus``).
  3. CSS — ``.moa-reference`` / ``.moa-ref-counter`` / ``.moa-status``.

These are static source-structure tests (repo convention for streaming issues):
they pin the fix's shape so a future refactor cannot silently re-drop the
events.
"""
from __future__ import annotations

import pathlib
import re

REPO = pathlib.Path(__file__).parent.parent
STREAMING_PY = (REPO / "api" / "streaming.py").read_text(encoding="utf-8")
MESSAGES_JS = (REPO / "static" / "messages.js").read_text(encoding="utf-8")
STYLE_CSS = (REPO / "static" / "style.css").read_text(encoding="utf-8")


def extract_fn(src: str, name: str) -> str:
    """Return the full source of ``function name(...) {...}`` / ``def name(...)``.

    JS functions are brace-counted from the opening ``{``. Python functions are
    indentation-based: slice from the ``def`` line to the next same-indent
    ``def``/non-body line (dict literals inside the body would otherwise
    confuse brace counting).
    """
    m = re.search(rf"(?:function|def) {re.escape(name)}\s*\(", src)
    assert m, f"{name}() not found in source"
    if src[m.start():m.start() + 3] == "def":
        line_start = src.rfind("\n", 0, m.start()) + 1
        indent = src[line_start:m.start()]
        # End at the next line that starts a top-level statement at the same indent.
        body_start = m.start()
        for candidate in re.finditer(rf"\n{re.escape(indent)}def |\n{re.escape(indent)}class |\n{re.escape(indent)}[A-Za-z_]", src[body_start + 1:]):
            pos = body_start + 1 + candidate.start()
            # Skip our own def line (the match starts at the same indent but is
            # the def itself — the first regex hit after the signature is the
            # first body statement, which is indented deeper, so the next
            # same-indent hit is a sibling).
            if pos > body_start + len(m.group(0)):
                return src[body_start:pos]
        return src[body_start:]
    brace = src.index("{", m.end())
    depth = 1
    pos = brace + 1
    while pos < len(src) and depth > 0:
        ch = src[pos]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
        pos += 1
    return src[m.start():pos]


# ── Backend: on_tool must forward the MoA display-event family ────────────

def test_on_tool_forwards_moa_reference():
    fn = extract_fn(STREAMING_PY, "on_tool")
    # The MoA branch must be a distinct guard (not folded into tool.started).
    assert "('moa.reference', 'moa.progress', 'moa.phase', 'moa.aggregating')" in fn
    # Reference payload must carry the labelled block fields.
    assert "'label': name" in fn
    assert "'text': preview" in fn
    # Progress payload carries the N/M counters.
    assert "'refs_done'" in fn
    assert "'refs_total'" in fn
    # The event is emitted on the SSE queue under its own name.
    assert "put(event_type, _moa_payload)" in fn
    # Display-only: it must return before tool-card bookkeeping runs.
    assert fn.index("moa.reference") < fn.index("event_type in (None, 'tool.started')")


def test_on_tool_moa_branch_sits_before_tool_guards():
    fn = extract_fn(STREAMING_PY, "on_tool")
    moa_pos = fn.index("('moa.reference', 'moa.progress', 'moa.phase', 'moa.aggregating')")
    tool_started_pos = fn.index("event_type in (None, 'tool.started')")
    assert moa_pos < tool_started_pos, "MoA branch must be handled before tool.started guards"


def test_on_tool_moa_payload_keeps_aggregator_and_count():
    fn = extract_fn(STREAMING_PY, "on_tool")
    # moa.aggregating / moa.phase carry the aggregator label + ref count.
    assert "'aggregator'" in fn
    assert "'ref_count'" in fn


# ── Frontend: SSE listeners + labelled collapsible block rendering ────────

def test_wire_sse_registers_moa_listeners():
    fn = extract_fn(MESSAGES_JS, "_wireSSE")
    for evt in ("moa.reference", "moa.progress", "moa.phase", "moa.aggregating"):
        assert f"addEventListener('{evt}'" in fn, f"missing {evt} listener in _wireSSE"


def test_upsert_moa_reference_renders_labelled_block():
    fn = extract_fn(MESSAGES_JS, "_upsertMoaReference")
    # Labelled, collapsible block: model name in header, text in body.
    assert "data-moa-ref" in fn
    assert "thinking-card" in fn
    assert "thinking-card-label" in fn
    assert "thinking-card-body" in fn
    assert "esc(String(d.label||''))" in fn
    # Escaped body text, never raw innerHTML of the reference text.
    assert "body.textContent=String(d.text)" in fn
    # Guarded by session identity like every other live-stream handler.
    assert "S.session.session_id!==activeSid" in fn


def test_update_moa_status_shows_progress_and_aggregating():
    fn = extract_fn(MESSAGES_JS, "_updateMoaStatus")
    assert "MoA: ${d.refs_done}/${d.refs_total} refs done" in fn
    assert "Aggregating with ${d.aggregator}" in fn


def test_moa_css_classes_present():
    assert ".moa-reference .thinking-card-label" in STYLE_CSS
    assert ".moa-ref-counter" in STYLE_CSS
    assert ".moa-status" in STYLE_CSS


def test_moa_status_uses_existing_animation_keyframes():
    assert "@keyframes hermes-cursor-blink" in STYLE_CSS
