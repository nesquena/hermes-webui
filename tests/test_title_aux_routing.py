"""Regression tests for auxiliary title-generation config routing.

Covers:
  - _aux_title_configured() broad detection (provider, model, base_url)
  - generate_title_raw_via_aux() reads timeout from config instead of hardcoding 15.0
  - aux→agent fallback triggers on 'llm_invalid_aux' status (Comment 1)
  - _aux_title_timeout rejects zero, negative, and non-numeric values (Comment 4)
"""
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

        mock_resp = MagicMock()
        mock_resp.choices = [MagicMock()]
        mock_resp.choices[0].message.content = 'Test Title'

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


class TestAuxTitleTimeoutEdgeCases(unittest.TestCase):
    """Comment 4: _aux_title_timeout must reject zero, negative, and non-numeric values."""

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
    """Comment 1: when aux returns llm_invalid_aux, the agent route must be tried as fallback.

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
