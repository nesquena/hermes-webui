"""Static-analysis contract tests for the DOM-INFLIGHT reattach sync invariant (#3040).

Every site that writes to INFLIGHT[] must persist the change so that a reconnect
or session-switch can seed the new closure from durable state rather than a stale
DOM snapshot. These tests pin the structural invariants to catch regressions that
remove a persistence call or add a new INFLIGHT write without pairing it with one.
"""
from __future__ import annotations

import re
from pathlib import Path

MESSAGES_JS = (Path(__file__).resolve().parent.parent / "static" / "messages.js").read_text(
    encoding="utf-8"
)
_LINES = MESSAGES_JS.splitlines()


def _line_window(line_no: int, radius: int = 20) -> str:
    """Return up to `radius` lines on either side of `line_no` (1-based) as one string."""
    start = max(0, line_no - 1 - radius)
    end = min(len(_LINES), line_no - 1 + radius + 1)
    return "\n".join(_LINES[start:end])


def _inflight_write_line_numbers() -> list[int]:
    """Return 1-based line numbers for every INFLIGHT[activeSid] assignment site."""
    results = []
    for i, line in enumerate(_LINES, start=1):
        if re.search(r"INFLIGHT\[activeSid\]\s*=\s*\{", line) or re.search(
            r"INFLIGHT\[activeSid\]\s*\|\|\s*\(INFLIGHT\[activeSid\]\s*=\s*\{", line
        ):
            results.append(i)
    return results


def test_inflight_write_sites_exist():
    """Sanity-check: messages.js must contain at least 3 INFLIGHT[activeSid] write sites."""
    sites = _inflight_write_line_numbers()
    assert len(sites) >= 3, (
        f"Expected at least 3 INFLIGHT[activeSid] assignment sites, found {len(sites)}. "
        "If send() was refactored, update the expected count."
    )


def _extract_send_body() -> str:
    """Extract the body of the send() function up to attachLiveStream() as one string."""
    # send() starts with `async function send(` and the INFLIGHT writes in the
    # optimistic path and catch block both precede the attachLiveStream call.
    send_start = MESSAGES_JS.find("async function send(")
    assert send_start != -1, "async function send() not found in messages.js."
    attach_call = MESSAGES_JS.find("attachLiveStream(activeSid, streamId", send_start)
    assert attach_call != -1, "attachLiveStream call not found inside send()."
    return MESSAGES_JS[send_start:attach_call]


def test_send_inflight_writes_are_paired_with_saveInflightState():
    """The INFLIGHT[activeSid] assignments in send() must be covered by saveInflightState()
    calls within the same send() scope.

    send() makes the optimistic UI write before calling /api/chat/start so that a tab
    reload during the network round-trip can restore the user's message. If the write
    is not persisted, the only recovery window is lost.

    There are 3 write sites but only 2 saveInflightState calls because the catch-block
    fallback write (line ~530) shares its persistence call with the post-start path:
    if /api/chat/start succeeds, the post-start block's saveInflightState covers it;
    if /api/chat/start fails, the error handler deletes INFLIGHT[activeSid] anyway.
    A minimum of 2 saveInflightState calls pins this intentional design.
    """
    send_body = _extract_send_body()

    # Count how many INFLIGHT assignments appear in send() before attachLiveStream.
    write_count = len(re.findall(r"INFLIGHT\[activeSid\]\s*=\s*\{", send_body))
    assert write_count >= 2, (
        f"Expected at least 2 INFLIGHT[activeSid] write sites in send(), found {write_count}. "
        "The optimistic write and the catch-block fallback should both be present."
    )

    # There must be at least 2 saveInflightState calls covering the write paths.
    # (The catch-block fallback write shares the post-start persistence call.)
    persist_count = send_body.count("saveInflightState(")
    assert persist_count >= 2, (
        f"send() has only {persist_count} saveInflightState() call(s) before attachLiveStream "
        "(expected at least 2: one before /api/chat/start for optimistic reload recovery, "
        "one after start for stream-id persistence). "
        "Removing either leaves a reload window with unrecoverable INFLIGHT state."
    )


def test_syncInflightAssistantMessage_calls_throttled_persist():
    """syncInflightAssistantMessage() must call _throttledPersist() to flush the live
    assistant text into persistent INFLIGHT state on every token and interim_assistant event."""
    fn_start = MESSAGES_JS.find("function syncInflightAssistantMessage(){")
    assert fn_start != -1, "syncInflightAssistantMessage() not found in messages.js."
    fn_body_start = MESSAGES_JS.find("{", fn_start)
    # Walk braces to find the closing brace of the function body.
    depth = 1
    i = fn_body_start + 1
    while i < len(MESSAGES_JS) and depth:
        if MESSAGES_JS[i] == "{":
            depth += 1
        elif MESSAGES_JS[i] == "}":
            depth -= 1
        i += 1
    fn_body = MESSAGES_JS[fn_body_start:i]
    assert "_throttledPersist()" in fn_body, (
        "syncInflightAssistantMessage() must call _throttledPersist() so each "
        "live-text update is scheduled for persistence. Without this, a reconnect "
        "seeds from the last tool-event snapshot and loses all interim assistant text."
    )


