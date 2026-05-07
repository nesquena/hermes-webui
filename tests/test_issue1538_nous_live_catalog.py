"""Regression tests for #1538 — Nous Portal model picker should live-fetch
the full catalog (~30 models) instead of returning the four-entry static list.

Background
----------
Settings → Default Model showed only four Nous models (Claude Opus 4.6, Claude
Sonnet 4.6, GPT-5.4 Mini, Gemini 3.1 Pro Preview) because
``_build_available_models_uncached()`` fell through to the generic
``pid in _PROVIDER_MODELS`` branch and returned ``copy.deepcopy(_PROVIDER_MODELS["nous"])``.
The actual Nous Portal catalog has ~30 models live — including the latest
Anthropic 4.7 family, GPT-5.5, Gemini 3.1 Pro/Flash, Kimi K2.6, MiniMax M2.7,
several Xiaomi/Tencent/StepFun entries.

Fix
---
A dedicated ``elif pid == "nous":`` branch in ``_build_available_models_uncached()``
mirroring the Ollama Cloud pattern: live-fetch via
``hermes_cli.models.provider_model_ids("nous")``, prefix every id with ``@nous:``
to match the existing routing convention, fall back to the curated static
list when ``hermes_cli`` is unavailable.
"""

from __future__ import annotations

import sys
import types

import api.config as config
import api.profiles as profiles


# Sample Nous catalog used in the live-fetch test. Mirrors the shape returned
# by hermes_cli.models.provider_model_ids("nous") (see #1538 issue body).
SAMPLE_NOUS_LIVE_IDS = [
    "moonshotai/kimi-k2.6",
    "xiaomi/mimo-v2.5-pro",
    "anthropic/claude-opus-4.7",
    "anthropic/claude-opus-4.6",
    "anthropic/claude-sonnet-4.6",
    "anthropic/claude-haiku-4.5",
    "openai/gpt-5.5",
    "openai/gpt-5.4-mini",
    "openai/gpt-5.3-codex",
    "google/gemini-3-pro-preview",
    "google/gemini-3.1-pro-preview",
    "google/gemini-3.1-flash-lite-preview",
    "qwen/qwen3.5-plus-02-15",
    "minimax/minimax-m2.7",
    "z-ai/glm-5.1",
    "x-ai/grok-4.20-beta",
    "tencent/hy3-preview",
    "stepfun/step-3.5-flash",
    "nvidia/nemotron-3-super-120b-a12b",
    "arcee-ai/trinity-large-thinking",
]


def _install_fake_hermes_cli(monkeypatch, *, nous_ids=None, raise_on_lookup=False):
    """Install fake ``hermes_cli`` modules so detection sees Nous as authenticated
    and ``provider_model_ids("nous")`` returns the desired catalog.

    Mirrors :func:`tests.test_issue1420_lmstudio_provider_env_var._install_fake_hermes_cli`
    but specialised for Nous detection (Nous is OAuth so the env-var path
    is not used — we drive detection via ``hermes_cli.auth.list_auth_providers``).
    """
    fake_pkg = types.ModuleType("hermes_cli")
    fake_pkg.__path__ = []

    fake_models = types.ModuleType("hermes_cli.models")
    fake_models.list_available_providers = lambda: []
    if raise_on_lookup:
        def _raise(_pid):
            raise RuntimeError("simulated hermes_cli failure")
        fake_models.provider_model_ids = _raise
    else:
        ids = list(nous_ids) if nous_ids is not None else []
        fake_models.provider_model_ids = lambda pid: ids if pid == "nous" else []

    fake_auth = types.ModuleType("hermes_cli.auth")

    def _list_auth_providers():
        return [{"id": "nous", "authenticated": True}]

    def _get_auth_status(pid):
        return {"logged_in": True, "key_source": ""} if pid == "nous" else {}

    fake_auth.list_auth_providers = _list_auth_providers
    fake_auth.get_auth_status = _get_auth_status

    monkeypatch.setitem(sys.modules, "hermes_cli", fake_pkg)
    monkeypatch.setitem(sys.modules, "hermes_cli.models", fake_models)
    monkeypatch.setitem(sys.modules, "hermes_cli.auth", fake_auth)
    monkeypatch.delitem(sys.modules, "agent.credential_pool", raising=False)
    monkeypatch.delitem(sys.modules, "agent", raising=False)

    config.invalidate_models_cache()


