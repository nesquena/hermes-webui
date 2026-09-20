"""Regression tests for #1804: surface a text label on the busy-mode
primary action button (stop / queue / interrupt / steer).

Phase 1 of #1804 (New chat, Stop, Interrupt+Queue, Steer) is already
shipped in the WebUI via ``btnNewChat`` and the ``btnSend`` action
state machine in ``_setComposerPrimaryButtonIcon``. This PR layers a
visible text label on top of the existing icon-only button so users
do not need to hover to discover the current mode.

Two layers are under test:

- **CSS contract**: the ``::after`` pseudo-element reads
  ``content: attr(data-label)`` so the button pill surfaces the
  current action name in any locale.
- **JS contract**: ``_setComposerPrimaryButtonIcon`` sets a
  ``data-label`` attribute on the button (resolved through ``t()``)
  for the four busy-mode actions, and removes it for send/disabled.
"""
from __future__ import annotations

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
        r"\.send-btn\[data-action=\"stop\"\],\s*"
        r"\.send-btn\[data-action=\"queue\"\],\s*"
        r"\.send-btn\[data-action=\"interrupt\"\],\s*"
        r"\.send-btn\[data-action=\"steer\"\]\s*\{[^}]*border-radius:\s*999px",
        re.DOTALL,
    )
    assert pattern.search(STYLE_CSS), (
        "All four busy-mode data-action selectors must share a pill "
        "border-radius rule so the button stops reading as a circular "
        "send button."
    )


def test_send_btn_after_pseudo_reads_data_label():
    """The visible label must come from the ``data-label`` attribute so
    i18n via ``t()`` flows through without touching CSS. ``::after``
    with ``attr()`` is the supported way to do this.
    """
    pattern = re.compile(
        r"\.send-btn\[data-action=\"(?:stop|queue|interrupt|steer)\"]::after\s*\{[^}]*"
        r"content:\s*attr\(data-label\)",
        re.DOTALL,
    )
    assert pattern.search(STYLE_CSS), (
        "The ::after pseudo-element for busy-mode actions must read "
        "content:attr(data-label) so the JS-resolved label shows up."
    )


def test_send_btn_pill_label_has_visible_typography():
    """The label pseudo-element must have non-default font-size and
    font-weight so it is actually legible inside the 34 px button.
    """
    # Find the ::after block and assert it sets font-size and font-weight.
    m = re.search(
        r"\.send-btn\[data-action=\"stop\"\],\s*"
        r"\.send-btn\[data-action=\"queue\"\],\s*"
        r"\.send-btn\[data-action=\"interrupt\"\],\s*"
        r"\.send-btn\[data-action=\"steer\"\]\s*\{[^}]*\}",
        STYLE_CSS,
        re.DOTALL,
    )
    # Find the ::after rule that comes right after.
    after_idx = STYLE_CSS.find("::after", m.end() if m else 0)
    assert after_idx != -1
    block_end = STYLE_CSS.find("}", after_idx)
    block = STYLE_CSS[after_idx:block_end]
    assert "font-size" in block, "label ::after must set a font-size"
    assert "font-weight" in block, "label ::after must set a font-weight"


# ── JS contract ────────────────────────────────────────────────────────


def test_set_composer_primary_button_icon_sets_data_label():
    """_setComposerPrimaryButtonIcon must set ``data-label`` for the
    four busy-mode actions so CSS can pick it up. The label is
    resolved through ``t()`` with the locale key.
    """
    # Brace-counted extraction so the embedded ``{...}`` object literal
    # (the icons map) does not truncate the function body.
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
    # The data-label assignment must be conditional on the action
    # being one of the four busy-mode actions, not the send/disabled
    # case (those should not surface a label).
    assert "btn.dataset.label" in body, (
        "_setComposerPrimaryButtonIcon must write btn.dataset.label"
    )
    assert "delete btn.dataset.label" in body, (
        "send/disabled actions must clear data-label so the pill shape "
        "collapses back to the circular send button."
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
    m = re.search(
        r"const\s+locale\s*=\s*\{|\n  en:\s*\{",
        I18N_JS,
    )
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


# ── Behavioural: run the helper in a Node VM to confirm the attr is set
#     (per the #7649 review lesson: execute the helper, don't grep it).


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


def test_helper_actually_sets_data_label_in_node_vm():
    """Per the #7649 review lesson: run the helper, don't just grep
    the source. This drives the real function in a Node VM and
    confirms ``data-label`` lands on the button for each busy mode.
    """
    import subprocess
    import json

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
        const btn = {{ innerHTML: '', dataset: {{}} }};
        const actions = ['send', 'stop', 'queue', 'interrupt', 'steer', 'disabled'];
        const out = {{}};
        for (const a of actions) {{
          ctx._setComposerPrimaryButtonIcon(btn, a);
          out[a] = btn.dataset.label === undefined ? null : btn.dataset.label;
        }}
        console.log(JSON.stringify(out));
        """
    )
    result = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 0, f"Node VM failed: {result.stderr}"
    out = json.loads(result.stdout.strip().splitlines()[-1])
    # Busy-mode actions must carry a label; send/disabled must not.
    assert out["stop"] == "composer_action_stop"
    assert out["queue"] == "composer_action_queue"
    assert out["interrupt"] == "composer_action_interrupt"
    assert out["steer"] == "composer_action_steer"
    assert out["send"] is None
    assert out["disabled"] is None