def test_interim_assistant_calls_syncInflightAssistantMessage_after_accumulating_text():
    """The interim_assistant event handler must call syncInflightAssistantMessage()
    after it appends to assistantText and visibleInterimSnippets.

    Without this call, the INFLIGHT assistant message is not updated before a
    potential reconnect, causing already-streamed content to be lost.
    """
    handler_start = MESSAGES_JS.find("source.addEventListener('interim_assistant',")
    assert handler_start != -1, "interim_assistant event listener not found in messages.js."
    # Extract the handler body up to the matching closing brace.
    brace_pos = MESSAGES_JS.find("{", handler_start)
    depth = 1
    i = brace_pos + 1
    while i < len(MESSAGES_JS) and depth:
        if MESSAGES_JS[i] == "{":
            depth += 1
        elif MESSAGES_JS[i] == "}":
            depth -= 1
        i += 1
    handler_body = MESSAGES_JS[brace_pos:i]

    assert "assistantText" in handler_body, (
        "interim_assistant handler does not reference assistantText — handler may have been refactored."
    )
    assert "visibleInterimSnippets" in handler_body, (
        "interim_assistant handler does not reference visibleInterimSnippets — "
        "interim snippet tracking was removed."
    )
    # syncInflightAssistantMessage() must appear AFTER the accumulation lines.
    accum_pos = handler_body.find("visibleInterimSnippets.push(")
    sync_pos = handler_body.find("syncInflightAssistantMessage()")
    assert sync_pos != -1, (
        "interim_assistant handler does not call syncInflightAssistantMessage(). "
        "Without this, INFLIGHT is not updated with interim text and a reconnect loses progress."
    )
    assert accum_pos != -1 and sync_pos > accum_pos, (
        "syncInflightAssistantMessage() must appear AFTER visibleInterimSnippets.push() "
        "so that the persisted snapshot includes the freshly accumulated snippet."
    )


def test_attachLiveStream_reconnect_seeds_from_INFLIGHT_not_querySelector():
    """On reconnect, attachLiveStream() must seed assistantText from INFLIGHT state,
    not from a live DOM query (querySelector).

    Seeding from the DOM is fragile: an off-screen or BFCache-restored session may
    have no rendered content, so the closure would start empty and duplicate tokens
    would overwrite the already-displayed response.
    """
    fn_start = MESSAGES_JS.find("function attachLiveStream(")
    assert fn_start != -1, "attachLiveStream() not found in messages.js."
    # Capture up to 60 lines after the function signature for the seed section.
    fn_head_end = fn_start
    for _ in range(60):
        fn_head_end = MESSAGES_JS.find("\n", fn_head_end + 1)
        if fn_head_end == -1:
            break
    seed_section = MESSAGES_JS[fn_start:fn_head_end]

    # The reconnect seed must come from INFLIGHT[activeSid].
    assert "INFLIGHT[activeSid]" in seed_section, (
        "attachLiveStream() seed section does not reference INFLIGHT[activeSid]. "
        "Reconnect must read accumulated text from durable INFLIGHT state."
    )
    assert "reconnecting" in seed_section, (
        "attachLiveStream() seed section does not branch on `reconnecting`. "
        "The reconnect path must distinguish a fresh open from a re-attach."
    )
    # The seed must NOT use querySelector to read DOM content for the initial text.
    idx = seed_section.find("let assistantText")
    assert idx != -1, "let assistantText not found in seed section — capture window too narrow or variable renamed"
    seed_up_to_assistantText = seed_section[:idx]
    assert "querySelector" not in seed_up_to_assistantText, (
        "attachLiveStream() reads DOM via querySelector before seeding assistantText. "
        "Use INFLIGHT state exclusively; DOM queries are unreliable for off-screen sessions."
    )


def test_syncInflightAssistantMessage_exists_as_closure_inside_attachLiveStream():
    """syncInflightAssistantMessage() must be defined as a closure inside attachLiveStream()
    so it closes over the live assistantText and reasoningText accumulators.

    If it were a module-level function it could not access the per-stream state,
    breaking the token→INFLIGHT sync for every concurrent stream.
    """
    attach_start = MESSAGES_JS.find("function attachLiveStream(")
    assert attach_start != -1, "attachLiveStream() not found."
    # syncInflightAssistantMessage definition must appear after attachLiveStream opens.
    sync_def = MESSAGES_JS.find("function syncInflightAssistantMessage(){", attach_start)
    assert sync_def != -1, (
        "syncInflightAssistantMessage() is not defined inside attachLiveStream(). "
        "It must be a closure over the stream accumulators (assistantText, reasoningText)."
    )
    # And there must NOT be a top-level definition before attachLiveStream.
    top_level_def = MESSAGES_JS.find("function syncInflightAssistantMessage(){")
    assert top_level_def == sync_def, (
        "syncInflightAssistantMessage() has a top-level definition. "
        "It must live inside attachLiveStream() to close over per-stream state."
    )
