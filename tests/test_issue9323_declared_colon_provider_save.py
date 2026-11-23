"""#9323: saving a default model keeps a config-declared colon provider verbatim.

exp-v0.52.742 (#9304 release folds) splits ``<built-in>:<suffix>`` provider values into the built-in prefix unless
``_provider_is_config_declared()`` finds the name in config. That check missed two declaration shapes the Agent
routes by name:
  * a native ``providers:`` record whose ``name`` is the colon name (``providers.eu.name: openrouter:eu``);
  * a declared record that is switched off (``enabled: false``), which must stay as written so the Agent refuses it,
    not be rewritten to the public built-in.
Both were saved as plain ``openrouter`` (no base_url), so later chats went to public OpenRouter. v0.52.732 and
exp-v0.52.741 kept them. The #9300 picker-pin case (an undeclared ``nous:<model group>``) must still be stripped.
"""
from pathlib import Path

import pytest
import yaml

import api.config as config
from api.config import set_hermes_default_model

EU = "https://eu-proxy.example/v1"


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    old_cfg = dict(config.cfg)
    old_mtime = config._cfg_mtime
    old_cache = config._available_models_cache
    old_cache_ts = config._available_models_cache_ts
    config_file = tmp_path / "config.yaml"
    config_file.write_text("model:\n  provider: openrouter\n  default: gpt-x\n", encoding="utf-8")
    monkeypatch.setattr(config, "_get_config_path", lambda: Path(str(config_file)))
    config.cfg.clear()
    config.cfg.update({"model": {"provider": "openrouter", "default": "gpt-x"}})
    try:
        config._cfg_mtime = config_file.stat().st_mtime
    except OSError:
        config._cfg_mtime = 0.0
    config.invalidate_models_cache()
    yield
    config.cfg.clear()
    config.cfg.update(old_cfg)
    config._cfg_mtime = old_mtime
    config._available_models_cache = old_cache
    config._available_models_cache_ts = old_cache_ts


def _write_config(extra: dict, *, reload: bool = False) -> Path:
    data = {"model": {"provider": "openrouter:eu", "default": "gpt-x", "base_url": EU}, **extra}
    path = config._get_config_path()
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    if reload:
        config.reload_config()
    else:
        config.cfg.clear()
        config.cfg.update(yaml.safe_load(path.read_text(encoding="utf-8")))
    return path


def _saved_model(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))["model"]


def _assert_kept(path: Path, result: dict) -> None:
    saved = _saved_model(path)
    assert result["provider"] == "openrouter:eu", result
    assert saved["provider"] == "openrouter:eu", saved
    assert saved.get("base_url") == EU, saved


def test_native_name_alias_is_kept():
    path = _write_config({"providers": {"eu": {"name": "openrouter:eu", "base_url": EU, "api_key": "DUMMY_EU"}}})
    _assert_kept(path, set_hermes_default_model("gpt-x", provider="openrouter:eu"))


def test_env_expanded_native_name_alias_is_kept(monkeypatch):
    monkeypatch.setenv("EU_PROXY_NAME", "openrouter:eu")
    path = _write_config(
        {"providers": {"eu": {"name": "${EU_PROXY_NAME}", "base_url": EU, "api_key": "DUMMY_EU"}}}, reload=True,
    )
    _assert_kept(path, set_hermes_default_model("gpt-x", provider="openrouter:eu"))


@pytest.mark.parametrize("enabled", [False, "false", "off", 0])
def test_switched_off_native_declaration_is_kept(enabled):
    path = _write_config(
        {"providers": {"openrouter:eu": {"base_url": EU, "api_key": "DUMMY_EU", "enabled": enabled}}}
    )
    _assert_kept(path, set_hermes_default_model("gpt-x", provider="openrouter:eu"))


def test_switched_off_native_name_alias_is_kept():
    path = _write_config(
        {"providers": {"eu": {"name": "openrouter:eu", "base_url": EU, "api_key": "DUMMY_EU", "enabled": False}}}
    )
    _assert_kept(path, set_hermes_default_model("gpt-x", provider="openrouter:eu"))


def test_switched_off_legacy_custom_provider_is_kept():
    path = _write_config(
        {"custom_providers": [{"name": "openrouter:eu", "base_url": EU, "api_key": "DUMMY_EU", "enabled": False}]}
    )
    _assert_kept(path, set_hermes_default_model("gpt-x", provider="openrouter:eu"))


@pytest.mark.parametrize("shape", ["native_key", "legacy_name"])
def test_enabled_declarations_still_kept(shape):
    entry = {"base_url": EU, "api_key": "DUMMY_EU"}
    extra = ({"providers": {"openrouter:eu": entry}} if shape == "native_key"
             else {"custom_providers": [{"name": "openrouter:eu", **entry}]})
    path = _write_config(extra)
    _assert_kept(path, set_hermes_default_model("gpt-x", provider="openrouter:eu"))


def test_undeclared_picker_pin_is_still_stripped_9300():
    """The #9300 case: a picker group id the config does not declare saves its built-in provider only, even when
    the config declares some other colon name."""
    path = _write_config({"providers": {"eu": {"name": "openrouter:eu", "base_url": EU, "api_key": "DUMMY_EU"}}})
    result = set_hermes_default_model("meituan/longcat-2.5:free", provider="nous:meituan/longcat-2.5")
    saved = _saved_model(path)
    assert result["provider"] == "nous", result
    assert saved["provider"] == "nous", saved
    assert saved["default"] == "meituan/longcat-2.5:free", saved


def test_undeclared_colon_provider_is_still_stripped():
    """A colon value no record declares (key, name, or legacy name) is still treated as a picker pin."""
    path = _write_config({"providers": {"eu": {"name": "openrouter:us", "base_url": EU, "api_key": "DUMMY_EU"}}})
    result = set_hermes_default_model("gpt-x", provider="openrouter:eu")
    assert result["provider"] == "openrouter", result
    assert _saved_model(path)["provider"] == "openrouter"
