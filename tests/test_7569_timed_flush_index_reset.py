"""#7569 review fix: timed-flush must not leave the folded index stale.

The ~10 Hz reasoning throttle in the stream callback clears the coalescing
buffer before emitting:

    put('reasoning', {'text': _reasoning_buffer[0]})
    _reasoning_buffer[0] = ''
    # ← _reasoning_buffer_index.reset() was missing here

``_CompactEchoIndex`` records raw offsets *relative to the buffer's accumulated
length*. Clearing the buffer without resetting the index leaves it describing
text that is no longer there, so the next ``cut_to`` returns a raw offset that
no longer corresponds to the buffer — the live-echo strip then truncates the
buffer at a phantom position and falsely marks the interim as
``reasoning_echo: true`` (leaks a truncated copy into both the SSE stream and
the durable journal).

This file pins the invariant at the *call site*, not just on the index class:
revert the fix commit and the first test fails.
"""

import pathlib
import re

REPO = pathlib.Path(__file__).resolve().parent.parent
STREAMING = (REPO / "api" / "streaming.py").read_text(encoding="utf-8")


def _timed_flush_block() -> str:
    """The ``if now - _reasoning_last_put[0] >= 0.1:`` throttle branch body."""
    match = re.search(
        r"if now - _reasoning_last_put\[0\] >= 0\.1:\n(.*?)(?=\n\s{16}(?:# Track reasoning deltas|_metering_reasoning_deltas))",
        STREAMING,
        re.DOTALL,
    )
    assert match, "timed-flush throttle branch not found in api/streaming.py"
    return match.group(1)


def test_timed_flush_clears_index_together_with_the_buffer():
    """Clearing the reasoning buffer must also reset the folded index, in the
    same throttle branch, after the clear (ordering matters: the reset cannot
    sit in an unrelated branch or before the clear)."""
    block = _timed_flush_block()

    assert "_reasoning_buffer[0] = ''" in block, (
        "timed-flush branch must clear the coalesced reasoning buffer"
    )
    assert "_reasoning_buffer_index.reset()" in block, (
        "timed-flush branch must reset the folded index when it clears the "
        "buffer — leaving the index stale makes later cut_to() offsets "
        "describe text that is no longer in the buffer (#7569 re-gate)"
    )
    # reset must come AFTER the clear, and be at the same indent depth (8
    # spaces inside the throttle branch), not in a sibling branch.
    clear_pos = block.index("_reasoning_buffer[0] = ''")
    reset_pos = block.index("_reasoning_buffer_index.reset()")
    assert reset_pos > clear_pos, (
        "reset must follow the buffer clear, so the next append starts from "
        "an empty raw-length base"
    )
    assert re.search(r"^\s{20}_reasoning_buffer_index\.reset\(\)", block, re.M), (
        "reset must be inside the timed-flush branch (deeper than the clear), "
        "not in an outer/sibling scope"
    )


def test_all_buffer_clears_are_index_safe():
    """Every site that zeroes ``_reasoning_buffer[0]`` must reset the folded
    index immediately after — grep the codebase for clear sites and require
    each to be paired with the index reset."""
    for clear_match in re.finditer(r"_reasoning_buffer\[0\] = ''", STREAMING):
        window = STREAMING[clear_match.start(): clear_match.start() + 400]
        reset_present = re.search(
            r"_reasoning_buffer_index\.reset\(\)", window
        )
        assert reset_present, (
            "a buffer clear at api/streaming.py has no adjacent "
            "_reasoning_buffer_index.reset() — stale offsets would corrupt "
            "the next cut_to() call"
        )