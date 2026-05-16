"""Regression test for stage-364 Opus-caught SHOULD-FIX:

When the live SSE stream errors out mid-stream and the frontend falls back
to journal replay, live frames emitted by `_sse()` in `api/streaming.py`
have no `id:` field. The frontend's `_lastRunJournalSeq` therefore stays
at 0 during the live phase. Without resetting accumulators, the replay
(which arrives with `after_seq=0`, i.e. all events from seq 1) double-
renders every token because `assistantText` already holds the live phase's
accumulated text.

The fix in `static/messages.js` resets `assistantText`, `reasoningText`,
`liveReasoningText`, `segmentStart`, and sets `_smdReconnect=true` before
opening the replay EventSource. This regression test asserts the reset
block exists in source.

This is a static-grep test scoped to the bug surface (per Pitfall 2 / 5
from test-augmentation-and-validation-pitfalls.md). The behavioral
end-to-end test would require driving a real EventSource lifecycle in
the browser — out of scope for the pytest suite. The grep is wide enough
(checks all 4 reset lines + `_smdReconnect=true`) that a future refactor
moving the reset would still trigger.
"""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
MESSAGES_JS = REPO_ROOT / "static" / "messages.js"


def _read_messages_js() -> str:
    return MESSAGES_JS.read_text(encoding="utf-8")


def test_replay_resets_assistant_text_accumulator():
    src = _read_messages_js()
    # Find the `if(st.replay_available)` block in the error-reconnect handler
    idx = src.find("if(st.replay_available){")
    assert idx != -1, "replay_available branch not found in messages.js"
    # The reset block must come before the next EventSource construction
    block = src[idx:idx + 1200]
    assert "assistantText=''" in block, (
        "Replay branch must reset assistantText to '' before opening replay "
        "EventSource — otherwise live-phase text doubles when journal replay "
        "arrives with after_seq=0 (Opus-caught stage-364 SHOULD-FIX)"
    )
    assert "reasoningText=''" in block, "Replay branch must reset reasoningText"
    assert "liveReasoningText=''" in block, "Replay branch must reset liveReasoningText"


def test_replay_sets_smd_reconnect_to_force_dom_reset():
    src = _read_messages_js()
    idx = src.find("if(st.replay_available){")
    block = src[idx:idx + 1200]
    assert "_smdReconnect=true" in block, (
        "Replay branch must set _smdReconnect=true so the next live token "
        "clears assistantBody.innerHTML (matching the reset accumulator)"
    )


def test_replay_resets_segment_start():
    src = _read_messages_js()
    idx = src.find("if(st.replay_available){")
    block = src[idx:idx + 1200]
    assert "segmentStart=0" in block, (
        "Replay branch must reset segmentStart to 0 so the new segment "
        "starts at the new assistantText origin"
    )


def test_resets_precede_new_eventsource_construction():
    """The resets must happen BEFORE `new EventSource(...)`, not after."""
    src = _read_messages_js()
    idx = src.find("if(st.replay_available){")
    block = src[idx:idx + 1200]
    reset_idx = block.find("assistantText=''")
    eventsource_idx = block.find("new EventSource")
    assert reset_idx < eventsource_idx, (
        f"Reset (idx {reset_idx}) must occur BEFORE EventSource construction "
        f"(idx {eventsource_idx}) — otherwise the very first replay token "
        f"would still race against the unreset accumulator"
    )
