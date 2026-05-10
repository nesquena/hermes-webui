"""Regression tests for credential_pool provider detection in /api/models."""

import json
import sys
import types

import api.config as config
import api.profiles as profiles

_AMBIENT_SOURCES = {"gh_cli", "gh auth token"}


def _install_fake_hermes_cli(
    monkeypatch, *, with_load_pool: bool = False, pool_data: dict | None = None
):
    """Stub hermes_cli modules so tests are deterministic and offline.

    When *with_load_pool* is True, also stubs hermes_cli.credential_pool with a
    suppression-aware load_pool() implementation that mirrors upstream behaviour:
    entries whose source/label/key_source signals ambient gh-cli auth are filtered out.
    """
    fake_pkg = types.ModuleType("hermes_cli")
    fake_pkg.__path__ = []

    fake_models = types.ModuleType("hermes_cli.models")
    fake_models.list_available_providers = lambda: []
    fake_models.provider_model_ids = lambda pid: (
        ["gpt-oss:20b", "qwen3:30b-a3b"] if pid == "ollama-cloud" else []
    )

    fake_auth = types.ModuleType("hermes_cli.auth")
    fake_auth.get_auth_status = lambda _pid: {}

    monkeypatch.setitem(sys.modules, "hermes_cli", fake_pkg)
    monkeypatch.setitem(sys.modules, "hermes_cli.models", fake_models)
    monkeypatch.setitem(sys.modules, "hermes_cli.auth", fake_auth)

    # Always remove the real agent.credential_pool so get_available_models() takes
    # the ImportError fallback path and reads from the monkeypatched auth store,
    # not the live ~/.hermes/auth.json via the real venv module.
    monkeypatch.delitem(sys.modules, "agent.credential_pool", raising=False)
    monkeypatch.delitem(sys.modules, "agent", raising=False)

    if with_load_pool:
        _pool_data = pool_data or {}

        class _FakeEntry:
            """Minimal PooledCredential stand-in with attribute access (matching the real class)."""

            def __init__(self, d):
                self.source = d.get("source", "manual")
                self.label = d.get("label", "")
                self.key_source = d.get("key_source", "")
                self.id = d.get("id", "")

        class _FakePool:
            def __init__(self, entries_list):
                self._entries = entries_list

            def entries(self):
                return self._entries

        def _fake_load_pool(pid):
            # Return ALL entries without filtering — mirrors the real load_pool()
            # which does NOT suppress ambient gh-cli tokens on its own.
            # Ambient-source filtering is the webui's responsibility.
            raw = _pool_data.get(pid, [])
            return _FakePool([_FakeEntry(e) for e in raw])

        fake_cp = types.ModuleType("agent.credential_pool")
        fake_cp.load_pool = _fake_load_pool
        monkeypatch.setitem(sys.modules, "agent.credential_pool", fake_cp)


def _call_get_available_models(
    monkeypatch, tmp_path, auth_payload, *, with_load_pool: bool = False
):
    """Call get_available_models() with auth.json pinned to a temp Hermes home."""
    _install_fake_hermes_cli(
        monkeypatch,
        with_load_pool=with_load_pool,
        pool_data=auth_payload.get("credential_pool", {}),
    )

    (tmp_path / "auth.json").write_text(json.dumps(auth_payload), encoding="utf-8")
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)

    old_cfg = dict(config.cfg)
    old_mtime = config._cfg_mtime
    config.cfg.clear()
    config.cfg["model"] = {}
    try:
        # Pin mtime to avoid reload_config() clobbering our in-memory cfg patch.
        config._cfg_mtime = config.Path(config._get_config_path()).stat().st_mtime
    except Exception:
        config._cfg_mtime = 0.0

    config.invalidate_models_cache()
    try:
        return config.get_available_models()
    finally:
        config.cfg.clear()
        config.cfg.update(old_cfg)
        config._cfg_mtime = old_mtime
        config.invalidate_models_cache()


def _group_by_provider(result):
    return {g["provider"]: g["models"] for g in result.get("groups", [])}


