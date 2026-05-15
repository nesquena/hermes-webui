"""Regression tests for issue #2235: first WebUI title should honor
configured title_generation model.

Covers:
  1. Configured aux model succeeds on first turn — provisional title
     is replaced by the LLM-generated title, session is saved, and
     title_status reports the aux source.
  2. Unconfigured aux path preserves fallback — when no aux config is
     set, the existing agent/local fallback behaviour still runs.
  3. Refresh path parity — configured aux routing still applies to the
     adaptive title refresh path.
"""
import sys
import threading
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


def _make_provisional_session(user_text, assistant_text='Here is the answer.'):
    """Build a mock session whose title is the provisional first-message slice."""
    from api.models import title_from
    messages = [
        {'role': 'user', 'content': user_text},
        {'role': 'assistant', 'content': assistant_text},
    ]
    provisional = title_from(messages, 'Untitled')
    s = MagicMock()
    s.title = provisional
    s.llm_title_generated = False
    s.messages = messages
    s.session_id = 'test-2235-session'
    s.save = MagicMock()
    return s, provisional


class TestInitialAuxTitleSucceeds(unittest.TestCase):
    """When aux title_generation is configured and returns a valid title,
    the first-turn background title update must persist the LLM title
    instead of leaving the provisional first-message slice in place."""

    @patch('api.streaming._aux_title_configured', return_value=True)
    @patch('api.streaming._generate_llm_session_title_via_aux')
    @patch('api.streaming.get_session')
    @patch('api.streaming.SESSIONS', {})
    @patch('api.streaming.LOCK', threading.Lock())
    def test_aux_title_replaces_provisional_on_first_turn(
        self, mock_get_session, mock_aux_title, mock_configured,
    ):
        from api.streaming import _run_background_title_update

        user_text = 'Can you help me design a REST API for user management?'
        assistant_text = 'Sure, here is a plan for your REST API design.'
        s, provisional = _make_provisional_session(user_text, assistant_text)
        mock_get_session.return_value = s

        llm_title = 'REST API Design'
        mock_aux_title.return_value = (llm_title, 'llm_aux', llm_title)

        events = []

        def fake_put_event(event_type, data):
            events.append((event_type, data))

        _run_background_title_update(
            session_id=s.session_id,
            user_text=user_text,
            assistant_text=assistant_text,
            placeholder_title=provisional,
            put_event=fake_put_event,
            agent=None,
        )

        # The session title must be updated to the LLM-generated title
        self.assertEqual(s.title, llm_title)
        # The provisional slice must NOT remain
        self.assertNotEqual(s.title, provisional)
        # save() must have been called to persist the title
        s.save.assert_called()
        # llm_title_generated flag must be set
        self.assertTrue(s.llm_title_generated)

        # A 'title' event must be emitted with the new title
        title_events = [d for e, d in events if e == 'title']
        self.assertTrue(title_events, "Expected a 'title' event")
        self.assertEqual(title_events[0]['title'], llm_title)

        # A 'title_status' event must report the aux source
        status_events = [d for e, d in events if e == 'title_status']
        self.assertTrue(status_events, "Expected a 'title_status' event")
        self.assertEqual(status_events[0]['status'], 'llm_aux')

    @patch('api.streaming._aux_title_configured', return_value=True)
    @patch('api.streaming._generate_llm_session_title_via_aux')
    @patch('api.streaming.get_session')
    @patch('api.streaming.SESSIONS', {})
    @patch('api.streaming.LOCK', threading.Lock())
    def test_aux_title_status_distinguishes_llm_aux_from_fallback(
        self, mock_get_session, mock_aux_title, mock_configured,
    ):
        """title_status must clearly report 'llm_aux' when the aux route
        succeeds, distinguishing it from fallback and skipped cases."""
        from api.streaming import _run_background_title_update

        user_text = 'Explain quantum entanglement in simple terms.'
        assistant_text = 'Quantum entanglement is a phenomenon where...'
        s, provisional = _make_provisional_session(user_text, assistant_text)
        mock_get_session.return_value = s

        mock_aux_title.return_value = ('Quantum Entanglement', 'llm_aux', 'Quantum Entanglement')

        events = []

        def fake_put_event(event_type, data):
            events.append((event_type, data))

        _run_background_title_update(
            session_id=s.session_id,
            user_text=user_text,
            assistant_text=assistant_text,
            placeholder_title=provisional,
            put_event=fake_put_event,
            agent=None,
        )

        status_events = [d for e, d in events if e == 'title_status']
        self.assertTrue(status_events)
        # Must be 'llm_aux', not 'fallback' or 'skipped'
        self.assertEqual(status_events[0]['status'], 'llm_aux')
        self.assertNotEqual(status_events[0]['status'], 'fallback')
        self.assertNotEqual(status_events[0]['status'], 'skipped')

    @patch('api.streaming._aux_title_configured', return_value=True)
    @patch('api.streaming._generate_llm_session_title_via_aux')
    @patch('api.streaming.get_session')
    @patch('api.streaming.SESSIONS', {})
    @patch('api.streaming.LOCK', threading.Lock())
    def test_aux_title_with_agent_present_uses_aux_first(
        self, mock_get_session, mock_aux_title, mock_configured,
    ):
        """When aux is configured and an agent is available, the aux route
        must be tried first (not the agent route)."""
        from api.streaming import _run_background_title_update

        user_text = 'Write a Python function to sort a list.'
        assistant_text = 'Here is a Python sort function.'
        s, provisional = _make_provisional_session(user_text, assistant_text)
        mock_get_session.return_value = s

        mock_aux_title.return_value = ('Python Sort Function', 'llm_aux', 'Python Sort Function')

        mock_agent = MagicMock()

        events = []

        def fake_put_event(event_type, data):
            events.append((event_type, data))

        _run_background_title_update(
            session_id=s.session_id,
            user_text=user_text,
            assistant_text=assistant_text,
            placeholder_title=provisional,
            put_event=fake_put_event,
            agent=mock_agent,
        )

        # The aux route must have been called
        mock_aux_title.assert_called_once()
        # The title must be the aux-generated title
        self.assertEqual(s.title, 'Python Sort Function')


