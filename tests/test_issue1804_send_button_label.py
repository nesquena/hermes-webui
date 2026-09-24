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


@pytest.mark.parametrize("key", [
    "composer_action_stop",
    "composer_action_queue",
    "composer_action_interrupt",
    "composer_action_steer",
])
def test_label_key_present_in_every_locale_block(key):
    """All 15 locale blocks (en + 13 translations + zh-Hant) must define
    the new key so the invariant ``test_*_locale_covers_english_keys``
    holds.
    """
    # The blocks are the top-level locale objects. We rely on the
    # existing invariant test for the count; here we just assert that
    # every block where the key is present defines it with a non-empty
    # string.
    pattern = re.compile(
        rf"\n    {re.escape(key)}:\s*'([^']*)',"
    )
    matches = pattern.findall(I18N_JS)
    assert len(matches) >= 15, (
        f"Expected >=15 locale entries for {key!r}, found {len(matches)}"
    )
    for v in matches:
        assert v.strip(), f"Empty translation for {key!r}"


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
