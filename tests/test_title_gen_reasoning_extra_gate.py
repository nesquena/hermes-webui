"""Consolidated regression tests for title-gen reasoning extra_body gating.

Consolidates #2083 (suppress thinking on reasoning models so titles aren't
polluted) with #4161 (OpenAI/Azure Chat Completions reject the `reasoning`
extra_body param with a 400 -> silent fallback to heuristic titles).

Aux path (`generate_title_raw_via_aux`, no agent object): inject the
reasoning-disable EXCEPT on reject-listed routes (OpenAI/Azure).
Agent path (`generate_title_raw_via_agent`): inject only when the agent's
canonical `_supports_reasoning_extra_body()` says the route is reasoning-
tolerant AND the route is not reject-listed — which additionally excludes
OpenRouter Anthropic mandatory-reasoning models (Claude Sonnet 4.6 / Opus 4.8)
that are reasoning-capable but 400 on a disable.
"""
from __future__ import annotations

from api.streaming import _route_rejects_reasoning_extra


class TestAuxRejectList:
    def test_openai_direct_is_reject_listed(self):
        assert _route_rejects_reasoning_extra("openai", "gpt-5.5", "https://api.openai.com/v1") is True

    def test_azure_is_reject_listed(self):
        assert _route_rejects_reasoning_extra("azure", "gpt-4", "https://x.openai.azure.com/") is True
        assert _route_rejects_reasoning_extra("azure/foo", "gpt-4", "") is True

    def test_azure_foundry_aliases_reject_listed(self):
        assert _route_rejects_reasoning_extra("azure-foundry", "gpt-5", "") is True
        assert _route_rejects_reasoning_extra("azure-ai-foundry", "gpt-5", "") is True
        assert _route_rejects_reasoning_extra("azure-ai", "gpt-5", "") is True
        # Foundry host-based detection (services.ai.azure.com / cognitiveservices)
        assert _route_rejects_reasoning_extra("custom", "gpt-5", "https://x.services.ai.azure.com/v1") is True

    def test_hostname_match_not_substring(self):
        # A proxy whose PATH merely contains api.openai.com must NOT be reject-listed.
        assert _route_rejects_reasoning_extra("custom", "qwen3", "https://proxy.example.test/api.openai.com/v1") is False
        # but the real OpenAI host is.
        assert _route_rejects_reasoning_extra("custom", "gpt-5", "https://api.openai.com/v1") is True

    def test_openai_codex_alias_is_reject_listed(self):
        assert _route_rejects_reasoning_extra("openai-codex", "gpt-5", "") is True

    def test_openrouter_non_anthropic_is_not_reject_listed(self):
        assert _route_rejects_reasoning_extra("openrouter", "deepseek/deepseek-r1", "https://openrouter.ai/api/v1") is False

    def test_openrouter_anthropic_mandatory_is_reject_listed(self):
        # Promoted into the shared helper so BOTH aux and agent paths skip it.
        assert _route_rejects_reasoning_extra("openrouter", "anthropic/claude-sonnet-4.6", "https://openrouter.ai/api/v1") is True
        assert _route_rejects_reasoning_extra("openrouter", "anthropic/claude-opus-4.8", "https://openrouter.ai/api/v1") is True

    def test_local_lmstudio_is_not_reject_listed(self):
        assert _route_rejects_reasoning_extra("lmstudio", "qwen3-8b", "http://localhost:1234/v1") is False

    def test_minimax_is_not_reject_listed(self):
        assert _route_rejects_reasoning_extra("", "minimax-m2", "https://api.minimaxi.com/v1") is False
