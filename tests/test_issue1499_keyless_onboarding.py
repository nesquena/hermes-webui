"""Regression: self-hosted providers accept empty api_key in onboarding (#1499 sub-bug 3).

Pre-fix, ``apply_onboarding_setup`` rejected an empty ``api_key`` for every
wizard provider with the error ``f"{env_var} is required"``. For LM Studio,
Ollama, and Custom — which run keyless on most local installs — this forced
users to type a placeholder string into the API key field just to clear the
wizard. The ``LMSTUDIO_NOAUTH_PLACEHOLDER`` substitution at chat-time was the
agent's workaround for the no-auth case, but the wizard side rejected the
empty input first, so users never got that far without typing gibberish.

The fix adds a ``key_optional: True`` flag to the affected providers in
``_SUPPORTED_PROVIDER_SETUPS``. When that flag is set:

  * ``apply_onboarding_setup`` skips the "key required" check.
  * No write to ``.env`` happens for the empty-key case (no
    ``LM_API_KEY=*** placeholder lying in the user's .env file`` either).
  * ``_status_from_runtime`` reports ``provider_ready=True`` based on
    ``base_url`` alone, so the wizard doesn't refire on the next page load
    just because there's no api_key.
  * The setup catalog exposes ``key_optional`` so the frontend can render
    "(optional)" hint copy + accept empty submit.

Symmetric tests verify the existing required-key path still rejects empty
api_keys for cloud providers (openrouter, anthropic, openai), so this fix
doesn't accidentally make every provider keyless.

Reporters: @chwps, @AdoneyGalvan via #1420 → split into #1499.
"""

from __future__ import annotations

import sys
import types

import pytest

import api.config as config
import api.profiles as profiles


def _install_fake_hermes_cli(monkeypatch):
    """Stub hermes_cli modules so tests are deterministic and offline.

    Mirrors the helper in test_provider_management.py — kept inline so this
    regression test stays self-contained.
    """
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


def _isolate_onboarding_writes(monkeypatch, tmp_path):
    """Redirect every onboarding write target to ``tmp_path`` and clear
    every relevant env var so the test starts from a known clean state.

    Pattern from webui-onboarding-provider-readiness skill — without this,
    tests that call ``apply_onboarding_setup`` directly write to the real
    ``~/.hermes`` and clobber the developer's actual config.
    """
    from api import onboarding as ob

    monkeypatch.setattr(ob, "_get_active_hermes_home", lambda: tmp_path)
    cfg_path = tmp_path / "config.yaml"
    monkeypatch.setattr(ob, "_get_config_path", lambda: cfg_path)
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)
    monkeypatch.delenv("HERMES_WEBUI_SKIP_ONBOARDING", raising=False)
    for var in (
        "LM_API_KEY",
        "LMSTUDIO_API_KEY",
        "OLLAMA_API_KEY",
        "OPENAI_API_KEY",
        "OPENROUTER_API_KEY",
        "ANTHROPIC_API_KEY",
        "GOOGLE_API_KEY",
        "GH_TOKEN",
        "GITHUB_TOKEN",
    ):
        monkeypatch.delenv(var, raising=False)
    return cfg_path


