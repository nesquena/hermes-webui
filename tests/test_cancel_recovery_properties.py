"""Deterministic recovery combinations through production lifecycle and history builders."""
from __future__ import annotations

import base64
import copy
import itertools
import queue
import random
import struct
import threading
import time
import zlib
from unittest.mock import Mock

import pytest

from api import config, models, routes
from api.models import Session
from api.run_journal import RunJournalWriter
from api.streaming import cancel_stream
from tests.test_cancel_restart_journal_recovery import _isolated_state  # noqa: F401
from tests.test_cancel_restart_journal_recovery import (
    _simulate_restart, _pending_stream_hook, _stream_output,
    _assert_retry_turn_ownership,
)

SEED = 782920261003
AXES = (
    ('eager', 'deferred'), (10, 10.125, 10.875),
    ('plain', 'native', 'api', 'attachments'), (False, True),
    (False, True), (False, True), (False, True), (False, True),
)
ALL_CASES = list(itertools.product(*AXES))
_rng = random.Random(SEED)
_rng.shuffle(ALL_CASES)
CASES = ALL_CASES[:128]


def _history(session, prompt):
    from api.streaming import (
        _new_turn_context_from_messages, _dedupe_replayed_context_messages,
        _dedupe_replayed_active_context, _sanitize_messages_for_agent,
    )
    rows = models.reconciled_state_db_messages_for_session(session, prefer_context=True, state_messages=[])
    rows = _new_turn_context_from_messages(rows, prompt)
    rows = _dedupe_replayed_context_messages(rows, rows, prompt)
    rows = _dedupe_replayed_active_context(rows, rows, prompt)
    return _sanitize_messages_for_agent(rows)


def _tool_pair(tid, content):
    return [
        {'role': 'assistant', 'content': '', 'tool_calls': [{
            'id': tid, 'type': 'function', 'function': {
                'name': 'read_file', 'arguments': '{"path":"synthetic.txt"}',
            },
        }]},
        {'role': 'tool', 'content': content, 'tool_call_id': tid},
    ]


def _register(s, stream, prompt, started):
    s.pending_user_message = prompt
    s.pending_started_at = started
    s.pending_user_source = 'webui'
    s.active_stream_id = stream
    models.SESSIONS[s.session_id] = s
    config.STREAMS[stream] = queue.Queue()
    config.CANCEL_FLAGS[stream] = threading.Event()
    agent = Mock()
    agent.session_id = s.session_id
    config.AGENT_INSTANCES[stream] = agent
    config.ACTIVE_RUNS[stream] = {'session_id': s.session_id, 'phase': 'running', 'started_at': time.time()}
    s.save()


def _png(tmp_path):
    def chunk(kind, data):
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data))
    body = b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', 1, 1, 8, 2, 0, 0, 0))
    body += chunk(b'IDAT', zlib.compress(b'\0\xfb\xef\xff')) + chunk(b'IEND', b'')
    path = tmp_path / 'synthetic.png'
    path.write_bytes(body)
    return path, body


