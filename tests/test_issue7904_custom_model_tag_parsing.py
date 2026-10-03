"""Regression test for issue #7904: @custom:<model>:<tag> on generic custom group.

_parse_provider_qualified_model_id() must correctly distinguish:
1. Generic custom provider ("custom") routing a model with colon tag (e.g. qwen3-30b-a3b:latest)
2. Named custom provider ("custom:<slug>") routing an untagged model (e.g. backup:model-a)
3. Named custom provider ("custom:<slug>") routing a tagged model (e.g. backup:model-a:free)
4. Host:port custom endpoint slugs (e.g. custom:10.8.71.41:8080:Qwen3)
"""

from unittest.mock import patch
import api.config as config
from api.config import (
    _parse_provider_qualified_model_id,
    model_with_provider_context,
)
from api.routes import _clean_session_model_provider, _split_provider_qualified_model


def test_generic_custom_colon_tagged_model_parses_correctly():
    """@custom:<model>:<tag> must parse as provider 'custom' and bare model '<model>:<tag>'."""
    assert _parse_provider_qualified_model_id("@custom:qwen3-30b-a3b:latest") == (
        "qwen3-30b-a3b:latest",
        "custom",
    )
    assert _split_provider_qualified_model("@custom:qwen3-30b-a3b:latest") == (
        "qwen3-30b-a3b:latest",
        "custom",
    )
    assert _clean_session_model_provider("@custom:qwen3-30b-a3b:latest") == "custom"


def test_generic_custom_producer_parser_roundtrip():
    """Producer model_with_provider_context and parser must roundtrip cleanly."""
    with patch.dict(config.cfg, {"model": {"provider": "deepseek"}}, clear=True):
        routed = model_with_provider_context("qwen3-30b-a3b:latest", "custom")
        assert routed == "@custom:qwen3-30b-a3b:latest"
        parsed_model, parsed_provider = _parse_provider_qualified_model_id(routed)
        assert parsed_model == "qwen3-30b-a3b:latest"
        assert parsed_provider == "custom"


def test_generic_custom_various_model_tag_shapes():
    """Verify other model architectures with parameter sizes, quants, and versions."""
    cases = [
        ("@custom:qwen3-30b-a3b:latest", ("qwen3-30b-a3b:latest", "custom")),
        ("@custom:deepseek-r1:1.5b", ("deepseek-r1:1.5b", "custom")),
        ("@custom:deepseek-r1:70b", ("deepseek-r1:70b", "custom")),
        ("@custom:llama3.2:1b", ("llama3.2:1b", "custom")),
        ("@custom:mistral-0.3:7b-instruct", ("mistral-0.3:7b-instruct", "custom")),
        ("@custom:phi-4:mini", ("phi-4:mini", "custom")),
        ("@custom:qwen2.5-coder:32b-instruct", ("qwen2.5-coder:32b-instruct", "custom")),
        ("@custom:my-model:q4_k_m", ("my-model:q4_k_m", "custom")),
        ("@custom:local-model:free", ("local-model:free", "custom")),
    ]
    for raw, (expected_model, expected_provider) in cases:
        assert _parse_provider_qualified_model_id(raw) == (expected_model, expected_provider)
        assert _split_provider_qualified_model(raw) == (expected_model, expected_provider)


def test_configured_named_custom_provider_retains_slug():
    """When a custom provider is configured in custom_providers, its slug is retained."""
    cfg_mock = {
        "custom_providers": [
            {"name": "mybox", "base_url": "http://127.0.0.1:8000/v1"},
        ]
    }
    with patch.dict(config.cfg, cfg_mock, clear=True):
        # Untagged model on configured custom provider
        assert _parse_provider_qualified_model_id("@custom:mybox:my-model") == (
            "my-model",
            "custom:mybox",
        )
        # Tagged model on configured custom provider
        assert _parse_provider_qualified_model_id("@custom:mybox:my-model:latest") == (
            "my-model:latest",
            "custom:mybox",
        )


def test_endpoint_like_slugs_retained():
    """Host:port and IP custom slugs are retained as providers."""
    assert _parse_provider_qualified_model_id("@custom:10.8.71.41:8080:Qwen3") == (
        "Qwen3",
        "custom:10.8.71.41:8080",
    )
    assert _parse_provider_qualified_model_id("@custom:localhost:11434:Qwen3") == (
        "Qwen3",
        "custom:localhost:11434",
    )
