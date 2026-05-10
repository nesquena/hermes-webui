"""Tests for #1112 — CSP allows Google Fonts stylesheet and font files."""

import re


def _helpers_src() -> str:
    with open("api/helpers.py") as f:
        return f.read()


class TestCSPGoogleFonts:
    """style-src and font-src must allow fonts.googleapis.com / fonts.gstatic.com."""

    def test_style_src_includes_google_fonts(self):
        """style-src must include https://fonts.googleapis.com for Google Fonts CSS."""
        src = _helpers_src()
        assert "https://fonts.googleapis.com" in src, (
            "style-src must allow fonts.googleapis.com (Google Fonts stylesheets)"
        )
        # Must be in the style-src directive, not accidentally elsewhere
        style_match = re.search(r"style-src\s+([^;]+);", src)
        assert style_match, "style-src directive must exist"
        assert "fonts.googleapis.com" in style_match.group(1), (
            "fonts.googleapis.com must be in style-src directive"
        )

    def test_font_src_includes_fonts_gstatic(self):
        """font-src must include https://fonts.gstatic.com for Google Font files."""
        src = _helpers_src()
        assert "https://fonts.gstatic.com" in src, (
            "font-src must allow fonts.gstatic.com (Google Font WOFF2/WOFF files)"
        )
        # Must be in the font-src directive
        font_match = re.search(r"font-src\s+([^;]+);", src)
        assert font_match, "font-src directive must exist"
        assert "fonts.gstatic.com" in font_match.group(1), (
            "fonts.gstatic.com must be in font-src directive"
        )

    def test_existing_csp_directives_preserved(self):
        """All pre-existing CSP directives must still be present after the fix."""
        src = _helpers_src()
        for directive in (
            "default-src 'self'",
            "script-src 'self' 'unsafe-inline'",
            "style-src 'self' 'unsafe-inline'",
            "img-src 'self' data:",
            "font-src 'self' data:",
            "connect-src 'self'",
            "manifest-src 'self'",
            "base-uri 'self'",
            "form-action 'self'",
        ):
            assert directive in src, f"CSP must still contain: {directive}"
