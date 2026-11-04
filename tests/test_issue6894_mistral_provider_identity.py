"""Regression tests for #6894 — Mistral models never appear even though the
API key is configured.

Root cause: a provider-identity split.  The WebUI keyed Mistral as
``mistralai`` (``api/providers.py:_PROVIDER_ENV_VAR``,
``api/config.py:_PROVIDER_MODELS`` / ``_PROVIDER_DISPLAY`` /
``_build_available_models_uncached`` env detection, ``api/routes.py:
_OPENAI_COMPAT_ENDPOINTS``, ``api/onboarding.py:
_SUPPORTED_PROVIDER_SETUPS``), while every agent-side surface —
``hermes_cli.models.provider_model_ids``, ``_MODELS_DEV_PREFERRED``,
``hermes_cli/config_defaults`` STT/TTS — answers to the bare slug
``mistral``.  Neither the WebUI's ``_PROVIDER_ALIASES`` nor the agent's
carried a ``mistralai`` ↔ ``mistral`` entry, so nothing normalised the pair.

Consequence: ``/api/models/live?provider=mistral`` succeeded (the agent
resolves that slug) while the cached ``/api/models`` catalog emitted a group
under ``provider_id: mistralai`` — a provider id the agent cannot resolve —
or dropped the group entirely.

The fix picks ONE canonical identity, ``mistral``, across key detection,
live discovery, catalog grouping, saved default, new-session state and
runtime dispatch, and keeps ``mistralai`` as a read-only alias so configs
written by earlier WebUI builds keep resolving.

Acceptance chain (maintainer's words): add key → ``/api/models`` contains
Mistral → save a Mistral default (via ``/api/default-model``, NOT
``/api/settings``) → start a new chat with the same provider.
"""

from __future__ import annotations

import io
import json
import sys
import types
from urllib.parse import urlparse

import pytest
import yaml

import api.config as config
import api.onboarding as onboarding
import api.profiles as profiles
import api.providers as providers
import api.routes as routes


# Live Mistral ids as the agent reports them.  ``mistral`` resolves;
# ``mistralai`` does not — that asymmetry is the whole bug.
_MISTRAL_LIVE_IDS = [
    "mistral-large-latest",
    "mistral-small-latest",
    "ministral-14b-2512",
    "devstral-medium-latest",
]


def _scrub_provider_env(monkeypatch) -> None:
    for var in (
        "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GOOGLE_API_KEY", "GEMINI_API_KEY",
        "DEEPSEEK_API_KEY", "XAI_API_KEY", "GROQ_API_KEY",
        "MISTRAL_API_KEY", "OPENROUTER_API_KEY",
        "OLLAMA_CLOUD_API_KEY", "OLLAMA_API_KEY",
        "GLM_API_KEY", "KIMI_API_KEY", "MOONSHOT_API_KEY",
        "MINIMAX_API_KEY", "MINIMAX_CN_API_KEY",
        "XIAOMI_API_KEY",
        "OPENCODE_ZEN_API_KEY", "OPENCODE_GO_API_KEY",
        "NOUS_API_KEY", "NVIDIA_API_KEY", "LM_API_KEY", "LMSTUDIO_API_KEY",
    ):
        monkeypatch.delenv(var, raising=False)


def _install_fake_hermes_cli(monkeypatch) -> None:
    """Stub ``hermes_cli`` so provider discovery is deterministic offline.

    Mirrors the real agent's Mistral behaviour: ``provider_model_ids``
    resolves the bare ``mistral`` slug and returns nothing for any other
    spelling of it.
    """
    fake_pkg = types.ModuleType("hermes_cli")
    fake_pkg.__path__ = []

    fake_models = types.ModuleType("hermes_cli.models")
    fake_models.list_available_providers = lambda: [
        {"id": "mistral", "label": "Mistral", "aliases": [], "authenticated": True},
    ]
    fake_models.provider_model_ids = lambda pid: (
        list(_MISTRAL_LIVE_IDS) if str(pid or "").strip().lower() == "mistral" else []
    )

    fake_auth = types.ModuleType("hermes_cli.auth")
    fake_auth.get_auth_status = lambda _pid: {}

    monkeypatch.setitem(sys.modules, "hermes_cli", fake_pkg)
    monkeypatch.setitem(sys.modules, "hermes_cli.models", fake_models)
    monkeypatch.setitem(sys.modules, "hermes_cli.auth", fake_auth)
    monkeypatch.delitem(sys.modules, "agent.credential_pool", raising=False)
    monkeypatch.delitem(sys.modules, "agent", raising=False)

    config.invalidate_models_cache()


def _swap_in_test_config(extra_cfg: dict):
    old_cfg = dict(config.cfg)
    old_mtime = config._cfg_mtime
    config.cfg.clear()
    config.cfg["model"] = {}
    config.cfg.update(extra_cfg)
    try:
        config._cfg_mtime = config.Path(config._get_config_path()).stat().st_mtime
    except Exception:
        config._cfg_mtime = 0.0

    def _restore():
        config.cfg.clear()
        config.cfg.update(old_cfg)
        config._cfg_mtime = old_mtime

    return _restore


def _mistral_groups(catalog: dict) -> list[dict]:
    return [
        g for g in catalog.get("groups", [])
        if "mistral" in (g.get("provider_id") or "").lower()
        or "mistral" in (g.get("provider") or "").lower()
    ]


# ────────────────────────────────────────────────────────────────────────
# Section 1 — one canonical identity in every WebUI table
# ────────────────────────────────────────────────────────────────────────


