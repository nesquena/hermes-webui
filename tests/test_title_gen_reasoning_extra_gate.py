"""Regression coverage for auxiliary title reasoning-suppression routing."""
from __future__ import annotations

from api.streaming import _route_accepts_reasoning_extra


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
