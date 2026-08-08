"""Nous Blue skin registration and palette affordances.

Matches the Hermes desktop app's canonical 'nous' theme (NOUS_BLUE #0053FD),
so mobile/browser WebUI clients can present the same identity as the desktop.
"""

from pathlib import Path

REPO = Path(__file__).parent.parent
CSS = (REPO / "static" / "style.css").read_text(encoding="utf-8")
BOOT_JS = (REPO / "static" / "boot.js").read_text(encoding="utf-8")
CONFIG_PY = (REPO / "api" / "config.py").read_text(encoding="utf-8")
INDEX_HTML = (REPO / "static" / "index.html").read_text(encoding="utf-8")
I18N_JS = (REPO / "static" / "i18n.js").read_text(encoding="utf-8")


def test_nousblue_skin_is_registered_in_all_files():
    assert "{name:'Nous Blue'" in BOOT_JS
    assert "'nousblue':1" in INDEX_HTML
    assert '"nousblue"' in CONFIG_PY


def test_nousblue_light_palette_is_nous_blue():
    assert ':root[data-skin="nousblue"]{' in CSS
    assert "--bg:#F8FAFF" in CSS
    assert "--text:#17171A" in CSS
    assert "--accent:#0053FD" in CSS


def test_nousblue_dark_palette_is_deep_blue_with_cream():
    assert ':root.dark[data-skin="nousblue"]{' in CSS
    assert "--bg:#0D2F86" in CSS
    assert "--text:#FFE6CB" in CSS
    assert "--accent:#FFE6CB" in CSS
    assert "--border:#3158AD" in CSS


def test_nousblue_has_both_light_and_dark_variants():
    # Unlike the dark-only verdigris skin, Nous Blue ships light + dark.
    assert ':root[data-skin="nousblue"]{' in CSS
    assert ':root.dark[data-skin="nousblue"]{' in CSS


def test_nousblue_i18n_lists_skin_in_all_locales():
    # nousblue is inserted before the trailing verdigris in every locale's
    # /theme help string: 13 locales use the ASCII closing paren, 2 Chinese
    # locales use the full-width paren.
    assert (
        I18N_JS.count("nousblue/verdigris)") + I18N_JS.count("nousblue/verdigris）") == 15
    )