def test_ollama_cloud_manual_credential_shows_group(monkeypatch, tmp_path):
    auth_payload = {
        "version": 1,
        "providers": {},
        "active_provider": "openai-codex",
        "credential_pool": {
            "ollama-cloud": [
                {
                    "id": "abc123",
                    "label": "ollama-manual",
                    "source": "manual",
                    "auth_type": "api_key",
                    "base_url": "https://ollama.com/v1",
                }
            ]
        },
    }

    result = _call_get_available_models(monkeypatch, tmp_path, auth_payload)
    groups = _group_by_provider(result)
    assert "Ollama Cloud" in groups, f"Expected Ollama Cloud in {list(groups)}"
    model_ids = [m["id"] for m in groups["Ollama Cloud"]]
    assert model_ids == ["@ollama-cloud:gpt-oss:20b", "@ollama-cloud:qwen3:30b-a3b"], (
        model_ids
    )


def test_copilot_gh_cli_only_credential_hidden(monkeypatch, tmp_path):
    auth_payload = {
        "version": 1,
        "providers": {},
        "active_provider": "openai-codex",
        "credential_pool": {
            "copilot": [
                {
                    "id": "def456",
                    "label": "gh auth token",
                    "source": "gh_cli",
                    "auth_type": "api_key",
                    "base_url": "https://api.githubcopilot.com",
                }
            ]
        },
    }

    result = _call_get_available_models(monkeypatch, tmp_path, auth_payload)
    groups = _group_by_provider(result)
    assert "GitHub Copilot" not in groups, (
        "GitHub Copilot should be hidden when only ambient gh auth token is present; "
        f"got {list(groups)}"
    )


def test_copilot_mixed_credential_pool_remains_visible(monkeypatch, tmp_path):
    auth_payload = {
        "version": 1,
        "providers": {},
        "active_provider": "openai-codex",
        "credential_pool": {
            "copilot": [
                {
                    "id": "def456",
                    "label": "gh auth token",
                    "source": "gh_cli",
                    "auth_type": "api_key",
                    "base_url": "https://api.githubcopilot.com",
                },
                {
                    "id": "ghi789",
                    "label": "explicit-copilot",
                    "source": "manual",
                    "auth_type": "api_key",
                    "base_url": "https://api.githubcopilot.com",
                },
            ]
        },
    }

    result = _call_get_available_models(monkeypatch, tmp_path, auth_payload)
    groups = _group_by_provider(result)
    assert "GitHub Copilot" in groups, f"Expected GitHub Copilot in {list(groups)}"
    model_ids = [m["id"] for m in groups["GitHub Copilot"]]
    assert "@copilot:gpt-5.4" in model_ids, model_ids
    assert "@copilot:claude-opus-4.6" in model_ids, model_ids


def test_copilot_empty_field_entries_are_treated_as_explicit(monkeypatch, tmp_path):
    auth_payload = {
        "version": 1,
        "providers": {},
        "active_provider": "openai-codex",
        "credential_pool": {
            "copilot": [
                {
                    "id": "jkl012",
                }
            ]
        },
    }

    result = _call_get_available_models(monkeypatch, tmp_path, auth_payload)
    groups = _group_by_provider(result)
    assert "GitHub Copilot" in groups, f"Expected GitHub Copilot in {list(groups)}"


def test_copilot_oauth_credential_is_visible(monkeypatch, tmp_path):
    auth_payload = {
        "version": 1,
        "providers": {},
        "active_provider": "openai-codex",
        "credential_pool": {
            "copilot": [
                {
                    "id": "mno345",
                    "label": "github-oauth",
                    "source": "oauth",
                    "auth_type": "oauth",
                    "base_url": "https://api.githubcopilot.com",
                }
            ]
        },
    }

    result = _call_get_available_models(monkeypatch, tmp_path, auth_payload)
    groups = _group_by_provider(result)
    assert "GitHub Copilot" in groups, f"Expected GitHub Copilot in {list(groups)}"


# --- load_pool path (suppression-aware) ---


