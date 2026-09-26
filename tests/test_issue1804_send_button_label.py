"""Regression tests for #1804: surface a text label on the busy-mode
primary action button (stop / queue / interrupt / steer).

Phase 1 of #1804 (New chat, Stop, Interrupt+Queue, Steer) is already
shipped in the WebUI via ``btnNewChat`` and the ``btnSend`` action
state machine in ``_setComposerPrimaryButtonIcon``. This PR layers a
visible text label on top of the existing icon-only button so users
do not need to hover to discover the current mode.

Two layers are under test:

- **CSS contract**: the label is a real ``<span class="send-btn-label">``
  child of ``#btnSend`` (positioned by the ``.send-btn`` flex container
  alongside the icon SVG). The pill shape (border-radius: 999px) and
  the typography (font-size / font-weight) are owned by a single
  ``.send-btn-label`` class rule. The label is intentionally NOT a
  ``::after`` pseudo-element because ``#btnSend`` is also
  ``.has-tooltip`` and the ``.has-tooltip::after`` rule (line 2110) is
  the single owner of that pseudo-element.
- **JS contract**: ``_setComposerPrimaryButtonIcon`` appends the
  ``<span class="send-btn-label">`` child for the four busy-mode
  actions (resolved through ``t()``) and rebuilds ``innerHTML`` to
  the icon-only form for send / disabled.

The DOM harness test in ``test_issue1804_send_button_label_dom.py``
confirms the label element actually renders with non-zero
``offsetWidth`` in a real Chromium instance — catching the silent
regression that the maintainer 9/24 review flagged (the original
``::after``-based approach shared the ``.has-tooltip::after``
pseudo-element, so the label was never visible).
"""
from __future__ import annotations

import json
import re
import textwrap
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[1]
STYLE_CSS = (REPO / "static" / "style.css").read_text(encoding="utf-8")
UI_JS = (REPO / "static" / "ui.js").read_text(encoding="utf-8")
I18N_JS = (REPO / "static" / "i18n.js").read_text(encoding="utf-8")


# ── CSS contract ───────────────────────────────────────────────────────


def test_send_btn_pill_shape_is_documented_for_busy_modes():
    """The four busy-mode actions must trigger a pill shape so the
    button no longer reads as a circular send button.
    """
    # All four selectors in one rule.
    pattern = re.compile(
        r"\.send-btn\[data-action=\"stop\"\]\s*,"
        r"\s*\.send-btn\[data-action=\"queue\"\]\s*,"
        r"\s*\.send-btn\[data-action=\"interrupt\"\]\s*,"
        r"\s*\.send-btn\[data-action=\"steer\"\]\s*\{[^}]*border-radius:\s*999px",
        re.DOTALL,
    )
    assert pattern.search(STYLE_CSS), (
        "All four busy-mode data-action selectors must share a pill "
        "border-radius rule so the button stops reading as a circular "
        "send button."
    )


def test_send_btn_label_uses_real_child_not_after_pseudo():
    """The busy-mode action label must be a real child element
    (``.send-btn-label``), not a ``::after`` pseudo-element. The
    send button is also ``.has-tooltip`` and the ``.has-tooltip::after``
    rule (style.css:2110) is the single owner of that pseudo-element
    for the hover tooltip; sharing it for the label would either
    replace the tooltip text with the action name or get overridden
    by the tooltip, leaving the busy-mode pill silently invisible
    (the exact regression the 9/24 review flagged).
    """
    # Scope-anchored search: look only inside the busy-mode rule and
    # any ::after rule that follows it. If we find a `::after { ... }`
    # block with a `content:` declaration for any of the four
    # busy-mode selectors, the test fails.
    busy_after = re.compile(
        r"\.send-btn\[data-action=\"(?:stop|queue|interrupt|steer)\"]\s*::after\s*\{[^}]*content\s*:",
        re.DOTALL,
    )
    assert not busy_after.search(STYLE_CSS), (
        "Found a `.send-btn[data-action=...]: ::after { content: ... }` "
        "rule — the busy-mode action label must be a real <span "
        "class=\"send-btn-label\"> child, not a ::after pseudo-element, "
        "because the send button is also .has-tooltip and the "
        ".has-tooltip::after rule owns that pseudo-element for the "
        "hover tooltip. The two pseudo-elements collide silently: the "
        "label is never visible in the busy-mode pill (see #1804 "
        "re-gate 9/24 review)."
    )
    # Positive pin: the label class must exist with layout-affecting
    # typography so it actually renders a visible string.
    label_class = re.compile(
        r"\.send-btn-label\s*\{[^}]*font-size\s*:[^;}]+;[^}]*font-weight\s*:[^;}]+;",
        re.DOTALL,
    )
    assert label_class.search(STYLE_CSS), (
        "The .send-btn-label class must declare font-size and font-weight "
        "so the busy-mode label is actually legible inside the pill."
    )