def _swap_in_test_config(extra_cfg):
    """Snapshot config.cfg, replace with a minimal test config; return restore-fn."""
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


def _scrub_provider_env(monkeypatch):
    """Drop every provider env var so detection only sees what we install
    via the fake hermes_cli stubs (not unrelated keys leaked from the runner)."""
    for var in (
        "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GOOGLE_API_KEY", "GEMINI_API_KEY",
        "DEEPSEEK_API_KEY", "XAI_API_KEY", "GROQ_API_KEY",
        "MISTRAL_API_KEY", "OPENROUTER_API_KEY",
        "OLLAMA_CLOUD_API_KEY", "OLLAMA_API_KEY",
        "GLM_API_KEY", "KIMI_API_KEY", "MOONSHOT_API_KEY",
        "MINIMAX_API_KEY", "MINIMAX_CN_API_KEY",
        "OPENCODE_ZEN_API_KEY", "OPENCODE_GO_API_KEY",
        "NOUS_API_KEY", "NVIDIA_API_KEY", "LM_API_KEY", "LMSTUDIO_API_KEY",
    ):
        monkeypatch.delenv(var, raising=False)


class TestNousLiveCatalog:
    """When the Nous live catalog is available, the dropdown must surface it
    in full (>=20 entries) — not the four-entry static fallback (#1538)."""

    def test_nous_models_live_fetch_when_hermes_cli_available(self, monkeypatch, tmp_path):
        _scrub_provider_env(monkeypatch)
        _install_fake_hermes_cli(monkeypatch, nous_ids=SAMPLE_NOUS_LIVE_IDS)
        monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)

        restore = _swap_in_test_config({"model": {"provider": "nous"}})
        try:
            data = config.get_available_models()
            nous_groups = [g for g in data.get("groups", []) if g.get("provider_id") == "nous"]
            assert len(nous_groups) == 1, (
                f"Expected exactly one Nous group, got {len(nous_groups)}: "
                f"{[g.get('provider_id') for g in data.get('groups', [])]}"
            )
            models = nous_groups[0]["models"]
            assert len(models) >= 20, (
                f"Live-fetched Nous catalog should expose >=20 entries, got "
                f"{len(models)}. The dispatch branch fell through to the four-entry "
                f"static list — pre-#1538 behaviour."
            )
        finally:
            restore()

    def test_nous_model_ids_carry_at_nous_prefix(self, monkeypatch, tmp_path):
        _scrub_provider_env(monkeypatch)
        _install_fake_hermes_cli(monkeypatch, nous_ids=SAMPLE_NOUS_LIVE_IDS)
        monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)

        restore = _swap_in_test_config({"model": {"provider": "nous"}})
        try:
            data = config.get_available_models()
            nous_group = next(g for g in data["groups"] if g["provider_id"] == "nous")
            for m in nous_group["models"]:
                assert m["id"].startswith("@nous:"), (
                    f"Every Nous model id must start with '@nous:' so "
                    f"resolve_model_provider routes through the explicit-provider-hint "
                    f"branch (matches the static-list invariant from "
                    f"tests/test_nous_portal_routing.py). Got: {m['id']!r}"
                )
        finally:
            restore()

    def test_nous_labels_carry_via_nous_suffix(self, monkeypatch, tmp_path):
        _scrub_provider_env(monkeypatch)
        _install_fake_hermes_cli(monkeypatch, nous_ids=SAMPLE_NOUS_LIVE_IDS)
        monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)

        restore = _swap_in_test_config({"model": {"provider": "nous"}})
        try:
            data = config.get_available_models()
            nous_group = next(g for g in data["groups"] if g["provider_id"] == "nous")
            for m in nous_group["models"]:
                assert m["label"].endswith(" (via Nous)"), (
                    f"Every Nous live-fetched label must end with ' (via Nous)' so "
                    f"the user can distinguish them from same-named direct-provider "
                    f"entries (e.g. 'Claude Opus 4.7' via direct Anthropic). "
                    f"Got: {m['label']!r}"
                )
        finally:
            restore()

    def test_nous_live_catalog_includes_recent_models(self, monkeypatch, tmp_path):
        """Sanity: the recent-flagship models from the user's bug report
        (Claude Opus 4.7, GPT-5.5, Kimi K2.6) must reach the dropdown."""
        _scrub_provider_env(monkeypatch)
        _install_fake_hermes_cli(monkeypatch, nous_ids=SAMPLE_NOUS_LIVE_IDS)
        monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)

        restore = _swap_in_test_config({"model": {"provider": "nous"}})
        try:
            data = config.get_available_models()
            nous_group = next(g for g in data["groups"] if g["provider_id"] == "nous")
            ids = {m["id"] for m in nous_group["models"]}
            for required in (
                "@nous:anthropic/claude-opus-4.7",
                "@nous:openai/gpt-5.5",
                "@nous:moonshotai/kimi-k2.6",
                "@nous:google/gemini-3.1-pro-preview",
                "@nous:minimax/minimax-m2.7",
            ):
                assert required in ids, (
                    f"{required} missing from live-fetched Nous catalog. Either "
                    f"the hermes_cli dispatch is broken or the @nous: prefix is "
                    f"missing."
                )
        finally:
            restore()


