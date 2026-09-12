"""Regression coverage for providers with ``models_discovered: true``.

Upstream Hermes Agent treats a ``models`` mapping as per-model metadata, not a
picker allowlist, when the entry carries ``models_discovered: true`` (a catalog
Hermes itself persisted after a successful /v1/models probe). WebUI must do the
same: the live /v1/models catalog stays authoritative and the configured subset
must not clamp the dropdown to its own keys (#7404).
"""


def _provider_group(payload: dict, provider_id: str) -> dict:
    for group in payload.get("groups", []):
        if group.get("provider_id") == provider_id:
            return group
    raise AssertionError(f"provider group {provider_id!r} not found: {payload.get('groups')!r}")


def _ids_from_group(group: dict) -> list[str]:
    return [m["id"] for m in group["models"]]


def _setup(monkeypatch, tmp_path, cfg, live_models):
    import api.config as config

    monkeypatch.setattr(config, "cfg", cfg, raising=False)
    monkeypatch.setattr(config, "_get_config_path", lambda: tmp_path / "config.yaml")
    monkeypatch.setattr(config, "_get_auth_store_path", lambda: tmp_path / "auth.json")
    monkeypatch.setattr(config, "_get_models_cache_path", lambda: tmp_path / "models_cache.json")
    monkeypatch.setattr(config, "_models_cache_source_fingerprint", lambda: {"test": "fingerprint"})
    monkeypatch.setattr(config, "reload_config_if_stale", lambda: None)
    monkeypatch.setattr(config, "reload_config", lambda: None)
    monkeypatch.setattr(config, "_cfg_mtime", 0.0, raising=False)
    monkeypatch.setattr(config, "_LIVE_REBUILD_BUDGET_SECONDS", 0.0, raising=False)
    monkeypatch.setattr(
        config,
        "_read_live_provider_model_ids",
        lambda pid: live_models if pid == cfg["model"]["provider"] else [],
    )
    config.invalidate_models_cache()
    return config


def test_models_discovered_catalog_is_not_a_picker_allowlist(monkeypatch, tmp_path):
    """models_discovered: true must surface the full live catalog, not just the
    configured subset."""
    live_models = [
        "deepseek-v4-pro",
        "deepseek-v4-flash",
        "deepseek-v4-lite",
        "deepseek-r1-0706",
        "deepseek-v4-reasoner",
    ]
    cfg = {
        "model": {"default": "deepseek-v4-pro", "provider": "deepseek"},
        "providers": {
            "deepseek": {
                "name": "DeepSeek",
                "models_discovered": True,
                "models": {
                    "deepseek-v4-pro": {"context_length": 65536},
                    "deepseek-v4-flash": {"context_length": 32768},
                },
            }
        },
    }
    config = _setup(monkeypatch, tmp_path, cfg, live_models)

    payload = config.get_available_models(force_refresh=True)
    group = _provider_group(payload, "deepseek")
    ids = _ids_from_group(group)

    assert set(ids) == set(live_models), f"expected full live catalog, got {ids}"
    assert "deepseek-v4-lite" in ids
    assert "deepseek-r1-0706" in ids


def test_models_discovered_false_keeps_configured_allowlist(monkeypatch, tmp_path):
    """Without the discovered flag the configured models remain a strict
    allowlist (existing behavior must not regress)."""
    live_models = [
        "deepseek-v4-pro",
        "deepseek-v4-flash",
        "deepseek-v4-lite",
        "deepseek-r1-0706",
    ]
    cfg = {
        "model": {"default": "deepseek-v4-pro", "provider": "deepseek"},
        "providers": {
            "deepseek": {
                "name": "DeepSeek",
                "models_discovered": False,
                "models": {
                    "deepseek-v4-pro": {"context_length": 65536},
                    "deepseek-v4-flash": {"context_length": 32768},
                },
            }
        },
    }
    config = _setup(monkeypatch, tmp_path, cfg, live_models)

    payload = config.get_available_models(force_refresh=True)
    group = _provider_group(payload, "deepseek")
    ids = _ids_from_group(group)

    assert set(ids) == {"deepseek-v4-pro", "deepseek-v4-flash"}, f"expected configured allowlist, got {ids}"


