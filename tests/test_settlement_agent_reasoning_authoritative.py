"""Settlement attaches each streamed reasoning segment to the step that produced it.

Adaptive-thinking models skip reasoning on some tool-call steps, so the live
segment index drifts; stream-only providers return ``reasoning: None`` on the
final message. Ownership is bound per tool call when the tool starts.
"""

import copy
import queue
import sys
import types
import uuid
from types import SimpleNamespace
from unittest import mock

from api.streaming import _settle_turn_reasoning

_MISSING = object()


def _tool_step(call_id, reasoning):
    return {
        'role': 'assistant', 'content': '', 'reasoning': reasoning,
        'tool_calls': [{'id': call_id, 'type': 'function', 'function': {'name': 'terminal', 'arguments': '{}'}}],
    }


def _tool_result(call_id):
    return {'role': 'tool', 'tool_call_id': call_id, 'content': 'ok'}


def _reasonings(messages):
    return [m.get('reasoning') for m in messages if m.get('role') == 'assistant']


def test_agent_reasoning_wins_over_drifted_segments():
    prior = [{'role': 'user', 'content': 'q0'}, {'role': 'assistant', 'content': 'a0', 'reasoning': 'old'}]
    s = SimpleNamespace(messages=prior + [
        {'role': 'user', 'content': 'q1'},
        _tool_step('c1', 'think A'), _tool_result('c1'),
        _tool_step('c2', None), _tool_result('c2'),
        _tool_step('c3', 'think C'), _tool_result('c3'),
        {'role': 'assistant', 'content': 'done', 'reasoning': None},
    ])
    _settle_turn_reasoning(s, prior, {0: 'think A', 1: 'think C'}, {'c1': [0], 'c2': [None], 'c3': [1]}, None,
                           tool_start_order=['c1', 'c2', 'c3'])
    assert _reasonings(s.messages) == ['old', 'think A', None, 'think C', None]


def test_segments_fill_positionally_without_tool_bindings():
    s = SimpleNamespace(messages=[
        {'role': 'user', 'content': 'q'},
        {'role': 'assistant', 'content': '', 'tool_calls': [{'id': 'c1'}]},
        _tool_result('c1'),
        {'role': 'assistant', 'content': 'answer', 'reasoning': None},
    ])
    _settle_turn_reasoning(s, [], {0: 'seg0', 1: 'seg1'})
    assert _reasonings(s.messages) == ['seg0', 'seg1']


def test_inline_think_still_split_from_content():
    s = SimpleNamespace(messages=[
        {'role': 'user', 'content': 'q'},
        {'role': 'assistant', 'content': '<think>plan</think>\nanswer', 'reasoning': None},
    ])
    _settle_turn_reasoning(s, [], {})
    assert s.messages[1]['content'] == 'answer'
    assert s.messages[1]['reasoning'] == 'plan'


