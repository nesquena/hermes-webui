import pytest
from api import config
from api.config import (
    AmbiguousCustomProviderError,
    _custom_provider_slug_from_name,
    _named_custom_provider_slug_for_provider,
    _unique_custom_provider_entry,
    _custom_provider_slug_key,
    resolve_model_provider,
)


def test_non_ascii_custom_provider_slug_hash_fallback():
    # Pure non-ASCII name without provider_key derives stable hash, not host/port
    slug1 = _custom_provider_slug_from_name("晨光鑫遇专用")
    slug2 = _custom_provider_slug_from_name("晨光鑫遇专用")
    assert slug1.startswith("custom:named-")
    assert slug1 == slug2
    assert len(slug1) == len("custom:named-") + 8


def test_non_ascii_custom_provider_slug_provider_key_fallback():
    entry = {
        "name": "晨光鑫遇专用",
        "provider_key": "cgxy-cpa",
        "base_url": "http://127.0.0.1:8317/v1",
    }
    slug = _custom_provider_slug_from_name("晨光鑫遇专用", entry)
    assert slug == "custom:cgxy-cpa"

    # Also test dict-as-first-argument form
    slug_dict = _custom_provider_slug_from_name(entry)
    assert slug_dict == "custom:cgxy-cpa"


def test_same_host_different_path_and_same_endpoint_siblings():
    """Different CJK names sharing same host or same endpoint do not collapse onto one slug."""
    entry_a = {
        "name": "晨光账户甲",
        "base_url": "https://relay.example/account-a/v1",
        "api_key": "secret-a",
        "model": "gpt-4o",
    }
    entry_b = {
        "name": "晨光账户乙",
        "base_url": "https://relay.example/account-b/v1",
        "api_key": "secret-b",
        "model": "gpt-4o",
    }
    entry_c = {
        "name": "晨光账户丙",
        "base_url": "https://relay.example/account-a/v1",  # exact same endpoint as A
        "api_key": "secret-c",
        "model": "gpt-4o",
    }

    slug_a = _custom_provider_slug_from_name(entry_a.get("name"), entry_a)
    slug_b = _custom_provider_slug_from_name(entry_b.get("name"), entry_b)
    slug_c = _custom_provider_slug_from_name(entry_c.get("name"), entry_c)

    # Distinct stable slugs derived from name, NOT from relay.example
    assert slug_a.startswith("custom:named-")
    assert slug_b.startswith("custom:named-")
    assert slug_c.startswith("custom:named-")
    assert slug_a != slug_b
    assert slug_a != slug_c
    assert slug_b != slug_c

    custom_providers = [entry_a, entry_b, entry_c]
    # Each entry resolves to its own exact owning row, base_url, and credential
    res_a = _unique_custom_provider_entry(custom_providers, slug_a.removeprefix("custom:"))
    assert res_a is entry_a
    assert res_a["api_key"] == "secret-a"
    assert res_a["base_url"] == "https://relay.example/account-a/v1"

    res_b = _unique_custom_provider_entry(custom_providers, slug_b.removeprefix("custom:"))
    assert res_b is entry_b
    assert res_b["api_key"] == "secret-b"
    assert res_b["base_url"] == "https://relay.example/account-b/v1"

    res_c = _unique_custom_provider_entry(custom_providers, slug_c.removeprefix("custom:"))
    assert res_c is entry_c
    assert res_c["api_key"] == "secret-c"


def test_collision_refusal_for_identical_names():
    """Identical names with same derived slug trigger AmbiguousCustomProviderError."""
    entry1 = {
        "name": "晨光重名",
        "base_url": "https://relay1.example/v1",
        "api_key": "key-1",
    }
    entry2 = {
        "name": "晨光重名",
        "base_url": "https://relay2.example/v1",
        "api_key": "key-2",
    }
    slug = _custom_provider_slug_from_name(entry1["name"], entry1)
    slug_key = slug.removeprefix("custom:")

    with pytest.raises(AmbiguousCustomProviderError) as exc_info:
        _unique_custom_provider_entry([entry1, entry2], slug_key)
    assert "all normalize to the same provider slug" in str(exc_info.value)


