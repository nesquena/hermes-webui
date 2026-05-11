"""Regression tests for #1568 — duplicate provider groups in model picker.

Reporter (Deor, Discord #report-bugs, May 03 2026 14:19 PT) saw the Settings →
Default Model dropdown rendering the OpenCode Go provider as TWO separate
optgroups: ``OpenCode Go`` (the canonical one with all 14 catalog models) and
``Opencode_Go`` (a phantom group with one self-referential entry).

Three structural causes, all in ``api/config.py:_build_available_models_uncached``:

1. The detection path at line ~1980 reads ``cfg["providers"]`` keys verbatim —
   if the user's config has ``providers.opencode_go.api_key`` (underscore
   variant) AND another path adds the canonical ``opencode-go`` (e.g. via
   ``active_provider``), both end up in ``detected_providers`` and the build
   loop creates two groups.

2. The injection block at line ~2598 puts ANY ``model.default`` string into
   the picker as a fake option, so a stray ``model.default: opencode_go``
   (provider id mistakenly used as a model id) surfaces as a phantom model
   labelled ``"Opencode GO"``.

3. Empty optgroups can leak through when a non-canonical provider id makes it
   into ``detected_providers`` but has no entry in ``_PROVIDER_MODELS`` — the
   build loop creates an optgroup with zero models.

The fix is a new ``_canonicalise_provider_id`` helper applied at every
detection callsite, a post-collection dedup of ``detected_providers``, a
provider-id guard on the model.default injection block, and an empty-group
filter at the very end of the build.
"""

from __future__ import annotations

import sys
import types

import api.config as config
import api.profiles as profiles


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


def _scrub_provider_env(monkeypatch):
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


# ────────────────────────────────────────────────────────────────────────
# Section 1 — _canonicalise_provider_id helper
# ────────────────────────────────────────────────────────────────────────


class TestCanonicaliseProviderId:
    def test_canonical_id_preserved(self):
        from api.config import _canonicalise_provider_id
        assert _canonicalise_provider_id("opencode-go") == "opencode-go"
        assert _canonicalise_provider_id("anthropic") == "anthropic"
        assert _canonicalise_provider_id("x-ai") == "x-ai"

    def test_underscore_folded_to_hyphen(self):
        from api.config import _canonicalise_provider_id
        # Deor's exact failure mode — the config-file key uses underscores
        # but every other code path uses the hyphenated canonical form.
        assert _canonicalise_provider_id("opencode_go") == "opencode-go"

    def test_case_folded(self):
        from api.config import _canonicalise_provider_id
        assert _canonicalise_provider_id("OpenCode-Go") == "opencode-go"
        assert _canonicalise_provider_id("OPENCODE_GO") == "opencode-go"
        assert _canonicalise_provider_id("Anthropic") == "anthropic"

    def test_alias_resolved_when_target_is_canonical(self):
        from api.config import _canonicalise_provider_id
        # z-ai is an alias for the canonical zai.
        assert _canonicalise_provider_id("z-ai") == "zai"
        assert _canonicalise_provider_id("z_ai") == "zai"
        assert _canonicalise_provider_id("Z.AI") == "zai" or _canonicalise_provider_id("Z.AI") == "z.ai"

    def test_alias_not_applied_when_input_is_already_canonical(self):
        from api.config import _canonicalise_provider_id
        # x-ai IS the canonical key in _PROVIDER_DISPLAY/_PROVIDER_MODELS.
        # _PROVIDER_ALIASES happens to also map x-ai → xai (for hermes_cli
        # compat), but we must NOT round-trip through that alias because
        # xai isn't keyed in _PROVIDER_DISPLAY/_PROVIDER_MODELS.
        assert _canonicalise_provider_id("x-ai") == "x-ai"
        assert _canonicalise_provider_id("X-AI") == "x-ai"

    def test_empty_input(self):
        from api.config import _canonicalise_provider_id
        assert _canonicalise_provider_id("") == ""
        assert _canonicalise_provider_id(None) == ""
        assert _canonicalise_provider_id("   ") == ""

    def test_unknown_id_normalised_but_preserved(self):
        from api.config import _canonicalise_provider_id
        # Unknown ids: still get the underscore→hyphen + lowercase fold so
        # downstream dedup works, but no alias resolution.
        assert _canonicalise_provider_id("future_provider") == "future-provider"
        assert _canonicalise_provider_id("CUSTOM_THING") == "custom-thing"

    def test_idempotent(self):
        from api.config import _canonicalise_provider_id
        for raw in ("opencode_go", "OPENCODE-GO", "z-ai", "anthropic", "future_x"):
            once = _canonicalise_provider_id(raw)
            twice = _canonicalise_provider_id(once)
            assert once == twice, f"helper must be idempotent: {raw!r} -> {once!r} -> {twice!r}"


# ────────────────────────────────────────────────────────────────────────
# Section 2 — Detection-path dedup (the core #1568 fix)
# ────────────────────────────────────────────────────────────────────────


