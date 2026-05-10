"""
Tests for issue #1202 — OAuth provider cards always show "Not Configured"
when auth is via config.yaml or when a token was consumed by the native CLI.

Root cause: get_providers() unconditionally overwrote has_key=True from
_provider_has_key() with has_key=False from get_auth_status(), discarding
a valid working token in config.yaml.

Fixes:
  1. api/providers.py: elif has_key branch preserves config.yaml token
  2. api/providers.py: except clause no longer forces has_key=False
  3. api/providers.py: auth_error field added to provider dict
  4. static/panels.js: OAuth card shows correct hint + badge per key_source
  5. static/i18n.js: new i18n keys for config_yaml and not_configured hints
"""

import sys
import types
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).parent.parent.resolve()


# ---------------------------------------------------------------------------
# Helper: build a fake hermes_cli.auth module so tests work without the dep
# ---------------------------------------------------------------------------


def _make_fake_auth(
    logged_in: bool, error: str | None = None, key_source: str = "oauth"
):
    mod = types.ModuleType("hermes_cli.auth")

    def get_auth_status(pid):
        if logged_in:
            return {"logged_in": True, "key_source": key_source}
        result = {"logged_in": False}
        if error:
            result["error"] = error
        return result

    mod.get_auth_status = get_auth_status
    return mod


# ---------------------------------------------------------------------------
# Tests for api/providers.py OAuth block
# ---------------------------------------------------------------------------


class TestGetProvidersOauthBlock:
    """Unit tests for the OAuth override block in get_providers()."""

    def _call_get_providers_for_codex(self, fake_auth_module, has_key_in_config: bool):
        """
        Patch just the OAuth resolution path for openai-codex and return the
        provider dict for that one provider.
        """
        import api.providers as prov_mod

        # Patch _provider_has_key to return our desired value
        with (
            patch.object(prov_mod, "_provider_has_key", return_value=has_key_in_config),
            patch.object(
                prov_mod,
                "_provider_is_oauth",
                side_effect=lambda pid: pid in ("openai-codex", "nous", "copilot"),
            ),
            patch.dict(sys.modules, {"hermes_cli.auth": fake_auth_module}),
            patch.object(prov_mod, "get_config", return_value={}),
        ):
            result = prov_mod.get_providers()

        providers = {p["id"]: p for p in result["providers"]}
        return providers.get("openai-codex")

    def test_config_yaml_token_shows_configured_when_auth_logged_in(self):
        """When hermes auth says logged_in=True, has_key=True regardless of _provider_has_key."""
        auth = _make_fake_auth(logged_in=True)
        p = self._call_get_providers_for_codex(auth, has_key_in_config=False)
        assert p is not None
        assert p["has_key"] is True
        assert p["key_source"] == "oauth"

    def test_config_yaml_token_shows_configured_when_auth_not_logged_in(self):
        """
        REGRESSION TEST (#1202 Bug 1):
        When _provider_has_key() returns True (token in config.yaml) but
        get_auth_status() returns logged_in=False, has_key must remain True.

        Before the fix: has_key was overwritten to False, hiding the working token.
        """
        auth = _make_fake_auth(logged_in=False)
        p = self._call_get_providers_for_codex(auth, has_key_in_config=True)
        assert p is not None
        assert p["has_key"] is True, (
            "REGRESSION: config.yaml token was discarded because get_auth_status() "
            "returned logged_in=False. Bug #1202 has regressed."
        )
        assert p["key_source"] == "config_yaml", (
            f"Expected key_source='config_yaml', got {p['key_source']!r}"
        )

    def test_not_configured_when_no_key_and_not_logged_in(self):
        """When neither config.yaml token nor hermes auth, provider is not configured."""
        auth = _make_fake_auth(logged_in=False)
        p = self._call_get_providers_for_codex(auth, has_key_in_config=False)
        assert p is not None
        assert p["has_key"] is False

    def test_auth_error_preserved_when_not_logged_in_and_no_config_key(self):
        """auth_error from get_auth_status() is returned in the provider dict."""
        err_msg = "Refresh token consumed by Codex CLI. Run hermes auth."
        auth = _make_fake_auth(logged_in=False, error=err_msg)
        p = self._call_get_providers_for_codex(auth, has_key_in_config=False)
        assert p is not None
        assert p["has_key"] is False
        assert p["auth_error"] == err_msg

    def test_auth_error_preserved_when_not_logged_in_but_config_key_present(self):
        """auth_error is still returned even when config.yaml token is present."""
        err_msg = "Refresh token was already consumed."
        auth = _make_fake_auth(logged_in=False, error=err_msg)
        p = self._call_get_providers_for_codex(auth, has_key_in_config=True)
        assert p is not None
        assert p["has_key"] is True
        assert p["auth_error"] == err_msg

    def test_hermes_cli_import_error_does_not_discard_config_yaml_key(self):
        """
        REGRESSION TEST (#1202 Bug 1 - exception path):
        If hermes_cli.auth cannot be imported, has_key from _provider_has_key()
        must be preserved. Before the fix, the except clause forced has_key=False.
        """
        import api.providers as prov_mod

        # Use a module that raises ImportError
        bad_mod = types.ModuleType("hermes_cli.auth")

        def bad_get_auth_status(pid):
            raise ImportError("hermes_cli not installed")

        bad_mod.get_auth_status = bad_get_auth_status

        with (
            patch.object(prov_mod, "_provider_has_key", return_value=True),
            patch.object(
                prov_mod,
                "_provider_is_oauth",
                side_effect=lambda pid: pid in ("openai-codex", "nous", "copilot"),
            ),
            patch.dict(sys.modules, {"hermes_cli.auth": bad_mod}),
            patch.object(prov_mod, "get_config", return_value={}),
        ):
            result = prov_mod.get_providers()

        providers = {p["id"]: p for p in result["providers"]}
        p = providers.get("openai-codex")
        assert p is not None
        assert p["has_key"] is True, (
            "REGRESSION: hermes_cli import failure discarded config.yaml token. "
            "Exception handler must not override a known-good has_key=True."
        )

    def test_auth_error_field_present_on_all_oauth_providers(self):
        """Every provider dict must include auth_error (may be None)."""
        import api.providers as prov_mod

        auth = _make_fake_auth(logged_in=False)
        with (
            patch.object(prov_mod, "_provider_has_key", return_value=False),
            patch.dict(sys.modules, {"hermes_cli.auth": auth}),
            patch.object(prov_mod, "get_config", return_value={}),
        ):
            result = prov_mod.get_providers()

        for p in result["providers"]:
            assert "auth_error" in p, (
                f"Provider {p['id']!r} is missing the 'auth_error' field"
            )


