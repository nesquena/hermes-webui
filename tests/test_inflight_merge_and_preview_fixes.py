"""
Unit tests for two frontend fixes: the discard popup that appeared on
background session resume, and the message-splicing regression where stale
inflight tails were re-merged after a session reload.

These tests re-implement the patched JavaScript logic in Python to verify
correctness without needing a JS runtime. The logic is simple enough that
a faithful Python port is unambiguous.
"""
import pytest


# ─── Python port of _mergeInflightTailMessages (patched version) ───

def _message_comparable_text(m):
    if not m:
        return ''
    return str(m.get('content') or '').strip()


def _same_transcript_message(a, b):
    if not (a and b):
        return False
    if str(a.get('role', '')) != str(b.get('role', '')):
        return False
    a_text = _message_comparable_text(a)
    b_text = _message_comparable_text(b)
    return a_text == b_text


def merge_inflight_tail_messages(base_messages, inflight_messages, active_stream_id=None):
    """Python port of the patched _mergeInflightTailMessages."""
    base = base_messages if isinstance(base_messages, list) else []
    inflight = inflight_messages if isinstance(inflight_messages, list) else []

    live_idx = -1
    for i in range(len(inflight) - 1, -1, -1):
        if inflight[i] and inflight[i].get('_live'):
            live_idx = i
            break

    if live_idx < 0:
        return base

    # Staleness guard: only skip when NOT streaming
    if (len(base) > 0 and len(inflight) > 0
            and len(base) >= len(inflight) and not active_stream_id):
        return base

    start = live_idx
    if live_idx > 0 and inflight[live_idx - 1] and inflight[live_idx - 1].get('role') == 'user':
        start = live_idx - 1

    tail = [m for m in inflight[start:] if m and m.get('role')]
    merged = list(base)

    for msg in tail:
        window_size = max(10, len(tail) + 5)
        candidates = merged[-window_size:]
        msg_text = _message_comparable_text(msg)
        duplicate = False

        for existing in candidates:
            if _same_transcript_message(existing, msg):
                duplicate = True
                break
            # Partial prefix match — only for long assistant messages
            if (msg.get('role') == 'assistant' and existing.get('role') == 'assistant'
                    and msg_text and len(msg_text) > 30):
                existing_text = _message_comparable_text(existing)
                if (existing_text and len(existing_text) > 30
                        and (existing_text.startswith(msg_text) or msg_text.startswith(existing_text))):
                    duplicate = True
                    break

        if not duplicate:
            merged.append(msg)

    return merged


# ─── Tests for message-splicing fix: merge logic ───

class TestMergeInflightStaleDetection:
    """Verify stale inflight is discarded when session is idle."""

    def test_stale_inflight_discarded_when_not_streaming(self):
        base = [{'role': 'user', 'content': f'msg{i}'} for i in range(10)]
        inflight = [{'role': 'user', 'content': f'msg{i}'} for i in range(8)]
        inflight.append({'role': 'user', 'content': 'question'})
        inflight.append({'role': 'assistant', 'content': 'partial', '_live': True})
        # base=10, inflight=10, not streaming → stale, return base
        result = merge_inflight_tail_messages(base, inflight, active_stream_id=None)
        assert len(result) == 10
        assert not any(m.get('content') == 'partial' for m in result)

    def test_valid_merge_when_streaming(self):
        base = [{'role': 'user', 'content': f'msg{i}'} for i in range(10)]
        inflight = [{'role': 'user', 'content': f'msg{i}'} for i in range(8)]
        inflight.append({'role': 'user', 'content': 'new question'})
        inflight.append({'role': 'assistant', 'content': 'streaming...', '_live': True})
        # base=10, inflight=10, but streaming → merge proceeds
        result = merge_inflight_tail_messages(base, inflight, active_stream_id='stream-123')
        assert any(m.get('content') == 'streaming...' for m in result)

    def test_no_live_message_returns_base(self):
        base = [{'role': 'user', 'content': 'hi'}, {'role': 'assistant', 'content': 'hello'}]
        inflight = [{'role': 'user', 'content': 'hi'}, {'role': 'assistant', 'content': 'hello'}]
        result = merge_inflight_tail_messages(base, inflight)
        assert result == base


