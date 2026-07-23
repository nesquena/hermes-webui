# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0

"""#6419: mid-stream reconnect must keep pending user message before live response."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
UI_JS = (ROOT / "static" / "ui.js").read_text(encoding="utf-8")
SESSIONS_JS = (ROOT / "static" / "sessions.js").read_text(encoding="utf-8")


def _function_body(src: str, name: str) -> str:
    start = src.find(f"function {name}(")
    assert start != -1, f"{name} not found"
    brace = src.find("{", start)
    depth = 0
    for i, ch in enumerate(src[brace:]):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return src[brace : brace + i + 1]
    raise AssertionError(f"{name} body unterminated")


# ─── Shared helper contract ────────────────────────────────────────────────

def test_merge_pending_session_message_is_a_global_helper():
    """The fix must expose _mergePendingSessionMessage as a single, top-level
    identity-aware helper rather than duplicated insertion logic nested inside
    individual recovery paths."""
    import re

    # Exactly one definition in sessions.js.
    defs = [m.start() for m in re.finditer(r"\bfunction _mergePendingSessionMessage\b", SESSIONS_JS)]
    assert len(defs) == 1, (
        f"_mergePendingSessionMessage should be defined exactly once; found {len(defs)}. "
        "The fix must consolidate helper logic at one chokepoint, not duplicate it."
    )
    # The sole definition must be at a top-level scope (zero indentation), not nested.
    line_start = SESSIONS_JS.rfind("\n", 0, defs[0]) + 1
    indent = len(SESSIONS_JS[line_start:defs[0]]) - len(SESSIONS_JS[line_start:defs[0]].lstrip())
    assert indent == 0, (
        f"_mergePendingSessionMessage must be at module top level (indent=0); found indent={indent}."
    )


def test_merge_helper_inserts_pending_user_before_live_assistant():
    """Given a live assistant row, the pending user row must appear before it."""
    body = _function_body(SESSIONS_JS, "_mergePendingSessionMessage")
    assert "getPendingSessionMessage" in body, "helper derives the candidate row"
    assert "findIndex(m=>m&&m.role==='assistant'&&m._live)" in body, (
        "helper looks for the live assistant boundary"
    )
    assert "messages.splice(liveAssistantIdx,0,pendingMsg)" in body, (
        "helper inserts pending user before the live assistant"
    )
    assert "messages.push(pendingMsg)" in body, (
        "helper falls back to append only when there is no live assistant"
    )
    assert "_hasCurrentTailUserDuplicate" in body, (
        "helper deduplicates against the current tail user row"
    )


def test_refreshSession_uses_shared_helper():
    """refreshSession() must no longer append pending_user_message at the end
    unconditionally; it must route through _mergePendingSessionMessage so that a
    live assistant tail during recovery keeps the user prompt before it."""
    body = _function_body(UI_JS, "refreshSession")
    assert "_mergePendingSessionMessage" in body, (
        "refreshSession uses the shared identity-aware merge helper"
    )
    assert body.find("_mergePendingSessionMessage") < body.find("renderMessages"), (
        "merge must happen before the transcript is re-rendered"
    )
    # The old unconditional push must be gone.
    assert "if(pendingMsg) S.messages.push(pendingMsg)" not in body, (
        "refreshSession must not unconditionally push the pending message"
    )


# ─── loadSession reattach path (was already correct before #6419) ─────────────────

def test_loadSession_inflight_reattach_merges_pending_user_before_render():
    """Regression for the #2341 contract: loadSession INFLIGHT branch must call
    the shared helper and render afterwards."""
    start = SESSIONS_JS.find("if(INFLIGHT[sid]){")
    assert start != -1, "loadSession INFLIGHT branch not found"
    end = SESSIONS_JS.find("}else{", start)
    assert end != -1, "loadSession INFLIGHT branch end not found"
    block = SESSIONS_JS[start:end]

    merge_pos = block.find("_mergePendingSessionMessage")
    render_pos = block.find("renderMessages(")
    assert merge_pos != -1, "INFLIGHT branch must merge pending user message"
    assert render_pos != -1, "INFLIGHT branch must render messages"
    assert merge_pos < render_pos, (
        "pending user row must be merged before renderMessages() rebuilds the transcript"
    )
