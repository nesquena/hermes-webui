"""Regression coverage for auxiliary title reasoning-suppression routing."""
from __future__ import annotations

import sys
import types
from unittest.mock import patch

import pytest

_agent_stub = types.ModuleType('agent')
_aux_stub = types.ModuleType('agent.auxiliary_client')
sys.modules['agent'] = _agent_stub
sys.modules['agent.auxiliary_client'] = _aux_stub
_agent_stub.auxiliary_client = _aux_stub

from api.streaming import _route_accepts_reasoning_extra, generate_title_raw_via_aux


class TestAuxReasoningExtraRouteContract:
    def test_known_reasoning_routes_keep_suppression(self):
        assert _route_accepts_reasoning_extra(
            'openrouter', 'deepseek/deepseek-r1', 'https://openrouter.ai/api/v1'
        ) is True
        assert _route_accepts_reasoning_extra('lmstudio', 'qwen3-8b', 'http://localhost:1234/v1') is True
        assert _route_accepts_reasoning_extra('', 'minimax-m2', 'https://api.minimaxi.com/v1') is True

    def test_builtin_routes_are_resolved_when_url_is_implicit(self):
        assert _route_accepts_reasoning_extra('deepseek', 'deepseek-reasoner', '') is True
        assert _route_accepts_reasoning_extra('anthropic', 'claude-sonnet-4-6', '') is True
        assert _route_accepts_reasoning_extra('lmstudio', 'qwen3-8b', '') is True
        assert _route_accepts_reasoning_extra('google-gemini', 'gemini-2.5-pro', '') is True
        assert _route_accepts_reasoning_extra('x-ai', 'grok-4', '') is True
        assert _route_accepts_reasoning_extra('ollama', 'qwen3', '') is True

    def test_known_reject_routes_omit_suppression(self):
        assert _route_accepts_reasoning_extra('openai', 'gpt-5', 'https://api.openai.com/v1') is False
        assert _route_accepts_reasoning_extra('azure', 'gpt-4', '') is False
        assert _route_accepts_reasoning_extra('custom', 'gpt-5', 'https://x.services.ai.azure.com/v1') is False
        assert _route_accepts_reasoning_extra(
            'openrouter', 'anthropic/claude-sonnet-4.6', 'https://openrouter.ai/api/v1'
        ) is False

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
