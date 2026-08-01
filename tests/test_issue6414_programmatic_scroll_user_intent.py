"""Regression for #6414: real upward wheel intent wins during a render scroll guard."""

import json
import re
import subprocess
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
UI_JS = (REPO / "static" / "ui.js").read_text(encoding="utf-8")


SESSIONS_JS = (REPO / "static" / "sessions.js").read_text(encoding="utf-8")


def _function_body(name: str, source: str = UI_JS) -> str:
    marker = f"function {name}"
    start = source.find(marker)
    if start < 0:
        marker = f"async function {name}"
        start = source.index(marker)
    brace = source.index("{", start)
    depth = 0
    for index in range(brace, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[start : index + 1]
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


def test_older_message_fallback_executes_real_slow_render_writer_contract():
    """Exercise the production fallback, rather than retyping its ownership edge."""
    script = f"""
let now = 1000;
const performance = {{ now: () => now }};
let storedTop = 100;
let writeObservation = null;
const container = {{
  scrollHeight: 1000,
  clientHeight: 300,
  get scrollTop() {{ return storedTop; }},
  set scrollTop(value) {{
    storedTop = value;
    writeObservation = {{ value, at: now, fresh: _freshProgrammaticScrollActive() }};
  }},
}};
const document = {{ getElementById: (id) => id === 'messages' ? container : null }};
const window = {{}};
const S = {{
  session: {{ session_id: 'slow-render-session' }},
  messages: [{{ role: 'assistant', content: 'tail' }}],
}};
let _loadingOlder = false;
let _messagesTruncated = true;
let _oldestIdx = 1;
let _messagesGeneration = 0;
let _loadingSessionId = null;
let _messageRenderWindowSize = 50;
let _scrollPinned = true;
let _programmaticScroll = true;
let _programmaticScrollSetAt = now;
const _INITIAL_MSG_LIMIT = 50;
const _msgLimitMax = 100;
const MESSAGE_RENDER_WINDOW_DEFAULT = 50;
const PROGRAMMATIC_SCROLL_VALID_MS = 150;
async function api() {{
  return {{
    session: {{
      messages: [
        {{ role: 'user', content: 'older' }},
        {{ role: 'assistant', content: 'tail' }},
      ],
      _messages_truncated: true,
      _messages_offset: 0,
      tool_calls: [],
    }},
  }};
}}
function _sameTranscriptMessage(a, b) {{ return a && b && a.role === b.role && a.content === b.content; }}
function _syncToolCallsForLoadedMessages() {{}}
function _messageIsRenderable() {{ return true; }}
function msgContent(message) {{ return message && message.content; }}
function _currentMessageRenderWindowSize() {{ return _messageRenderWindowSize; }}
function renderMessages() {{ now += 200; container.scrollHeight = 1300; }}
function _captureMessageViewportAnchor() {{ return null; }}
function _restoreMessageViewportAnchor() {{ return false; }}
function _messageVirtualPrependedHeightDelta() {{ return null; }}
function requestAnimationFrame() {{ return 0; }}
{_function_body('_freshProgrammaticScrollActive')}
{_function_body('_loadOlderMessages', SESSIONS_JS)}

await _loadOlderMessages();
if (!writeObservation) throw new Error('real fallback did not write scrollTop');
if (!writeObservation.fresh) throw new Error('real fallback write inherited a stale latch');
if (writeObservation.at !== 1200) throw new Error('fallback timestamp was not taken after slow render');
now = writeObservation.at + 151;
if (_freshProgrammaticScrollActive()) throw new Error('fallback latch did not expire from its own write');
console.log(JSON.stringify({{ top: storedTop, observation: writeObservation }}));
"""
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        check=True,
        capture_output=True,
        text=True,
    )
    observed = json.loads(result.stdout)
    assert observed["top"] == 400
    assert observed["observation"] == {"value": 400, "at": 1200, "fresh": True}


def test_all_programmatic_scroll_writers_stamp_the_shared_freshness_clock():
    """Keep UI and session writers from silently drifting apart."""
    for source_name, source in (("ui.js", UI_JS), ("sessions.js", SESSIONS_JS)):
        writers = list(re.finditer(r"_programmaticScroll\s*=\s*true\s*;", source))
        assert writers, f"{source_name} has no programmatic-scroll writers"
        for writer in writers:
            tail = source[writer.end() : writer.end() + 160]
            assert re.match(
                r"\s*_programmaticScrollSetAt\s*=\s*performance\.now\(\)\s*;",
                tail,
            ), f"{source_name} writer lacks a paired freshness timestamp"