class TestKeyOptionalProviderSchema:
    """The catalog declares which providers may run keyless."""

    def test_lmstudio_is_key_optional(self):
        from api.onboarding import _SUPPORTED_PROVIDER_SETUPS

        assert _SUPPORTED_PROVIDER_SETUPS["lmstudio"].get("key_optional") is True, (
            "lmstudio must declare key_optional=True so onboarding accepts an "
            "empty api_key.  Pre-fix the wizard required users to type "
            "gibberish to clear the form.  See #1499 (third sub-bug from #1420)."
        )

    def test_ollama_is_key_optional(self):
        from api.onboarding import _SUPPORTED_PROVIDER_SETUPS

        assert _SUPPORTED_PROVIDER_SETUPS["ollama"].get("key_optional") is True, (
            "ollama must declare key_optional=True — local Ollama runs keyless "
            "by default."
        )

    def test_custom_is_key_optional(self):
        from api.onboarding import _SUPPORTED_PROVIDER_SETUPS

        assert _SUPPORTED_PROVIDER_SETUPS["custom"].get("key_optional") is True, (
            "custom must declare key_optional=True — many self-hosted "
            "OpenAI-compatible servers (vLLM, llama-server, TabbyAPI) run "
            "keyless behind a private network."
        )

    def test_cloud_providers_are_not_key_optional(self):
        """Regression-defense: openrouter/anthropic/openai must STILL require a key."""
        from api.onboarding import _SUPPORTED_PROVIDER_SETUPS

        for pid in ("openrouter", "anthropic", "openai"):
            assert not _SUPPORTED_PROVIDER_SETUPS[pid].get("key_optional"), (
                f"{pid} must NOT be key_optional — cloud providers always need "
                f"a real key.  This test catches an accidental flag flip."
            )

    def test_setup_catalog_exposes_key_optional_flag(self):
        """Frontend reads `provider.key_optional` from the catalog."""
        from api.onboarding import _build_setup_catalog

        catalog = _build_setup_catalog({"model": {"provider": "lmstudio"}})
        by_id = {p["id"]: p for p in catalog["providers"]}
        assert by_id["lmstudio"]["key_optional"] is True
        assert by_id["ollama"]["key_optional"] is True
        assert by_id["custom"]["key_optional"] is True
        assert by_id["openrouter"]["key_optional"] is False, (
            "Catalog must expose key_optional=False for cloud providers so "
            "the frontend doesn't accidentally label them optional."
        )


