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
// #7686: the composer primary button's rendered outer width by state. An
// idle `send` is a 34px circle; every busy action is a wider text pill, and
// the pill grows with the label text it carries (the `data-label` ::after).
const IDLE_BUTTON_WIDTH = 34;
const BUSY_BUTTON_BASE = 60;
// Slack the footer's content box keeps beyond the left group's clientWidth,
// mirroring a real flex row where `.composer-left` is the flexible member and
// the fixed-width primary button sits beside it. Set to the widest button
// state (a labelled busy pill) so the idle-width row never looks cramped.
const BTN_ROW_SLACK = BUSY_BUTTON_BASE + 16 * 7;

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
  // #7686: a stub of the composer primary button with enough surface for
  // the footer fit's worst-case row measurement. `getComputedStyle` is faked
  // globally below (see `installComputedStyle`), this stub only models the
  // element: its attribute store (so the probe's temporary data-action /
  // data-label stamping is observable and reversible) and its outer width,
  // which depends on the stamped action exactly like the real CSS: a `send`
  // circle is IDLE_BUTTON_WIDTH wide, any busy action is a wider pill.
  const btnAttrs = {};
  const btn = {
    style: {},
    getAttribute(name) {
      layout('btn.getAttribute(' + name + ')');
      return Object.prototype.hasOwnProperty.call(btnAttrs, name) ? btnAttrs[name] : null;
    },
    setAttribute(name, value) {
      btnAttrs[name] = String(value);
      layout('btn.setAttribute(' + name + ')');
    },
    removeAttribute(name) {
      delete btnAttrs[name];
      layout('btn.removeAttribute(' + name + ')');
    },
    get offsetWidth() { return btnWidth(); },
    getBoundingClientRect() { return { left: 0, top: 0, width: btnWidth(), height: 34 }; },
  };
  function btnWidth() {
    const action = Object.prototype.hasOwnProperty.call(btnAttrs, 'data-action')
      ? btnAttrs['data-action'] : 'send';
    const idle = action === 'send';
    // A busy pill is wider than the idle circle, and a stamped label widens
    // it further (matching `.send-btn[data-action=...]::after{content:attr(data-label)}`).
    if (idle) return IDLE_BUTTON_WIDTH;
    const label = Object.prototype.hasOwnProperty.call(btnAttrs, 'data-label')
      ? String(btnAttrs['data-label']).length : 0;
    return BUSY_BUTTON_BASE + label * 7;
  }
  const footer = {
    style, classList,
    querySelector(sel) {
      if (sel === '.composer-left') return left;
      if (sel === '#btnSend') return btn;
      return null;
    },
    get clientWidth() { return opts.availableWidth + (opts.btnSlack === undefined ? BTN_ROW_SLACK : opts.btnSlack); },
    getBoundingClientRect() { return { left: 0, top: 0, width: opts.availableWidth + (opts.btnSlack === undefined ? BTN_ROW_SLACK : opts.btnSlack), height: borderBoxHeight() }; },
  };
  const document = {
    querySelector(sel) { return sel === '.composer-footer' ? footer : null; },
  };
  return {
    document, samples, styleWrites, store, btnAttrs,
    snapshot() { layout('snapshot'); return samples[samples.length - 1]; },
    classes() { return Array.from(classes).sort().join(' '); },
    stage() { return stageOf(classes); },
    buttonAction() {
      return Object.prototype.hasOwnProperty.call(btnAttrs, 'data-action')
        ? btnAttrs['data-action'] : null;
    },
    buttonLabel() {
      return Object.prototype.hasOwnProperty.call(btnAttrs, 'data-label')
        ? btnAttrs['data-label'] : null;
    },
  };
}

function dedupe(arr) { return arr.filter((v, i) => i === 0 || v !== arr[i - 1]); }