def test_explicit_list_isolation_from_ambient_cfg(monkeypatch):
    """Explicit-list lookup does not depend on or cross-match against ambient cfg."""
    ambient_entry = {
        "name": "晨光隔离",
        "provider_key": "ambient-alpha",
        "base_url": "https://ambient.example/v1",
    }
    monkeypatch.setattr(config, "cfg", {"custom_providers": [ambient_entry]})

    explicit_list = [
        {
            "name": "晨光隔离",
            "provider_key": "explicit-beta",
            "base_url": "https://explicit.example/v1",
            "api_key": "beta-key",
        }
    ]

    # Explicit list must match its own key 'explicit-beta'
    matched = _unique_custom_provider_entry(explicit_list, "explicit-beta")
    assert matched is not None
    assert matched["provider_key"] == "explicit-beta"
    assert matched["api_key"] == "beta-key"

    # Explicit list must NOT match ambient 'ambient-alpha'
    assert _unique_custom_provider_entry(explicit_list, "ambient-alpha") is None


def test_empty_or_ascii_custom_provider_slug():
    assert _custom_provider_slug_from_name("") == ""
    assert _custom_provider_slug_from_name("   ") == ""
    assert _custom_provider_slug_from_name(None) == ""
    assert _custom_provider_slug_from_name("custom:already") == "custom:already"
    assert (
        _custom_provider_slug_from_name("Local (127.0.0.1:15721)")
        == "custom:local-127.0.0.1-15721"
    )


def test_catalog_routing_and_resolve_model_provider(monkeypatch):
    """Real catalog option IDs (@custom:<slug>:<model>) are consumed by resolve_model_provider."""
    cjk_entry_pk = {
        "name": "晨光专用",
        "provider_key": "cgxy-pk",
        "base_url": "https://api.cgxy.cn/v1",
        "model": "deepseek-v3",
        "models": ["deepseek-v3", "deepseek-r1"],
    }
    cjk_entry_hash = {
        "name": "无键模型服务",
        "base_url": "https://api.nokey.cn/v1",
        "model": "qwen-max",
        "models": ["qwen-max"],
    }
    ascii_entry = {
        "name": "Local LLM",
        "base_url": "http://127.0.0.1:8000/v1",
        "model": "llama-3",
        "models": ["llama-3"],
    }

    test_cfg = {
        "model": "default-model",
        "provider": "openrouter",
        "custom_providers": [cjk_entry_pk, cjk_entry_hash, ascii_entry],
    }
    monkeypatch.setattr(config, "cfg", test_cfg)

    slug_pk = _custom_provider_slug_from_name(cjk_entry_pk["name"], cjk_entry_pk)
    slug_hash = _custom_provider_slug_from_name(cjk_entry_hash["name"], cjk_entry_hash)
    slug_ascii = _custom_provider_slug_from_name(ascii_entry["name"], ascii_entry)

    assert slug_pk == "custom:cgxy-pk"
    assert slug_hash.startswith("custom:named-")
    assert slug_ascii == "custom:local-llm"

    # 1. Resolve via explicit dropdown hint @<slug>:<model>
    m1, p1, b1 = resolve_model_provider(f"@{slug_pk}:deepseek-v3")
    assert m1 == "deepseek-v3"
    assert p1 == slug_pk
    assert b1 == "https://api.cgxy.cn/v1"

    m2, p2, b2 = resolve_model_provider(f"@{slug_hash}:qwen-max")
    assert m2 == "qwen-max"
    assert p2 == slug_hash
    assert b2 == "https://api.nokey.cn/v1"

    m3, p3, b3 = resolve_model_provider(f"@{slug_ascii}:llama-3")
    assert m3 == "llama-3"
    assert p3 == slug_ascii
    assert b3 == "http://127.0.0.1:8000/v1"

    # 2. Final connection-record selection via _unique_custom_provider_entry
    entry1 = _unique_custom_provider_entry(test_cfg["custom_providers"], slug_pk.removeprefix("custom:"))
    assert entry1 is cjk_entry_pk

    entry2 = _unique_custom_provider_entry(test_cfg["custom_providers"], slug_hash.removeprefix("custom:"))
    assert entry2 is cjk_entry_hash

    entry3 = _unique_custom_provider_entry(test_cfg["custom_providers"], slug_ascii.removeprefix("custom:"))
    assert entry3 is ascii_entry