@pytest.mark.parametrize('case', CASES, ids=lambda c: '-'.join(map(str, c)))
def test_seeded_production_recovery_exact_next_send(case, tmp_path, monkeypatch):
    mode, started, shape, new_output, cold, native_pair, repeated, rollback = case
    sid = 'seeded-recovery'
    old_stream, new_stream = 'seed-old', 'seed-new'
    old_prompt = 'Same prompt' if repeated else 'Old question'
    new_prompt = old_prompt if repeated else 'New question'
    old_answer = 'Same answer' if repeated else 'Old answer'
    new_answer = old_answer if repeated else 'New answer'
    prior = [{'role': 'user', 'content': 'Before question', 'timestamp': 1},
             {'role': 'assistant', 'content': 'Before answer', 'timestamp': 2}]
    s = Session(session_id=sid, messages=copy.deepcopy(prior), context_messages=copy.deepcopy(prior))
    if mode == 'deferred':
        s.context_messages.append({'role': 'user', 'content': old_prompt, 'timestamp': started})
    monkeypatch.setattr(routes, 'get_webui_session_save_mode', lambda: mode)
    routes._prepare_chat_start_session_for_stream(
        s, msg=old_prompt, attachments=[], workspace=str(tmp_path), model='synthetic',
        model_provider=None, stream_id=old_stream, started_at=started,
    )
    _simulate_restart()
    s = models.get_session(sid)
    assert _pending_stream_hook(s, old_stream) is not None
    display = next(r for r in s.messages if r.get('role') == 'user' and r.get('timestamp') == started)
    context = next(r for r in s.context_messages if r.get('role') == 'user' and r.get('timestamp') == started)
    from api.streaming import _build_native_multimodal_message, _workspace_context_prefix
    prefix = _workspace_context_prefix(str(tmp_path))
    if shape == 'native':
        path, image_bytes = _png(tmp_path)
        attachments = [{'path': str(path), 'name': path.name, 'mime': 'image/png'}]
        context['content'] = _build_native_multimodal_message(prefix, old_prompt, attachments, str(tmp_path), cfg={'agent': {'image_input_mode': 'native'}})
        assert base64.b64decode(context['content'][1]['image_url']['url'].split(',', 1)[1]) == image_bytes
        context[models._WEBUI_TRUSTED_AGENT_INPUT_FIELD] = prefix + old_prompt
        display['attachments'] = attachments
    elif shape == 'api':
        context['api_content'] = prefix + old_prompt + '\n\n[Attached files: synthetic.txt]'
    elif shape == 'attachments':
        display['attachments'] = [{'name': 'synthetic.txt', 'path': str(tmp_path / 'synthetic.txt')}]
        context['content'] = prefix + old_prompt + '\n\n[Attached files: synthetic.txt]'
    original_payload = copy.deepcopy(context)
    pair = _tool_pair('old-native', 'Old native result') if native_pair else []
    s.context_messages.extend(copy.deepcopy(pair))
    owner = {'role': 'user', 'content': new_prompt, 'timestamp': 20.625}
    s.messages.append(copy.deepcopy(owner))
    s.context_messages.append(copy.deepcopy(owner))
    _register(s, new_stream, new_prompt, 20.625)
    assert cancel_stream(new_stream) is True
    writer = RunJournalWriter(sid, new_stream)
    if new_output:
        writer.append_sse_event('token', {'text': new_answer})
    writer.append_sse_event('tool', {'name': 'read_file', 'tid': 'new-display-card', 'args': {'path': 'new.txt'}})
    writer.append_sse_event('tool_complete', {'name': 'read_file', 'tid': 'new-display-card', 'preview': 'New tool result'})
    writer.append_sse_event('cancel', {'message': 'New stop'})
    _simulate_restart()
    s = models.get_session(sid)
    assert not _stream_output(s, old_stream)
    new_pair = _tool_pair('new-native', 'New native result') if native_pair and new_output else []
    if new_pair:
        new_owner_idx = next(i for i, r in enumerate(s.context_messages) if r.get('role') == 'user' and r.get('timestamp') == 20.625)
        s.context_messages[new_owner_idx + 1:new_owner_idx + 1] = copy.deepcopy(new_pair)
        s.save()
    old = RunJournalWriter(sid, old_stream)
    old.append_sse_event('token', {'text': old_answer})
    old.append_sse_event('reasoning', {'text': 'Private reasoning'})
    old.append_sse_event('tool', {'name': 'read_file', 'tid': 'old-display-card', 'args': {'path': 'old.txt'}})
    old.append_sse_event('tool_complete', {'name': 'read_file', 'tid': 'old-display-card', 'preview': 'Old tool result'})
    old.append_sse_event('cancel', {'message': 'Late old stop'})
    if rollback:
        before = copy.deepcopy((s.messages, s.context_messages, s.tool_calls, s.updated_at))
        saved = Session.save
        monkeypatch.setattr(Session, 'save', Mock(side_effect=OSError('seeded save failure')))
        assert models._retry_journal_recovery_in_place(s) is False
        assert (s.messages, s.context_messages, s.tool_calls, s.updated_at) == before
        monkeypatch.setattr(Session, 'save', saved)
    expected = [(r['role'], r['content']) for r in prior]
    expected += [('user', original_payload['content'])] + [(r['role'], r['content']) for r in pair] + [('assistant', old_answer)]
    if new_output:
        expected += [('user', new_prompt)] + [(r['role'], r['content']) for r in new_pair] + [('assistant', new_answer)]
    stable = None
    for read in range(4):
        if cold:
            _simulate_restart()
        s = models.get_session(sid)
        history = _history(s, old_prompt if read % 2 else 'Actual next question')
        assert [(r['role'], r.get('content', '')) for r in history] == expected
        if 'api_content' in original_payload:
            assert history[2]['api_content'] == original_payload['api_content']
        if native_pair:
            assert history[3]['tool_calls'] == pair[0]['tool_calls']
            assert history[4]['tool_call_id'] == 'old-native'
        assert _pending_stream_hook(s, old_stream) is None
        assert len([r for r in _stream_output(s, old_stream) if r.get('content') == old_answer]) == 1
        _assert_retry_turn_ownership(s, ['ordinary', 'interrupted', 'cancelled'], ['prior-no-stream', old_stream, new_stream])
        cards = {t['tid']: t for t in s.tool_calls}
        assert cards['old-display-card']['preview'] == 'Old tool result'
        assert cards['new-display-card']['preview'] == 'New tool result'
        current = copy.deepcopy((s.messages, s.context_messages, s.tool_calls))
        if stable is not None:
            assert current == stable
        stable = current