class TestSingleCanonicalIdentity:
    """Every table that names Mistral must use the same canonical slug."""

    def test_canonical_slug_is_mistral(self):
        from api.config import _canonicalise_provider_id, _resolve_provider_alias

        # _resolve_provider_alias lowercases and trims but does NOT fold
        # underscores, so ``mistral_ai`` only canonicalises through
        # _canonicalise_provider_id (which folds _ → -) below.
        for raw in ("mistral", "mistralai", "mistral-ai"):
            assert _resolve_provider_alias(raw) == "mistral", (
                f"{raw!r} must resolve to the canonical 'mistral' slug"
            )
        for raw in ("mistral", "Mistral", "MISTRAL", "mistralai", "MistralAI",
                    "mistral_ai", "mistral-ai"):
            assert _canonicalise_provider_id(raw) == "mistral", (
                f"{raw!r} must canonicalise to 'mistral'"
            )

    def test_canonicalisation_is_idempotent(self):
        from api.config import _canonicalise_provider_id

        for raw in ("mistralai", "MISTRAL_AI", "mistral-ai", "mistral"):
            once = _canonicalise_provider_id(raw)
            assert _canonicalise_provider_id(once) == once

    def test_legacy_alias_is_read_only(self):
        """``mistralai`` must be an alias, never a second canonical key."""
        from api.config import _PROVIDER_ALIASES, _PROVIDER_DISPLAY, _PROVIDER_MODELS

        assert _PROVIDER_ALIASES.get("mistralai") == "mistral"
        assert "mistralai" not in _PROVIDER_DISPLAY, (
            "'mistralai' must not be a second canonical display key"
        )
        assert "mistralai" not in _PROVIDER_MODELS, (
            "'mistralai' must not be a second canonical catalog key"
        )
        assert "mistral" in _PROVIDER_DISPLAY
        assert "mistral" in _PROVIDER_MODELS

    def test_env_var_table_uses_canonical_slug(self):
        assert providers._PROVIDER_ENV_VAR.get("mistral") == "MISTRAL_API_KEY"
        assert "mistralai" not in providers._PROVIDER_ENV_VAR

    def test_openai_compat_endpoint_uses_canonical_slug(self):
        assert routes._OPENAI_COMPAT_ENDPOINTS.get("mistral") == "https://api.mistral.ai/v1"
        assert "mistralai" not in routes._OPENAI_COMPAT_ENDPOINTS

    def test_onboarding_setup_uses_canonical_slug(self):
        assert "mistral" in onboarding._SUPPORTED_PROVIDER_SETUPS
        assert "mistralai" not in onboarding._SUPPORTED_PROVIDER_SETUPS
        assert onboarding._SUPPORTED_PROVIDER_SETUPS["mistral"]["env_var"] == "MISTRAL_API_KEY"

    def test_env_detection_adds_canonical_slug(self):
        with open("api/config.py", encoding="utf-8") as f:
            src = f.read()
        assert 'add("mistral")' in src, (
            "MISTRAL_API_KEY detection must add the canonical 'mistral' slug"
        )
        assert 'add("mistralai")' not in src, (
            "no detection path may add the non-canonical 'mistralai' slug"
        )


# ────────────────────────────────────────────────────────────────────────
# Section 2 — key detection and live discovery share the identity
# ────────────────────────────────────────────────────────────────────────


class TestKeyDetectionAndLiveDiscovery:
    def test_live_discovery_resolves_both_spellings(self, monkeypatch):
        """``_read_live_provider_model_ids`` tries the alias as a secondary
        candidate, so a legacy ``mistralai`` caller still gets the catalog."""
        _install_fake_hermes_cli(monkeypatch)
        for pid in ("mistral", "mistralai"):
            ids = config._read_live_provider_model_ids(pid)
            assert ids, f"live discovery returned nothing for {pid!r}"
            assert "mistral-large-latest" in ids

    def test_key_detection_uses_canonical_env_var(self, monkeypatch, tmp_path):
        _scrub_provider_env(monkeypatch)
        monkeypatch.setenv("MISTRAL_API_KEY", "fake-mistral-key")
        monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)
        assert providers._provider_has_key("mistral") is True
        assert providers._provider_env_var_for("mistral") == "MISTRAL_API_KEY"

    def test_static_catalog_reports_canonical_provider_id(self, monkeypatch, tmp_path):
        """The network-free catalog is the cached /api/models surface: its
        group must carry the canonical provider id, not the alias."""
        _scrub_provider_env(monkeypatch)
        monkeypatch.setenv("MISTRAL_API_KEY", "fake-mistral-key")
        monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)
        _install_fake_hermes_cli(monkeypatch)

        catalog = config._static_models_catalog_without_live_probes()
        groups = _mistral_groups(catalog)
        assert len(groups) == 1, (
            f"expected exactly one Mistral group, got "
            f"{[(g['provider'], g['provider_id']) for g in groups]}"
        )
        assert groups[0]["provider_id"] == "mistral", (
            f"catalog group must use the canonical 'mistral' id, got "
            f"{groups[0]['provider_id']!r}"
        )


# ────────────────────────────────────────────────────────────────────────
# Section 3 — the maintainer's acceptance chain
# ────────────────────────────────────────────────────────────────────────


