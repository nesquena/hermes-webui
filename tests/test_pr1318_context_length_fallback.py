"""Regression test for #1318 fallback (#1344 follow-up).

PR #1318 / #1341 / a5c10d5 (in v0.50.246) persisted context_length to the
session when agent.context_compressor was present. But for fresh agents or
interrupted streams, context_compressor may be absent or report 0 — leaving
the context-ring indicator showing 0% even with the writer in place.

This follow-up adds a fallback to agent.model_metadata.get_model_context_length()
that resolves the model's static context window when the compressor didn't.

Sourced from @jasonjcwu's PR #1344, extracted into a focused follow-up.

Tests:
1. Writer block contains the fallback after the compressor block
2. Fallback gates on s.context_length being 0/falsy
3. Fallback uses agent.model + agent.base_url
4. Fallback exception is silently swallowed (older agent builds)
5. Fallback runs before s.save() so the value is persisted
"""
import importlib
import re
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

STREAMING = Path(__file__).resolve().parent.parent / "api" / "streaming.py"


def _persistence_block():
    """Return the source range covering the post-merge per-turn save block."""
    src = STREAMING.read_text(encoding="utf-8")
    start = src.find("if _reasoning_text and s.messages:")
    assert start != -1, "Reasoning trace marker not found in streaming.py"
    end = src.find("\n                s.save()", start)
    assert end != -1, "s.save() not found after the reasoning trace marker"
    # Include the s.save() line so we can verify ordering
    end = src.find("\n", end + 1)
    return src[start:end]


def _usage_payload_block():
    """Return the source range covering the live SSE usage payload fallback."""
    src = STREAMING.read_text(encoding="utf-8")
    start = src.find("usage = {")
    assert start != -1, "usage payload block not found in streaming.py"
    end = src.find("put('done'", start)
    assert end != -1, "done SSE payload not found after usage block"
    return src[start:end]


def _resolver_helper_block():
    """Return the helper that resolves context length for both fallback sites."""
    src = STREAMING.read_text(encoding="utf-8")
    start = src.find("def _resolve_context_length_fallback(")
    assert start != -1, "context-length fallback helper missing"
    end = src.find("\n\ndef ", start + 1)
    assert end != -1, "helper end marker not found"
    return src[start:end]


def _install_fake_model_metadata(monkeypatch, get_model_context_length):
    """Install a fake agent.model_metadata module for CI, where hermes-agent is absent."""
    agent_pkg = ModuleType("agent")
    agent_pkg.__path__ = []
    metadata = ModuleType("agent.model_metadata")
    metadata.get_model_context_length = get_model_context_length
    monkeypatch.setitem(sys.modules, "agent", agent_pkg)
    monkeypatch.setitem(sys.modules, "agent.model_metadata", metadata)
    return metadata


def test_fallback_uses_model_metadata():
    """Fallback helper must import and call get_model_context_length on missing compressor data."""
    helper = _resolver_helper_block()
    assert "from agent.model_metadata import get_model_context_length" in helper, (
        "Fallback must import get_model_context_length from agent.model_metadata"
    )
    assert "get_model_context_length(" in helper, (
        "Fallback must call get_model_context_length()"
    )


def test_fallback_gates_on_falsy_context_length():
    """Fallback runs only when the compressor didn't populate s.context_length.

    The gate must check s.context_length (not _cc_for_save) — if the compressor
    set context_length but it was 0, we still want the fallback to fire.
    """
    block = _persistence_block()
    # The conditional must reference s.context_length (or getattr(s, 'context_length', 0))
    assert (
        "if not getattr(s, 'context_length'" in block
        or "if not s.context_length" in block
    ), "Fallback must gate on s.context_length being falsy"


def test_fallback_passes_model_and_base_url():
    """Fallback helper must source model and base_url from the agent itself."""
    helper = _resolver_helper_block()
    assert "agent, 'model'" in helper, "Fallback must read agent.model"
    assert "agent, 'base_url'" in helper, "Fallback must read agent.base_url"


def test_fallback_exception_is_swallowed():
    """If get_model_context_length raises (older agent build, network error,
    bad provider config), the fallback must not break s.save()."""
    block = _persistence_block()
    # Must wrap the call in try/except so session save still completes.
    fallback_section = block[block.find("Fallback"):]
    assert "try:" in fallback_section, "Fallback must use try/except"
    assert "except Exception:" in fallback_section, (
        "Fallback must catch broad Exception (older agent builds may not have the helper)"
    )


def test_fallback_runs_before_save():
    """The fallback must mutate s.context_length BEFORE s.save() so the value lands on disk."""
    block = _persistence_block()
    fallback_idx = block.find("_resolve_context_length_fallback")
    save_idx = block.rfind("s.save()")
    assert fallback_idx != -1 and save_idx != -1
    assert fallback_idx < save_idx, (
        "Fallback must run BEFORE s.save() — otherwise the resolved context_length "
        "is not persisted to the session JSON."
    )


def test_fallback_assigns_context_length_when_resolved():
    """The fallback must assign s.context_length when get_model_context_length returns a non-zero value."""
    block = _persistence_block()
    fallback_section = block[block.find("Fallback"):]
    assert "_resolved_cl" in fallback_section, "Fallback must capture the result"
    assert "s.context_length = _resolved_cl" in fallback_section, (
        "Fallback must assign the resolved value to s.context_length"
    )


