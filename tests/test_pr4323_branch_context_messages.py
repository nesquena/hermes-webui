"""Behavior tests for /api/session/branch context_messages semantics.

Full forks should preserve a curated model-context snapshot, while truncated
forks must align context_messages to the visible fork prefix so discarded tail
rows cannot rehydrate on the next send.
"""
import json
import urllib.request

from api.models import Session
from tests.conftest import TEST_BASE, _post


def _get(path):
    """GET helper -- returns parsed JSON, or raises HTTPError on non-2xx."""
    with urllib.request.urlopen(TEST_BASE + path, timeout=10) as r:
        return json.loads(r.read())


def _create_saved_session(cleanup_list, messages, context_messages):
    session = Session(
        title='Branch context regression',
        messages=messages,
        context_messages=context_messages,
        model='openai/gpt-5.4-mini',
    )
    session.save()
    cleanup_list.append(session.session_id)
    return session.session_id


def _export_session(session_id):
    return _get(f'/api/session/export?session_id={session_id}')


def test_branch_full_fork_preserves_curated_context_messages(cleanup_test_sessions):
    """A full /branch preserves compressed/curated source context_messages."""
    messages = [
        {'role': 'user', 'content': 'visible turn 1'},
        {'role': 'assistant', 'content': 'visible answer 1'},
        {'role': 'user', 'content': 'visible turn 2'},
        {'role': 'assistant', 'content': 'visible answer 2'},
    ]
    curated_context = [
        {'role': 'system', 'content': 'summary of earlier synthetic context'},
        {'role': 'user', 'content': 'visible turn 2'},
        {'role': 'assistant', 'content': 'visible answer 2'},
    ]
    sid = _create_saved_session(cleanup_test_sessions, messages, curated_context)

    r = _post(TEST_BASE, '/api/session/branch', {'session_id': sid})
    assert r.get('session_id') and r['session_id'] != sid, r
    cleanup_test_sessions.append(r['session_id'])

    branch = _export_session(r['session_id'])
    assert branch['messages'] == messages
    assert branch['context_messages'] == curated_context


def test_branch_truncated_fork_context_messages_match_visible_prefix(cleanup_test_sessions):
    """A keep_count fork must not inherit hidden context rows beyond the prefix."""
    messages = [
        {'role': 'user', 'content': 'prefix turn'},
        {'role': 'assistant', 'content': 'prefix answer'},
        {'role': 'user', 'content': 'discarded turn'},
        {'role': 'assistant', 'content': 'discarded answer'},
    ]
    full_context = messages + [
        {'role': 'assistant', 'content': 'hidden tail context that must not rehydrate'},
    ]
    sid = _create_saved_session(cleanup_test_sessions, messages, full_context)

    r = _post(TEST_BASE, '/api/session/branch', {'session_id': sid, 'keep_count': 2})
    assert r.get('session_id') and r['session_id'] != sid, r
    cleanup_test_sessions.append(r['session_id'])

    branch = _export_session(r['session_id'])
    assert branch['messages'] == messages[:2]
    assert branch['context_messages'] == messages[:2]
