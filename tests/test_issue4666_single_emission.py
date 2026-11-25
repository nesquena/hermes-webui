"""#4666 — one emission path per tool call, and id-based persisted summaries.

The legacy ``tool_progress_callback`` fires exactly four positionals
(``event_type, name, preview, args``) and the completed form adds keyword-only
``duration`` / ``is_error`` / ``result``.  When the structured
``tool_start_callback`` / ``tool_complete_callback`` are wired, both feeds reach
the WebUI closure for the same call, so the legacy path must not emit a second
card.  Persisted summaries must then resolve the live-progress fallback by tool
id, not by positional index.
"""

import copy
import queue
import sys
import types
import uuid
from unittest import mock

from api.streaming import _extract_tool_calls_from_messages

_MISSING = object()


def _tool_step(call_id, name='terminal'):
    return {
        'role': 'assistant', 'content': '',
        'tool_calls': [{'id': call_id, 'type': 'function',
                        'function': {'name': name, 'arguments': '{}'}}],
    }


def _tool_result(call_id, content='ok'):
    return {'role': 'tool', 'tool_call_id': call_id, 'content': content}


def _run_turn(script, final_messages, legacy=False):
    """Drive ``_run_agent_streaming`` with a fake agent; return (events, saved)."""
    import api.streaming as streaming

    saved_snapshots = []
    live_holder = []
    run_id = uuid.uuid4().hex[:12]

    class FakeSession:
        def __init__(self):
            self.session_id = f'emission_{run_id}'
            self.title = 'Emission'
            self.workspace = '/tmp'
            self.model = 'gpt-test'
            self.model_provider = None
            self.profile = None
            self.personality = None
            self.messages = []
            self.context_messages = []
            self.input_tokens = self.output_tokens = self.estimated_cost = 0
            self.cache_read_tokens = self.cache_write_tokens = 0
            self.active_stream_id = None
            self.pending_user_message = None
            self.pending_attachments = []
            self.pending_started_at = None
            self.context_length = self.threshold_tokens = self.last_prompt_tokens = 0
            self.llm_title_generated = True

        def save(self, *args, **kwargs):
            saved_snapshots.append(copy.deepcopy(self.messages))

        def compact(self):
            return {'session_id': self.session_id, 'title': self.title, 'workspace': self.workspace,
                    'model': self.model, 'created_at': 0, 'updated_at': 0, 'pinned': False,
                    'archived': False, 'project_id': None, 'profile': None, 'input_tokens': 0,
                    'output_tokens': 0, 'estimated_cost': 0, 'cache_read_tokens': 0,
                    'cache_write_tokens': 0, 'personality': None}

    class ScriptedAgent:
        def __init__(self, model=None, provider=None, base_url=None, platform=None, quiet_mode=False,
                     enabled_toolsets=None, fallback_model=None, session_id=None, session_db=None,
                     prefill_messages=None, stream_delta_callback=None, reasoning_callback=None,
                     tool_progress_callback=None, tool_start_callback=None, tool_complete_callback=None,
                     clarify_callback=None, interim_assistant_callback=None, **_kwargs):
            self.cb = {'token': stream_delta_callback, 'progress': tool_progress_callback,
                       'start': tool_start_callback, 'complete': tool_complete_callback}
            self.context_compressor = None
            self.session_prompt_tokens = self.session_completion_tokens = 0
            self.session_estimated_cost_usd = 0
            self.session_cache_read_tokens = self.session_cache_write_tokens = 0
            self.reasoning_config = None
            self.ephemeral_system_prompt = None
            self._last_error = None

        def _legacy_start(self, name, args):
            # Real agent/tool_executor.py:1029 — four positionals, no tool-call id.
            self.cb['progress']('tool.started', name, 'preview', args)

        def _legacy_complete(self, name, result):
            # Real agent/tool_executor.py:1167 — four positionals plus keyword-only extras.
            self.cb['progress']('tool.completed', name, None, None,
                                duration=0.1, is_error=False, result=result)

        def run_conversation(self, **kwargs):
            for kind, *rest in script:
                if kind == 'tool':
                    tid, name = rest
                    self._legacy_start(name, {'cmd': 'x'})
                    if self.cb['start'] is not None:
                        self.cb['start'](tid, name, {'cmd': 'x'})
                        self.cb['complete'](tid, name, {'cmd': 'x'}, 'ok')
                    else:
                        self._legacy_complete(name, 'ok')
                elif kind == 'start_only':
                    tid, name = rest
                    self._legacy_start(name, {'cmd': 'x'})
                    if self.cb['start'] is not None:
                        self.cb['start'](tid, name, {'cmd': 'x'})
                elif kind == 'complete_only':
                    tid, name = rest
                    self._legacy_complete(name, 'ok')
                    if self.cb['complete'] is not None:
                        self.cb['complete'](tid, name, {'cmd': 'x'}, 'ok')
                elif kind == 'token':
                    self.cb['token'](rest[0])
            # Snapshot the live ledger while the turn is still running: teardown
            # drops it from the registry (#1361), so reading after the call
            # cannot observe it.
            _live = next(iter(streaming.STREAM_LIVE_TOOL_CALLS.values()), []) or []
            live_holder.append([dict(tc) for tc in _live])
            return {'messages': kwargs.get('conversation_history', []) + [
                {'role': 'user', 'content': kwargs['persist_user_message']},
            ] + copy.deepcopy(final_messages)}

        def interrupt(self, _message):
            pass

    class LegacyScriptedAgent(ScriptedAgent):
        def __init__(self, model=None, provider=None, base_url=None, platform=None, quiet_mode=False,
                     enabled_toolsets=None, fallback_model=None, session_id=None, session_db=None,
                     prefill_messages=None, stream_delta_callback=None, reasoning_callback=None,
                     tool_progress_callback=None, clarify_callback=None, interim_assistant_callback=None,
                     **_kwargs):
            super().__init__(stream_delta_callback=stream_delta_callback,
                             reasoning_callback=reasoning_callback,
                             tool_progress_callback=tool_progress_callback,
                             interim_assistant_callback=interim_assistant_callback)

    agent_cls = LegacyScriptedAgent if legacy else ScriptedAgent
    fake_session = FakeSession()
    stream_id = f'stream_emission_{run_id}'
    fake_session.active_stream_id = stream_id
    runtime_module = types.ModuleType('hermes_cli.runtime_provider')
    payload = {'provider': 'openai', 'base_url': None, 'api_mode': 'chat_completions',
               'command': None, 'args': [], 'credential_pool': None}
    payload['api_' + 'key'] = '***'
    runtime_module.__dict__['resolve_runtime_provider'] = mock.Mock(return_value=payload)
    hermes_cli = types.ModuleType('hermes_cli')
    hermes_cli.__dict__['runtime_provider'] = runtime_module
    hermes_state = types.ModuleType('hermes_state')
    hermes_state.__dict__['SessionDB'] = mock.Mock(return_value=None)
    injected = {'hermes_cli': hermes_cli, 'hermes_cli.runtime_provider': runtime_module,
                'hermes_state': hermes_state}
    saved_mods = {k: sys.modules.get(k, _MISSING) for k in injected}
    sys.modules.update(injected)
    captured = []
    try:
        with mock.patch.object(streaming, 'get_session', return_value=fake_session), \
             mock.patch.object(streaming, '_get_ai_agent', return_value=agent_cls), \
             mock.patch.object(streaming, 'resolve_model_provider', return_value=('gpt-test', 'openai', None)), \
             mock.patch('api.config.get_config', return_value={}), \
             mock.patch('api.config._resolve_cli_toolsets', return_value=[]):
            q = queue.Queue()
            streaming.STREAMS[stream_id] = q
            # The stream registry is initialised when the stream is published;
            # the live ledger mirror only appends when the key already exists.
            streaming.STREAM_LIVE_TOOL_CALLS[stream_id] = []
            streaming._run_agent_streaming(session_id=fake_session.session_id, msg_text='go',
                                           model='gpt-test', workspace='/tmp', stream_id=stream_id)
            while not q.empty():
                captured.append(q.get_nowait())
            live_calls = live_holder[-1] if live_holder else []
    finally:
        streaming.STREAMS.pop(stream_id, None)
        streaming.STREAM_LIVE_TOOL_CALLS.pop(stream_id, None)
        for k, prev in saved_mods.items():
            if prev is _MISSING:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = prev
    assert saved_snapshots, 'turn must be saved'
    return captured, saved_snapshots[-1], live_calls