# ---------------------------------------------------------------------------
# Tests for static/panels.js isOauth detection
# ---------------------------------------------------------------------------


class TestBuildProviderCardJs:
    """Static-analysis tests for the JS card rendering."""

    JS = (REPO_ROOT / "static" / "panels.js").read_text(encoding="utf-8")

    def _get_fn(self):
        idx = self.JS.find("function _buildProviderCard(p){")
        assert idx != -1, "_buildProviderCard not found in panels.js"
        depth = 0
        for i, ch in enumerate(self.JS[idx:], idx):
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return self.JS[idx : i + 1]
        raise AssertionError("Could not extract _buildProviderCard")

    def test_isOauth_uses_is_oauth_backend_field(self):
        """
        REGRESSION TEST (#1202):
        isOauth must use p.is_oauth from the backend, not a hardcoded list of IDs.
        Using p.is_oauth ensures new OAuth providers added to _OAUTH_PROVIDERS in Python
        automatically get the correct card rendering without changing the JS.
        """
        fn = self._get_fn()
        isOauth_line_idx = fn.find("const isOauth=")
        assert isOauth_line_idx != -1, "isOauth not found in _buildProviderCard"
        isOauth_line = fn[isOauth_line_idx : isOauth_line_idx + 150]
        assert "p.is_oauth" in isOauth_line, (
            "REGRESSION: isOauth does not use p.is_oauth from the backend. "
            "OAuth providers may not render the OAuth card correctly."
        )

    def test_oauth_body_has_config_yaml_branch(self):
        """OAuth card body must render a different hint for config_yaml tokens."""
        fn = self._get_fn()
        assert "config_yaml" in fn, (
            "OAuth card body has no config_yaml branch. "
            "Providers with config.yaml tokens will show the generic OAuth hint "
            "instead of explaining how the token was configured."
        )

    def test_oauth_body_surfaces_auth_error(self):
        """OAuth card body must display p.auth_error when present."""
        fn = self._get_fn()
        assert "p.auth_error" in fn, (
            "OAuth card body does not reference p.auth_error. "
            "Actionable error messages (e.g. 'refresh token consumed') will not be shown."
        )

    def test_configured_badge_shown_for_config_yaml(self):
        """The Configured badge must appear when has_key is True regardless of key_source."""
        fn = self._get_fn()
        badge_idx = fn.find("provider-card-badge")
        assert badge_idx != -1, "provider-card-badge not found in _buildProviderCard"
        badge_ctx = fn[max(0, badge_idx - 50) : badge_idx + 80]
        assert "p.has_key" in badge_ctx or "has_key" in badge_ctx, (
            "Configured badge is not conditional on p.has_key"
        )