def test_send_btn_label_has_visible_typography():
    """The label class must declare layout-affecting typography
    (font-size, font-weight, letter-spacing, white-space:nowrap) so
    it renders a single-line, non-collapsing string inside the
    ``.send-btn`` flex container.
    """
    m = re.search(r"\.send-btn-label\s*\{[^}]*\}", STYLE_CSS, re.DOTALL)
    assert m, "Expected a .send-btn-label CSS rule with at least one declaration"
    block = m.group(0)
    assert "font-size" in block, "label class must set a font-size"
    assert "font-weight" in block, "label class must set a font-weight"
    # white-space:nowrap keeps "Interrupt" from wrapping inside the
    # narrow mobile pill.
    assert "white-space" in block and "nowrap" in block, (
        "label class must set white-space:nowrap so the pill does not "
        "wrap to two lines on narrow viewports"
    )


# ── JS contract ────────────────────────────────────────────────────────


def test_set_composer_primary_button_icon_inserts_label_span():
    """_setComposerPrimaryButtonIcon must append a real
    ``<span class="send-btn-label">`` child for the four busy-mode
    actions so the CSS class rule can pick it up. The text is
    resolved through ``t()`` with the locale key. The helper must
    NOT use the ``::after`` pseudo-element (no data-label attribute,
    no ``btn.dataset.label`` write) because ``#btnSend`` is also
    ``.has-tooltip`` and the tooltip owns that pseudo-element.
    """
    # Brace-counted extraction so the embedded ``{...}`` object literal
    # (the icons map and the label-keys map) does not truncate the
    # function body.
    i = UI_JS.find("function _setComposerPrimaryButtonIcon")
    assert i != -1
    brace_open = UI_JS.find("{", i)
    depth = 0
    end = brace_open
    for j in range(brace_open, len(UI_JS)):
        ch = UI_JS[j]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = j + 1
                break
    body = UI_JS[i:end]
    for action, key in (
        ("stop", "composer_action_stop"),
        ("queue", "composer_action_queue"),
        ("interrupt", "composer_action_interrupt"),
        ("steer", "composer_action_steer"),
    ):
        assert f"'{key}'" in body, (
            f"Missing i18n key reference for {action!r}: expected "
            f"{key!r} in _setComposerPrimaryButtonIcon."
        )
    # The label class must be referenced in innerHTML construction.
    assert '"send-btn-label"' in body or "'send-btn-label'" in body, (
        "_setComposerPrimaryButtonIcon must build innerHTML that includes "
        'a `<span class="send-btn-label">...</span>` child for busy modes.'
    )
    # Negative pin: the old data-label / ::after approach must not be
    # resurrected. The new approach writes the text into the span, not
    # into a data attribute consumed by CSS.
    assert "btn.dataset.label" not in body, (
        "_setComposerPrimaryButtonIcon must NOT write btn.dataset.label "
        "anymore — the label is a real child <span class=\"send-btn-label\">, "
        "not a ::after pseudo-element. (See #1804 re-gate 9/24 review.)"
    )
    # The label HTML escape must be defensive: translator-controlled
    # text lands in innerHTML, so the helper must escape &<>"' to
    # avoid a stored XSS regression.
    assert "&amp;" in body and "&lt;" in body, (
        "_setComposerPrimaryButtonIcon must HTML-escape the label text "
        "before injecting it into innerHTML."
    )


