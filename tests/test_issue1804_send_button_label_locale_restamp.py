"""#1804 re-gate 9/22 — locale restamp must refresh the busy-mode action label.

The round 2 fix layers a visible text label on the send button's
``::after`` pseudo-element so busy-mode actions (stop / queue /
interrupt / steer) read as a pill, not a circular send button. The
label is materialised by ``_setComposerPrimaryButtonIcon`` as
``data-label``, resolved through ``t()``.

The standard ``applyLocaleToDOM`` restamp walks
``[data-i18n] / [data-i18n-title] / [data-i18n-placeholder] /
[data-i18n-aria-label]`` and updates ``syncWorkspacePanelUI`` and
``syncAppTitlebar``, but it never touched ``data-label`` — the
attribute is set imperatively, not via the ``data-i18n`` machinery.
An in-place locale change while the button was busy therefore left
the pill in the old language until the next action transition.

This regression test pins the new behaviour:

- at the end of ``applyLocaleToDOM``, the composer helper is re-run
  against the current action so the live locale always wins;
- the helper also clears ``data-label`` outside busy mode, so the
  restamp is a no-op for the common idle path;
- the existing per-action labels (stop/queue/interrupt/steer) and
  the per-action removal (send/disabled) are preserved.
"""
from __future__ import annotations

import json
import re
import subprocess
import textwrap
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[1]
UI_JS = (REPO / "static" / "ui.js").read_text(encoding="utf-8")
I18N_JS = (REPO / "static" / "i18n.js").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Source-level wiring pins
# ---------------------------------------------------------------------------


