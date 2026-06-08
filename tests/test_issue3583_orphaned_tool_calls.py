"""Tests for orphaned tool_calls stripping in _sanitize_messages_for_api.

When a session is aborted before tool results flush, assistant messages may
contain tool_calls entries that have no matching tool-role response. Strict
APIs (DeepSeek, newer OpenAI) reject these histories with HTTP 400. The third
pass in _sanitize_messages_for_api and _api_safe_message_positions removes
those dangling entries.
"""

from api.streaming import _sanitize_messages_for_api, _api_safe_message_positions


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _asst(content=None, tool_calls=None):
    msg = {'role': 'assistant'}
    if content is not None:
        msg['content'] = content
    if tool_calls is not None:
        msg['tool_calls'] = tool_calls
    return msg


def _tool_call(call_id, name='fn'):
    return {'id': call_id, 'type': 'function', 'function': {'name': name, 'arguments': '{}'}}


def _tool_call_anthropic(call_id, name='fn'):
    # Anthropic format uses 'call_id' instead of 'id'
    return {'call_id': call_id, 'type': 'function', 'function': {'name': name, 'arguments': '{}'}}


def _tool_resp(call_id, content='result'):
    return {'role': 'tool', 'tool_call_id': call_id, 'content': content}


# ---------------------------------------------------------------------------
# Test 1: basic orphan strip — one answered, one orphaned
# ---------------------------------------------------------------------------

def test_partial_orphan_strip():
    """Assistant references two tool calls; only one has a tool response.
    The orphaned call should be removed; the answered one must stay."""
    messages = [
        {'role': 'user', 'content': 'run two tools'},
        _asst(content='', tool_calls=[_tool_call('tc-1'), _tool_call('tc-2')]),
        _tool_resp('tc-1', 'first result'),
        # tc-2 response was never flushed (session aborted)
    ]
    result = _sanitize_messages_for_api(messages)
    asst_msgs = [m for m in result if m.get('role') == 'assistant']
    assert len(asst_msgs) == 1
    tc_ids = [tc.get('id') for tc in asst_msgs[0].get('tool_calls', [])]
    assert 'tc-1' in tc_ids, "answered call must be kept"
    assert 'tc-2' not in tc_ids, "orphaned call must be stripped"


# ---------------------------------------------------------------------------
# Test 2: all calls orphaned — with and without content
# ---------------------------------------------------------------------------

def test_all_orphaned_with_content_keeps_message():
    """All tool_calls orphaned but assistant message has text content.
    The message should remain with tool_calls removed."""
    messages = [
        {'role': 'user', 'content': 'hi'},
        _asst(content='Let me check...', tool_calls=[_tool_call('tc-orphan')]),
        # no tool response at all
    ]
    result = _sanitize_messages_for_api(messages)
    asst_msgs = [m for m in result if m.get('role') == 'assistant']
    assert len(asst_msgs) == 1, "message with content must survive"
    assert 'tool_calls' not in asst_msgs[0], "tool_calls key must be removed"


def test_all_orphaned_no_content_drops_message():
    """All tool_calls orphaned and no content. Message should be removed entirely."""
    messages = [
        {'role': 'user', 'content': 'hi'},
        _asst(content='', tool_calls=[_tool_call('tc-orphan')]),
        # no tool response
    ]
    result = _sanitize_messages_for_api(messages)
    asst_msgs = [m for m in result if m.get('role') == 'assistant']
    assert len(asst_msgs) == 0, "content-less fully-orphaned assistant message must be dropped"


def test_all_orphaned_none_content_drops_message():
    """Same as above but content is None rather than empty string."""
    messages = [
        {'role': 'user', 'content': 'hi'},
        _asst(content=None, tool_calls=[_tool_call('tc-orphan')]),
    ]
    result = _sanitize_messages_for_api(messages)
    asst_msgs = [m for m in result if m.get('role') == 'assistant']
    assert len(asst_msgs) == 0


# ---------------------------------------------------------------------------
# Test 3: no orphans — complete round-trip unchanged
# ---------------------------------------------------------------------------