class TestAcceptanceChain:
    """add key → /api/models contains Mistral → save a Mistral default →
    start a new chat with the same provider."""

    def _build_catalog(self, monkeypatch, tmp_path, cfg_extra: dict) -> dict:
        _scrub_provider_env(monkeypatch)
        monkeypatch.setenv("MISTRAL_API_KEY", "fake-mistral-key")
        monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)
        _install_fake_hermes_cli(monkeypatch)
        restore = _swap_in_test_config(cfg_extra)
        try:
            return config.get_available_models(force_refresh=True)
        finally:
            restore()

    def test_step1_catalog_contains_mistral_with_live_models(self, monkeypatch, tmp_path):
        catalog = self._build_catalog(
            monkeypatch, tmp_path,
            {"providers": {"mistral": {}}},
        )
        groups = _mistral_groups(catalog)
        assert len(groups) == 1, (
            f"expected exactly one Mistral group, got "
            f"{[(g['provider'], g['provider_id']) for g in groups]}"
        )
        group = groups[0]
        assert group["provider_id"] == "mistral", (
            f"group must use the canonical 'mistral' id, got {group['provider_id']!r}"
        )
        model_ids = [m["id"] for m in group.get("models", [])]
        assert "mistral-large-latest" in model_ids, (
            f"Mistral group must carry the live catalog, got {model_ids}"
        )
        assert "devstral-medium-latest" in model_ids, (
            f"Mistral group must carry the live catalog, got {model_ids}"
        )

    def test_step2_saving_default_keeps_canonical_provider(self, monkeypatch, tmp_path):
        """The UI persists through /api/default-model (not /api/settings)."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text("model: {}\n", encoding="utf-8")
        old_cfg = dict(config.cfg)
        old_mtime = config._cfg_mtime
        old_path = config._cfg_path
        monkeypatch.setattr(config, "_get_config_path", lambda: config_file)
        config.cfg.clear()
        config.cfg["model"] = {}
        config._cfg_path = config_file
        config._cfg_mtime = config_file.stat().st_mtime
        try:
            result = config.set_hermes_default_model(
                "mistral-large-latest", provider="mistral"
            )
            saved = yaml.safe_load(config_file.read_text(encoding="utf-8"))
        finally:
            config.cfg.clear()
            config.cfg.update(old_cfg)
            config._cfg_mtime = old_mtime
            config._cfg_path = old_path
            config.invalidate_models_cache()

        assert result["ok"] is True
        assert saved["model"]["provider"] == "mistral", (
            f"saved default must persist the canonical provider, got "
            f"{saved['model'].get('provider')!r}"
        )
        assert saved["model"]["default"] == "mistral-large-latest"

    def test_step2_default_model_route_persists_canonical_provider(self, monkeypatch, tmp_path):
        """Exercise the real POST /api/default-model route the UI calls."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text("model: {}\n", encoding="utf-8")
        old_cfg = dict(config.cfg)
        old_mtime = config._cfg_mtime
        old_path = config._cfg_path
        monkeypatch.setattr(config, "_get_config_path", lambda: config_file)
        config.cfg.clear()
        config.cfg["model"] = {}
        config._cfg_path = config_file
        config._cfg_mtime = config_file.stat().st_mtime
        try:
            body = json.dumps(
                {"model": "mistral-large-latest", "provider": "mistral"}
            ).encode("utf-8")
            handler = _FakeHandler(body_bytes=body)
            handled = routes.handle_post(handler, urlparse("/api/default-model"))
            saved = yaml.safe_load(config_file.read_text(encoding="utf-8"))
        finally:
            config.cfg.clear()
            config.cfg.update(old_cfg)
            config._cfg_mtime = old_mtime
            config._cfg_path = old_path
            config.invalidate_models_cache()

        assert handled is not False
        assert handler.status in (200, None), (
            f"POST /api/default-model failed with {handler.status}: "
            f"{handler.json_body()}"
        )
        payload = handler.json_body()
        assert payload.get("provider") == "mistral", (
            f"/api/default-model must answer with the canonical provider, got "
            f"{payload.get('provider')!r}"
        )
        assert saved["model"]["provider"] == "mistral", (
            f"/api/default-model must persist the canonical provider, got "
            f"{saved['model'].get('provider')!r}"
        )

    def test_step3_new_session_uses_the_same_provider(self, monkeypatch):
        """A new chat seeded from the picker's group must keep 'mistral'."""
        model, provider = routes._session_model_state_from_request(
            "mistral-large-latest", "mistral"
        )
        assert model == "mistral-large-latest"
        assert provider == "mistral", (
            f"new-session state must carry the canonical provider, got {provider!r}"
        )

        # The picker's @provider:model routing hint keeps its shape; what
        # matters is that the provider half is the canonical slug.
        model, provider = routes._session_model_state_from_request(
            "@mistral:mistral-large-latest", None
        )
        assert model == "@mistral:mistral-large-latest"
        assert provider == "mistral", (
            f"new-session state must carry the canonical provider, got {provider!r}"
        )

    def test_step3_runtime_dispatch_resolves_canonical_provider(self, monkeypatch):
        monkeypatch.setattr(config, "cfg", {
            "model": {"provider": "mistral", "default": "mistral-large-latest"},
            "providers": {"mistral": {"api_key": "fake-mistral-key"}},
            "custom_providers": [],
        })
        try:
            model, provider, base_url = config.resolve_model_provider(
                "@mistral:mistral-large-latest"
            )
        finally:
            config.reload_config()

        assert model == "mistral-large-latest"
        assert provider == "mistral", (
            f"runtime dispatch must resolve to the canonical provider, got {provider!r}"
        )

    def test_legacy_mistralai_config_still_resolves_to_canonical(self, monkeypatch, tmp_path):
        """A config.yaml written by an older WebUI keeps working."""
        _scrub_provider_env(monkeypatch)
        monkeypatch.setenv("MISTRAL_API_KEY", "fake-mistral-key")
        monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)
        _install_fake_hermes_cli(monkeypatch)

        restore = _swap_in_test_config({
            "model": {"provider": "mistralai", "default": "mistral-large-latest"},
            "providers": {"mistralai": {"api_key": "fake-mistral-key"}},
        })
        try:
            catalog = config.get_available_models(force_refresh=True)
        finally:
            restore()

        groups = _mistral_groups(catalog)
        assert len(groups) == 1, (
            f"legacy 'mistralai' config must collapse to ONE group, got "
            f"{[(g['provider'], g['provider_id']) for g in groups]}"
        )
        assert groups[0]["provider_id"] == "mistral"
        assert catalog.get("active_provider") == "mistral"

    def test_full_chain_add_key_to_new_chat(self, monkeypatch, tmp_path):
        """The maintainer's acceptance chain, driven end-to-end.

        add key → /api/models contains Mistral → save a Mistral default via
        /api/default-model → start a new chat with the SAME provider id the
        picker emitted.  Every hop consumes the previous hop's output, so a
        break anywhere in the identity chain fails here.
        """
        config_file = tmp_path / "config.yaml"
        config_file.write_text("model: {}\n", encoding="utf-8")
        _scrub_provider_env(monkeypatch)
        monkeypatch.setenv("MISTRAL_API_KEY", "fake-mistral-key")
        monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)
        _install_fake_hermes_cli(monkeypatch)

        old_cfg = dict(config.cfg)
        old_mtime = config._cfg_mtime
        old_path = config._cfg_path
        monkeypatch.setattr(config, "_get_config_path", lambda: config_file)
        config.cfg.clear()
        config.cfg["model"] = {}
        config.cfg["providers"] = {"mistral": {}}
        config._cfg_path = config_file
        config._cfg_mtime = config_file.stat().st_mtime
        try:
            # ── hop 1: /api/models contains Mistral ───────────────────────
            catalog = config.get_available_models(force_refresh=True)
            groups = _mistral_groups(catalog)
            assert len(groups) == 1, (
                f"expected exactly one Mistral group, got "
                f"{[(g['provider'], g['provider_id']) for g in groups]}"
            )
            provider_id = groups[0]["provider_id"]
            model_id = groups[0]["models"][0]["id"]

            # ── hop 2: save a Mistral default through /api/default-model ───
            body = json.dumps({"model": model_id, "provider": provider_id}).encode("utf-8")
            handler = _FakeHandler(body_bytes=body)
            routes.handle_post(handler, urlparse("/api/default-model"))
            saved = yaml.safe_load(config_file.read_text(encoding="utf-8"))

            assert saved["model"]["provider"] == provider_id, (
                f"saved default must keep the picker's provider id, got "
                f"{saved['model'].get('provider')!r} (picker emitted {provider_id!r})"
            )

            # ── hop 3: a new chat with the same provider ──────────────────
            new_model, new_provider = routes._session_model_state_from_request(
                model_id, provider_id
            )
            assert new_provider == provider_id
        finally:
            config.cfg.clear()
            config.cfg.update(old_cfg)
            config._cfg_mtime = old_mtime
            config._cfg_path = old_path
            config.invalidate_models_cache()

        assert provider_id == "mistral", (
            f"the whole chain must run on the canonical 'mistral' identity, "
            f"but the picker emitted {provider_id!r}"
        )