def _run_turn(script, final_messages, legacy=False):
    """Drive _run_agent_streaming with a fake agent; return the messages last saved.

    ``legacy=True`` uses an Agent whose constructor predates ``tool_start_callback`` /
    ``tool_complete_callback`` (Agent builds before 2026-04), so the WebUI falls back to
    positional settlement.
    """
    import api.streaming as streaming

    saved_snapshots = []
    run_id = uuid.uuid4().hex[:12]

    class FakeSession:
        def __init__(self):
            self.session_id = f'settle_reasoning_{run_id}'
            self.title = 'Settle'
            self.workspace = '/tmp'
            self.model = 'gpt-test'
            self.model_provider = None
            self.profile = None
            self.personality = None
            self.messages = []
            self.context_messages = []
            self.input_tokens = self.output_tokens = self.estimated_cost = 0
            self.cache_read_tokens = self.cache_write_tokens = 0
            self.tool_calls = []
            self.gateway_routing = None
            self.gateway_routing_history = []
            self.active_stream_id = ''
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
            self.cb = {'reasoning': reasoning_callback, 'progress': tool_progress_callback,
                       'start': tool_start_callback, 'complete': tool_complete_callback,
                       'token': stream_delta_callback, 'interim': interim_assistant_callback}
            self.context_compressor = None
            self.session_prompt_tokens = self.session_completion_tokens = 0
            self.session_estimated_cost_usd = 0
            self.session_cache_read_tokens = self.session_cache_write_tokens = 0
            self.reasoning_config = None
            self.ephemeral_system_prompt = None
            self._last_error = None

        def run_conversation(self, **kwargs):
            for kind, value in script:
                if kind == 'reasoning':
                    self.cb['reasoning'](value)
                elif kind == 'tool':
                    # tool_executor order: progress 'tool.started', then tool_start_callback.
                    self.cb['progress']('tool.started', 'terminal', 'ls', {})
                    if self.cb['start'] is not None:
                        self.cb['start'](value, 'terminal', {})
                        self.cb['complete'](value, 'terminal', {}, 'ok')
                    else:  # legacy Agent: only progress events, no start/complete callbacks
                        self.cb['progress']('tool.completed', 'terminal', 'ls', {})
                elif kind == 'interim':  # agent/stream_delivery.py:_deliver_interim
                    self.cb['interim'](value, already_streamed=False)
                elif kind == 'token':
                    self.cb['token'](value)
            return {'messages': kwargs.get('conversation_history', []) + [
                {'role': 'user', 'content': kwargs['persist_user_message']},
            ] + copy.deepcopy(final_messages)}

        def interrupt(self, _message):
            pass

    class LegacyScriptedAgent(ScriptedAgent):
        # Constructor without tool_start_callback / tool_complete_callback, like Agent
        # builds before the callback was added; the WebUI inspects this signature.
        def __init__(self, model=None, provider=None, base_url=None, platform=None, quiet_mode=False,
                     enabled_toolsets=None, fallback_model=None, session_id=None, session_db=None,
                     prefill_messages=None, stream_delta_callback=None, reasoning_callback=None,
                     tool_progress_callback=None, clarify_callback=None, interim_assistant_callback=None,
                     **_kwargs):
            super().__init__(stream_delta_callback=stream_delta_callback, reasoning_callback=reasoning_callback,
                             tool_progress_callback=tool_progress_callback,
                             interim_assistant_callback=interim_assistant_callback)

    agent_cls = LegacyScriptedAgent if legacy else ScriptedAgent
    fake_session = FakeSession()
    stream_id = f'stream_settle_reasoning_{run_id}'
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
    try:
        with mock.patch.object(streaming, 'get_session', return_value=fake_session), \
             mock.patch.object(streaming, '_get_ai_agent', return_value=agent_cls), \
             mock.patch.object(streaming, 'resolve_model_provider', return_value=('gpt-test', 'openai', None)), \
             mock.patch('api.config.get_config', return_value={}), \
             mock.patch('api.config._resolve_cli_toolsets', return_value=[]):
            streaming.STREAMS[stream_id] = queue.Queue()
            streaming._run_agent_streaming(session_id=fake_session.session_id, msg_text='go',
                                           model='gpt-test', workspace='/tmp', stream_id=stream_id)
    finally:
        streaming.STREAMS.pop(stream_id, None)
        for k, prev in saved_mods.items():
            if prev is _MISSING:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = prev
    assert saved_snapshots, 'turn must be saved'
    return saved_snapshots[-1]


def _parallel_step(call_ids, reasoning):
    step = _tool_step(call_ids[0], reasoning)
    step['tool_calls'] = [dict(step['tool_calls'][0], id=cid) for cid in call_ids]
    return step


def test_parallel_tool_calls_keep_reasoning_whichever_call_started_first(cleanup_test_sessions):
    saved = _run_turn(
        [('reasoning', 'plan both'), ('tool', 'p2'), ('tool', 'p1'), ('token', 'done')],
        [_parallel_step(['p1', 'p2'], None), _tool_result('p1'), _tool_result('p2'),
         {'role': 'assistant', 'content': 'done', 'reasoning': None}],
    )
    assert _reasonings(saved) == ['plan both', None]


def test_stream_only_reasoning_survives_reload(cleanup_test_sessions):
    saved = _run_turn(
        [('reasoning', 'streamed thinking'), ('token', 'answer')],
        [{'role': 'assistant', 'content': 'answer', 'reasoning': None}],
    )
    assert _reasonings(saved) == ['streamed thinking']


def test_skipped_thinking_tool_step_keeps_each_card_on_its_step(cleanup_test_sessions):
    saved = _run_turn(
        [('reasoning', 'think A'), ('tool', 'c1'), ('tool', 'c2'),
         ('reasoning', 'think C'), ('tool', 'c3'), ('token', 'done')],
        [_tool_step('c1', 'think A'), _tool_result('c1'),
         _tool_step('c2', None), _tool_result('c2'),
         _tool_step('c3', 'think C'), _tool_result('c3'),
         {'role': 'assistant', 'content': 'done', 'reasoning': None}],
    )
    assert _reasonings(saved) == ['think A', None, 'think C', None]


