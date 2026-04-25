"""
Tests for issue #1044 — Mermaid CSP font violation.

Mermaid's built-in themes inject an @import for Google Fonts (Manrope) at
render time, which is blocked by the CSP's style-src directive. Fix: pass
fontFamily:'inherit' in themeVariables so Mermaid never requests an external
font URL.
"""

from pathlib import Path

ROOT = Path(__file__).parent.parent


def _ui_js() -> str:
    return (ROOT / "static" / "ui.js").read_text(encoding="utf-8")


class TestMermaidCSPFont:
    def test_mermaid_init_has_font_family_inherit(self):
        """themeVariables in mermaid.initialize() must set fontFamily to 'inherit'."""
        src = _ui_js()
        assert "fontFamily:'inherit'" in src, (
            "mermaid.initialize() themeVariables must set fontFamily:'inherit' "
            "to suppress the Google Fonts (Manrope) import that violates CSP"
        )

    def test_mermaid_init_no_google_fonts_url(self):
        """ui.js must not contain a hardcoded fonts.googleapis.com URL."""
        src = _ui_js()
        assert "fonts.googleapis.com" not in src, (
            "ui.js must not reference fonts.googleapis.com — use fontFamily:'inherit'"
        )

    def test_mermaid_font_family_inside_theme_variables_block(self):
        """fontFamily:'inherit' must be inside the themeVariables block of mermaid.initialize()."""
        src = _ui_js()
        init_idx = src.find("mermaid.initialize(")
        assert init_idx != -1, "mermaid.initialize() call not found in ui.js"
        # Find the themeVariables block after the initialize call
        tv_idx = src.find("themeVariables", init_idx)
        assert tv_idx != -1, "themeVariables not found inside mermaid.initialize()"
        font_idx = src.find("fontFamily:'inherit'", tv_idx)
        assert font_idx != -1, (
            "fontFamily:'inherit' must appear inside themeVariables in mermaid.initialize()"
        )
        # The closing brace of themeVariables should come after fontFamily
        close_brace = src.find("})", tv_idx)
        assert font_idx < close_brace, (
            "fontFamily:'inherit' must be inside the themeVariables block (before })"
        )
