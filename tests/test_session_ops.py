"""End-to-end tests for /api/session/retry, /api/session/undo,
/api/session/status, /api/session/usage.

Tests run against the live test subprocess server (see tests/conftest.py).
We seed transcripts via POST /api/session/import (ignores incoming
session_id; returns a fresh one we register for cleanup).
"""
import json
import urllib.request
import urllib.error

import pytest

from tests.conftest import TEST_BASE, _post, make_session_tracked


def _get(path):
    """GET helper -- returns parsed JSON, or raises HTTPError on non-2xx."""
    with urllib.request.urlopen(TEST_BASE + path, timeout=10) as r:
        return json.loads(r.read())


def _import_session_with_messages(cleanup_list, messages, model='openai/gpt-5.4-mini'):
    """Create a session pre-populated with `messages` via /api/session/import.

    Returns the server-assigned session_id (registered for cleanup).

    api/routes.py:2588 takes {title, messages, model, workspace, tool_calls,
    pinned} and IGNORES any incoming session_id -- always generates a fresh
    one via Session(...). We use the server's returned id, not a self-
    generated one.
    """
    body = {
        'title': 'test',
        'messages': messages,
        'model': model,
    }
    r = _post(TEST_BASE, '/api/session/import', body)
    assert r.get('ok') is True and 'session' in r, f"Import failed: {r}"
    sid = r['session']['session_id']
    cleanup_list.append(sid)
    return sid


# -- /api/session/retry ----------------------------------------------------

def test_retry_returns_last_user_text(cleanup_test_sessions):
    sid = _import_session_with_messages(cleanup_test_sessions, [
        {'role': 'user', 'content': 'first user msg'},
        {'role': 'assistant', 'content': 'first reply'},
        {'role': 'user', 'content': 'second user msg'},
        {'role': 'assistant', 'content': 'second reply'},
        {'role': 'tool', 'content': 'tool output'},
    ])
    r = _post(TEST_BASE, '/api/session/retry', {'session_id': sid})
    assert r.get('ok') is True, r
    assert r.get('last_user_text') == 'second user msg'
    assert r.get('removed_count') == 3


def test_retry_truncates_transcript(cleanup_test_sessions):
    sid = _import_session_with_messages(cleanup_test_sessions, [
        {'role': 'user', 'content': 'first user msg'},
        {'role': 'assistant', 'content': 'first reply'},
        {'role': 'user', 'content': 'second user msg'},
        {'role': 'assistant', 'content': 'second reply'},
    ])
    _post(TEST_BASE, '/api/session/retry', {'session_id': sid})
    sess = _get(f'/api/session?session_id={sid}')['session']
    # After retry: only the first exchange remains (2 messages).
    assert len(sess['messages']) == 2
    assert sess['messages'][-1]['content'] == 'first reply'


def test_retry_no_user_returns_error(cleanup_test_sessions):
    sid = _import_session_with_messages(cleanup_test_sessions, [
        {'role': 'assistant', 'content': 'orphan reply'},
    ])
    r = _post(TEST_BASE, '/api/session/retry', {'session_id': sid})
    assert 'error' in r
    assert 'no previous message' in r['error'].lower()


def test_retry_unknown_session_returns_404():
    # _post catches HTTPError and returns the body as JSON.
    # bad(handler, ..., 404) sends 404 + {error: "..."}.
    r = _post(TEST_BASE, '/api/session/retry', {'session_id': 'nonexistent_zzz'})
    assert 'error' in r
    assert 'not found' in r['error'].lower()


def test_retry_missing_session_id_returns_error():
    r = _post(TEST_BASE, '/api/session/retry', {})
    assert 'error' in r