# ────────────────────────────────────────────────────────────────────────
# Section 4 — a legacy raw record keeps its endpoint (#8664 review P2)
# ────────────────────────────────────────────────────────────────────────

_LEGACY_MISTRAL_RECORD = {
    "api_key": "legacy-record-canary",
    "base_url": "https://legacy-relay.example/v1",
    "models": ["mistral-small-latest"],
}
# The review's fixture: an unrelated active provider, a Mistral record
# written under the legacy ``mistralai`` key, and no ambient Mistral key.
_LEGACY_MISTRAL_CFG = {
    "model": {"provider": "anthropic", "default": "unrelated-model"},
    "providers": {"mistralai": dict(_LEGACY_MISTRAL_RECORD)},
}


def _static_catalog(monkeypatch, tmp_path, cfg_extra: dict) -> dict:
    """Network-free /api/models surface for *cfg_extra* (test cfg only)."""
    _scrub_provider_env(monkeypatch)
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)
    _install_fake_hermes_cli(monkeypatch)
    restore = _swap_in_test_config(cfg_extra)
    try:
        return config._static_models_catalog_without_live_probes()
    finally:
        restore()


class TestLegacyRecordEndpointRetention:
    """The catalog emits the canonical ``@mistral:`` option; the endpoint it
    must route to still lives on the legacy ``providers.mistralai`` record."""

    def test_canonical_option_resolves_to_the_legacy_endpoint(self, monkeypatch, tmp_path):
        catalog = _static_catalog(monkeypatch, tmp_path, _LEGACY_MISTRAL_CFG)
        groups = _mistral_groups(catalog)
        assert len(groups) == 1
        assert groups[0]["provider_id"] == "mistral"
        option = groups[0]["models"][0]["id"]
        assert option == "@mistral:mistral-small-latest", (
            f"static catalog must emit the canonical option, got {option!r}"
        )

        restore = _swap_in_test_config(_LEGACY_MISTRAL_CFG)
        try:
            resolved = config.resolve_model_provider(option)
        finally:
            restore()
        assert resolved == (
            "mistral-small-latest",
            "mistral",
            "https://legacy-relay.example/v1",
        ), (
            "the canonical option must keep routing to the legacy record's "
            f"endpoint, got {resolved!r}"
        )

    def test_base_url_lookup_finds_the_legacy_record_for_either_spelling(self):
        restore = _swap_in_test_config(_LEGACY_MISTRAL_CFG)
        try:
            for spelling in ("mistral", "mistralai"):
                assert config._raw_provider_key_for_provider_id(spelling) == "mistralai", (
                    f"{spelling!r} must resolve to the legacy raw config key"
                )
                assert config._get_provider_base_url(spelling) == (
                    "https://legacy-relay.example/v1"
                ), f"{spelling!r} lost the legacy record's endpoint"
        finally:
            restore()

    def test_legacy_option_from_an_older_build_still_keeps_its_endpoint(self):
        """A session stored by the frozen base carries ``@mistralai:...``."""
        restore = _swap_in_test_config(_LEGACY_MISTRAL_CFG)
        try:
            resolved = config.resolve_model_provider("@mistralai:mistral-small-latest")
        finally:
            restore()
        assert resolved == (
            "mistral-small-latest",
            "mistralai",
            "https://legacy-relay.example/v1",
        ), f"the legacy option must keep its endpoint, got {resolved!r}"

    def test_legacy_active_model_block_base_url_survives_canonical_request(self):
        """``model.provider: mistralai`` + ``model.base_url`` (the historical
        single-record shape) must still answer a canonical request."""
        restore = _swap_in_test_config({
            "model": {
                "provider": "mistralai",
                "default": "mistral-small-latest",
                "base_url": "https://legacy-relay.example/v1",
            },
            "providers": {"mistralai": {"api_key": "legacy-record-canary"}},
        })
        try:
            assert config._get_provider_base_url("mistral") == (
                "https://legacy-relay.example/v1"
            )
            assert config._get_provider_base_url("mistralai") == (
                "https://legacy-relay.example/v1"
            )
        finally:
            restore()

    def test_unrelated_provider_record_is_untouched(self):
        """The alias-aware lookup must not leak across providers."""
        restore = _swap_in_test_config({
            "model": {"provider": "anthropic", "default": "unrelated-model"},
            "providers": {
                "mistralai": dict(_LEGACY_MISTRAL_RECORD),
                "anthropic": {"base_url": "https://anthropic-relay.example/v1"},
            },
        })
        try:
            assert config._get_provider_base_url("anthropic") == (
                "https://anthropic-relay.example/v1"
            )
            assert config._get_provider_base_url("openai") is None
        finally:
            restore()


