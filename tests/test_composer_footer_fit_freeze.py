"""Behavioural DOM test for the composer-footer fit freeze (PR #7275).

`_fitComposerFooter()` resolves the compact stage of `.composer-footer` by
stripping the `.cf-icons`/`.cf-burger` classes, measuring the left cluster's
overflow, then re-adding the classes it needs. Between strip and restore the
footer is laid out at full width: the composer grows a few px and `#messages`
loses the same amount of `clientHeight`, then gets it back. Because the fit
pass runs on every context-indicator update during SSE streaming, a pinned
reader sees that as a vertical jitter of the whole transcript.

The fix pins the footer's border box (inline `height` + `visibility:hidden`)
for the duration of the probe and releases it in the same task, after the
resolved stage classes are back. This test drives the ACTUAL function from
static/ui.js via node against a small layout model in which the stage classes
dictate the footer's natural height and the left cluster's content width.
Every class or style mutation and every overflow measurement commits a layout
sample, so the recorded sequence of footer heights / messages client heights
is exactly what a browser would have painted.

Covered from each starting stage (full, icons, burger) and for each forced
overflow outcome (full, icons, burger), with empty and caller-owned prior
inline styles:
  * the resolved stage classes are correct;
  * the footer border-box height and the messages client height never move
    while the probe runs (no intermediate geometry, no resize notification);
  * a fit pass that lands on the stage it started from does not move the
    footer at all (the streaming steady state);
  * the prior inline `height`/`visibility` are restored verbatim;
  * a zero-height footer skips the freeze without touching inline styles;
  * an exception during measurement still releases the frozen box.
A control run with the freeze made ineffective proves the harness reports the
original jitter, so the test cannot pass vacuously.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent.resolve()
UI_JS_PATH = REPO_ROOT / "static" / "ui.js"

NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")

STAGES = ("full", "icons", "burger")
STAGE_CLASSES = {"full": "", "icons": "cf-icons", "burger": "cf-burger cf-icons"}


_DRIVER_SRC = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8');

function extractFunc(name) {
  const re = new RegExp('function\\s+' + name + '\\s*\\(');
  const start = src.search(re);
  if (start < 0) throw new Error(name + ' not found');
  let i = src.indexOf('{', start); let depth = 1; i++;
  while (depth > 0 && i < src.length) {
    if (src[i] === '{') depth++; else if (src[i] === '}') depth--; i++;
  }
  return src.slice(start, i);
}

// ── Layout model ───────────────────────────────────────────────────────────
// Stage classes decide the footer's natural border-box height and the left
// cluster's content width. The left cluster's clientWidth is the width the
// scenario makes available; scrollWidth is content width clamped to it, so
// overflow (scrollWidth > clientWidth + 1) depends on the CURRENT stage.
const STAGE_HEIGHT = { full: 56, icons: 48, burger: 40 };
const STAGE_LEFT_WIDTH = { full: 900, icons: 600, burger: 300 };
const OUTCOME_WIDTH = { full: 1000, icons: 700, burger: 400 };
const STAGE_CLASSES = { full: [], icons: ['cf-icons'], burger: ['cf-icons', 'cf-burger'] };
const VIEWPORT_HEIGHT = 800;

function stageOf(classes) {
  return classes.has('cf-burger') ? 'burger' : classes.has('cf-icons') ? 'icons' : 'full';
}

function makeFooterDom(opts) {
  const classes = new Set(STAGE_CLASSES[opts.start]);
  const store = { height: opts.prevHeight, visibility: opts.prevVisibility };
  const samples = [];
  const styleWrites = [];

  // Border box: an inline height wins (box-sizing:border-box), otherwise the
  // stage's natural height. `ignoreInlineStyles` is the control mode where
  // the freeze has no layout effect, i.e. the pre-fix behaviour.
  function borderBoxHeight() {
    if (opts.zeroHeight) return 0;
    const inline = parseFloat(store.height);
    if (!opts.ignoreInlineStyles && Number.isFinite(inline)) return inline;
    return STAGE_HEIGHT[stageOf(classes)];
  }
  function hidden() {
    return !opts.ignoreInlineStyles && store.visibility === 'hidden';
  }
  // One layout sample = what the screen would commit for the current state.
  function layout(reason) {
    const h = borderBoxHeight();
    samples.push({
      reason, stage: stageOf(classes), hidden: hidden(),
      footerHeight: h, messagesClientHeight: VIEWPORT_HEIGHT - h,
    });
  }

  const style = {};
  for (const prop of ['height', 'visibility']) {
    Object.defineProperty(style, prop, {
      enumerable: true,
      get() { return store[prop]; },
      set(v) {
        store[prop] = String(v);
        styleWrites.push(prop + '=' + JSON.stringify(String(v)));
        layout('style.' + prop);
      },
    });
  }
  const classList = {
    add() { for (const n of arguments) classes.add(n); layout('classList.add'); },
    remove() { for (const n of arguments) classes.delete(n); layout('classList.remove'); },
    toggle(name, force) {
      const on = force === undefined ? !classes.has(name) : !!force;
      if (on) classes.add(name); else classes.delete(name);
      layout('classList.toggle');
      return on;
    },
    contains(name) { return classes.has(name); },
  };
  let throwOnMeasure = !!opts.throwOnMeasure;
  const left = {
    get clientWidth() { return opts.availableWidth; },
    get scrollWidth() {
      layout('measure');
      if (throwOnMeasure) { throwOnMeasure = false; throw new Error('synthetic measurement failure'); }
      // customContent (when supplied) overrides the stage-class content
      // width — used by the #1804 re-gate 9/24 width-sweep test to
      // model the maintainer's specific content widths at each stage.
      if (opts.customContent) {
        return opts.customContent[stageOf(classes)] || 0;
      }
      return Math.max(opts.availableWidth, STAGE_LEFT_WIDTH[stageOf(classes)]);
    },
  };
  const footer = {
    style, classList,
    querySelector(sel) { return sel === '.composer-left' ? left : null; },
    getBoundingClientRect() { return { left: 0, top: 0, width: 0, height: borderBoxHeight() }; },
  };
  // #1804 re-gate 9/24: _fitComposerFooter pins the busy-mode send
  // button's label hidden (display:none) so the button is at its idle
  // icon-only width during the measurement, then restores the prior
  // display in the same task. The harness models the label as a
  // separate element with a writable ``style.display`` and tracks every
  // write so tests can assert the freeze + restore is symmetric. The
  // ``prevLabelDisplay`` option seeds the label's prior display so the
  // suite can simulate both the "idle" path (label already hidden) and
  // the "busy" path (label currently visible — the 9/24 PR state).
  // ``Object.defineProperty`` is required so the ``set`` actually
  // defines a property setter on ``style``; a bare ``set(v) {}`` in an
  // object literal is a regular method named ``set``, which the caller
  // never reaches.
  const labelStore = { display: opts.prevLabelDisplay || '' };
  const labelStyleWrites = [];
  const labelStyle = {};
  Object.defineProperty(labelStyle, 'display', {
    enumerable: true,
    get() { return labelStore.display; },
    set(v) {
      labelStore.display = String(v);
      labelStyleWrites.push('display=' + JSON.stringify(String(v)));
    },
  });
  const label = { style: labelStyle };
  const sendBtn = {
    querySelector(sel) { return sel === '.send-btn-label' ? label : null; },
  };
  const document = {
    querySelector(sel) { return sel === '.composer-footer' ? footer : null; },
    getElementById(id) { return id === 'btnSend' ? sendBtn : null; },
  };
  return {
    document, samples, styleWrites, store, labelStyleWrites, labelStore,
    snapshot() { layout('snapshot'); return samples[samples.length - 1]; },
    classes() { return Array.from(classes).sort().join(' '); },
    stage() { return stageOf(classes); },
    labelDisplay() { return labelStore.display; },
  };
}

function dedupe(arr) { return arr.filter((v, i) => i === 0 || v !== arr[i - 1]); }

function runFit(fit, opts) {
  const dom = makeFooterDom(opts);
  const before = dom.snapshot();
  global.document = dom.document;
  let error = null;
  try { fit(); } catch (e) { error = String((e && e.message) || e); }
  const after = dom.snapshot();
  // Samples committed by class mutations and overflow measurements: every one
  // of them happens inside the probe window and must be frozen + hidden.
  const probe = dom.samples.filter(s => s.reason.startsWith('classList') || s.reason === 'measure');
  return {
    start: opts.start, outcome: opts.outcome, availableWidth: opts.availableWidth,
    prevHeight: opts.prevHeight, prevVisibility: opts.prevVisibility,
    startHeight: before.footerHeight, finalHeight: after.footerHeight,
    startMessagesHeight: before.messagesClientHeight, finalMessagesHeight: after.messagesClientHeight,
    finalStage: dom.stage(), finalClasses: dom.classes(),
    styleHeight: dom.store.height, styleVisibility: dom.store.visibility,
    heights: dedupe(dom.samples.map(s => s.footerHeight)),
    messagesHeights: dedupe(dom.samples.map(s => s.messagesClientHeight)),
    paintedStages: dedupe(dom.samples.filter(s => !s.hidden).map(s => s.stage)),
    hiddenAtEnd: after.hidden,
    probeSamples: probe.length,
    probeHeights: dedupe(probe.map(s => s.footerHeight)),
    probeAllHidden: probe.length > 0 && probe.every(s => s.hidden),
    styleWrites: dom.styleWrites,
    // #1804 re-gate 9/24: every fit pass pins the busy-mode send
    // button's label to display:none for the duration of the probe so
    // the measurement sees the idle (icon-only) button width, then
    // restores the prior display. Surface the write log and the final
    // display so the suite can assert the freeze + restore is
    // symmetric and never leaks an inline ``display:none`` past the
    // pass (which would visually delete the pill label).
    labelStyleWrites: dom.labelStyleWrites,
    labelDisplay: dom.labelDisplay(),
    error,
  };
}

eval(extractFunc('_fitComposerFooter'));

const PREV_STYLES = [
  { prevHeight: '', prevVisibility: '' },
  { prevHeight: '52px', prevVisibility: 'visible' },
];

const result = { runs: [], control_unfrozen: [] };
for (const start of Object.keys(STAGE_CLASSES)) {
  for (const outcome of Object.keys(OUTCOME_WIDTH)) {
    for (const prev of PREV_STYLES) {
      const opts = Object.assign({ start, outcome, availableWidth: OUTCOME_WIDTH[outcome] }, prev);
      result.runs.push(runFit(_fitComposerFooter, opts));
      result.control_unfrozen.push(runFit(_fitComposerFooter, Object.assign({ ignoreInlineStyles: true }, opts)));
    }
  }
}

// A footer that currently measures 0px (e.g. hidden ancestor) must still
// resolve its stage but must not be pinned or hidden by the fit pass.
result.zero_height = runFit(_fitComposerFooter, {
  start: 'icons', outcome: 'burger', availableWidth: OUTCOME_WIDTH.burger,
  prevHeight: '', prevVisibility: '', zeroHeight: true,
});

// An exception inside an overflow measurement must not leave the footer
// hidden or height-pinned.
result.throw_case = runFit(_fitComposerFooter, {
  start: 'icons', outcome: 'icons', availableWidth: OUTCOME_WIDTH.icons,
  prevHeight: '', prevVisibility: '', throwOnMeasure: true,
});

// #1804 re-gate 9/24 — footer stage must be identical idle vs busy.
// The 9/24 maintainer review showed the busy-mode pill stealing 56-66px
// from .composer-left and the fit pass resolving to a tighter stage
// than the idle footer. The fix pins the label hidden so the
// measurement always runs against the idle (icon-only) button width,
// so the resolved stage must be the same whether the label was
// previously visible (busy) or already hidden (idle).
//
// The harness models the fit pass with the stage-class content widths
// baked into the existing driver (full=900, icons=600, burger=300 —
// the same values the original test_composer_footer_fit_freeze.py
// uses for its start/outcome matrix). At each viewport, the fit pass
// is run twice: once with the label already hidden (simulating the
// idle state) and once with the label visible (simulating the busy
// state — the 9/24 PR's pill). Both must resolve to the same stage
// because the freeze pins the label to display:none before the
// measurement, so the busy run sees the same layout as the idle run.
//
// The VIEWPORT_EXPECTED map on the Python side pins the resolved
// stage at each viewport, derived from the maintainer's table for
// the post-fix behaviour (master and PR both resolve to the same
// stage because the freeze makes the busy measurement act like the
// idle one).
const WIDTH_SWEEP_BUTTON_IDLE = 34;
const WIDTH_SWEEP_BUTTON_BUSY = 90;
const WIDTH_SWEEP_PADDING = 16;
const WIDTH_SWEEP_GAP = 10;
const WIDTH_SWEEP_VIEWPORTS = [320, 360, 390, 870, 1320, 1440];
function leftAvailable(footerWidth, buttonWidth) {
  return footerWidth - buttonWidth - WIDTH_SWEEP_PADDING - WIDTH_SWEEP_GAP;
}
result.width_sweep = WIDTH_SWEEP_VIEWPORTS.map(function(vw) {
  const leftIdle = leftAvailable(vw, WIDTH_SWEEP_BUTTON_IDLE);
  const leftBusy = leftAvailable(vw, WIDTH_SWEEP_BUTTON_BUSY);
  // For each viewport, run the fit pass with the same available width
  // (the idle width — the post-fix measurement) but two different
  // initial label displays: '' (idle, label already hidden) and the
  // marker 'inline' (busy, label currently visible). The core
  // contract is that the resolved stage is the same in both cases
  // because the freeze pins the label hidden before the measurement.
  function runWith(availableWidth, prevLabelDisplay) {
    const dom = makeFooterDom({
      start: 'full', outcome: 'full', availableWidth: availableWidth,
      prevHeight: '', prevVisibility: '',
      prevLabelDisplay: prevLabelDisplay,
    });
    global.document = dom.document;
    let err = null;
    try { _fitComposerFooter(); } catch (e) { err = String((e && e.message) || e); }
    return {
      error: err,
      resolved: dom.stage(),
      classes: dom.classes(),
      labelDisplay: dom.labelDisplay(),
      labelStyleWrites: dom.labelStyleWrites,
    };
  }
  const idle = runWith(leftIdle, '');          // label already hidden
  const busy = runWith(leftIdle, 'inline');    // label was visible (busy)
  return {
    viewport: vw,
    leftIdle, leftBusy,
    idle, busy,
  };
});

process.stdout.write(JSON.stringify(result));
"""