def test_retry_does_not_double_append(cleanup_test_sessions):
    """After /api/session/retry, the truncated transcript must end at the
    message BEFORE the last user message. Critical assertion: no duplicate
    of the resent user message gets left behind in the truncated transcript.
    """
    sid = _import_session_with_messages(cleanup_test_sessions, [
        {'role': 'user', 'content': 'msg A'},
        {'role': 'assistant', 'content': 'reply A'},
        {'role': 'user', 'content': 'msg B'},
        {'role': 'assistant', 'content': 'reply B'},
    ])
    r = _post(TEST_BASE, '/api/session/retry', {'session_id': sid})
    assert r['removed_count'] == 2  # msg B + reply B
    sess = _get(f'/api/session?session_id={sid}')['session']
    msgs = sess['messages']
    # Only msg A + reply A remain. Critically: there is NO 'msg B' anywhere.
    assert len(msgs) == 2
    assert msgs[0]['content'] == 'msg A'
    assert msgs[1]['content'] == 'reply A'


# ── /api/session/undo ─────────────────────────────────────────────────────

def test_undo_returns_removed_preview(cleanup_test_sessions):
    sid = _import_session_with_messages(cleanup_test_sessions, [
        {'role': 'user', 'content': 'first user msg'},
        {'role': 'assistant', 'content': 'first reply'},
        {'role': 'user', 'content': 'second user msg'},
        {'role': 'assistant', 'content': 'second reply'},
        {'role': 'tool', 'content': 'tool output'},
    ])
    r = _post(TEST_BASE, '/api/session/undo', {'session_id': sid})
    assert r.get('ok') is True
    assert r.get('removed_count') == 3
    assert 'second user msg' in r.get('removed_preview', '')


def test_undo_truncates_transcript(cleanup_test_sessions):
    sid = _import_session_with_messages(cleanup_test_sessions, [
        {'role': 'user', 'content': 'first user msg'},
        {'role': 'assistant', 'content': 'first reply'},
        {'role': 'user', 'content': 'second user msg'},
        {'role': 'assistant', 'content': 'second reply'},
    ])
    _post(TEST_BASE, '/api/session/undo', {'session_id': sid})
    sess = _get(f'/api/session?session_id={sid}')['session']
    assert len(sess['messages']) == 2
    assert sess['messages'][-1]['content'] == 'first reply'


def test_undo_repeated_until_empty(cleanup_test_sessions):
    sid = _import_session_with_messages(cleanup_test_sessions, [
        {'role': 'user', 'content': 'msg A'},
        {'role': 'assistant', 'content': 'reply A'},
    ])
    _post(TEST_BASE, '/api/session/undo', {'session_id': sid})
    r = _post(TEST_BASE, '/api/session/undo', {'session_id': sid})
    assert 'error' in r
    assert 'nothing to undo' in r['error'].lower()


def test_undo_unknown_session_returns_404():
    r = _post(TEST_BASE, '/api/session/undo', {'session_id': 'nonexistent_zzz'})
    assert 'error' in r
    assert 'not found' in r['error'].lower()


# ── /api/session/status ───────────────────────────────────────────────────

def test_status_returns_summary(cleanup_test_sessions):
    sid = _import_session_with_messages(cleanup_test_sessions, [
        {'role': 'user', 'content': 'a'},
        {'role': 'assistant', 'content': 'b'},
        {'role': 'user', 'content': 'c'},
    ])
    r = _get(f'/api/session/status?session_id={sid}')
    assert r['session_id'] == sid
    assert r['title'] == 'test'
    assert r['message_count'] == 3
    assert 'model' in r
    assert 'workspace' in r
    assert 'created_at' in r
    assert 'updated_at' in r
    assert r['agent_running'] is False  # no active stream


def test_status_unknown_returns_404():
    try:
        _get('/api/session/status?session_id=nonexistent_zzz')
        pytest.fail('Expected HTTPError')
    except urllib.error.HTTPError as e:
        assert e.code == 404


def test_status_missing_param():
    try:
        _get('/api/session/status')
        pytest.fail('Expected HTTPError')
    except urllib.error.HTTPError as e:
        assert e.code == 400


# ── /api/session/usage ────────────────────────────────────────────────────

def test_usage_returns_token_counts(cleanup_test_sessions):
    sid, _ws = make_session_tracked(cleanup_test_sessions)
    # Usage on a new session: zero everything.
    r = _get(f'/api/session/usage?session_id={sid}')
    assert r['input_tokens'] == 0
    assert r['output_tokens'] == 0
    assert r['total_tokens'] == 0