# ────────────────────────────────────────────────────────────────────────
# Section 5 — canonical+legacy record collision semantics (#8664 review)
# ────────────────────────────────────────────────────────────────────────

# Documented rule: when BOTH ``providers.mistral`` and ``providers.mistralai``
# exist, the FIRST record in config order owns the canonical identity — the
# same ``setdefault`` rule the catalog builders use for
# ``canonical_to_raw_provider_key`` — and the choice does NOT depend on which
# spelling the caller used. No stage may select a different record.
_COLLISION_CFG_LEGACY_FIRST = {
    "model": {"provider": "anthropic", "default": "unrelated-model"},
    "providers": {
        "mistralai": {
            "api_key": "legacy-record-canary",
            "base_url": "https://legacy-relay.example/v1",
            "models": ["mistral-small-latest"],
        },
        "mistral": {
            "api_key": "canonical-record-canary",
            "base_url": "https://api.mistral.ai/v1",
            "models": ["mistral-large-latest"],
        },
    },
}
_COLLISION_CFG_CANONICAL_FIRST = {
    "model": {"provider": "anthropic", "default": "unrelated-model"},
    "providers": {
        "mistral": {
            "api_key": "canonical-record-canary",
            "base_url": "https://api.mistral.ai/v1",
            "models": ["mistral-large-latest"],
        },
        "mistralai": {
            "api_key": "legacy-record-canary",
            "base_url": "https://legacy-relay.example/v1",
            "models": ["mistral-small-latest"],
        },
    },
}


class TestCanonicalLegacyRecordCollision:
    """Both records present, different endpoints — behaviour must be defined,
    deterministic, and identical at every stage."""

    def test_first_config_record_wins_for_both_spellings(self):
        restore = _swap_in_test_config(_COLLISION_CFG_LEGACY_FIRST)
        try:
            for spelling in ("mistral", "mistralai"):
                assert config._raw_provider_key_for_provider_id(spelling) == "mistralai", (
                    f"{spelling!r} must resolve to the first config record"
                )
                assert config._get_provider_base_url(spelling) == (
                    "https://legacy-relay.example/v1"
                ), f"{spelling!r} resolved to a different record's endpoint"
        finally:
            restore()

    def test_every_stage_selects_the_same_record(self, monkeypatch, tmp_path):
        """Catalog record identity, endpoint lookup and routing all land on
        the first config record — never a mix of the two.

        The catalog MERGES both records' model lists (pre-existing behaviour
        for two keys that canonicalise to one id), but the record IDENTITY —
        the raw key the catalog's ``canonical_to_raw_provider_key`` map
        selects — is the first config entry, and so is the endpoint below.
        """
        catalog = _static_catalog(monkeypatch, tmp_path, _COLLISION_CFG_LEGACY_FIRST)
        groups = _mistral_groups(catalog)
        assert len(groups) == 1
        model_ids = [m["id"] for m in groups[0]["models"]]
        assert model_ids[0] == "@mistral:mistral-small-latest", (
            "the catalog's record identity must be the first config record "
            f"(its model leads the group), got {model_ids}"
        )
        assert "@mistral:mistral-large-latest" in model_ids

        restore = _swap_in_test_config(_COLLISION_CFG_LEGACY_FIRST)
        try:
            resolved = config.resolve_model_provider("@mistral:mistral-small-latest")
        finally:
            restore()
        assert resolved == (
            "mistral-small-latest",
            "mistral",
            "https://legacy-relay.example/v1",
        ), (
            "routing must use the SAME record the catalog selected, "
            f"got {resolved!r}"
        )

    def test_config_order_decides_not_the_requested_spelling(self):
        """Reversing config order flips the winner for BOTH spellings —
        proof the rule is config-order-based and spelling-independent."""
        restore = _swap_in_test_config(_COLLISION_CFG_CANONICAL_FIRST)
        try:
            for spelling in ("mistral", "mistralai"):
                assert config._raw_provider_key_for_provider_id(spelling) == "mistral"
                assert config._get_provider_base_url(spelling) == (
                    "https://api.mistral.ai/v1"
                )
        finally:
            restore()

    def test_single_legacy_record_is_not_a_collision(self):
        """Legacy-only config: one record, one endpoint, no ambiguity."""
        restore = _swap_in_test_config(_LEGACY_MISTRAL_CFG)
        try:
            assert config._get_provider_base_url("mistral") == (
                "https://legacy-relay.example/v1"
            )
            assert config._get_provider_base_url("mistralai") == (
                "https://legacy-relay.example/v1"
            )
        finally:
            restore()


# ────────────────────────────────────────────────────────────────────────
# Section 6 — onboarding keeps accepting the legacy selector (#8664 P2)
# ────────────────────────────────────────────────────────────────────────


