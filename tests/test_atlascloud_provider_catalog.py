"""Atlas Cloud provider catalog and routing tests."""

import pathlib
import sys

REPO = pathlib.Path(__file__).parent.parent
sys.path.insert(0, str(REPO))

import api.config as config


def test_atlascloud_provider_catalog_entry_is_available():
    assert config._PROVIDER_DISPLAY["atlascloud"] == "Atlas Cloud"
    assert config._resolve_provider_alias("atlas-cloud") == "atlascloud"

    atlas_models = {
        model["id"]: model["label"]
        for model in config._PROVIDER_MODELS["atlascloud"]
    }
    assert atlas_models["qwen/qwen3.5-flash"] == "Qwen3.5 Flash"
    assert atlas_models["deepseek-ai/deepseek-v4-pro"] == "DeepSeek V4 Pro"


def test_atlascloud_preserves_namespaced_model_ids(monkeypatch):
    monkeypatch.setattr(
        config,
        "cfg",
        {
            "model": {
                "provider": "atlascloud",
                "default": "qwen/qwen3.5-flash",
            }
        },
    )

    assert config.resolve_model_provider("qwen/qwen3.5-flash") == (
        "qwen/qwen3.5-flash",
        "atlascloud",
        config.ATLASCLOUD_API_BASE_URL,
    )