def test_stream_only_and_skipped_thinking_in_one_turn(cleanup_test_sessions):
    saved = _run_turn(
        [('reasoning', 'think A'), ('tool', 'c1'), ('tool', 'c2'),
         ('reasoning', 'think C'), ('tool', 'c3'),
         ('reasoning', 'final thinking'), ('token', 'done')],
        [_tool_step('c1', 'think A'), _tool_result('c1'),
         _tool_step('c2', None), _tool_result('c2'),
         _tool_step('c3', None), _tool_result('c3'),  # stream-only on this step
         {'role': 'assistant', 'content': 'done', 'reasoning': None}],
    )
    assert _reasonings(saved) == ['think A', None, 'think C', 'final thinking']


def test_interim_step_keeps_reasoning_when_next_tool_step_skips_thinking(cleanup_test_sessions):
    # A visible non-tool step, then a tool step that streamed no thinking: the
    # tool call must not claim the interim step's segment.
    saved = _run_turn(
        [('reasoning', 'think A'), ('interim', 'Let me check.'), ('tool', 'c1'),
         ('reasoning', 'final thinking'), ('token', 'done')],
        [{'role': 'assistant', 'content': 'Let me check.', 'reasoning': None},
         _tool_step('c1', None), _tool_result('c1'),
         {'role': 'assistant', 'content': 'done', 'reasoning': None}],
    )
    assert _reasonings(saved) == ['think A', None, 'final thinking']


def test_interim_step_after_skipped_thinking_is_not_shifted(cleanup_test_sessions):
    # Assistant ordinals and segment indexes diverge after a skipped step.
    saved = _run_turn(
        [('reasoning', 'think A'), ('tool', 'c1'), ('tool', 'c2'),
         ('reasoning', 'think B'), ('interim', 'Halfway there.'),
         ('reasoning', 'final thinking'), ('token', 'done')],
        [_tool_step('c1', None), _tool_result('c1'),
         _tool_step('c2', None), _tool_result('c2'),
         {'role': 'assistant', 'content': 'Halfway there.', 'reasoning': None},
         {'role': 'assistant', 'content': 'done', 'reasoning': None}],
    )
    assert _reasonings(saved) == ['think A', None, 'think B', 'final thinking']


def test_tool_step_with_visible_commentary_keeps_its_reasoning(cleanup_test_sessions):
    # agent/turn_tool_round.py emits the tool step's content as an interim
    # message before its tools start.
    saved = _run_turn(
        [('reasoning', 'think A'), ('interim', 'Checking the logs.'), ('tool', 'c1'),
         ('tool', 'c2'), ('reasoning', 'final thinking'), ('token', 'done')],
        [dict(_tool_step('c1', None), content='Checking the logs.'), _tool_result('c1'),
         _tool_step('c2', None), _tool_result('c2'),
         {'role': 'assistant', 'content': 'done', 'reasoning': None}],
    )
    assert _reasonings(saved) == ['think A', None, 'final thinking']


def test_unmatched_interim_does_not_block_later_ones():
    s = SimpleNamespace(messages=[
        {'role': 'user', 'content': 'q'},
        {'role': 'assistant', 'content': 'Second note.', 'reasoning': None},
        {'role': 'assistant', 'content': 'done', 'reasoning': None},
    ])
    _settle_turn_reasoning(s, [], {0: 'seg0', 1: 'seg1', 2: 'seg2'}, {}, 2,
                           [('Firstnote.', 0), ('Secondnote.', 1)])
    assert _reasonings(s.messages) == ['seg1', 'seg2']


def test_omitted_prefix_interim_does_not_claim_later_step():
    s = SimpleNamespace(messages=[
        {'role': 'user', 'content': 'q'},
        {'role': 'assistant', 'content': 'Checking logs', 'reasoning': None},
        {'role': 'assistant', 'content': 'done', 'reasoning': None},
    ])
    _settle_turn_reasoning(s, [], {0: 'seg0', 1: 'seg1', 2: 'seg2'}, {}, 2,
                           [('Check', 0), ('Checkinglogs', 1)])
    assert _reasonings(s.messages) == ['seg1', 'seg2']


def _bare_tool_step():
    return {'role': 'assistant', 'content': '', 'reasoning': None}  # no tool_calls


def test_bare_tool_step_owns_reasoning_via_tool_result_id(cleanup_test_sessions):
    saved = _run_turn(
        [('reasoning', 'think A'), ('tool', 'c1'), ('reasoning', 'final thinking'), ('token', 'done')],
        [_bare_tool_step(), _tool_result('c1'),
         {'role': 'assistant', 'content': 'done', 'reasoning': None}],
    )
    assert _reasonings(saved) == ['think A', 'final thinking']


