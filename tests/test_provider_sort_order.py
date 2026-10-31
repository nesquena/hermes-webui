"""Tests for provider/model-picker sort ordering.

Feature: configured and custom providers float to the top in both
the Settings provider list and the model picker dropdown.

Sort tiers (both endpoints):
  0 — active provider
  1 — custom:* providers
  2 — providers with a configured key (credential pool or config.yaml api_key)
  3 — everyone else, alphabetically by provider id
"""
from __future__ import annotations

import copy
import sys
import types
from unittest import mock

import pytest
import yaml

from api import plugin_providers

import api.config as config
import api.providers as providers_mod


# ---------------------------------------------------------------------------
# Helpers — lightweight stubs so we don't need a real hermes-agent install
# ---------------------------------------------------------------------------

def _install_fake_hermes_cli(monkeypatch):
    """Stub hermes_cli so detection is deterministic in tests."""
    fake_pkg = types.ModuleType("hermes_cli")
    fake_pkg.__path__ = []

    fake_models = types.ModuleType("hermes_cli.models")
    fake_models.list_available_providers = lambda: []
    fake_models.provider_model_ids = lambda pid: []

    fake_auth = types.ModuleType("hermes_cli.auth")
    fake_auth.get_auth_status = lambda _pid: {}

    monkeypatch.setitem(sys.modules, "hermes_cli", fake_pkg)
    monkeypatch.setitem(sys.modules, "hermes_cli.models", fake_models)
    monkeypatch.setitem(sys.modules, "hermes_cli.auth", fake_auth)
    monkeypatch.delitem(sys.modules, "agent.credential_pool", raising=False)
    monkeypatch.delitem(sys.modules, "agent", raising=False)

    config.invalidate_models_cache()


@pytest.fixture(autouse=True)
def _isolate_cache():
    """Invalidate the TTL model cache around each test AND restore config.cfg.

    ``_setup_config`` calls ``config.reload_config()`` against a temp config.yaml,
    which repopulates the shared ``config.cfg`` with this file's provider/model
    config and never resets it. Under cross-file ordering a sibling test (e.g.
    test_model_resolver) then inherits a leaked ``providers`` block that injects
    an unexpected provider group and flakes its assertions. Snapshot + restore
    ``config.cfg`` (a shallow dict copy — cheap) so the leak can't escape this
    file. We deliberately do NOT touch ``_cfg_cache`` / ``_yaml_file_cache``
    (restoring those globals destabilizes unrelated state-db reconciliation
    tests in the same shard).
    """
    _saved_cfg = dict(config.cfg)
    markers = {name: getattr(config, name) for name in ("_cfg_mtime", "_cfg_path", "_cfg_fingerprint")}
    try:
        config.invalidate_models_cache()
    except Exception:
        pass
    yield
    try:
        config.invalidate_models_cache()
    except Exception:
        pass
    try:
        if isinstance(config.cfg, dict):
            config.cfg.clear()
            config.cfg.update(_saved_cfg)
        for name, value in markers.items():
            setattr(config, name, value)
    except Exception:
        pass


def _model_list(*ids):
    """Helper: build [{"id": x, "label": x}, ...] for _PROVIDER_MODELS."""
    return [{"id": m, "label": m} for m in ids]


def _setup_config(tmp_path, monkeypatch, yaml_text, provider_models=None):
    """Write config.yaml, point config module at it, install stubs."""
    _install_fake_hermes_cli(monkeypatch)

    cfgfile = tmp_path / "config.yaml"
    cfgfile.write_text(yaml_text, encoding="utf-8")
    monkeypatch.setattr(config, "_get_config_path", lambda: cfgfile)

    # Bind credential reads to this fixture's profile as well as its YAML. A
    # preceding profile-switch test may leave its temporary .env/process keys.
    monkeypatch.setattr(providers_mod, "_get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(config._thread_ctx, "env", {}, raising=False)
    monkeypatch.setattr(config._thread_ctx, "block_process_env_fallback", True, raising=False)

    # Point auth store at a non-existent file so it stays empty
    auth_path = tmp_path / "auth.json"
    monkeypatch.setattr(config, "_get_auth_store_path", lambda: auth_path)

    # Inject provider models if given
    if provider_models:
        for pid, models in provider_models.items():
            monkeypatch.setitem(config._PROVIDER_MODELS, pid, models)

    config.reload_config()


def _teardown_config():
    config.reload_config()


def _isolate_external_catalogs(tmp_path, monkeypatch):
    """Keep the host's catalog sources out of the static-card assertions.

    The installed Agent's provider plugins add cards (``xai`` beside ``x-ai``)
    and a ``~/.codex`` models cache replaces the static openai-codex list, so
    without this these cases pass on agent-free CI and fail on a host with an
    Agent and a Codex login (#8448 release gate).
    """
    # Patch the loader, not the cache: reload_config() invalidates the cache,
    # and monkeypatch restores the previous cache value afterwards.
    monkeypatch.setattr(plugin_providers, "_load_profiles_by_name", lambda: {})
    monkeypatch.setattr(plugin_providers, "_PROFILES_BY_NAME", None)
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "empty-codex-home"))