def test_load_pool_copilot_ambient_only_remains_hidden(monkeypatch, tmp_path):
    """load_pool path: copilot with only ambient gh-cli entries is suppressed."""
    auth_payload = {
        "version": 1,
        "providers": {},
        "active_provider": "openai-codex",
        "credential_pool": {
            "copilot": [
                {
                    "id": "lp001",
                    "label": "gh auth token",
                    "source": "gh_cli",
                    "auth_type": "api_key",
                    "base_url": "https://api.githubcopilot.com",
                }
            ]
        },
    }

    result = _call_get_available_models(
        monkeypatch, tmp_path, auth_payload, with_load_pool=True
    )
    groups = _group_by_provider(result)
    assert "GitHub Copilot" not in groups, (
        "GitHub Copilot must be hidden when load_pool returns no usable entries; "
        f"got {list(groups)}"
    )


def test_load_pool_copilot_ambient_key_source_only_remains_hidden(
    monkeypatch, tmp_path
):
    """load_pool path: key_source-only ambient markers must also be suppressed."""
    auth_payload = {
        "version": 1,
        "providers": {},
        "active_provider": "openai-codex",
        "credential_pool": {
            "copilot": [
                {
                    "id": "lp001b",
                    "label": "copilot-token",
                    "source": "manual",
                    "key_source": "gh auth token",
                    "auth_type": "api_key",
                    "base_url": "https://api.githubcopilot.com",
                }
            ]
        },
    }

    result = _call_get_available_models(
        monkeypatch, tmp_path, auth_payload, with_load_pool=True
    )
    groups = _group_by_provider(result)
    assert "GitHub Copilot" not in groups, (
        "GitHub Copilot must stay hidden when load_pool entries only differ by key_source ambient markers; "
        f"got {list(groups)}"
    )


def test_load_pool_alias_provider_key_is_resolved(monkeypatch, tmp_path):
    """load_pool path: aliased pool keys should resolve to canonical provider ids."""
    auth_payload = {
        "version": 1,
        "providers": {},
        "active_provider": "openai-codex",
        "credential_pool": {
            "google": [
                {
                    "id": "gp001",
                    "label": "explicit-gemini",
                    "source": "manual",
                    "auth_type": "api_key",
                    "base_url": "https://generativelanguage.googleapis.com",
                }
            ]
        },
    }

    result = _call_get_available_models(
        monkeypatch, tmp_path, auth_payload, with_load_pool=True
    )
    groups = _group_by_provider(result)
    assert "Gemini" in groups, f"Expected Gemini in {list(groups)}"
    assert "Google" not in groups, (
        f"Aliased provider key should not render under raw alias name: {list(groups)}"
    )


def test_load_pool_explicit_credential_shows_provider(monkeypatch, tmp_path):
    """load_pool path: provider with at least one explicit entry is visible."""
    auth_payload = {
        "version": 1,
        "providers": {},
        "active_provider": "openai-codex",
        "credential_pool": {
            "copilot": [
                {
                    "id": "lp002",
                    "label": "gh auth token",
                    "source": "gh_cli",
                    "auth_type": "api_key",
                    "base_url": "https://api.githubcopilot.com",
                },
                {
                    "id": "lp003",
                    "label": "explicit-pat",
                    "source": "manual",
                    "auth_type": "api_key",
                    "base_url": "https://api.githubcopilot.com",
                },
            ]
        },
    }

    result = _call_get_available_models(
        monkeypatch, tmp_path, auth_payload, with_load_pool=True
    )
    groups = _group_by_provider(result)
    assert "GitHub Copilot" in groups, (
        f"GitHub Copilot must appear when load_pool has at least one usable entry; got {list(groups)}"
    )


# --- _apply_provider_prefix helper ---


def test_apply_provider_prefix_ollama_cloud_non_active():
    """Bare ollama-cloud model ids get @ollama-cloud: prefix when not active."""
    from api.config import _apply_provider_prefix

    raw = [
        {"id": "gpt-oss:20b", "label": "gpt-oss:20b"},
        {"id": "qwen3:30b-a3b", "label": "qwen3:30b-a3b"},
    ]
    result = _apply_provider_prefix(raw, "ollama-cloud", "openai-codex")
    ids = [m["id"] for m in result]
    assert ids == ["@ollama-cloud:gpt-oss:20b", "@ollama-cloud:qwen3:30b-a3b"], ids