def _run_onboarding_setup(tmp_path, monkeypatch, provider: str, model: str):
    """Drive the real ``apply_onboarding_setup`` against a real temp
    config.yaml. Returns (saved_settings_calls, written_config)."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text("model: {}\n", encoding="utf-8")

    monkeypatch.setattr("api.onboarding._get_config_path", lambda: config_path)
    monkeypatch.setattr("api.onboarding._get_active_hermes_home", lambda: tmp_path)
    monkeypatch.setattr("api.onboarding._write_env_file", lambda p, d: None)
    monkeypatch.setattr("api.onboarding._provider_api_key_present", lambda *a: True)
    monkeypatch.setattr("api.onboarding.reload_config", lambda: None)
    # Keep the test hermetic: the status tail is not what this finding is
    # about, and it would otherwise re-read the developer's real config.
    monkeypatch.setattr(
        "api.onboarding.get_onboarding_status", lambda: {"completed": True}
    )
    settings_calls: list[dict] = []
    monkeypatch.setattr(
        "api.onboarding.save_settings", lambda s: settings_calls.append(dict(s)) or s
    )

    onboarding.apply_onboarding_setup({
        "provider": provider,
        "model": model,
        "api_key": "",
        "confirm_overwrite": True,
    })
    written = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    return settings_calls, written


class TestLegacyOnboardingSelector:
    """``mistralai`` used to be a supported setup key; the request must keep
    taking the real setup path instead of the unsupported early completion."""

    def test_legacy_mistralai_request_writes_provider_and_endpoint(self, tmp_path, monkeypatch):
        settings_calls, written = _run_onboarding_setup(
            tmp_path, monkeypatch, "mistralai", "mistral-large-latest"
        )
        assert written["model"]["provider"] == "mistral", (
            f"legacy selector must persist the canonical provider, got "
            f"{written['model'].get('provider')!r}"
        )
        assert written["model"]["default"] == "mistral-large-latest"
        assert written["model"]["base_url"] == "https://api.mistral.ai/v1"
        assert settings_calls == [], (
            "a supported legacy selector must NOT take the unsupported "
            f"early-completion path, got {settings_calls!r}"
        )

    def test_legacy_alias_spellings_are_accepted_too(self, tmp_path, monkeypatch):
        for spelling in ("mistral-ai", "mistral_ai", "MistralAI"):
            settings_calls, written = _run_onboarding_setup(
                tmp_path, monkeypatch, spelling, "mistral-large-latest"
            )
            assert written["model"]["provider"] == "mistral", (
                f"{spelling!r} must fold onto the canonical setup"
            )
            assert written["model"]["base_url"] == "https://api.mistral.ai/v1"
            assert settings_calls == []

    def test_canonical_mistral_request_still_writes_endpoint(self, tmp_path, monkeypatch):
        settings_calls, written = _run_onboarding_setup(
            tmp_path, monkeypatch, "mistral", "mistral-large-latest"
        )
        assert written["model"]["provider"] == "mistral"
        assert written["model"]["base_url"] == "https://api.mistral.ai/v1"
        assert settings_calls == []

    def test_genuinely_unsupported_provider_still_early_completes(self, tmp_path, monkeypatch):
        """Over-normalisation guard: a provider with no supported setup must
        keep the mark-complete-and-pass-through behaviour."""
        for provider in ("copilot", "openai-codex"):
            settings_calls, written = _run_onboarding_setup(
                tmp_path, monkeypatch, provider, "some-model"
            )
            assert written["model"] == {}, (
                f"{provider!r} must not write a model block, got {written['model']!r}"
            )
            assert settings_calls == [{"onboarding_completed": True}], (
                f"{provider!r} must still early-complete onboarding"
            )


class _FakeHandler:
    """Minimal BaseHTTPRequestHandler stand-in for routes.handle_post."""

    def __init__(self, body_bytes: bytes = b""):
        self.status = None
        self.sent_headers = []
        self.body = bytearray()
        self.wfile = self
        self.rfile = io.BytesIO(body_bytes)
        self.headers = {"Content-Length": str(len(body_bytes))}
        self.request = None

    def send_response(self, status):
        self.status = status

    def send_header(self, name, value):
        self.sent_headers.append((name, value))

    def end_headers(self):
        pass

    def write(self, data):
        self.body.extend(data)

    def json_body(self):
        return json.loads(bytes(self.body).decode("utf-8"))


# ────────────────────────────────────────────────────────────────────────
# Section 7 — the two round-2 findings (#8664 review 2026-11-02)
# ────────────────────────────────────────────────────────────────────────
#
# P2-1: the final connection bundle must come from ONE record. With both
# ``providers.mistralai`` and ``providers.mistral`` present, the catalog emits
# a canonical ``@mistral:`` option while the native resolver picks its
# credential by EXACT spelling, so the endpoint and the key could come from
# different records. The bundle now re-attaches the credential (and the
# endpoint) to the record the endpoint resolution already chose.
#
# P2-2: onboarding must reuse an EXISTING configured key. ``_provider_api_key_present``
# did an exact ``providers.<provider>`` lookup, so a legacy ``providers.mistralai``
# record read as "no key configured" once the selector was canonicalised to
# ``mistral``. The detection now resolves the record canonically, the same way
# the catalog and the endpoint lookup do.

_COLLISION_LEGACY_FIRST_PAIR = {
    "model": {"provider": "anthropic", "default": "unrelated-model"},
    "providers": {
        "mistralai": {
            "api_key": "legacy-record-canary",
            "base_url": "https://legacy-relay.example/v1",
        },
        "mistral": {
            "api_key": "canonical-record-canary",
            "base_url": "https://canonical-relay.example/v1",
        },
    },
}
_COLLISION_CANONICAL_FIRST_PAIR = {
    "model": {"provider": "anthropic", "default": "unrelated-model"},
    "providers": {
        "mistral": {
            "api_key": "canonical-record-canary",
            "base_url": "https://canonical-relay.example/v1",
        },
        "mistralai": {
            "api_key": "legacy-record-canary",
            "base_url": "https://legacy-relay.example/v1",
        },
    },
}


def _final_bundle_inputs(option: str) -> tuple:
    """The ``(provider, api_key, base_url, runtime)`` a caller passes to
    ``merge_custom_provider_runtime_bundle`` for *option*, with the native
    resolver picking its credential by EXACT spelling.

    Call inside an active ``_swap_in_test_config`` so the resolution reads the
    same config the assertion will.
    """
    _model, provider, webui_base_url = config.resolve_model_provider(option)
    providers_cfg = config.cfg.get("providers") or {}
    entry = providers_cfg.get(provider) if isinstance(providers_cfg, dict) else None
    native_key = (
        str(entry.get("api_key") or "").strip() if isinstance(entry, dict) else ""
    ) or None
    runtime = {
        "provider": provider,
        "api_key": native_key,
        "base_url": webui_base_url,
    }
    return provider, native_key, webui_base_url, runtime


def _final_bundle_for(cfg_extra: dict, option: str):
    """Emitted option -> endpoint selection -> native credential -> bundle.

    The native resolver is asked for the same provider id the WebUI resolved
    and picks its credential by EXACT spelling, which is the split this
    finding is about.
    """
    restore = _swap_in_test_config(cfg_extra)
    try:
        _model, provider, webui_base_url = config.resolve_model_provider(option)
        providers_cfg = config.cfg.get("providers") or {}
        entry = providers_cfg.get(provider) if isinstance(providers_cfg, dict) else None
        native_key = (
            str(entry.get("api_key") or "").strip()
            if isinstance(entry, dict)
            else ""
        ) or None
        bundle = config.merge_custom_provider_runtime_bundle(
            provider,
            native_key,
            webui_base_url,
            {"provider": provider, "api_key": native_key, "base_url": webui_base_url},
        )
        return bundle
    finally:
        restore()


class TestFinalBundleRecordOwnership:
    """P2-1: endpoint and credential must come from the SAME record."""

    def test_bundle_never_mixes_two_records(self):
        for label, cfg_extra in (
            ("legacy-first", _COLLISION_LEGACY_FIRST_PAIR),
            ("canonical-first", _COLLISION_CANONICAL_FIRST_PAIR),
        ):
            for option in (
                "@mistral:mistral-small-latest",
                "@mistralai:mistral-small-latest",
            ):
                restore = _swap_in_test_config(cfg_extra)
                try:
                    bundle = config.merge_custom_provider_runtime_bundle(
                        *_final_bundle_inputs(option)
                    )
                    # The owner is resolved from the SAME config the bundle
                    # read, so the two cannot disagree about ordering.
                    owner = config._raw_provider_key_for_provider_id(
                        bundle["provider"]
                    )
                    owner_record = cfg_extra["providers"].get(owner) or {}
                finally:
                    restore()
                assert bundle["api_key"] == owner_record.get("api_key"), (
                    f"{label} {option}: the bundle's credential came from a "
                    f"different record than the endpoint owner {owner!r} "
                    f"(bundle key {bundle['api_key']!r}, owner key "
                    f"{owner_record.get('api_key')!r})"
                )
                assert bundle["base_url"] == owner_record.get("base_url"), (
                    f"{label} {option}: the bundle's endpoint came from a "
                    f"different record than its owner {owner!r} "
                    f"(bundle url {bundle['base_url']!r}, owner url "
                    f"{owner_record.get('base_url')!r})"
                )

    def test_single_record_config_is_untouched(self):
        """A config with no collision must keep exactly what it declared."""
        cfg_extra = {
            "model": {"provider": "anthropic", "default": "unrelated-model"},
            "providers": {
                "mistralai": {
                    "api_key": "legacy-record-canary",
                    "base_url": "https://legacy-relay.example/v1",
                },
            },
        }
        bundle = _final_bundle_for(cfg_extra, "@mistral:mistral-small-latest")
        assert bundle["api_key"] == "legacy-record-canary"
        assert bundle["base_url"] == "https://legacy-relay.example/v1"

    def test_truly_keyless_owner_never_borrows_the_sibling_key(self):
        """The FIRST (owning) record declares NO credential source at all.
        #8664 review P2-1 (2026-11-03) called out that the earlier version of
        "this test gave the OWNING record a key ("legacy-record-canary") and
        "only left the sibling keyless — it never tested a keyless owner. This
        "version makes the owner genuinely keyless and verifies the final",
        "bundle carries the owner's OWN keyless authority, never the",
        "sibling's inline key."""
        cfg_extra = {
            "model": {"provider": "anthropic", "default": "unrelated-model"},
            "providers": {
                "mistralai": {
                    "base_url": "https://legacy-relay.example/v1",
                },
                "mistral": {
                    "api_key": "sibling-record-canary",
                    "base_url": "https://canonical-relay.example/v1",
                },
            },
        }
        restore = _swap_in_test_config(cfg_extra)
        try:
            # Production shape: the native result for the non-owning spelling
            # is the SIBLING's complete authority (its URL, its key, its
            # protocol) — the very input the finding's protocol/pool cases are
            # about. The owner is the FIRST config record and is keyless.
            bundle = config.merge_custom_provider_runtime_bundle(
                "mistral",
                "sibling-record-canary",
                "https://canonical-relay.example/v1",
                {
                    "provider": "mistral",
                    "api_key": "sibling-record-canary",
                    "base_url": "https://canonical-relay.example/v1",
                },
            )
        finally:
            restore()
        assert bundle["base_url"] == "https://legacy-relay.example/v1", (
            "the endpoint owner is the FIRST config record (the documented ",
            "collision rule), which declares this URL"
        )
        assert bundle["api_key"] != "sibling-record-canary", (
            "a truly keyless owner must never borrow the sibling's inline key; ",
            f"got {bundle['api_key']!r}"
        )
        assert bundle["api_key"] == "no-key-required", (
            "the record declares NO credential source, so by its own statement ",
            "it is keyless — the same answer the installed runtime gives for ",
            f"this record; got {bundle['api_key']!r}"
        )


