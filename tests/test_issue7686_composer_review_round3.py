"""#7686 review round 3 — the composer changes Codex and the senior review found.

Round 2 fixed the visible composer regressions (the label paints, the footer
does not flip stage at 1100px, geometry no longer transitions mid-measurement).
The 2026-10-08 review found four more, three of them must-fix:

1. **[must fix] Busy labels hide working composer controls.** The fitter hid
   ``.send-btn-label`` while measuring, sized the stage for the idle button, and
   then restored the wider pill in ``finally`` — which shrank ``.composer-left``
   (``overflow-x:auto``, scrollbar hidden) and clipped its chips with no cue.
   Measured 62 clipped rows on desktop, 36px off Interrupt at 1320px, 52px off
   Interrupt at 360px.

2. **[must fix] Phone Send/Stop tap target shrinks 44×44 → 34×34.** The top-level
   ``cf-burger`` rule (specificity 0,4,0) outranks the phone rule inside the
   640px block (0,1,0).

3. **[must fix] CI lint red** — unused ``shutil`` and unused local ``snippet``.

4. **[should fix] A locale change while busy contradicts the pill.** The generic
   restamp writes "Send message" into ``data-tooltip``/``aria-label`` while the
   pill reads "Stop", and no caller followed up with ``updateSendBtn``.

5. **[should fix] Non-English action labels** — the ``composer_action_*`` keys
   live only in the English block, so Chinese showed the English word "Stop".
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    subprocess.run(["which", "node"], capture_output=True).returncode != 0,
    reason="node not on PATH",
)

REPO_ROOT = Path(__file__).resolve().parents[1]
UI_JS = (REPO_ROOT / "static" / "ui.js").read_text(encoding="utf-8")
I18N_JS = (REPO_ROOT / "static" / "i18n.js").read_text(encoding="utf-8")
STYLE_CSS = (REPO_ROOT / "static" / "style.css").read_text(encoding="utf-8")


# ── finding 1: the widest busy footprint is reserved while measuring ─────────


def test_the_fitter_pins_the_label_visible_while_it_measures() -> None:
    """The fitter must pin the label HIDDEN while it measures.

    #7686 re-gate (must fix): the round-3 fix pinned the label VISIBLE so
    the widest busy footprint was reserved — but that let the pill decide
    the STAGE. At 1320px with the sidebar and workspace open, a pill that
    would only fit in ``cf-icons`` pushed the whole footer into it: 46px →
    56px, Saved prompts and the menu hidden, and it stayed that way after
    the turn ended (the maintainer's 10-12 repro).

    The stage is now resolved against the button's real ICON footprint,
    with the label hidden for the measurement only; the pill's overflow is
    absorbed afterwards by collapsing the label/button geometry through
    ``.cf-action-icon`` (see test_issue1804_footer_fit_dom.py for the
    behavioural pin). This test guards the source contract.
    """
    start = UI_JS.find("function _fitComposerFooter(")
    assert start >= 0, "_fitComposerFooter not found in static/ui.js"
    body = UI_JS[start : UI_JS.find("\nwindow._fitComposerFooter", start)]
    # The new pin must be present.
    assert "sendBtnLabel.style.display='none'" in body, (
        "the fitter must pin the label HIDDEN while measuring so the stage "
        "is resolved against the icon footprint and the busy pill cannot "
        "flip the footer stage (#7686 must-fix)"
    )
    # And it must restore in the finally block, or the pin leaks.
    assert "sendBtnLabel.style.display=prevLabelDisplay" in body, (
        "the label pin is never released — a leaked display value would "
        "delete the busy-mode pill label"
    )
    # The pill's overflow must be absorbed without re-deciding the stage.
    assert "cf-action-icon" in body, (
        "the fitter must collapse the restored pill through .cf-action-icon "
        "when it overflows .composer-left, instead of letting it change the "
        "stage (#7686 must-fix)"
    )


def test_the_fitter_restores_the_button_box_in_finally() -> None:
    """The temporary width/min-width overrides must be symmetric."""
    start = UI_JS.find("function _fitComposerFooter(")
    body = UI_JS[start : UI_JS.find("\nwindow._fitComposerFooter", start)]
    assert "_btnStyle.width='auto'" in body, (
        "the fitter must measure at the pill's own width, not the circle's"
    )
    assert "_btnStyle.minWidth=(window.innerWidth<=640?'44px':'34px')" in body, (
        "the fitter must reserve the button's real minimum while measuring "
        "(34px desktop / 44px phone); min-width:0 measures the bare 16px "
        "SVG in idle and clips the composer controls"
    )
    assert "_btnStyle.width=prevBtnWidth" in body
    assert "_btnStyle.minWidth=prevBtnMinWidth" in body


def test_the_measurement_override_survives_a_button_without_a_style_object() -> None:
    """Guard the defensive read the harness taught us.

    The freeze-test harness models ``#btnSend`` as an object with only
    ``querySelector`` — no ``.style``. A bare ``sendBtn.style.width`` throws
    inside the fit pass and the freeze is never released.
    """
    start = UI_JS.find("function _fitComposerFooter(")
    body = UI_JS[start : UI_JS.find("\nwindow._fitComposerFooter", start)]
    assert "_btnStyle=sendBtn&&sendBtn.style?sendBtn.style:null" in body, (
        "the button style must be read defensively; a #btnSend without a "
        ".style object used to crash the fit pass mid-freeze"
    )


def test_the_busy_label_is_hidden_at_phone_widths_regardless_of_stage() -> None:
    """Phone widths hide ``.send-btn-label`` in every stage, not just burger."""
    block = STYLE_CSS[STYLE_CSS.find("@media (max-width: 640px)") :]
    assert ".send-btn-label{display:none!important;}" in block, (
        "the phone block must hide the busy label at every stage; the pill "
        "steals room .composer-left cannot spare on a phone (#7686 finding 1)"
    )


# ── finding 2: the phone 44px touch-target contract ─────────────────────────


def test_the_phone_send_button_pins_a_44px_minimum() -> None:
    """The 640px block must pin min-width/min-height:44px on ``.send-btn``.

    Without the min-*, the top-level ``.composer-footer.cf-burger .send-btn``
    rule (specificity 0,4,0) wins over the phone rule (0,1,0) and the target
    shrinks to 34×34 — below the 44px contract the 340px rule's own comment
    states.
    """
    block = STYLE_CSS[STYLE_CSS.find("@media (max-width: 640px)") :]
    # The phone rule is the one whose declarations contain BOTH width:44px and
    # the minimum; the top-level .send-btn rule outside the block also matches
    # a bare `\.send-btn\{`, so anchor on the 44px width that is phone-only.
    rule = re.search(r"\.send-btn\{width:44px;height:44px[^}]*\}", block)
    assert rule, "no 44px .send-btn rule inside the 640px block"
    decls = rule.group(0)
    assert "min-width:44px" in decls, (
        f"the phone .send-btn rule must pin min-width:44px; got {decls!r}"
    )
    assert "min-height:44px" in decls, (
        f"the phone .send-btn rule must pin min-height:44px; got {decls!r}"
    )


def test_the_burger_stage_still_asks_for_34px_on_desktop() -> None:
    """The desktop circle is unchanged — the min-* only bites on phones."""
    assert ".composer-footer.cf-burger .send-btn{width:34px;height:34px" in STYLE_CSS, (
        "the desktop burger circle was altered; only the phone minimum should "
        "have changed"
    )


# ── finding 3: lint is clean ────────────────────────────────────────────────


@pytest.mark.parametrize(
    "relpath",
    [
        "tests/test_issue1804_footer_fit_dom.py",
        "tests/test_issue1804_send_button_label.py",
    ],
)
def test_no_unused_imports_or_locals_in_the_changed_tests(relpath: str) -> None:
    """CI lint was red on exactly these two files."""
    src = (REPO_ROOT / relpath).read_text(encoding="utf-8")
    assert "import shutil" not in src, f"{relpath}: unused shutil import is back"
    assert not re.search(r"^\s*snippet\s*=", src, re.M), (
        f"{relpath}: the unused local 'snippet' is back"
    )


# ── finding 4: the locale restamp must re-resolve the tooltip ───────────────


def test_the_locale_restamp_calls_update_send_btn() -> None:
    """The pill and its tooltip/aria-label must not disagree.

    The generic ``[data-i18n-title]`` restamp writes the *send* key into
    ``data-tooltip``/``aria-label``, while the label restamp only redraws the
    ``<span>``. ``updateSendBtn`` is the single owner of both attributes and
    resolves the title from the live action, so it has to run after the restamp.
    """
    start = I18N_JS.find("function applyLocaleToDOM(")
    assert start >= 0
    body = I18N_JS[start : start + 6000]
    assert "_setComposerPrimaryButtonIcon(" in body, (
        "the label restamp disappeared from applyLocaleToDOM"
    )
    assert "updateSendBtn()" in body, (
        "applyLocaleToDOM must call updateSendBtn() after the label restamp, "
        "otherwise the tooltip and the screen-reader name keep the send key "
        "while the pill reads Stop/Interrupt/Steer (#7686 finding 4)"
    )
    # It must come AFTER the label restamp, not before it.
    assert body.index("updateSendBtn()") > body.index("_setComposerPrimaryButtonIcon("), (
        "updateSendBtn() must run after the label restamp so it cannot be "
        "overwritten by the generic [data-i18n-title] pass"
    )


# ── finding 5: non-English action labels fall back to the English word ──────


def test_a_missing_translation_falls_back_to_the_english_word() -> None:
    """``t()`` echoes the key for a missing entry; that is not a translation.

    The ``composer_action_*`` keys live only in the English block, so before
    this fix Chinese users saw the English word "Stop" via the key path.
    """
    start = UI_JS.find("const _labelKeys={")
    assert start >= 0
    body = UI_JS[start : start + 900]
    assert "_labelFallback" in body, (
        "the action-label resolver must carry an explicit English fallback"
    )
    assert "_val!==_key" in body, (
        "a t() result that equals the key must be treated as missing"
    )


def test_the_translated_value_still_wins_when_present() -> None:
    """A real translation must not be replaced by the English fallback."""
    start = UI_JS.find("const _labelKeys={")
    body = UI_JS[start : start + 900]
    assert "?_val:" in body, (
        "when t() returns a real translation it must win over the fallback"
    )


# ---------------------------------------------------------------------------
# 2026-10-18 re-gate: the fitter's measurement box and the collapse ordering.
# ---------------------------------------------------------------------------


def _fitter_body() -> str:
    start = UI_JS.find("function _fitComposerFooter(")
    assert start >= 0, "_fitComposerFooter not found in static/ui.js"
    return UI_JS[start : UI_JS.find("\nwindow._fitComposerFooter", start)]


def test_the_icon_measurement_zeroes_padding_and_gap() -> None:
    """The busy button must measure as the 34px icon, not a 38px pill.

    Hiding the label alone leaves the busy button's horizontal padding and gap
    active (``padding: 0px 12px 0px 10px; gap: 6px``), so the native measurement
    reads 38px busy against 34px idle and a stage-boundary window survives the
    new minimum. Zeroing padding/gap alongside width/minWidth removes it.
    """
    body = _fitter_body()
    assert "_btnStyle.padding='0'" in body, (
        "the icon measurement must zero the button's horizontal padding"
    )
    assert "_btnStyle.gap='0'" in body, (
        "the icon measurement must zero the button's gap"
    )
    # ...and both must be restored in finally, or the pill loses its shape.
    assert "_btnStyle.padding=prevBtnPadding" in body, (
        "the zeroed padding must be restored in finally"
    )
    assert "_btnStyle.gap=prevBtnGap" in body, (
        "the zeroed gap must be restored in finally"
    )


def test_the_collapse_class_is_cleared_before_the_overflow_probe() -> None:
    """Consecutive fits must not oscillate between collapsed and overflowing.

    The overflow check reads .composer-left's scrollWidth. While the previous
    pass's .cf-action-icon is still on the button the probe sees the collapsed
    geometry, so a fit that fits removes the class, restores a pill that does
    not fit, and the next fit collapses it again -- the 34px/0px <-> 107.75px/58px
    alternation the maintainer reproduced over six consecutive fits. The class
    must therefore be removed BEFORE the probe and re-added only on a real
    overflow of the fully restored pill.
    """
    body = _fitter_body()
    # Only the `finally` tail matters: the same scrollWidth expression also
    # appears in the stage-selection probe near the top of the function, so the
    # ordering must be judged inside the restore block.
    tail_at = body.find("if(sendBtnLabel) sendBtnLabel.style.display=prevLabelDisplay;")
    assert tail_at >= 0, "the restore block was not found"
    tail = body[tail_at:]
    remove_at = tail.find("sendBtn.classList.remove('cf-action-icon')")
    probe_at = tail.find("left.scrollWidth>left.clientWidth+1")
    add_at = tail.find("sendBtn.classList.add('cf-action-icon')")
    assert remove_at >= 0, "the previous collapse class must be cleared"
    assert probe_at > remove_at, (
        "the overflow probe must run against the fully restored pill, i.e. "
        "after the previous .cf-action-icon has been removed"
    )
    assert add_at > probe_at, (
        "the collapse must be re-applied only after the probe measured the "
        "restored pill"
    )
    # The collapse stays conditional: a footer that fits keeps its label.
    assert "if(sendBtn&&sendBtn.classList&&left.scrollWidth>left.clientWidth+1){" in body, (
        "the collapse must remain conditional on a real overflow"
    )
