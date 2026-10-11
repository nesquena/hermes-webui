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
      return Math.max(opts.availableWidth, STAGE_LEFT_WIDTH[stageOf(classes)]);
    },
  };
  const footer = {
    style, classList,
    querySelector(sel) { return sel === '.composer-left' ? left : null; },
    getBoundingClientRect() { return { left: 0, top: 0, width: 0, height: borderBoxHeight() }; },
  };
  const document = {
    querySelector(sel) { return sel === '.composer-footer' ? footer : null; },
  };
  return {
    document, samples, styleWrites, store,
    snapshot() { layout('snapshot'); return samples[samples.length - 1]; },
    classes() { return Array.from(classes).sort().join(' '); },
    stage() { return stageOf(classes); },
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
