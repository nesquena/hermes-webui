"""Regression tests for retry preserving recoverable interrupted-turn output.

When a retry would discard meaningful post-user output, it must branch the
session instead of rewriting the active transcript in place, per the
WebUI run-state consistency contract (#2361).
"""
import json
import urllib.request

from tests.conftest import TEST_BASE, _post


def _get(path):
    """GET helper -- returns parsed JSON, or raises HTTPError on non-2xx."""
    with urllib.request.urlopen(TEST_BASE + path, timeout=10) as r:
        return json.loads(r.read())


def _import_session_with_messages(cleanup_list, messages, context_messages=None,
                                    model='openai/gpt-5.4-mini', **extra_fields):
    """Create a session pre-populated with `messages` via /api/session/import."""
    body = {
        'title': 'test',
        'messages': messages,
        'model': model,
    }
    if context_messages is not None:
        body['context_messages'] = context_messages
    body.update(extra_fields)
    r = _post(TEST_BASE, '/api/session/import', body)
    assert r.get('ok') is True and 'session' in r, f"Import failed: {r}"
    sid = r['session']['session_id']
    cleanup_list.append(sid)
    return sid


def test_retry_provider_error_in_place(cleanup_test_sessions):
    """A strict provider error marker after the last user is retried in place."""
    sid = _import_session_with_messages(cleanup_test_sessions, [
        {'role': 'user', 'content': 'first user msg'},
        {'role': 'assistant', 'content': 'first reply'},
        {'role': 'user', 'content': 'second user msg'},
        {'role': 'assistant', 'content': '**Error:** Provider openai is unavailable. Please retry.'},
    ])
    r = _post(TEST_BASE, '/api/session/retry', {'session_id': sid})
    assert r.get('ok') is True, r
    assert r.get('mode') == 'in_place'
    assert r.get('last_user_text') == 'second user msg'
    assert r.get('removed_count') == 2

    sess = _get(f'/api/session?session_id={sid}')['session']
    assert len(sess['messages']) == 2
    assert sess['messages'][0]['content'] == 'first user msg'
    assert sess['messages'][1]['content'] == 'first reply'


def test_retry_compression_error_in_place(cleanup_test_sessions):
    """A context-compression error marker after the last user is retried in place."""
    sid = _import_session_with_messages(cleanup_test_sessions, [
        {'role': 'user', 'content': 'first user msg'},
        {'role': 'assistant', 'content': 'first reply'},
        {'role': 'user', 'content': 'compress me'},
        {'role': 'assistant', 'content': '**Error:** No response received after context compression. Please retry.'},
    ])
    r = _post(TEST_BASE, '/api/session/retry', {'session_id': sid})
    assert r.get('ok') is True, r
    assert r.get('mode') == 'in_place'
    assert r.get('last_user_text') == 'compress me'
    assert r.get('removed_count') == 2

    sess = _get(f'/api/session?session_id={sid}')['session']
    assert len(sess['messages']) == 2


def test_retry_tool_output_branches(cleanup_test_sessions):
    """A tail containing a tool row forces branch mode and preserves the original."""
    sid = _import_session_with_messages(
        cleanup_test_sessions,
        [
            {'role': 'user', 'content': 'first user msg'},
            {'role': 'assistant', 'content': 'first reply'},
            {'role': 'user', 'content': 'use the tool'},
            {'role': 'assistant', 'content': 'calling tool'},
            {'role': 'tool', 'content': '{"result": 42}', 'name': 'test_tool'},
            {'role': 'assistant', 'content': 'done'},
        ],
        context_messages=[
            {'role': 'user', 'content': 'first user msg'},
            {'role': 'assistant', 'content': 'first reply'},
            {'role': 'user', 'content': 'use the tool'},
            {'role': 'assistant', 'content': 'calling tool'},
            {'role': 'tool', 'content': '{"result": 42}', 'name': 'test_tool'},
            {'role': 'assistant', 'content': 'done'},
        ],
    )
    original = _get(f'/api/session?session_id={sid}')['session']
    original_len = len(original['messages'])
    original_messages = [m['content'] for m in original['messages']]

    r = _post(TEST_BASE, '/api/session/retry', {'session_id': sid})
    assert r.get('ok') is True, r
    assert r.get('mode') == 'branch'
    assert r.get('parent_session_id') == sid
    assert isinstance(r.get('session_id'), str)
    assert r.get('session_id') != sid
    assert r.get('last_user_text') == 'use the tool'
    assert r.get('removed_count') == 4

    # Original session is untouched.
    after_original = _get(f'/api/session?session_id={sid}')['session']
    assert len(after_original['messages']) == original_len
    assert [m['content'] for m in after_original['messages']] == original_messages

    # Branch contains only the prefix before the last user.
    branch = _get(f"/api/session?session_id={r['session_id']}")['session']
    assert branch['parent_session_id'] == sid
    assert branch.get('session_source') == 'fork'
    assert len(branch['messages']) == 2
    assert branch['messages'][0]['content'] == 'first user msg'
    assert branch['messages'][1]['content'] == 'first reply'
    cleanup_test_sessions.append(r['session_id'])


