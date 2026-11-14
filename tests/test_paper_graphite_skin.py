"""Paper Graphite skin registration, palette affordances and danger semantics."""

import re
from pathlib import Path

REPO = Path(__file__).parent.parent
from tests.skin_css import read_base_css, read_skin_css

SKIN = "paper-graphite"
SKIN_CSS = read_skin_css(SKIN)
CSS = read_base_css() + SKIN_CSS
BOOT_JS = (REPO / "static" / "boot.js").read_text(encoding="utf-8")
CONFIG_PY = (REPO / "api" / "config.py").read_text(encoding="utf-8")
INDEX_HTML = (REPO / "static" / "index.html").read_text(encoding="utf-8")
I18N_JS = (REPO / "static" / "i18n.js").read_text(encoding="utf-8")


def test_paper_graphite_is_registered_in_all_files():
    assert "{name:'Paper Graphite'" in BOOT_JS
    assert "value:'paper-graphite'" in BOOT_JS
    assert "'paper-graphite':1" in INDEX_HTML
    assert '"paper-graphite"' in CONFIG_PY


def test_paper_graphite_i18n_lists_skin_in_all_locales():
    # One entry per locale inside the /theme help string.
    assert I18N_JS.count("/paper-graphite/") == 15
    # Guard: inserting before verdigris keeps the existing verdigris assertion
    # (which counts `verdigris)` literally) intact.
    assert I18N_JS.count("verdigris)") + I18N_JS.count("verdigris）") == 15


def test_paper_graphite_light_palette_is_graphite_on_paper():
    assert ':root[data-skin="paper-graphite"]{' in CSS
    assert "--bg:#FFFFFF" in CSS
    assert "--sidebar:#F9F9F9" in CSS
    assert "--surface:#F7F7F7" in CSS
    assert "--border:#E7E7E7" in CSS
    assert "--text:#1A1A1A" in CSS


def test_paper_graphite_dark_palette_is_gray_matte():
    assert ':root.dark[data-skin="paper-graphite"]' in CSS
    assert "--bg:#181818" in CSS
    assert "--sidebar:#171717" in CSS
    assert "--surface:#212121" in CSS
    assert "--border:#2A2A2A" in CSS
    assert "--text:#F5F5F5" in CSS


def test_paper_graphite_accent_is_interaction_blue():
    assert "--accent:#0285FF" in CSS
    assert "--accent-hover:#0169CC" in CSS
    assert "--focus-ring:rgba(2,133,255,0.30)" in CSS
    assert "--accent:#339CFF" in CSS
    assert "--focus-ring:rgba(51,156,255,0.40)" in CSS


def test_paper_graphite_carries_the_openai_radius_scale():
    assert "--radius-sm:4px" in CSS
    assert "--radius-md:8px" in CSS
    assert "--radius-card:12px" in CSS
    assert "--codex-radius-control:10px" in CSS
    assert "--codex-radius-composer:20px" in CSS


def test_paper_graphite_sets_reading_rhythm_tokens():
    assert "--message-body-line-height:1.625" in CSS
    assert "--msg-max:880px" in CSS


def test_paper_graphite_repaints_hardcoded_dialog_surfaces():
    # The base .app-dialog rules hardcode a warm paper gradient (light) and a
    # navy gradient (dark), so a token-only skin would leave dialogs off-palette.
    assert '.app-dialog{background:var(--bg)!important' in CSS
    assert ':root.dark[data-skin="paper-graphite"] .app-dialog{background:var(--surface)!important' in CSS
    assert '.app-dialog-btn.confirm{background:var(--accent)!important' in CSS


def test_paper_graphite_keeps_danger_semantic():
    """Destructive confirmations must stay on --error, not the interaction accent.

    ``showConfirmDialog()`` emits ``.confirm.danger`` for the batch-delete path.
    The skin's plain ``.confirm`` rule is ``!important`` and more specific than
    the stock ``.app-dialog-btn.confirm.danger`` rule, so without an explicit
    ``.confirm.danger`` pair the red warning turns blue.
    """
    danger = re.findall(
        r':root(?:\.dark)?\[data-skin="paper-graphite"\]\s*\.app-dialog-btn\.confirm\.danger'
        r'(?::hover)?\{([^}]*)\}', SKIN_CSS)
    assert len(danger) == 2, f"expected normal + hover .confirm.danger rules, got {len(danger)}"
    for body in danger:
        assert "border-color:var(--error)!important" in body, body
        assert "color:var(--error)!important" in body, body
        assert "var(--accent)" not in body, "danger must not use the interaction accent"
        assert "color-mix(in srgb,var(--error)" in body, body
    # Both must be !important to beat the base rule, which is itself !important.
    # Declaration order is not load-bearing here: `.confirm.danger` carries one
    # more class than `.confirm`, so specificity decides. This function only
    # guards the declarations' shape; the behavioural proof is the native-browser
    # computed-style coverage in tests/test_paper_graphite_danger_browser.py.
    # (A CSS-text assertion cannot see the difference: dropping `!important` keeps
    # every declaration and every substring intact while the button turns blue.)
    assert SKIN_CSS.count(".app-dialog-btn.confirm.danger{") == 1
    assert SKIN_CSS.count(".app-dialog-btn.confirm.danger:hover{") == 1


def test_paper_graphite_does_not_regress_default_skin():
    # The default skin clears data-skin, so its own palette must stay untouched.
    assert ':root[data-skin="paper-graphite"]' not in BOOT_JS
