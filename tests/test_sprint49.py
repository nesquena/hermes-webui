"""Tests for sprint 49 timestamp footer polish — v0.50.97.

Covers:
  - #680: assistant messages now render footer timestamps, not just user messages
  - messages from prior days render a fuller date+time string in the footer
  - timestamp/action footer stays attached to visible response segments only
  - user and assistant footer chrome is hover-only by default
  - last assistant turn keeps cumulative usage visible and reveals time/actions on hover
  - unchanged historical messages preserve their original timestamps across turns
"""

import pathlib
import re

from api.streaming import _restore_reasoning_metadata


REPO = pathlib.Path(__file__).parent.parent
UI_JS = (REPO / "static" / "ui.js").read_text(encoding="utf-8")
UI_CSS = (REPO / "static" / "style.css").read_text(encoding="utf-8")
STREAMING_PY = (REPO / "api" / "streaming.py").read_text(encoding="utf-8")


def test_footer_timestamp_is_not_limited_to_user_messages():
    assert "const timeHtml = tsTime ?" in UI_JS
    assert "isUser && tsTime" not in UI_JS, (
        "Timestamp footer should no longer be gated to user messages only"
    )


def test_footer_timestamp_uses_richer_format_for_older_messages():
    assert "function _formatMessageFooterTimestamp(tsVal)" in UI_JS
    assert "month:'short'" in UI_JS or 'month: "short"' in UI_JS
    assert "day:'numeric'" in UI_JS or 'day: "numeric"' in UI_JS
    assert "hour:'numeric'" in UI_JS or 'hour: "numeric"' in UI_JS
    assert "minute:'2-digit'" in UI_JS or 'minute: "2-digit"' in UI_JS


def test_timestamp_footer_stays_on_visible_response_segments():
    assert "if(hasVisibleBody){" in UI_JS
    assert 'seg.insertAdjacentHTML(\'beforeend\', `${filesHtml}<div class="msg-body">${bodyHtml}</div>${footHtml}`);' in UI_JS, (
        "Footer timestamp should stay attached to visible response segments"
    )
    assert "else if(!thinkingText){" in UI_JS, (
        "Thinking-only assistant segments should still avoid rendering a footer"
    )


def test_footer_chrome_is_hover_only_for_user_and_assistant_messages():
    assert ".msg-row[data-role=\"user\"] .msg-foot {\n  opacity: 0;" in UI_CSS
    assert ".msg-row[data-role=\"user\"]:hover .msg-foot," in UI_CSS
    assert ".msg-row[data-role=\"assistant\"] .msg-foot," in UI_CSS
    assert ".assistant-turn .msg-foot {" in UI_CSS
    assert ".assistant-turn:hover .msg-foot," in UI_CSS


def test_last_assistant_keeps_usage_visible_and_reveals_time_and_actions_on_hover():
    assert "usage.className='msg-usage-inline';" in UI_JS
    assert "targetFoot.classList.add('msg-foot-with-usage');" in UI_JS
    assert "targetFoot.insertBefore(usage, targetFoot.firstChild);" in UI_JS
    assert ".assistant-turn .msg-foot-with-usage," in UI_CSS
    assert ".msg-row[data-role=\"assistant\"] .msg-foot-with-usage {\n  opacity: 1;" in UI_CSS
    assert ".msg-foot-with-usage .msg-time,\n.msg-foot-with-usage .msg-actions {\n  opacity: 0;" in UI_CSS
    assert ".assistant-turn:hover .msg-foot-with-usage .msg-time," in UI_CSS


def test_restore_reasoning_metadata_preserves_existing_timestamps():
    assert "def _restore_reasoning_metadata(previous_messages, updated_messages):" in STREAMING_PY
    assert "if prev_msg.get('timestamp') and not cur_msg.get('timestamp'):" in STREAMING_PY
    assert "cur_msg['timestamp'] = prev_msg['timestamp']" in STREAMING_PY
    assert "elif prev_msg.get('_ts') and not cur_msg.get('_ts') and not cur_msg.get('timestamp'):" in STREAMING_PY
    assert "cur_msg['_ts'] = prev_msg['_ts']" in STREAMING_PY


def test_restore_reasoning_metadata_preserves_timestamp_on_reload_for_unchanged_messages():
    previous_messages = [
        {"role": "user", "content": "hello", "timestamp": 1713500000},
        {"role": "assistant", "content": "world", "timestamp": 1713500060},
    ]
    updated_messages = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "world"},
    ]

    restored = _restore_reasoning_metadata(previous_messages, updated_messages)

    assert restored[0]["timestamp"] == 1713500000
    assert restored[1]["timestamp"] == 1713500060


def test_restore_reasoning_metadata_does_not_preserve_timestamp_for_changed_messages():
    previous_messages = [
        {"role": "user", "content": "hello", "timestamp": 1713500000},
        {"role": "assistant", "content": "old answer", "timestamp": 1713500060},
    ]
    updated_messages = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "new answer"},
    ]

    restored = _restore_reasoning_metadata(previous_messages, updated_messages)

    assert restored[0]["timestamp"] == 1713500000
    assert "timestamp" not in restored[1]