NEGATIVE = (
    'bool-time', 'nan-time', 'inf-time', 'string-time', 'missing-time',
    'two-float-time', 'same-second-third', 'wrong-source', 'duplicate-owner',
    'id-alias-conflict', 'assistant-id-reuse', 'context-only-token',
    'different-token', 'api-conflict', 'attachment-conflict',
    'removed-owner', 'context-suffix', 'tokenless-successor',
)


@pytest.mark.parametrize('failure', NEGATIVE)
@pytest.mark.parametrize('pair', [False, True])
@pytest.mark.parametrize('cold', [False, True])
def test_ambiguous_rich_owner_combination_preserves_provider_payload(failure, pair, cold):
    from tests.test_cancel_restart_journal_recovery import _persist_multi_retry_turns
    sid = 'ambiguous-combination'
    streams = _persist_multi_retry_turns(sid, ['interrupted', 'cancelled'], [
        [('token', {'text': 'Repeated final answer'})],
        [('token', {'text': 'Repeated final answer'})],
    ])
    s = models.get_session(sid)
    assert not _stream_output(s, streams[0])
    display = next(r for r in s.messages if r.get('role') == 'user' and r.get('timestamp') == 10)
    owner = s.context_messages[0]
    display['timestamp'] = 10.125
    owner['timestamp'] = 10.125
    owner['api_content'] = 'Provider-only preserved payload'
    if pair:
        s.context_messages[1:1] = _tool_pair('ambiguous-native', 'Preserved result')
    if failure in {'bool-time', 'nan-time', 'inf-time', 'string-time', 'missing-time', 'two-float-time'}:
        owner['timestamp'] = {
            'bool-time': True, 'nan-time': float('nan'), 'inf-time': float('inf'),
            'string-time': '10.125', 'missing-time': None, 'two-float-time': 10.875,
        }[failure]
    elif failure == 'same-second-third':
        owner['timestamp'] = 10
        extra = copy.deepcopy(display)
        extra['timestamp'] = 10.875
        s.messages.insert(0, extra)
    elif failure == 'wrong-source':
        owner['_source'] = 'telegram'
    elif failure == 'duplicate-owner':
        s.context_messages.insert(1, copy.deepcopy(owner))
    elif failure == 'id-alias-conflict':
        display.update(id='owner-id', message_id='different-id')
        owner['id'] = 'owner-id'
    elif failure == 'assistant-id-reuse':
        display['id'] = owner['id'] = 'owner-id'
        s.context_messages.append({'role': 'assistant', 'content': 'Unrelated answer', 'id': 'owner-id'})
    elif failure == 'context-only-token':
        owner['_active_turn_token'] = 'unknown-owner-token'
    elif failure == 'different-token':
        display['_active_turn_token'] = 'display-token'
        owner['_active_turn_token'] = 'different-context-token'
    elif failure == 'api-conflict':
        display['api_content'] = 'Contradictory provider payload'
    elif failure == 'attachment-conflict':
        display['attachments'] = [{'name': 'display.txt'}]
        owner['attachments'] = [{'name': 'provider.txt'}]
    elif failure == 'removed-owner':
        s.context_messages = [{'role': 'system', 'content': 'Compressed state'}] + s.context_messages[1:]
    elif failure == 'context-suffix':
        s.context_messages = [owner, {'role': 'system', 'content': 'Newer work summary'}]
    elif failure == 'tokenless-successor':
        next(r for r in s.context_messages if r.get('role') == 'user' and r.get('timestamp') == 20).pop('_active_turn_token', None)
    s.save()
    import json
    before = json.dumps(s.context_messages, sort_keys=True)
    for _ in range(4):
        if cold:
            _simulate_restart()
        s = models.get_session(sid)
        assert json.dumps(s.context_messages, sort_keys=True) == before
        assert _pending_stream_hook(s, streams[0]) is None
        assert all(r.get('_recovered_display_only') is True for r in _stream_output(s, streams[0]))
        rows = _history(s, 'Prompt 0')
        # Equal prose from the newer answered turn remains; old output never
        # acquires provider authority through rich payload or tool equality.
        assert len([r for r in rows if r.get('content') == 'Repeated final answer']) == (0 if failure == 'context-suffix' else 1)
        if pair and failure not in {'context-suffix', 'removed-owner'}:
            assert any(r.get('tool_call_id') == 'ambiguous-native' for r in rows)


