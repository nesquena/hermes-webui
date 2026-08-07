"""Regression coverage for auxiliary title reasoning-suppression routing."""
from __future__ import annotations

import importlib
import importlib.util
import sys
from unittest.mock import patch

import pytest

_MISSING = object()
_AGENT_BEFORE_COLLECTION = sys.modules.get("agent", _MISSING)
_AGENT_SPEC_BEFORE_COLLECTION = getattr(_AGENT_BEFORE_COLLECTION, "__spec__", _MISSING)
_AGENT_PATH_BEFORE_COLLECTION = getattr(_AGENT_BEFORE_COLLECTION, "__path__", _MISSING)
try:
    _AGENT_IMPORT_SPEC_BEFORE_COLLECTION = importlib.util.find_spec("agent")
except (ImportError, ValueError):
    _AGENT_IMPORT_SPEC_BEFORE_COLLECTION = None

from api.streaming import _route_accepts_reasoning_extra, generate_title_raw_via_aux

_AGENT_AFTER_COLLECTION = sys.modules.get("agent", _MISSING)


def test_collection_preserves_the_real_agent_package():
    """Importing this regression module must not poison later Agent imports."""
    if _AGENT_BEFORE_COLLECTION is _MISSING:
        agent_module = _AGENT_AFTER_COLLECTION
        if agent_module is _MISSING:
            if _AGENT_IMPORT_SPEC_BEFORE_COLLECTION is None:
                pytest.skip("Hermes Agent is not installed in this WebUI-only environment")
            agent_module = importlib.import_module("agent")

        agent_spec = getattr(agent_module, "__spec__", None)
        agent_path = getattr(agent_module, "__path__", _MISSING)
        assert agent_spec is not None
        assert agent_path is not _MISSING
        if _AGENT_IMPORT_SPEC_BEFORE_COLLECTION is not None:
            assert agent_spec.origin == _AGENT_IMPORT_SPEC_BEFORE_COLLECTION.origin
            assert tuple(agent_path) == tuple(
                _AGENT_IMPORT_SPEC_BEFORE_COLLECTION.submodule_search_locations or ()
            )
        assert importlib.import_module("agent.model_metadata") is not None
        return

    assert _AGENT_AFTER_COLLECTION is _AGENT_BEFORE_COLLECTION
    assert getattr(_AGENT_AFTER_COLLECTION, "__spec__", _MISSING) is _AGENT_SPEC_BEFORE_COLLECTION
    assert getattr(_AGENT_AFTER_COLLECTION, "__path__", _MISSING) is _AGENT_PATH_BEFORE_COLLECTION
    assert _AGENT_SPEC_BEFORE_COLLECTION is not None
    assert _AGENT_PATH_BEFORE_COLLECTION is not _MISSING
    assert importlib.import_module("agent.model_metadata") is not None


@pytest.fixture
def _scoped_auxiliary_client_module(monkeypatch):
    """Provide a call boundary per test without mutating collection state."""
    try:
        auxiliary_client = importlib.import_module("agent.auxiliary_client")
    except Exception:
        agent_module = sys.modules.get("agent")
        if agent_module is None:
            agent_spec = importlib.util.spec_from_loader(
                "agent", loader=None, is_package=True
            )
            agent_module = importlib.util.module_from_spec(agent_spec)
            monkeypatch.setitem(sys.modules, "agent", agent_module)

        auxiliary_spec = importlib.util.spec_from_loader(
            "agent.auxiliary_client", loader=None
        )
        auxiliary_client = importlib.util.module_from_spec(auxiliary_spec)
        monkeypatch.setitem(
            sys.modules, "agent.auxiliary_client", auxiliary_client
        )
        monkeypatch.setattr(
            agent_module, "auxiliary_client", auxiliary_client, raising=False
        )

    yield auxiliary_client