function runFit(fit, opts) {
  const dom = makeFooterDom(opts);
  const before = dom.snapshot();
  global.document = dom.document;
  // #7686: the fit's row measurement reads `window.getComputedStyle` for the
  // button's / footer's box model. The real CSS used here contributes no
  // margin, padding, border or column gap, so a zero-valued style sheet is a
  // faithful stand-in — what matters is that the API exists, otherwise the
  // fit falls back to the legacy predicate and would stop seeing the button.
  const prevComputedStyle = global.window && global.window.getComputedStyle;
  global.window = global.window || {};
  global.window.getComputedStyle = function () {
    return {
      marginLeft: '0px', marginRight: '0px',
      borderLeftWidth: '0px', borderRightWidth: '0px',
      paddingLeft: '0px', paddingRight: '0px',
      columnGap: '0px', gap: '0px',
    };
  };
  let error = null;
  try { fit(); } catch (e) { error = String((e && e.message) || e); }
  global.window.getComputedStyle = prevComputedStyle;
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
    buttonAction: dom.buttonAction(),
    buttonLabel: dom.buttonLabel(),
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


# ── #7686: the footer fit must see the busy pill ─────────────────────────


def _run_fit_with(tmp_path, ui_js_text, available_width, btn_slack=None, strip_button=False):
    driver = tmp_path / "fit_driver_busy_pill.js"
    extra = ""
    if btn_slack is not None:
        extra += f", btnSlack: {btn_slack}"
    payload = (
        _DRIVER_SRC
        + "\n"
        + f"const out = runFit(eval('(' + extractFunc('_fitComposerFooter') + ')'), {{ start: 'full', availableWidth: {available_width}{extra}, prevHeight: '', prevVisibility: '' }});\n"
        "process.stdout.write('\\n@@BUSYPILL@@' + JSON.stringify(out) + '\\n');\n"
    )
    driver.write_text(payload, encoding="utf-8")
    probe = tmp_path / "probe_ui.js"
    text = ui_js_text
    if strip_button:
        text = text.replace(
            "const btn=footer.querySelector('#btnSend');", "const btn=null;", 1
        )
        assert "const btn=null;" in text, "could not strip the button measurement"
    probe.write_text(text, encoding="utf-8")
    r = subprocess.run(
        [NODE, str(driver), str(probe)], capture_output=True, text=True, timeout=60
    )
    if r.returncode != 0:
        raise AssertionError(r.stderr[-2000:])
    # _DRIVER_SRC is a complete script that already prints its own JSON; the
    # marker isolates the run we asked for.
    marker = "@@BUSYPILL@@"
    if marker not in r.stdout:
        raise AssertionError("driver produced no marker: " + r.stdout[-500:])
    return json.loads(r.stdout.split(marker, 1)[1].strip())


def _strip_button_measurement(ui_js_text):
    """Control variant: make the fit ignore the button (the pre-fix
    predicate that only ever compared .composer-left against itself)."""
    return ui_js_text.replace(
        "const btn=footer.querySelector('#btnSend');",
        "const btn=null;",
        1,
    )


# A row that fits only because the idle circle was assumed:
#   left demand       900   (STAGE_LEFT_WIDTH.full)
#   busy pill width   172   (BUSY_BUTTON_BASE 60 + 16-char label * 7)
#   footer content box 900 + 60 = 960
#   -> legacy (left-only) predicate sees 900 <= 960  and resolves FULL
#   -> new    (left+pill)  predicate sees 1072 > 960 and resolves BURGER
_BUSY_PILL_ROW_AVAILABLE = 900
_BUSY_PILL_ROW_SLACK = 60


def test_fit_uses_busy_pill_width_not_idle_circle(tmp_path):
    """The footer fit must see the busy-mode pill, not just the idle circle.

    On a row that fits only because the idle 34px circle was assumed, the
    fit resolves to the compact stage, and the busy state it stamps on the
    button for the measurement is reverted before the function returns (the
    temporary stamp never paints: the probe freezes visibility)."""
    run = _run_fit_with(
        tmp_path,
        UI_JS_PATH.read_text(encoding="utf-8"),
        _BUSY_PILL_ROW_AVAILABLE,
        btn_slack=_BUSY_PILL_ROW_SLACK,
    )
    assert run["error"] is None, run["error"]
    assert run["finalStage"] == "burger", run
    assert run["buttonAction"] is None, (
        "the probe's temporary busy stamp must be reverted: "
        f"data-action left as {run['buttonAction']!r}"
    )


def test_fit_outcome_is_invariant_when_only_the_idle_width_fits(tmp_path):
    """Control: with the button measurement removed (the pre-#7686
    predicate), the same tight footer resolves to the WIDE stage -- so the
    button-aware measurement is what changes the outcome, and the
    assertions above cannot pass vacuously."""
    ui = UI_JS_PATH.read_text(encoding="utf-8")
    control = _run_fit_with(
        tmp_path,
        _strip_button_measurement(ui),
        _BUSY_PILL_ROW_AVAILABLE,
        btn_slack=_BUSY_PILL_ROW_SLACK,
        strip_button=True,
    )
    assert control["error"] is None, control["error"]
    assert control["finalStage"] == "full", (
        "control run must resolve wide when the button is invisible to the "
        f"measurement, got {control['finalStage']!r}"
    )