def test_legacy_discovered_sentinel_is_also_not_an_allowlist(monkeypatch, tmp_path):
    """The legacy in-mapping ``__discovered_model_catalog__`` sentinel written
    by older Hermes versions must behave like ``models_discovered: true``."""
    live_models = [
        "deepseek-v4-pro",
        "deepseek-v4-flash",
        "deepseek-v4-lite",
        "deepseek-r1-0706",
    ]
    cfg = {
        "model": {"default": "deepseek-v4-pro", "provider": "deepseek"},
        "providers": {
            "deepseek": {
                "name": "DeepSeek",
                "models": {
                    "__discovered_model_catalog__": True,
                    "deepseek-v4-pro": {"context_length": 65536},
                    "deepseek-v4-flash": {"context_length": 32768},
                },
            }
        },
    }
    config = _setup(monkeypatch, tmp_path, cfg, live_models)

    payload = config.get_available_models(force_refresh=True)
    group = _provider_group(payload, "deepseek")
    ids = _ids_from_group(group)

    assert set(ids) == set(live_models), f"expected full live catalog, got {ids}"
    assert "__discovered_model_catalog__" not in ids


# ── Network-free / static-catalog consistency (review #7406) ───────────────


def _static_catalog_setup(monkeypatch, tmp_path, providers_cfg, *, default="claude-sonnet-4.6"):
    """Drive _static_models_catalog_without_live_probes() offline against a
    temp config with a single known provider (anthropic)."""
    import api.config as config
    from api import providers as prov

    config_path = tmp_path / "config.yaml"
    config_path.write_text("model: {}\n", encoding="utf-8")
    auth_store_path = tmp_path / "auth.json"
    auth_store_path.write_text("{}", encoding="utf-8")
    hermes_home = tmp_path / "hermes-home"
    hermes_home.mkdir(exist_ok=True)
    (hermes_home / ".env").write_text("", encoding="utf-8")

    monkeypatch.setattr(config, "_get_config_path", lambda: config_path)
    monkeypatch.setattr(config, "_cfg_path", config_path, raising=False)
    monkeypatch.setattr(config, "_cfg_mtime", config_path.stat().st_mtime, raising=False)
    monkeypatch.setattr(config, "_get_auth_store_path", lambda: auth_store_path)
    monkeypatch.setattr(config, "_get_models_cache_path", lambda: tmp_path / "models_cache.json")
    monkeypatch.setattr(config, "_load_models_cache_from_disk", lambda: None)
    monkeypatch.setattr(config, "_save_models_cache_to_disk", lambda *_a, **_k: None)
    monkeypatch.setattr(config.os, "getenv", lambda key, default=None: default or "", raising=False)

    # Config: active provider is the known anthropic provider, with its
    # configured models mapping (the per-case flag shape drives the assertion).
    monkeypatch.setattr(
        config,
        "cfg",
        {
            "model": {"provider": "anthropic", "default": default},
            "providers": providers_cfg,
        },
        raising=False,
    )
    # Only anthropic has a key, so the static catalog stays hermetic.
    monkeypatch.setattr(prov, "_provider_has_key", lambda pid: pid == "anthropic")
    if hasattr(prov, "invalidate_providers_cache"):
        prov.invalidate_providers_cache()

    from api.plugin_providers import invalidate_plugin_model_provider_cache

    invalidate_plugin_model_provider_cache()
    return config


def _static_group_ids(config, provider_id="anthropic"):
    catalog = config._static_models_catalog_without_live_probes()
    for group in catalog["groups"]:
        if group.get("provider_id") == provider_id:
            return [m["id"] for m in group["models"]]
    raise AssertionError(f"anthropic group not in static catalog: {catalog['groups']!r}")


def _all_static_ids(config):
    catalog = config._static_models_catalog_without_live_probes()
    ids = [m["id"] for group in catalog["groups"] for m in group["models"]]
    return ids


def test_static_catalog_models_discovered_keeps_broader_static_catalog(monkeypatch, tmp_path):
    """models_discovered: true must NOT clamp the offline static catalog to the
    persisted subset — the broader _PROVIDER_MODELS list stays authoritative
    (review #7406, fix 2)."""
    providers_cfg = {
        "anthropic": {
            "name": "Anthropic",
            "api_key": "sk-ant-test",
            "models_discovered": True,
            "models": {
                "claude-sonnet-4.6": {"context_length": 200000},
                "claude-haiku-4-5": {},
                "claude-for-editing": {},  # persisted-only: absent from static catalog
            },
        }
    }
    config = _static_catalog_setup(monkeypatch, tmp_path, providers_cfg)
    ids = _static_group_ids(config)
    # The broader static catalog (5 claude models) must be retained, not clamped
    # to the 3 persisted keys above.
    assert "claude-opus-4.7" in ids
    assert "claude-opus-4.6" in ids
    assert "claude-sonnet-4-5" in ids
    assert len(ids) >= 5, f"expected broad static catalog, got {ids}"
    # Persisted model IDs are merged in as fallback metadata (not dropped),
    # including persisted-only IDs absent from the static catalog.
    assert "claude-sonnet-4.6" in ids
    assert "claude-haiku-4-5" in ids
    assert "claude-for-editing" in ids


