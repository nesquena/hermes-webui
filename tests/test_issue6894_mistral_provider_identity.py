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