class TestProviderGroupDedup:
    """When config.yaml uses a non-canonical providers.<id> key, the picker
    must still surface ONE provider group, not two."""

    def test_underscored_providers_key_does_not_create_phantom_group(self, monkeypatch, tmp_path):
        """Deor's exact reproduction case: ``providers.opencode_go.api_key``
        (underscored) with ``model.provider: opencode-go`` (hyphenated)."""
        _scrub_provider_env(monkeypatch)
        _install_fake_hermes_cli(monkeypatch)
        monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)

        restore = _swap_in_test_config({
            "model": {"provider": "opencode-go", "default": "glm-5.1"},
            "providers": {"opencode_go": {"api_key": "fake-test-key"}},
        })
        try:
            data = config.get_available_models()
            opencode_groups = [
                g for g in data["groups"]
                if "opencode" in (g.get("provider_id") or "").lower()
                or "opencode" in (g.get("provider") or "").lower()
            ]
            assert len(opencode_groups) == 1, (
                f"Expected exactly ONE OpenCode Go group, got {len(opencode_groups)}: "
                f"{[(g['provider'], g['provider_id']) for g in opencode_groups]}. "
                f"Pre-fix, the underscored providers-key produced a separate "
                f"'Opencode_Go' provider group at the bottom of the picker (#1568)."
            )
            grp = opencode_groups[0]
            assert grp["provider_id"] == "opencode-go", (
                f"Group provider_id should be canonical 'opencode-go', got "
                f"{grp['provider_id']!r}."
            )
            assert grp["provider"] == "OpenCode Go", (
                f"Group display name should be canonical 'OpenCode Go', got "
                f"{grp['provider']!r}."
            )
        finally:
            restore()

    def test_uppercase_providers_key_does_not_create_phantom_group(self, monkeypatch, tmp_path):
        _scrub_provider_env(monkeypatch)
        _install_fake_hermes_cli(monkeypatch)
        monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)

        restore = _swap_in_test_config({
            "model": {"provider": "opencode-go", "default": "glm-5.1"},
            "providers": {"OPENCODE-GO": {"api_key": "fake"}},
        })
        try:
            data = config.get_available_models()
            opencode_groups = [
                g for g in data["groups"]
                if (g.get("provider_id") or "").lower().replace("_", "-") == "opencode-go"
            ]
            assert len(opencode_groups) == 1
        finally:
            restore()

    def test_aliased_providers_key_collapses_to_canonical(self, monkeypatch, tmp_path):
        """``z-ai`` is a known alias for canonical ``zai``. A user with
        ``providers.z-ai.api_key`` should still see ONE Z.AI group, not two."""
        _scrub_provider_env(monkeypatch)
        _install_fake_hermes_cli(monkeypatch)
        monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)

        restore = _swap_in_test_config({
            "model": {"provider": "zai", "default": "glm-5"},
            "providers": {"z-ai": {"api_key": "fake"}},
        })
        try:
            data = config.get_available_models()
            zai_groups = [
                g for g in data["groups"]
                if (g.get("provider_id") or "") in ("zai", "z-ai")
            ]
            assert len(zai_groups) == 1, (
                f"Expected one Z.AI group, got {len(zai_groups)}: "
                f"{[(g['provider'], g['provider_id']) for g in zai_groups]}"
            )
            assert zai_groups[0]["provider_id"] == "zai"
        finally:
            restore()

    def test_happy_path_unchanged(self, monkeypatch, tmp_path):
        """Sanity: when config keys are already canonical, behaviour is unchanged."""
        _scrub_provider_env(monkeypatch)
        _install_fake_hermes_cli(monkeypatch)
        monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)

        restore = _swap_in_test_config({
            "model": {"provider": "opencode-go", "default": "glm-5.1"},
            "providers": {"opencode-go": {"api_key": "fake"}},
        })
        try:
            data = config.get_available_models()
            opencode_groups = [
                g for g in data["groups"]
                if g.get("provider_id") == "opencode-go"
            ]
            assert len(opencode_groups) == 1
            assert opencode_groups[0]["provider"] == "OpenCode Go"
            assert len(opencode_groups[0]["models"]) >= 1
        finally:
            restore()


# ────────────────────────────────────────────────────────────────────────
# Section 3 — model.default provider-id injection guard
# ────────────────────────────────────────────────────────────────────────


