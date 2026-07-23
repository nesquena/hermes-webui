"""Regression for #6414: real upward wheel intent wins during a render scroll guard."""

import json
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


def _run_small_upward_wheel_case(programmatic_scroll_age_ms: int) -> dict[str, bool | int]:
    script = f"""
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
let _programmaticScrollSetAt = now - {programmatic_scroll_age_ms};
let _messageUserUnpinned = false;
let _scrollPinned = true;
let _nearBottomCount = 2;
let _lastNonMessageScrollIntentMs = -Infinity;
let _lastMessageScrollIntentMs = -Infinity;
let _lastMessageWheelIntentMs = -Infinity;
let _touchStartY = null;
let _messageTouchScrollActive = false;
let writes = 0;
let cancels = 0;
const _autoScrollFollow = true;
const PROGRAMMATIC_SCROLL_VALID_MS = 150;
function _cancelBottomSettle() {{ cancels += 1; }}
function _markMessageTouchScrollIntent() {{}}
function _recentNonMessageScrollIntent() {{ return false; }}
function _recentMessageScrollIntent() {{ return false; }}
function _recentMessageTouchScrollIntent() {{ return false; }}
function _recentMessageWheelIntent() {{ return now - _lastMessageWheelIntentMs < 1200; }}
function _recentMessageKeyScrollIntent() {{ return false; }}
function _messageBottomDistance() {{ return messages.scrollHeight - messages.scrollTop - messages.clientHeight; }}
function _setMessageScrollToBottom() {{ writes += 1; }}
function _settleMessageScrollToBottom() {{ writes += 1; }}
{_function_body('_freshProgrammaticScrollActive')}
{_function_body('_recordNonMessageScrollIntent')}
{_function_body('scrollIfPinned')}

_recordNonMessageScrollIntent({{
  type: 'wheel',
  target: messages,
  deltaY: -5,
}});
scrollIfPinned();
console.log(JSON.stringify({{
  cancels,
  writes,
  programmaticScroll: _programmaticScroll,
  messageUserUnpinned: _messageUserUnpinned,
  scrollPinned: _scrollPinned,
}}));
"""
    result = subprocess.run(
        ["node", "-e", script],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def test_small_upward_wheel_unpins_during_fresh_programmatic_scroll_guard():
    """A capture-phase wheel event must beat the scroll listener's early return.

    Small trackpad deltas are intentionally below the ordinary sticky-unpin
    threshold. They still represent a reader trying to leave the live tail when
    a render has armed `_programmaticScroll`; otherwise the listener returns and
    the next stream tick pulls the reader back down.
    """

    result = _run_small_upward_wheel_case(programmatic_scroll_age_ms=40)
    assert result["messageUserUnpinned"] is True
    assert result["scrollPinned"] is False
    assert result["cancels"] == 1
    assert result["writes"] == 0


def test_small_upward_wheel_does_not_unpin_after_programmatic_guard_stales():
    """A stale latch must fall back to the ordinary low-delta wheel threshold."""

    result = _run_small_upward_wheel_case(programmatic_scroll_age_ms=200)
    assert result["messageUserUnpinned"] is False
    assert result["scrollPinned"] is True
    assert result["programmaticScroll"] is False
    assert result["cancels"] == 0
