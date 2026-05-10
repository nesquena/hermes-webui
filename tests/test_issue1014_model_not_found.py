"""
Tests for issue #1014 — model-not-found error classification.

Covers:
  1. streaming.py: 404/model-not-found errors detected and classified as 'model_not_found'
  2. streaming.py: HTML tags stripped from provider error messages before classification
  3. static/messages.js: apperror handler has model_not_found branch
  4. static/i18n.js: model_not_found_label key present in all locales
  5. streaming.py: model_not_found checked after auth but before generic error
"""

import pathlib
import re

REPO_ROOT = pathlib.Path(__file__).parent.parent.resolve()


def _read(rel_path: str) -> str:
    return (REPO_ROOT / rel_path).read_text(encoding="utf-8")


# ── 1. streaming.py: model-not-found error detection ─────────────────────────


class TestStreamingModelNotFoundDetection:
    """streaming.py must classify 404/model-not-found errors as model_not_found."""

    def test_model_not_found_type_defined_in_streaming(self):
        """'model_not_found' type must be emitted for 404 errors."""
        src = _read("api/streaming.py")
        assert "model_not_found" in src, (
            "model_not_found type not found in streaming.py — "
            "404 errors will not be surfaced with a helpful message"
        )

    def test_is_not_found_flag_defined(self):
        """_exc_is_not_found variable must exist in the exception handler."""
        src = _read("api/streaming.py")
        assert "_exc_is_not_found" in src, (
            "_exc_is_not_found flag not found in streaming.py"
        )

    def test_not_found_detects_404(self):
        """'404' must be part of the model-not-found detection logic."""
        src = _read("api/streaming.py")
        idx = src.find("_exc_is_not_found")
        assert idx != -1, "_exc_is_not_found not found"
        block = src[idx : idx + 600]
        assert "'404'" in block or '"404"' in block, (
            "'404' not in model-not-found detection block"
        )

    def test_not_found_detects_not_found_string(self):
        """'not found' must be part of the detection logic."""
        src = _read("api/streaming.py")
        idx = src.find("_exc_is_not_found")
        block = src[idx : idx + 600]
        assert "not found" in block.lower(), (
            "'not found' not in model-not-found detection block"
        )

    def test_not_found_detects_does_not_exist(self):
        """'does not exist' must be part of the detection logic."""
        src = _read("api/streaming.py")
        idx = src.find("_exc_is_not_found")
        block = src[idx : idx + 600]
        assert "does not exist" in block.lower(), (
            "'does not exist' not in model-not-found detection block"
        )

    def test_not_found_detects_invalid_model(self):
        """'invalid model' must be part of the detection logic."""
        src = _read("api/streaming.py")
        idx = src.find("_exc_is_not_found")
        block = src[idx : idx + 600]
        assert "invalid model" in block.lower(), (
            "'invalid model' not in model-not-found detection block"
        )

    def test_not_found_hint_mentions_settings(self):
        """The model_not_found hint must mention Settings or hermes model."""
        src = _read("api/streaming.py")
        idx = src.find("model_not_found")
        block = src[idx : idx + 500]
        assert "Settings" in block or "hermes model" in block, (
            "model_not_found hint must mention Settings or hermes model command"
        )

    def test_not_found_check_order_after_auth(self):
        """model_not_found must be checked after auth_mismatch (auth first)."""
        src = _read("api/streaming.py")
        auth_idx = src.find("elif _exc_is_auth")
        nf_idx = src.find("elif _exc_is_not_found")
        assert auth_idx != -1, "_exc_is_auth not found"
        assert nf_idx != -1, "_exc_is_not_found not found"
        assert auth_idx < nf_idx, (
            "auth_mismatch should be checked before model_not_found — "
            "auth errors must not be mistaken for not-found errors"
        )


# ── 2. streaming.py: HTML sanitization ───────────────────────────────────────


class TestStreamingHtmlSanitization:
    """Provider error messages containing HTML must be stripped."""

    def test_html_strip_before_classification(self):
        """HTML tags must be stripped before error classification."""
        src = _read("api/streaming.py")
        # Find the HTML sanitization block in the exception handler
        # It should appear before _exc_lower = err_str.lower()
        sanitize_idx = src.find("re.sub(r'<[^>]+>'")
        exc_lower_idx = src.find("_exc_lower = err_str.lower()")
        assert sanitize_idx != -1, (
            "HTML tag stripping (re.sub) not found in streaming.py exception handler"
        )
        assert exc_lower_idx != -1, "_exc_lower not found"
        assert sanitize_idx < exc_lower_idx, (
            "HTML sanitization must happen before error classification"
        )

    def test_whitespace_normalization(self):
        """Stripped HTML must have whitespace collapsed."""
        src = _read("api/streaming.py")
        sanitize_idx = src.find("re.sub(r'<[^>]+>'")
        block = src[sanitize_idx : sanitize_idx + 300]
        assert r"\s+" in block, (
            "Whitespace normalization (\\s+) not found after HTML strip"
        )


# ── 3. static/messages.js: apperror handler ──────────────────────────────────


class TestApperrorModelNotFound:
    """messages.js apperror handler must handle model_not_found type."""

    def test_model_not_found_type_handled(self):
        """apperror handler must check for type='model_not_found'."""
        src = _read("static/messages.js")
        assert "model_not_found" in src, (
            "model_not_found type not handled in messages.js apperror handler"
        )

    def test_model_not_found_label(self):
        """'Model not found' label must appear in the error handling."""
        src = _read("static/messages.js")
        assert "Model not found" in src, (
            "'Model not found' label not found in messages.js"
        )

    def test_is_model_not_found_variable(self):
        """isModelNotFound variable must be defined."""
        src = _read("static/messages.js")
        assert "isModelNotFound" in src, (
            "isModelNotFound variable not found in messages.js apperror handler"
        )


# ── 4. static/i18n.js: all locales ───────────────────────────────────────────


class TestI18nModelNotFound:
    """All locales must have model_not_found_label."""

    REQUIRED_KEY = "model_not_found_label"

    def _locale_names(self, src: str) -> list:
        pattern = re.compile(
            r"^\s{2}(?:'(?P<quoted>[A-Za-z0-9-]+)'|(?P<plain>[A-Za-z0-9-]+))\s*:\s*\{",
            re.MULTILINE,
        )
        names = []
        for match in pattern.finditer(src):
            names.append(match.group("quoted") or match.group("plain"))
        return names

    def _count_key(self, src: str, key: str) -> int:
        return len(re.findall(r"\b" + re.escape(key) + r"\b", src))

    def test_all_locales_have_model_not_found_label(self):
        """model_not_found_label must appear in all locales."""
        src = _read("static/i18n.js")
        locale_count = len(self._locale_names(src))
        count = self._count_key(src, self.REQUIRED_KEY)
        assert count >= locale_count, (
            f"model_not_found_label found {count} times, expected >= {locale_count} "
            f"(one per locale)"
        )

    def test_english_label_is_plain_string(self):
        """English model_not_found_label must be a plain string, not a function."""
        src = _read("static/i18n.js")
        en_start = src.find("\n  en: {")
        es_start = src.find("\n  es: {")
        en_block = src[en_start:es_start]
        assert self.REQUIRED_KEY in en_block, "Key not in en block"
        idx = en_block.find(self.REQUIRED_KEY)
        line = en_block[idx : idx + 200]
        assert "=>" not in line, (
            "model_not_found_label should be a plain string, not an arrow function"
        )