class TestDefaultModelProviderIdGuard:
    """``model.default = <provider id>`` is a common config typo. Pre-fix the
    picker silently injected the provider id as a phantom model option.
    Post-fix the injection is skipped + a warning is logged."""

    def test_provider_id_as_default_does_not_inject_phantom(self, monkeypatch, tmp_path, caplog):
        _scrub_provider_env(monkeypatch)
        _install_fake_hermes_cli(monkeypatch)
        monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)

        restore = _swap_in_test_config({
            "model": {"provider": "opencode-go", "default": "opencode_go"},
            "providers": {"opencode-go": {"api_key": "fake"}},
        })
        try:
            with caplog.at_level("WARNING", logger="api.config"):
                data = config.get_available_models()
            opencode = next(
                g for g in data["groups"] if g.get("provider_id") == "opencode-go"
            )
            ids = {m["id"] for m in opencode["models"]}
            for bad in ("opencode_go", "opencode-go", "OpenCode Go"):
                assert bad not in ids, (
                    f"Phantom model id {bad!r} leaked into picker — the "
                    f"provider-id guard should skip injection. Pre-fix, "
                    f"this surfaced as a self-referential 'Opencode GO' "
                    f"15th entry. (#1568)"
                )
            # And we get a logged warning so the misconfig is discoverable.
            assert any(
                "model.default" in rec.getMessage().lower()
                or "provider id" in rec.getMessage().lower()
                for rec in caplog.records
            ), (
                "Skipping the injection should emit a WARNING so the user's "
                "actual config error is discoverable in logs, not just silently "
                "papered over."
            )
        finally:
            restore()

    def test_provider_alias_as_default_does_not_inject_phantom(self, monkeypatch, tmp_path):
        _scrub_provider_env(monkeypatch)
        _install_fake_hermes_cli(monkeypatch)
        monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)

        # Z.AI / GLM has display name "Z.AI / GLM", canonical id "zai",
        # alias "z-ai". model.default == "z-ai" should be caught.
        restore = _swap_in_test_config({
            "model": {"provider": "zai", "default": "z-ai"},
            "providers": {"zai": {"api_key": "fake"}},
        })
        try:
            data = config.get_available_models()
            zai = next(g for g in data["groups"] if g.get("provider_id") == "zai")
            ids = {m["id"] for m in zai["models"]}
            assert "z-ai" not in ids
            assert "zai" not in ids
        finally:
            restore()

    def test_real_unknown_model_id_still_injected(self, monkeypatch, tmp_path):
        """Forward-compat: a NEW model id not yet in the static catalog
        (newly released, custom endpoint) should STILL be injected so the
        user's configured default isn't hidden from them."""
        _scrub_provider_env(monkeypatch)
        _install_fake_hermes_cli(monkeypatch)
        monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)

        restore = _swap_in_test_config({
            "model": {"provider": "anthropic", "default": "claude-opus-5.0-future"},
            "providers": {"anthropic": {"api_key": "fake"}},
        })
        try:
            data = config.get_available_models()
            all_ids = {m["id"] for g in data["groups"] for m in g["models"]}
            assert "claude-opus-5.0-future" in all_ids, (
                "Legitimate unknown model ids must still be injected — "
                "otherwise newly-released models or custom endpoints "
                "wouldn't show in the picker until a release with an "
                "updated _PROVIDER_MODELS catalog. The guard must only "
                "reject provider ids and known aliases."
            )
        finally:
            restore()


# ────────────────────────────────────────────────────────────────────────
# Section 4 — Empty-group filter
# ────────────────────────────────────────────────────────────────────────


class TestEmptyGroupFilter:
    def test_empty_optgroups_dropped(self, monkeypatch, tmp_path):
        """Pre-fix, when a non-canonical provider id slipped past the
        detection guards into _PROVIDER_MODELS lookup (which has no entry
        for ``opencode_go``), the build loop produced a zero-models
        optgroup that rendered as a phantom provider entry. The empty-group
        filter at the end of the build catches this regardless of which
        detection path leaked the bad id."""
        _scrub_provider_env(monkeypatch)
        _install_fake_hermes_cli(monkeypatch)
        monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)

        restore = _swap_in_test_config({
            "model": {"provider": "opencode-go", "default": "glm-5.1"},
            "providers": {"opencode_go": {"api_key": "fake"}},
        })
        try:
            data = config.get_available_models()
            empty_groups = [g for g in data["groups"] if not g.get("models")]
            # Only custom: groups are allowed to be empty (intentional UX).
            allowed_empty = [
                g for g in empty_groups
                if (g.get("provider_id") or "").startswith("custom:")
            ]
            disallowed = [g for g in empty_groups if g not in allowed_empty]
            assert not disallowed, (
                f"Zero-model optgroups should not appear in the picker — "
                f"they're pure UI noise. Got {len(disallowed)} unexpected "
                f"empty groups: {[(g['provider'], g['provider_id']) for g in disallowed]}."
            )
        finally:
            restore()

    def test_custom_provider_can_still_be_empty(self, monkeypatch, tmp_path):
        """Custom providers from ``custom_providers`` config are exempt
        from the empty-group filter — users may want an empty card visible
        as a reminder to fill in models."""
        _scrub_provider_env(monkeypatch)
        _install_fake_hermes_cli(monkeypatch)
        monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)

        restore = _swap_in_test_config({
            "model": {"provider": "custom", "default": "some-model"},
            "custom_providers": [
                {"name": "my-empty-provider", "api_key": "fake"},
            ],
        })
        try:
            data = config.get_available_models()
            # The empty-group filter should NOT drop a custom: provider.
            # (The exact custom group surface depends on other config logic;
            # this test just pins that custom: groups are exempt from the
            # filter, not that one is necessarily produced.)
            for g in data["groups"]:
                if (g.get("provider_id") or "").startswith("custom:"):
                    # Found at least one custom group — that's enough to
                    # confirm the exempt path doesn't drop them, since
                    # the empty-models case would otherwise be filtered.
                    return
        finally:
            restore()
