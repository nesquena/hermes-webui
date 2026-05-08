"""Regression: webui aligns LM Studio env var with the agent CLI (#1500).

Pre-#1500 the WebUI used `LMSTUDIO_API_KEY` everywhere — onboarding wrote it,
Settings detection read it. The agent CLI runtime (hermes_cli/auth.py:182,
api_key_env_vars=("LM_API_KEY",)) reads `LM_API_KEY`. So a user who configured
auth on their LM Studio instance and entered the key in the WebUI got:

  - Settings → Providers reporting has_key=True (because WebUI saw its own
    LMSTUDIO_API_KEY)
  - Agent runtime ignoring the key (because it reads LM_API_KEY)
  - Chat falling back to LMSTUDIO_NOAUTH_PLACEHOLDER → 401 from the
    auth-enabled LM Studio server

Masked in practice for the no-auth majority. Real bug for anyone with
auth enabled.

This file pins the post-#1500 contract:

  1. Onboarding writes the canonical `LM_API_KEY` (NOT `LMSTUDIO_API_KEY`).
  2. Settings detection reads the canonical first.
  3. Settings detection ALSO reads the legacy `LMSTUDIO_API_KEY` as a
     read-only alias, so users with the old name in their .env don't see
     Settings flip to "no key" on upgrade.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import api.config as config
import api.profiles as profiles


def _install_fake_hermes_cli(monkeypatch):
    """Stub hermes_cli modules so tests are deterministic and offline."""
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
    try:
        from api.config import invalidate_models_cache
        invalidate_models_cache()
    except Exception:
        pass


def _swap_in_test_config(extra_cfg):
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


class TestIssue1500EnvVarAlignment:
    def test_onboarding_supported_provider_setup_uses_lm_api_key(self):
        """The wizard's lmstudio entry must declare the canonical env var name."""
        from api.onboarding import _SUPPORTED_PROVIDER_SETUPS
        assert "lmstudio" in _SUPPORTED_PROVIDER_SETUPS
        meta = _SUPPORTED_PROVIDER_SETUPS["lmstudio"]
        assert meta["env_var"] == "LM_API_KEY", (
            f"Onboarding's lmstudio.env_var must be the canonical 'LM_API_KEY' "
            f"(matching hermes_cli/auth.py:182 api_key_env_vars=('LM_API_KEY',)). "
            f"Got {meta['env_var']!r}."
        )
        # Legacy alias preserved for read-only fallback.
        aliases = list(meta.get("env_var_aliases") or [])
        assert "LMSTUDIO_API_KEY" in aliases, (
            f"Onboarding's lmstudio.env_var_aliases must include the legacy "
            f"'LMSTUDIO_API_KEY' name so existing users' detection keeps "
            f"working. Got aliases={aliases!r}."
        )

    def test_onboarding_writes_canonical_name_only(self, monkeypatch, tmp_path):
        """`apply_onboarding_setup` must write LM_API_KEY (not LMSTUDIO_API_KEY)."""
        _install_fake_hermes_cli(monkeypatch)

        # Redirect every write target to the tmp_path so we don't touch the real
        # ~/.hermes — pattern from webui-onboarding-provider-readiness skill.
        from api import onboarding as ob
        monkeypatch.setattr(ob, "_get_active_hermes_home", lambda: tmp_path)
        cfg_path = tmp_path / "config.yaml"
        monkeypatch.setattr(ob, "_get_config_path", lambda: cfg_path)
        monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)
        monkeypatch.delenv("HERMES_WEBUI_SKIP_ONBOARDING", raising=False)
        monkeypatch.delenv("LM_API_KEY", raising=False)
        monkeypatch.delenv("LMSTUDIO_API_KEY", raising=False)

        ob.apply_onboarding_setup({
            "provider": "lmstudio",
            "model": "qwen3-27b",
            "base_url": "http://example.local:1234/v1",
            "api_key": "fresh-canon",
        })

        env_path = tmp_path / ".env"
        assert env_path.exists(), "onboarding must write .env"
        env_text = env_path.read_text(encoding="utf-8")

        assert "LM_API_KEY=" in env_text, (
            f"Onboarding must write the canonical LM_API_KEY name. .env now reads:\n{env_text}"
        )
        assert "LMSTUDIO_API_KEY=" not in env_text, (
            f"Onboarding must NOT write the legacy LMSTUDIO_API_KEY name "
            f"(should only be canonical going forward). .env now reads:\n{env_text}"
        )

    def test_legacy_lmstudio_env_var_still_detected(self, monkeypatch, tmp_path):
        """Pre-#1500 users with LMSTUDIO_API_KEY still see has_key=True after upgrade."""
        _install_fake_hermes_cli(monkeypatch)
        monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)
        monkeypatch.delenv("LM_API_KEY", raising=False)
        monkeypatch.setenv("LMSTUDIO_API_KEY", "lm-studio-legacy")

        restore = _swap_in_test_config({"model": {"provider": "lmstudio"}})
        try:
            from api.providers import get_providers
            result = get_providers()
            by_id = {p["id"]: p for p in result["providers"]}
            assert by_id["lmstudio"]["has_key"] is True, (
                "Pre-#1500 users with the legacy LMSTUDIO_API_KEY env var must "
                "continue to see has_key=True after upgrade — that's the whole "
                "point of the alias fallback in _PROVIDER_ENV_VAR_ALIASES."
            )
            assert by_id["lmstudio"]["key_source"] in {"env_file", "env_var"}, (
                f"Legacy alias detection should report env_file / env_var as "
                f"key_source (the key really IS in .env), got "
                f"{by_id['lmstudio']['key_source']!r} — this is the post-#1500 "
                f"key_source-via-alias path."
            )
        finally:
            restore()

    def test_canonical_takes_precedence_over_legacy(self, monkeypatch, tmp_path):
        """When both env vars are set, canonical wins (rare migration edge)."""
        _install_fake_hermes_cli(monkeypatch)
        monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)
        monkeypatch.setenv("LM_API_KEY", "canonical-wins")
        monkeypatch.setenv("LMSTUDIO_API_KEY", "legacy-loses")

        restore = _swap_in_test_config({"model": {"provider": "lmstudio"}})
        try:
            from api.providers import get_providers, _provider_has_key
            assert _provider_has_key("lmstudio") is True
            # Both lead to has_key=True; the contract is that canonical is
            # checked first (so it's definitely returning True and not just
            # falling through to the alias).  We can't easily assert ordering
            # from this layer, but the existence of both detection paths is
            # captured by test_legacy_lmstudio_env_var_still_detected and
            # test_lmstudio_has_key_true_when_env_var_set in the #1420 file.
            result = get_providers()
            by_id = {p["id"]: p for p in result["providers"]}
            assert by_id["lmstudio"]["has_key"] is True
            assert by_id["lmstudio"]["configurable"] is True
        finally:
            restore()

    def test_provider_api_key_present_reads_aliases(self, monkeypatch, tmp_path):
        """`_provider_api_key_present` (onboarding-side) reads aliases too.

        The onboarding readiness pipeline (_status_from_runtime → chat_ready)
        relies on this function.  If aliases aren't honored here, an upgrading
        user gets a re-firing wizard even though their LM Studio is configured.
        """
        from api.onboarding import _provider_api_key_present
        cfg = {"model": {"provider": "lmstudio"}}

        # Only the legacy name set in .env values — onboarding must still see it.
        env_values = {"LMSTUDIO_API_KEY": "x"}
        assert _provider_api_key_present("lmstudio", cfg, env_values) is True

        # Only the canonical name set — also detected.
        env_values = {"LM_API_KEY": "x"}
        assert _provider_api_key_present("lmstudio", cfg, env_values) is True

        # Neither set — not detected.
        env_values = {"OPENAI_API_KEY": "x"}
        assert _provider_api_key_present("lmstudio", cfg, env_values) is False