def test_no_orphans_unchanged():
    """Complete tool round-trip. Nothing should be stripped."""
    messages = [
        {'role': 'user', 'content': 'use a tool'},
        _asst(content='', tool_calls=[_tool_call('tc-ok')]),
        _tool_resp('tc-ok', 'done'),
        _asst(content='All done'),
    ]
    result = _sanitize_messages_for_api(messages)
    asst_msgs = [m for m in result if m.get('role') == 'assistant']
    # Both assistant messages present
    assert len(asst_msgs) == 2
    first = asst_msgs[0]
    assert 'tool_calls' in first
    assert first['tool_calls'][0]['id'] == 'tc-ok'


# ---------------------------------------------------------------------------
# Test 4: mixed — multiple assistant messages, selective stripping
# ---------------------------------------------------------------------------

def test_mixed_selective_stripping():
    """Multiple assistant messages: some complete, some orphaned. Only orphans stripped."""
    messages = [
        {'role': 'user', 'content': 'go'},
        _asst(content='', tool_calls=[_tool_call('tc-a')]),
        _tool_resp('tc-a', 'a-result'),
        _asst(content='', tool_calls=[_tool_call('tc-b'), _tool_call('tc-c')]),
        _tool_resp('tc-b', 'b-result'),
        # tc-c never answered
        _asst(content='Summary'),
    ]
    result = _sanitize_messages_for_api(messages)
    asst_msgs = [m for m in result if m.get('role') == 'assistant']
    assert len(asst_msgs) == 3

    # First: complete, tc-a retained
    assert asst_msgs[0].get('tool_calls', [{}])[0].get('id') == 'tc-a'

    # Second: tc-b kept, tc-c stripped
    second_ids = [tc.get('id') for tc in asst_msgs[1].get('tool_calls', [])]
    assert 'tc-b' in second_ids
    assert 'tc-c' not in second_ids

    # Third: plain text, no tool_calls
    assert 'tool_calls' not in asst_msgs[2]


# ---------------------------------------------------------------------------
# Test 5: Anthropic call_id format
# ---------------------------------------------------------------------------

def test_anthropic_call_id_format():
    """Tool calls using call_id (Anthropic format) are stripped correctly."""
    messages = [
        {'role': 'user', 'content': 'anthropic test'},
        _asst(content='', tool_calls=[
            _tool_call_anthropic('ac-answered'),
            _tool_call_anthropic('ac-orphaned'),
        ]),
        _tool_resp('ac-answered', 'ok'),
        # ac-orphaned never answered
    ]
    result = _sanitize_messages_for_api(messages)
    asst_msgs = [m for m in result if m.get('role') == 'assistant']
    assert len(asst_msgs) == 1
    tc_call_ids = [tc.get('call_id') for tc in asst_msgs[0].get('tool_calls', [])]
    assert 'ac-answered' in tc_call_ids
    assert 'ac-orphaned' not in tc_call_ids


# ---------------------------------------------------------------------------
# _api_safe_message_positions mirrors the same logic
# ---------------------------------------------------------------------------

def test_positions_partial_orphan_strip():
    """_api_safe_message_positions also strips the orphaned call."""
    messages = [
        {'role': 'user', 'content': 'run two tools'},
        _asst(content='', tool_calls=[_tool_call('tc-1'), _tool_call('tc-2')]),
        _tool_resp('tc-1', 'first result'),
    ]
    positions = _api_safe_message_positions(messages)
    asst_entries = [(i, m) for i, m in positions if m.get('role') == 'assistant']
    assert len(asst_entries) == 1
    tc_ids = [tc.get('id') for tc in asst_entries[0][1].get('tool_calls', [])]
    assert 'tc-1' in tc_ids
    assert 'tc-2' not in tc_ids


def test_positions_all_orphaned_no_content_drops():
    """_api_safe_message_positions drops a fully-orphaned no-content assistant message."""
    messages = [
        {'role': 'user', 'content': 'hi'},
        _asst(content='', tool_calls=[_tool_call('tc-orphan')]),
    ]
    positions = _api_safe_message_positions(messages)
    asst_entries = [(i, m) for i, m in positions if m.get('role') == 'assistant']
    assert len(asst_entries) == 0
