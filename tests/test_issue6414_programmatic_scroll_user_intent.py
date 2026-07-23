"""Regression for #6414: real upward wheel intent wins during a render scroll guard."""

import subprocess
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
UI_JS = (REPO / "static" / "ui.js").read_text(encoding="utf-8")


def _function_body(name: str) -> str:
    marker = f"function {name}"
    start = UI_JS.index(marker)
    brace = UI_JS.index("{", start)
    depth = 0
    for index in range(brace, len(UI_JS)):
        if UI_JS[index] == "{":
            depth += 1
        elif UI_JS[index] == "}":
            depth -= 1
            if depth == 0:
                return UI_JS[start : index + 1]
    raise AssertionError(f"{name} did not terminate")


def test_small_upward_wheel_unpins_during_programmatic_scroll_guard():
    """A capture-phase wheel event must beat the scroll listener's early return.

    Small trackpad deltas are intentionally below the ordinary sticky-unpin
    threshold. They still represent a reader trying to leave the live tail when
    a render has armed `_programmaticScroll`; otherwise the listener returns and
    the next stream tick pulls the reader back down.
    """

    script = f"""
const assert = require('assert');
let now = 1000;
const performance = {{ now: () => now }};
const messages = {{
  scrollHeight: 5000,
  scrollTop: 4400,
  clientHeight: 500,
  contains: (target) => target === messages,
}};
const document = {{ getElementById: (id) => id === 'messages' ? messages : null }};
let _programmaticScroll = true;
let _messageUserUnpinned = false;
let _scrollPinned = true;
let _nearBottomCount = 2;
let _lastNonMessageScrollIntentMs = -Infinity;
let _lastMessageScrollIntentMs = -Infinity;
let _lastMessageWheelIntentMs = -Infinity;
let _touchStartY = null;
let _messageTouchScrollActive = false;
let writes = 0;
const _autoScrollFollow = true;
function _cancelBottomSettle() {{}}
function _markMessageTouchScrollIntent() {{}}
function _recentNonMessageScrollIntent() {{ return false; }}
function _recentMessageScrollIntent() {{ return false; }}
function _recentMessageTouchScrollIntent() {{ return false; }}
function _recentMessageWheelIntent() {{ return now - _lastMessageWheelIntentMs < 1200; }}
function _recentMessageKeyScrollIntent() {{ return false; }}
function _messageBottomDistance() {{ return messages.scrollHeight - messages.scrollTop - messages.clientHeight; }}
function _setMessageScrollToBottom() {{ writes += 1; }}
function _settleMessageScrollToBottom() {{ writes += 1; }}
{_function_body('_recordNonMessageScrollIntent')}
{_function_body('scrollIfPinned')}

_recordNonMessageScrollIntent({{
  type: 'wheel',
  target: messages,
  deltaY: -5,
}});
assert.strictEqual(_messageUserUnpinned, true);
assert.strictEqual(_scrollPinned, false);
scrollIfPinned();
assert.strictEqual(writes, 0);
"""
    subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)