# ---------------------------------------------------------------------------
# Tests for i18n.js new keys
# ---------------------------------------------------------------------------


class TestI18nNewKeys:
    """Verify new i18n keys exist in the English locale."""

    I18N = (REPO_ROOT / "static" / "i18n.js").read_text(encoding="utf-8")

    def test_providers_oauth_config_yaml_hint_exists(self):
        assert "providers_oauth_config_yaml_hint" in self.I18N, (
            "Missing i18n key: providers_oauth_config_yaml_hint. "
            "OAuth providers with config.yaml tokens will show undefined as the hint text."
        )

    def test_providers_oauth_not_configured_hint_exists(self):
        assert "providers_oauth_not_configured_hint" in self.I18N, (
            "Missing i18n key: providers_oauth_not_configured_hint."
        )

    def test_is_oauth_field_true_for_oauth_providers(self):
        """
        REGRESSION TEST (#1202 — Opus blocking issue):
        Provider dicts must include is_oauth=True for OAuth providers.
        Without this, the JS filter p.configurable||p.is_oauth would exclude OAuth
        providers from the Settings panel entirely (configurable=False for OAuth).
        """
        import api.providers as prov_mod

        auth = _make_fake_auth(logged_in=False)
        with (
            patch.object(prov_mod, "_provider_has_key", return_value=False),
            patch.dict(sys.modules, {"hermes_cli.auth": auth}),
            patch.object(prov_mod, "get_config", return_value={}),
        ):
            result = prov_mod.get_providers()

        providers = {p["id"]: p for p in result["providers"]}
        for pid in ("openai-codex", "nous", "copilot"):
            p = providers.get(pid)
            if p is not None:
                assert p["is_oauth"] is True, (
                    f"REGRESSION: {pid} must have is_oauth=True in the provider dict. "
                    "Without it, the JS filter configurable||is_oauth will exclude it "
                    "from Settings → Providers."
                )


class TestProviderListFilter:
    """Test that the JS filter includes OAuth providers."""

    JS = (Path(__file__).parent.parent / "static" / "panels.js").read_text(
        encoding="utf-8"
    )

    def test_providers_filter_includes_is_oauth(self):
        """
        REGRESSION TEST (#1202 — Opus blocking issue):
        The provider list filter in panels.js must include p.is_oauth so OAuth providers
        appear in the Settings panel even when configurable=False.

        Before the fix: filter(p=>p.configurable) excluded ALL OAuth providers.
        After the fix: filter(p=>p.configurable||p.is_oauth) includes them.
        """
        filter_idx = self.JS.find("filter(p=>p.configurable)")
        assert filter_idx == -1, (
            "REGRESSION: The provider filter 'filter(p=>p.configurable)' is still present. "
            "This excludes ALL OAuth providers (openai-codex, nous, copilot) from the "
            "Settings → Providers panel. The filter must be 'filter(p=>p.configurable||p.is_oauth)'."
        )
        fixed_filter_idx = self.JS.find("filter(p=>p.configurable||p.is_oauth)")
        assert fixed_filter_idx != -1, (
            "The provider filter 'filter(p=>p.configurable||p.is_oauth)' is missing from panels.js. "
            "OAuth providers will not appear in Settings → Providers."
        )
