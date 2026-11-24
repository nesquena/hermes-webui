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


def _write_config(extra: dict, *, reload: bool = False, provider: str = "openrouter:eu") -> Path:
    data = {"model": {"provider": provider, "default": "gpt-x", "base_url": EU}, **extra}
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


# Other spellings the Agent routes to the same record (hermes_cli.providers.custom_provider_aliases): a name's
# space-to-hyphen form, and for a ``custom:``-prefixed name its suffix. Each is a declaration whether the record is
# switched on or off.
SPELLINGS = {
    "hyphenated_name": ({"name": "openrouter:eu proxy"}, "openrouter:eu-proxy"),
    "custom_prefixed_name": ({"name": "custom:openrouter:eu"}, "openrouter:eu"),
}


@pytest.mark.parametrize("enabled", [True, False])
@pytest.mark.parametrize("spelling", sorted(SPELLINGS))
def test_agent_spelling_of_a_declared_name_is_kept(spelling, enabled):
    record, saved_id = SPELLINGS[spelling]
    path = _write_config(
        {"providers": {"eu": {**record, "base_url": EU, "api_key": "DUMMY_EU", "enabled": enabled}}},
        provider=saved_id,
    )
    result = set_hermes_default_model("gpt-x", provider=saved_id)
    saved = _saved_model(path)
    assert result["provider"] == saved_id, result
    assert saved["provider"] == saved_id, saved
    assert saved.get("base_url") == EU, saved


def test_declared_name_in_other_case_and_whitespace_is_kept():
    """Kept as the user selected it (surrounding whitespace trimmed), not split into the built-in."""
    path = _write_config(
        {"providers": {"eu": {"name": "openrouter:eu", "base_url": EU, "api_key": "DUMMY_EU"}}}, provider="OpenRouter:EU",
    )
    result = set_hermes_default_model("gpt-x", provider="  OpenRouter:EU  ")
    saved = _saved_model(path)
    assert result["provider"] == "OpenRouter:EU", result
    assert saved["provider"] == "OpenRouter:EU", saved
    assert saved.get("base_url") == EU, saved


ALIAS_INPUTS = [
    ("openrouter:eu proxy", "eu"), ("custom:openrouter:eu", "eu"), ("OpenRouter:EU", ""), ("", "openrouter:eu"),
    ("  Custom:My Proxy  ", "My Key"), ("custom:", "k"), ("custom:custom:x", ""), ("a  b", "c d"),
    ("nous:meituan/longcat-2.5", ""), ("CUSTOM: spaced", "Key With Spaces"), ("", ""), ("openrouter:eu", "openrouter:eu"),
]


@pytest.mark.parametrize("name,key", ALIAS_INPUTS)
def test_local_alias_copy_matches_the_agent(name, key):
    providers = pytest.importorskip("hermes_cli.providers")
    if not hasattr(providers, "custom_provider_aliases"):
        pytest.skip("Agent has no custom_provider_aliases")
    assert config._local_custom_provider_aliases(name, key) == frozenset(providers.custom_provider_aliases(name, key))


def test_alias_helper_falls_back_to_the_local_copy_without_the_agent_function(monkeypatch):
    """An Agent without ``hermes_cli.providers.custom_provider_aliases`` gets the local copy, not an error."""
    import sys

    monkeypatch.setattr(config, "_custom_provider_aliases", None)
    monkeypatch.setitem(sys.modules, "hermes_cli.providers", None)  # import raises ImportError
    assert config._provider_record_aliases("openrouter:eu proxy", "eu") == config._local_custom_provider_aliases(
        "openrouter:eu proxy", "eu"
    )
    assert config._custom_provider_aliases is config._local_custom_provider_aliases


