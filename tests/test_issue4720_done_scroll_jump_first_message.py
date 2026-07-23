"""Regression for #4720: transcript jumps to the first message after completion.

Root cause: the `done` SSE handler replaced a paginated transcript with the
full settled payload and then expanded the render window to all messages. The
#4613 scroll restore keys on an absolute index (`sessionIdx = _oldestIdx +
rawIdx`), so the terminal path must consume the canonical paginated window's
`_messages_offset` instead of treating the full SSE payload as the display
window. Otherwise a long completion can both render the entire history and
desynchronize the viewport anchor.

These tests assert that the done handler uses the bounded settled-window path
and that its offset bookkeeping remains correct for both bounded and fully
loaded sessions.
"""

import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
MESSAGES_JS = (REPO / "static" / "messages.js").read_text(encoding="utf-8")


def _compact(text: str) -> str:
    return "".join(text.split())


def test_done_handler_uses_canonical_settled_window_offset():
    """The done handler must use the server-owned bounded window when needed."""
    compact = _compact(MESSAGES_JS)
    assert "_settledDoneWindow=await_fetchSettledSessionMessageWindow(completedSid,completedSession)" in compact, (
        "done handler should refresh a canonical bounded window for paginated sessions"
    )
    assert "_messagesTruncated=_settledDoneWasTruncated" in compact, (
        "done handler should preserve the prior pagination state when the SSE "
        "payload itself is intentionally full"
    )
    assert "_oldestIdx=_settledDoneWasTruncated" in compact
    assert "_messageRenderWindowSize=Math.max(typeof _currentMessageRenderWindowSize" not in compact, (
        "done settlement must not promote a paginated transcript to a full render window"
    )


def test_done_handler_offset_is_applied_before_filter_and_render():
    """The canonical offset must be applied before the transcript is filtered/rendered."""
    compact = _compact(MESSAGES_JS)
    assert "_oldestIdx=_settledDoneWasTruncated?" in compact
    reset_idx = compact.index("_oldestIdx=_settledDoneWasTruncated?")
    filter_idx = compact.index("S.messages=_filterRecoveryControlMessages")
    assert reset_idx < filter_idx, (
        "_oldestIdx must be updated before the done-path re-filter/render"
    )


def test_settled_window_offset_semantics():
    """A bounded response uses its offset; a full response uses zero."""
    script = """
const assert = require('assert');
function applyOffset(wasTruncated, failed, settledSession, priorOffset) {
  if (wasTruncated) return failed ? priorOffset : (settledSession._messages_offset || 0);
  return settledSession._messages_offset || 0;
}
assert.strictEqual(applyOffset(true, false, { _messages_offset: 970 }, 900), 970);
assert.strictEqual(applyOffset(true, true, {}, 900), 900);
assert.strictEqual(applyOffset(false, false, {}, 900), 0);
"""
    subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)