def test_persistence_fallback_uses_shared_full_context_resolver():
    """Session-save fallback must use the shared resolver that threads config/provider context."""
    block = _persistence_block()
    assert "_resolve_context_length_fallback(" in block
    assert "resolved_model=resolved_model" in block
    assert "resolved_provider=resolved_provider" in block
    assert "resolved_base_url=resolved_base_url" in block


def test_sse_usage_fallback_uses_shared_full_context_resolver():
    """Live SSE usage fallback must mirror the persisted-session context resolver."""
    block = _usage_payload_block()
    assert "_resolve_context_length_fallback(" in block
    assert "resolved_model=resolved_model" in block
    assert "resolved_provider=resolved_provider" in block
    assert "resolved_base_url=resolved_base_url" in block


def test_shared_resolver_threads_config_provider_and_custom_providers():
    """The resolver must pass all context that agent.model_metadata needs."""
    helper = _resolver_helper_block()
    assert "get_config()" in helper
    assert "config_context_length=" in helper
    assert "provider=" in helper
    assert "custom_providers=" in helper
    assert "getattr(agent, 'provider'" in helper


def test_shared_resolver_respects_model_context_length_override(monkeypatch):
    """Configured model.context_length must win over the 256K metadata fallback."""
    streaming = importlib.import_module("api.streaming")
    calls = []

    def fake_get_model_context_length(
        model,
        base_url="",
        api_key="",
        config_context_length=None,
        provider="",
        custom_providers=None,
    ):
        calls.append(
            {
                "model": model,
                "base_url": base_url,
                "config_context_length": config_context_length,
                "provider": provider,
                "custom_providers": custom_providers,
            }
        )
        return config_context_length or 256_000

    _install_fake_model_metadata(monkeypatch, fake_get_model_context_length)
    monkeypatch.setattr(
        streaming,
        "get_config",
        lambda: {"model": {"context_length": 1_048_576}, "custom_providers": []},
    )

    agent = SimpleNamespace(
        model="deepseek-v4-flash",
        base_url="https://example.invalid/v1",
        provider="custom:deepseek",
        context_compressor=SimpleNamespace(context_length=0),
    )

    resolved = streaming._resolve_context_length_fallback(
        agent,
        resolved_model="deepseek-v4-flash",
        resolved_provider="custom:deepseek",
        resolved_base_url="https://example.invalid/v1",
    )

    assert resolved == 1_048_576
    assert calls[-1]["config_context_length"] == 1_048_576
    assert calls[-1]["provider"] == "custom:deepseek"
    assert calls[-1]["custom_providers"] == []


def test_shared_resolver_passes_custom_provider_model_overrides(monkeypatch):
    """Named custom provider model context overrides must reach the metadata resolver."""
    streaming = importlib.import_module("api.streaming")
    custom_providers = [
        {
            "name": "DeepSeek Gateway",
            "base_url": "https://deepseek.example/v1",
            "models": {
                "deepseek-v4-flash": {"context_length": 1_048_576},
            },
        }
    ]
    calls = []

    def fake_get_model_context_length(
        model,
        base_url="",
        api_key="",
        config_context_length=None,
        provider="",
        custom_providers=None,
    ):
        calls.append(
            {
                "model": model,
                "base_url": base_url,
                "config_context_length": config_context_length,
                "provider": provider,
                "custom_providers": custom_providers,
            }
        )
        model_cfg = custom_providers[0]["models"][model]
        return int(model_cfg["context_length"])

    _install_fake_model_metadata(monkeypatch, fake_get_model_context_length)
    monkeypatch.setattr(
        streaming,
        "get_config",
        lambda: {"model": {}, "custom_providers": custom_providers},
    )

    agent = SimpleNamespace(
        model="deepseek-v4-flash",
        base_url="https://deepseek.example/v1",
        provider="custom:deepseek-gateway",
    )

    resolved = streaming._resolve_context_length_fallback(
        agent,
        resolved_model="deepseek-v4-flash",
        resolved_provider="custom:deepseek-gateway",
        resolved_base_url="https://deepseek.example/v1",
    )

    assert resolved == 1_048_576
    assert calls[-1]["base_url"] == "https://deepseek.example/v1"
    assert calls[-1]["provider"] == "custom:deepseek-gateway"
    assert calls[-1]["custom_providers"] == custom_providers


def test_shared_resolver_preserves_default_metadata_fallback(monkeypatch):
    """Without config/custom overrides, the underlying metadata fallback still decides."""
    streaming = importlib.import_module("api.streaming")
    calls = []

    def fake_get_model_context_length(
        model,
        base_url="",
        api_key="",
        config_context_length=None,
        provider="",
        custom_providers=None,
    ):
        calls.append(
            {
                "model": model,
                "base_url": base_url,
                "config_context_length": config_context_length,
                "provider": provider,
                "custom_providers": custom_providers,
            }
        )
        return 256_000

    _install_fake_model_metadata(monkeypatch, fake_get_model_context_length)
    monkeypatch.setattr(streaming, "get_config", lambda: {"model": {}})

    agent = SimpleNamespace(model="unknown-model", base_url="", provider="")

    resolved = streaming._resolve_context_length_fallback(agent)

    assert resolved == 256_000
    assert calls[-1]["config_context_length"] is None
    assert calls[-1]["custom_providers"] is None
