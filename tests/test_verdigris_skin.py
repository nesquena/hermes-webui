"""Verdigris skin registration and emerald/bronze palette affordances."""

from pathlib import Path

REPO = Path(__file__).parent.parent
CSS = (REPO / "static" / "style.css").read_text(encoding="utf-8")
BOOT_JS = (REPO / "static" / "boot.js").read_text(encoding="utf-8")
CONFIG_PY = (REPO / "api" / "config.py").read_text(encoding="utf-8")
INDEX_HTML = (REPO / "static" / "index.html").read_text(encoding="utf-8")
I18N_JS = (REPO / "static" / "i18n.js").read_text(encoding="utf-8")


def test_verdigris_skin_is_registered_in_all_files():
    assert "{name:'Verdigris'" in BOOT_JS
    assert "'verdigris':1" in INDEX_HTML
    assert '"verdigris"' in CONFIG_PY


def test_verdigris_dark_palette_is_emerald():
    assert ':root.dark[data-skin="verdigris"]' in CSS
    assert "--bg:#0F1714" in CSS
    assert "--sidebar:#121D18" in CSS
    assert "--border:#22342C" in CSS


def test_verdigris_accent_is_bronze():
    assert "--accent:#C89A5A" in CSS
    assert "--accent-hover:#D6AE74" in CSS
    assert "--focus-ring:rgba(200,154,90,.30)" in CSS


def test_verdigris_has_no_light_variant():
    # The skin is dark-only; no light root block should be registered.
    assert ':root[data-skin="verdigris"]{' not in CSS
    assert ':root[data-skin=\"verdigris\"]{\n' not in CSS


def test_verdigris_i18n_lists_skin_in_all_locales():
    # There are 12 locales; each should now include verdigris as the trailing skin.
    # 10 locales use ASCII closing paren, 2 Chinese locales use full-width paren.
    assert I18N_JS.count("verdigris)") + I18N_JS.count("verdigris）") == 15