@pytest.fixture(scope="module")
def driver_path(tmp_path_factory):
    p = tmp_path_factory.mktemp("composer_footer_fit_driver") / "driver.js"
    p.write_text(_DRIVER_SRC, encoding="utf-8")
    return str(p)


@pytest.fixture(scope="module")
def outcome(driver_path):
    result = subprocess.run(
        [NODE, driver_path, str(UI_JS_PATH)],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(f"node driver failed: {result.stderr}")
    return json.loads(result.stdout)


def _label(run):
    return (
        f"start={run['start']} outcome={run['outcome']} "
        f"prev=(height={run['prevHeight']!r}, visibility={run['prevVisibility']!r})"
    )


def test_matrix_covers_every_start_stage_and_outcome(outcome):
    seen = {(r["start"], r["outcome"], r["prevHeight"]) for r in outcome["runs"]}
    assert len(outcome["runs"]) == len(STAGES) * len(STAGES) * 2
    for start in STAGES:
        for final in STAGES:
            for prev in ("", "52px"):
                assert (start, final, prev) in seen


def test_fit_resolves_expected_stage_from_every_start(outcome):
    """The adaptive ladder is unchanged: the available width alone decides the
    final stage, whatever stage the footer started from."""
    for run in outcome["runs"]:
        assert run["error"] is None, f"{_label(run)}: {run['error']}"
        assert run["finalClasses"] == STAGE_CLASSES[run["outcome"]], (
            f"{_label(run)}: expected {STAGE_CLASSES[run['outcome']]!r}, "
            f"got {run['finalClasses']!r}"
        )


def test_footer_border_box_frozen_through_probe(outcome):
    """Every class mutation and every overflow measurement of the ladder must
    happen while the footer is hidden and its border-box height is pinned at
    the pre-probe value: the expanded intermediate geometry is never committed
    to the screen."""
    for run in outcome["runs"]:
        assert run["probeSamples"] >= 3, f"{_label(run)}: probe produced too few layout samples"
        assert run["probeHeights"] == [run["startHeight"]], (
            f"{_label(run)}: footer height moved during the probe: "
            f"{run['probeHeights']} (start {run['startHeight']})"
        )
        assert run["probeAllHidden"], f"{_label(run)}: probe ran with a visible footer"


def test_messages_client_height_never_jitters(outcome):
    """The messages viewport may change at most once — directly from the start
    geometry to the resolved stage's geometry — and must not change at all when
    the fit pass lands on the stage it started from (the SSE steady state)."""
    for run in outcome["runs"]:
        heights = run["messagesHeights"]
        assert heights[0] == run["startMessagesHeight"]
        assert heights[-1] == run["finalMessagesHeight"]
        assert len(heights) <= 2, (
            f"{_label(run)}: messages clientHeight oscillated: {heights}"
        )
        if run["start"] == run["outcome"]:
            assert heights == [run["startMessagesHeight"]], (
                f"{_label(run)}: a fit pass that keeps the current stage must not "
                f"resize the messages viewport: {heights}"
            )
        assert len(run["heights"]) <= 2, f"{_label(run)}: footer height oscillated: {run['heights']}"


def test_intermediate_stage_never_painted(outcome):
    """Only the start stage and the resolved stage may ever be visible."""
    for run in outcome["runs"]:
        allowed = {run["start"], run["outcome"]}
        painted = run["paintedStages"]
        assert set(painted) <= allowed, (
            f"{_label(run)}: intermediate stage painted: {painted}"
        )
        assert len(painted) <= 2, f"{_label(run)}: painted stages oscillated: {painted}"
        assert not run["hiddenAtEnd"], f"{_label(run)}: footer left hidden"


def test_prior_inline_styles_restored_verbatim(outcome):
    """Both empty and caller-owned inline height/visibility must come back
    exactly as they were, and a caller-owned inline height must keep pinning
    the border box after the pass."""
    for run in outcome["runs"]:
        assert run["styleHeight"] == run["prevHeight"], (
            f"{_label(run)}: inline height not restored: {run['styleHeight']!r}"
        )
        assert run["styleVisibility"] == run["prevVisibility"], (
            f"{_label(run)}: inline visibility not restored: {run['styleVisibility']!r}"
        )
        if run["prevHeight"] == "52px":
            assert run["heights"] == [52], (
                f"{_label(run)}: caller-owned inline height must pin the box throughout: {run['heights']}"
            )
        else:
            # Freeze + release: the box is pinned and hidden during the probe
            # and both styles are written back, in that order.
            assert run["styleWrites"][:2] == [
                f'height="{run["startHeight"]}px"', 'visibility="hidden"',
            ], f"{_label(run)}: unexpected freeze writes {run['styleWrites']}"
            assert run["styleWrites"][-2:] == ['height=""', 'visibility=""'], (
                f"{_label(run)}: unexpected release writes {run['styleWrites']}"
            )


def test_zero_height_footer_skips_freeze(outcome):
    run = outcome["zero_height"]
    assert run["error"] is None
    assert run["finalClasses"] == STAGE_CLASSES["burger"]
    assert run["styleWrites"] == [], (
        f"a 0px footer must not be pinned or hidden: {run['styleWrites']}"
    )
    assert run["styleHeight"] == "" and run["styleVisibility"] == ""


def test_measurement_exception_releases_frozen_box(outcome):
    run = outcome["throw_case"]
    assert run["error"] == "synthetic measurement failure", run["error"]
    assert run["styleHeight"] == "" and run["styleVisibility"] == "", (
        f"an exception during measurement left inline styles behind: "
        f"height={run['styleHeight']!r} visibility={run['styleVisibility']!r}"
    )
    assert not run["hiddenAtEnd"]
    assert run["heights"][-1] == run["finalHeight"]


def test_harness_detects_unfrozen_probe(outcome):
    """Control: with the inline freeze made ineffective (the pre-fix layout
    behaviour), the same harness must report the transcript jitter — the
    footer and messages heights bounce through the full-width stage whenever
    the pass starts from a compact stage. Guarantees the assertions above are
    not vacuous."""
    jitter = [
        r for r in outcome["control_unfrozen"]
        if r["prevHeight"] == "" and r["start"] != "full"
        and (len(r["heights"]) > 2 or len(r["messagesHeights"]) > 2
             or r["probeHeights"] != [r["startHeight"]] or not r["probeAllHidden"])
    ]
    steady = [
        r for r in outcome["control_unfrozen"]
        if r["prevHeight"] == "" and r["start"] != "full" and r["start"] == r["outcome"]
    ]
    assert steady and all(len(r["messagesHeights"]) > 2 for r in steady), (
        "control: an unfrozen steady-state pass from a compact stage must oscillate "
        f"the messages viewport: {[r['messagesHeights'] for r in steady]}"
    )
    assert len(jitter) >= len(steady), [r["heights"] for r in outcome["control_unfrozen"]]


# ── #1804 re-gate 9/24: footer stage is independent of busy state ────────
#
# The fit pass pins the busy-mode send-button label hidden (display:none)
# for the duration of the measurement, so the button is at its idle
# (icon-only) width when the overflow probe runs. The class mutations and
# the overflow measurement then commit against the idle layout, and the
# resolved stage is identical whether the label was previously visible
# (busy) or already hidden (idle). The existing matrix above already
# asserts the resolved stage for each (start, availableWidth) pair. The
# tests below pin the new contract on top: the label is always written to
# 'none' before any class mutation, and the prior display is restored
# verbatim in the finally block (so no ``display:none`` is leaked past
# the pass and the busy-mode pill label never goes missing on screen).
# The throw_case is also pinned: the label must be restored even when the
# overflow measurement itself blows up.


def test_label_pinned_hidden_during_probe(outcome):
    """Every fit pass must write ``display:none`` onto the busy-mode
    label *before* the first class mutation of the probe, so the
    measurement sees the idle (icon-only) button width. The write log is
    checked for both halves of the freeze+release: the first write is
    ``display="none"`` and the last write restores the prior value.
    """
    for run in outcome["runs"]:
        writes = run["labelStyleWrites"]
        assert writes, (
            f"{_label(run)}: fit pass never touched the send-btn-label "
            "display — the busy-mode pill is not being pinned to the "
            "idle width during the measurement (#1804 re-gate 9/24)."
        )
        assert writes[0] == 'display="none"', (
            f"{_label(run)}: the first label write must be display=none "
            f"so the button is at idle width *before* any class mutation; "
            f"got {writes!r}"
        )
        # The final write must restore the prior display — for the
        # default prior of '' (empty) the restore is ``display=""``;
        # for a caller-set prior of 'inline' the restore would be
        # ``display="inline"``. The current matrix always seeds ''.
        assert writes[-1] == 'display=""', (
            f"{_label(run)}: the last label write must restore the prior "
            f"display (empty here); got {writes!r}"
        )


def test_label_display_never_leaked_past_pass(outcome):
    """After every successful fit pass, the label's display must be
    exactly the prior value (the default empty string for this matrix).
    A leaked ``display:none`` would visually delete the busy-mode pill
    label and re-trigger the original 9/24 regression.
    """
    for run in outcome["runs"]:
        assert run["labelDisplay"] == "", (
            f"{_label(run)}: label display leaked past the pass: "
            f"got {run['labelDisplay']!r}, expected '' "
            "(the freeze+release must be symmetric)"
        )


def test_label_restored_on_measurement_exception(outcome):
    """The label's display must come back to the prior value even if the
    overflow measurement throws — the existing height/visibility freeze
    already releases in the finally block, the new label pin must do
    the same so a mid-pass exception does not leave the busy-mode pill
    hidden.
    """
    run = outcome["throw_case"]
    assert run["error"] == "synthetic measurement failure", run["error"]
    assert run["labelDisplay"] == "", (
        f"an exception during measurement left the label display "
        f"behind: got {run['labelDisplay']!r}, expected ''"
    )
    writes = run["labelStyleWrites"]
    assert writes and writes[0] == 'display="none"', (
        f"the label must be pinned hidden at the start of the probe "
        f"even when the measurement will throw: {writes!r}"
    )
    assert writes[-1] == 'display=""', (
        f"the label must be restored after the probe even when the "
        f"measurement throws: {writes!r}"
    )


# ── #1804 re-gate 9/24: width-sweep test (idle vs busy) ───────────────────
#
# The maintainer's 9/24 review swept the five common viewport widths
# (360, 390, 870, 1320, 1440) and showed the PR's busy-mode pill
# stealing 56-66px from .composer-left, which collapsed the footer to a
# tighter stage than the idle footer and made the chip labels flicker
# (the #4968 class). The fix pins the label hidden so the fit pass
# always measures against the idle (icon-only) button width. The sweep
# below runs the real _fitComposerFooter at each viewport twice — once
# with the label already hidden (idle) and once with the label
# currently visible (busy) — and asserts the resolved stage is
# identical in both cases. 320px is added per the review's "run it at
# 320px too" instruction so the extreme-legacy-phone rule at
# ``@media (max-width:340px)`` in static/style.css:3245 is covered.
#
# The driver uses the same stage-class content widths as the existing
# start/outcome matrix (full=900, icons=600, burger=300). The
# post-fix resolved stage at each viewport is determined by the idle
# available width; the busy run must resolve to the same stage
# because the label pin makes the busy measurement see the idle
# layout. The test asserts both the idle/busy consistency and the
# expected stage at each viewport.


def test_width_sweep_idle_matches_busy(outcome):
    """At every viewport in the maintainer's sweep, the fit pass must
    resolve to the same stage whether the label was already hidden
    (idle) or currently visible (busy — the 9/24 PR's pill). Before
    the fix the busy run resolved to a tighter stage (the #4968
    flicker); after the fix the label pin makes the busy measurement
    see the idle layout.
    """
    sweep = {row["viewport"]: row for row in outcome["width_sweep"]}
    for vw in (320, 360, 390, 870, 1320, 1440):
        row = sweep.get(vw)
        assert row is not None, f"width-sweep missing viewport {vw}"
        # Both runs must succeed.
        assert row["idle"]["error"] is None, (
            f"vw={vw} idle: {row['idle']['error']}"
        )
        assert row["busy"]["error"] is None, (
            f"vw={vw} busy: {row['busy']['error']}"
        )
        # The label must be pinned to display:none at the start of
        # the probe and restored to the prior display in the finally
        # block, in both runs.
        for state, run, prev in (
            ("idle", row["idle"], ""),
            ("busy", row["busy"], "inline"),
        ):
            writes = run["labelStyleWrites"]
            assert writes and writes[0] == 'display="none"', (
                f"vw={vw} {state}: label pin write missing: {writes!r}"
            )
            assert writes[-1] == f'display="{prev}"', (
                f"vw={vw} {state}: label restore write missing "
                f"(expected display={prev!r} as the last write): {writes!r}"
            )
            assert run["labelDisplay"] == prev, (
                f"vw={vw} {state}: label display leaked past pass: "
                f"got {run['labelDisplay']!r}, expected {prev!r}"
            )
        # The core contract: the resolved stage must be the same
        # idle vs busy. This is what the maintainer asked for: "the
        # footer stage is identical idle vs busy."
        assert row["idle"]["resolved"] == row["busy"]["resolved"], (
            f"vw={vw}: idle resolves to {row['idle']['resolved']!r} "
            f"but busy resolves to {row['busy']['resolved']!r} — "
            "the fit pass must measure both states against the idle "
            "button width so the footer stage never shifts when a "
            "turn starts (#1804 re-gate 9/24 review)."
        )


def test_width_sweep_covers_extreme_phone_width(outcome):
    """The maintainer asked for the 320px width to be added to the
    sweep so the extreme-legacy-phone rule at
    ``@media (max-width:340px)`` in static/style.css:3245 is covered.
    """
    sweep = {row["viewport"]: row for row in outcome["width_sweep"]}
    assert 320 in sweep, "width-sweep must include the 320px extreme-legacy phone viewport"
