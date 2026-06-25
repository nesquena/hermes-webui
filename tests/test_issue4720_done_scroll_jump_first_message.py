"""Regression for #4720: transcript jumps to the first message after completion.

Root cause: the `done` SSE handler in static/messages.js replaced the transcript
with the full payload and updated `_messagesTruncated`, but did NOT reset
`_oldestIdx` from `d.session._messages_offset` the way the canonical full-load
paths do (sessions.js `_ensureMessagesLoaded`, ui.js `loadSession`). The #4613
scroll restore keys on an absolute index (`sessionIdx = _oldestIdx + rawIdx`);
leaving `_oldestIdx` stale after a truncated initial load desynchronized that
anchor once the done handler expands the render window to all messages, so the
viewport jumped to the first message on every completion.

These tests assert the one-line symmetry fix is present in the done handler and
that it behaves correctly (full payload -> offset 0; explicit offset honored).
"""

import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
MESSAGES_JS = (REPO / "static" / "messages.js").read_text(encoding="utf-8")


def _compact(text: str) -> str:
    return "".join(text.split())


def test_done_handler_resets_oldest_idx_from_payload_offset():
    """The done handler must reset _oldestIdx alongside _messagesTruncated."""
    compact = _compact(MESSAGES_JS)
    # The truncated flag and the offset reset must be wired off the SAME done
    # payload (d.session), mirroring sessions.js / ui.js full-load paths.
    assert "_messagesTruncated=!!d.session._messages_truncated" in compact, (
        "done handler should still set _messagesTruncated from the done payload"
    )
    assert "_oldestIdx=d.session._messages_offset||0" in compact, (
        "#4720: done handler must reset _oldestIdx from the done payload offset "
        "so the absolute scroll anchor stays valid after the render-window expansion"
    )


def test_done_handler_oldest_idx_reset_is_guarded_and_ordered_before_filter():
    """Reset must be typeof-guarded and happen before the messages are re-filtered/rendered."""
    compact = _compact(MESSAGES_JS)
    assert "if(typeof_oldestIdx!=='undefined')_oldestIdx=d.session._messages_offset||0" in compact, (
        "_oldestIdx reset should be typeof-guarded like _messagesTruncated"
    )
    # The reset must precede _filterRecoveryControlMessages (which precedes the
    # done-path renderMessages), so the anchor coordinate system is correct when
    # the transcript is rebuilt.
    reset_idx = compact.index("_oldestIdx=d.session._messages_offset||0")
    filter_idx = compact.index("S.messages=_filterRecoveryControlMessages")
    assert reset_idx < filter_idx, (
        "_oldestIdx must be reset before the done-path re-filter/render"
    )


def test_oldest_idx_reset_matches_full_load_offset_semantics():
    """Execute the reset expression to confirm offset semantics (full payload -> 0)."""
    script = """
const assert = require('assert');
function applyReset(doneSession) {
  let _oldestIdx = 7;  // stale value from a truncated initial load
  // mirror the done-handler line exactly:
  if (typeof _oldestIdx !== 'undefined') _oldestIdx = doneSession._messages_offset || 0;
  return _oldestIdx;
}
// Full transcript payload (no offset field) -> reset to 0.
assert.strictEqual(applyReset({ messages: [1, 2, 3] }), 0);
// Explicit zero offset -> 0.
assert.strictEqual(applyReset({ _messages_offset: 0 }), 0);
// Explicit non-zero offset is honored (defensive; current done payload is full).
assert.strictEqual(applyReset({ _messages_offset: 12 }), 12);
"""
    subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)