def _events(events, name):
    return [data for ev, data in events if ev == name]


def test_structured_callbacks_are_the_single_owner_of_the_tool_card(cleanup_test_sessions):
    """A four-positional legacy pair plus the structured pair yields ONE card."""
    events, _saved, _live = _run_turn(
        [('tool', 'c1', 'terminal'), ('token', 'done')],
        [_tool_step('c1'), _tool_result('c1'),
         {'role': 'assistant', 'content': 'done'}],
    )
    starts = _events(events, 'tool')
    completes = _events(events, 'tool_complete')
    assert len(starts) == 1, f'expected one tool start, got {len(starts)}: {starts}'
    assert len(completes) == 1, f'expected one tool completion, got {len(completes)}: {completes}'
    assert starts[0].get('tid') == 'c1'
    assert completes[0].get('tid') == 'c1'


def test_concurrent_same_name_calls_keep_distinct_cards(cleanup_test_sessions):
    """Two same-name calls in flight: two cards, each completed by its own id."""
    events, _saved, live = _run_turn(
        [('start_only', 'c1', 'terminal'), ('start_only', 'c2', 'terminal'),
         ('complete_only', 'c1', 'terminal'), ('complete_only', 'c2', 'terminal'),
         ('token', 'done')],
        [_tool_step('c1'), _tool_result('c1'), _tool_step('c2'), _tool_result('c2'),
         {'role': 'assistant', 'content': 'done'}],
    )
    starts = _events(events, 'tool')
    completes = _events(events, 'tool_complete')
    assert len(starts) == 2, f'expected two tool starts, got {len(starts)}: {starts}'
    assert len(completes) == 2, f'expected two tool completions, got {len(completes)}'
    assert sorted(d.get('tid') for d in starts) == ['c1', 'c2']
    assert sorted(d.get('tid') for d in completes) == ['c1', 'c2']
    assert len(live) == 2, f'live ledger must hold one entry per call, got {live}'
    assert all(tc.get('done') for tc in live), live


