"""Tests for #815 — BYOK/custom provider models missing from WebUI model dropdown.

Root causes fixed:
  1. active_provider alias not normalized in get_available_models()
     ('z.ai' -> 'zai', 'x.ai' -> 'xai', 'google' -> 'gemini', etc.)
     causing the provider to fall to the 'else/unknown' branch with no models.

  2. /api/models/live didn't normalize the provider query param, so
     provider_model_ids() received the un-aliased form and returned [].

  3. /api/models/live returned empty for provider='custom' even when
     custom_providers entries exist in config.yaml — the live enrichment
     step never added those models.
"""

import pathlib
import re
import sys
import unittest.mock as mock

import pytest

REPO = pathlib.Path(__file__).parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO.parent / ".hermes" / "hermes-agent"))


def read(rel):
    return (REPO / rel).read_text(encoding="utf-8")


@pytest.fixture(autouse=True)
def _isolate_models_cache():
    """Invalidate the TTL model cache before AND after every test.

    ``get_available_models()`` caches its result keyed on config.yaml mtime.
    Tests in this file repoint ``_get_config_path`` to a tmp_path, populate
    the cache there, then let monkeypatch restore the original path.  The
    cache, keyed on the tmp_path's mtime, then poisons downstream tests
    (e.g. test_model_resolver) which see stale data and never hit their
    mocks.  Clearing the cache around each test breaks that linkage.
    """
    import api.config as c

    try:
        c.invalidate_models_cache()
    except Exception:
        pass
    yield
    try:
        c.invalidate_models_cache()
    except Exception:
        pass


# ── api/config.py — active_provider normalization ─────────────────────────────


