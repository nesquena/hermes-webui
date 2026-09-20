"""Gruvbox skin registration and paired light/dark palette affordances."""

from pathlib import Path

REPO = Path(__file__).parent.parent
CSS = (REPO / "static" / "style.css").read_text(encoding="utf-8")
BOOT_JS = (REPO / "static" / "boot.js").read_text(encoding="utf-8")
CONFIG_PY = (REPO / "api" / "config.py").read_text(encoding="utf-8")
INDEX_HTML = (REPO / "static" / "index.html").read_text(encoding="utf-8")
SHARE_HTML = (REPO / "static" / "share.html").read_text(encoding="utf-8")
I18N_JS = (REPO / "static" / "i18n.js").read_text(encoding="utf-8")


def test_gruvbox_skin_is_registered_in_all_files():
    assert "{name:'Gruvbox'" in BOOT_JS
    assert "'gruvbox':1" in INDEX_HTML
    assert "'gruvbox':1" in SHARE_HTML
    assert '"gruvbox"' in CONFIG_PY


def test_gruvbox_light_palette_is_cream_paper():
    assert ':root[data-skin="gruvbox"]{' in CSS
    assert "--bg:#FBF1C7" in CSS
    assert "--sidebar:#EBDBB2" in CSS
    assert "--border:#D5C4A1" in CSS


def test_gruvbox_dark_palette_is_bg0():
    assert ':root.dark[data-skin="gruvbox"]' in CSS
    assert "--bg:#282828" in CSS
    assert "--sidebar:#3C3836" in CSS
    assert "--border:#45403D" in CSS


def test_gruvbox_accent_is_aqua():
    # Light uses the faded aqua step, dark the bright aqua step; hover sits on
    # the neutral aqua in both modes (one gruvbox step toward the middle).
    assert "--accent:#427B58" in CSS
    assert "--accent:#8EC07C" in CSS
    assert "--accent-hover:#689D6A" in CSS
    assert "--focus-ring:rgba(66,123,88,.30)" in CSS
    assert "--focus-ring:rgba(142,192,124,.30)" in CSS
    assert "--accent-rgb:142,192,124" in CSS


def test_gruvbox_i18n_lists_skin_in_all_locales():
    # There are 15 locales; each should now include gruvbox as the trailing skin.
    # 13 locales use ASCII closing paren, 2 Chinese locales use full-width paren.
    assert I18N_JS.count("gruvbox)") + I18N_JS.count("gruvbox）") == 15