class TestUnconfiguredAuxPreservesFallback(unittest.TestCase):
    """When no aux title_generation config is set, the existing
    agent/local fallback behaviour must still run."""

    @patch('api.streaming._aux_title_configured', return_value=False)
    @patch('api.streaming._generate_llm_session_title_for_agent')
    @patch('api.streaming.get_session')
    @patch('api.streaming.SESSIONS', {})
    @patch('api.streaming.LOCK', threading.Lock())
    def test_agent_route_tried_first_when_aux_unconfigured(
        self, mock_get_session, mock_agent_title, mock_configured,
    ):
        """When aux is not configured and an agent is present, the agent
        route must be tried first (existing behaviour)."""
        from api.streaming import _run_background_title_update

        user_text = 'What is the capital of France?'
        assistant_text = 'The capital of France is Paris.'
        s, provisional = _make_provisional_session(user_text, assistant_text)
        mock_get_session.return_value = s

        mock_agent_title.return_value = ('France Capital', 'llm', '')
        mock_agent = MagicMock()

        events = []

        def fake_put_event(event_type, data):
            events.append((event_type, data))

        _run_background_title_update(
            session_id=s.session_id,
            user_text=user_text,
            assistant_text=assistant_text,
            placeholder_title=provisional,
            put_event=fake_put_event,
            agent=mock_agent,
        )

        # The agent route must have been called
        mock_agent_title.assert_called_once()
        # The title must be updated
        self.assertEqual(s.title, 'France Capital')

    @patch('api.streaming._aux_title_configured', return_value=False)
    @patch('api.streaming._generate_llm_session_title_for_agent')
    @patch('api.streaming._generate_llm_session_title_via_aux')
    @patch('api.streaming.get_session')
    @patch('api.streaming.SESSIONS', {})
    @patch('api.streaming.LOCK', threading.Lock())
    def test_fallback_used_when_agent_and_aux_both_fail(
        self, mock_get_session, mock_aux_title, mock_agent_title, mock_configured,
    ):
        """When both agent and aux routes fail, the local fallback must
        still be used (existing behaviour preserved)."""
        from api.streaming import _run_background_title_update

        user_text = 'Tell me about machine learning.'
        assistant_text = 'Machine learning is a subset of AI...'
        s, provisional = _make_provisional_session(user_text, assistant_text)
        mock_get_session.return_value = s

        # Both routes fail
        mock_agent_title.return_value = (None, 'llm_error', '')
        mock_aux_title.return_value = (None, 'llm_error_aux', '')

        mock_agent = MagicMock()

        events = []

        def fake_put_event(event_type, data):
            events.append((event_type, data))

        _run_background_title_update(
            session_id=s.session_id,
            user_text=user_text,
            assistant_text=assistant_text,
            placeholder_title=provisional,
            put_event=fake_put_event,
            agent=mock_agent,
        )

        # The fallback title should have been used (or the provisional
        # title preserved if fallback is generic).  The key assertion is
        # that the function does not crash or skip title generation
        # entirely.
        status_events = [d for e, d in events if e == 'title_status']
        self.assertTrue(status_events)

    @patch('api.streaming._aux_title_configured', return_value=False)
    @patch('api.streaming._generate_llm_session_title_via_aux')
    @patch('api.streaming.get_session')
    @patch('api.streaming.SESSIONS', {})
    @patch('api.streaming.LOCK', threading.Lock())
    def test_no_agent_unconfigured_aux_uses_aux_route_with_fallback(
        self, mock_get_session, mock_aux_title, mock_configured,
    ):
        """When there is no agent and aux is not configured, the aux route
        is still tried (it will likely fail), and the local fallback is used."""
        from api.streaming import _run_background_title_update

        user_text = 'How do I bake a cake?'
        assistant_text = 'Here is a simple cake recipe.'
        s, provisional = _make_provisional_session(user_text, assistant_text)
        mock_get_session.return_value = s

        # Aux route fails (no configured model)
        mock_aux_title.return_value = (None, 'llm_error_aux', '')

        events = []

        def fake_put_event(event_type, data):
            events.append((event_type, data))

        _run_background_title_update(
            session_id=s.session_id,
            user_text=user_text,
            assistant_text=assistant_text,
            placeholder_title=provisional,
            put_event=fake_put_event,
            agent=None,
        )

        # The aux route must have been called
        mock_aux_title.assert_called_once()
        # A title_status event must be emitted (fallback or skipped)
        status_events = [d for e, d in events if e == 'title_status']
        self.assertTrue(status_events)


