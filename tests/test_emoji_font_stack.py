"""Emoji font fallback should avoid Windows Segoe Emoji when possible."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STYLE_CSS = (ROOT / "static" / "style.css").read_text(encoding="utf-8")
TERMINAL_JS = (ROOT / "static" / "terminal.js").read_text(encoding="utf-8")
VENDOR = ROOT / "static" / "vendor" / "noto-color-emoji" / "0.2.0"


def test_noto_color_emoji_is_self_hosted_for_chrome_edge():
    assert STYLE_CSS.startswith('@import url("vendor/noto-color-emoji/0.2.0/index.css");')
    index_css = (VENDOR / "index.css").read_text(encoding="utf-8")
    assert "font-family: 'Noto Color Emoji'" in index_css
    assert "tech(color-COLRv1)" in index_css
    assert (VENDOR / "LICENSE").is_file()
    for idx in range(11):
        assert (VENDOR / "files" / f"{idx}.woff2").is_file()


def test_default_font_tokens_prefer_apple_then_self_hosted_noto_before_segoe():
    assert '--font-emoji:"Apple Color Emoji","Noto Color Emoji","Segoe UI Emoji","Segoe UI Symbol"' in STYLE_CSS
    emoji_idx = STYLE_CSS.index('--font-emoji:"Apple Color Emoji","Noto Color Emoji","Segoe UI Emoji","Segoe UI Symbol"')
    assert STYLE_CSS.index('"Apple Color Emoji"', emoji_idx) < STYLE_CSS.index('"Noto Color Emoji"', emoji_idx)
    assert STYLE_CSS.index('"Noto Color Emoji"', emoji_idx) < STYLE_CSS.index('"Segoe UI Emoji"', emoji_idx)
    assert '--font-ui:-apple-system,BlinkMacSystemFont,"Segoe UI",Inter,system-ui,var(--font-emoji),sans-serif;' in STYLE_CSS
    assert '--font-mono:ui-monospace,"SFMono-Regular","SF Mono",Menlo,Consolas,"Liberation Mono",var(--font-emoji),monospace;' in STYLE_CSS


def test_skin_font_ui_overrides_keep_emoji_fallback():
    css_without_comments = re.sub(r'/\*.*?\*/', '', STYLE_CSS, flags=re.S)
    declarations = re.findall(r'--font-ui:[^;]+;', css_without_comments)
    assert declarations, "expected at least one --font-ui declaration"
    missing = [decl for decl in declarations if 'var(--font-emoji)' not in decl]
    assert not missing, f"skin UI font declarations missing emoji fallback: {missing}"


def test_terminal_font_fallback_also_has_color_emoji_fonts():
    assert '"Apple Color Emoji","Noto Color Emoji","Segoe UI Emoji","Segoe UI Symbol",monospace' in TERMINAL_JS
