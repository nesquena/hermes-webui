"""Regression coverage for auxiliary title reasoning-suppression routing."""
from __future__ import annotations

import sys
import types
from unittest.mock import patch

import pytest

_agent_stub = types.ModuleType('agent')
_aux_stub = types.ModuleType('agent.auxiliary_client')
sys.modules.setdefault('agent', _agent_stub)
sys.modules.setdefault('agent.auxiliary_client', _aux_stub)
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
             ('deepseek', 'deepseek-reasoner', None), {'reasoning': {'enabled': False}}),
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
            }), patch('agent.auxiliary_client.call_llm', side_effect=call_llm, create=True):
                generate_title_raw_via_aux('question', 'answer')
            request = captured[-1]
            assert (request['provider'], request['model'], request['base_url']) == expected_route
            assert request['extra_body'] == expected_extra

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
