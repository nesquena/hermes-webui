"""Regression tests for auxiliary title-generation config routing.

Covers:
  - _aux_title_configured() broad detection (provider, model, base_url)
  - generate_title_raw_via_aux() reads timeout from config instead of hardcoding 15.0
  - aux→agent fallback triggers on 'llm_invalid_aux' status
  - _aux_title_timeout rejects zero, negative, and non-numeric values
"""
import os
import sys
import types
import unittest
from unittest.mock import MagicMock, patch

# Stub agent.auxiliary_client so it is importable in the test environment
# (the real package lives in hermes-agent, which is not installed here).
_agent_stub = types.ModuleType('agent')
_aux_stub = types.ModuleType('agent.auxiliary_client')
sys.modules.setdefault('agent', _agent_stub)
sys.modules.setdefault('agent.auxiliary_client', _aux_stub)
_agent_stub.auxiliary_client = _aux_stub


def _patch_tg_config(config_dict):
    """Return a patch context manager that makes _get_auxiliary_task_config return config_dict."""
    return patch('agent.auxiliary_client._get_auxiliary_task_config', return_value=config_dict, create=True)


class TestAuxTitleConfigured(unittest.TestCase):
    def _call(self, tg_config):
        from api.streaming import _aux_title_configured
        with _patch_tg_config(tg_config):
            return _aux_title_configured()

    def test_model_set_returns_true(self):
        self.assertTrue(self._call({'provider': '', 'model': 'gpt-4o-mini', 'base_url': ''}))

    def test_base_url_set_returns_true(self):
        self.assertTrue(self._call({'provider': '', 'model': '', 'base_url': 'http://localhost:1234'}))

    def test_provider_set_non_auto_returns_true(self):
        self.assertTrue(self._call({'provider': 'openai', 'model': '', 'base_url': ''}))

    def test_provider_auto_returns_false(self):
        self.assertFalse(self._call({'provider': 'auto', 'model': '', 'base_url': ''}))

    def test_provider_auto_case_insensitive_returns_false(self):
        self.assertFalse(self._call({'provider': 'AUTO', 'model': '', 'base_url': ''}))

    def test_all_empty_returns_false(self):
        self.assertFalse(self._call({'provider': '', 'model': '', 'base_url': ''}))

    def test_empty_dict_returns_false(self):
        self.assertFalse(self._call({}))

    def test_provider_configured_model_blank_returns_true(self):
        """Regression: provider set + blank model must still be treated as configured."""
        self.assertTrue(self._call({'provider': 'anthropic', 'model': '', 'base_url': ''}))

    def test_base_url_only_returns_true(self):
        """Regression: base_url alone (no model) must still be treated as configured."""
        self.assertTrue(self._call({'provider': '', 'model': '', 'base_url': 'https://api.example.com'}))

    def test_import_error_returns_false(self):
        from api.streaming import _aux_title_configured
        with patch('agent.auxiliary_client._get_auxiliary_task_config', side_effect=ImportError("no module"), create=True):
            self.assertFalse(_aux_title_configured())