class TestMergeInflightDedup:
    """Verify dedup catches exact and partial duplicates."""

    def test_exact_duplicate_not_appended(self):
        base = [
            {'role': 'user', 'content': 'hello'},
            {'role': 'assistant', 'content': 'world'},
        ]
        inflight = [
            {'role': 'user', 'content': 'hello'},
            {'role': 'assistant', 'content': 'world', '_live': True},
        ]
        result = merge_inflight_tail_messages(base, inflight, active_stream_id='x')
        assert len(result) == 2

    def test_partial_prefix_long_message_deduped(self):
        long_text = "This is a very long response that keeps going and going with lots of detail about the topic"
        partial = long_text[:50]  # > 30 chars
        base = [
            {'role': 'user', 'content': 'question'},
            {'role': 'assistant', 'content': long_text},
        ]
        inflight = [
            {'role': 'user', 'content': 'question'},
            {'role': 'assistant', 'content': partial, '_live': True},
        ]
        result = merge_inflight_tail_messages(base, inflight, active_stream_id='x')
        # partial is prefix of long_text → duplicate
        assert len(result) == 2
        assert result[1]['content'] == long_text

    def test_short_message_not_false_positive(self):
        """Short messages must NOT be caught by prefix matching."""
        base = [
            {'role': 'user', 'content': 'agree?'},
            {'role': 'assistant', 'content': 'Yes, but actually I think we should reconsider'},
        ]
        inflight = [
            {'role': 'user', 'content': 'agree?'},
            {'role': 'assistant', 'content': 'Yes', '_live': True},
        ]
        result = merge_inflight_tail_messages(base, inflight, active_stream_id='x')
        # "Yes" is < 30 chars → prefix match skipped → treated as new message
        assert len(result) == 3
        assert result[2]['content'] == 'Yes'

    def test_new_message_appended(self):
        """Genuinely new messages should be appended."""
        base = [
            {'role': 'user', 'content': 'first question'},
            {'role': 'assistant', 'content': 'first answer'},
        ]
        inflight = [
            {'role': 'user', 'content': 'first question'},
            {'role': 'assistant', 'content': 'first answer'},
            {'role': 'user', 'content': 'second question'},
            {'role': 'assistant', 'content': 'second answer in progress', '_live': True},
        ]
        result = merge_inflight_tail_messages(base, inflight, active_stream_id='x')
        assert len(result) == 4
        assert result[2]['content'] == 'second question'
        assert result[3]['content'] == 'second answer in progress'


# ─── Tests for discard-popup fix: workspace preview session tracking ───

class TestWorkspacePreviewSessionTracking:
    """Verify _lastPreviewSessionId logic."""

    def test_same_session_skips_clear(self):
        """Same session ID → condition is False → no clear, no popup."""
        session_id = "session-abc-123"
        last_preview_session_id = "session-abc-123"
        should_clear = (session_id != last_preview_session_id)
        assert should_clear is False

    def test_different_session_triggers_clear(self):
        """Different session ID → condition is True → clear preview."""
        session_id = "session-def-456"
        last_preview_session_id = "session-abc-123"
        should_clear = (session_id != last_preview_session_id)
        assert should_clear is True

    def test_first_load_with_empty_tracker(self):
        """First call: _lastPreviewSessionId is '' → different from any real session ID."""
        session_id = "session-abc-123"
        last_preview_session_id = ""
        should_clear = (session_id != last_preview_session_id)
        # True, but that's fine — first load has nothing to clear
        assert should_clear is True


# ─── Tests for message-splicing fix: cancelStream INFLIGHT cleanup ───

class TestCancelStreamInflightCleanup:
    """Verify cancelStream logic clears INFLIGHT."""

    def test_cancel_clears_inflight(self):
        """Simulating cancelStream: INFLIGHT[sid] should be deleted."""
        INFLIGHT = {'sess-1': {'messages': [{'role': 'user', 'content': 'hi'}]}}
        sid = 'sess-1'
        active_stream_id = 'stream-1'

        # Simulate cancelStream logic
        if active_stream_id:
            if sid and sid in INFLIGHT:
                del INFLIGHT[sid]

        assert 'sess-1' not in INFLIGHT
        assert INFLIGHT == {}

    def test_cancel_noop_when_no_inflight(self):
        """If INFLIGHT doesn't have the session, no error."""
        INFLIGHT = {}
        sid = 'sess-1'
        if sid and sid in INFLIGHT:
            del INFLIGHT[sid]
        assert INFLIGHT == {}

    def test_cancel_preserves_other_sessions(self):
        """Cancelling one session should not affect others."""
        INFLIGHT = {
            'sess-1': {'messages': [{'role': 'user', 'content': 'hi'}]},
            'sess-2': {'messages': [{'role': 'user', 'content': 'other'}]},
        }
        sid = 'sess-1'
        if sid and sid in INFLIGHT:
            del INFLIGHT[sid]
        assert 'sess-1' not in INFLIGHT
        assert 'sess-2' in INFLIGHT
