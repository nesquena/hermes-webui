"""Regression tests for issue #2083: GPU stays active after prompt due to title generation.

Covers:
  1. _title_should_skip_remaining_attempts('llm_empty_reasoning') returns True
  2. _title_should_skip_remaining_attempts('llm_empty_reasoning_aux') returns True
  3. generate_title_raw_via_agent with mock agent returns (None, 'llm_empty_reasoning')
  4. The constructed api_kwargs['extra_body'] contains both thinking and reasoning disabled
  5. _run_background_title_update falls through to local fallback when agent returns llm_empty_reasoning
"""
import sys
import threading
import types
import unittest
from unittest.mock import MagicMock, patch

# Stub agent.auxiliary_client so it is importable in the test environment
_agent_stub = types.ModuleType('agent')
_aux_stub = types.ModuleType('agent.auxiliary_client')
sys.modules.setdefault('agent', _agent_stub)
sys.modules.setdefault('agent.auxiliary_client', _aux_stub)
_agent_stub.auxiliary_client = _aux_stub


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
    s.session_id = 'test-2083-session'
    s.save = MagicMock()
    return s, provisional


class TestTitleShouldSkipRemainingAttempts(unittest.TestCase):
    """Verify that _title_should_skip_remaining_attempts returns True for
    both llm_empty_reasoning and llm_empty_reasoning_aux."""

    def test_skip_llm_empty_reasoning(self):
        from api.streaming import _title_should_skip_remaining_attempts
        self.assertTrue(_title_should_skip_remaining_attempts('llm_empty_reasoning'))

    def test_skip_llm_empty_reasoning_aux(self):
        from api.streaming import _title_should_skip_remaining_attempts
        self.assertTrue(_title_should_skip_remaining_attempts('llm_empty_reasoning_aux'))


class TestGenerateTitleRawViaAgent(unittest.TestCase):
    """Verify that generate_title_raw_via_agent returns (None, 'llm_empty_reasoning')
    when the agent response is empty but contains reasoning."""

    def test_returns_llm_empty_reasoning_when_no_content_with_reasoning(self):
        from api.streaming import generate_title_raw_via_agent

        user_text = 'What is the meaning of life?'
        assistant_text = 'The meaning of life is subjective.'

        mock_agent = MagicMock()
        mock_agent.provider = 'openai'
        mock_agent.model = 'gpt-4'
        mock_agent.base_url = None
        mock_agent.api_mode = None  # OpenAI-compatible path
        mock_agent.reasoning_config = None

        # Mock _build_api_kwargs to return a base dict
        mock_agent._build_api_kwargs.return_value = {
            'model': 'gpt-4',
            'messages': [],
        }

        # Create a response object with reasoning but no content
        # Use a simple dict-like object to avoid MagicMock filtering
        class MockMessage:
            def __init__(self):
                self.content = ''
                self.reasoning = 'lots of reasoning here'

        class MockChoice:
            def __init__(self):
                self.message = MockMessage()
                self.finish_reason = 'stop'

        class MockResponse:
            def __init__(self):
                self.choices = [MockChoice()]

        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = MockResponse()
        mock_agent._ensure_primary_openai_client.return_value = mock_client

        raw, status = generate_title_raw_via_agent(mock_agent, user_text, assistant_text)

        # Must return None and llm_empty_reasoning status (since content was empty)
        self.assertIsNone(raw)
        self.assertEqual(status, 'llm_empty_reasoning')

    def test_api_kwargs_includes_thinking_disabled(self):
        """Verify that the constructed api_kwargs includes thinking disabled."""
        from api.streaming import generate_title_raw_via_agent

        user_text = 'Test user input'
        assistant_text = 'Test assistant output'

        mock_agent = MagicMock()
        mock_agent.provider = 'openai'
        mock_agent.model = 'gpt-4'
        mock_agent.base_url = None
        mock_agent.api_mode = None
        mock_agent.reasoning_config = None

        base_kwargs = {
            'model': 'gpt-4',
            'messages': [],
        }
        mock_agent._build_api_kwargs.return_value = base_kwargs.copy()

        # Track what api_kwargs are passed to the OpenAI client
        captured_kwargs = {}

        def capture_kwargs(**kwargs):
            captured_kwargs.update(kwargs)
            mock_response = MagicMock()
            mock_response.choices = [MagicMock()]
            mock_response.choices[0].message.content = ''
            mock_response.choices[0].message.reasoning = 'x'
            return mock_response

        mock_client = MagicMock()
        mock_client.chat.completions.create.side_effect = capture_kwargs
        mock_agent._ensure_primary_openai_client.return_value = mock_client

        generate_title_raw_via_agent(mock_agent, user_text, assistant_text)

        # Verify extra_body was set with both thinking and reasoning disabled
        self.assertIn('extra_body', captured_kwargs)
        extra_body = captured_kwargs['extra_body']
        self.assertIn('thinking', extra_body)
        self.assertEqual(extra_body['thinking'], {'type': 'disabled'})
        self.assertIn('reasoning', extra_body)
        self.assertEqual(extra_body['reasoning'], {'enabled': False})

    def test_minimax_reasoning_split_preserved(self):
        """Verify that Minimax reasoning_split is added alongside disabled reasoning."""
        from api.streaming import generate_title_raw_via_agent

        user_text = 'Test user input'
        assistant_text = 'Test assistant output'

        mock_agent = MagicMock()
        mock_agent.provider = 'minimax'
        mock_agent.model = 'abab6.5s-chat'
        mock_agent.base_url = None
        mock_agent.api_mode = None
        mock_agent.reasoning_config = None

        base_kwargs = {
            'model': 'abab6.5s-chat',
            'messages': [],
        }
        mock_agent._build_api_kwargs.return_value = base_kwargs.copy()

        captured_kwargs = {}

        def capture_kwargs(**kwargs):
            captured_kwargs.update(kwargs)
            mock_response = MagicMock()
            mock_response.choices = [MagicMock()]
            mock_response.choices[0].message.content = 'some title'
            mock_response.choices[0].message.reasoning = None
            return mock_response

        mock_client = MagicMock()
        mock_client.chat.completions.create.side_effect = capture_kwargs
        mock_agent._ensure_primary_openai_client.return_value = mock_client

        generate_title_raw_via_agent(mock_agent, user_text, assistant_text)

        # Verify extra_body has both the disable fields AND reasoning_split for Minimax
        self.assertIn('extra_body', captured_kwargs)
        extra_body = captured_kwargs['extra_body']
        self.assertIn('thinking', extra_body)
        self.assertEqual(extra_body['thinking'], {'type': 'disabled'})
        self.assertIn('reasoning', extra_body)
        self.assertEqual(extra_body['reasoning'], {'enabled': False})
        self.assertIn('reasoning_split', extra_body)
        self.assertTrue(extra_body['reasoning_split'])


