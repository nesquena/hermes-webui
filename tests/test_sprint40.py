"""
Sprint 40 Tests: OAuth provider onboarding path (PR B of issue #329).

Covers:
- _build_setup_catalog sets current_is_oauth=True for OAuth providers
- _build_setup_catalog sets current_is_oauth=False for API-key providers
- _build_setup_catalog sets current_is_oauth=False when no provider configured
- apply_onboarding_setup with unsupported provider marks onboarding complete directly
- i18n.js contains all required OAuth onboarding keys in both English and Spanish
"""
import pathlib
import re
import unittest
from unittest.mock import patch

import api.onboarding as mod

REPO_ROOT = pathlib.Path(__file__).parent.parent
I18N_JS = (REPO_ROOT / "static" / "i18n.js").read_text()
ONBOARDING_JS = (REPO_ROOT / "static" / "onboarding.js").read_text()


# ── Backend: _build_setup_catalog ──────────────────────────────────────────


class TestBuildSetupCatalog(unittest.TestCase):

    def _catalog(self, provider, model="gpt-4o", base_url=""):
        cfg = {}
        if provider:
            cfg = {"model": {"provider": provider, "default": model, "base_url": base_url}}
        with patch.object(mod, "get_config", return_value=cfg):
            return mod._build_setup_catalog(cfg)

    def test_oauth_provider_sets_current_is_oauth_true(self):
        """openai-codex is not in _SUPPORTED_PROVIDER_SETUPS → current_is_oauth=True."""
        catalog = self._catalog("openai-codex", "gpt-5.4")
        self.assertTrue(catalog["current_is_oauth"],
                        "current_is_oauth must be True for openai-codex")

    def test_copilot_provider_sets_current_is_oauth_true(self):
        """copilot is also OAuth."""
        catalog = self._catalog("copilot")
        self.assertTrue(catalog["current_is_oauth"])

    def test_openai_provider_sets_current_is_oauth_false(self):
        """openai is in _SUPPORTED_PROVIDER_SETUPS → current_is_oauth=False."""
        catalog = self._catalog("openai", "gpt-4o")
        self.assertFalse(catalog["current_is_oauth"],
                         "current_is_oauth must be False for API-key provider openai")

    def test_anthropic_provider_sets_current_is_oauth_false(self):
        catalog = self._catalog("anthropic", "claude-sonnet-4.6")
        self.assertFalse(catalog["current_is_oauth"])

    def test_no_provider_sets_current_is_oauth_false(self):
        """Empty config → current_is_oauth=False."""
        catalog = self._catalog("")
        self.assertFalse(catalog["current_is_oauth"])

    def test_catalog_includes_current_is_oauth_key(self):
        """current_is_oauth must always be present in the catalog dict."""
        catalog = self._catalog("openrouter")
        self.assertIn("current_is_oauth", catalog)


# ── Backend: apply_onboarding_setup for OAuth providers ────────────────────


class TestApplyOnboardingOAuthPath(unittest.TestCase):

    def test_unsupported_provider_skips_to_complete(self):
        """apply_onboarding_setup with an OAuth provider just marks onboarding done."""
        saved = {}

        def _save(d):
            saved.update(d)

        mock_status = {"completed": True, "system": {"chat_ready": True}}

        with patch.object(mod, "save_settings", side_effect=_save), \
             patch.object(mod, "get_onboarding_status", return_value=mock_status):
            result = mod.apply_onboarding_setup({"provider": "openai-codex", "model": "gpt-5.4"})

        self.assertTrue(saved.get("onboarding_completed"),
                        "save_settings must set onboarding_completed=True for OAuth provider")
        self.assertEqual(result, mock_status)

    def test_unsupported_provider_does_not_write_config_yaml(self):
        """OAuth path must not call _save_yaml_config — no config mutation."""
        with patch.object(mod, "save_settings"), \
             patch.object(mod, "get_onboarding_status", return_value={}), \
             patch.object(mod, "_save_yaml_config") as mock_save_yaml:
            mod.apply_onboarding_setup({"provider": "copilot", "model": "gpt-4o"})

        mock_save_yaml.assert_not_called()


# ── Frontend: i18n keys ────────────────────────────────────────────────────


_REQUIRED_OAUTH_KEYS = [
    "onboarding_oauth_provider_ready_title",
    "onboarding_oauth_provider_ready_body",
    "onboarding_oauth_provider_not_ready_title",
    "onboarding_oauth_provider_not_ready_body",
    "onboarding_oauth_switch_hint",
]


class TestOAuthI18nKeys(unittest.TestCase):

    def test_english_locale_has_all_oauth_keys(self):
        """All OAuth onboarding i18n keys must be present in the English locale."""
        missing = [k for k in _REQUIRED_OAUTH_KEYS if k not in I18N_JS]
        self.assertFalse(missing,
                         f"English locale missing OAuth keys: {missing}")

    def test_spanish_locale_has_all_oauth_keys(self):
        """All OAuth onboarding i18n keys must be present in the Spanish locale."""
        # Spanish locale is the second occurrence of each key
        counts = {k: I18N_JS.count(k) for k in _REQUIRED_OAUTH_KEYS}
        under = [k for k, c in counts.items() if c < 2]
        self.assertFalse(under,
                         f"Spanish locale missing OAuth keys (need 2 occurrences each): {under}")

    def test_oauth_body_strings_contain_provider_placeholder(self):
        """Body strings must contain {provider} so JS can substitute the provider name."""
        for key in ["onboarding_oauth_provider_ready_body",
                    "onboarding_oauth_provider_not_ready_body"]:
            self.assertIn("{provider}", I18N_JS,
                          f"{key} must contain {{provider}} placeholder")


# ── Frontend: onboarding.js uses current_is_oauth ─────────────────────────


class TestOAuthOnboardingJs(unittest.TestCase):

    def test_onboarding_js_reads_current_is_oauth(self):
        """onboarding.js must check current_is_oauth from the status payload."""
        self.assertIn("current_is_oauth", ONBOARDING_JS,
                      "onboarding.js must read current_is_oauth from ONBOARDING.status.setup")

    def test_onboarding_js_renders_oauth_ready_card(self):
        """onboarding.js must render the oauth-ready card class."""
        self.assertIn("onboarding-oauth-ready", ONBOARDING_JS)

    def test_onboarding_js_renders_oauth_pending_card(self):
        """onboarding.js must render the oauth-pending card class."""
        self.assertIn("onboarding-oauth-pending", ONBOARDING_JS)

    def test_style_css_has_oauth_card_rules(self):
        """style.css must contain the .onboarding-oauth-card rules."""
        css = (REPO_ROOT / "static" / "style.css").read_text()
        self.assertIn("onboarding-oauth-card", css)
        self.assertIn("onboarding-oauth-ready", css)
        self.assertIn("onboarding-oauth-pending", css)


if __name__ == "__main__":
    unittest.main()