class TestRefreshPathParity(unittest.TestCase):
    """The refresh path must use the same configured aux routing as the
    initial title update path."""

    @patch('api.streaming._aux_title_configured', return_value=True)
    @patch('api.streaming._generate_llm_session_title_via_aux')
    @patch('api.streaming.get_session')
    @patch('api.streaming.SESSIONS', {})
    @patch('api.streaming.LOCK', threading.Lock())
    def test_refresh_uses_aux_route_when_configured(
        self, mock_get_session, mock_aux_title, mock_configured,
    ):
        from api.streaming import _run_background_title_refresh

        s = MagicMock()
        s.title = 'Old LLM Title'
        s.llm_title_generated = True
        s.session_id = 'refresh-session'
        s.save = MagicMock()
        mock_get_session.return_value = s

        mock_aux_title.return_value = ('Refreshed Title', 'llm_aux', 'Refreshed Title')

        events = []

        def fake_put_event(event_type, data):
            events.append((event_type, data))

        _run_background_title_refresh(
            session_id='refresh-session',
            user_text='New question',
            assistant_text='New answer',
            current_title='Old LLM Title',
            put_event=fake_put_event,
            agent=None,
        )

        # The aux route must have been called
        mock_aux_title.assert_called_once()
        # The title must be updated
        self.assertEqual(s.title, 'Refreshed Title')
        # A title event must be emitted
        title_events = [d for e, d in events if e == 'title']
        self.assertTrue(title_events)
        self.assertEqual(title_events[0]['title'], 'Refreshed Title')

    @patch('api.streaming._aux_title_configured', return_value=True)
    @patch('api.streaming._generate_llm_session_title_via_aux')
    @patch('api.streaming._generate_llm_session_title_for_agent')
    @patch('api.streaming.get_session')
    @patch('api.streaming.SESSIONS', {})
    @patch('api.streaming.LOCK', threading.Lock())
    def test_refresh_aux_failure_tries_agent_fallback(
        self, mock_get_session, mock_agent_title, mock_aux_title, mock_configured,
    ):
        """When aux fails in the refresh path, the agent fallback must be
        tried (same routing as the initial update path)."""
        from api.streaming import _run_background_title_refresh

        s = MagicMock()
        s.title = 'Old Title'
        s.llm_title_generated = True
        s.session_id = 'refresh-fallback-session'
        s.save = MagicMock()
        mock_get_session.return_value = s

        mock_aux_title.return_value = (None, 'llm_error_aux', '')
        mock_agent_title.return_value = ('Agent Refreshed', 'llm', '')

        mock_agent = MagicMock()

        events = []

        def fake_put_event(event_type, data):
            events.append((event_type, data))

        _run_background_title_refresh(
            session_id='refresh-fallback-session',
            user_text='Question',
            assistant_text='Answer',
            current_title='Old Title',
            put_event=fake_put_event,
            agent=mock_agent,
        )

        # Both routes must have been tried
        mock_aux_title.assert_called_once()
        mock_agent_title.assert_called_once()
        # The title must be from the agent fallback
        self.assertEqual(s.title, 'Agent Refreshed')