def test_bare_tool_steps_without_ids_follow_live_tool_start_order(cleanup_test_sessions):
    saved = _run_turn(
        [('reasoning', 'think A'), ('tool', 'c1'), ('tool', 'c2'),
         ('reasoning', 'think C'), ('tool', 'c3'), ('token', 'done')],
        [_bare_tool_step(), {'role': 'tool', 'content': 'ok'},
         _bare_tool_step(), {'role': 'tool', 'content': 'ok'},
         _bare_tool_step(), {'role': 'tool', 'content': 'ok'},
         {'role': 'assistant', 'content': 'done', 'reasoning': None}],
    )
    assert _reasonings(saved) == ['think A', None, 'think C', None]


def test_mixed_id_and_idless_results_do_not_shift_later_step(cleanup_test_sessions):
    saved = _run_turn(
        [('reasoning', 'think A'), ('tool', 'c1'), ('tool', 'c2'),
         ('reasoning', 'think B'), ('tool', 'c3'), ('token', 'done')],
        [_bare_tool_step(), _tool_result('c1'), {'role': 'tool', 'content': 'ok'},
         _bare_tool_step(), {'role': 'tool', 'content': 'ok'},
         {'role': 'assistant', 'content': 'done', 'reasoning': None}],
    )
    assert _reasonings(saved) == ['think A', 'think B', None]


def test_start_without_persisted_result_leaves_idless_steps_unbound(cleanup_test_sessions):
    # c2 started but its result was never persisted: 3 starts vs 2 ID-less
    # results can't be matched one-to-one, so no step borrows another's card.
    saved = _run_turn(
        [('reasoning', 'think A'), ('tool', 'c1'), ('tool', 'c2'),
         ('reasoning', 'think B'), ('tool', 'c3'), ('token', 'done')],
        [_bare_tool_step(), {'role': 'tool', 'content': 'ok'},
         _bare_tool_step(), {'role': 'tool', 'content': 'ok'},
         {'role': 'assistant', 'content': 'done', 'reasoning': None}],
    )
    assert _reasonings(saved) == [None, None, None]


def test_call_id_repeated_across_steps_keeps_reasoning_in_step_order(cleanup_test_sessions):
    # deterministic_call_id() yields the same ID for an identical call at the
    # same index in two responses ("run the same check again").
    saved = _run_turn(
        [('reasoning', 'think A'), ('tool', 'dup'), ('reasoning', 'think B'), ('tool', 'dup'),
         ('token', 'done')],
        [_tool_step('dup', None), _tool_result('dup'),
         _tool_step('dup', None), _tool_result('dup'),
         {'role': 'assistant', 'content': 'done', 'reasoning': None}],
    )
    assert _reasonings(saved) == ['think A', 'think B', None]


def test_idless_results_before_reused_call_id_keep_start_order(cleanup_test_sessions):
    # Starts dup, c2, dup; saved: two ID-less results, then an explicit dup.
    # Slots align with starts in order, so the later dup must not claim the first.
    saved = _run_turn(
        [('reasoning', 'think A'), ('tool', 'dup'), ('reasoning', 'think B'), ('tool', 'c2'),
         ('reasoning', 'think C'), ('tool', 'dup'), ('token', 'done')],
        [_bare_tool_step(), {'role': 'tool', 'content': 'ok'},
         _bare_tool_step(), {'role': 'tool', 'content': 'ok'},
         _tool_step('dup', None), _tool_result('dup'),
         {'role': 'assistant', 'content': 'done', 'reasoning': None}],
    )
    assert _reasonings(saved) == ['think A', 'think B', 'think C', None]


def test_explicit_id_out_of_start_order_leaves_idless_steps_unbound(cleanup_test_sessions):
    # Starts c1, c2 but the saved explicit c1 sits in the second slot: the
    # in-order alignment is contradicted, so the ID-less step stays unbound.
    saved = _run_turn(
        [('reasoning', 'think A'), ('tool', 'c1'), ('reasoning', 'think B'), ('tool', 'c2'),
         ('token', 'done')],
        [_bare_tool_step(), {'role': 'tool', 'content': 'ok'},
         _tool_step('c1', None), _tool_result('c1'),
         {'role': 'assistant', 'content': 'done', 'reasoning': None}],
    )
    assert _reasonings(saved) == [None, 'think A', None]


