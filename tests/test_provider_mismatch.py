"""
Tests for issue #266 — provider/model mismatch warning.

Covers:
  1. streaming.py: auth errors detected and classified as 'auth_mismatch'
  2. static/ui.js: _checkProviderMismatch() helper exists and logic is correct
  3. static/messages.js: apperror handler has auth_mismatch branch
  4. static/i18n.js: provider_mismatch_warning and provider_mismatch_label keys
     present in all 5 locales (en, es, de, zh, zh-Hant)
  5. static/boot.js: modelSelect.onchange calls _checkProviderMismatch
  6. /api/models: response includes active_provider field
"""
import json
import pathlib
import re
import urllib.request

REPO_ROOT = pathlib.Path(__file__).parent.parent.resolve()
from tests._pytest_port import BASE


def _read(rel_path: str) -> str:
    return (REPO_ROOT / rel_path).read_text(encoding="utf-8")


# ── 1. streaming.py: auth error detection ───────────────────────────────────

class TestStreamingAuthErrorDetection:
    """streaming.py must classify auth/401 errors as auth_mismatch."""

    def test_auth_mismatch_type_defined_in_streaming(self):
        """'auth_mismatch' type must be emitted for auth errors."""
        src = _read("api/streaming.py")
        assert "auth_mismatch" in src, (
            "auth_mismatch type not found in streaming.py — "
            "401/auth errors will not be surfaced with a helpful message"
        )

    def test_is_auth_error_flag_defined(self):
        """is_auth_error variable must exist in the error handler."""
        src = _read("api/streaming.py")
        assert "is_auth_error" in src, (
            "is_auth_error flag not found in streaming.py"
        )

    def test_auth_error_detects_401(self):
        """'401' must be part of the auth error detection logic."""
        src = _read("api/streaming.py")
        # Find the is_auth_error block
        idx = src.find("is_auth_error")
        assert idx != -1
        block = src[idx:idx + 400]
        assert "'401'" in block or '"401"' in block, (
            "'401' not in is_auth_error detection block"
        )

    def test_auth_error_detects_unauthorized(self):
        """'unauthorized' must be part of the auth error detection logic."""
        src = _read("api/streaming.py")
        idx = src.find("is_auth_error")
        block = src[idx:idx + 400]
        assert "unauthorized" in block.lower(), (
            "'unauthorized' not in is_auth_error detection block"
        )

    def test_auth_error_hint_mentions_hermes_model(self):
        """The auth_mismatch hint must mention 'hermes model' command."""
        src = _read("api/streaming.py")
        # Find the auth_mismatch apperror block
        idx = src.find("auth_mismatch")
        block = src[idx:idx + 500]
        assert "hermes model" in block, (
            "auth_mismatch hint must mention 'hermes model' command "
            "so users know how to fix provider mismatch"
        )

    def test_auth_error_does_not_catch_rate_limit(self):
        """Rate limit errors must not be reclassified as auth_mismatch."""
        src = _read("api/streaming.py")
        # is_rate_limit must come before is_auth_error in the elif chain
        rl_idx = src.find("is_rate_limit")
        ae_idx = src.find("is_auth_error")
        assert rl_idx < ae_idx, (
            "is_rate_limit check should precede is_auth_error — "
            "rate limit errors must not be mistaken for auth errors"
        )


# ── 2. static/ui.js: _checkProviderMismatch() ───────────────────────────────

class TestCheckProviderMismatch:
    """ui.js must expose _checkProviderMismatch() helper."""

    def test_function_defined(self):
        """_checkProviderMismatch function must be defined in ui.js."""
        src = _read("static/ui.js")
        assert "function _checkProviderMismatch" in src, (
            "_checkProviderMismatch not defined in ui.js"
        )

    def test_uses_window_active_provider(self):
        """Function must read window._activeProvider."""
        src = _read("static/ui.js")
        idx = src.find("function _checkProviderMismatch")
        block = src[idx:idx + 800]
        assert "_activeProvider" in block, (
            "_checkProviderMismatch must read window._activeProvider"
        )

    def test_skips_check_for_openrouter(self):
        """OpenRouter can route to any provider — skip the warning."""
        src = _read("static/ui.js")
        idx = src.find("function _checkProviderMismatch")
        block = src[idx:idx + 800]
        assert "openrouter" in block.lower(), (
            "_checkProviderMismatch must skip the check for openrouter"
        )

    def test_skips_check_for_custom(self):
        """Custom endpoints can serve any model — skip the warning."""
        src = _read("static/ui.js")
        idx = src.find("function _checkProviderMismatch")
        block = src[idx:idx + 800]
        assert "custom" in block.lower(), (
            "_checkProviderMismatch must skip the check for custom provider"
        )

    def test_active_provider_stored_on_model_load(self):
        """populateModelDropdown must store active_provider from /api/models."""
        src = _read("static/ui.js")
        # Find the function definition (skip the comment that also mentions the name)
        idx = src.find("async function populateModelDropdown")
        assert idx != -1, "async function populateModelDropdown not found"
        block = src[idx:idx + 800]
        assert "_activeProvider" in block, (
            "populateModelDropdown must set window._activeProvider "
            "from the /api/models response"
        )