class TestKeylessOnboarding:
    """``apply_onboarding_setup`` accepts empty api_key for key_optional providers."""

    def test_lmstudio_empty_api_key_accepted(self, monkeypatch, tmp_path):
        """Pre-fix this raised; post-fix it succeeds and writes no .env entry."""
        _install_fake_hermes_cli(monkeypatch)
        cfg_path = _isolate_onboarding_writes(monkeypatch, tmp_path)

        from api import onboarding as ob

        # Empty api_key — should NOT raise.
        ob.apply_onboarding_setup(
            {
                "provider": "lmstudio",
                "model": "qwen3-27b",
                "base_url": "http://example.local:1234/v1",
                "api_key": "",
            }
        )

        # config.yaml gets written with provider/model/base_url
        assert cfg_path.exists()
        cfg_text = cfg_path.read_text(encoding="utf-8")
        assert "provider: lmstudio" in cfg_text
        assert "base_url: http://example.local:1234/v1" in cfg_text

        # .env should NOT have an API_KEY entry — empty key means we don't
        # write a placeholder into .env.
        env_path = tmp_path / ".env"
        if env_path.exists():
            env_text = env_path.read_text(encoding="utf-8")
            assert "LM_API_KEY=" not in env_text, (
                f"Onboarding wrote LM_API_KEY to .env even though user "
                f"submitted an empty api_key. .env contents:\n{env_text}"
            )
            assert "LMSTUDIO_API_KEY=" not in env_text

    def test_ollama_empty_api_key_accepted(self, monkeypatch, tmp_path):
        _install_fake_hermes_cli(monkeypatch)
        _isolate_onboarding_writes(monkeypatch, tmp_path)

        from api import onboarding as ob

        ob.apply_onboarding_setup(
            {
                "provider": "ollama",
                "model": "qwen3:32b",
                "base_url": "http://localhost:11434/v1",
                "api_key": "",
            }
        )
        env_path = tmp_path / ".env"
        if env_path.exists():
            assert "OLLAMA_API_KEY=" not in env_path.read_text(encoding="utf-8")

    def test_custom_empty_api_key_accepted(self, monkeypatch, tmp_path):
        _install_fake_hermes_cli(monkeypatch)
        _isolate_onboarding_writes(monkeypatch, tmp_path)

        from api import onboarding as ob

        ob.apply_onboarding_setup(
            {
                "provider": "custom",
                "model": "gpt-4o-mini",
                "base_url": "http://my-vllm.local/v1",
                "api_key": "",
            }
        )
        env_path = tmp_path / ".env"
        if env_path.exists():
            assert "OPENAI_API_KEY=" not in env_path.read_text(encoding="utf-8")

    def test_openrouter_empty_api_key_still_rejected(self, monkeypatch, tmp_path):
        """Cloud providers must still reject empty api_key (regression defense)."""
        _install_fake_hermes_cli(monkeypatch)
        _isolate_onboarding_writes(monkeypatch, tmp_path)

        from api import onboarding as ob

        with pytest.raises(ValueError, match="OPENROUTER_API_KEY is required"):
            ob.apply_onboarding_setup(
                {
                    "provider": "openrouter",
                    "model": "anthropic/claude-sonnet-4.6",
                    "base_url": "",
                    "api_key": "",
                }
            )

    def test_anthropic_empty_api_key_still_rejected(self, monkeypatch, tmp_path):
        _install_fake_hermes_cli(monkeypatch)
        _isolate_onboarding_writes(monkeypatch, tmp_path)

        from api import onboarding as ob

        with pytest.raises(ValueError, match="ANTHROPIC_API_KEY is required"):
            ob.apply_onboarding_setup(
                {
                    "provider": "anthropic",
                    "model": "claude-sonnet-4.6",
                    "api_key": "",
                }
            )

    def test_lmstudio_with_explicit_api_key_still_writes_env(
        self, monkeypatch, tmp_path
    ):
        """Auth-enabled LM Studio: user supplies a key, .env still gets written.

        Regression-defense for the keyless path: when the user DOES supply a
        key, we still write it under the canonical name (LM_API_KEY, post-#1500).
        """
        _install_fake_hermes_cli(monkeypatch)
        _isolate_onboarding_writes(monkeypatch, tmp_path)

        from api import onboarding as ob

        ob.apply_onboarding_setup(
            {
                "provider": "lmstudio",
                "model": "qwen3-27b",
                "base_url": "http://example.local:1234/v1",
                "api_key": "real-secret-token",
            }
        )
        env_text = (tmp_path / ".env").read_text(encoding="utf-8")
        assert "LM_API_KEY=" in env_text, (
            f"Auth-enabled lmstudio user supplied an api_key but .env doesn't "
            f"contain LM_API_KEY. Contents:\n{env_text}"
        )