def test_active_keyed_cjk_shared_model_routing_and_connection(monkeypatch):
    """Network-free catalog option resolution chooses active keyed CJK over earlier ASCII sibling."""
    from api.config import (
        _static_models_catalog_without_live_probes,
        resolve_custom_provider_connection,
    )

    ascii_provider = {
        "name": "ascii-first",
        "base_url": "https://ascii.example/v1",
        "api_key": "ascii-fixture-key",
        "model": "shared-model-id",
        "models": ["shared-model-id"],
    }
    cjk_keyed_provider = {
        "name": "晨光账户乙",
        "provider_key": "account-b",
        "base_url": "https://cjk-keyed.example/v1",
        "api_key": "cjk-keyed-fixture-key",
        "model": "shared-model-id",
        "models": ["shared-model-id"],
    }
    cjk_hash_provider = {
        "name": "晨光哈希丙",
        "base_url": "https://cjk-hash.example/v1",
        "api_key": "cjk-hash-fixture-key",
        "model": "hash-only-model",
        "models": ["hash-only-model"],
    }

    test_cfg = {
        "model": {
            "provider": "custom:account-b",
            "default": "shared-model-id",
        },
        "custom_providers": [ascii_provider, cjk_keyed_provider, cjk_hash_provider],
    }
    monkeypatch.setattr(config, "cfg", test_cfg)
    monkeypatch.setattr(config, "get_config", lambda: test_cfg)

    # 1. Obtain options from the actual network-free catalog
    catalog = _static_models_catalog_without_live_probes()
    groups = {g["provider_id"]: g for g in catalog.get("groups", [])}

    # Verify catalog groups exist for all three providers
    assert "custom:account-b" in groups
    assert "custom:ascii-first" in groups
    hash_group_id = next(gid for gid in groups if gid.startswith("custom:named-"))
    assert hash_group_id in groups

    # For the active provider, catalog models may be bare or prefixed
    active_models = [m["id"] for m in groups["custom:account-b"]["models"]]
    assert any("shared-model-id" in mid for mid in active_models)

    ascii_models = [m["id"] for m in groups["custom:ascii-first"]["models"]]
    assert any("shared-model-id" in mid for mid in ascii_models)

    hash_models = [m["id"] for m in groups[hash_group_id]["models"]]
    assert any("hash-only-model" in mid for mid in hash_models)

    # 2. Resolve the active option (@custom:account-b:shared-model-id or shared-model-id)
    # Even though ascii_first declares shared-model-id earlier in config, active custom provider wins:
    active_mid = next(mid for mid in active_models if "shared-model-id" in mid)
    resolved_model, resolved_provider, resolved_base = resolve_model_provider(active_mid)
    assert resolved_provider == "custom:account-b"
    assert resolved_base == "https://cjk-keyed.example/v1"

    # Connection resolution selects the authoritative CJK row and its fixture credential
    conn_key, conn_base = resolve_custom_provider_connection(resolved_provider)
    assert conn_key == "cjk-keyed-fixture-key"
    assert conn_base == "https://cjk-keyed.example/v1"

    # 3. Hash-only CJK control passes and resolves to its own stable named slug
    hash_mid = next(mid for mid in hash_models if "hash-only-model" in mid)
    h_model, h_provider, h_base = resolve_model_provider(hash_mid)
    assert h_provider == hash_group_id
    assert h_base == "https://cjk-hash.example/v1"

    h_key, h_conn_base = resolve_custom_provider_connection(h_provider)
    assert h_key == "cjk-hash-fixture-key"
    assert h_conn_base == "https://cjk-hash.example/v1"

    # 4. ASCII option resolves to earlier ASCII provider
    ascii_mid = next(mid for mid in ascii_models if "shared-model-id" in mid)
    a_model, a_provider, a_base = resolve_model_provider(ascii_mid)
    assert a_provider == "custom:ascii-first"
    assert a_base == "https://ascii.example/v1"

    a_key, a_conn_base = resolve_custom_provider_connection(a_provider)
    assert a_key == "ascii-fixture-key"
    assert a_conn_base == "https://ascii.example/v1"