def test_legacy_only_agent_still_emits_exactly_once(cleanup_test_sessions):
    """Agent builds without the structured callbacks keep the legacy path (#4666)."""
    events, _saved, _live = _run_turn(
        [('tool', 'c1', 'terminal'), ('token', 'done')],
        [_tool_step('c1'), _tool_result('c1'),
         {'role': 'assistant', 'content': 'done'}],
        legacy=True,
    )
    starts = _events(events, 'tool')
    completes = _events(events, 'tool_complete')
    assert len(starts) == 1, f'legacy start must still emit: {starts}'
    assert len(completes) == 1, f'legacy completion must still emit: {completes}'
    assert starts[0].get('name') == 'terminal'
    assert completes[0].get('name') == 'terminal'


def test_persisted_summary_resolves_live_fallback_by_tool_id():
    """A row whose id the live ledger knows keeps its own name/args, not a neighbour's."""
    messages = [
        {'role': 'user', 'content': 'q'},
        # A is announced and resolves by id; B is a result whose call never
        # reached the transcript, and it arrives BEFORE A's row.
        _tool_step('A', 'terminal'),
        _tool_result('B', 'written'),
        _tool_result('A', 'ls ok'),
        {'role': 'assistant', 'content': 'done'},
    ]
    live = [
        {'name': 'terminal', 'args': {'cmd': 'ls'}, 'tid': 'A'},
        {'name': 'write_file', 'args': {'path': 'y'}, 'tid': 'B'},
    ]
    calls = _extract_tool_calls_from_messages(messages, live_tool_calls=live)
    by_tid = {c.get('tid'): c for c in calls}
    assert 'B' in by_tid, f'the unrouted row must not be relabelled away: {calls}'
    assert by_tid['B']['name'] == 'write_file', by_tid['B']
    assert by_tid['B']['args'] == {'path': 'y'}, by_tid['B']


def test_persisted_summary_keeps_a_row_the_live_ledger_knows():
    """Resolved rows must not push a later known id past the end of the ledger."""
    messages = [
        {'role': 'user', 'content': 'q'},
        _tool_step('A', 'terminal'),
        _tool_result('A', 'ls ok'),
        _tool_step('C', 'read_file'),
        _tool_result('C', 'file body'),
        _tool_result('D', 'written'),
        {'role': 'assistant', 'content': 'done'},
    ]
    # Partial ledger, as a resumed stream carries: D is known, A and C are not.
    live = [{'name': 'write_file', 'args': {'path': 'z'}, 'tid': 'D'}]
    calls = _extract_tool_calls_from_messages(messages, live_tool_calls=live)
    by_tid = {c.get('tid'): c for c in calls}
    assert 'D' in by_tid, f'the row the ledger knows must not vanish: {calls}'
    assert by_tid['D']['name'] == 'write_file', by_tid['D']


def test_persisted_summary_keeps_positional_pairing_without_ids():
    """Rows without a tool id still consume the live ledger in order."""
    messages = [
        {'role': 'user', 'content': 'q'},
        _tool_step('A', 'terminal'),
        {'role': 'tool', 'content': 'ok'},
        {'role': 'assistant', 'content': 'done'},
    ]
    live = [{'name': 'terminal', 'args': {'cmd': 'ls'}}]
    calls = _extract_tool_calls_from_messages(messages, live_tool_calls=live)
    assert len(calls) == 1, calls
    assert calls[0]['name'] == 'terminal', calls[0]


def test_persisted_summary_does_not_drop_a_row_the_ledger_cannot_name():
    """An id the ledger does not know still pairs positionally, as it did before."""
    messages = [
        {'role': 'user', 'content': 'q'},
        _tool_step('A', 'terminal'),
        _tool_result('A', 'ls ok'),
        _tool_result('Z', 'written'),
        {'role': 'assistant', 'content': 'done'},
    ]
    live = [
        {'name': 'terminal', 'args': {}, 'tid': 'A'},
        {'name': 'write_file', 'args': {'path': 'z'}, 'tid': 'B'},
    ]
    calls = _extract_tool_calls_from_messages(messages, live_tool_calls=live)
    assert len(calls) == 2, f'the unknown-id row must not vanish: {calls}'
    by_tid = {c.get('tid'): c for c in calls}
    assert by_tid['Z']['name'] == 'write_file', by_tid['Z']