def test_retry_assistant_prose_branches(cleanup_test_sessions):
    """Normal assistant prose after the last user forces branch mode."""
    sid = _import_session_with_messages(cleanup_test_sessions, [
        {'role': 'user', 'content': 'first user msg'},
        {'role': 'assistant', 'content': 'first reply'},
        {'role': 'user', 'content': 'second user msg'},
        {'role': 'assistant', 'content': 'partial reply here'},
    ])
    r = _post(TEST_BASE, '/api/session/retry', {'session_id': sid})
    assert r.get('ok') is True, r
    assert r.get('mode') == 'branch'
    assert r.get('last_user_text') == 'second user msg'

    original = _get(f'/api/session?session_id={sid}')['session']
    assert len(original['messages']) == 4

    branch = _get(f"/api/session?session_id={r['session_id']}")['session']
    assert len(branch['messages']) == 2
    assert branch['messages'][-1]['content'] == 'first reply'

    cleanup_test_sessions.append(r['session_id'])


def test_retry_concurrent_output_retries_preserve_original(cleanup_test_sessions):
    """Concurrent branch-mode retries leave the source transcript unchanged."""
    from concurrent.futures import ThreadPoolExecutor

    messages = [
        {'role': 'user', 'content': 'first user msg'},
        {'role': 'assistant', 'content': 'first reply'},
        {'role': 'user', 'content': 'second user msg'},
        {'role': 'assistant', 'content': 'partial second reply'},
    ]
    sid = _import_session_with_messages(cleanup_test_sessions, messages)

    def _do_retry():
        return _post(TEST_BASE, '/api/session/retry', {'session_id': sid})

    with ThreadPoolExecutor(max_workers=4) as ex:
        results = [f.result() for f in [ex.submit(_do_retry) for _ in range(4)]]

    for r in results:
        assert r.get('ok') is True, r
        assert r.get('mode') == 'branch'
        assert r.get('parent_session_id') == sid
        assert r.get('last_user_text') == 'second user msg'
        cleanup_test_sessions.append(r['session_id'])
        branch = _get(f"/api/session?session_id={r['session_id']}")['session']
        assert [m['content'] for m in branch['messages']] == ['first user msg', 'first reply']

    original = _get(f'/api/session?session_id={sid}')['session']
    assert [m['content'] for m in original['messages']] == [m['content'] for m in messages]


def test_retry_empty_tail_in_place(cleanup_test_sessions):
    """When the last message is a user message, retry truncates in place."""
    sid = _import_session_with_messages(cleanup_test_sessions, [
        {'role': 'user', 'content': 'first user msg'},
        {'role': 'assistant', 'content': 'first reply'},
        {'role': 'user', 'content': 'pending user msg'},
    ])
    r = _post(TEST_BASE, '/api/session/retry', {'session_id': sid})
    assert r.get('ok') is True, r
    assert r.get('mode') == 'in_place'
    assert r.get('last_user_text') == 'pending user msg'
    assert r.get('removed_count') == 1

    sess = _get(f'/api/session?session_id={sid}')['session']
    assert len(sess['messages']) == 2
    assert sess['messages'][-1]['content'] == 'first reply'


def test_retry_first_turn_output_branches_to_empty_prefix(cleanup_test_sessions):
    """Meaningful output on the first turn branches to a persisted empty prefix."""
    sid = _import_session_with_messages(cleanup_test_sessions, [
        {'role': 'user', 'content': 'first user msg'},
        {'role': 'assistant', 'content': 'partial first reply'},
    ])
    r = _post(TEST_BASE, '/api/session/retry', {'session_id': sid})
    assert r.get('ok') is True, r
    assert r.get('mode') == 'branch'
    assert r.get('parent_session_id') == sid
    assert r.get('last_user_text') == 'first user msg'
    assert r.get('removed_count') == 2

    original = _get(f'/api/session?session_id={sid}')['session']
    assert len(original['messages']) == 2

    branch = _get(f"/api/session?session_id={r['session_id']}")['session']
    assert branch['parent_session_id'] == sid
    assert branch.get('session_source') == 'fork'
    assert branch['messages'] == []

    cleanup_test_sessions.append(r['session_id'])


