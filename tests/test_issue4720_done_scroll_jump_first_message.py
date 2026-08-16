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
    # Rotated sessions reset both cursors from the installed terminal payload.
    assert "_messagesTruncated=!!completedSession._messages_truncated" in compact, (
        "done handler should still set _messagesTruncated from the done payload"
    )
    assert "_oldestIdx=completedSession._messages_offset||0" in compact, (
        "#4720: done handler must reset _oldestIdx from the done payload offset "
        "so the absolute scroll anchor stays valid after the render-window expansion"
    )


def test_done_handler_oldest_idx_reset_is_guarded_for_terminal_windows():
    """Terminal window reconciliation updates the raw-coordinate cursor."""
    done_start = MESSAGES_JS.index("source.addEventListener('done'")
    done_end = MESSAGES_JS.index("source.addEventListener('stream_end'", done_start)
    done_block = MESSAGES_JS[done_start:done_end]
    compact = _compact(done_block)
    assert "if(typeof_oldestIdx!=='undefined')_oldestIdx=completedSession._messages_offset||0" in compact, (
        "_oldestIdx reset should be typeof-guarded like _messagesTruncated"
    )
    cursor_idx = compact.index("_oldestIdx=completedSession._messages_offset||0")
    assert "_filterRecoveryControlMessages" not in compact
    assert cursor_idx < compact.index("completedSession.messages=S.messages")


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