def test_apply_provider_prefix_copilot_non_active():
    """Bare copilot model ids get @copilot: prefix when not active."""
    from api.config import _apply_provider_prefix

    raw = [
        {"id": "gpt-5.4", "label": "GPT-5.4"},
        {"id": "claude-opus-4.6", "label": "Claude Opus 4.6"},
    ]
    result = _apply_provider_prefix(raw, "copilot", "openai-codex")
    ids = [m["id"] for m in result]
    assert ids == ["@copilot:gpt-5.4", "@copilot:claude-opus-4.6"], ids


def test_apply_provider_prefix_no_double_prefix():
    """Already-prefixed or provider/model ids are not double-prefixed."""
    from api.config import _apply_provider_prefix

    raw = [
        {"id": "@copilot:gpt-5.4", "label": "already prefixed"},
        {"id": "openai/gpt-5.4", "label": "slash form"},
        {"id": "bare-model", "label": "bare"},
    ]
    result = _apply_provider_prefix(raw, "copilot", "openai-codex")
    ids = [m["id"] for m in result]
    assert ids == ["@copilot:gpt-5.4", "openai/gpt-5.4", "@copilot:bare-model"], ids


def test_apply_provider_prefix_active_provider_no_prefix():
    """No prefix is added when the provider is already the active one."""
    from api.config import _apply_provider_prefix

    raw = [{"id": "gpt-5.4", "label": "GPT-5.4"}]
    result = _apply_provider_prefix(raw, "openai-codex", "openai-codex")
    ids = [m["id"] for m in result]
    assert ids == ["gpt-5.4"], ids


def test_copilot_mixed_pool_prefixed_models(monkeypatch, tmp_path):
    """Copilot with mixed pool and non-active provider has @copilot: prefixed model ids."""
    auth_payload = {
        "version": 1,
        "providers": {},
        "active_provider": "openai-codex",
        "credential_pool": {
            "copilot": [
                {
                    "id": "lp010",
                    "label": "explicit-copilot",
                    "source": "manual",
                    "auth_type": "api_key",
                    "base_url": "https://api.githubcopilot.com",
                }
            ]
        },
    }

    result = _call_get_available_models(monkeypatch, tmp_path, auth_payload)
    groups = _group_by_provider(result)
    assert "GitHub Copilot" in groups
    model_ids = [m["id"] for m in groups["GitHub Copilot"]]
    assert all(mid.startswith("@copilot:") for mid in model_ids), model_ids


def test_auth_store_active_provider_alias_is_resolved(monkeypatch, tmp_path):
    """active_provider read from auth.json must be alias-normalized.

    Regression: previously the alias table was applied only to config.yaml's
    active_provider, so an aliased name in auth.json (e.g. 'google') would
    not match the canonical pid ('gemini') and the prefixing logic would
    add an unwanted '@gemini:' prefix to the active provider's models.
    """
    auth_payload = {
        "version": 1,
        "providers": {},
        # Aliased name: 'google' → 'gemini' per _PROVIDER_ALIASES.
        "active_provider": "google",
        "credential_pool": {},
    }

    result = _call_get_available_models(monkeypatch, tmp_path, auth_payload)
    groups = _group_by_provider(result)
    # Gemini should appear under its canonical display name and its model
    # ids should NOT be prefixed (it's the active provider).
    assert "Gemini" in groups, f"Expected Gemini in {list(groups)}"
    model_ids = [m["id"] for m in groups["Gemini"]]
    assert model_ids, "Gemini group should have models"
    assert not any(mid.startswith("@") for mid in model_ids), (
        f"Active provider models must not be prefixed; got {model_ids}"
    )