def test_name_given_as_env_reference_is_kept_when_runtime_config_is_stale(monkeypatch):
    """The file this save loads decides, not an older runtime config that predates the record."""
    monkeypatch.setenv("EU_PROXY_NAME", "openrouter:eu proxy")
    path = _write_config(
        {"providers": {"eu": {"name": "${EU_PROXY_NAME}", "base_url": EU, "api_key": "DUMMY_EU"}}},
        provider="openrouter:eu-proxy",
    )
    config.cfg.clear()
    config.cfg.update({"model": {"provider": "openrouter", "default": "gpt-x"}})
    set_hermes_default_model("gpt-x", provider="openrouter:eu-proxy")
    assert _saved_model(path)["provider"] == "openrouter:eu-proxy"


def test_runtime_config_declaration_still_keeps_the_name():
    """A record only an older runtime config still has keeps the id as written, as on v0.52.741: the Agent then
    refuses an id it can't route, instead of the save sending it to the built-in."""
    path = _write_config({"providers": {"eu": {"name": "openrouter:us", "base_url": EU, "api_key": "DUMMY_EU"}}})
    config.cfg.update({"providers": {"eu": {"name": "openrouter:eu", "base_url": EU}}})
    result = set_hermes_default_model("gpt-x", provider="openrouter:eu")
    assert result["provider"] == "openrouter:eu", result
    assert _saved_model(path)["provider"] == "openrouter:eu"


@pytest.mark.parametrize("spelling", sorted(SPELLINGS))
def test_spellings_are_kept_with_the_local_alias_copy(monkeypatch, spelling):
    """An Agent without the alias helper falls back to the local copy, with the same result."""
    monkeypatch.setattr(config, "_custom_provider_aliases", config._local_custom_provider_aliases)
    record, saved_id = SPELLINGS[spelling]
    path = _write_config(
        {"providers": {"eu": {**record, "base_url": EU, "api_key": "DUMMY_EU", "enabled": False}}}, provider=saved_id,
    )
    set_hermes_default_model("gpt-x", provider=saved_id)
    assert _saved_model(path)["provider"] == saved_id


def test_undeclared_picker_pin_is_still_stripped_9300():
    """The #9300 case: a picker group id the config does not declare saves its built-in provider only, even when
    the config declares some other colon name."""
    path = _write_config({"providers": {"eu": {"name": "openrouter:eu", "base_url": EU, "api_key": "DUMMY_EU"}}})
    result = set_hermes_default_model("meituan/longcat-2.5:free", provider="nous:meituan/longcat-2.5")
    saved = _saved_model(path)
    assert result["provider"] == "nous", result
    assert saved["provider"] == "nous", saved
    assert saved["default"] == "meituan/longcat-2.5:free", saved


def test_legacy_provider_key_does_not_declare_a_picker_pin():
    """The Agent ignores a legacy ``custom_providers`` row's ``provider_key``, so it declares nothing: the #9300 pin
    is still stripped, while the row's ``name`` still declares itself."""
    legacy = {"custom_providers": [
        {"name": "openrouter:eu", "provider_key": "nous:meituan/longcat-2.5", "base_url": EU, "api_key": "DUMMY_EU"},
    ]}
    path = _write_config(legacy)
    result = set_hermes_default_model("meituan/longcat-2.5:free", provider="nous:meituan/longcat-2.5")
    assert result["provider"] == "nous", result
    assert _saved_model(path)["provider"] == "nous"
    path = _write_config(legacy)
    result = set_hermes_default_model("gpt-x", provider="openrouter:eu")
    assert result["provider"] == "openrouter:eu", result
    assert _saved_model(path)["provider"] == "openrouter:eu"


def test_spaced_request_for_a_hyphenated_key_is_kept_as_selected():
    """The Agent normalises a requested ``openrouter:eu proxy`` to ``openrouter:eu-proxy`` and routes it to that key;
    the save keeps the id exactly as selected."""
    path = _write_config(
        {"providers": {"openrouter:eu-proxy": {"base_url": EU, "api_key": "DUMMY_EU"}}}, provider="openrouter:eu proxy",
    )
    result = set_hermes_default_model("gpt-x", provider="openrouter:eu proxy")
    saved = _saved_model(path)
    assert result["provider"] == "openrouter:eu proxy", result
    assert saved["provider"] == "openrouter:eu proxy", saved
    assert saved.get("base_url") == EU, saved


