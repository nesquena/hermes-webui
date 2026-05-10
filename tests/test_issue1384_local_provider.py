"""Regression tests for #1384 — ``provider: "local"`` mid-conversation crash.

Earlier WebUI builds auto-detected unknown loopback hosts and persisted
``provider: "local"`` to ``config.yaml``. That value is not a registered
provider in ``hermes_cli.auth.PROVIDER_REGISTRY``, so the agent's auxiliary
client (compression, vision, web extraction) raised
``"Provider 'local' is set in config.yaml but no API key was found"``
mid-conversation when the context-compression threshold was hit.

The fix is three layers deep so a user with an already-broken config gets
healed automatically:

1. The auto-detect block now writes ``provider: "custom"`` for unknown
   loopback hosts (``custom`` is the canonical generic-OpenAI-compat
   provider, and the agent's auxiliary client takes the
   ``no-key-required`` path for it).
2. ``resolve_model_provider()`` rewrites legacy ``"local"`` to ``"custom"``
   at read time, so existing configs route correctly without requiring the
   user to edit ``config.yaml`` by hand.
3. ``set_hermes_default_model()`` refuses to persist ``"local"`` going
   forward, so any other code path that tries to write it is also healed.
4. The local alias table has ``"local" → "custom"`` for any consumer that
   normalises through ``_resolve_provider_alias``.
"""

import re
from pathlib import Path


import api.config as cfg


# ── 1. Auto-detect block writes ``custom``, not ``local`` ────────────────


class TestAutoDetectWritesCustom:
    """The auto-detect branch in ``_build_available_models_uncached`` must
    never write ``provider = "local"`` because that value breaks the
    auxiliary client mid-conversation."""

    def test_source_code_no_local_assignment(self):
        """The string ``provider = "local"`` must not appear in api/config.py."""
        src = Path(cfg.__file__).read_text(encoding="utf-8")
        assert 'provider = "local"' not in src, (
            'api/config.py must not assign provider = "local" — see #1384. '
            "Use ``custom`` instead so the agent's auxiliary client takes the "
            "``no-key-required`` OpenAI-compat path."
        )

    def test_auto_detect_branch_uses_custom(self):
        """The else-branch in the auto-detect block resolves to ``custom``."""
        src = Path(cfg.__file__).read_text(encoding="utf-8")
        # Find the auto-detect block (host-keyword classifier).
        m = re.search(
            r'if "ollama" in host or "127\.0\.0\.1" in host or "localhost" in host:\s*\n'
            r'\s*provider = "ollama"\s*\n'
            r'\s*elif "lmstudio" in host or "lm-studio" in host:\s*\n'
            r'\s*provider = "lmstudio"\s*\n'
            r"\s*else:",
            src,
        )
        assert m, "Auto-detect host-classifier block not found in api/config.py"
        # Find the next provider assignment after the else.
        tail = src[m.end() : m.end() + 1500]
        provider_assign = re.search(r'provider = "([a-z-]+)"', tail)
        assert provider_assign, "No provider assignment found after auto-detect else"
        assert provider_assign.group(1) == "custom", (
            f'Auto-detect else branch must assign provider = "custom", '
            f"got {provider_assign.group(1)!r}"
        )


# ── 2. resolve_model_provider() heals legacy ``local`` configs ───────────


class TestResolveModelProviderHealsLegacyLocal:
    """Existing ``config.yaml`` files with ``provider: local`` (written by
    earlier WebUI builds) must be normalised to ``custom`` at read time so
    downstream agent calls take the working path."""

    def test_provider_local_normalised_to_custom(self, tmp_path, monkeypatch):
        cfgfile = tmp_path / "config.yaml"
        cfgfile.write_text(
            "model:\n"
            "  default: qwen2.5-coder:14b\n"
            "  provider: local\n"
            "  base_url: http://127.0.0.1:11434/v1\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(cfg, "_get_config_path", lambda: cfgfile)
        cfg.reload_config()
        try:
            model_id, provider, base_url = cfg.resolve_model_provider(
                "qwen2.5-coder:14b"
            )
            assert provider == "custom", (
                f"resolve_model_provider must rewrite legacy 'local' to 'custom', "
                f"got {provider!r}"
            )
            assert base_url == "http://127.0.0.1:11434/v1"
            assert model_id == "qwen2.5-coder:14b"
        finally:
            cfg.reload_config()

    def test_provider_local_uppercase_also_normalised(self, tmp_path, monkeypatch):
        """Case-insensitive match — YAML may have ``Local`` or ``LOCAL``."""
        cfgfile = tmp_path / "config.yaml"
        cfgfile.write_text(
            "model:\n"
            "  default: my-model\n"
            "  provider: Local\n"
            "  base_url: http://192.168.1.10:8000/v1\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(cfg, "_get_config_path", lambda: cfgfile)
        cfg.reload_config()
        try:
            _, provider, _ = cfg.resolve_model_provider("my-model")
            assert provider == "custom"
        finally:
            cfg.reload_config()

    def test_other_providers_pass_through_unchanged(self, tmp_path, monkeypatch):
        """The migration must not touch any other provider name."""
        cfgfile = tmp_path / "config.yaml"
        cfgfile.write_text(
            "model:\n  default: claude-sonnet-4.6\n  provider: anthropic\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(cfg, "_get_config_path", lambda: cfgfile)
        cfg.reload_config()
        try:
            _, provider, _ = cfg.resolve_model_provider("claude-sonnet-4.6")
            assert provider == "anthropic"
        finally:
            cfg.reload_config()


# ── 3. set_hermes_default_model never persists 'local' ───────────────────


class TestSetHermesDefaultModelNeverPersistsLocal:
    """Even if a caller (or a stale resolver) hands us ``provider='local'``,
    we must not write that value back to ``config.yaml``."""

    def test_existing_local_provider_is_replaced_on_save(self, tmp_path, monkeypatch):
        """Writing a new default model when previous_provider is 'local'
        must persist 'custom' instead, because previous_provider falls
        through into persisted_provider when resolve_model_provider doesn't
        return a new provider hint."""
        import yaml

        cfgfile = tmp_path / "config.yaml"
        cfgfile.write_text(
            "model:\n"
            "  default: old-model\n"
            "  provider: local\n"
            "  base_url: http://127.0.0.1:11434/v1\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(cfg, "_get_config_path", lambda: cfgfile)
        cfg.reload_config()
        try:
            cfg.set_hermes_default_model("qwen2.5-coder:14b")
            saved = yaml.safe_load(cfgfile.read_text(encoding="utf-8"))
            persisted = saved.get("model", {}).get("provider", "")
            assert persisted != "local", (
                f"set_hermes_default_model must rewrite 'local' on save — "
                f"got {persisted!r}"
            )
            assert persisted == "custom"
        finally:
            cfg.reload_config()


# ── 4. Alias table has the entry ─────────────────────────────────────────


class TestAliasTableHasLocalEntry:
    """``_resolve_provider_alias`` must rewrite ``local`` → ``custom`` for
    any other code path that normalises through the alias table."""

    def test_local_alias_resolves_to_custom(self):
        assert cfg._resolve_provider_alias("local") == "custom"

    def test_local_alias_case_insensitive(self):
        assert cfg._resolve_provider_alias("LOCAL") == "custom"
        assert cfg._resolve_provider_alias("Local") == "custom"

    def test_alias_table_contains_local_entry(self):
        assert cfg._PROVIDER_ALIASES.get("local") == "custom", (
            "_PROVIDER_ALIASES must map 'local' → 'custom' for #1384"
        )
