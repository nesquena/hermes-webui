"""Gruvbox skin registration and paired light/dark palette affordances."""

from pathlib import Path

REPO = Path(__file__).parent.parent
CSS = (REPO / "static" / "style.css").read_text(encoding="utf-8")
BOOT_JS = (REPO / "static" / "boot.js").read_text(encoding="utf-8")
CONFIG_PY = (REPO / "api" / "config.py").read_text(encoding="utf-8")
INDEX_HTML = (REPO / "static" / "index.html").read_text(encoding="utf-8")
SHARE_HTML = (REPO / "static" / "share.html").read_text(encoding="utf-8")
I18N_JS = (REPO / "static" / "i18n.js").read_text(encoding="utf-8")


def _skin_block(css: str, selector: str) -> str:
    """The full declaration block for a `selector{...}` rule, else ''."""
    start = css.find(selector)
    while start != -1:
        open_brace = css.find("{", start)
        if open_brace != -1 and "{" not in css[start + len(selector) : open_brace]:
            close_brace = css.find("}", open_brace)
            if close_brace != -1:
                return css[start : close_brace + 1]
        start = css.find(selector, start + 1)
    return ""


GRUVBOX_LIGHT = _skin_block(CSS, ':root[data-skin="gruvbox"]{')
GRUVBOX_DARK = _skin_block(CSS, ':root.dark[data-skin="gruvbox"]{')
assert GRUVBOX_LIGHT and GRUVBOX_DARK


def test_gruvbox_skin_is_registered_in_all_files():
    assert "{name:'Gruvbox'" in BOOT_JS
    assert "'gruvbox':1" in INDEX_HTML
    assert "'gruvbox':1" in SHARE_HTML
    assert '"gruvbox"' in CONFIG_PY


def test_gruvbox_light_palette_is_cream_paper():
    assert "--bg:#FBF1C7" in GRUVBOX_LIGHT
    assert "--sidebar:#EBDBB2" in GRUVBOX_LIGHT
    assert "--border:#D5C4A1" in GRUVBOX_LIGHT
    assert "--muted:#5F564B" in GRUVBOX_LIGHT


def test_gruvbox_dark_palette_is_bg0():
    assert "--bg:#282828" in GRUVBOX_DARK
    assert "--sidebar:#3C3836" in GRUVBOX_DARK
    assert "--border:#45403D" in GRUVBOX_DARK
    assert "--muted:#A89984" in GRUVBOX_DARK


def test_gruvbox_accent_is_aqua():
    # Light uses the faded aqua step, dark the bright aqua step; hover sits on
    # the neutral aqua in both modes (one gruvbox step toward the middle).
    assert "--accent:#427B58" in GRUVBOX_LIGHT
    assert "--accent-text:#2C5A43" in GRUVBOX_LIGHT  # darkened for 4.5:1 on bg1/bg2
    assert "--accent:#8EC07C" in GRUVBOX_DARK
    assert "--accent-hover:#689D6A" in GRUVBOX_LIGHT
    assert "--accent-hover:#689D6A" in GRUVBOX_DARK
    assert "--focus-ring:rgba(66,123,88,.30)" in GRUVBOX_LIGHT
    assert "--focus-ring:rgba(142,192,124,.30)" in GRUVBOX_DARK
    assert "--accent-rgb:142,192,124" in GRUVBOX_DARK


def test_gruvbox_scrollbar_tint_covers_descendant_scrollers():
    # :root-only rules are overridden by the later light-mode generic gray rules
    # on descendant scrolling regions, so both root and descendant selectors
    # must carry the aqua tint (and the standard-property fallback per mode).
    assert ':root[data-skin="gruvbox"]::-webkit-scrollbar-thumb' in CSS
    assert ':root[data-skin="gruvbox"] ::-webkit-scrollbar-thumb' in CSS
    assert ':root[data-skin="gruvbox"] *{scrollbar-color:' in CSS
    assert ':root.dark[data-skin="gruvbox"]::-webkit-scrollbar-thumb' in CSS
    assert ':root.dark[data-skin="gruvbox"] ::-webkit-scrollbar-thumb' in CSS
    assert ':root.dark[data-skin="gruvbox"] *{scrollbar-color:' in CSS


def test_gruvbox_i18n_lists_skin_in_all_locales():
    # There are 15 locales; each should now include gruvbox as the trailing skin.
    # 13 locales use ASCII closing paren, 2 Chinese locales use full-width paren.
    assert I18N_JS.count("gruvbox)") + I18N_JS.count("gruvbox）") == 15