@pytest.mark.parametrize('state', ['ready', 'empty', 'live', 'nonterminal', 'expired', 'arriving'])
@pytest.mark.parametrize('cold', [False, True])
@pytest.mark.parametrize('ordinary', [False, True])
def test_four_turn_budget_and_native_pair_order(state, cold, ordinary):
    from tests.test_cancel_restart_journal_recovery import _persist_multi_retry_turns
    from api.run_journal import _run_path
    sid = 'four-turn-combination'
    kinds = ['interrupted', 'cancelled', 'ordinary' if ordinary else 'cancelled', 'cancelled']
    outputs = [
        [('token', {'text': 'Old interrupted answer'}), ('tool', {'name': 'read_file', 'tid': 'tool-0', 'args': {'path': '0.txt'}})],
        [],
        [] if ordinary else [('token', {'text': 'Middle answer'}), ('tool', {'name': 'read_file', 'tid': 'tool-2', 'args': {'path': '2.txt'}})],
        [('token', {'text': 'Newest answer'})] if state == 'ready' else [],
    ]
    streams = _persist_multi_retry_turns(sid, kinds, outputs)
    s = Session.load(sid)
    if not ordinary:
        next_owner = next(i for i, r in enumerate(s.context_messages) if r.get('content') == 'Prompt 3')
        s.context_messages[next_owner:next_owner] = _tool_pair('middle-native', 'Middle native result')
    newest = _pending_stream_hook(s, streams[-1])
    if state == 'live':
        config.ACTIVE_RUNS[streams[-1]] = {'session_id': sid, 'phase': 'cancelling', 'started_at': time.time()}
    elif state == 'nonterminal':
        newest['_journal_retry_process_token'] = models._JOURNAL_RECOVERY_PROCESS_TOKEN
        _run_path(sid, streams[-1]).unlink()
        RunJournalWriter(sid, streams[-1]).append_sse_event('token', {'text': 'Unsealed newest answer'})
    elif state == 'expired':
        newest['_journal_retry_attempts'] = models._JOURNAL_RETRY_MAX_ATTEMPTS
    elif state == 'arriving':
        _run_path(sid, streams[-1]).unlink()
    s.save()
    models.SESSIONS.clear()
    previous_attempt = 0
    for read in range(4):
        # Cold reads clear the cache, not the live-worker/process ownership;
        # a real interpreter restart would deliberately authorize nonterminal.
        if cold:
            models.SESSIONS.clear()
        s = models.get_session(sid)
        attempt = _pending_stream_hook(s, streams[1])['_journal_retry_attempts']
        assert 0 <= attempt - previous_attempt <= 1
        previous_attempt = attempt
        if state in {'live', 'nonterminal', 'arriving'}:
            assert _pending_stream_hook(s, streams[-1])['_journal_retry_attempts'] == 0
        if state == 'empty':
            assert _pending_stream_hook(s, streams[-1])['_journal_retry_attempts'] == read + 1
    _assert_retry_turn_ownership(s, kinds, streams)
    assert bool(_stream_output(s, streams[0])) is (not ordinary)
    assert previous_attempt >= 1
    expected = [('user', 'Prompt 0'), ('assistant', 'Old interrupted answer')] if not ordinary else [('user', 'Prompt 0')]
    if ordinary:
        expected += [('user', 'Prompt 2'), ('assistant', 'Ordinary answer 2')]
    else:
        expected += [('user', 'Prompt 2')]
        expected += [(r['role'], r['content']) for r in _tool_pair('middle-native', 'Middle native result')]
        expected += [('assistant', 'Middle answer')]
    if state == 'ready':
        expected += [('user', 'Prompt 3'), ('assistant', 'Newest answer')]
    assert [(r['role'], r.get('content', '')) for r in _history(s, 'Next question')] == expected