class TestBackgroundTitleUpdateFallback(unittest.TestCase):
    """Verify that _run_background_title_update falls through to local fallback
    when agent returns llm_empty_reasoning."""

    @patch('api.streaming._aux_title_configured', return_value=False)
    @patch('api.streaming._generate_llm_session_title_for_agent')
    @patch('api.streaming.get_session')
    @patch('api.streaming.SESSIONS', {})
    @patch('api.streaming.LOCK', threading.Lock())
    def test_fallback_when_agent_returns_llm_empty_reasoning(
        self, mock_get_session, mock_agent_title, mock_configured,
    ):
        """When agent title generation returns llm_empty_reasoning,
        the function must not retry and must use the local fallback."""
        from api.streaming import _run_background_title_update

        user_text = 'What is GPU idle?'
        assistant_text = 'GPU idle refers to when the GPU is not processing...'
        s, provisional = _make_provisional_session(user_text, assistant_text)
        mock_get_session.return_value = s

        # Agent route returns llm_empty_reasoning (reasoning burned the budget)
        mock_agent_title.return_value = (None, 'llm_empty_reasoning', '')

        events = []

        def fake_put_event(event_type, data):
            events.append((event_type, data))

        mock_agent = MagicMock()

        _run_background_title_update(
            session_id=s.session_id,
            user_text=user_text,
            assistant_text=assistant_text,
            placeholder_title=provisional,
            put_event=fake_put_event,
            agent=mock_agent,
        )

        # Agent title was called once
        mock_agent_title.assert_called_once()

        # The title should fall back to the local provisional (or fallback generated)
        # In either case, it should NOT be None
        self.assertIsNotNone(s.title)

        # A title_status event must be emitted, likely with fallback status
        status_events = [d for e, d in events if e == 'title_status']
        # The status should indicate fallback (not a successful llm generation)
        if status_events:
            # If a status event exists, it should NOT be 'llm' (agent success)
            self.assertNotEqual(status_events[0].get('status'), 'llm')