def test_providers_card_and_catalog_identity_agreement(monkeypatch):
    """Providers card and catalog use identical slug IDs for keyed and hash CJK providers."""
    import api.providers as providers_mod
    from api.config import _static_models_catalog_without_live_probes

    cjk_keyed = {
        "name": "晨光卡片甲",
        "provider_key": "card-account-a",
        "base_url": "https://card-a.example/v1",
        "api_key": "sec-a",
        "model": "model-a",
        "models": ["model-a"],
    }
    cjk_hash = {
        "name": "晨光卡片乙",
        "base_url": "https://card-b.example/v1",
        "api_key": "sec-b",
        "model": "model-b",
        "models": ["model-b"],
    }
    ascii_card = {
        "name": "ascii-card",
        "base_url": "https://card-c.example/v1",
        "api_key": "sec-c",
        "model": "model-c",
        "models": ["model-c"],
    }

    test_cfg = {
        "provider": "openrouter",
        "custom_providers": [cjk_keyed, cjk_hash, ascii_card],
    }
    monkeypatch.setattr(config, "cfg", test_cfg)
    monkeypatch.setattr(config, "get_config", lambda: test_cfg)
    monkeypatch.setattr(providers_mod, "get_config", lambda: test_cfg)

    # Catalog provider IDs
    catalog = _static_models_catalog_without_live_probes()
    catalog_pids = {g["provider_id"] for g in catalog.get("groups", [])}
    assert "custom:card-account-a" in catalog_pids
    assert "custom:ascii-card" in catalog_pids
    hash_pid = next(pid for pid in catalog_pids if pid.startswith("custom:named-"))

    # Providers card IDs
    card_data = providers_mod.get_providers()
    card_pids = {p["id"] for p in card_data.get("providers", [])}

    # Verify identity agreement between Providers cards and Catalog
    assert "custom:card-account-a" in card_pids
    assert hash_pid in card_pids
    assert "custom:ascii-card" in card_pids

    # Key status matching verifies candidate record
    assert providers_mod._custom_provider_name_matches("custom:card-account-a", cjk_keyed["name"], cjk_keyed)
    assert providers_mod._custom_provider_name_matches("card-account-a", cjk_keyed["name"], cjk_keyed)
    assert providers_mod._custom_provider_name_matches(hash_pid, cjk_hash["name"], cjk_hash)


def test_session_bare_model_repair_with_keyed_cjk():
    """_repair_bare_custom_provider_model repairs bare models for keyed and hash CJK providers."""
    from api.routes import _repair_bare_custom_provider_model

    cjk_keyed = {
        "name": "晨光修复甲",
        "provider_key": "account-repair",
        "base_url": "https://repair.example/v1",
        "model": "vendor/deepseek-v3",
        "models": ["vendor/deepseek-v3", "vendor/qwen-2.5"],
    }
    cjk_hash = {
        "name": "晨光修复乙",
        "base_url": "https://repair2.example/v1",
        "model": "relay/chat-hash",
        "models": ["relay/chat-hash"],
    }

    test_cfg = {
        "custom_providers": [cjk_keyed, cjk_hash],
    }

    # Bare model "deepseek-v3" on provider "custom:account-repair" repairs to "vendor/deepseek-v3"
    repaired_keyed = _repair_bare_custom_provider_model(
        "deepseek-v3",
        "custom:account-repair",
        config_obj=test_cfg,
    )
    assert repaired_keyed == "vendor/deepseek-v3"

    # Bare model on hash-only CJK provider
    hash_slug = _custom_provider_slug_from_name(cjk_hash["name"], cjk_hash)
    repaired_hash = _repair_bare_custom_provider_model(
        "chat-hash",
        hash_slug,
        config_obj=test_cfg,
    )
    assert repaired_hash == "relay/chat-hash"