def test_retry_assistant_with_tool_calls_branches(cleanup_test_sessions):
    """Assistant content with tool_calls forces branch mode."""
    sid = _import_session_with_messages(cleanup_test_sessions, [
        {'role': 'user', 'content': 'first user msg'},
        {'role': 'assistant', 'content': 'first reply'},
        {'role': 'user', 'content': 'call a tool'},
        {'role': 'assistant', 'content': '', 'tool_calls': [{'id': 'c1', 'function': {'name': 'foo', 'arguments': '{}'}}]},
    ])
    r = _post(TEST_BASE, '/api/session/retry', {'session_id': sid})
    assert r.get('ok') is True, r
    assert r.get('mode') == 'branch'
    assert r.get('last_user_text') == 'call a tool'

    original = _get(f'/api/session?session_id={sid}')['session']
    assert len(original['messages']) == 4

    branch = _get(f"/api/session?session_id={r['session_id']}")['session']
    assert len(branch['messages']) == 2

    cleanup_test_sessions.append(r['session_id'])


def test_retry_pending_user_message_wins_over_older_persisted_user(cleanup_test_sessions):
    """A non-empty pending_user_message anchors retry on the interrupted turn."""
    sid = _import_session_with_messages(
        cleanup_test_sessions,
        [
            {'role': 'user', 'content': 'old turn A'},
            {'role': 'assistant', 'content': 'old reply A'},
            {'role': 'user', 'content': 'old turn B'},
            {'role': 'assistant', 'content': 'old reply B'},
        ],
        pending_user_message='new interrupted turn C',
        active_stream_id='stream-123',
        pending_started_at=123.0,
    )

    r = _post(TEST_BASE, '/api/session/retry', {'session_id': sid})
    assert r.get('ok') is True, r
    assert r.get('mode') == 'pending'
    assert r.get('last_user_text') == 'new interrupted turn C'
    assert r.get('removed_count') == 0

    sess = _get(f'/api/session?session_id={sid}')['session']
    assert len(sess['messages']) == 4
    assert sess['messages'][0]['content'] == 'old turn A'
    assert sess['messages'][1]['content'] == 'old reply A'
    assert sess['messages'][2]['content'] == 'old turn B'
    assert sess['messages'][3]['content'] == 'old reply B'
    assert sess.get('active_stream_id') is None
    assert sess.get('pending_user_message') is None
    assert sess.get('pending_attachments') in (None, [])
    assert sess.get('pending_started_at') is None


def test_retry_pending_user_message_preserves_meaningful_persisted_tail(cleanup_test_sessions):
    """Pending interrupted turn wins even if older persisted output exists."""
    sid = _import_session_with_messages(
        cleanup_test_sessions,
        [
            {'role': 'user', 'content': 'old turn A'},
            {'role': 'assistant', 'content': 'old reply A'},
            {'role': 'user', 'content': 'old turn B'},
            {'role': 'assistant', 'content': 'old reply B'},
            {'role': 'tool', 'content': '{"result": 42}', 'name': 'test_tool'},
            {'role': 'assistant', 'content': 'tool summary'},
        ],
        pending_user_message='new interrupted turn C',
        active_stream_id='stream-456',
        pending_attachments=[{'type': 'text', 'text': 'extra'}],
    )

    r = _post(TEST_BASE, '/api/session/retry', {'session_id': sid})
    assert r.get('ok') is True, r
    assert r.get('mode') == 'pending'
    assert r.get('last_user_text') == 'new interrupted turn C'
    assert r.get('removed_count') == 0

    sess = _get(f'/api/session?session_id={sid}')['session']
    assert len(sess['messages']) == 6
    assert sess.get('active_stream_id') is None
    assert sess.get('pending_user_message') is None
    assert sess.get('pending_attachments') in (None, [])
    assert sess.get('pending_started_at') is None


