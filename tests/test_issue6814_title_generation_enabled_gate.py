"""Regression tests for issue #6814: auxiliary.title_generation.enabled=false
must disable WebUI automatic title-generation LLM calls.

Covers:
  1. enabled=false + aux route configured -> neither aux nor agent LLM runs,
     provisional title untouched, status skipped/title_generation_disabled.
  2. enabled=false + no aux route -> active agent model is NOT used.
  3. enabled omitted / truthy -> existing default-on behavior preserved.
  4. string and boolean false forms parse identically (canonical parser).
  5. adaptive refresh path honors the flag.
  6. manual regenerate path returns an explicit disabled response.
  7. helper unit cases (default, bool, string forms).
"""

import threading
from unittest.mock import MagicMock, patch

import pytest

from tests._aux_client_helpers import auxiliary_client_modules, patch_tg_config


@pytest.fixture(autouse=True)
def _install_auxiliary_client_modules():
    with auxiliary_client_modules():
        yield


def _make_provisional_session(user_text, assistant_text='Here is the answer.'):
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
    s.session_id = 'test-6814-session'
    s.save = MagicMock()
    return s, provisional


def _run_update(session, provisional, events):
    from api.streaming import _run_background_title_update
    _run_background_title_update(
        session_id=session.session_id,
        user_text=str(session.messages[0]['content']),
        assistant_text=str(session.messages[1]['content']),
        placeholder_title=provisional,
        put_event=lambda e, d: events.append((e, d)),
        agent=None,
    )
    return events


@pytest.mark.parametrize('disabled_value', [False, 'false', '0', 'no', 'off'])
def test_disabled_skips_llm_and_keeps_provisional_title(disabled_value):
    """enabled=false (any canonical form) with an aux route configured:
    no LLM call, provisional title untouched, skipped status."""
    from api.streaming import _run_background_title_update
    user_text = 'Design a REST API for user management.'
    s, provisional = _make_provisional_session(user_text)
    events = []
    with patch('api.streaming.get_session', return_value=s), \
         patch('api.streaming.SESSIONS', {}), \
         patch('api.streaming.LOCK', threading.Lock()), \
         patch_tg_config({'provider': 'openai', 'model': 'gpt-x', 'enabled': disabled_value}), \
         patch('api.streaming._generate_llm_session_title_via_aux') as mock_aux, \
         patch('api.streaming._generate_llm_session_title_for_agent') as mock_agent:
        _run_update(s, provisional, events)

    mock_aux.assert_not_called()
    mock_agent.assert_not_called()
    assert s.title == provisional  # untouched
    s.save.assert_not_called()
    statuses = [d for e, d in events if e == 'title_status']
    assert statuses and statuses[0]['status'] == 'skipped'
    assert statuses[0]['reason'] == 'title_generation_disabled'
    # stream_end must still be emitted (finally block)
    assert any(e == 'stream_end' for e, _ in events)


def test_disabled_without_aux_route_does_not_use_agent_model():
    """enabled=false with no aux route: the active chat model must NOT be
    used as a fallback (the original bug path)."""
    from api.streaming import _run_background_title_update
    user_text = 'Explain quantum entanglement simply.'
    s, provisional = _make_provisional_session(user_text)
    events = []
    with patch('api.streaming.get_session', return_value=s), \
         patch('api.streaming.SESSIONS', {}), \
         patch('api.streaming.LOCK', threading.Lock()), \
         patch_tg_config({'enabled': False}), \
         patch('api.streaming._generate_llm_session_title_for_agent') as mock_agent:
        _run_update(s, provisional, events)

    mock_agent.assert_not_called()
    assert s.title == provisional
    statuses = [d for e, d in events if e == 'title_status']
    assert statuses[0]['status'] == 'skipped'
    assert statuses[0]['reason'] == 'title_generation_disabled'


