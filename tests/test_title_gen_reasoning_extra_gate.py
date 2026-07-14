"""Regression coverage for auxiliary title reasoning-suppression routing."""
from __future__ import annotations

import sys
import types
from unittest.mock import patch

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

    def test_auto_and_local_routes_use_their_resolved_effective_route(self):
        cases = (
            ('auto', 'qwen-title', 'qwen', 'https://dashscope.example/v1', True),
            ('local', 'deepseek-title', 'deepseek', 'https://api.deepseek.com/v1', True),
            ('auto', 'openai-title', 'openai', 'https://api.openai.com/v1', False),
            ('local', 'unknown-title', 'custom:relay', 'https://relay.example/v1', False),
        )
        for provider, model, resolved_provider, resolved_url, accepted in cases:
            captured = []

            def call_llm(**kwargs):
                captured.append(kwargs)
                return {'choices': [{'message': {'content': 'Title'}, 'finish_reason': 'stop'}]}

            with patch('api.streaming._get_aux_title_config', return_value={
                'provider': provider, 'model': model, 'base_url': '',
            }), patch('api.streaming.resolve_model_provider', return_value=(
                model, resolved_provider, resolved_url,
            )), patch('agent.auxiliary_client.call_llm', side_effect=call_llm, create=True):
                generate_title_raw_via_aux('question', 'answer')
            expected = {'reasoning': {'enabled': False}} if accepted else None
            assert captured[-1]['extra_body'] == expected

    def test_delimiter_wrapped_url_is_redacted_from_route_and_traceback(self):
        url = ')https://USER_MARKER:PASSWORD_MARKER@relay.example/v1]?api_key=KEY_MARKER&token=TOKEN_MARKER'
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
        for marker in ('USER_MARKER', 'PASSWORD_MARKER', 'KEY_MARKER', 'TOKEN_MARKER'):
            assert marker not in output