class TestNousStaticFallback:
    """When ``hermes_cli`` is not importable or its lookup raises, we fall back
    to the curated four-entry static list — never empty."""

    def test_static_fallback_when_hermes_cli_raises(self, monkeypatch, tmp_path):
        _scrub_provider_env(monkeypatch)
        _install_fake_hermes_cli(monkeypatch, raise_on_lookup=True)
        monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)

        restore = _swap_in_test_config({"model": {"provider": "nous"}})
        try:
            data = config.get_available_models()
            nous_groups = [g for g in data.get("groups", []) if g.get("provider_id") == "nous"]
            assert nous_groups, (
                "Nous group must still appear when hermes_cli fails — the "
                "branch should fall back to the curated static list."
            )
            models = nous_groups[0]["models"]
            assert len(models) == 4, (
                f"Static fallback should expose exactly the four curated entries "
                f"in _PROVIDER_MODELS['nous']. Got {len(models)}: "
                f"{[m['id'] for m in models]}"
            )
            for m in models:
                assert m["id"].startswith("@nous:"), m["id"]
        finally:
            restore()


class TestFormatNousLabel:
    """Unit tests for the label formatter helper."""

    def test_strips_vendor_namespace(self):
        from api.config import _format_nous_label
        assert _format_nous_label("anthropic/claude-opus-4.7") == "Claude Opus 4.7 (via Nous)"
        assert _format_nous_label("openai/gpt-5.4-mini") == "GPT 5.4 Mini (via Nous)"

    def test_handles_missing_vendor(self):
        from api.config import _format_nous_label
        # Defensive: id without slash should still render a sane label.
        assert _format_nous_label("kimi-k2.6") == "Kimi K2.6 (via Nous)"

    def test_handles_variant_after_colon(self):
        from api.config import _format_nous_label
        # Variant rendered in parentheses, mirroring _format_ollama_label.
        out = _format_nous_label("minimax/minimax-m2.5:free")
        assert out.endswith(" (via Nous)")
        assert "Free" in out
        assert "MiniMax M2.5" in out

    def test_minimax_renders_mixed_case(self):
        from api.config import _format_nous_label
        # Live wire returns lowercase 'minimax/minimax-...' but the curated
        # convention is mixed-case 'MiniMax'.
        assert _format_nous_label("minimax/minimax-m2.7").startswith("MiniMax M2.7")

    def test_label_always_ends_with_via_nous_suffix(self):
        from api.config import _format_nous_label
        for sample in [
            "anthropic/claude-opus-4.7",
            "openai/gpt-5.5",
            "google/gemini-3.1-pro-preview",
            "moonshotai/kimi-k2.6",
            "z-ai/glm-5.1",
            "stepfun/step-3.5-flash",
        ]:
            assert _format_nous_label(sample).endswith(" (via Nous)"), sample


class TestStaticListPreservedAsFallback:
    """The curated ``_PROVIDER_MODELS['nous']`` entry stays as the static
    fallback; existing routing invariants from
    :mod:`tests.test_nous_portal_routing` must remain valid."""

    def test_static_list_present(self):
        from api.config import _PROVIDER_MODELS
        assert _PROVIDER_MODELS.get("nous"), (
            "The curated static Nous list must remain in _PROVIDER_MODELS as "
            "a fallback for environments where hermes_cli is unavailable."
        )

    def test_static_list_keeps_at_nous_prefix(self):
        # Keep parity with tests/test_nous_portal_routing.py — ensures the
        # static fallback path produces correctly-routable ids when used.
        from api.config import _PROVIDER_MODELS
        for m in _PROVIDER_MODELS["nous"]:
            assert m["id"].startswith("@nous:"), m["id"]
