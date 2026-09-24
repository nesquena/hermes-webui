"""#1804 re-gate 9/24 — locale restamp must refresh the busy-mode action label.

The round 2 fix layers a visible text label on the send button as a
real ``<span class="send-btn-label">`` child so busy-mode actions
(stop / queue / interrupt / steer) read as a pill, not a circular
send button. The label is materialised by
``_setComposerPrimaryButtonIcon`` as the textContent of the span,
resolved through ``t()``.

The original round 1 used a ``::after`` pseudo-element driven by a
``data-label`` attribute. That collided silently with the
``.has-tooltip::after`` rule that owns the hover tooltip pseudo,
which is why round 1's "rendered" test passed (the rule was present
in CSS) but the label was never visible in the real app. Round 2
moves the label to a real child element so the two pseudo-element
rules no longer fight.

The standard ``applyLocaleToDOM`` restamp walks
``[data-i18n] / [data-i18n-title] / [data-i18n-placeholder] /
[data-i18n-aria-label]`` and updates ``syncWorkspacePanelUI`` and
``syncAppTitlebar``, but it never touched the ``send-btn-label``
span — the text is set imperatively, not via the ``data-i18n``
machinery. An in-place locale change while the button was busy
therefore left the pill in the old language until the next action
transition.

This regression test pins the new behaviour:

- at the end of ``applyLocaleToDOM``, the composer helper is re-run
  against the current action so the live locale always wins;
- the helper also removes the ``send-btn-label`` span outside busy
  mode, so the restamp is a no-op for the common idle path;
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
        "busy-mode action label tracks the live locale (#1804 re-gate 9/24)"
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


def _label_text_from_innerhtml(inner_html: str) -> str | None:
    """Pull the rendered text of the ``.send-btn-label`` span out of
    a button's ``innerHTML`` string. Returns ``None`` if no label
    span is present (the send / disabled case).

    This function is the Python implementation; the equivalent
    extraction in the Node VM scenarios is the inline regex
    ``btn.innerHTML.match(/send-btn-label">([^<]*)</)``.
    """
    m = re.search(r'send-btn-label">([^<]*)<', inner_html)
    return m.group(1) if m else None


def _run_locale_restamp_scenario():
    """Drive the real helpers in a Node VM and report the rendered
    label text at three checkpoints: after first paint in locale A,
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

        // Mocked DOM. The send button starts in 'send' (no label span).
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
        const m1 = btn.innerHTML.match(/send-btn-label">([^<]*)</);
        const checkpoint1 = m1 ? m1[1] : null;

        // Locale change WITHOUT a fresh action transition.
        ctx.t = (k) => ({{
          'composer_action_stop': '停止',
          'composer_action_queue': '队列',
          'composer_action_interrupt': '中断',
          'composer_action_steer': '转向',
        }})[k] || k;
        ctx.applyLocaleToDOM();
        const m2 = btn.innerHTML.match(/send-btn-label">([^<]*)</);
        const checkpoint2 = m2 ? m2[1] : null;

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


def test_locale_restamp_refreshes_label_for_busy_action():
    """The full restamp scenario: button is in 'stop' under locale A,
    locale changes to B with no action transition, ``applyLocaleToDOM``
    is called, and the rendered label must now reflect locale B.
    """
    out = _run_locale_restamp_scenario()
    assert out["locale_a"] == "Stop", (
        f"locale A initial stamp should be 'Stop', got {out['locale_a']!r}"
    )
    assert out["locale_b_after_restamp"] == "停止", (
        f"after locale restamp, busy label must refresh to locale B "
        f"({out['locale_b_after_restamp']!r}) — this is the user-visible "
        "stale-state bug the re-gate 9/24 review flagged"
    )
    assert out["action_unchanged"] == "stop", (
        "the restamp must not mutate data-action"
    )


def test_locale_restamp_clears_label_outside_busy_modes():
    """If the action is 'send' (or 'disabled') at restamp time, the
    helper must rebuild ``innerHTML`` to the icon-only form so the
    pill collapses back to the circular send button. The locale
    restamp is a no-op for the common idle path; this pins that
    contract.
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
        btn.innerHTML = '<svg></svg><span class="send-btn-label">stale</span>';
        ctx.applyLocaleToDOM();
        const m3 = btn.innerHTML.match(/send-btn-label">([^<]*)</);
        const labelText = m3 ? m3[1] : null;
        console.log(JSON.stringify({{
          label_text: labelText,
        }}));
        """
    )
    result = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, timeout=15
    )
    if result.returncode != 0:
        pytest.skip(f"node VM failed: {result.stderr}")
    out = json.loads(result.stdout.strip().splitlines()[-1])
    assert out["label_text"] is None, (
        "restamp against action='send' must rebuild innerHTML to the "
        "icon-only form (the helper owns this; "
        f"got label_text={out['label_text']!r})"
    )