def test_static_catalog_legacy_sentinel_keeps_broader_static_catalog(monkeypatch, tmp_path):
    """The legacy in-mapping __discovered_model_catalog__ sentinel must behave
    the same as models_discovered: true in the offline static catalog."""
    providers_cfg = {
        "anthropic": {
            "name": "Anthropic",
            "api_key": "sk-ant-test",
            "models": {
                "__discovered_model_catalog__": True,
                "claude-sonnet-4.6": {},
                "claude-sonnet-4-5": {},
            },
        }
    }
    config = _static_catalog_setup(monkeypatch, tmp_path, providers_cfg)
    ids = _static_group_ids(config)
    assert "claude-opus-4.7" in ids
    assert "claude-haiku-4-5" in ids
    assert "claude-sonnet-4.6" in ids
    assert len(ids) >= 5, f"expected broad static catalog, got {ids}"


def test_static_catalog_unflagged_mapping_is_strict_allowlist(monkeypatch, tmp_path):
    """Without the discovered flag, a configured models mapping stays a strict
    allowlist — the intentional #644 user-pin behavior must not regress."""
    providers_cfg = {
        "anthropic": {
            "name": "Anthropic",
            "api_key": "sk-ant-test",
            "models": {
                "claude-sonnet-4.6": {"context_length": 200000},
                "claude-haiku-4-5": {},
            },
        }
    }
    config = _static_catalog_setup(monkeypatch, tmp_path, providers_cfg)
    ids = _static_group_ids(config)
    assert set(ids) == {"claude-sonnet-4.6", "claude-haiku-4-5"}, (
        f"unflagged mapping must pin the allowlist, got {ids}"
    )


def test_configured_model_ids_filters_both_sentinels(monkeypatch):
    """Central filter: neither compatibility sentinel may surface as a model id
    from any supported mapping shape (review #7406, fix 1)."""
    import api.config as config

    mapping = {
        "__discovered_model_catalog__": True,
        "__explicit_model_allowlist__": True,
        "claude-sonnet-4.6": {"context_length": 200000},
        "claude-haiku-4-5": {},
    }
    ids = config._configured_model_ids(mapping)
    assert "__discovered_model_catalog__" not in ids
    assert "__explicit_model_allowlist__" not in ids
    assert "claude-sonnet-4.6" in ids
    assert "claude-haiku-4-5" in ids
    # Only the legacy sentinel present (no explicit-allowlist key).
    legacy_only = {
        "__discovered_model_catalog__": True,
        "deepseek-v4-flash": {},
    }
    legacy_ids = config._configured_model_ids(legacy_only)
    assert legacy_ids == ["deepseek-v4-flash"]
    # No sentinels at all — plain allowlist preserved as before.
    plain = {"deepseek-v4-flash": {}, "deepseek-v4-pro": {}}
    assert config._configured_model_ids(plain) == ["deepseek-v4-flash", "deepseek-v4-pro"]


def test_static_catalog_no_sentinel_in_models_or_extra_models(monkeypatch, tmp_path):
    """Neither sentinel may leak into the static catalog models or the picker
    overflow bucket for either discovery shape."""
    provider_configs = [
        {
            # Discovery shape (a): entry-level models_discovered flag.
            "anthropic": {
                "name": "Anthropic",
                "api_key": "sk-ant-test",
                "models_discovered": True,
                "models": {
                    "claude-sonnet-4.6": {"context_length": 200000},
                    "claude-haiku-4-5": {},
                },
            }
        },
        {
            # Discovery shape (b): legacy in-mapping sentinel.
            "anthropic": {
                "name": "Anthropic",
                "api_key": "sk-ant-test",
                "models": {
                    "__discovered_model_catalog__": True,
                    "claude-sonnet-4.6": {},
                    "claude-haiku-4-5": {},
                },
            }
        },
    ]
    for providers_cfg in provider_configs:
        config = _static_catalog_setup(monkeypatch, tmp_path, providers_cfg)
        ids = _all_static_ids(config)
        assert "__discovered_model_catalog__" not in ids
        assert "__explicit_model_allowlist__" not in ids


# ── /api/models/live policy (review #7406, fix 1) ──────────────────────────


def _install_fake_provider_model_ids(monkeypatch, fn):
    import sys
    import types

    hermes_cli = types.ModuleType("hermes_cli")
    hermes_cli.__path__ = []
    models = types.ModuleType("hermes_cli.models")
    models.provider_model_ids = fn
    monkeypatch.setitem(sys.modules, "hermes_cli", hermes_cli)
    monkeypatch.setitem(sys.modules, "hermes_cli.models", models)


