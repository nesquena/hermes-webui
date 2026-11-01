"""#8129 / #8032 x #8448: the auxiliary slot picks a custom provider as master does and keeps its URL reference.

``set_auxiliary_model`` reads the RAW config (so ``${VAR}`` references survive the save) but must choose the
provider exactly as master chose on its env-expanded read: a legacy ``custom_providers`` entry whose ``name: ${VAR}``
resolves to a native key's identity still shadows that native key (#8448 legacy-over-native precedence), and a native
``api: ${EMPTY}`` alias falls through to a valid ``base_url``. The winning URL is written as its raw text.
Synthetic config and environment only.
"""
import pytest
import yaml

from api import config


@pytest.fixture
def cfg_path(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    monkeypatch.setattr(config, "_get_config_path", lambda: path)
    monkeypatch.setattr(config._thread_ctx, "env", {}, raising=False)
    monkeypatch.setattr(config._thread_ctx, "block_process_env_fallback", False, raising=False)
    monkeypatch.setenv("AUXSEL_CP_NAME", "Shared")
    monkeypatch.setenv("AUXSEL_EMPTY", "")
    monkeypatch.setenv("AUXSEL_LEGACY_URL", "https://legacy.example.test/v1")
    monkeypatch.setenv("AUXSEL_NATIVE_URL", "https://native-ref.example.test/v1")
    yield path
    # Restore the real config path and env BEFORE reloading, so the module cache
    # never keeps this test's synthetic config for a later test.
    monkeypatch.undo()
    config.reload_config()


def _save(path, extra):
    data = {
        "model": {"default": "gpt-4o", "provider": "openai", "base_url": "https://main.example.test/v1"},
        **extra,
    }
    config._save_yaml_config_file(path, data)
    config.reload_config()
    config.set_auxiliary_model("vision", "custom:shared", "gpt-4o")
    return yaml.safe_load(path.read_text())


def test_env_named_legacy_entry_still_shadows_the_native_key(cfg_path):
    saved = _save(cfg_path, {
        "custom_providers": [{"name": "${AUXSEL_CP_NAME}", "base_url": "https://legacy.example.test/v1"}],
        "providers": {"shared": {"base_url": "https://native.example.test/v1"}},
    })
    assert saved["auxiliary"]["vision"]["base_url"] == "https://legacy.example.test/v1"


def test_empty_api_alias_falls_through_to_the_native_base_url(cfg_path):
    saved = _save(cfg_path, {
        "providers": {"custom:shared": {"api": "${AUXSEL_EMPTY}", "base_url": "https://fallback.example.test/v1"}},
    })
    assert saved["auxiliary"]["vision"]["base_url"] == "https://fallback.example.test/v1"


def test_legacy_url_reference_is_written_as_the_reference(cfg_path):
    saved = _save(cfg_path, {
        "custom_providers": [{"name": "${AUXSEL_CP_NAME}", "base_url": "${AUXSEL_LEGACY_URL}"}],
    })
    assert saved["auxiliary"]["vision"]["base_url"] == "${AUXSEL_LEGACY_URL}"
    assert saved["custom_providers"][0] == {"name": "${AUXSEL_CP_NAME}", "base_url": "${AUXSEL_LEGACY_URL}"}


def test_native_url_reference_is_written_as_the_reference(cfg_path):
    saved = _save(cfg_path, {
        "providers": {"shared": {"api": "${AUXSEL_EMPTY}", "base_url": "${AUXSEL_NATIVE_URL}"}},
    })
    assert saved["auxiliary"]["vision"]["base_url"] == "${AUXSEL_NATIVE_URL}"
    assert saved["providers"]["shared"] == {"api": "${AUXSEL_EMPTY}", "base_url": "${AUXSEL_NATIVE_URL}"}


def test_two_entries_resolving_to_one_slug_still_fail_closed(cfg_path):
    with pytest.raises(config.AmbiguousCustomProviderError):
        _save(cfg_path, {
            "custom_providers": [
                {"name": "${AUXSEL_CP_NAME}", "base_url": "https://a.example.test/v1"},
                {"name": "shared", "base_url": "https://b.example.test/v1"},
            ],
        })


@pytest.mark.parametrize("providers", [
    # The canonical record wins; an earlier record's equal literal must not
    # replace the winner's reference.
    {"shared": {"base_url": "https://rotating.example.test/v1"},
     "custom:shared": {"base_url": "${AUXSEL_ROTATING_URL}"}},
    # The winner is chosen through its display-name alias under another key.
    {"shared": {"models": ["gpt-4o"]},
     "different-key": {"name": "Shared", "base_url": "${AUXSEL_ROTATING_URL}"}},
], ids=["earlier-equal-literal", "winning-display-name-alias"])
def test_winning_record_reference_is_written_and_follows_rotation(cfg_path, monkeypatch, providers):
    monkeypatch.setenv("AUXSEL_ROTATING_URL", "https://rotating.example.test/v1")
    saved = _save(cfg_path, {"providers": providers})
    assert saved["auxiliary"]["vision"]["base_url"] == "${AUXSEL_ROTATING_URL}"
    monkeypatch.setenv("AUXSEL_ROTATING_URL", "https://rotated.example.test/v1")
    expanded = config._load_yaml_config_file(cfg_path)["auxiliary"]["vision"]["base_url"]
    assert expanded == "https://rotated.example.test/v1"