def test_undeclared_colon_provider_is_still_stripped():
    """A colon value no record declares (key, name, or legacy name) is still treated as a picker pin."""
    path = _write_config({"providers": {"eu": {"name": "openrouter:us", "base_url": EU, "api_key": "DUMMY_EU"}}})
    result = set_hermes_default_model("gpt-x", provider="openrouter:eu")
    assert result["provider"] == "openrouter", result
    assert _saved_model(path)["provider"] == "openrouter"


# ── What the Agent resolves after the save ────────────────────────────────────
# The save must leave the runtime where v0.52.741 left it: a switched-off declaration stays unresolvable, and an
# enabled declaration resolves to its own record's endpoint and key. Both save routes are covered: the Settings
# picker (provider only) and Main model options, which re-posts the pre-filled base URL.

RECORD_KEY = "placeholder-record-key"
BUILTIN_KEY = "placeholder-openrouter-key"
RUNTIME_SHAPES = {
    "name_alias": {"providers": {"eu": {"name": "openrouter:eu", "base_url": EU, "api_key": RECORD_KEY}}},
    "name_alias_off": {
        "providers": {"eu": {"name": "openrouter:eu", "base_url": EU, "api_key": RECORD_KEY, "enabled": False}}
    },
    "key_off": {"providers": {"openrouter:eu": {"base_url": EU, "api_key": RECORD_KEY, "enabled": False}}},
    "key": {"providers": {"openrouter:eu": {"base_url": EU, "api_key": RECORD_KEY}}},
    "hyphenated_name": {"providers": {"eu": {"name": "openrouter:eu proxy", "base_url": EU, "api_key": RECORD_KEY}}},
    "hyphenated_name_off": {
        "providers": {"eu": {"name": "openrouter:eu proxy", "base_url": EU, "api_key": RECORD_KEY, "enabled": False}}
    },
    "custom_prefixed_name": {
        "providers": {"eu": {"name": "custom:openrouter:eu", "base_url": EU, "api_key": RECORD_KEY}}
    },
    "custom_prefixed_name_off": {
        "providers": {"eu": {"name": "custom:openrouter:eu", "base_url": EU, "api_key": RECORD_KEY, "enabled": False}}
    },
}
# The id each shape is saved under: the Agent's spelling for that record.
SAVED_ID = {"hyphenated_name": "openrouter:eu-proxy", "hyphenated_name_off": "openrouter:eu-proxy"}
SAVE_ROUTES = {"picker": None, "model_options": {"base_url": EU}}


def _runtime_after_save(monkeypatch, shape: str, route: str):
    path = _write_config(RUNTIME_SHAPES[shape], provider=SAVED_ID.get(shape, "openrouter:eu"))
    resolve = _isolate_runtime(monkeypatch, path)
    set_hermes_default_model("gpt-x", provider=SAVED_ID.get(shape, "openrouter:eu"), advanced=SAVE_ROUTES[route])
    return resolve