def _live_models_setup(monkeypatch, cfg, live_models_by_provider, *, stub_alias=True):
    import api.config as config
    import api.routes as routes

    routes._clear_live_models_cache()
    monkeypatch.setattr(
        routes,
        "j",
        lambda _handler, payload, status=200, extra_headers=None: payload,
    )
    monkeypatch.setattr(config, "get_config", lambda: cfg)
    if stub_alias:
        monkeypatch.setattr(config, "_resolve_provider_alias", lambda provider: provider)
    _install_fake_provider_model_ids(
        monkeypatch,
        lambda provider: list(live_models_by_provider.get(provider, [])),
    )
    return routes, config


def _live_ids(monkeypatch, cfg, live_models_by_provider, query_provider, *, stub_alias=True):
    from urllib.parse import urlparse

    routes, _ = _live_models_setup(
        monkeypatch, cfg, live_models_by_provider, stub_alias=stub_alias
    )
    parsed = urlparse(f"/api/models/live?provider={query_provider}")
    payload = routes._handle_live_models(object(), parsed)
    return [m["id"] for m in payload.get("models", [])]


def test_live_models_custom_provider_filters_sentinel_keys(monkeypatch):
    """A legacy custom provider's persisted sentinels must never surface as
    selectable models on /api/models/live (#7406 fix 1)."""
    cfg = {
        "model": {"provider": "custom:acme"},
        "custom_providers": [
            {
                "name": "Acme",
                "models": {
                    "__discovered_model_catalog__": True,
                    "__explicit_model_allowlist__": True,
                    "acme-real-model": {},
                },
            }
        ],
    }
    ids = _live_ids(monkeypatch, cfg, {}, "custom:acme")
    assert "__discovered_model_catalog__" not in ids
    assert "__explicit_model_allowlist__" not in ids
    assert "acme-real-model" in ids


def test_live_models_genuine_pin_restricts_catalog(monkeypatch):
    """Without a discovery marker, the configured models stay a strict
    allowlist — an unpinned live model must not leak into the response."""
    cfg = {
        "model": {"provider": "openai"},
        "providers": {
            "openai": {
                "models": {"gpt-4o": {}, "gpt-4o-mini": {}},
            }
        },
    }
    ids = _live_ids(
        monkeypatch,
        cfg,
        {"openai": ["gpt-4o", "gpt-4o-mini", "gpt-5-unpinned"]},
        "openai",
    )
    assert set(ids) == {"gpt-4o", "gpt-4o-mini"}
    assert "gpt-5-unpinned" not in ids


def test_live_models_discovered_catalog_stays_live_authoritative(monkeypatch):
    """A discovered catalog is metadata, not a pin: the live /v1/models list is
    authoritative even when it is broader than the persisted subset."""
    cfg = {
        "model": {"provider": "openai"},
        "providers": {
            "openai": {
                "models_discovered": True,
                "models": {"gpt-4o": {}, "gpt-4o-mini": {}},
            }
        },
    }
    ids = _live_ids(monkeypatch, cfg, {"openai": ["gpt-4o", "gpt-5-discovered"]}, "openai")
    assert set(ids) == {"gpt-4o", "gpt-5-discovered"}


def test_live_models_discovered_probe_failure_falls_back_to_configured(monkeypatch):
    """When discovery probe returns nothing, the persisted (sanitized) catalog
    is the fallback — not an empty list."""
    cfg = {
        "model": {"provider": "openai"},
        "providers": {
            "openai": {
                "models_discovered": True,
                "models": {"gpt-4o": {}, "account-only-model": {}},
            }
        },
    }
    ids = _live_ids(monkeypatch, cfg, {}, "openai")
    assert "gpt-4o" in ids
    assert "account-only-model" in ids


def test_live_models_copilot_settings_map_is_not_a_pin(monkeypatch):
    """Copilot's models mapping is per-model settings, so it must not clamp the
    live catalog, and sentinel metadata must not leak (#7406 fix 1)."""
    cfg = {
        "model": {"provider": "copilot"},
        "providers": {
            "copilot": {
                "models": {
                    "gpt-5": {"reasoning_effort": "high"},
                    "__explicit_model_allowlist__": True,
                },
            }
        },
    }
    live = ["gpt-5", "gpt-5-mini", "claude-opus-4.7"]
    ids = _live_ids(monkeypatch, cfg, {"copilot": live}, "copilot")
    assert set(ids) == set(live)
    assert "__explicit_model_allowlist__" not in ids