def test_retry_recovered_pending_turn_does_not_jump_to_older_user(cleanup_test_sessions):
    """Retry targets a materialized recovered pending turn, not older history."""
    sid = _import_session_with_messages(
        cleanup_test_sessions,
        [
            {'role': 'user', 'content': 'old turn A'},
            {'role': 'assistant', 'content': 'old reply A'},
            {'role': 'user', 'content': 'old turn B'},
            {'role': 'assistant', 'content': 'old reply B'},
        ],
        pending_user_message='new interrupted turn C',
        active_stream_id='stream-789',
        pending_started_at=123.0,
    )
    opened = _get(f'/api/session?session_id={sid}')['session']
    assert opened.get('pending_user_message') is None
    assert opened['messages'][-2]['role'] == 'user'
    assert opened['messages'][-2]['content'] == 'new interrupted turn C'
    assert opened['messages'][-2].get('_recovered') is True
    assert opened['messages'][-1].get('type') == 'interrupted'

    r = _post(TEST_BASE, '/api/session/retry', {'session_id': sid})
    assert r.get('ok') is True, r
    # The interrupted marker carries recovered output, so the retry branches
    # to preserve the original transcript (including the recovered turn).
    assert r.get('mode') == 'branch'
    assert r.get('last_user_text') == 'new interrupted turn C'

    # Original session is preserved with the recovered turn.
    original = _get(f'/api/session?session_id={sid}')['session']
    assert len(original['messages']) == 6

    cleanup_test_sessions.append(r['session_id'])


def test_retry_empty_string_pending_falls_through_to_persisted(cleanup_test_sessions):
    """An empty-string pending_user_message must not trigger pending mode."""
    sid = _import_session_with_messages(
        cleanup_test_sessions,
        [
            {'role': 'user', 'content': 'first user msg'},
            {'role': 'assistant', 'content': 'first reply'},
            {'role': 'user', 'content': 'second user msg'},
            {'role': 'assistant', 'content': '**Error:** Provider openai is unavailable. Please retry.'},
        ],
        pending_user_message='',
    )
    r = _post(TEST_BASE, '/api/session/retry', {'session_id': sid})
    assert r.get('ok') is True, r
    assert r.get('mode') == 'in_place'
    assert r.get('last_user_text') == 'second user msg'


def test_retry_pending_equal_to_last_user_falls_through(cleanup_test_sessions):
    """When pending_user_message equals the last persisted user, fall through."""
    sid = _import_session_with_messages(
        cleanup_test_sessions,
        [
            {'role': 'user', 'content': 'first user msg'},
            {'role': 'assistant', 'content': 'first reply'},
            {'role': 'user', 'content': 'duplicate turn'},
            {'role': 'assistant', 'content': '**Error:** Provider openai is unavailable. Please retry.'},
        ],
        pending_user_message='duplicate turn',
        active_stream_id='stream-dup',
    )
    r = _post(TEST_BASE, '/api/session/retry', {'session_id': sid})
    assert r.get('ok') is True, r
    # Should NOT be pending mode — should fall through to persisted logic
    assert r.get('mode') == 'in_place'
    assert r.get('last_user_text') == 'duplicate turn'


def test_retry_interrupted_marker_with_recovered_output_branches(cleanup_test_sessions):
    """An interrupted marker carrying recovered output must force branch mode."""
    sid = _import_session_with_messages(
        cleanup_test_sessions,
        [
            {'role': 'user', 'content': 'first user msg'},
            {'role': 'assistant', 'content': 'first reply'},
            {'role': 'user', 'content': 'second user msg'},
            {'role': 'assistant', 'content': 'The partial output above was recovered from the run journal, but the interrupted agent process could not continue.', 'type': 'interrupted', '_error': True},
        ],
    )
    r = _post(TEST_BASE, '/api/session/retry', {'session_id': sid})
    assert r.get('ok') is True, r
    # Must branch because the interrupted marker carries recovered output
    assert r.get('mode') == 'branch'
    assert r.get('last_user_text') == 'second user msg'

    original = _get(f'/api/session?session_id={sid}')['session']
    assert len(original['messages']) == 4

    cleanup_test_sessions.append(r['session_id'])


def test_retry_interrupted_marker_no_content_in_place(cleanup_test_sessions):
    """An interrupted marker with no content is safe to retry in place."""
    sid = _import_session_with_messages(
        cleanup_test_sessions,
        [
            {'role': 'user', 'content': 'first user msg'},
            {'role': 'assistant', 'content': 'first reply'},
            {'role': 'user', 'content': 'second user msg'},
            {'role': 'assistant', 'content': '', 'type': 'interrupted', '_error': True},
        ],
    )
    r = _post(TEST_BASE, '/api/session/retry', {'session_id': sid})
    assert r.get('ok') is True, r
    assert r.get('mode') == 'in_place'
    assert r.get('last_user_text') == 'second user msg'

    sess = _get(f'/api/session?session_id={sid}')['session']
    assert len(sess['messages']) == 2
