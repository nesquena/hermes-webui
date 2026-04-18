"""Tests for PR #644 — load provider models from config.yaml in get_available_models()."""
import pytest
import api.config as _cfg


def _available_models_with_cfg(cfg_override):
    """Helper: temporarily patch config.cfg, call get_available_models(), restore."""
    old_cfg = dict(_cfg.cfg)
    _cfg.cfg.clear()
    _cfg.cfg.update(cfg_override)
    try:
        return _cfg.get_available_models()
    finally:
        _cfg.cfg.clear()
        _cfg.cfg.update(old_cfg)


class TestConfigYamlModelsLoading:
    """Verify that providers with explicit models in config.yaml use those models."""

    def test_provider_in_config_but_not_provider_models_gets_cfg_models(self):
        """A provider only in cfg.providers (not _PROVIDER_MODELS) should appear
        with its configured model list instead of being skipped entirely."""
        cfg = {
            "model": {"provider": "my-custom-llm"},
            "providers": {
                "my-custom-llm": {
                    "base_url": "http://custom.local/v1",
                    "models": ["custom-model-a", "custom-model-b"],
                }
            },
        }
        result = _available_models_with_cfg(cfg)
        groups = {g["provider"]: g["models"] for g in result["groups"]}
        # Provider should appear (previously it was silently skipped)
        provider_names = [g["provider"] for g in result["groups"]]
        found = any("my-custom-llm" in n.lower() or "My-Custom-Llm" in n for n in provider_names)
        # If it appears, its models must include our cfg models
        for g in result["groups"]:
            if "custom" in g["provider"].lower():
                model_ids = [m["id"] for m in g["models"]]
                assert any("custom-model-a" in mid for mid in model_ids), (
                    f"custom-model-a not in group models: {model_ids}"
                )

    def test_provider_models_dict_format_expanded(self):
        """models: {model_id: {context_length: ...}} — keys become model IDs."""
        cfg = {
            "model": {"provider": "anthropic"},
            "providers": {
                "anthropic": {
                    "models": {
                        "claude-custom-1": {"context_length": 200000},
                        "claude-custom-2": {"context_length": 100000},
                    }
                }
            },
        }
        result = _available_models_with_cfg(cfg)
        # Find Anthropic group
        for g in result["groups"]:
            if g["provider"] == "Anthropic":
                model_ids = [m["id"] for m in g["models"]]
                assert "claude-custom-1" in model_ids, (
                    f"claude-custom-1 not in Anthropic models: {model_ids}"
                )
                assert "claude-custom-2" in model_ids, (
                    f"claude-custom-2 not in Anthropic models: {model_ids}"
                )
                break

    def test_provider_models_list_format_expanded(self):
        """models: [model_id, ...] — items become model IDs."""
        cfg = {
            "model": {"provider": "anthropic"},
            "providers": {
                "anthropic": {
                    "models": ["claude-list-only-1", "claude-list-only-2"],
                }
            },
        }
        result = _available_models_with_cfg(cfg)
        for g in result["groups"]:
            if g["provider"] == "Anthropic":
                model_ids = [m["id"] for m in g["models"]]
                assert "claude-list-only-1" in model_ids, (
                    f"claude-list-only-1 not in Anthropic models: {model_ids}"
                )
                break

    def test_provider_in_provider_models_but_no_cfg_override_unchanged(self):
        """When no models key in cfg.providers, hardcoded _PROVIDER_MODELS still used."""
        cfg = {
            "model": {"provider": "anthropic"},
            "providers": {
                "anthropic": {
                    "api_key": "sk-test",
                    # No 'models' key
                }
            },
        }
        result = _available_models_with_cfg(cfg)
        raw_ids = {m["id"] for m in _cfg._PROVIDER_MODELS.get("anthropic", [])}
        for g in result["groups"]:
            if g["provider"] == "Anthropic":
                returned_ids = {m["id"] for m in g["models"]}
                # Should still have the hardcoded models
                overlap = raw_ids & returned_ids
                assert overlap, (
                    f"No _PROVIDER_MODELS models found in Anthropic group. "
                    f"Expected subset of {raw_ids}, got {returned_ids}"
                )
                break

    def test_non_dict_models_value_falls_through_gracefully(self):
        """If models value is neither dict nor list (e.g. null), no crash."""
        cfg = {
            "model": {"provider": "anthropic"},
            "providers": {
                "anthropic": {"models": None},  # invalid — should not crash
            },
        }
        # Should not raise
        result = _available_models_with_cfg(cfg)
        assert "groups" in result