# ── Aliased config keys must still hit the pin policy (#7406 alias gap) ─────
#
# ``/api/models/live`` resolves the requested provider id to its canonical slug
# (``z-ai`` -> ``zai``, ``google`` -> ``gemini``) but config.yaml stores the
# entry under the RAW key the user wrote.  The old lookup keyed straight off the
# canonical slug, missed the entry, skipped the discovered-vs-pinned block, and
# returned the full live catalog -- bypassing a genuine pin.  These tests must
# NOT stub ``_resolve_provider_alias`` to identity (which is what hid the bug).


def test_live_models_aliased_config_key_genuine_pin_restricts(monkeypatch):
    """The exact P1: ``providers: {"z-ai": ...}`` queried as canonical ``zai``
    must apply the user's allowlist, not return the whole live catalog."""
    cfg = {
        "model": {"provider": "zai"},
        "providers": {
            "z-ai": {"models": {"glm-4": {}, "glm-4-air": {}}},
        },
    }
    ids = _live_ids(
        monkeypatch,
        cfg,
        {"zai": ["glm-4", "glm-4-air", "glm-4-unpinned"]},
        "zai",
        stub_alias=False,
    )
    assert set(ids) == {"glm-4", "glm-4-air"}
    assert "glm-4-unpinned" not in ids


def test_live_models_aliased_config_key_discovered_is_live_authoritative(monkeypatch):
    """The alias fix must not over-correct: a discovered aliased entry keeps the
    live catalog authoritative."""
    cfg = {
        "model": {"provider": "zai"},
        "providers": {
            "z-ai": {
                "models_discovered": True,
                "models": {"glm-4": {}},
            },
        },
    }
    ids = _live_ids(
        monkeypatch,
        cfg,
        {"zai": ["glm-4", "glm-4-discovered"]},
        "zai",
        stub_alias=False,
    )
    assert set(ids) == {"glm-4", "glm-4-discovered"}


def test_live_models_mixed_case_config_key_pin_restricts(monkeypatch):
    """A mixed-case raw key (``CLIPpoxy``) must resolve to its config entry."""
    cfg = {
        "model": {"provider": "clippoxy"},
        "providers": {
            "CLIPpoxy": {"models": {"clip-model-a": {}}},
        },
    }
    ids = _live_ids(
        monkeypatch,
        cfg,
        {"clippoxy": ["clip-model-a", "clip-model-b"]},
        "clippoxy",
        stub_alias=False,
    )
    assert set(ids) == {"clip-model-a"}


def test_live_models_underscore_config_key_pin_restricts(monkeypatch):
    """An underscore raw key (``opencode_go``) must resolve to its config entry
    when queried in the canonical hyphenated form."""
    cfg = {
        "model": {"provider": "opencode-go"},
        "providers": {
            "opencode_go": {"models": {"oc-model-a": {}}},
        },
    }
    ids = _live_ids(
        monkeypatch,
        cfg,
        {"opencode-go": ["oc-model-a", "oc-model-b"]},
        "opencode-go",
        stub_alias=False,
    )
    assert set(ids) == {"oc-model-a"}


def test_resolve_raw_provider_key_maps_aliases_and_case():
    """Direct unit coverage for the shared raw-key resolver."""
    from api import config

    providers = {
        "z-ai": {"models": {"glm-4": {}}},
        "google": {"models": {"gemini-2.5-pro": {}}},
        "CLIPpoxy": {"models": {"clip-model-a": {}}},
        "opencode_go": {"models": {"oc-model-a": {}}},
        "openai": {"models": {"gpt-4o": {}}},
    }
    resolver = config._resolve_raw_provider_key
    assert resolver("zai", providers) == "z-ai"
    assert resolver("z-ai", providers) == "z-ai"
    assert resolver("gemini", providers) == "google"
    assert resolver("CLIPpoxy", providers) == "CLIPpoxy"
    assert resolver("clippoxy", providers) == "CLIPpoxy"
    assert resolver("opencode-go", providers) == "opencode_go"
    assert resolver("openai", providers) == "openai"
    assert resolver("mystery", providers) == "mystery"
    assert resolver("mystery", None) == "mystery"

    assert config._get_provider_cfg_for_id("zai", providers) == providers["z-ai"]
    assert config._get_provider_cfg_for_id("mystery", providers) == {}


def test_provider_api_key_resolves_aliased_config_key(monkeypatch):
    """``api.providers`` credential resolution must find an aliased entry."""
    from api import providers as prov

    monkeypatch.setattr(prov, "_provider_env_var_for", lambda _pid: None)
    monkeypatch.setattr(prov, "_pool_entry_payloads", lambda _pid: [])
    monkeypatch.setattr(
        prov,
        "get_config",
        lambda: {"providers": {"z-ai": {"api_key": "sk-zai-alias-test"}}},
    )
    assert prov._get_provider_api_key("zai") == "sk-zai-alias-test"