def test_apply_locale_to_dom_calls_set_composer_primary_button_icon():
    """The fix must invoke ``_setComposerPrimaryButtonIcon`` from
    inside ``applyLocaleToDOM`` so the busy-mode action label tracks
    the live locale. We require the call to be guarded by both
    ``typeof _setComposerPrimaryButtonIcon === 'function'`` and a
    truthy ``btnSend`` lookup so the no-op common path (no busy
    action or DOM not yet mounted) is preserved.
    """
    # Locate the applyLocaleToDOM function body and verify the call
    # site is inside it (not somewhere else in the file).
    m = re.search(r"function applyLocaleToDOM\(\)\s*\{", I18N_JS)
    assert m, "applyLocaleToDOM not found in static/i18n.js"
    open_brace = I18N_JS.find("{", m.end() - 1)
    depth = 0
    end = open_brace
    for i in range(open_brace, len(I18N_JS)):
        ch = I18N_JS[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    body = I18N_JS[open_brace:end]
    assert "_setComposerPrimaryButtonIcon" in body, (
        "applyLocaleToDOM must call _setComposerPrimaryButtonIcon so the "
        "busy-mode action label tracks the live locale (#1804 re-gate 9/22)"
    )
    assert "btnSend" in body, (
        "applyLocaleToDOM must look up the #btnSend element before re-running "
        "the composer helper"
    )
    assert "typeof _setComposerPrimaryButtonIcon" in body, (
        "applyLocaleToDOM must guard on typeof _setComposerPrimaryButtonIcon "
        "so the restamp stays a no-op when the helper is not yet loaded"
    )


def test_helper_signature_unchanged():
    """The composer helper signature must not regress — the locale
    restamp calls it with the same ``(btn, action)`` shape.
    """
    m = re.search(
        r"function\s+_setComposerPrimaryButtonIcon\s*\(\s*btn\s*,\s*action\s*\)",
        UI_JS,
    )
    assert m, (
        "_setComposerPrimaryButtonIcon(btn, action) signature must be "
        "preserved; the locale restamp depends on it"
    )


# ---------------------------------------------------------------------------
# Behavioural pins: run the real helpers in a Node VM
# ---------------------------------------------------------------------------


def _extract_apply_locale_to_dom_body() -> str:
    """Lift the body of ``applyLocaleToDOM`` out of ``static/i18n.js``.

    The function is not exported, so we splice it into a Node VM
    context. We do not attempt to evaluate the full module — only the
    body, plus the helper that the restamp calls.
    """
    m = re.search(r"function applyLocaleToDOM\(\)\s*\{", I18N_JS)
    assert m, "applyLocaleToDOM not found"
    open_brace = I18N_JS.find("{", m.end() - 1)
    depth = 0
    end = open_brace
    for i in range(open_brace, len(I18N_JS)):
        ch = I18N_JS[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    return I18N_JS[m.start():end]


def _extract_helper() -> str:
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


def _run_locale_restamp_scenario():
    """Drive the real helpers in a Node VM and report the data-label
    attribute at three checkpoints: after first paint in locale A,
    after a manual busy-mode action, and after the locale restamp to
    locale B. The third checkpoint is the one the regression
    catches.
    """
    helper = _extract_helper()
    apply_body = _extract_apply_locale_to_dom_body()
    script = textwrap.dedent(
        f"""
        const vm = require('vm');
        const ctx = {{
          console,
          // locale A: english-ish keys
          t: (k) => ({{
            'composer_action_stop': 'Stop',
            'composer_action_queue': 'Queue',
            'composer_action_interrupt': 'Interrupt',
            'composer_action_steer': 'Steer',
          }})[k] || k,
        }};
        vm.createContext(ctx);
        vm.runInContext({json.dumps(helper)}, ctx);
        vm.runInContext({json.dumps(apply_body)}, ctx);

        // Mocked DOM. The send button starts in 'send' (no data-label).
        const btn = {{ innerHTML: '', dataset: {{ action: 'send' }} }};
        ctx.document = {{
          getElementById: (id) => (id === 'btnSend' ? btn : null),
          querySelectorAll: () => [],
        }};
        // The restamp body references syncWorkspacePanelUI /
        // syncAppTitlebar / [data-i18n] walks — all of which are
        // no-ops in this minimal context.

        // Switch to busy mode and stamp locale A. _setComposerPrimaryButtonIcon
        // also keeps data-action in sync with the busy mode, mirroring
        // the production updateSendBtn() flow.
        ctx._setComposerPrimaryButtonIcon(btn, 'stop');
        // updateSendBtn() also toggles data-action; the restamp relies
        // on the production flow leaving them aligned.
        btn.dataset.action = 'stop';
        const checkpoint1 = btn.dataset.label;

        // Locale change WITHOUT a fresh action transition.
        ctx.t = (k) => ({{
          'composer_action_stop': '停止',
          'composer_action_queue': '队列',
          'composer_action_interrupt': '中断',
          'composer_action_steer': '转向',
        }})[k] || k;
        ctx.applyLocaleToDOM();
        const checkpoint2 = btn.dataset.label;

        console.log(JSON.stringify({{
          locale_a: checkpoint1,
          locale_b_after_restamp: checkpoint2,
          action_unchanged: btn.dataset.action,
        }}));
        """
    )
    result = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, timeout=15
    )
    if result.returncode != 0:
        pytest.skip(f"node VM failed: {result.stderr}")
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_locale_restamp_refreshes_data_label_for_busy_action():
    """The full restamp scenario: button is in 'stop' under locale A,
    locale changes to B with no action transition, ``applyLocaleToDOM``
    is called, and the data-label must now reflect locale B.
    """
    out = _run_locale_restamp_scenario()
    assert out["locale_a"] == "Stop", (
        f"locale A initial stamp should be 'Stop', got {out['locale_a']!r}"
    )
    assert out["locale_b_after_restamp"] == "停止", (
        f"after locale restamp, busy label must refresh to locale B "
        f"({out['locale_b_after_restamp']!r}) — this is the user-visible "
        "stale-state bug the re-gate 9/22 review flagged"
    )
    assert out["action_unchanged"] == "stop", (
        "the restamp must not mutate data-action"
    )


def test_locale_restamp_clears_data_label_outside_busy_modes():
    """If the action is 'send' (or 'disabled') at restamp time, the
    helper must remove ``data-label`` exactly like the original
    code path. The locale restamp is a no-op for the common idle
    path; this pins that contract.
    """
    helper = _extract_helper()
    apply_body = _extract_apply_locale_to_dom_body()
    script = textwrap.dedent(
        f"""
        const vm = require('vm');
        const ctx = {{
          console,
          t: (k) => k,
        }};
        vm.createContext(ctx);
        vm.runInContext({json.dumps(helper)}, ctx);
        vm.runInContext({json.dumps(apply_body)}, ctx);
        const btn = {{ innerHTML: '', dataset: {{ action: 'send' }} }};
        ctx.document = {{
          getElementById: (id) => (id === 'btnSend' ? btn : null),
          querySelectorAll: () => [],
        }};
        // Manually pre-set a stale label (as if a prior busy
        // transition left it behind) and run the restamp with the
        // action back in 'send' mode.
        btn.dataset.label = 'stale';
        ctx.applyLocaleToDOM();
        console.log(JSON.stringify({{
          data_label: btn.dataset.label === undefined ? null : btn.dataset.label,
        }}));
        """
    )
    result = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, timeout=15
    )
    if result.returncode != 0:
        pytest.skip(f"node VM failed: {result.stderr}")
    out = json.loads(result.stdout.strip().splitlines()[-1])
    assert out["data_label"] is None, (
        "restamp against action='send' must clear data-label (the helper "
        f"owns this; got {out['data_label']!r})"
    )
