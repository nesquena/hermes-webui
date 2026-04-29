"""Tests for #1106 — custom_providers[].models dict keys populate model dropdown."""
import pytest
import api.config as config


def _reset():
    try:
        config.invalidate_models_cache()
    except Exception:
        pass


def _models_with_cfg(model_cfg=None, custom_providers=None, active_provider=None):
    """Temporarily patch config.cfg, call get_available_models(), restore.

    Also pins _cfg_mtime to prevent reload_config() from overwriting patches.
    """
    old_cfg = dict(config.cfg)
    old_mtime = config._cfg_mtime
    config.cfg.clear()
    if model_cfg:
        config.cfg["model"] = model_cfg
    if custom_providers is not None:
        config.cfg["custom_providers"] = custom_providers
    try:
        config._cfg_mtime = config.Path(config._get_config_path()).stat().st_mtime
    except Exception:
        config._cfg_mtime = 0.0
    try:
        return config.get_available_models()
    finally:
        config.cfg.clear()
        config.cfg.update(old_cfg)
        config._cfg_mtime = old_mtime


def _all_model_ids(result):
    """Extract all model IDs from all groups."""
    ids = []
    for g in result.get("groups", []):
        for m in g.get("models", []):
            ids.append(m["id"])
    return ids


def _group_for(result, provider_name):
    """Get a group by provider name."""
    for g in result.get("groups", []):
        if g.get("provider") == provider_name:
            return g
    return None


class TestCustomProvidersModelsDict:
    """custom_providers entries with a 'models' dict should populate all keys in the dropdown."""

    def test_models_dict_keys_appear_in_dropdown(self):
        """Each key in custom_providers[].models should appear as a selectable model."""
        result = _models_with_cfg(
            model_cfg={"provider": "custom"},
            custom_providers=[
                {
                    "name": "Llama-swap",
                    "base_url": "http://llama-swap:8880/v1",
                    "model": "unsloth-qwen3.6-35b-a3b",
                    "models": {
                        "unsloth-qwen3.6-35b-a3b": {"context_length": 262144},
                        "gemma4-26b": {},
                        "qwen3.5-27b": {},
                        "qwen3-coder-30b": {},
                    },
                }
            ],
        )
        ids = _all_model_ids(result)
        for expected in ["unsloth-qwen3.6-35b-a3b", "gemma4-26b", "qwen3.5-27b", "qwen3-coder-30b"]:
            assert expected in ids, f"Expected '{expected}' in model IDs, got {ids}"

    def test_models_dict_without_model_field_still_works(self):
        """If only 'models' dict is present (no singular 'model'), all dict keys should appear."""
        result = _models_with_cfg(
            model_cfg={"provider": "custom"},
            custom_providers=[
                {
                    "name": "Local-LLM",
                    "base_url": "http://localhost:8080/v1",
                    "models": {
                        "llama-3-8b": {},
                        "mistral-7b": {},
                    },
                }
            ],
        )
        ids = _all_model_ids(result)
        assert "llama-3-8b" in ids
        assert "mistral-7b" in ids

    def test_no_duplicates_when_model_and_models_overlap(self):
        """If 'model' value also appears in 'models' dict, it should not be duplicated."""
        result = _models_with_cfg(
            model_cfg={"provider": "custom"},
            custom_providers=[
                {
                    "name": "MyServer",
                    "base_url": "http://myserver:8000/v1",
                    "model": "base-model",
                    "models": {
                        "base-model": {},
                        "other-model": {},
                    },
                }
            ],
        )
        ids = _all_model_ids(result)
        assert ids.count("base-model") == 1, f"'base-model' should appear exactly once, got {ids.count('base-model')}"
        assert "other-model" in ids

    def test_unnamed_provider_models_dict_works(self):
        """custom_providers without 'name' should still populate 'Custom' group."""
        result = _models_with_cfg(
            model_cfg={"provider": "custom"},
            custom_providers=[
                {
                    "model": "my-model",
                    "models": {
                        "extra-model-a": {},
                        "extra-model-b": {},
                    },
                }
            ],
        )
        ids = _all_model_ids(result)
        for expected in ["my-model", "extra-model-a", "extra-model-b"]:
            assert expected in ids, f"Expected '{expected}' in model IDs, got {ids}"

    def test_empty_models_dict_is_ignored(self):
        """An empty 'models' dict should not break anything."""
        result = _models_with_cfg(
            model_cfg={"provider": "custom"},
            custom_providers=[
                {
                    "name": "TestServer",
                    "model": "only-model",
                    "models": {},
                }
            ],
        )
        ids = _all_model_ids(result)
        assert "only-model" in ids

    def test_non_string_models_keys_are_skipped(self):
        """Non-string keys in models dict should be silently skipped."""
        result = _models_with_cfg(
            model_cfg={"provider": "custom"},
            custom_providers=[
                {
                    "name": "TestServer",
                    "model": "valid-model",
                    "models": {
                        "another-valid": {},
                        123: {},  # non-string key
                        None: {},  # non-string key
                    },
                }
            ],
        )
        ids = _all_model_ids(result)
        assert "valid-model" in ids
        assert "another-valid" in ids

    def test_multiple_custom_providers_each_keep_models_separate(self):
        """Multiple named custom_providers should each have their own models."""
        result = _models_with_cfg(
            model_cfg={"provider": "custom"},
            custom_providers=[
                {
                    "name": "Server-A",
                    "model": "model-a1",
                    "models": {"model-a2": {}},
                },
                {
                    "name": "Server-B",
                    "model": "model-b1",
                    "models": {"model-b2": {}},
                },
            ],
        )
        group_a = _group_for(result, "Server-A")
        group_b = _group_for(result, "Server-B")
        assert group_a is not None, "Server-A group missing"
        assert group_b is not None, "Server-B group missing"
        ids_a = [m["id"] for m in group_a["models"]]
        ids_b = [m["id"] for m in group_b["models"]]
        assert "model-a1" in ids_a and "model-a2" in ids_a
        assert "model-b1" in ids_b and "model-b2" in ids_b
        # No cross-contamination
        assert "model-b1" not in ids_a
        assert "model-a1" not in ids_b
