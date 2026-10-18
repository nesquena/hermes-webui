"""Gruvbox skin registration and paired light/dark palette affordances."""

from pathlib import Path

from tests._gruvbox_contrast import failing_component_pairs, failing_pairs

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
    assert '"gruvbox"' in CONFIG_PY


def test_gruvbox_light_palette_is_cream_paper():
    assert "--bg:#FBF1C7" in GRUVBOX_LIGHT
    assert "--sidebar:#EBDBB2" in GRUVBOX_LIGHT
    assert "--border:#D5C4A1" in GRUVBOX_LIGHT
    assert "--muted:#5F564B" in GRUVBOX_LIGHT


def test_gruvbox_accent_is_aqua():
    # Light tokens are darkened off the raw gruvbox steps to clear WCAG AA
    # (4.5:1) on the cream surfaces; hover sits one step darker than accent.
    assert "--accent:#356747" in GRUVBOX_LIGHT
    assert "--accent-hover:#2C5A43" in GRUVBOX_LIGHT
    assert "--accent-text:#2F5D43" in GRUVBOX_LIGHT
    assert "--focus-ring:rgba(66,123,88,.30)" in GRUVBOX_LIGHT


def test_gruvbox_accent_is_aqua_dark():
    # Dark keeps the raw gruvbox bright-aqua steps; they already pass AA.
    assert "--accent:#8EC07C" in GRUVBOX_DARK
    assert "--accent-hover:#689D6A" in GRUVBOX_DARK
    assert "--accent-text:#9ECE8A" in GRUVBOX_DARK
    assert "--focus-ring:rgba(142,192,124,.30)" in GRUVBOX_DARK
    assert "--accent-rgb:142,192,124" in GRUVBOX_DARK


def test_gruvbox_dark_palette_is_bg0():
    assert "--bg:#282828" in GRUVBOX_DARK
    assert "--sidebar:#3C3836" in GRUVBOX_DARK
    assert "--border:#45403D" in GRUVBOX_DARK
    assert "--muted:#BDAE93" in GRUVBOX_DARK


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


def test_gruvbox_overrides_do_not_shadow_base_selector_substrings():
    # Other tests slice style.css between base rules via literal find()s, e.g.
    # tests/test_session_touch_actions.py slices from '.session-swipe-action-stack{'
    # to '.session-swipe-badge{'. A gruvbox override written as
    # '... .session-swipe-badge{' would shadow the first '.session-swipe-badge{'
    # occurrence and blank that slice. Keep the brace on its own line so the
    # literal base-selector substring only matches the real base rule.
    assert ':root[data-skin="gruvbox"] .session-swipe-badge{' not in CSS
    assert ':root.dark[data-skin="gruvbox"] .session-swipe-badge{' not in CSS
    assert ':root[data-skin="gruvbox"] .session-swipe-badge\n' in CSS
    assert ':root.dark[data-skin="gruvbox"] .session-swipe-badge\n' in CSS


def test_gruvbox_light_and_dark_text_pairs_meet_wcag_aa():
    # Every text token must clear 4.5:1 against every surface it can render on,
    # including the toast context (token color over a 14% self-mix on surface).
    failures = failing_pairs()
    assert not failures, (
        "gruvbox palette has WCAG AA (4.5:1) text failures: "
        + ", ".join(f"{m}/{t} on {s}={r:.2f}" for m, t, s, r in failures)
    )


def test_gruvbox_component_paint_pairs_meet_wcag_aa():
    # Component-level pairs the token matrix can't see: dark ink on the solid
    # aqua fills (Send Edit, Settings Save/Retry, unsaved-bar Save, New Chat,
    # Send, ...), the hard-coded tree/diff syntax steps on --code-bg, and the
    # elements whose opacity the skin pins back to 1 (search previews, session
    # meta, project/tool card toggles, provider cards, chips, log debug lines).
    failures = failing_component_pairs()
    assert not failures, (
        "gruvbox component pairs fail WCAG AA: "
        + ", ".join(f"{m}/{c} on {s}={r:.2f}" for m, c, s, r in failures)
    )


def test_gruvbox_dark_solid_fills_carry_dark_ink():
    # The four solid-accent controls the gate named: Send Edit, both Settings
    # Save selectors, autosave Retry — plus the unsaved-settings bar's Save,
    # whose inline color:#fff (panels.js) forces the !important.
    assert ':root.dark[data-skin="gruvbox"] .msg-edit-send' in CSS
    assert ':root.dark[data-skin="gruvbox"] #mainSettings .settings-btn' in CSS
    assert ':root.dark[data-skin="gruvbox"] #mainSettings .sm-btn[onclick^="saveSettings"]' in CSS
    assert ':root.dark[data-skin="gruvbox"] #mainSettings .settings-autosave-status button' in CSS
    assert ':root.dark[data-skin="gruvbox"] #settingsUnsavedBar button[onclick^="saveSettings"]' in CSS
    assert "{color:#1D2021!important;}" in CSS


def test_gruvbox_pins_opacity_to_one_where_master_faded_it():
    # Dark gruvbox: inherited base opacities composite the palette under AA;
    # the overrides restore them so the browser paints the full token.
    for selector in (
        ':root[data-skin="gruvbox"] .session-item .session-search-preview',
        ':root[data-skin="gruvbox"] .session-item.active .session-meta',
        ':root[data-skin="gruvbox"] .project-picker-create',
        ':root[data-skin="gruvbox"] .project-create-btn',
        ':root[data-skin="gruvbox"] .tool-card-more',
        ':root[data-skin="gruvbox"] .tool-cards-toggle button',
        ':root[data-skin="gruvbox"] .session-date-header',
        ':root[data-skin="gruvbox"] .provider-card-meta',
        ':root[data-skin="gruvbox"] .log-line-debug',
    ):
        assert selector in CSS, f"missing opacity fix for {selector}"
    assert "{opacity:1!important;}" in CSS


def test_gruvbox_search_hit_and_tree_bool_meet_aa():
    # Active-row search hit renders --strong; light tree booleans darkened one
    # gruvbox step (#AF3A03 -> #A23803) to clear 4.5:1 on the code surface.
    assert ':root[data-skin="gruvbox"] .session-item.active .session-search-hit{color:var(--strong);}' in CSS
    assert ':root[data-skin="gruvbox"] .tree-bool{color:#A23803;}' in CSS