SOURCE_CASES = [
    ('zero', 0, False), ('false', False, False), ('empty-list', [], False),
    ('empty-dict', {}, False), ('missing', 'MISSING', True),
    ('none', None, True), ('empty-string', '', True), ('case-spaces', '  WEBUI  ', True),
    ('paired-whitespace', '   ', True),
]


@pytest.mark.parametrize('source_case', SOURCE_CASES, ids=lambda c: c[0])
@pytest.mark.parametrize('projection', ['display', 'context', 'both'])
@pytest.mark.parametrize('identity', ['fallback', 'shared-id', 'shared-token'])
def test_falsy_source_real_late_journal_authority(source_case, projection, identity, monkeypatch):
    from tests.test_cancel_restart_journal_recovery import _persist_multi_retry_turns
    label, value, should_recover = source_case
    sid = 'source-authority-combination'
    streams = _persist_multi_retry_turns(sid, ['interrupted', 'cancelled'], [
        [('token', {'text': 'Source-owned old answer'})],
        [('token', {'text': 'New answer'})],
    ], defer_first=True)
    s = models.get_session(sid)
    display = next(r for r in s.messages if r.get('content') == 'Prompt 0')
    context = s.context_messages[0]
    if identity == 'shared-id':
        display['id'] = context['id'] = 'source-owner-id'
    elif identity == 'shared-token':
        display['_active_turn_token'] = context['_active_turn_token'] = 'source-owner-token'
    targets = {'display': [display], 'context': [context], 'both': [display, context]}[projection]
    if label == 'paired-whitespace':
        targets = [display, context]
    for target in targets:
        if label == 'missing':
            target.pop('_source', None)
        else:
            target['_source'] = copy.deepcopy(value)
    s.save()
    writer = RunJournalWriter(sid, streams[0])
    writer.append_sse_event('token', {'text': 'Source-owned old answer'})
    writer.append_sse_event('cancel', {'message': 'Source old journal arrives after actual newer Stop'})
    observed = []
    original = models._journal_user_metadata_key
    def observe(row):
        if row.get('role') == 'user' and row.get('content') == 'Prompt 0':
            observed.append(copy.deepcopy(row.get('_source', 'MISSING')))
        return original(row)
    monkeypatch.setattr(models, '_journal_user_metadata_key', observe)
    _simulate_restart()
    s = models.get_session(sid)
    assert observed, 'actual ownership proof must execute'
    if projection == 'both' or label == 'paired-whitespace':
        assert all(item == value for item in observed) if label != 'missing' else all(item == 'MISSING' for item in observed)
    assert bool([r for r in _history(s, 'Next question') if r.get('content') == 'Source-owned old answer']) is should_recover
    if not should_recover:
        assert all(r.get('_recovered_display_only') is True for r in _stream_output(s, streams[0]))


@pytest.mark.parametrize('count', [1, 4, 16])
@pytest.mark.parametrize('expiry', ['none', 'attempt', 'age'])
def test_many_empty_hook_capacity_independent_budget(count, expiry):
    from tests.test_cancel_restart_journal_recovery import _persist_multi_retry_turns
    sid = 'capacity-retry'
    kinds = ['cancelled'] * (count + 1)
    outputs = [[('token', {'text': 'Oldest recoverable answer'})]] + [[] for _ in range(count)]
    streams = _persist_multi_retry_turns(sid, kinds, outputs)
    s = Session.load(sid)
    for index, stream in enumerate(streams[1:], start=1):
        hook = _pending_stream_hook(s, stream)
        hook['_journal_retry_attempts'] = index % 4
        if expiry == 'attempt' and index % 2:
            hook['_journal_retry_attempts'] = models._JOURNAL_RETRY_MAX_ATTEMPTS
        elif expiry == 'age' and index % 2:
            hook['_journal_retry_first_seen_ts'] = time.time() - models._JOURNAL_RETRY_GIVEUP_SECONDS - 1
    s.save()
    s = models.get_session(sid)
    assert _pending_stream_hook(s, streams[0]) is None
    assert len(_stream_output(s, streams[0])) == 1
    for index, stream in enumerate(streams[1:], start=1):
        hook = _pending_stream_hook(s, stream)
        if expiry != 'none' and index % 2:
            assert hook is None
        else:
            assert hook['_journal_retry_attempts'] == index % 4 + 1
    before = copy.deepcopy((s.messages, s.context_messages, s.tool_calls, s.updated_at))
    _simulate_restart()
    s = models.get_session(sid)
    assert len(_stream_output(s, streams[0])) == 1
    assert [(r['role'], r.get('content')) for r in _history(s, 'Next question')] == [
        ('user', 'Prompt 0'), ('assistant', 'Oldest recoverable answer'),
    ]
    assert s.updated_at == before[-1]
    _assert_retry_turn_ownership(s, kinds, streams)