class TestGenerateTitleRawViaAuxTimeout(unittest.TestCase):
    """Verify generate_title_raw_via_aux() reads timeout from config rather than hardcoding 15.0."""

    def _run_with_config(self, tg_config, expected_timeout):
        from api.streaming import generate_title_raw_via_aux

        mock_resp = types.SimpleNamespace(
            choices=[
                types.SimpleNamespace(
                    message=types.SimpleNamespace(content='Test Title'),
                    finish_reason='stop',
                )
            ]
        )

        captured = {}

        def fake_call_llm(**kwargs):
            captured['timeout'] = kwargs.get('timeout')
            return mock_resp

        with _patch_tg_config(tg_config):
            with patch('agent.auxiliary_client.call_llm', side_effect=fake_call_llm, create=True):
                result, status = generate_title_raw_via_aux(
                    user_text='What is the weather?',
                    assistant_text='It is sunny.',
                )

        self.assertEqual(result, 'Test Title')
        self.assertAlmostEqual(captured['timeout'], expected_timeout)

    def test_default_timeout_when_not_set(self):
        """No timeout in config → uses 15.0 default."""
        self._run_with_config({'provider': '', 'model': 'gpt-4o', 'base_url': ''}, 15.0)

    def test_custom_timeout_from_config(self):
        """Regression: timeout set in config must be used instead of hardcoded 15.0."""
        self._run_with_config(
            {'provider': '', 'model': 'gpt-4o', 'base_url': '', 'timeout': 30.0},
            30.0,
        )

    def test_integer_timeout_from_config(self):
        """Config timeout as int is coerced to float."""
        self._run_with_config(
            {'provider': '', 'model': 'gpt-4o', 'base_url': '', 'timeout': 5},
            5.0,
        )

    def test_timeout_none_in_config_falls_back_to_default(self):
        """Explicit None in config falls back to 15.0."""
        self._run_with_config(
            {'provider': '', 'model': 'gpt-4o', 'base_url': '', 'timeout': None},
            15.0,
        )