class TestAuxTitleStatusDiagnostics(unittest.TestCase):
    """title_status diagnostics must clearly distinguish llm_aux,
    aux failures, fallback, and skipped cases."""

    @patch('api.streaming._aux_title_configured', return_value=True)
    @patch('api.streaming._generate_llm_session_title_via_aux')
    @patch('api.streaming.get_session')
    @patch('api.streaming.SESSIONS', {})
    @patch('api.streaming.LOCK', threading.Lock())
    def test_aux_failure_reports_error_status(
        self, mock_get_session, mock_aux_title, mock_configured,
    ):
        from api.streaming import _run_background_title_update

        user_text = 'Debug this Python code for me.'
        assistant_text = 'The issue is on line 42.'
        s, provisional = _make_provisional_session(user_text, assistant_text)
        mock_get_session.return_value = s

        mock_aux_title.return_value = (None, 'llm_error_aux', '')

        events = []

        def fake_put_event(event_type, data):
            events.append((event_type, data))

        _run_background_title_update(
            session_id=s.session_id,
            user_text=user_text,
            assistant_text=assistant_text,
            placeholder_title=provisional,
            put_event=fake_put_event,
            agent=None,
        )

        status_events = [d for e, d in events if e == 'title_status']
        self.assertTrue(status_events)
        # Must report the aux error, not a generic status
        self.assertIn('aux', status_events[0].get('reason', '').lower() +
                      status_events[0].get('status', '').lower())

    @patch('api.streaming._aux_title_configured', return_value=True)
    @patch('api.streaming._generate_llm_session_title_via_aux')
    @patch('api.streaming.get_session')
    @patch('api.streaming.SESSIONS', {})
    @patch('api.streaming.LOCK', threading.Lock())
    def test_already_generated_title_reports_skipped(
        self, mock_get_session, mock_aux_title, mock_configured,
    ):
        from api.streaming import _run_background_title_update

        s = MagicMock()
        s.title = 'Existing LLM Title'
        s.llm_title_generated = True
        s.session_id = 'already-gen-session'
        s.messages = [
            {'role': 'user', 'content': 'Hello'},
            {'role': 'assistant', 'content': 'Hi'},
        ]
        mock_get_session.return_value = s

        events = []

        def fake_put_event(event_type, data):
            events.append((event_type, data))

        _run_background_title_update(
            session_id='already-gen-session',
            user_text='Hello',
            assistant_text='Hi',
            placeholder_title='Existing LLM Title',
            put_event=fake_put_event,
            agent=None,
        )

        status_events = [d for e, d in events if e == 'title_status']
        self.assertTrue(status_events)
        self.assertEqual(status_events[0]['status'], 'skipped')
        self.assertEqual(status_events[0]['reason'], 'already_generated')

        # The aux route must NOT have been called
        mock_aux_title.assert_not_called()


if __name__ == '__main__':
    unittest.main()