def test_onboarding_provider_key_present_resolves_aliased_config_key():
    """Onboarding readiness must not report an aliased provider as
    unconfigured.  Same class as the /api/models/live alias gap: the setup id
    is canonical (``zai``) while config.yaml keys the entry ``z-ai``."""
    from api.onboarding import _provider_api_key_present

    aliased = {"providers": {"z-ai": {"api_key": "sk-test-123"}}}
    assert _provider_api_key_present("zai", aliased, {}) is True
    # Control: an entry the user genuinely has not configured stays False.
    assert _provider_api_key_present("zai", {"providers": {"openai": {"api_key": "x"}}}, {}) is False


# ── Settings provider list (review #7406, fix 2) ───────────────────────────


def _providers_module_setup(monkeypatch):
    import sys
    import types

    from api import providers as prov

    fake_pkg = types.ModuleType("hermes_cli")
    fake_pkg.__path__ = []
    fake_models = types.ModuleType("hermes_cli.models")
    fake_models.list_available_providers = lambda: []
    fake_models.provider_model_ids = lambda _pid: []
    fake_auth = types.ModuleType("hermes_cli.auth")
    fake_auth.get_auth_status = lambda _pid: {}
    monkeypatch.setitem(sys.modules, "hermes_cli", fake_pkg)
    monkeypatch.setitem(sys.modules, "hermes_cli.models", fake_models)
    monkeypatch.setitem(sys.modules, "hermes_cli.auth", fake_auth)

    monkeypatch.setattr(prov, "_PROVIDER_DISPLAY", {"anthropic": "Anthropic"})
    monkeypatch.setattr(
        prov,
        "_PROVIDER_MODELS",
        {"anthropic": [{"id": "claude-static", "label": "Claude Static"}]},
    )
    monkeypatch.setattr(prov, "_OAUTH_PROVIDERS", frozenset())
    monkeypatch.setattr(prov, "plugin_model_provider_ids", lambda: set())
    monkeypatch.setattr(prov, "_provider_has_key", lambda _pid: False)
    if hasattr(prov, "invalidate_providers_cache"):
        prov.invalidate_providers_cache()
    return prov


def test_provider_list_filters_sentinel_keys(monkeypatch):
    """The Settings provider card must not show discovery sentinels as model
    tags for either discovery shape (#7406 fix 2)."""
    from api import providers as prov

    _providers_module_setup(monkeypatch)
    monkeypatch.setattr(
        prov,
        "get_config",
        lambda: {
            "model": {"provider": "anthropic"},
            "providers": {
                "anthropic": {
                    "models_discovered": True,
                    "models": {
                        "__discovered_model_catalog__": True,
                        "__explicit_model_allowlist__": True,
                        "claude-real": {},
                    },
                }
            },
        },
    )
    try:
        result = prov.get_providers()
        anthropic = next(p for p in result["providers"] if p["id"] == "anthropic")
        ids = [m["id"] for m in anthropic["models"]]
        assert "__discovered_model_catalog__" not in ids
        assert "__explicit_model_allowlist__" not in ids
        assert "claude-real" in ids
        assert "claude-static" in ids
    finally:
        if hasattr(prov, "invalidate_providers_cache"):
            prov.invalidate_providers_cache()


# ── Live-rebuild merge-on-probe-failure (review #7406, fix 3) ──────────────


def test_live_rebuild_discovered_probe_failure_merges_persisted_ids(monkeypatch, tmp_path):
    """A discovered-only persisted id must survive a live probe failure: the
    broader static catalog is retained and the persisted ids are MERGED in
    (#7406 fix 3 regression)."""
    live_models = []  # probe failed / returned nothing
    cfg = {
        "model": {"default": "claude-sonnet-4.6", "provider": "anthropic"},
        "providers": {
            "anthropic": {
                "name": "Anthropic",
                "api_key": "sk-ant-test",
                "models_discovered": True,
                "models": {
                    "claude-sonnet-4.6": {"context_length": 200000},
                    "account-only-model": {},
                },
            }
        },
    }
    config = _setup(monkeypatch, tmp_path, cfg, live_models)

    payload = config.get_available_models(force_refresh=True)
    group = _provider_group(payload, "anthropic")
    ids = _ids_from_group(group)

    assert "account-only-model" in ids, f"discovered-only id dropped: {ids}"
    assert "claude-opus-4.7" in ids, f"static catalog was replaced, not merged: {ids}"


# ── Custom-provider discovery probe (review #7406, fix 4) ──────────────────