class TestReasoningModelTitleGeneration(unittest.TestCase):
    """Regression coverage for reasoning models that spend output budget on reasoning."""

    def test_title_budget_defaults_to_reasoning_safe_value(self):
        """Title generation should not use a tiny output cap that starves final content."""
        from api.streaming import _title_completion_budget, _title_retry_completion_budget

        self.assertEqual(_title_completion_budget(), 512)
        self.assertEqual(_title_retry_completion_budget(), 1024)

    def test_aux_short_circuits_on_empty_reasoning_without_retrying(self):
        """Regression for #2083: reasoning models that emit only hidden
        reasoning tokens (no visible content) must NOT trigger a budget-doubling
        retry — the second call invariably produces the same empty-reasoning
        shape and just doubles the GPU/credit burn.  Short-circuit to the local
        fallback path instead."""
        from api.streaming import generate_title_raw_via_aux

        call_count = [0]

        def fake_call_llm(**kwargs):
            call_count[0] += 1
            return {
                'choices': [
                    {
                        'message': {'content': '', 'reasoning': 'long hidden reasoning'},
                        'finish_reason': 'length',
                    }
                ]
            }

        with _patch_tg_config({'provider': 'ollama', 'model': 'kimi-k2.6', 'base_url': 'https://ollama.com/v1'}):
            with patch('agent.auxiliary_client.call_llm', side_effect=fake_call_llm, create=True):
                result, status = generate_title_raw_via_aux(
                    user_text='Hey nur ein kurzer Test',
                    assistant_text='Alles klar, ich helfe dir dabei.',
                )

        self.assertIsNone(result)
        self.assertEqual(status, 'llm_empty_reasoning_aux')
        # One call per prompt at the base budget — no retry on prompt 0, no
        # second-prompt attempt either (short-circuited).
        self.assertEqual(call_count[0], 1)

    def test_aux_still_retries_finish_length_without_reasoning(self):
        """Length-truncated responses WITHOUT reasoning tokens still get the
        budget-doubling retry — those are legitimately recoverable by giving
        the model more headroom."""
        from api.streaming import generate_title_raw_via_aux

        responses = [
            {'choices': [{'message': {'content': ''}, 'finish_reason': 'length'}]},
            {'choices': [{'message': {'content': 'Useful Session Title'}, 'finish_reason': 'stop'}]},
        ]
        captured_budgets = []

        def fake_call_llm(**kwargs):
            captured_budgets.append(kwargs.get('max_tokens'))
            return responses.pop(0)

        with _patch_tg_config({'provider': 'ollama', 'model': 'kimi-k2.6', 'base_url': 'https://ollama.com/v1'}):
            with patch('agent.auxiliary_client.call_llm', side_effect=fake_call_llm, create=True):
                result, status = generate_title_raw_via_aux(
                    user_text='Hey nur ein kurzer Test',
                    assistant_text='Alles klar, ich helfe dir dabei.',
                )

        self.assertEqual(result, 'Useful Session Title')
        self.assertEqual(status, 'llm_aux_retry')
        self.assertEqual(captured_budgets, [512, 1024])

    def test_aux_returns_specific_status_when_reasoning_retry_still_empty(self):
        """Diagnostics should expose the provider failure mode instead of generic llm_error_aux."""
        from api.streaming import generate_title_raw_via_aux

        def empty_length_response(**kwargs):
            return {
                'choices': [
                    {
                        'message': {'content': '', 'reasoning': 'still reasoning'},
                        'finish_reason': 'length',
                    }
                ]
            }

        with _patch_tg_config({'provider': 'ollama', 'model': 'kimi-k2.6', 'base_url': 'https://ollama.com/v1'}):
            with patch('agent.auxiliary_client.call_llm', side_effect=empty_length_response, create=True):
                result, status = generate_title_raw_via_aux(
                    user_text='Hey nur ein kurzer Test',
                    assistant_text='Alles klar, ich helfe dir dabei.',
                )

        self.assertIsNone(result)
        self.assertEqual(status, 'llm_empty_reasoning_aux')

    def test_agent_route_short_circuits_on_empty_reasoning_without_retrying(self):
        """Regression for #2083 on the active-agent route: empty-reasoning
        responses must NOT trigger a budget-doubling retry."""
        from api.streaming import generate_title_raw_via_agent

        call_count = [0]

        def fake_create(**kwargs):
            call_count[0] += 1
            return {
                'choices': [
                    {
                        'message': {'content': '', 'reasoning': 'long hidden reasoning'},
                        'finish_reason': 'length',
                    }
                ]
            }

        client = types.SimpleNamespace(
            chat=types.SimpleNamespace(
                completions=types.SimpleNamespace(create=fake_create)
            )
        )
        agent = MagicMock()
        agent.api_mode = 'openai'
        agent.provider = 'ollama'
        agent.model = 'kimi-k2.6'
        agent.base_url = 'https://ollama.com/v1'
        agent.reasoning_config = None
        agent._build_api_kwargs.return_value = {}
        agent._ensure_primary_openai_client.return_value = client

        result, status = generate_title_raw_via_agent(
            agent,
            user_text='Hey nur ein kurzer Test',
            assistant_text='Alles klar, ich helfe dir dabei.',
        )

        self.assertIsNone(result)
        self.assertEqual(status, 'llm_empty_reasoning')
        # One call per prompt at base budget — no retry, no second-prompt attempt.
        self.assertEqual(call_count[0], 1)
        self.assertIsNone(agent.reasoning_config)

    def test_agent_route_still_retries_finish_length_without_reasoning(self):
        """The active-agent route should preserve retry-on-length-no-reasoning."""
        from api.streaming import generate_title_raw_via_agent

        responses = [
            {'choices': [{'message': {'content': ''}, 'finish_reason': 'length'}]},
            {'choices': [{'message': {'content': 'Agent Session Title'}, 'finish_reason': 'stop'}]},
        ]
        captured_budgets = []

        def fake_create(**kwargs):
            captured_budgets.append(kwargs.get('max_tokens') or kwargs.get('max_completion_tokens'))
            return responses.pop(0)

        client = types.SimpleNamespace(
            chat=types.SimpleNamespace(
                completions=types.SimpleNamespace(create=fake_create)
            )
        )
        agent = MagicMock()
        agent.api_mode = 'openai'
        agent.provider = 'ollama'
        agent.model = 'kimi-k2.6'
        agent.base_url = 'https://ollama.com/v1'
        agent.reasoning_config = None
        agent._build_api_kwargs.return_value = {}
        agent._ensure_primary_openai_client.return_value = client

        result, status = generate_title_raw_via_agent(
            agent,
            user_text='Hey nur ein kurzer Test',
            assistant_text='Alles klar, ich helfe dir dabei.',
        )

        self.assertEqual(result, 'Agent Session Title')
        self.assertEqual(status, 'llm_retry')
        self.assertEqual(captured_budgets, [512, 1024])
        self.assertIsNone(agent.reasoning_config)

    @patch('api.streaming._aux_title_configured', return_value=True)
    @patch('api.streaming._generate_llm_session_title_via_aux')
    @patch('api.streaming.get_session')
    def test_fallback_title_status_keeps_underlying_llm_reason(
        self, mock_get_session, mock_aux_title, mock_configured,
    ):
        """Local fallback should not hide that the LLM failed because it hit length."""
        from api.streaming import _run_background_title_update

        mock_session = MagicMock()
        mock_session.title = 'Untitled'
        mock_session.llm_title_generated = False
        mock_session.messages = [
            {'role': 'user', 'content': 'Hey nur ein kurzer Test'},
            {'role': 'assistant', 'content': 'Alles klar, ich helfe dir dabei.'},
        ]
        mock_get_session.return_value = mock_session
        mock_aux_title.return_value = (None, 'llm_length_aux', '')
        events = []

        _run_background_title_update(
            session_id='reasoning-title-session',
            user_text='Hey nur ein kurzer Test',
            assistant_text='Alles klar, ich helfe dir dabei.',
            placeholder_title='Untitled',
            put_event=lambda event_type, data: events.append((event_type, data)),
            agent=None,
        )

        title_status = [data for event_type, data in events if event_type == 'title_status']
        self.assertTrue(title_status)
        self.assertEqual(title_status[0]['status'], 'fallback')
        self.assertEqual(title_status[0]['reason'], 'local_summary:llm_length_aux')

    @patch('api.streaming._aux_title_configured', return_value=True)
    @patch('api.streaming._generate_llm_session_title_via_aux')
    @patch('api.streaming.get_session')
    def test_generic_fallback_title_is_not_persisted(
        self, mock_get_session, mock_aux_title, mock_configured,
    ):
        """A generic local fallback is worse than the provisional first-message title."""
        from api.streaming import _run_background_title_update

        provisional_title = '\u5e2e\u6211\u53bb\u627e\u4e00\u672c\u300a\u7ea2\u697c\u68a6\u300b\u7535\u5b50\u4e66'
        first_user_text = provisional_title + '\u3002'
        mock_session = MagicMock()
        mock_session.title = provisional_title
        mock_session.llm_title_generated = False
        mock_session.messages = [
            {'role': 'user', 'content': first_user_text},
            {'role': 'assistant', 'content': ''},
        ]
        mock_get_session.return_value = mock_session
        mock_aux_title.return_value = (None, 'llm_error_aux', '')
        events = []

        _run_background_title_update(
            session_id='generic-title-session',
            user_text=first_user_text,
            assistant_text='',
            placeholder_title=provisional_title,
            put_event=lambda event_type, data: events.append((event_type, data)),
            agent=None,
        )

        title_events = [data for event_type, data in events if event_type == 'title']
        title_status = [data for event_type, data in events if event_type == 'title_status']
        self.assertEqual(title_events, [])
        self.assertTrue(title_status)
        self.assertEqual(title_status[0]['status'], 'skipped')
        self.assertEqual(title_status[0]['reason'], 'llm_error_aux')
        self.assertEqual(title_status[0]['title'], provisional_title)
        self.assertEqual(mock_session.title, provisional_title)
        self.assertFalse(mock_session.llm_title_generated)
        mock_session.save.assert_not_called()


