"""CoreWeave skin registration and brand palette affordances."""

from pathlib import Path

REPO = Path(__file__).parent.parent
CSS = (REPO / "static" / "style.css").read_text(encoding="utf-8")
BOOT_JS = (REPO / "static" / "boot.js").read_text(encoding="utf-8")
INDEX_HTML = (REPO / "static" / "index.html").read_text(encoding="utf-8")
I18N_JS = (REPO / "static" / "i18n.js").read_text(encoding="utf-8")


def test_coreweave_skin_is_registered_in_all_files():
    assert "{name:'CoreWeave'" in BOOT_JS
    assert "coreweave:1" in INDEX_HTML


def test_coreweave_light_palette_is_cw_off_white():
    assert ':root[data-skin="coreweave"]' in CSS
    assert "--bg:#F9FAFC" in CSS
    assert "--sidebar:#F2F4F7" in CSS
    assert "--border:#E2E6EB" in CSS


def test_coreweave_light_accent_is_cw_blue():
    assert "--accent:#2741E7" in CSS
    assert "--accent-hover:#0541E9" in CSS
    assert "--focus-ring:rgba(39,65,231,.25)" in CSS


def test_coreweave_dark_palette_is_deep_grey():
    assert ':root.dark[data-skin="coreweave"]' in CSS
    assert "--bg:#191919" in CSS
    assert "--sidebar:#1E1E1E" in CSS
    assert "--border:#2A2A2A" in CSS


def test_coreweave_dark_accent_is_bright_blue():
    assert "--accent:#63A4FF" in CSS
    assert "--accent-hover:#7EB8FF" in CSS
    assert "--focus-ring:rgba(99,164,255,.30)" in CSS


def test_coreweave_has_both_variants():
    # This skin is full dual-mode (light and dark)
    assert ':root[data-skin="coreweave"]{' in CSS
    assert ':root.dark[data-skin="coreweave"]{' in CSS


def test_coreweave_component_overrides_exist():
    assert ".new-chat-btn" in CSS
    assert ".send-btn" in CSS
    assert ".session-item.active" in CSS
    assert ".tool-card" in CSS
    assert ".composer-box" in CSS


def test_coreweave_i18n_lists_skin_in_all_locales():
    # 10 locales use ASCII closing paren, 2 Chinese locales use full-width paren.
    assert I18N_JS.count("coreweave)") + I18N_JS.count("coreweave）") == 15