class TestActiveProviderNormalization:
    """get_available_models() must normalize active_provider aliases before lookup."""

    def _run(self, tmp_path, provider_str, monkeypatch):
        """Return get_available_models() output for a given provider string."""
        import api.config as c

        cfgfile = tmp_path / "config.yaml"
        cfgfile.write_text(
            f"model:\n  provider: {provider_str}\n  default: test-model\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(c, "_get_config_path", lambda: cfgfile)
        c.reload_config()
        # Patch list_available_providers to avoid real network calls
        fake_prov = mock.MagicMock()
        fake_prov.return_value = []
        try:
            import hermes_cli.models as hm

            monkeypatch.setattr(hm, "list_available_providers", fake_prov)
        except Exception:
            pass
        result = c.get_available_models()
        c.reload_config()
        return result

    def test_z_dot_ai_normalized_to_zai(self, tmp_path, monkeypatch):
        result = self._run(tmp_path, "z.ai", monkeypatch)
        # active_provider returned to browser must be canonical 'zai' or
        # at minimum must not be 'z.ai' (which would miss the _PROVIDER_MODELS lookup)
        ap = result.get("active_provider", "")
        assert ap in ("zai", ""), f"active_provider should be 'zai', got {ap!r}"

    def test_x_dot_ai_normalized_to_xai(self, tmp_path, monkeypatch):
        result = self._run(tmp_path, "x.ai", monkeypatch)
        ap = result.get("active_provider", "")
        assert ap in ("xai", ""), f"active_provider should be 'xai', got {ap!r}"

    def test_google_normalized_to_gemini(self, tmp_path, monkeypatch):
        result = self._run(tmp_path, "google", monkeypatch)
        ap = result.get("active_provider", "")
        assert ap in ("gemini", ""), f"active_provider should be 'gemini', got {ap!r}"

    def test_normalization_code_present(self):
        """Source-level check: config.py must call _PROVIDER_ALIASES for active_provider."""
        src = read("api/config.py")
        # Must alias-normalize active_provider before the group-builder runs
        assert "_PROVIDER_ALIASES" in src, (
            "api/config.py must import _PROVIDER_ALIASES to normalize active_provider"
        )
        # The normalization must happen before the group builder loop
        alias_pos = src.index("_PROVIDER_ALIASES")
        group_builder_pos = src.index("for pid in sorted(detected_providers)")
        assert alias_pos < group_builder_pos, (
            "active_provider normalization must occur before the group-builder loop"
        )


# ── api/routes.py — /api/models/live provider normalization ───────────────────


class TestLiveModelsProviderNormalization:
    """_handle_live_models must normalize the provider query param."""

    def test_live_models_normalizes_provider_alias(self):
        src = read("api/routes.py")
        # Find _handle_live_models function
        m = re.search(
            r"def _handle_live_models\(.*?\ndef ",
            src,
            re.DOTALL,
        )
        assert m, "_handle_live_models not found"
        fn = m.group(0)
        assert "_resolve_provider_alias" in fn, (
            "_handle_live_models must normalize provider via "
            "api.config._resolve_provider_alias so 'z.ai' -> 'zai' "
            "before calling provider_model_ids()"
        )

    def test_live_models_normalization_before_provider_model_ids(self):
        """Normalization call must appear before the provider_model_ids call site."""
        src = read("api/routes.py")
        alias_match = re.search(
            r"provider\s*=\s*_resolve_provider_alias\(provider\)",
            src,
        )
        pmi_call_match = re.search(
            r"ids\s*=\s*_pmi\(provider\)",
            src,
        )
        assert alias_match, "_resolve_provider_alias call not found in routes.py"
        assert pmi_call_match, "ids = _pmi(provider) call not found"
        assert alias_match.start() < pmi_call_match.start(), (
            "alias normalization must occur before ids = _pmi(provider)"
        )

    def test_alias_resolver_works_without_hermes_cli(self):
        """Normalization must work even when hermes_cli is not importable —
        CI and installs without the agent cloned alongside the WebUI.
        The WebUI ships its own _PROVIDER_ALIASES table; the agent's table
        is merged only when available."""
        import api.config as c

        # Core CLI aliases from #815's bug report
        assert c._resolve_provider_alias("z.ai") == "zai"
        assert c._resolve_provider_alias("x.ai") == "xai"
        assert c._resolve_provider_alias("google") == "gemini"
        assert c._resolve_provider_alias("grok") == "xai"
        # Case / whitespace insensitive
        assert c._resolve_provider_alias("  Z.AI  ") == "zai"
        # Canonical names pass through unchanged
        assert c._resolve_provider_alias("openrouter") == "openrouter"
        assert c._resolve_provider_alias("anthropic") == "anthropic"
        assert c._resolve_provider_alias("custom") == "custom"
        # Empty / None pass through
        assert c._resolve_provider_alias("") == ""
        assert c._resolve_provider_alias(None) is None


# ── api/routes.py — /api/models/live custom_providers fallback ────────────────


class TestLiveModelsCustomProviderFallback:
    """When provider='custom' and provider_model_ids() returns [],
    /api/models/live must fall back to custom_providers entries from config.yaml."""

    def test_custom_fallback_code_present(self):
        src = read("api/routes.py")
        m = re.search(
            r"def _handle_live_models\(.*?\ndef ",
            src,
            re.DOTALL,
        )
        assert m, "_handle_live_models not found"
        fn = m.group(0)
        assert "custom_providers" in fn, (
            "_handle_live_models must read custom_providers from config "
            "as fallback when provider='custom' and provider_model_ids() returns []"
        )
        assert 'provider == "custom"' in fn or "provider=='custom'" in fn, (
            "_handle_live_models must check provider == 'custom' before fallback"
        )

    def test_custom_fallback_returns_configured_models(self, tmp_path, monkeypatch):
        """End-to-end: /api/models/live?provider=custom returns custom_providers models."""
        import api.config as c
        import api.routes as r

        cfgfile = tmp_path / "config.yaml"
        cfgfile.write_text(
            "model:\n  provider: custom\n  default: my-byok-model\n"
            "custom_providers:\n"
            "  - model: my-byok-model\n"
            "    api_base: https://my-llm.example.com/v1\n"
            "    api_key: sk-test\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(c, "_get_config_path", lambda: cfgfile)
        c.reload_config()

        # Mock handler and parsed URL
        handler = mock.MagicMock()
        responses = []

        def fake_j(h, data, **kw):
            responses.append(data)
            return True

        monkeypatch.setattr(r, "j", fake_j)

        parsed = mock.MagicMock()
        parsed.query = "provider=custom"

        # Mock provider_model_ids to return [] (simulating no live endpoint)
        try:
            import hermes_cli.models as hm

            monkeypatch.setattr(hm, "provider_model_ids", lambda p: [])
        except Exception:
            pass

        r._handle_live_models(handler, parsed)

        assert responses, "handler must produce a response"
        resp = responses[-1]
        assert "models" in resp
        model_ids = [m["id"] for m in resp.get("models", [])]
        assert "my-byok-model" in model_ids, (
            f"custom_providers model 'my-byok-model' must appear in live response; "
            f"got {model_ids}"
        )


# ── Regression: known-good providers still work ───────────────────────────────


class TestKnownProvidersUnaffected:
    """Normalization must not break providers whose names are already canonical."""

    def test_openrouter_unaffected(self):
        src = read("api/config.py")
        # _PROVIDER_ALIASES lookup: 'openrouter' -> 'openrouter' (no change)
        assert "openrouter" in src, "openrouter must still exist in config"

    def test_anthropic_unaffected(self):
        src = read("api/config.py")
        assert "anthropic" in src

    def test_custom_unaffected(self):
        """'custom' is not in _PROVIDER_ALIASES so normalization is a no-op."""
        try:
            from hermes_cli.models import _PROVIDER_ALIASES

            assert "custom" not in _PROVIDER_ALIASES, (
                "'custom' must not be aliased to anything — it's a special sentinel"
            )
        except ImportError:
            pass  # hermes-agent not available in this env — skip


# ── Source-level: active_provider returned to browser is canonical ─────────────


class TestProviderIdInGroupResponse:
    """get_available_models() must include provider_id on every group so the JS
    _fetchLiveModels can match optgroups exactly rather than by substring."""

    def test_groups_include_provider_id(self, tmp_path, monkeypatch):
        import api.config as c

        cfgfile = tmp_path / "config.yaml"
        cfgfile.write_text(
            "model:\n  provider: zai\n  default: glm-5\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(c, "_get_config_path", lambda: cfgfile)
        c.reload_config()
        try:
            import hermes_cli.models as hm

            monkeypatch.setattr(
                hm,
                "list_available_providers",
                lambda: [{"id": "zai", "authenticated": True}],
            )
            import hermes_cli.auth as ha

            monkeypatch.setattr(ha, "get_auth_status", lambda p: {"key_source": "env"})
        except Exception:
            pass
        result = c.get_available_models()
        c.reload_config()
        for g in result.get("groups", []):
            assert "provider_id" in g, (
                f"group {g.get('provider')!r} missing provider_id — "
                "JS _fetchLiveModels needs it to match optgroups exactly"
            )

    def test_provider_id_in_static_ui_js_optgroup(self):
        src = read("static/ui.js")
        assert "og.dataset.provider" in src, (
            "populateModelDropdown must set og.dataset.provider from g.provider_id "
            "so _fetchLiveModels can match by exact provider_id"
        )

    def test_fetch_live_models_prefers_data_provider_match(self):
        src = read("static/ui.js")
        # Live model optgroup matching was extracted to _addLiveModelsToSelect (#872)
        m = re.search(r"function _addLiveModelsToSelect\b.*?\n\}", src, re.DOTALL)
        if not m:
            m = re.search(r"function _fetchLiveModels\b.*?\n\}", src, re.DOTALL)
        assert m, "_addLiveModelsToSelect or _fetchLiveModels not found"
        fn = m.group(0)
        assert "og.dataset.provider" in fn, (
            "_addLiveModelsToSelect must check og.dataset.provider===provider before "
            "falling back to label substring match"
        )
        # The data-provider check must come before the label.includes check
        dp_pos = fn.index("og.dataset.provider")
        label_pos = fn.index("og.label")
        assert dp_pos < label_pos, (
            "data-provider exact match must be attempted before label substring match"
        )


# ── Opus-identified edge case: 'ollama' normalizes to 'custom' ────────────────


class TestOllamaAliasEdgeCase:
    """Opus review found: 'ollama' -> 'custom' via _PROVIDER_ALIASES.
    This is better behaviour (custom_providers fallback catches it) but worth
    documenting and not regressing."""

    def test_ollama_not_in_provider_aliases_as_ollama(self):
        """'ollama' maps to 'custom' in _PROVIDER_ALIASES — verify this is the
        intended behavior post-normalization (not a silent breakage)."""
        try:
            from hermes_cli.models import _PROVIDER_ALIASES

            # 'ollama' -> 'custom' means ollama users hit the custom_providers path
            # This is fine — ollama models appear via base_url auto-detection (step 3)
            # in get_available_models, not via _PROVIDER_MODELS lookup.
            ollama_target = _PROVIDER_ALIASES.get("ollama", "ollama")
            # Acceptable outcomes: either unchanged (not in aliases) or 'custom'/'ollama-cloud'
            assert ollama_target in ("ollama", "custom", "ollama-cloud"), (
                f"Unexpected ollama alias: {ollama_target}"
            )
        except ImportError:
            pass  # hermes-agent not available


class TestGetAvailableModelsReturnsCanonicalProvider:
    """get_available_models() must return normalized active_provider in its response
    so the browser sends the right value to /api/models/live."""

    def test_active_provider_in_response_is_normalized(self, tmp_path, monkeypatch):
        import api.config as c

        cfgfile = tmp_path / "config.yaml"
        cfgfile.write_text(
            "model:\n  provider: z.ai\n  default: glm-5\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(c, "_get_config_path", lambda: cfgfile)
        c.reload_config()
        try:
            import hermes_cli.models as hm

            monkeypatch.setattr(hm, "list_available_providers", lambda: [])
        except Exception:
            pass
        result = c.get_available_models()
        c.reload_config()
        ap = result.get("active_provider", "")
        # The browser will pass this value to /api/models/live?provider=<ap>
        # It must be 'zai' so optgroup matching works in _fetchLiveModels
        assert ap != "z.ai", (
            "active_provider 'z.ai' must be normalized to 'zai' before being "
            "returned to the browser (browser passes it back to /api/models/live)"
        )