class TestBackgroundTitleProfileRouting(unittest.TestCase):
    def test_profile_env_context_logs_fail_open_resolution_errors(self):
        """Profile env setup failures should be diagnosable without breaking workers."""
        import api.profiles as profiles

        session = types.SimpleNamespace(profile='work')
        captured = {}

        with patch(
            'api.profiles.get_hermes_home_for_profile',
            side_effect=RuntimeError('profile lookup failed'),
        ):
            with patch.dict(os.environ, {'HERMES_HOME': 'default-home'}, clear=False):
                with self.assertLogs('api.profiles', level='DEBUG') as logs:
                    with profiles.profile_env_for_background_worker(session, 'background title'):
                        captured['HERMES_HOME'] = os.environ.get('HERMES_HOME')

        message_found = any(
            'Failed to resolve profile env for background title profile work' in record.getMessage()
            for record in logs.records
        )
        self.assertEqual(captured['HERMES_HOME'], 'default-home')
        self.assertTrue(message_found)
        self.assertTrue(any(record.exc_info for record in logs.records))

    def test_skill_home_snapshot_removes_modules_imported_during_context(self):
        """Modules first imported inside a temporary profile context must not leak."""
        import api.profiles as profiles

        original_parent = sys.modules.get('tools')
        original_skill_module = sys.modules.get('tools.skills_tool')
        original_manager_module = sys.modules.get('tools.skill_manager_tool')

        sys.modules.pop('tools.skills_tool', None)
        sys.modules.pop('tools.skill_manager_tool', None)
        tools_parent = types.ModuleType('tools')
        sys.modules['tools'] = tools_parent
        try:
            snapshot = profiles.snapshot_skill_home_modules()

            imported_during_context = types.ModuleType('tools.skills_tool')
            setattr(imported_during_context, 'HERMES_HOME', 'profile-home')
            setattr(imported_during_context, 'SKILLS_DIR', 'profile-home/skills')
            sys.modules['tools.skills_tool'] = imported_during_context
            setattr(tools_parent, 'skills_tool', imported_during_context)

            profiles.restore_skill_home_modules(snapshot)

            self.assertNotIn('tools.skills_tool', sys.modules)
            self.assertFalse(hasattr(tools_parent, 'skills_tool'))
        finally:
            sys.modules.pop('tools.skills_tool', None)
            sys.modules.pop('tools.skill_manager_tool', None)
            if original_parent is None:
                sys.modules.pop('tools', None)
            else:
                sys.modules['tools'] = original_parent
            if original_skill_module is not None:
                sys.modules['tools.skills_tool'] = original_skill_module
            if original_manager_module is not None:
                sys.modules['tools.skill_manager_tool'] = original_manager_module

    @patch('api.streaming._aux_title_configured', return_value=True)
    @patch('api.streaming.get_session')
    def test_background_title_generation_uses_session_profile_home(
        self, mock_get_session, mock_configured,
    ):
        """A background title worker for a non-default profile must resolve aux config from that profile."""
        from api.streaming import _run_background_title_update

        mock_session = MagicMock()
        mock_session.title = 'Untitled'
        mock_session.profile = 'work'
        mock_session.llm_title_generated = False
        mock_session.messages = [
            {'role': 'user', 'content': 'This is a test message'},
            {'role': 'assistant', 'content': 'Received.'},
        ]
        mock_get_session.return_value = mock_session

        captured = {}

        original_skill_module = sys.modules.get('tools.skills_tool')
        fake_skill_module = types.ModuleType('tools.skills_tool')
        setattr(fake_skill_module, 'HERMES_HOME', 'default-home')
        setattr(fake_skill_module, 'SKILLS_DIR', 'default-home/skills')
        sys.modules['tools.skills_tool'] = fake_skill_module

        def fake_aux_title(*args, **kwargs):
            captured['hermes_home'] = os.environ.get('HERMES_HOME')
            captured['skill_module_home'] = getattr(fake_skill_module, 'HERMES_HOME')
            captured['skill_module_dir'] = getattr(fake_skill_module, 'SKILLS_DIR')
            return ('Profile Routed Title', 'llm_aux', '')

        events = []
        try:
            with patch('api.profiles.get_hermes_home_for_profile', return_value='profile-home'):
                with patch('api.streaming._generate_llm_session_title_via_aux', side_effect=fake_aux_title):
                    with patch.dict(os.environ, {'HERMES_HOME': 'default-home'}, clear=False):
                        _run_background_title_update(
                            session_id='profile-title-session',
                            user_text='This is a test message',
                            assistant_text='Received.',
                            placeholder_title='Untitled',
                            put_event=lambda event_type, data: events.append((event_type, data)),
                            agent=None,
                        )
                        captured['restored_hermes_home'] = os.environ.get('HERMES_HOME')
        finally:
            if original_skill_module is None:
                sys.modules.pop('tools.skills_tool', None)
            else:
                sys.modules['tools.skills_tool'] = original_skill_module

        self.assertEqual(captured.get('hermes_home'), 'profile-home')
        self.assertEqual(str(captured.get('skill_module_home')), 'profile-home')
        self.assertEqual(str(captured.get('skill_module_dir')), 'profile-home/skills')
        self.assertEqual(captured.get('restored_hermes_home'), 'default-home')
        self.assertEqual(getattr(fake_skill_module, 'HERMES_HOME'), 'default-home')
        self.assertEqual(getattr(fake_skill_module, 'SKILLS_DIR'), 'default-home/skills')
        self.assertEqual(mock_session.title, 'Profile Routed Title')