@pytest.mark.parametrize('cold', [False, True])
@pytest.mark.parametrize('count', [1, 8, 32])
def test_many_same_named_tool_cards_exact_completion_and_stream_owner(count, cold):
    from tests.test_cancel_restart_journal_recovery import _persist_multi_retry_turns
    sid = 'tool-capacity-retry'
    events = []
    for index in range(count):
        events.append(('tool', {'name': 'read_file', 'tid': f'tool-{index}', 'args': {str(j): 'x' * 100 for j in range(20)}}))
    for index in reversed(range(count)):
        events.append(('tool_complete', {'name': 'read_file', 'tool_call_id': f'tool-{index}', 'preview': f'result-{index}', 'duration': index}))
    events.append(('token', {'text': 'Same final answer'}))
    streams = _persist_multi_retry_turns(sid, ['cancelled', 'cancelled'], [events, events])
    for _ in range(4):
        if cold:
            _simulate_restart()
        s = models.get_session(sid)
    assert len(s.tool_calls) == 2 * count
    for stream in streams:
        tools = [t for t in s.tool_calls if t.get('_recovered_stream_id') == stream]
        assert len(tools) == count
        for tool in tools:
            index = int(tool['tid'].split('-')[1])
            assert tool['done'] is True
            assert tool['preview'] == f'result-{index}'
            assert tool['duration'] == index
            assert len(tool['args']) <= 4
    _assert_retry_turn_ownership(s, ['cancelled', 'cancelled'], streams)
    assert [(r['role'], r.get('content', '')) for r in _history(s, 'Next question')] == [
        ('user', 'Prompt 0'), ('assistant', 'Same final answer'),
        ('user', 'Prompt 1'), ('assistant', 'Same final answer'),
    ]


@pytest.fixture(autouse=True)
def _runtime_test_bite_mutation(monkeypatch):
    """Opt-in runtime-only mutants; never edit production files."""
    import os
    mutation = os.environ.get('HERMES_PROBE_MUTATION')
    if mutation == 'producer-int':
        original = models._recovered_pending_timestamp
        monkeypatch.setattr(models, '_recovered_pending_timestamp', lambda value=None: int(original(value)))
    elif mutation == 'both-floats-bucket':
        original = models._journal_user_timestamps_match
        def buckets(left, right):
            if type(left) is float and type(right) is float:
                import math
                if math.isfinite(left) and math.isfinite(right):
                    return int(left) == int(right)
            return original(left, right)
        monkeypatch.setattr(models, '_journal_user_timestamps_match', buckets)


@pytest.mark.parametrize('mode', ['eager', 'deferred'])
@pytest.mark.parametrize('prompt', ['', ' ', '\n\t'])
@pytest.mark.parametrize('attachment', [False, True])
def test_empty_chat_input_does_not_create_recoverable_intent(mode, prompt, attachment, monkeypatch):
    from api import compression_continuation
    s = Session(session_id='empty-input-boundary', messages=[], context_messages=[])
    s.save()
    before = copy.deepcopy(s.__dict__)
    durable_before = (models.SESSION_DIR / f'{s.session_id}.json').read_bytes()
    monkeypatch.setattr(routes, 'get_webui_session_save_mode', lambda: mode)
    monkeypatch.setattr(routes, '_agent_runtime_barrier_response', lambda **kwargs: None)
    monkeypatch.setattr(routes, '_get_or_materialize_session', lambda *args, **kwargs: s)
    monkeypatch.setattr(compression_continuation, 'durable_compression_continuation', lambda session: (False, None))
    monkeypatch.setattr(routes, 'bad', lambda handler, message, *args, **kwargs: {'error': message})
    prepare = Mock(side_effect=AssertionError('empty request must not prepare a turn'))
    monkeypatch.setattr(routes, '_prepare_chat_start_session_for_stream', prepare)
    body = {'session_id': s.session_id, 'message': prompt}
    if attachment:
        body['attachments'] = [{'name': 'synthetic.png', 'path': '/tmp/nonexistent-synthetic.png'}]
    response = routes._handle_chat_start(Mock(), body)
    assert response == {'error': 'message is required'}
    assert not prepare.called
    assert s.__dict__ == before
    assert (models.SESSION_DIR / f'{s.session_id}.json').read_bytes() == durable_before