def test_ollama_cloud_empty_catalog_skips_group(monkeypatch, tmp_path):
    """When hermes_cli returns no models for ollama-cloud, the group is omitted.

    Matches the named-custom and unknown-provider branches: we don't invent a
    catalog we can't enumerate. The logger.warning in the except branch keeps
    diagnostics available for operators.
    """
    _install_fake_hermes_cli(monkeypatch)

    # Override the stub to return empty for ollama-cloud.
    import sys as _sys

    _sys.modules["hermes_cli.models"].provider_model_ids = lambda pid: []

    auth_payload = {
        "version": 1,
        "providers": {},
        "active_provider": "openai-codex",
        "credential_pool": {
            "ollama-cloud": [
                {
                    "id": "oc-empty",
                    "label": "ollama-manual",
                    "source": "manual",
                    "auth_type": "api_key",
                }
            ]
        },
    }

    (tmp_path / "auth.json").write_text(json.dumps(auth_payload), encoding="utf-8")
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)

    old_cfg = dict(config.cfg)
    old_mtime = config._cfg_mtime
    config.cfg.clear()
    config.cfg["model"] = {}
    try:
        config._cfg_mtime = config.Path(config._get_config_path()).stat().st_mtime
    except Exception:
        config._cfg_mtime = 0.0

    try:
        result = config.get_available_models()
    finally:
        config.cfg.clear()
        config.cfg.update(old_cfg)
        config._cfg_mtime = old_mtime

    groups = _group_by_provider(result)
    assert "Ollama Cloud" not in groups, (
        f"Ollama Cloud group should be skipped when catalog is empty; got {list(groups)}"
    )


# --- _format_ollama_label helper ---


def test_format_ollama_label_simple():
    from api.config import _format_ollama_label

    assert _format_ollama_label("kimi-k2.5") == "Kimi K2.5"


def test_format_ollama_label_with_variant():
    from api.config import _format_ollama_label

    assert _format_ollama_label("qwen3-vl:235b-instruct") == "Qwen3 VL (235B Instruct)"


def test_format_ollama_label_short_acronym():
    from api.config import _format_ollama_label

    assert _format_ollama_label("glm-5.1") == "GLM 5.1"


def test_format_ollama_label_gpt_oss_with_size():
    from api.config import _format_ollama_label

    assert _format_ollama_label("gpt-oss:20b") == "GPT OSS (20B)"


def test_format_ollama_label_empty_string():
    from api.config import _format_ollama_label

    assert _format_ollama_label("") == ""


def test_format_ollama_label_no_variant():
    from api.config import _format_ollama_label

    assert _format_ollama_label("nemotron-3-super") == "Nemotron 3 Super"


# --- Fallback-path (ImportError branch) alias resolution ---


def test_fallback_path_resolves_alias_when_load_pool_unavailable(monkeypatch, tmp_path):
    """When agent.credential_pool can't be imported, the manual-inspection
    branch must still canonicalize pool keys so aliased names (e.g. 'google')
    end up under their canonical provider id ('gemini')."""
    _install_fake_hermes_cli(monkeypatch)
    # Ensure agent.credential_pool is not importable so the fallback branch runs.
    monkeypatch.setitem(sys.modules, "agent.credential_pool", None)

    auth_payload = {
        "version": 1,
        "providers": {},
        "active_provider": "openai-codex",
        "credential_pool": {
            "google": [
                {
                    "id": "gp-fallback",
                    "label": "explicit-gemini",
                    "source": "manual",
                    "auth_type": "api_key",
                }
            ]
        },
    }

    (tmp_path / "auth.json").write_text(json.dumps(auth_payload), encoding="utf-8")
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)

    old_cfg = dict(config.cfg)
    old_mtime = config._cfg_mtime
    config.cfg.clear()
    config.cfg["model"] = {}
    try:
        config._cfg_mtime = config.Path(config._get_config_path()).stat().st_mtime
    except Exception:
        config._cfg_mtime = 0.0

    try:
        result = config.get_available_models()
    finally:
        config.cfg.clear()
        config.cfg.update(old_cfg)
        config._cfg_mtime = old_mtime

    groups = _group_by_provider(result)
    assert "Gemini" in groups, (
        f"Fallback path must resolve 'google' -> 'gemini'; got {list(groups)}"
    )
    assert "Google" not in groups, (
        f"Raw alias name must not leak when fallback path runs; got {list(groups)}"
    )