def _patch_custom_endpoint_urlopen(monkeypatch, config, payload, calls):
    import json

    class _FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def read(self):
            return json.dumps(payload).encode("utf-8")

    def _fake_urlopen(req, timeout=None):
        calls.append(getattr(req, "full_url", str(req)))
        return _FakeResponse()

    monkeypatch.setattr(config.urllib.request, "urlopen", _fake_urlopen)


def _bare_id(model_id):
    """Strip the ``@provider_id:`` routing prefix the picker adds when the
    custom provider is not the active provider."""
    text = str(model_id or "")
    if text.startswith("@") and ":" in text:
        return text.rsplit(":", 1)[1]
    return text


def test_discovered_custom_provider_still_probes_live(monkeypatch, tmp_path):
    """A custom_providers[] entry carrying a discovery marker is metadata, not
    an allowlist, so it must still probe /v1/models (#7406 fix 4)."""
    calls = []
    cfg = {
        "model": {"default": "claude-sonnet-4.6", "provider": "anthropic"},
        "providers": {"anthropic": {"api_key": "sk-ant-test"}},
        "custom_providers": [
            {
                "name": "Acme",
                "base_url": "http://127.0.0.1:9999/v1",
                "api_key": "sk-acme",
                "models_discovered": True,
                "models": {"acme-cached": {}},
            }
        ],
    }
    config = _setup(monkeypatch, tmp_path, cfg, [])
    _patch_custom_endpoint_urlopen(
        monkeypatch,
        config,
        {"data": [{"id": "acme-live-1"}, {"id": "acme-live-2"}]},
        calls,
    )

    payload = config.get_available_models(force_refresh=True)
    group = _provider_group(payload, "custom:acme")
    ids = [_bare_id(mid) for mid in _ids_from_group(group)]

    assert calls, "discovered custom provider did not probe /v1/models"
    assert "acme-live-1" in ids
    assert "acme-live-2" in ids
    assert "acme-cached" in ids


def test_pinned_custom_provider_skips_live_probe(monkeypatch, tmp_path):
    """Control: a genuine custom-provider pin stays an allowlist and does not
    probe /v1/models (#7404 user-pin behavior preserved)."""
    calls = []
    cfg = {
        "model": {"default": "claude-sonnet-4.6", "provider": "anthropic"},
        "providers": {"anthropic": {"api_key": "sk-ant-test"}},
        "custom_providers": [
            {
                "name": "Acme",
                "base_url": "http://127.0.0.1:9999/v1",
                "api_key": "sk-acme",
                "models": {"acme-pinned": {}},
            }
        ],
    }
    config = _setup(monkeypatch, tmp_path, cfg, [])
    _patch_custom_endpoint_urlopen(
        monkeypatch,
        config,
        {"data": [{"id": "should-not-appear"}]},
        calls,
    )

    payload = config.get_available_models(force_refresh=True)
    group = _provider_group(payload, "custom:acme")
    ids = [_bare_id(mid) for mid in _ids_from_group(group)]

    assert not calls, f"pinned custom provider probed live: {calls}"
    assert "acme-pinned" in ids
    assert "should-not-appear" not in ids


# ── Named custom_providers[] pins + cache authority (#7406 review) ──────────
#
# The live route used to merge every ID the custom endpoint returned and only
# clamped against ``providers{}`` pins, so a pin stored in a matching
# ``custom_providers[]`` entry was silently widened.  These drive the REAL
# handler (``routes._handle_live_models``), not the helper functions.


def _live_ids_via_handler(monkeypatch, cfg, live_models_by_provider, query_provider,
                          *, stub_alias=True):
    from urllib.parse import urlparse

    routes, _ = _live_models_setup(
        monkeypatch, cfg, live_models_by_provider, stub_alias=stub_alias
    )
    parsed = urlparse(f"/api/models/live?provider={query_provider}")
    payload = routes._handle_live_models(object(), parsed)
    return routes, [m["id"] for m in payload.get("models", [])]


def test_live_models_custom_provider_pin_restricts_live_catalog(monkeypatch, tmp_path):
    """A pin in a matching ``custom_providers[]`` entry must restrict the
    returned IDs exactly like a ``providers.<id>`` pin: an unpinned live ID must
    not leak (maintainer's ``custom:acme`` repro)."""
    cfg = {
        "model": {"provider": "custom:acme"},
        "custom_providers": [
            {
                "name": "Acme",
                "base_url": "http://127.0.0.1:9999/v1",
                "api_key": "sk-acme",
                "models": {"pinned-only": {}},
            }
        ],
    }
    calls = []
    routes, config = _live_models_setup(monkeypatch, cfg, {})
    _patch_custom_endpoint_urlopen(
        monkeypatch,
        config,
        {"data": [{"id": "pinned-only"}, {"id": "unpinned-live"}]},
        calls,
    )
    from urllib.parse import urlparse

    payload = routes._handle_live_models(
        object(), urlparse("/api/models/live?provider=custom:acme")
    )
    ids = [m["id"] for m in payload.get("models", [])]
    assert calls, "expected the custom endpoint probe to run"
    assert set(ids) == {"pinned-only"}, ids
    assert "unpinned-live" not in ids