# ── i18n invariant ────────────────────────────────────────────────────


def _locale_block(text, lang):
    """Return the body of one top-level locale block (``lang: { ... }``) or
    ``None`` if the locale is not present. Used to assert per-locale key
    coverage without leaning on the regex's ``\\n    `` anchor (which can
    miss keys placed at the start of a block after a previous deletion).
    """
    m = re.search(
        rf"^  {re.escape(lang)}: \{{(.*?)(?=^  [a-z]+: \{{|\Z)",
        text, re.MULTILINE | re.DOTALL,
    )
    return m.group(1) if m else None


@pytest.mark.parametrize("lang", [
    "it", "ja", "ru", "es", "de", "zh", "pt", "ko", "fr", "cs", "tr", "pl", "vi",
])
@pytest.mark.parametrize("key", [
    "composer_action_stop",
    "composer_action_queue",
    "composer_action_interrupt",
    "composer_action_steer",
])
def test_label_key_absent_from_non_english_locale_block(lang, key):
    """#1804 re-gate 9/24 maintainer finding: the four new keys were
    English in all 14 non-English locales while the neighbouring tooltip
    keys (``composer_stop`` and friends) are translated. Fix is to make
    the English fallback explicit — drop the keys from every non-English
    block so ``t()`` resolves them via ``LOCALES.en`` (see static/i18n.js
    line ~27065: ``val = _locale[key] ?? LOCALES.en[key]``). The 9/24
    review accepted either translating or dropping; dropping keeps the
    i18n.js diff small and the English fallback honest in the locale
    files instead of a duplicate copy-paste of the en values.
    """
    body = _locale_block(I18N_JS, lang)
    assert body is not None, f"locale {lang!r} not present in i18n.js"
    # The key MUST NOT appear in non-English blocks. ``t()`` falls back
    # to LOCALES.en, so the busy-mode pill renders the English label
    # (the maintainer-accepted trade-off — see the comment in this file
    # and the review on PR #7686).
    assert not re.search(rf"^\s{{4}}{re.escape(key)}:", body, re.MULTILINE), (
        f"key {key!r} should be absent from locale {lang!r}; "
        "non-English blocks must rely on the LOCALES.en fallback so the "
        "English fallback is explicit (see #1804 re-gate 9/24 review)."
    )


@pytest.mark.parametrize("key", [
    "composer_action_stop",
    "composer_action_queue",
    "composer_action_interrupt",
    "composer_action_steer",
])
def test_label_key_present_only_in_en_block(key):
    """The four keys live in ``LOCALES.en`` (the i18n fallback) and
    nowhere else. With the 14 non-English blocks dropping them, the
    total occurrence count is exactly 1 (the en block).
    """
    pattern = re.compile(rf"\n    {re.escape(key)}:\s*'([^']*)',")
    matches = pattern.findall(I18N_JS)
    assert len(matches) == 1, (
        f"Expected exactly 1 entry for {key!r} (LOCALES.en only — "
        f"non-English blocks must drop the key so the English fallback "
        f"is explicit, per #1804 re-gate 9/24 review); found {len(matches)}"
    )
    body = _locale_block(I18N_JS, "en")
    assert body is not None, "en locale block missing"
    assert re.search(rf"^\s{{4}}{re.escape(key)}:", body, re.MULTILINE), (
        f"key {key!r} must live in LOCALES.en so t() can fall back to it"
    )
    # The English value must be a non-empty label string (not a raw key).
    assert matches[0].strip(), f"Empty en translation for {key!r}"


# ── Behavioural: run the helper in a Node VM to confirm the span is
#     materialised (per the #7649 review lesson: execute the helper,
#     don't grep it).


def _extract_helper() -> str:
    """Pull the literal source of _setComposerPrimaryButtonIcon from
    ui.js so the test exercises the real function rather than a copy.
    """
    i = UI_JS.find("function _setComposerPrimaryButtonIcon")
    assert i != -1
    brace_open = UI_JS.find("{", i)
    depth = 0
    end = brace_open
    for j, ch in enumerate(UI_JS[brace_open:], start=brace_open):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = j + 1
                break
    return UI_JS[i:end]