class TestAuxTitleTimeoutEdgeCases(unittest.TestCase):
    """_aux_title_timeout must reject zero, negative, and non-numeric values."""

    def _call(self, tg_config, default=15.0):
        from api.streaming import _aux_title_timeout
        with _patch_tg_config(tg_config):
            return _aux_title_timeout(default=default)

    def test_timeout_zero_falls_back_to_default(self):
        """timeout: 0 is not strictly positive → fall back to default."""
        result = self._call({'timeout': 0}, default=15.0)
        self.assertEqual(result, 15.0)

    def test_timeout_negative_falls_back_to_default(self):
        """timeout: -1 is not strictly positive → fall back to default."""
        result = self._call({'timeout': -1}, default=15.0)
        self.assertEqual(result, 15.0)

    def test_timeout_non_numeric_string_falls_back_to_default(self):
        """timeout: 'abc' cannot be coerced to float → fall back to default."""
        result = self._call({'timeout': 'abc'}, default=15.0)
        self.assertEqual(result, 15.0)

    def test_timeout_empty_string_falls_back_to_default(self):
        """timeout: '' cannot be coerced to a positive float → fall back to default."""
        result = self._call({'timeout': ''}, default=15.0)
        self.assertEqual(result, 15.0)

    def test_timeout_positive_passes_through(self):
        """A valid positive timeout is returned as-is."""
        result = self._call({'timeout': 25.0}, default=15.0)
        self.assertEqual(result, 25.0)

    def test_custom_default_used_on_invalid(self):
        """When the value is invalid, the caller-supplied *default* is returned."""
        result = self._call({'timeout': 0}, default=20.0)
        self.assertEqual(result, 20.0)