def _isolate_runtime(monkeypatch, path: Path):
    """Point the Agent at ``path`` with only dummy keys in the env; returns its runtime resolver."""
    runtime_provider = pytest.importorskip("hermes_cli.runtime_provider")
    from api.oauth import resolve_runtime_provider_with_anthropic_env_lock

    home = path.parent
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_BASE_HOME", str(home))
    monkeypatch.setenv("HERMES_CONFIG_PATH", str(path))
    monkeypatch.setattr(config, "_cfg_path", getattr(config, "_cfg_path", None), raising=False)
    for name in list(__import__("os").environ):
        if name.endswith("_API_KEY") or name in ("OPENAI_BASE_URL", "OPENROUTER_BASE_URL"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("OPENROUTER_API_KEY", BUILTIN_KEY)
    return lambda: resolve_runtime_provider_with_anthropic_env_lock(runtime_provider.resolve_runtime_provider)


# What the Agent raises for each switched-off shape: an id it no longer routes to any record (hermes_cli AuthError,
# a RuntimeError) and a mapping key marked disabled (ValueError).
SWITCHED_OFF_REFUSALS = {
    "name_alias_off": (RuntimeError, r"Unknown provider 'openrouter:eu'"),
    "key_off": (ValueError, r"'openrouter:eu' is disabled in config"),
    "hyphenated_name_off": (RuntimeError, r"Unknown provider 'openrouter:eu-proxy'"),
    "custom_prefixed_name_off": (RuntimeError, r"Unknown provider 'openrouter:eu'"),
}


@pytest.mark.parametrize("route", sorted(SAVE_ROUTES))
@pytest.mark.parametrize("shape", sorted(SWITCHED_OFF_REFUSALS))
def test_switched_off_declaration_stays_unresolvable_after_save(monkeypatch, shape, route):
    resolve = _runtime_after_save(monkeypatch, shape, route)
    expected, message = SWITCHED_OFF_REFUSALS[shape]
    with pytest.raises(expected, match=message):
        resolve()


@pytest.mark.parametrize("route", sorted(SAVE_ROUTES))
@pytest.mark.parametrize("shape", ["name_alias", "key", "hyphenated_name", "custom_prefixed_name"])
def test_enabled_declaration_resolves_to_its_own_record_after_save(monkeypatch, shape, route):
    resolve = _runtime_after_save(monkeypatch, shape, route)
    providers = __import__("hermes_cli.providers", fromlist=["_"])
    if shape == "custom_prefixed_name" and not hasattr(providers, "custom_provider_aliases"):
        # Agents older than the alias helper don't route a ``custom:`` name's suffix to the record at all; the id
        # kept as written is refused there (as on v0.52.741), never sent to the built-in.
        with pytest.raises(RuntimeError, match=r"Unknown provider 'openrouter:eu'"):
            resolve()
        return
    runtime = resolve()
    assert (runtime.get("provider"), runtime.get("base_url"), runtime.get("api_key")) == (
        "custom", EU, RECORD_KEY,
    ), runtime


MANAGED_SHAPES = {
    "name_alias": {"providers": {"eu": {"name": "openrouter:eu", "base_url": EU, "api_key": RECORD_KEY}}},
    "key": {"providers": {"openrouter:eu": {"base_url": EU, "api_key": RECORD_KEY}}},
    "legacy": {"custom_providers": [{"name": "openrouter:eu", "base_url": EU, "api_key": RECORD_KEY}]},
}


@pytest.mark.parametrize("shape", sorted(MANAGED_SHAPES))
def test_managed_scope_declaration_is_kept_and_resolves_to_its_record(monkeypatch, tmp_path, shape):
    """A record declared only in the Agent's managed-scope config.yaml keeps the id, which the Agent then routes to
    that record."""
    managed_scope = pytest.importorskip("hermes_cli.managed_scope")
    managed_dir = tmp_path / "managed"
    managed_dir.mkdir()
    (managed_dir / "config.yaml").write_text(yaml.safe_dump(MANAGED_SHAPES[shape]), encoding="utf-8")
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed_dir))
    managed_scope.invalidate_managed_cache()
    try:
        path = _write_config({})
        resolve = _isolate_runtime(monkeypatch, path)
        result = set_hermes_default_model("gpt-x", provider="openrouter:eu")
        saved = _saved_model(path)
        assert result["provider"] == "openrouter:eu", result
        assert saved["provider"] == "openrouter:eu", saved
        runtime = resolve()
        assert (runtime.get("base_url"), runtime.get("api_key")) == (EU, RECORD_KEY), runtime
    finally:
        managed_scope.invalidate_managed_cache()