def test_helper_actually_inserts_label_span_in_node_vm():
    """Per the #7649 review lesson: run the helper, don't just grep
    the source. This drives the real function in a Node VM and
    confirms the ``<span class="send-btn-label">`` child lands in
    ``innerHTML`` for each busy mode and is absent for send/disabled.
    """
    import subprocess

    helper = _extract_helper()
    # Inline t() returns the key as-is so the test stays locale-free.
    script = textwrap.dedent(
        f"""
        const vm = require('vm');
        const ctx = {{
          console,
          t: (k) => k,
        }};
        vm.createContext(ctx);
        vm.runInContext({json.dumps(helper)}, ctx);
        const btn = {{ innerHTML: '' }};
        const actions = ['send', 'stop', 'queue', 'interrupt', 'steer', 'disabled'];
        const out = {{}};
        for (const a of actions) {{
          ctx._setComposerPrimaryButtonIcon(btn, a);
          out[a] = {{
            has_label: btn.innerHTML.includes('send-btn-label'),
            label_text: (btn.innerHTML.match(/send-btn-label">([^<]*)</) || [null, null])[1],
          }};
        }}
        console.log(JSON.stringify(out));
        """
    )
    result = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 0, f"Node VM failed: {result.stderr}"
    out = json.loads(result.stdout.strip().splitlines()[-1])
    # Busy-mode actions must carry a real label span; send/disabled
    # must not.
    assert out["stop"]["has_label"] is True, out
    assert out["queue"]["has_label"] is True, out
    assert out["interrupt"]["has_label"] is True, out
    assert out["steer"]["has_label"] is True, out
    assert out["send"]["has_label"] is False, out
    assert out["disabled"]["has_label"] is False, out
    # The label text must be the (untranslated) i18n key when t() is
    # the identity function, so locale-restamp can re-run t() and
    # swap the text in place.
    for action, key in (
        ("stop", "composer_action_stop"),
        ("queue", "composer_action_queue"),
        ("interrupt", "composer_action_interrupt"),
        ("steer", "composer_action_steer"),
    ):
        assert out[action]["label_text"] == key, (
            f"label text for {action!r} should equal i18n key {key!r} "
            f"when t() is the identity function, got {out[action]['label_text']!r}"
        )


def test_helper_escapes_label_text_in_innerhtml():
    """The label text is translator-controlled and lands in
    ``innerHTML``; the helper must HTML-escape ``&<>"'`` to avoid a
    stored-XSS regression if a locale file ever ships a stray
    character. (Default en values are clean strings, so this only
    matters if a translator adds markup; the escape is cheap and
    mandatory.)
    """
    import subprocess

    helper = _extract_helper()
    # t() returns a value with all five dangerous characters to make
    # sure each one is escaped.
    script = textwrap.dedent(
        f"""
        const vm = require('vm');
        const ctx = {{
          console,
          t: (k) => `<script>alert("x")</script>&'`,
        }};
        vm.createContext(ctx);
        vm.runInContext({json.dumps(helper)}, ctx);
        const btn = {{ innerHTML: '' }};
        ctx._setComposerPrimaryButtonIcon(btn, 'stop');
        console.log(btn.innerHTML);
        """
    )
    result = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 0, f"Node VM failed: {result.stderr}"
    rendered = result.stdout.strip().splitlines()[-1]
    assert "<script>" not in rendered, (
        f"label text must be HTML-escaped before innerHTML injection; "
        f"got {rendered!r}"
    )
    assert "&lt;script&gt;" in rendered, (
        f"label text should escape < and > to entities; got {rendered!r}"
    )
    assert "&amp;" in rendered, (
        f"label text should escape &; got {rendered!r}"
    )
    assert "&#39;" in rendered or "&apos;" in rendered, (
        f"label text should escape '; got {rendered!r}"
    )
    assert "&quot;" in rendered, (
        f"label text should escape \"; got {rendered!r}"
    )