def test_enabled_omitted_preserves_default_on_behavior():
    """enabled omitted (default true): existing behavior — LLM title runs
    and replaces the provisional title."""
    from api.streaming import _run_background_title_update
    user_text = 'Write a haiku about the moon.'
    s, provisional = _make_provisional_session(user_text)
    events = []
    with patch('api.streaming.get_session', return_value=s), \
         patch('api.streaming.SESSIONS', {}), \
         patch('api.streaming.LOCK', threading.Lock()), \
         patch_tg_config({'provider': 'openai', 'model': 'gpt-x'}), \
         patch('api.streaming._generate_llm_session_title_via_aux',
               return_value=('Moon Haiku', 'llm_aux', 'Moon Haiku')):
        _run_update(s, provisional, events)

    assert s.title == 'Moon Haiku'
    s.save.assert_called()
    assert s.llm_title_generated is True


def test_enabled_true_string_parses_as_enabled():
    from api.streaming import _aux_title_generation_enabled
    with patch_tg_config({'enabled': 'true'}):
        assert _aux_title_generation_enabled() is True
    with patch_tg_config({'enabled': 'True'}):
        assert _aux_title_generation_enabled() is True


def test_refresh_path_honors_disabled():
    """Adaptive refresh worker must skip when the flag is false."""
    from api.streaming import _run_background_title_refresh
    s = MagicMock()
    s.title = 'Existing LLM title'
    s.llm_title_generated = True
    s.messages = [{'role': 'user', 'content': 'u'}, {'role': 'assistant', 'content': 'a'}]
    s.session_id = 'test-6814-refresh'
    s.save = MagicMock()
    events = []
    with patch('api.streaming.get_session', return_value=s), \
         patch('api.streaming.SESSIONS', {}), \
         patch('api.streaming.LOCK', threading.Lock()), \
         patch_tg_config({'enabled': False}), \
         patch('api.streaming._generate_llm_session_title_via_aux') as mock_aux, \
         patch('api.streaming._generate_llm_session_title_for_agent') as mock_agent:
        _run_background_title_refresh(
            session_id=s.session_id,
            user_text='u', assistant_text='a',
            current_title=s.title,
            put_event=lambda e, d: events.append((e, d)),
            agent=None,
        )
    mock_aux.assert_not_called()
    mock_agent.assert_not_called()
    statuses = [d for e, d in events if e == 'title_status']
    assert statuses and statuses[0]['status'] == 'refresh_skipped'
    assert statuses[0]['reason'] == 'title_generation_disabled'


def test_manual_regenerate_returns_explicit_disabled():
    """Explicit manual regenerate returns an explicit disabled response
    instead of calling any title LLM."""
    from api.streaming import generate_session_title_for_session
    s = MagicMock()
    s.title = 'Whatever'
    s.llm_title_generated = False
    s.messages = [
        {'role': 'user', 'content': 'First user message here'},
        {'role': 'assistant', 'content': 'First answer here'},
    ]
    s.session_id = 'test-6814-manual'
    with patch_tg_config({'enabled': False}), \
         patch('api.streaming._generate_llm_session_title_via_aux') as mock_aux, \
         patch('api.streaming._generate_llm_session_title_for_agent') as mock_agent:
        result = generate_session_title_for_session(s)
    assert result == (None, 'title_generation_disabled', '')
    mock_aux.assert_not_called()
    mock_agent.assert_not_called()


@pytest.mark.parametrize('cfg,expected', [
    ({'provider': 'openai', 'model': 'gpt-x'}, True),          # omitted -> default on
    ({'enabled': True}, True),
    ({'enabled': False}, False),
    ({'enabled': 'true'}, True),                                # allowlist -> on
    ({'enabled': 'on'}, True),
    ({'enabled': '1'}, True),
    ({'enabled': 'yes'}, True),
    ({'enabled': 'false'}, False),
    ({'enabled': '0'}, False),
    ({'enabled': 'no'}, False),
    ({'enabled': 'off'}, False),
    ({'enabled': 'FALSE'}, False),
    # Canonical is_truthy_value(default=True): an allowlist, not a blocklist.
    ({'enabled': ''}, False),                                   # empty string -> NOT truthy -> off
    ({'enabled': 'garbage'}, False),                            # unrecognized string -> off
    ({'enabled': None}, True),                                  # explicit null -> default (on)
])
def test_helper_parses_enabled_forms(cfg, expected):
    from api.streaming import _aux_title_generation_enabled
    with patch_tg_config(cfg):
        assert _aux_title_generation_enabled() is expected