@pytest.mark.usefixtures("_scoped_auxiliary_client_module")
class TestAuxReasoningExtraRouteContract:
    def test_known_reasoning_routes_keep_suppression(self):
        assert _route_accepts_reasoning_extra(
            'openrouter', 'deepseek/deepseek-r1', 'https://openrouter.ai/api/v1'
        ) is True
        assert _route_accepts_reasoning_extra('lmstudio', 'qwen3-8b', 'http://localhost:1234/v1') is True
        assert _route_accepts_reasoning_extra('', 'minimax-m2', 'https://api.minimaxi.com/v1') is True
        assert _route_accepts_reasoning_extra('', 'MiniMax-M3', 'https://api.minimax.io/v1') is True
        assert _route_accepts_reasoning_extra('', 'MiniMax-M3', 'https://edge.api.minimax.io/v1') is True

    def test_builtin_routes_are_resolved_when_url_is_implicit(self):
        assert _route_accepts_reasoning_extra('deepseek', 'deepseek-reasoner', '') is True
        assert _route_accepts_reasoning_extra('anthropic', 'claude-sonnet-4-6', '') is True
        assert _route_accepts_reasoning_extra('lmstudio', 'qwen3-8b', '') is True
        assert _route_accepts_reasoning_extra('google-gemini', 'gemini-2.5-pro', '') is True
        assert _route_accepts_reasoning_extra('x-ai', 'grok-4', '') is True
        assert _route_accepts_reasoning_extra('ollama', 'qwen3', '') is True

    @pytest.mark.parametrize('provider', (
        'ai-gateway',
        'vercel',
        'vercel-ai-gateway',
        'ai_gateway',
        'aigateway',
    ))
    def test_ai_gateway_request_keeps_reasoning_suppression(self, provider):
        captured = []

        def call_llm(**kwargs):
            captured.append(kwargs)
            return {'choices': [{'message': {'content': 'Title'}, 'finish_reason': 'stop'}]}

        with patch('api.streaming._get_aux_title_config', return_value={
            'provider': provider,
            'model': 'google/gemini-3-flash',
            'base_url': '',
        }), patch('agent.auxiliary_client.call_llm', side_effect=call_llm, create=True):
            generate_title_raw_via_aux('question', 'answer')

        request = captured[-1]
        assert (request['provider'], request['model'], request['base_url']) == (
            provider,
            'google/gemini-3-flash',
            None,
        )
        assert request['extra_body'] == {'reasoning': {'enabled': False}}

    @pytest.mark.parametrize(
        ("provider", "model", "base_url"),
        (
            ("openai", "gpt-5", "https://api.openai.com/v1"),
            ("openai-codex", "gpt-5", ""),
            ("azure", "gpt-4", ""),
            ("azure-foundry", "gpt-4", ""),
            ("azure-ai-foundry", "gpt-4", ""),
            ("azure-ai", "gpt-4", ""),
            ("azure/deployment", "gpt-4", ""),
            ("azure-deployment", "gpt-4", ""),
            ("custom", "gpt-5", "https://x.openai.azure.com/v1"),
            ("custom", "gpt-5", "https://x.services.ai.azure.com/v1"),
            ("custom", "gpt-5", "https://x.cognitiveservices.azure.com/v1"),
            (
                "openrouter",
                "anthropic/claude-sonnet-4.6",
                "https://openrouter.ai/api/v1",
            ),
            (
                "openrouter",
                "anthropic/claude-opus-4.8",
                "https://openrouter.ai/api/v1",
            ),
        ),
    )
    def test_known_reject_routes_omit_suppression(
        self, provider, model, base_url
    ):
        assert _route_accepts_reasoning_extra(provider, model, base_url) is False

    def test_unknown_custom_route_omits_suppression(self):
        assert _route_accepts_reasoning_extra('custom:relay', 'reasoning-model', 'https://relay.example.test/v1') is False

    def test_hostname_matching_does_not_use_path_substrings(self):
        assert _route_accepts_reasoning_extra(
            'deepseek', 'deepseek-r1', 'https://proxy.example.test/api.openai.com/v1'
        ) is True

    def test_missing_route_fields_are_not_treated_as_resolved(self):
        assert _route_accepts_reasoning_extra('', '', '') is False
        assert _route_accepts_reasoning_extra('custom:relay', '', 'https://relay.example.test/v1') is False
        assert _route_accepts_reasoning_extra('', 'reasoning-model', '') is False

    def test_aux_route_matrix_uses_one_resolved_route_for_request_and_gate(self):
        cases = (
            # auxiliary_provider, auxiliary_model, auxiliary_url,
            # default_provider, default_model, default_url, request route, extra_body
            ('auto', '', '', 'qwen', 'qwen3-title', '',
             ('qwen', 'qwen3-title', None), {'reasoning': {'enabled': False}}),
            ('local', '', '', 'deepseek', 'deepseek-reasoner', '',
             ('deepseek', 'deepseek-reasoner', None), {'reasoning': {'enabled': False}}),
            ('deepseek', '', '', 'deepseek', 'deepseek-reasoner', '',
             ('deepseek', 'deepseek-v4-flash', None), {'reasoning': {'enabled': False}}),
            ('auto', '', '', 'openai', 'gpt-5', '',
             ('openai', 'gpt-5', None), None),
            ('local', '', '', 'custom', 'title-model', 'https://relay.example/v1',
             ('custom', 'title-model', 'https://relay.example/v1'), None),
            ('auto', '@openrouter:deepseek/deepseek-r1:free', '', 'openai', 'gpt-5', '',
             ('openrouter', 'deepseek/deepseek-r1:free', None), {'reasoning': {'enabled': False}}),
            ('auto', '@custom:relay:vendor/model:thinking', 'https://relay.example/v1', 'openai', 'gpt-5', '',
             ('custom:relay', 'vendor/model:thinking', 'https://relay.example/v1'), None),
            ('auto', '', '', 'minimax', 'MiniMax-M2.5', 'https://api.minimaxi.com/v1',
             ('minimax', 'MiniMax-M2.5', 'https://api.minimaxi.com/v1'),
             {'reasoning': {'enabled': False}, 'reasoning_split': True}),
            # Explicit routes must not inherit a differing main route's model
            # or endpoint, including namespaced OpenRouter identifiers.
            ('deepseek', 'deepseek-reasoner', 'https://api.deepseek.com/v1', 'openai', 'gpt-5.5', 'https://api.openai.com/v1',
             ('deepseek', 'deepseek-reasoner', 'https://api.deepseek.com/v1'),
             {'reasoning': {'enabled': False}}),
            ('openrouter', 'deepseek/deepseek-r1:free', 'https://openrouter.ai/api/v1', 'openai', 'gpt-5.5', 'https://api.openai.com/v1',
             ('openrouter', 'deepseek/deepseek-r1:free', 'https://openrouter.ai/api/v1'),
             {'reasoning': {'enabled': False}}),
            ('openrouter', 'anthropic/claude-sonnet-4.6:thinking', 'https://openrouter.ai/api/v1', 'openai', 'gpt-5.5', 'https://api.openai.com/v1',
             ('openrouter', 'anthropic/claude-sonnet-4.6:thinking', 'https://openrouter.ai/api/v1'), None),
            # An explicit relay/model contract with a blank provider is
            # Agent-custom, not a request to borrow the differing main route.
            ('', 'gemma-4-31b-it', 'https://relay.example.test/v1', 'openai', 'gpt-main', 'https://api.openai.com/v1',
             (None, 'gemma-4-31b-it', 'https://relay.example.test/v1'), None),
        )
        for (
            provider, model, base_url, default_provider, default_model, default_url,
            expected_route, expected_extra,
        ) in cases:
            captured = []

            def call_llm(*, _captured=captured, **kwargs):
                _captured.append(kwargs)
                return {'choices': [{'message': {'content': 'Title'}, 'finish_reason': 'stop'}]}

            with patch('api.streaming._get_aux_title_config', return_value={
                'provider': provider, 'model': model, 'base_url': base_url,
            }), patch('api.config.cfg', {
                'model': {
                    'provider': default_provider,
                    'default': default_model,
                    'base_url': default_url,
                },
            }), patch(
                'agent.auxiliary_client._get_aux_model_for_provider',
                side_effect=lambda provider: {'deepseek': 'deepseek-v4-flash'}.get(provider, ''),
                create=True,
            ), patch('agent.auxiliary_client.call_llm', side_effect=call_llm, create=True):
                generate_title_raw_via_aux('question', 'answer')
            request = captured[-1]
            assert (request['provider'], request['model'], request['base_url']) == expected_route
            assert request['extra_body'] == expected_extra

    def test_explicit_blank_model_uses_its_own_default_not_the_main_route(self):
        captured = []

        def call_llm(**kwargs):
            captured.append(kwargs)
            return {'choices': [{'message': {'content': 'Title'}, 'finish_reason': 'stop'}]}

        with patch('api.streaming._get_aux_title_config', return_value={
            'provider': 'deepseek', 'model': '', 'base_url': '',
        }), patch('api.config.cfg', {
            'model': {'provider': 'openai', 'default': 'gpt-5.5', 'base_url': 'https://api.openai.com/v1'},
            'providers': {'deepseek': {'models': ['deepseek-chat']}},
        }), patch(
            'agent.auxiliary_client._get_aux_model_for_provider',
            return_value='deepseek-v4-flash',
            create=True,
        ) as aux_default, patch(
            'agent.auxiliary_client.call_llm', side_effect=call_llm, create=True,
        ):
            generate_title_raw_via_aux('question', 'answer')

        request = captured[-1]
        aux_default.assert_called_once_with('deepseek')
        assert request['provider'] == 'deepseek'
        assert request['model'] == 'deepseek-v4-flash'
        assert request['base_url'] is None
        assert request['extra_body'] == {'reasoning': {'enabled': False}}

    def test_explicit_blank_model_with_custom_url_uses_its_own_default(self):
        """A custom endpoint must not bypass an explicit provider's default."""
        captured = []

        def call_llm(**kwargs):
            captured.append(kwargs)
            return {'choices': [{'message': {'content': 'Title'}, 'finish_reason': 'stop'}]}

        with patch('api.streaming._get_aux_title_config', return_value={
            'provider': 'deepseek', 'model': '', 'base_url': 'https://deepseek.example.test/v1',
        }), patch('api.config.cfg', {
            'model': {'provider': 'openai', 'default': 'gpt-5.5', 'base_url': 'https://api.openai.com/v1'},
        }), patch(
            'agent.auxiliary_client._get_aux_model_for_provider',
            return_value='deepseek-chat',
            create=True,
        ) as aux_default, patch(
            'agent.auxiliary_client.call_llm', side_effect=call_llm, create=True,
        ):
            generate_title_raw_via_aux('question', 'answer')

        request = captured[-1]
        aux_default.assert_called_once_with('deepseek')
        assert (request['provider'], request['model'], request['base_url']) == (
            'deepseek', 'deepseek-chat', 'https://deepseek.example.test/v1',
        )
        assert request['extra_body'] == {'reasoning': {'enabled': False}}

    def test_explicit_blank_model_without_a_provider_default_fails_closed(self):
        captured = []

        def call_llm(**kwargs):
            captured.append(kwargs)
            return {'choices': [{'message': {'content': 'Title'}, 'finish_reason': 'stop'}]}

        with patch('api.streaming._get_aux_title_config', return_value={
            'provider': 'custom:unconfigured', 'model': '', 'base_url': 'https://relay.example/v1',
        }), patch('api.config.cfg', {
            'model': {'provider': 'openai', 'default': 'gpt-5.5', 'base_url': 'https://api.openai.com/v1'},
        }), patch(
            'agent.auxiliary_client._get_aux_model_for_provider',
            return_value='',
            create=True,
        ), patch('agent.auxiliary_client.call_llm', side_effect=call_llm, create=True):
            generate_title_raw_via_aux('question', 'answer')

        request = captured[-1]
        assert (request['provider'], request['model'], request['base_url']) == (
            'custom:unconfigured', None, 'https://relay.example/v1',
        )
        assert request['extra_body'] is None

    def test_unresolved_minimax_endpoint_omits_every_reasoning_extra(self):
        """An unresolved custom route cannot inherit MiniMax-specific extras."""
        captured = []

        def call_llm(**kwargs):
            captured.append(kwargs)
            return {'choices': [{'message': {'content': 'Title'}, 'finish_reason': 'stop'}]}

        with patch('api.streaming._get_aux_title_config', return_value={
            'provider': 'custom:unconfigured',
            'model': '',
            'base_url': 'https://api.minimaxi.com/v1',
        }), patch(
            'agent.auxiliary_client._get_aux_model_for_provider',
            return_value='',
            create=True,
        ), patch('agent.auxiliary_client.call_llm', side_effect=call_llm, create=True):
            generate_title_raw_via_aux('question', 'answer')

        request = captured[-1]
        assert (request['provider'], request['model'], request['base_url']) == (
            'custom:unconfigured', None, 'https://api.minimaxi.com/v1',
        )
        assert request['extra_body'] is None

    def test_blank_provider_minimax_global_endpoint_preserves_route_and_extras(self):
        """The canonical global MiniMax endpoint must not inherit the main route."""
        captured = []

        def call_llm(**kwargs):
            captured.append(kwargs)
            return {'choices': [{'message': {'content': 'Title'}, 'finish_reason': 'stop'}]}

        with patch('api.streaming._get_aux_title_config', return_value={
            'provider': '',
            'model': 'MiniMax-M3',
            'base_url': 'https://api.minimax.io/v1',
        }), patch('api.config.cfg', {
            'model': {
                'provider': 'openai',
                'default': 'gpt-main',
                'base_url': 'https://api.openai.com/v1',
            },
        }), patch('agent.auxiliary_client.call_llm', side_effect=call_llm, create=True):
            generate_title_raw_via_aux('question', 'answer')

        request = captured[-1]
        assert (request['provider'], request['model'], request['base_url']) == (
            None, 'MiniMax-M3', 'https://api.minimax.io/v1',
        )
        assert request['extra_body'] == {
            'reasoning': {'enabled': False},
            'reasoning_split': True,
        }

    @pytest.mark.parametrize(
        ('main_provider', 'main_model', 'main_base_url'),
        (
            ('openai', 'gpt-main', 'https://api.openai.com/v1'),
            ('anthropic', 'claude-main', 'https://api.anthropic.com'),
        ),
    )
    def test_explicit_relay_route_never_uses_the_differing_main_resolver(
        self, main_provider, main_model, main_base_url,
    ):
        """Production resolver regression: relay config is its own route."""
        captured = []

        def call_llm(**kwargs):
            captured.append(kwargs)
            return {'choices': [{'message': {'content': 'Title'}, 'finish_reason': 'stop'}]}

        with patch('api.streaming._get_aux_title_config', return_value={
            'provider': '',
            'model': 'gemma-4-31b-it',
            'base_url': 'https://relay.example.test/v1',
        }), patch('api.config.cfg', {
            'model': {
                'provider': main_provider,
                'default': main_model,
                'base_url': main_base_url,
            },
        }), patch('agent.auxiliary_client.call_llm', side_effect=call_llm, create=True):
            generate_title_raw_via_aux('question', 'answer')

        request = captured[-1]
        assert (request['provider'], request['model'], request['base_url']) == (
            None, 'gemma-4-31b-it', 'https://relay.example.test/v1',
        )
        assert request['extra_body'] is None

    def test_base_url_only_route_is_custom_not_the_anthropic_main_route(self):
        """A URL alone is an auxiliary custom-route contract."""
        captured = []

        def call_llm(**kwargs):
            captured.append(kwargs)
            return {'choices': [{'message': {'content': 'Title'}, 'finish_reason': 'stop'}]}

        with patch('api.streaming._get_aux_title_config', return_value={
            'provider': '', 'model': '', 'base_url': 'https://relay.example.test/v1',
        }), patch('api.config.cfg', {
            'model': {
                'provider': 'anthropic',
                'default': 'claude-main',
                'base_url': 'https://api.anthropic.com',
            },
        }), patch('agent.auxiliary_client.call_llm', side_effect=call_llm, create=True):
            generate_title_raw_via_aux('question', 'answer')

        request = captured[-1]
        assert (request['provider'], request['model'], request['base_url']) == (
            'custom', None, 'https://relay.example.test/v1',
        )
        assert request['extra_body'] is None

    def test_legacy_local_base_url_route_is_custom_not_the_codex_main_route(self):
        """The legacy local spelling must use the custom auxiliary client path."""
        captured = []

        def call_llm(**kwargs):
            captured.append(kwargs)
            return {'choices': [{'message': {'content': 'Title'}, 'finish_reason': 'stop'}]}

        with patch('api.streaming._get_aux_title_config', return_value={
            'provider': 'local', 'model': '', 'base_url': 'https://relay.example.test/v1',
        }), patch('api.config.cfg', {
            'model': {
                'provider': 'openai-codex',
                'default': 'gpt-main',
                'base_url': 'https://chatgpt.com/backend-api/codex',
            },
        }), patch('agent.auxiliary_client.call_llm', side_effect=call_llm, create=True):
            generate_title_raw_via_aux('question', 'answer')

        request = captured[-1]
        assert (request['provider'], request['model'], request['base_url']) == (
            'custom', None, 'https://relay.example.test/v1',
        )
        assert request['extra_body'] is None

    @pytest.mark.parametrize('model', (
        '@openai:gpt-5.5',
        '@openrouter:anthropic/claude-sonnet-4.6',
    ))
    def test_auto_provider_qualified_reject_routes_omit_suppression(self, model):
        captured = []

        def call_llm(**kwargs):
            captured.append(kwargs)
            return {'choices': [{'message': {'content': 'Title'}, 'finish_reason': 'stop'}]}

        with patch('api.streaming._get_aux_title_config', return_value={
            'provider': 'auto', 'model': model, 'base_url': '',
        }), patch('agent.auxiliary_client.call_llm', side_effect=call_llm, create=True):
            generate_title_raw_via_aux('question', 'answer')

        assert captured[-1]['extra_body'] is None

    @pytest.mark.parametrize('url', (
        '(https://USER MARKER:PASSWORD MARKER@relay.example/v1)?api_key=(KEY MARKER)&token=[TOKEN MARKER]',
        '["https://USER [MARKER] : PASSWORD (MARKER) @relay.example/v1"?token="TOKEN MARKER"&key=\'KEY MARKER\']',
    ))
    def test_delimiter_wrapped_url_is_redacted_from_route_and_traceback(self, url):
        logged = []

        def fail(**_kwargs):
            raise RuntimeError(f'provider failed at {url}')

        with patch('api.streaming._get_aux_title_config', return_value={
            'provider': 'custom:relay', 'model': 'title-model', 'base_url': url,
        }), patch('agent.auxiliary_client.call_llm', side_effect=fail, create=True), patch(
            'api.streaming.logger.error', side_effect=lambda *args: logged.append(args),
        ):
            generate_title_raw_via_aux('question', 'answer')

        output = '\n'.join(' '.join(map(str, args)) for args in logged)
        for marker in ('USER', 'PASSWORD', 'KEY', 'TOKEN'):
            assert marker not in output