# ── #1804 re-gate 9/24: hover tooltip sync ─────────────────────────────
#
# ``#btnSend`` is ``.has-tooltip`` and its hover tooltip is driven by
# the ``[data-tooltip]`` attribute (see ``.has-tooltip::after`` at
# static/style.css:2110). The static markup ships ``data-tooltip="Send
# message"`` (composer_send), so before the fix every busy mode
# surfaced "Send message" on hover even though ``title``/``aria-label``
# carried the correct mode name. The fix mirrors the resolved title
# into ``[data-tooltip]`` inside ``updateSendBtn`` so the hover
# tooltip, the screen-reader label, and the title stay in sync.


def test_update_send_btn_mirrors_title_into_data_tooltip():
    """``updateSendBtn`` must write the resolved title into the
    ``[data-tooltip]`` attribute for every action, so the
    ``.has-tooltip::after`` rule surfaces the same string on hover
    that ``title`` shows on the native browser tooltip. The
    extraction finds the literal source of ``updateSendBtn`` and
    asserts it sets ``data-tooltip`` to the same string used for
    ``title`` and ``aria-label``.
    """
    i = UI_JS.find("function updateSendBtn")
    assert i != -1, "updateSendBtn not found in static/ui.js"
    brace_open = UI_JS.find("{", i)
    depth = 0
    end = brace_open
    for j in range(brace_open, len(UI_JS)):
        ch = UI_JS[j]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = j + 1
                break
    body = UI_JS[i:end]
    # The mirror must happen after the title / aria-label writes so
    # the data-tooltip value is the *resolved* title (with locale
    # fallback), not a stale static string.
    title_idx = body.find("btn.title=")
    aria_idx = body.find("btn.setAttribute('aria-label'")
    if aria_idx == -1:
        aria_idx = body.find('btn.setAttribute("aria-label"')
    tooltip_idx = body.find("btn.setAttribute('data-tooltip'")
    if tooltip_idx == -1:
        tooltip_idx = body.find('btn.setAttribute("data-tooltip"')
    assert title_idx != -1, "updateSendBtn must still set btn.title"
    assert aria_idx != -1, (
        "updateSendBtn must still set aria-label (regression check)"
    )
    assert tooltip_idx != -1, (
        "updateSendBtn must mirror the resolved title into "
        "[data-tooltip] so the .has-tooltip::after hover tooltip "
        "matches the busy-mode action (see #1804 re-gate 9/24 "
        "review — data-tooltip was hard-coded 'Send message' on "
        "every busy mode)."
    )
    assert title_idx < tooltip_idx, (
        "data-tooltip mirror must happen after the title write so "
        "the tooltip carries the resolved mode name, not a stale "
        "value from a prior action."
    )
    assert aria_idx < tooltip_idx, (
        "data-tooltip mirror must happen after the aria-label write."
    )
    # The mirror must use the same variable that holds the resolved
    # title (_btnTitle), not a hard-coded "Send message" string —
    # otherwise the tooltip would still read "Send message" on the
    # busy modes, the exact regression the 9/24 review flagged.
    # Find the exact line that sets data-tooltip.
    tooltip_line = body[tooltip_idx:body.find("\n", tooltip_idx)]
    assert "_btnTitle" in tooltip_line, (
        f"data-tooltip mirror must use the resolved title variable, "
        f"not a hard-coded string: {tooltip_line!r}"
    )


# ── #1804 re-gate 9/24: cf-burger icon-only ────────────────────────────
#
# At the narrowest footer stage (cf-burger) the busy-mode send button
# would otherwise stay as a 74-108px pill and clip the mobile config
# burger (the fit pass already hides the workspace chip and the
# model/reasoning/toolsets/quota chips in cf-burger, leaving only the
# 44px workspace-files button + the 44px config button in
# .composer-left). The fix collapses the button back to a 34px round
# icon in cf-burger and hides the label, so the 34px width + 4px gap
# fits under even the 320px extreme-legacy phone rule. The CSS
# contract is pinned here so a future refactor that re-enables the
# pill in cf-burger trips this test directly.