def _codex_commentary_step(call_id, text, reasoning):
    # agent/codex_responses_adapter.py: commentary lives in codex_message_items, content is ''
    step = _tool_step(call_id, reasoning)
    step['codex_message_items'] = [{'type': 'message', 'role': 'assistant', 'phase': 'commentary',
                                    'content': [{'type': 'output_text', 'text': text}]}]
    return step


_CODEX_SCRIPT = [('reasoning', 'think A'), ('interim', 'Running the test suite.'), ('tool', 'c1')]


def test_codex_commentary_final_repeats_commentary_with_agent_reasoning(cleanup_test_sessions):
    saved = _run_turn(
        _CODEX_SCRIPT + [('reasoning', 'final'), ('token', 'Running the test suite. All green.')],
        [_codex_commentary_step('c1', 'Running the test suite.', 'think A'), _tool_result('c1'),
         {'role': 'assistant', 'content': 'Running the test suite. All green.', 'reasoning': None}],
    )
    assert _reasonings(saved) == ['think A', 'final']


def test_codex_commentary_final_repeats_commentary_without_thinking(cleanup_test_sessions):
    saved = _run_turn(
        _CODEX_SCRIPT + [('token', 'Running the test suite. All green.')],
        [_codex_commentary_step('c1', 'Running the test suite.', 'think A'), _tool_result('c1'),
         {'role': 'assistant', 'content': 'Running the test suite. All green.', 'reasoning': None}],
    )
    assert _reasonings(saved) == ['think A', None]


def test_codex_commentary_step_without_agent_reasoning_keeps_stream_segment(cleanup_test_sessions):
    saved = _run_turn(
        _CODEX_SCRIPT + [('reasoning', 'final'), ('token', 'Running the test suite. All green.')],
        [_codex_commentary_step('c1', 'Running the test suite.', None), _tool_result('c1'),
         {'role': 'assistant', 'content': 'Running the test suite. All green.', 'reasoning': None}],
    )
    assert _reasonings(saved) == ['think A', 'final']


def test_codex_commentary_final_does_not_repeat_commentary(cleanup_test_sessions):
    saved = _run_turn(
        _CODEX_SCRIPT + [('reasoning', 'final'), ('token', 'All green.')],
        [_codex_commentary_step('c1', 'Running the test suite.', 'think A'), _tool_result('c1'),
         {'role': 'assistant', 'content': 'All green.', 'reasoning': None}],
    )
    assert _reasonings(saved) == ['think A', 'final']


# Agent builds that predate tool_start_callback (before 2026-04) get positional settlement:
# the final step's open segment must not override the positional mapping, or a silent
# final step inherits the previous step's thinking (release gate #7788).
_FINAL_SILENT = {'role': 'assistant', 'content': 'done', 'reasoning': None}


def test_legacy_agent_final_silent_does_not_inherit_tool_step_thinking(cleanup_test_sessions):
    saved = _run_turn(
        [('reasoning', 'think A'), ('tool', 'c1'), ('token', 'done')],
        [_tool_step('c1', None), _tool_result('c1'), dict(_FINAL_SILENT)],
        legacy=True,
    )
    assert _reasonings(saved) == ['think A', None]


def test_legacy_agent_final_with_own_thinking_keeps_it(cleanup_test_sessions):
    saved = _run_turn(
        [('reasoning', 'think A'), ('tool', 'c1'), ('reasoning', 'final'), ('token', 'done')],
        [_tool_step('c1', None), _tool_result('c1'), dict(_FINAL_SILENT)],
        legacy=True,
    )
    assert _reasonings(saved) == ['think A', 'final']


def test_legacy_agent_two_silent_steps_after_thinking(cleanup_test_sessions):
    saved = _run_turn(
        [('reasoning', 'think A'), ('tool', 'c1'), ('tool', 'c2'), ('token', 'done')],
        [_tool_step('c1', None), _tool_result('c1'), _tool_step('c2', None), _tool_result('c2'),
         dict(_FINAL_SILENT)],
        legacy=True,
    )
    assert _reasonings(saved) == ['think A', None, None]


def test_legacy_agent_interim_step_then_silent_final(cleanup_test_sessions):
    saved = _run_turn(
        [('reasoning', 'think A'), ('interim', 'Checking.'), ('tool', 'c1'), ('token', 'done')],
        [dict(_tool_step('c1', None), content='Checking.'), _tool_result('c1'), dict(_FINAL_SILENT)],
        legacy=True,
    )
    assert _reasonings(saved) == ['think A', None]