# ===================================================================
# get_providers() sort order  (api/providers.py)
# ===================================================================

class TestProviderSortOrder:
    """providers returned by get_providers() should respect tier ordering."""

    def test_active_provider_comes_first(self, tmp_path, monkeypatch):
        """The active provider (from config model.provider) is tier-0."""
        _setup_config(tmp_path, monkeypatch,
            "model:\n  provider: openrouter\n  default: test-model\n"
            "providers:\n"
            "  anthropic:\n    api_key: sk-test-123\n"
            "  openai: {}\n"
            "  openrouter:\n    api_key: sk-or-123\n"
        )

        result = providers_mod.get_providers()
        prov_ids = [p["id"] for p in result["providers"]]

        assert prov_ids[0] == "openrouter", (
            f"Expected openrouter first (active), got order: {prov_ids}"
        )
        _teardown_config()

    @pytest.mark.parametrize("reverse", [False, True])
    @pytest.mark.parametrize("native_key", ["z-last", "z_last"])
    @pytest.mark.parametrize("spelling", ["bare", "canonical", "mixed-case"])
    def test_active_native_card_uses_stable_identity(
        self, tmp_path, monkeypatch, reverse, native_key, spelling,
    ):
        """Legacy active inputs must match the card ID, not a second authority."""
        pid = "custom:" + native_key
        active = {"bare": native_key, "canonical": pid, "mixed-case": pid.title()}[spelling]
        model = "z-model" if native_key == "z-last" else "z-model:tag"
        records = [
            ("a-first", {"base_url": "https://a.invalid/v1", "api_key": "synthetic-a", "models": ["a-model"]}),
            (native_key, {"base_url": "https://z.invalid/v1", "api_key": "synthetic-z", "models": [model]}),
            ("openai", {"api_key": "synthetic-builtin"}),
        ]
        if native_key == "z_last":
            records.append(("z-last", {"base_url": "https://hyphen.invalid/v1", "api_key": "synthetic-hyphen", "models": [model]}))
        data = {"model": {"provider": active, "default": model},
                "providers": dict(reversed(records) if reverse else records)}
        _setup_config(tmp_path, monkeypatch, yaml.safe_dump(data, sort_keys=False))
        before = copy.deepcopy(config.get_config())
        bundle_before = config.resolve_custom_provider_bundle(pid)
        route_before = config.resolve_model_provider(f"@{pid}:{model}")

        result = providers_mod.get_providers()
        ids = [card["id"] for card in result["providers"]]
        assert ids[0] == pid, ids
        assert result["active_provider"] == pid
        assert ids.index("custom:a-first") < ids.index("openai")
        cards = {card["id"]: card for card in result["providers"]}
        assert cards["openai"]["has_key"] is True
        assert cards[pid]["models"] == [{"id": model, "label": model}]
        assert cards["custom:a-first"]["models"] == [{"id": "a-model", "label": "a-model"}]
        assert not {"a-first", native_key}.intersection(ids)
        assert (bundle_before["base_url"], bundle_before["api_key"]) == ("https://z.invalid/v1", "synthetic-z")
        assert config.resolve_custom_provider_bundle(pid) == bundle_before
        assert config.resolve_model_provider(f"@{pid}:{model}") == route_before == (model, pid, "https://z.invalid/v1")
        if native_key == "z_last":
            neighbor = config.resolve_custom_provider_bundle("custom:z-last")
            assert (neighbor["base_url"], neighbor["api_key"]) == ("https://hyphen.invalid/v1", "synthetic-hyphen")
        assert config.get_config() == before
        assert config.get_config()["model"]["provider"] == active

    def test_active_native_identity_uses_captured_request_config(self, tmp_path, monkeypatch):
        data = {"model": {"provider": "z-last", "default": "z-model"}, "providers": {
            "a-first": {"base_url": "https://a.invalid/v1", "models": ["a-model"]},
            "z-last": {"base_url": "https://z.invalid/v1", "models": ["z-model"]},
        }}
        _setup_config(tmp_path, monkeypatch, yaml.safe_dump(data))
        request_cfg = copy.deepcopy(config.get_config())
        # A different profile's module-global snapshot cannot resolve this owner.
        foreign_cfg = {"model": {"provider": "openai"}, "custom_providers": []}
        monkeypatch.setattr(config, "cfg", foreign_cfg)
        monkeypatch.setattr(config, "get_config", lambda: request_cfg)
        monkeypatch.setattr(providers_mod, "get_config", lambda: request_cfg)
        providers_mod.invalidate_providers_cache()
        result = providers_mod.get_providers()
        assert result["providers"][0]["id"] == result["active_provider"] == "custom:z-last"
        assert foreign_cfg == {"model": {"provider": "openai"}, "custom_providers": []}
        assert request_cfg["model"]["provider"] == "z-last"

    @pytest.mark.parametrize("active", [None, "ghost", "custom", "custom:custom", "openai", "plugin-demo"])
    def test_active_identity_keeps_non_native_lanes(self, tmp_path, monkeypatch, active):
        data = {"model": {"provider": active, "default": "model:tag"}, "providers": {
            "custom": {"base_url": "https://default.invalid/v1", "api_key": "synthetic-default"},
            "custom:custom": {"base_url": "https://named.invalid/v1", "api_key": "synthetic-named", "models": ["model:tag"]},
            "openai": {"api_key": "synthetic-builtin"},
            "plugin-demo": {"api_key": "synthetic-plugin"},
        }}
        _setup_config(tmp_path, monkeypatch, yaml.safe_dump(data))
        monkeypatch.setattr(plugin_providers, "_PROFILES_BY_NAME", {"plugin-demo": types.SimpleNamespace(
            name="plugin-demo", display_name="Plugin Demo", env_vars=("PLUGIN_DEMO_API_KEY",),
        )})
        providers_mod.invalidate_providers_cache()
        before = copy.deepcopy(config.get_config())
        result = providers_mod.get_providers()
        assert result["active_provider"] == active
        ids = [card["id"] for card in result["providers"]]
        assert "custom:plugin-demo" not in ids
        if active in {"custom", "custom:custom", "openai", "plugin-demo"}:
            assert ids[0] == active
        else:
            assert ids[0] == "custom:custom"
        named = config.resolve_custom_provider_bundle("custom:custom")
        assert (named["base_url"], named["api_key"]) == ("https://named.invalid/v1", "synthetic-named")
        assert config.get_config() == before

    @pytest.mark.parametrize("reverse", [False, True])
    def test_active_xai_card_keeps_webui_not_runtime_alias(self, tmp_path, monkeypatch, reverse):
        """The reported config uses the real x-ai catalog, not an openai-only fake."""
        _isolate_external_catalogs(tmp_path, monkeypatch)
        records = [
            ("x-ai", {"api_key": "synthetic-builtin"}),
            ("a-first", {"base_url": "https://a.invalid/v1", "models": ["a-model"]}),
            ("openai", {"api_key": "synthetic-neighbor"}),
        ]
        data = {"model": {"provider": "x-ai", "default": "grok-4.20"},
                "providers": dict(reversed(records) if reverse else records)}
        _setup_config(tmp_path, monkeypatch, yaml.safe_dump(data, sort_keys=False))
        before = copy.deepcopy(config.get_config())
        assert "x-ai" in config._PROVIDER_DISPLAY and "x-ai" in config._PROVIDER_MODELS
        assert "xai" not in config._PROVIDER_DISPLAY and "xai" not in config._PROVIDER_MODELS
        assert config._resolve_provider_alias("x-ai") == "xai"
        assert config._canonicalise_provider_id("x-ai") == "x-ai"
        bundle_before = config.resolve_custom_provider_bundle("custom:a-first")

        result = providers_mod.get_providers()
        assert result["active_provider"] == "x-ai"
        assert result["providers"][0]["id"] == "x-ai"
        cards = {card["id"]: card for card in result["providers"]}
        assert cards["x-ai"]["models"] == config._PROVIDER_MODELS["x-ai"]
        assert cards["x-ai"]["has_key"] is cards["openai"]["has_key"] is True
        assert "xai" not in cards and "custom:x-ai" not in cards
        assert providers_mod._get_provider_api_key("x-ai") == "synthetic-builtin"
        assert providers_mod._get_provider_api_key("openai") == "synthetic-neighbor"
        assert config._get_provider_base_url("x-ai") is None
        assert config.resolve_custom_provider_bundle("custom:a-first") == bundle_before
        assert (bundle_before["base_url"], bundle_before["api_key"]) == ("https://a.invalid/v1", None)
        assert config.get_config() == before

    @pytest.mark.parametrize("reverse", [False, True])
    @pytest.mark.parametrize("mixed_case", [False, True])
    @pytest.mark.parametrize("pid", sorted(set(config._PROVIDER_DISPLAY) | set(config._PROVIDER_MODELS)))
    def test_active_builtin_metadata_card_identity(self, tmp_path, monkeypatch, reverse, mixed_case, pid):
        """Every current static card competes with native and keyed neighbors."""
        _isolate_external_catalogs(tmp_path, monkeypatch)
        active = f" {pid.upper()} " if mixed_case else pid
        neighbor = "anthropic" if pid != "anthropic" else "openai"
        records = [
            (pid, {"base_url": "https://builtin.invalid/v1", "api_key": "synthetic-builtin"}),
            ("a-first", {"base_url": "https://a.invalid/v1", "api_key": "synthetic-native", "models": ["a-model"]}),
            (neighbor, {"api_key": "synthetic-neighbor"}),
        ]
        data = {"model": {"provider": active, "default": "test-model"},
                "providers": dict(reversed(records) if reverse else records)}
        _setup_config(tmp_path, monkeypatch, yaml.safe_dump(data, sort_keys=False))
        before = copy.deepcopy(config.get_config())
        models_before = copy.deepcopy(config._PROVIDER_MODELS.get(pid, []))
        bundle_before = config.resolve_custom_provider_bundle("custom:a-first")

        result = providers_mod.get_providers()
        assert result["active_provider"] == pid
        assert result["providers"][0]["id"] == pid
        cards = {card["id"]: card for card in result["providers"]}
        assert cards[pid]["models"] == models_before
        assert cards[pid]["has_key"] is cards[neighbor]["has_key"] is True
        assert "custom:" + pid not in cards
        assert providers_mod._get_provider_api_key(pid) == "synthetic-builtin"
        assert providers_mod._get_provider_api_key(neighbor) == "synthetic-neighbor"
        assert config._get_provider_base_url(pid) == "https://builtin.invalid/v1"
        assert config.resolve_custom_provider_bundle("custom:a-first") == bundle_before
        assert (bundle_before["base_url"], bundle_before["api_key"]) == ("https://a.invalid/v1", "synthetic-native")
        assert config.get_config() == before
        assert config.get_config()["model"]["provider"] == active

    @pytest.mark.parametrize("reverse", [False, True])
    @pytest.mark.parametrize(("active", "pid"), [
        ("z.ai", "zai"), ("OPENCODE_GO", "opencode-go"),
        ("google-gemini", "gemini"), ("MINIMAX_CN", "minimax-cn"),
        ("GitHub-Copilot", "copilot"),
    ])
    def test_active_builtin_alias_matches_emitted_card(self, tmp_path, monkeypatch, reverse, active, pid):
        records = [(pid, {"api_key": "synthetic-builtin"}),
                   ("a-first", {"base_url": "https://a.invalid/v1", "models": ["a-model"]})]
        data = {"model": {"provider": active, "default": "test-model"},
                "providers": dict(reversed(records) if reverse else records)}
        _setup_config(tmp_path, monkeypatch, yaml.safe_dump(data, sort_keys=False))
        before = copy.deepcopy(config.get_config())
        result = providers_mod.get_providers()
        assert result["providers"][0]["id"] == result["active_provider"] == pid
        assert providers_mod._get_provider_api_key(pid) == "synthetic-builtin"
        assert config.get_config() == before

    @pytest.mark.parametrize("reverse", [False, True])
    @pytest.mark.parametrize("mixed_case", [False, True])
    @pytest.mark.parametrize("explicit_named", [False, True])
    @pytest.mark.parametrize("pid", ["openai", "x-ai", "google"])
    def test_active_builtin_and_explicit_named_card_keep_namespaces(
        self, tmp_path, monkeypatch, reverse, mixed_case, explicit_named, pid,
    ):
        named = "custom:" + pid
        expected = named if explicit_named else pid
        active = f" {expected.upper()} " if mixed_case else expected
        records = [(pid, {"base_url": "https://builtin.invalid/v1", "api_key": "synthetic-builtin"}),
                   (named, {"base_url": "https://named.invalid/v1", "api_key": "synthetic-named", "models": ["named-model:tag"]}),
                   ("a-first", {"base_url": "https://a.invalid/v1", "models": ["a-model"]})]
        data = {"model": {"provider": active, "default": "test-model"},
                "providers": dict(reversed(records) if reverse else records)}
        _setup_config(tmp_path, monkeypatch, yaml.safe_dump(data, sort_keys=False))
        before = copy.deepcopy(config.get_config())
        # The runtime resolver's existing named preference must NOT be changed globally.
        runtime_before = config._resolve_configured_provider_id(pid, before)
        assert runtime_before == named
        bundle_before = config.resolve_custom_provider_bundle(named)
        route_before = config.resolve_model_provider(f"@{named}:named-model:tag")

        result = providers_mod.get_providers()
        assert result["providers"][0]["id"] == result["active_provider"] == expected
        assert {pid, named, "custom:a-first"} <= {card["id"] for card in result["providers"]}
        assert providers_mod._get_provider_api_key(pid) == "synthetic-builtin"
        assert config._get_provider_base_url(pid) == "https://builtin.invalid/v1"
        assert (bundle_before["base_url"], bundle_before["api_key"]) == ("https://named.invalid/v1", "synthetic-named")
        assert config.resolve_custom_provider_bundle(named) == bundle_before
        assert config.resolve_model_provider(f"@{named}:named-model:tag") == route_before == ("named-model:tag", named, "https://named.invalid/v1")
        assert config._resolve_configured_provider_id(pid, before) == runtime_before
        assert config.get_config() == before

    @pytest.mark.parametrize("reverse", [False, True])
    @pytest.mark.parametrize("mixed_case", [False, True])
    @pytest.mark.parametrize("pid", ["plugin-demo", "plugin_demo"])
    def test_active_plugin_card_preserves_registered_identity(self, tmp_path, monkeypatch, reverse, mixed_case, pid):
        active = f" {pid.upper()} " if mixed_case else pid
        records = [("a-first", {"base_url": "https://a.invalid/v1", "models": ["a-model"]}),
                   ("openai", {"api_key": "synthetic-neighbor"})]
        # Keep model-scoped credentials as a separate positive lane from the
        # providers-mapping regressions below; neither lane replaces the other.
        data = {"model": {"provider": active, "default": "plugin-model", "api_key": "synthetic-plugin"},
                "providers": dict(reversed(records) if reverse else records)}
        _setup_config(tmp_path, monkeypatch, yaml.safe_dump(data, sort_keys=False))
        monkeypatch.setattr(plugin_providers, "_PROFILES_BY_NAME", {pid: types.SimpleNamespace(
            name=pid, display_name="Plugin Demo", env_vars=("PLUGIN_DEMO_API_KEY",),
        )})
        # Agent aliases cannot rename an already emitted WebUI/plugin card.
        monkeypatch.setattr(sys.modules["hermes_cli.models"], "_PROVIDER_ALIASES", {pid: "openai"}, raising=False)
        providers_mod.invalidate_providers_cache()
        before = copy.deepcopy(config.get_config())
        result = providers_mod.get_providers()
        assert result["providers"][0]["id"] == result["active_provider"] == pid
        card = result["providers"][0]
        assert card["is_plugin_provider"] is card["has_key"] is True
        assert "custom:" + pid not in {p["id"] for p in result["providers"]}
        assert providers_mod._get_provider_api_key(pid) == "synthetic-plugin"
        assert config.get_config() == before

    @pytest.mark.parametrize("reverse", [False, True])
    @pytest.mark.parametrize("mixed_case", [False, True])
    @pytest.mark.parametrize("pid", ["plugin-demo", "plugin_demo"])
    def test_registered_plugin_settings_card_is_not_native(self, tmp_path, monkeypatch, reverse, mixed_case, pid):
        active = f" {pid.upper()} " if mixed_case else pid
        records = [(pid, {"api_key": "synthetic-plugin"}),
                   ("a-first", {"base_url": "https://a.invalid/v1", "models": ["a-model"]})]
        data = {"model": {"provider": active, "default": "plugin-model"},
                "providers": dict(reversed(records) if reverse else records)}
        _setup_config(tmp_path, monkeypatch, yaml.safe_dump(data, sort_keys=False))
        # _setup_config invalidates discovery BEFORE registration. Invalidating
        # models afterwards would silently clear this real registry interface.
        monkeypatch.setattr(plugin_providers, "_PROFILES_BY_NAME", {pid: types.SimpleNamespace(
            name=pid, display_name="Plugin Demo", env_vars=("PLUGIN_DEMO_API_KEY",),
        )})
        providers_mod.invalidate_providers_cache()
        before = copy.deepcopy(config.get_config())
        assert "api_key" not in before["model"]
        assert plugin_providers.is_plugin_model_provider(pid) is True
        result = providers_mod.get_providers()
        ids = [card["id"] for card in result["providers"]]
        assert result["active_provider"] == ids[0] == pid
        assert "custom:" + pid not in ids
        card = result["providers"][0]
        assert card["is_plugin_provider"] is card["has_key"] is True
        assert card["key_source"] == "config_yaml"
        assert card["models"] == []  # the exact report has no plugin models setting
        assert providers_mod._get_provider_api_key(pid) == "synthetic-plugin"
        for spelling in (pid, f" {pid.upper()} "):
            assert plugin_providers.is_plugin_model_provider(spelling) is True
            assert config._is_native_custom_provider_key(spelling) is False
        for for_catalog in (False, True):
            entries = config._custom_provider_entries(before, for_catalog=for_catalog)
            assert [entry["provider_key"] for entry in entries] == ["a-first"]
        assert config.get_config() == before

    @pytest.mark.parametrize("reverse", [False, True])
    @pytest.mark.parametrize("mixed_case", [False, True])
    @pytest.mark.parametrize("pid", ["plugin-demo", "plugin_demo"])
    @pytest.mark.parametrize("producer", ["cold", "refreshed"])
    def test_registered_plugin_settings_models_stay_out_of_native_catalogs(
        self, tmp_path, monkeypatch, reverse, mixed_case, pid, producer,
    ):
        active = f" {pid.upper()} " if mixed_case else pid
        records = [(pid, {"api_key": "synthetic-plugin", "models": ["plugin-model:tag"]}),
                   ("a-first", {"base_url": "https://a.invalid/v1", "api_key": "synthetic-neighbor",
                                "models": ["a-model"]}),
                   ("openai", {"api_key": "synthetic-builtin"})]
        data = {"model": {"provider": active, "default": "plugin-model:tag"},
                "providers": dict(reversed(records) if reverse else records)}
        _setup_config(tmp_path, monkeypatch, yaml.safe_dump(data, sort_keys=False))
        monkeypatch.setattr(config, "_models_cache_path", tmp_path / "models_cache.json")
        monkeypatch.setattr(plugin_providers, "_PROFILES_BY_NAME", {pid: types.SimpleNamespace(
            name=pid, display_name="Plugin Demo", env_vars=("PLUGIN_DEMO_API_KEY",),
        )})
        providers_mod.invalidate_providers_cache()
        before = copy.deepcopy(config.get_config())
        assert plugin_providers.is_plugin_model_provider(pid) is True
        catalog = (config._static_models_catalog_without_live_probes() if producer == "cold"
                   else config.get_available_models(force_refresh=True))
        named = {g["provider_id"]: g for g in catalog["groups"] if g["provider_id"].startswith("custom:")}
        assert set(named) == {"custom:a-first"}
        assert {model["id"] for model in named["custom:a-first"]["models"]} == {"@custom:a-first:a-model"}
        for for_catalog in (False, True):
            entries = config._custom_provider_entries(before, for_catalog=for_catalog)
            assert [entry["provider_key"] for entry in entries] == ["a-first"]
        # Generic plugin catalog aliasing has its own policy; only native
        # read-adapter admission is under test, not a plugin SDK alias rewrite.
        assert any(model["id"].removeprefix(f"@{g['provider_id']}:") == "plugin-model:tag"
                   for g in catalog["groups"] if not g["provider_id"].startswith("custom:")
                   for model in g["models"])
        cards = {card["id"]: card for card in providers_mod.get_providers()["providers"]}
        assert "custom:" + pid not in cards
        assert {model["id"] for model in cards[pid]["models"]} == {"plugin-model:tag"}
        assert {model["id"] for model in cards["custom:a-first"]["models"]} == {"a-model"}
        assert providers_mod._get_provider_api_key(pid) == "synthetic-plugin"
        assert providers_mod._get_provider_api_key("openai") == "synthetic-builtin"
        assert providers_mod._get_provider_api_key("custom:a-first") == "synthetic-neighbor"
        assert plugin_providers.is_plugin_model_provider(pid) is True
        assert config.get_config() == before

    @pytest.mark.parametrize("reverse", [False, True])
    @pytest.mark.parametrize("pid", ["plugin-demo", "plugin_demo"])
    def test_explicit_named_plugin_collision_remains_separate(self, tmp_path, monkeypatch, reverse, pid):
        named = "custom:" + pid
        records = [(pid, {"api_key": "synthetic-plugin", "models": ["plugin-model:tag"]}),
                   (named, {"name": "Independent Named", "base_url": "https://named.invalid/v1",
                            "api_key": "synthetic-named", "models": ["named-model:tag"],
                            "transport": "codex_responses", "context_length": 123456}),
                   ("a-first", {"base_url": "https://a.invalid/v1", "models": ["a-model"]})]
        data = {"model": {"provider": "openai", "default": "other-model"},
                "providers": dict(reversed(records) if reverse else records)}
        _setup_config(tmp_path, monkeypatch, yaml.safe_dump(data, sort_keys=False))
        monkeypatch.setattr(config, "_models_cache_path", tmp_path / "models_cache.json")
        monkeypatch.setattr(plugin_providers, "_PROFILES_BY_NAME", {pid: types.SimpleNamespace(
            name=pid, display_name="Plugin Demo", env_vars=("PLUGIN_DEMO_API_KEY",),
        )})
        providers_mod.invalidate_providers_cache()
        before = copy.deepcopy(config.get_config())
        assert plugin_providers.is_plugin_model_provider(pid) is True
        assert config._is_native_custom_provider_key(named) is True
        entries = config._custom_provider_entries(before)
        assert {entry["provider_key"] for entry in entries} == {named, "a-first"}
        bundle = config.resolve_custom_provider_bundle(named)
        assert (bundle["base_url"], bundle["api_key"]) == ("https://named.invalid/v1", "synthetic-named")
        assert bundle["owned"]["api_mode"] == "codex_responses"
        assert bundle["record"]["name"] == "Independent Named"
        assert bundle["record"]["context_length"] == 123456
        cards = {card["id"]: card for card in providers_mod.get_providers()["providers"]}
        assert cards[pid]["is_plugin_provider"] is cards[pid]["has_key"] is True
        assert cards[named]["is_custom"] is cards[named]["has_key"] is True
        assert {m["id"] for m in cards[pid]["models"]} == {"plugin-model:tag"}
        assert {m["id"] for m in cards[named]["models"]} == {"named-model:tag"}
        assert providers_mod._get_provider_api_key(pid) == "synthetic-plugin"
        for catalog in (config._static_models_catalog_without_live_probes(), config.get_available_models(force_refresh=True)):
            owned = next(g for g in catalog["groups"] if g["provider_id"] == named)
            assert {m["id"] for m in owned["models"]} == {f"@{named}:named-model:tag"}
        assert config.resolve_custom_provider_bundle(named) == bundle
        assert config.get_config() == before

    @pytest.mark.parametrize("reverse", [False, True])
    @pytest.mark.parametrize("mixed_case", [False, True])
    @pytest.mark.parametrize("pid", ["empty-legacy", "empty_legacy"])
    def test_active_empty_bare_card_has_no_native_authority(self, tmp_path, monkeypatch, reverse, mixed_case, pid):
        active = f" {pid.upper()} " if mixed_case else pid
        records = [(pid, {}), ("a-first", {"base_url": "https://a.invalid/v1", "models": ["a-model"]})]
        data = {"model": {"provider": active, "default": "test-model"},
                "providers": dict(reversed(records) if reverse else records)}
        _setup_config(tmp_path, monkeypatch, yaml.safe_dump(data, sort_keys=False))
        before = copy.deepcopy(config.get_config())
        result = providers_mod.get_providers()
        assert result["providers"][0]["id"] == result["active_provider"] == pid
        assert result["providers"][0]["has_key"] is False
        assert "custom:" + pid not in {p["id"] for p in result["providers"]}
        bundle = config.resolve_custom_provider_bundle("custom:" + pid)
        assert bundle["status"] == config.CUSTOM_SELECTION_MISSING
        assert (bundle["base_url"], bundle["api_key"]) == (None, None)
        assert config.get_config() == before

    @pytest.mark.parametrize("reverse", [False, True])
    @pytest.mark.parametrize("prefixed", [False, True])
    @pytest.mark.parametrize("state", ["missing", "disabled", "ambiguous"])
    def test_unusable_native_does_not_activate_folded_empty_card(
        self, tmp_path, monkeypatch, reverse, prefixed, state,
    ):
        active = "custom:z_last" if prefixed else "z_last"
        records = [("z-last", {}), ("a-first", {"base_url": "https://a.invalid/v1", "models": ["a-model"]})]
        if state == "disabled":
            records.append(("z_last", {"enabled": False, "base_url": "https://disabled.invalid/v1"}))
        elif state == "ambiguous":
            records.extend([("Z_Last", {"base_url": "https://z.invalid/v1"}),
                            ("z_last", {"base_url": "https://other.invalid/v1"})])
        data = {"model": {"provider": active, "default": "test-model"},
                "providers": dict(reversed(records) if reverse else records)}
        _setup_config(tmp_path, monkeypatch, yaml.safe_dump(data, sort_keys=False))
        before = copy.deepcopy(config.get_config())
        result = providers_mod.get_providers()
        assert result["active_provider"] == active
        ids = [card["id"] for card in result["providers"]]
        assert ids[0] == "custom:a-first"
        assert "z-last" in ids and "custom:z_last" not in ids
        if state == "ambiguous":
            with pytest.raises(config.AmbiguousCustomProviderError):
                config.resolve_custom_provider_bundle("custom:z_last")
        else:
            bundle = config.resolve_custom_provider_bundle("custom:z_last")
            assert bundle["status"] == config.CUSTOM_SELECTION_MISSING
            assert (bundle["base_url"], bundle["api_key"]) == (None, None)
        assert config.get_config() == before

    @pytest.mark.parametrize("state", ["missing", "disabled", "ambiguous"])
    def test_active_identity_does_not_resurrect_unusable_native(self, tmp_path, monkeypatch, state):
        records = {"a-first": {"base_url": "https://a.invalid/v1", "models": ["a-model"]},
                   "openai": {"api_key": "synthetic-builtin"}}
        if state == "disabled":
            records["z-last"] = {"base_url": "https://z.invalid/v1", "enabled": False}
        elif state == "ambiguous":
            records.update({"Z-Last": {"base_url": "https://z.invalid/v1"},
                            "z-last": {"base_url": "https://other.invalid/v1"}})
        data = {"model": {"provider": "z-last", "default": "z-model"}, "providers": records}
        _setup_config(tmp_path, monkeypatch, yaml.safe_dump(data))
        before = copy.deepcopy(config.get_config())
        result = providers_mod.get_providers()
        ids = [card["id"] for card in result["providers"]]
        assert result["active_provider"] == "z-last"
        assert ids[0] == "custom:a-first"
        assert not {"z-last", "custom:z-last"}.intersection(ids)
        if state == "ambiguous":
            with pytest.raises(config.AmbiguousCustomProviderError):
                config.resolve_custom_provider_bundle("custom:z-last")
        else:
            bundle = config.resolve_custom_provider_bundle("custom:z-last")
            assert bundle["status"] == config.CUSTOM_SELECTION_MISSING
            assert (bundle["base_url"], bundle["api_key"]) == (None, None)
        assert config.get_config() == before

    def test_custom_provider_before_plain(self, tmp_path, monkeypatch):
        """custom:* providers (tier-1) sort before providers with keys (tier-2)."""
        _setup_config(tmp_path, monkeypatch,
            "model:\n  provider: openai\n  default: gpt-4\n"
            "providers:\n"
            "  openai: {}\n"
            "  anthropic:\n    api_key: sk-ant-123\n"
            "custom_providers:\n"
            "  - name: MyLocal\n"
            "    base_url: http://localhost:8080/v1\n"
            "    api_key: local-key\n"
            "    models:\n"
            "      - local-model-a\n"
        )

        result = providers_mod.get_providers()
        prov_ids = [p["id"] for p in result["providers"]]

        openai_idx = prov_ids.index("openai")
        custom_idx = prov_ids.index("custom:mylocal")
        anthropic_idx = prov_ids.index("anthropic")

        assert openai_idx < custom_idx < anthropic_idx, (
            f"Expected openai < custom:mylocal < anthropic, got {prov_ids}"
        )
        _teardown_config()

    def test_has_key_provider_before_no_key(self, tmp_path, monkeypatch):
        """Providers with keys (tier-2) come before those without (tier-3)."""
        _setup_config(tmp_path, monkeypatch,
            "model:\n  provider: openai\n  default: gpt-4\n"
            "providers:\n"
            "  openai: {}\n"
            "  deepseek:\n    api_key: sk-ds-123\n"
            "  google: {}\n"
            "  groq: {}\n"
        )

        result = providers_mod.get_providers()
        prov_ids = [p["id"] for p in result["providers"]]

        # deepseek has api_key in config → should be tier-2 (before tier-3)
        deepseek_idx = prov_ids.index("deepseek")
        google_idx = prov_ids.index("google")
        groq_idx = prov_ids.index("groq")

        assert deepseek_idx < google_idx, (
            f"deepseek(has_key) at {deepseek_idx} should be before google at {google_idx}"
        )
        assert deepseek_idx < groq_idx, (
            f"deepseek(has_key) at {deepseek_idx} should be before groq at {groq_idx}"
        )
        _teardown_config()