# ── 3. static/messages.js: apperror handler ─────────────────────────────────

class TestApperrorHandler:
    """messages.js apperror handler must handle auth_mismatch type."""

    def test_auth_mismatch_type_handled(self):
        """apperror handler must check for type='auth_mismatch'."""
        src = _read("static/messages.js")
        assert "auth_mismatch" in src, (
            "auth_mismatch type not handled in messages.js apperror handler"
        )

    def test_provider_mismatch_label(self):
        """'Provider mismatch' label must appear in the error handling."""
        src = _read("static/messages.js")
        assert "Provider mismatch" in src, (
            "'Provider mismatch' label not found in messages.js"
        )

    def test_is_auth_mismatch_variable(self):
        """isAuthMismatch variable must be defined."""
        src = _read("static/messages.js")
        assert "isAuthMismatch" in src, (
            "isAuthMismatch variable not found in messages.js apperror handler"
        )


# ── 4. static/i18n.js: all 5 locales ────────────────────────────────────────

class TestI18nProviderMismatch:
    """All 5 locales must have provider_mismatch_warning and provider_mismatch_label."""

    REQUIRED_KEYS = ["provider_mismatch_warning", "provider_mismatch_label"]

    def _count_key(self, src: str, key: str) -> int:
        return len(re.findall(r'\b' + re.escape(key) + r'\b', src))

    def test_all_locales_have_warning_key(self):
        """provider_mismatch_warning must appear in all 5 locales."""
        src = _read("static/i18n.js")
        count = self._count_key(src, "provider_mismatch_warning")
        assert count >= 5, (
            f"provider_mismatch_warning found {count} times, expected >= 5 "
            f"(one per locale: en, es, de, zh, zh-Hant)"
        )

    def test_all_locales_have_label_key(self):
        """provider_mismatch_label must appear in all 5 locales."""
        src = _read("static/i18n.js")
        count = self._count_key(src, "provider_mismatch_label")
        assert count >= 5, (
            f"provider_mismatch_label found {count} times, expected >= 5"
        )

    def test_warning_is_function_in_en(self):
        """English provider_mismatch_warning must be a function (m, p) => ..."""
        src = _read("static/i18n.js")
        # Find the en block
        en_start = src.find("\n  en: {")
        es_start = src.find("\n  es: {")
        en_block = src[en_start:es_start]
        assert "provider_mismatch_warning" in en_block, "Key not in en block"
        idx = en_block.find("provider_mismatch_warning")
        line = en_block[idx:idx + 200]
        # Must be a function, not a plain string
        assert "=>" in line, (
            "provider_mismatch_warning in en locale must be an arrow function "
            "that takes (m, p) parameters for model and provider interpolation"
        )

    def test_spanish_locale_key_coverage(self):
        """Spanish locale must have the new keys (parity with English)."""
        src = _read("static/i18n.js")
        es_start = src.find("\n  es: {")
        de_start = src.find("\n  de: {")
        es_block = src[es_start:de_start]
        for key in self.REQUIRED_KEYS:
            assert key in es_block, f"Key '{key}' missing from Spanish locale"


# ── 5. static/boot.js: dropdown change handler ──────────────────────────────

class TestBootModelSelectChange:
    """boot.js modelSelect.onchange must call _checkProviderMismatch."""

    def test_onchange_calls_check_function(self):
        """modelSelect.onchange must invoke _checkProviderMismatch."""
        src = _read("static/boot.js")
        assert "_checkProviderMismatch" in src, (
            "boot.js modelSelect.onchange must call _checkProviderMismatch "
            "to warn users about provider/model mismatches"
        )
        # Verify it's called from the onchange handler (near modelSelect.onchange)
        idx = src.find("'modelSelect').onchange") or src.find('"modelSelect").onchange')
        if idx == -1:
            # Try alternate patterns
            idx = src.find("modelSelect")
        block_start = src.rfind("\n", 0, src.find("_checkProviderMismatch")) or 0
        surrounding = src[max(0, block_start - 200):block_start + 400]
        assert "modelSelect" in surrounding or "selectedModel" in surrounding, (
            "_checkProviderMismatch must be called in the context of model selection"
        )

    def test_onchange_shows_toast_on_mismatch(self):
        """The warning must be shown via showToast, not alert()."""
        src = _read("static/boot.js")
        # Both _checkProviderMismatch call and showToast must be near each other
        idx = src.find("_checkProviderMismatch")
        assert idx != -1, "_checkProviderMismatch not found in boot.js"
        block = src[idx:idx + 300]
        assert "showToast" in block, (
            "Provider mismatch warning must be shown via showToast(), not alert()"
        )


# ── 6. /api/models: active_provider in response ──────────────────────────────

def test_api_models_includes_active_provider():
    """/api/models must include 'active_provider' key in response."""
    with urllib.request.urlopen(BASE + "/api/models", timeout=10) as r:
        data = json.loads(r.read())
    # active_provider can be None/null but the key must exist
    assert "active_provider" in data, (
        "/api/models response missing 'active_provider' field — "
        "frontend needs this to detect provider mismatches"
    )