CF_BURGER_BTN_RULE = re.compile(
    r"\.composer-footer\.cf-burger\s+\.send-btn\s*\{[^}]*\}",
    re.DOTALL,
)
CF_BURGER_LABEL_RULE = re.compile(
    r"\.composer-footer\.cf-burger\s+\.send-btn-label\s*\{[^}]*\}",
    re.DOTALL,
)
CF_BURGER_BTN_RULES = re.compile(
    r"\.composer-footer\.cf-burger\s+\.send-btn\[data-action=\"(?:stop|queue|interrupt|steer)\"\][^{}]*\{[^}]*\}",
    re.DOTALL,
)


def test_cf_burger_hides_send_btn_label():
    """The cf-burger stage must hide the ``.send-btn-label`` so the
    pill collapses to a 34px round icon and the 44px mobile config
    burger stays fully visible (the fit pass already hid the
    workspace / model / reasoning / toolsets / quota chips in
    cf-burger, so .composer-left is just the 44px workspace-files
    button + the 44px config button + the send button).
    """
    m = CF_BURGER_LABEL_RULE.search(STYLE_CSS)
    assert m, (
        "expected a `.composer-footer.cf-burger .send-btn-label { ... }` "
        "rule that hides the busy-mode label at the narrowest footer "
        "stage (#1804 re-gate 9/24 review)."
    )
    block = m.group(0)
    assert "display" in block and "none" in block, (
        f".send-btn-label must be display:none inside cf-burger, got: {block!r}"
    )


def test_cf_burger_collapses_send_btn_to_round_icon():
    """The cf-burger stage must reset the send button to a 34px round
    icon, overriding the busy-mode pill shape so the mobile config
    burger stays visible. The rule must cover the four busy-mode
    data-action selectors (stop / queue / interrupt / steer) AND the
    base ``.send-btn`` selector, with at least width, height, and
    border-radius set so the pill collapses to a round icon-only
    button.
    """
    base = CF_BURGER_BTN_RULE.search(STYLE_CSS)
    assert base, (
        "expected a `.composer-footer.cf-burger .send-btn { ... }` "
        "rule that collapses the button to a 34px round icon in the "
        "narrowest footer stage (#1804 re-gate 9/24 review)."
    )
    block = base.group(0)
    assert "width" in block and "34px" in block, (
        f"cf-burger .send-btn must set width:34px, got: {block!r}"
    )
    assert "height" in block and "34px" in block, (
        f"cf-burger .send-btn must set height:34px, got: {block!r}"
    )
    assert "border-radius" in block and "50%" in block, (
        f"cf-burger .send-btn must set border-radius:50%, got: {block!r}"
    )
    # The four busy-mode data-action selectors must share the same
    # collapse rule so the pill shape cannot override it via
    # specificity. The pattern matches either one combined rule
    # listing all four actions or a single base rule (the latter
    # already covers them because of CSS cascade order — the base
    # rule is later in the file than the busy-mode pill rule).
    # We assert at least one of the two patterns is present.
    combined = CF_BURGER_BTN_RULES.search(STYLE_CSS)
    assert combined is not None or base is not None, (
        "cf-burger must collapse the send button to a 34px round "
        "icon for all four busy-mode data-action selectors (stop / "
        "queue / interrupt / steer) so the pill shape cannot survive "
        "the cascade."
    )


def test_cf_burger_label_rule_uses_important():
    """The cf-burger label-hide rule must use ``!important`` (or
    come after the busy-mode label rule) so the pill cannot be
    resurrected by a later rule. The label rule is the only way
    to ensure the pill collapses to icon-only in cf-burger.
    """
    m = CF_BURGER_LABEL_RULE.search(STYLE_CSS)
    assert m, "cf-burger label rule missing"
    block = m.group(0)
    # Either !important OR the rule is positioned after the .send-btn-label
    # base rule. We check for !important because the existing label
    # base rule at line 2774 has no !important, and we need cf-burger
    # to win regardless of cascade.
    assert "!important" in block, (
        f"cf-burger .send-btn-label rule must use !important so "
        f"the busy-mode pill cannot be resurrected by a later rule: "
        f"{block!r}"
    )