class TestLegacyOnboardingReusesStoredKey:
    """P2-2: an existing configured key must satisfy onboarding."""

    def test_existing_legacy_key_is_reused(self, tmp_path):
        """Real ``_provider_api_key_present`` — NOT the hardcoded True stub."""
        config_path = tmp_path / "config.yaml"
        config_path.write_text(
            yaml.safe_dump({
                "model": {},
                "providers": {"mistralai": {"api_key": "legacy-record-canary"}},
            }),
            encoding="utf-8",
        )
        providers_cfg = {"mistralai": {"api_key": "legacy-record-canary"}}
        assert onboarding._provider_api_key_present(
            "mistral", {"providers": providers_cfg}, {}
        ), (
            "a stored providers.mistralai key must satisfy the canonical "
            "mistral selector"
        )

    def test_genuinely_missing_key_is_still_rejected(self):
        assert not onboarding._provider_api_key_present(
            "mistral", {"providers": {}}, {}
        ), "an absent key must never be treated as present"
        assert not onboarding._provider_api_key_present(
            "mistral", {"providers": {"mistral": {"api_key": "   "}}}, {}
        ), "a blank key must never be treated as present"

    def test_unrelated_record_key_does_not_satisfy(self):
        assert not onboarding._provider_api_key_present(
            "mistral",
            {"providers": {"openrouter": {"api_key": "other-record-canary"}}},
            {},
        ), "a different provider's key must not satisfy this selector"