class TestAuxInvalidAuxTriggersAgentFallback(unittest.TestCase):
    """When aux returns llm_invalid_aux, the agent route must be tried as fallback.

    Pins the behaviour so the fallback tuple in _run_background_title_update
    stays synchronised with the statuses that _generate_llm_session_title_via_aux
    actually emits.
    """

    @patch('api.streaming._aux_title_configured', return_value=True)
    @patch('api.streaming._generate_llm_session_title_via_aux')
    @patch('api.streaming._generate_llm_session_title_for_agent')
    @patch('api.streaming.get_session')
    def test_llm_invalid_aux_triggers_agent_fallback(
        self, mock_get_session, mock_agent_title, mock_aux_title, mock_configured,
    ):
        """Simulate aux returning (None, 'llm_invalid_aux', '...') and verify agent fallback fires."""
        from api.streaming import _run_background_title_update

        # Build a mock session that passes all the pre-checks
        mock_session = MagicMock()
        mock_session.title = 'Untitled'
        mock_session.llm_title_generated = False
        mock_session.messages = [
            {'role': 'user', 'content': 'What is the weather?'},
            {'role': 'assistant', 'content': 'It is sunny and warm.'},
        ]
        mock_get_session.return_value = mock_session

        # aux route returns invalid title
        mock_aux_title.return_value = (None, 'llm_invalid_aux', 'bad thinking preamble')

        # agent route succeeds
        mock_agent_title.return_value = ('Weather Report', 'llm', '')

        events = []

        def fake_put_event(event_type, data):
            events.append((event_type, data))

        _run_background_title_update(
            session_id='test-session',
            user_text='What is the weather?',
            assistant_text='It is sunny and warm.',
            placeholder_title='Untitled',
            put_event=fake_put_event,
            agent=MagicMock(),
        )

        # The agent fallback must have been invoked
        mock_agent_title.assert_called_once()

        # A title must have been produced via the agent route
        title_events = [(e, d) for e, d in events if e == 'title']
        self.assertTrue(len(title_events) > 0, "Expected a 'title' event to be emitted")
        self.assertEqual(title_events[0][1]['title'], 'Weather Report')

    @patch('api.streaming._aux_title_configured', return_value=True)
    @patch('api.streaming._generate_llm_session_title_via_aux')
    @patch('api.streaming._generate_llm_session_title_for_agent')
    @patch('api.streaming.get_session')
    def test_llm_error_aux_triggers_agent_fallback(
        self, mock_get_session, mock_agent_title, mock_aux_title, mock_configured,
    ):
        """Simulate aux returning (None, 'llm_error_aux', '') and verify agent fallback fires."""
        from api.streaming import _run_background_title_update

        mock_session = MagicMock()
        mock_session.title = 'Untitled'
        mock_session.llm_title_generated = False
        mock_session.messages = [
            {'role': 'user', 'content': 'Tell me a joke.'},
            {'role': 'assistant', 'content': 'Why did the chicken cross the road?'},
        ]
        mock_get_session.return_value = mock_session

        mock_aux_title.return_value = (None, 'llm_error_aux', '')
        mock_agent_title.return_value = ('Chicken Joke', 'llm', '')

        events = []

        def fake_put_event(event_type, data):
            events.append((event_type, data))

        _run_background_title_update(
            session_id='test-session-2',
            user_text='Tell me a joke.',
            assistant_text='Why did the chicken cross the road?',
            placeholder_title='Untitled',
            put_event=fake_put_event,
            agent=MagicMock(),
        )

        mock_agent_title.assert_called_once()

    @patch('api.streaming._aux_title_configured', return_value=True)
    @patch('api.streaming._generate_llm_session_title_via_aux')
    @patch('api.streaming._generate_llm_session_title_for_agent')
    @patch('api.streaming.get_session')
    def test_success_status_does_not_trigger_agent_fallback(
        self, mock_get_session, mock_agent_title, mock_aux_title, mock_configured,
    ):
        """When aux succeeds, the agent route must NOT be called."""
        from api.streaming import _run_background_title_update

        mock_session = MagicMock()
        mock_session.title = 'Untitled'
        mock_session.llm_title_generated = False
        mock_session.messages = [
            {'role': 'user', 'content': 'Hello'},
            {'role': 'assistant', 'content': 'Hi there'},
        ]
        mock_get_session.return_value = mock_session

        # aux succeeds on first try
        mock_aux_title.return_value = ('Greeting', 'llm_aux', '')

        events = []

        def fake_put_event(event_type, data):
            events.append((event_type, data))

        _run_background_title_update(
            session_id='test-session-3',
            user_text='Hello',
            assistant_text='Hi there',
            placeholder_title='Untitled',
            put_event=fake_put_event,
            agent=MagicMock(),
        )

        # Agent route must NOT have been invoked
        mock_agent_title.assert_not_called()


if __name__ == '__main__':
    unittest.main()