def test_live_models_custom_provider_discovery_stays_live_authoritative(monkeypatch, tmp_path):
    """Over-correction guard: a discovery marker on a ``custom_providers[]``
    entry makes its ``models`` mapping metadata, so the live catalog stays
    authoritative (persisted IDs are a probe-failure fallback only)."""
    cfg = {
        "model": {"provider": "custom:acme"},
        "custom_providers": [
            {
                "name": "Acme",
                "base_url": "http://127.0.0.1:9999/v1",
                "api_key": "sk-acme",
                "models_discovered": True,
                "models": {"acme-cached": {}},
            }
        ],
    }
    calls = []
    routes, config = _live_models_setup(monkeypatch, cfg, {})
    _patch_custom_endpoint_urlopen(
        monkeypatch,
        config,
        {"data": [{"id": "acme-live-1"}, {"id": "acme-live-2"}]},
        calls,
    )
    from urllib.parse import urlparse

    payload = routes._handle_live_models(
        object(), urlparse("/api/models/live?provider=custom:acme")
    )
    ids = [m["id"] for m in payload.get("models", [])]
    assert calls, "expected the custom endpoint probe to run"
    assert {"acme-live-1", "acme-live-2"}.issubset(set(ids)), ids
    assert "acme-cached" in ids


def test_live_models_cache_invalidated_when_discovered_flips_to_pin(monkeypatch):
    """A catalog cached while the provider was discovered must not be replayed
    after the same profile+provider is changed to a strict pin.  The policy
    fingerprint in the cache key must force a re-evaluation."""
    cfg = {
        "model": {"provider": "openai"},
        "providers": {
            "openai": {
                "models_discovered": True,
                "models": {"gpt-4o": {}},
            }
        },
    }
    routes, _ = _live_models_setup(
        monkeypatch, cfg, {"openai": ["gpt-4o", "gpt-5-discovered"]}
    )
    from urllib.parse import urlparse

    parsed = urlparse("/api/models/live?provider=openai")
    first = routes._handle_live_models(object(), parsed)
    first_ids = [m["id"] for m in first["models"]]
    assert "gpt-5-discovered" in first_ids

    # Same profile + provider, but the entry is now a genuine pin.
    cfg["providers"]["openai"] = {"models": {"gpt-4o": {}}}
    second = routes._handle_live_models(object(), parsed)
    second_ids = [m["id"] for m in second["models"]]
    assert set(second_ids) == {"gpt-4o"}, second_ids
    assert "gpt-5-discovered" not in second_ids


def test_live_models_cache_scoped_by_profile(monkeypatch):
    """A catalog cached for profile A must not be served for profile B."""
    cfg = {
        "model": {"provider": "openai"},
        "providers": {
            "openai": {
                "models_discovered": True,
                "models": {"gpt-4o": {}},
            }
        },
    }
    live = {"openai": ["gpt-4o", "gpt-5-profile-a"]}
    routes, _ = _live_models_setup(monkeypatch, cfg, live)
    monkeypatch.setattr(routes, "_active_profile_for_live_models_cache", lambda: "profile-a")
    from urllib.parse import urlparse

    parsed = urlparse("/api/models/live?provider=openai")
    ids_a = [m["id"] for m in routes._handle_live_models(object(), parsed)["models"]]
    assert "gpt-5-profile-a" in ids_a

    live["openai"] = ["gpt-4o", "gpt-5-profile-b"]
    monkeypatch.setattr(routes, "_active_profile_for_live_models_cache", lambda: "profile-b")
    ids_b = [m["id"] for m in routes._handle_live_models(object(), parsed)["models"]]
    assert "gpt-5-profile-b" in ids_b
    assert "gpt-5-profile-a" not in ids_b


def test_live_models_discovered_control_still_broad(monkeypatch):
    """Control: the unchanged ``providers{}`` discovered path still returns the
    broad live catalog."""
    cfg = {
        "model": {"provider": "openai"},
        "providers": {
            "openai": {
                "models_discovered": True,
                "models": {"gpt-4o": {}},
            }
        },
    }
    _, ids = _live_ids_via_handler(
        monkeypatch, cfg, {"openai": ["gpt-4o", "gpt-5-discovered"]}, "openai"
    )
    assert {"gpt-4o", "gpt-5-discovered"}.issubset(set(ids)), ids