# ===================================================================
# get_available_models() sort order  (api/config.py)
# ===================================================================

class TestModelPickerSortOrder:
    """Model picker groups should follow the same tier ordering."""

    def test_active_provider_group_is_first(self, tmp_path, monkeypatch):
        """The group for the active provider appears first in groups list."""
        _setup_config(tmp_path, monkeypatch,
            "model:\n  provider: anthropic\n  default: claude-3-5-sonnet\n"
            "providers:\n"
            "  openai:\n    api_key: sk-oai-123\n"
            "  anthropic: {}\n",
            provider_models={
                "openai": _model_list("gpt-4", "gpt-4o"),
                "anthropic": _model_list("claude-3-5-sonnet"),
            },
        )

        result = config.get_available_models()
        group_ids = [g.get("provider_id") for g in result.get("groups", [])]

        assert group_ids[0] == "anthropic", (
            f"Expected anthropic first (active), got: {group_ids}"
        )
        _teardown_config()

    def test_custom_groups_before_configured(self, tmp_path, monkeypatch):
        """custom:* groups sort before providers that merely have keys."""
        _setup_config(tmp_path, monkeypatch,
            "model:\n  provider: openai\n  default: gpt-4\n"
            "providers:\n"
            "  openai:\n    api_key: sk-oai-123\n"
            "custom_providers:\n"
            "  - name: MyLocal\n"
            "    base_url: http://localhost:8080/v1\n"
            "    api_key: local-key\n"
            "    models:\n"
            "      - local-a\n",
            provider_models={
                "openai": _model_list("gpt-4"),
            },
        )

        result = config.get_available_models()
        group_ids = [g.get("provider_id") for g in result.get("groups", [])]

        if "custom:mylocal" in group_ids:
            custom_idx = group_ids.index("custom:mylocal")
            for i, gid in enumerate(group_ids):
                if gid not in ("openai", "custom:mylocal"):
                    assert custom_idx < i, (
                        f"custom:mylocal at {custom_idx} should precede {gid} at {i}"
                    )
        _teardown_config()

    def test_configured_key_groups_before_no_key(self, tmp_path, monkeypatch):
        """Providers with api_key in config sort before those without."""
        _setup_config(tmp_path, monkeypatch,
            "model:\n  provider: openai\n  default: gpt-4\n"
            "providers:\n"
            "  openai:\n    api_key: sk-oai-123\n"
            "  deepseek:\n    api_key: sk-ds-123\n"
            "  google: {}\n",
            provider_models={
                "openai": _model_list("gpt-4"),
                "deepseek": _model_list("deepseek-chat"),
                "google": _model_list("gemini-pro"),
            },
        )

        result = config.get_available_models()
        group_ids = [g.get("provider_id") for g in result.get("groups", [])]

        if "deepseek" in group_ids and "google" in group_ids:
            ds_idx = group_ids.index("deepseek")
            google_idx = group_ids.index("google")
            assert ds_idx < google_idx, (
                f"deepseek (has key) at {ds_idx} should precede google (no key) at {google_idx}"
            )
        _teardown_config()