class TestWholeRuntimeBundleOwnership:
    """P2-1 (review 2026-11-03): endpoint, key, PROTOCOL and pool must all come
    from ONE record.

    The earlier repair replaced only ``base_url`` and ``api_key``, leaving
    ``api_mode`` and ``credential_pool`` from the native result — which under a
    canonical+legacy collision can belong to the SIBLING. A caller then ran the
    owner's endpoint with the sibling's wire protocol. Both config orders and
    both requested spellings are covered.
    """

    _LEGACY = {
        "base_url": "https://legacy-relay.example/v1",
        "api_key": "legacy-owner-canary",
        "api_mode": "anthropic_messages",
    }
    _CANONICAL = {
        "base_url": "https://canonical-relay.example/v1",
        "api_key": "canonical-sibling-canary",
        "api_mode": "chat_completions",
    }

    def _bundle(self, providers, request_spelling):
        restore = _swap_in_test_config({"model": {}, "providers": providers})
        try:
            sibling = providers["mistral"]
            bundle = config.merge_custom_provider_runtime_bundle(
                request_spelling,
                sibling["api_key"],
                sibling["base_url"],
                {
                    "provider": "mistral",
                    "api_key": sibling["api_key"],
                    "base_url": sibling["base_url"],
                    "api_mode": sibling["api_mode"],
                },
            )
            return bundle
        finally:
            restore()

    @pytest.mark.parametrize("order", ["legacy-first", "canonical-first"])
    @pytest.mark.parametrize("spelling", ["mistral", "mistralai"])
    def test_endpoint_key_and_protocol_share_one_owner(self, order, spelling):
        providers = (
            {"mistralai": self._LEGACY, "mistral": self._CANONICAL}
            if order == "legacy-first"
            else {"mistral": self._CANONICAL, "mistralai": self._LEGACY}
        )
        owner = next(iter(providers.values()))  # config order decides
        bundle = self._bundle(providers, spelling)
        assert bundle["base_url"] == owner["base_url"], (
            f"{order}/{spelling}: endpoint came from the sibling "
            f"(got {bundle['base_url']!r}, owner {owner['base_url']!r})"
        )
        assert bundle["api_key"] == owner["api_key"], (
            f"{order}/{spelling}: credential came from the sibling "
            f"(got {bundle['api_key']!r}, owner {owner['api_key']!r})"
        )
        assert bundle["api_mode"] == owner["api_mode"], (
            f"{order}/{spelling}: PROTOCOL came from the sibling "
            f"(got {bundle['api_mode']!r}, owner {owner['api_mode']!r}) — "
            "the owner's endpoint must never run on the sibling's wire format"
        )


class TestNonCollidingCredentialAuthority:
    """P2-2 (review 2026-11-03): without a collision the resolver's credential
    authority must survive — an ambient or pool key is not ours to replace."""

    def test_ambient_key_is_not_replaced_by_the_inline_key(self):
        restore = _swap_in_test_config({
            "model": {},
            "providers": {
                "openrouter": {
                    "base_url": "https://openrouter.example/v1",
                    "api_key": "inline-record-canary",
                },
            },
        })
        try:
            bundle = config.merge_custom_provider_runtime_bundle(
                "openrouter",
                "ambient-resolver-canary",
                "https://openrouter.example/v1",
                {
                    "provider": "openrouter",
                    "api_key": "ambient-resolver-canary",
                    "base_url": "https://openrouter.example/v1",
                },
            )
        finally:
            restore()
        assert bundle["api_key"] == "ambient-resolver-canary", (
            "a non-colliding provider must keep the credential the resolver "
            f"selected; got {bundle['api_key']!r}"
        )

    def test_pool_key_is_not_replaced_by_the_inline_key(self):
        restore = _swap_in_test_config({
            "model": {},
            "providers": {
                "openrouter": {
                    "base_url": "https://openrouter.example/v1",
                    "api_key": "inline-record-canary",
                    "credential_pool": "openrouter-pool",
                },
            },
        })
        try:
            bundle = config.merge_custom_provider_runtime_bundle(
                "openrouter",
                "pool-resolver-canary",
                "https://openrouter.example/v1",
                {
                    "provider": "openrouter",
                    "api_key": "pool-resolver-canary",
                    "base_url": "https://openrouter.example/v1",
                    "credential_pool": "openrouter-pool",
                },
            )
        finally:
            restore()
        assert bundle["api_key"] == "pool-resolver-canary", (
            "a pool-selected credential must survive on a non-colliding record; "
            f"got {bundle['api_key']!r}"
        )

    def test_single_record_addressed_by_the_other_spelling_still_resolves(self):
        """No sibling exists, yet the canonical spelling resolves nothing —
        the record's own key must still be attached."""
        restore = _swap_in_test_config({
            "model": {},
            "providers": {
                "mistralai": {
                    "base_url": "https://legacy-relay.example/v1",
                    "api_key": "legacy-record-canary",
                },
            },
        })
        try:
            bundle = config.merge_custom_provider_runtime_bundle(
                "mistral",
                None,
                "https://legacy-relay.example/v1",
                {"provider": "mistral", "base_url": "https://legacy-relay.example/v1"},
            )
        finally:
            restore()
        assert bundle["api_key"] == "legacy-record-canary", (
            "the single record that owns this canonical slug declares a key, so "
            f"the bundle must carry it; got {bundle['api_key']!r}"
        )