class TestKeylessChatReady:
    """``provider_ready`` and ``chat_ready`` are True for key_optional providers."""

    def test_lmstudio_keyless_provider_ready_via_status_runtime(
        self,
        monkeypatch,
        tmp_path,
    ):
        """``_status_from_runtime`` returns provider_ready=True with no api_key."""
        _install_fake_hermes_cli(monkeypatch)
        _isolate_onboarding_writes(monkeypatch, tmp_path)

        from api import onboarding as ob

        cfg = {
            "model": {
                "provider": "lmstudio",
                "default": "qwen3-27b",
                "base_url": "http://example.local:1234/v1",
            },
        }
        status = ob._status_from_runtime(cfg, imports_ok=True)
        assert status.get("provider_ready") is True, (
            "lmstudio with base_url + model + NO api_key must be provider_ready=True. "
            "Otherwise the wizard refires on every page load even though the "
            "user finished setup. See #1499 third sub-bug from #1420."
        )

    def test_ollama_keyless_provider_ready_via_status_runtime(
        self,
        monkeypatch,
        tmp_path,
    ):
        _install_fake_hermes_cli(monkeypatch)
        _isolate_onboarding_writes(monkeypatch, tmp_path)

        from api import onboarding as ob

        cfg = {
            "model": {
                "provider": "ollama",
                "default": "qwen3:32b",
                "base_url": "http://localhost:11434/v1",
            },
        }
        status = ob._status_from_runtime(cfg, imports_ok=True)
        assert status.get("provider_ready") is True

    def test_custom_keyless_provider_ready_requires_base_url(
        self,
        monkeypatch,
        tmp_path,
    ):
        """custom is key_optional but still requires base_url."""
        _install_fake_hermes_cli(monkeypatch)
        _isolate_onboarding_writes(monkeypatch, tmp_path)

        from api import onboarding as ob

        # With base_url → ready
        cfg_with = {
            "model": {
                "provider": "custom",
                "default": "gpt-4o-mini",
                "base_url": "http://my-vllm.local/v1",
            },
        }
        assert (
            ob._status_from_runtime(cfg_with, imports_ok=True).get("provider_ready")
            is True
        )

        # Without base_url → NOT ready (custom still requires it)
        cfg_without = {
            "model": {
                "provider": "custom",
                "default": "gpt-4o-mini",
            },
        }
        assert (
            ob._status_from_runtime(cfg_without, imports_ok=True).get("provider_ready")
            is False
        ), (
            "custom is key_optional but still requires base_url — this test "
            "catches a regression where the requires_base_url check is "
            "accidentally dropped for key_optional providers."
        )

    def test_openrouter_keyless_provider_ready_is_false(self, monkeypatch, tmp_path):
        """Cloud provider with no key → provider_ready=False (regression defense)."""
        _install_fake_hermes_cli(monkeypatch)
        _isolate_onboarding_writes(monkeypatch, tmp_path)

        from api import onboarding as ob

        cfg = {
            "model": {
                "provider": "openrouter",
                "default": "anthropic/claude-sonnet-4.6",
            },
        }
        assert (
            ob._status_from_runtime(cfg, imports_ok=True).get("provider_ready") is False
        ), (
            "openrouter with no api_key must NOT be provider_ready — that "
            "would silently let the wizard finish without the user actually "
            "entering a key."
        )

    def test_lmstudio_keyless_chat_ready_via_full_status(self, monkeypatch, tmp_path):
        """End-to-end: get_onboarding_status reports chat_ready=True after keyless save."""
        _install_fake_hermes_cli(monkeypatch)
        cfg_path = _isolate_onboarding_writes(monkeypatch, tmp_path)

        from api import onboarding as ob

        ob.apply_onboarding_setup(
            {
                "provider": "lmstudio",
                "model": "qwen3-27b",
                "base_url": "http://example.local:1234/v1",
                "api_key": "",
            }
        )

        # Reload config so get_onboarding_status sees the just-written values.
        # _swap_in_test_config-style — replicate just enough of that pattern.
        old_cfg = dict(config.cfg)
        old_mtime = config._cfg_mtime
        config.cfg.clear()
        try:
            import yaml

            config.cfg.update(
                yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
            )
        except Exception:
            pass
        try:
            config._cfg_mtime = cfg_path.stat().st_mtime
        except Exception:
            config._cfg_mtime = 0.0

        try:
            status = ob.get_onboarding_status()
            system = status.get("system", {})
            assert system.get("provider_ready") is True, (
                f"After saving lmstudio keyless config, provider_ready must be "
                f"True. Got: provider_ready={system.get('provider_ready')!r}, "
                f"chat_ready={system.get('chat_ready')!r}."
            )
            # chat_ready additionally requires _HERMES_FOUND + imports_ok which
            # depend on the test environment; provider_ready is the bit this
            # PR's fix actually controls.  But if hermes is importable, it
            # should also be chat_ready.
            if system.get("imports_ok"):
                assert system.get("chat_ready") is True
        finally:
            config.cfg.clear()
            config.cfg.update(old_cfg)
            config._cfg_mtime = old_mtime
