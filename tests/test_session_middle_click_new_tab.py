"""Middle-click (auxclick button 1) or Ctrl/Cmd+click on a sidebar session row
opens that session in a new browser tab instead of switching the current tab.

Covers the P1 from Greptile review on #7429 (modified clicks retaining
gesture state) via a regression lock, plus behavioral coverage the review
asked for: the new-tab helpers are extracted from the shipped
``static/sessions.js`` and driven through a Node VM that dispatches synthetic
pointer/aux/mouse events against a stub row and observes ``window.open``,
propagation, exclusions, and gesture cleanup.

Handler/gesture background: top-level ``.session-item`` rows swallow
non-left buttons (``onpointerup`` returns early for ``button !== 0`` and no
``auxclick`` handler existed), so middle-click did nothing or triggered
autoscroll. The fix wires ``_openSessionUrlInNewTab(sid)`` through the shared
``_consumeSessionNewTabClick`` choke point into all sidebar row kinds
(top-level rows, fork rows + main buttons, plain child buttons, lineage
segments) via ``auxclick`` (open) + ``mousedown`` (kill autoscroll) plus
Ctrl/Cmd+click on the tap paths, leaving right-click menu, select mode,
rename, swipe, and single-tap behavior untouched.
"""
import json
import shutil
import subprocess
from pathlib import Path

import tempfile

import pytest


REPO = Path(__file__).parent.parent
SESSIONS_JS = (REPO / "static" / "sessions.js").read_text(encoding="utf-8")
BOOT_JS = (REPO / "static" / "boot.js").read_text(encoding="utf-8")
NODE = shutil.which("node")


def _extract_function(source: str, name: str) -> str:
    """Extract ``function name(...)`` with balanced braces (sync or plain)."""
    for prefix in (f"function {name}(", f"async function {name}("):
        start = source.find(prefix)
        if start >= 0:
            break
    else:
        raise AssertionError(f"{name} function not found in static/sessions.js")
    brace = source.find("{", start)
    assert brace >= 0, f"{name} opening brace not found"
    depth = 0
    for idx in range(brace, len(source)):
        ch = source[idx]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return source[start:idx + 1]
    raise AssertionError(f"{name} function braces unbalanced")


def test_new_tab_helper_exists():
    """A shared `_openSessionUrlInNewTab(sid)` helper builds the deep link."""
    helper = _extract_function(SESSIONS_JS, "_openSessionUrlInNewTab")
    assert "_sessionUrlForSid(" in helper
    assert "window.open(" in helper
    assert "'_blank'" in helper or '"_blank"' in helper


def test_auxclick_wired_for_all_row_kinds():
    """Each row kind handles middle-click via the shared wiring helper."""
    # The auxclick/mousedown listeners live once in _wireSessionNewTabListeners;
    # every row kind (top-level .session-item, fork row, fork main button,
    # plain child button, lineage segment) must call the wirer.
    assert "_wireSessionNewTabListeners(el, ()=>s.session_id, ()=>s)" in SESSIONS_JS
    assert SESSIONS_JS.count("_wireSessionNewTabListeners(row, ()=>child.session_id, ()=>child, {exact:true})") == 2
    assert "_wireSessionNewTabListeners(mainBtn, ()=>child.session_id, ()=>child, {exact:true})" in SESSIONS_JS
    assert "_wireSessionNewTabListeners(row, ()=>seg.session_id, ()=>seg)" in SESSIONS_JS
    assert SESSIONS_JS.count("_wireSessionNewTabListeners(") >= 6  # def + 5 call sites
    # All opens route through the two choke points with the concrete sid and
    # the concrete session (so the owning-profile gate can inspect the row).
    assert "_openSessionUrlInNewTab(getSid(), typeof getSession==='function'?getSession():undefined, opts)" in SESSIONS_JS
    assert "_openSessionUrlInNewTab(sid, session, opts)" in SESSIONS_JS
    assert "_openSessionUrlInNewTab(childSession.session_id, childSession, {exact:true})" in SESSIONS_JS


def test_middle_mousedown_prevents_autoscroll():
    """`mousedown` on button 1 preventDefaults so the browser doesn't autoscroll."""
    assert "addEventListener('auxclick'" in SESSIONS_JS
    assert "addEventListener('mousedown'" in SESSIONS_JS
    wire = _extract_function(SESSIONS_JS, "_wireSessionNewTabListeners")
    assert "button" in wire and "1" in wire
    assert "preventDefault()" in wire
    # Ctrl/Cmd+click on the tap paths also routes to the new-tab opener.
    assert "_consumeSessionNewTabClick(e, child.session_id, child, {exact:true})" in SESSIONS_JS
    assert "_consumeSessionNewTabClick(e, s.session_id, s)" in SESSIONS_JS


def test_ctrl_click_opens_new_tab():
    """Ctrl/Cmd+left-click on a row opens the deep link in a new tab."""
    assert "e.ctrlKey||e.metaKey" in SESSIONS_JS.replace(" ", "")


def test_action_menu_and_select_mode_untouched():
    """New-tab must not fire from the ⋮ menu, checkboxes, or select mode.

    The action-menu guard is the shared ``.session-actions`` class in the
    helpers' exclusion list: the per-row ``_isSessionActionTarget`` predicate
    lives inside the row render closure and is invisible to the top-level
    helpers, so it cannot be consulted here (checked in the behavioral test
    below against a real ``.session-actions`` target).
    """
    consume = _extract_function(SESSIONS_JS, "_consumeSessionNewTabClick")
    assert ".session-actions" in consume
    assert "_sessionSelectMode" in consume
    assert "_renamingSid" in consume
    wire = _extract_function(SESSIONS_JS, "_wireSessionNewTabListeners")
    assert ".session-actions" in wire
    assert "session-actions" in wire


def test_openChildSession_new_tab_flag():
    """Child-row programmatic path supports open-in-new-tab without a same-tab switch."""
    idx = SESSIONS_JS.index("const openChildSession=async(childSession,")
    window = SESSIONS_JS[idx:idx + 500]
    assert "newTab" in window
    assert "_openSessionUrlInNewTab(childSession.session_id, childSession, {exact:true})" in window


def test_modified_click_cancels_pending_tap_before_new_tab():
    """P1 (#7429 review): the Ctrl/Cmd+click branch must clear the pending
    single-tap timer *before* opening the new tab.

    Without this, the deferred single-tap opener from the FIRST click of a
    fast modified double-click fires after the new tab opens and switches the
    current tab anyway — the exact stale-state class the review flagged.
    """
    idx = SESSIONS_JS.index("if((e.ctrlKey||e.metaKey)")
    window = SESSIONS_JS[idx:idx + 2400]
    assert "_consumeSessionNewTabClick(e, s.session_id, s)" in window
    clear_idx = window.index("clearTimeout(_tapTimer)")
    consume_idx = window.index("_consumeSessionNewTabClick(e, s.session_id, s)")
    assert clear_idx < consume_idx, (
        "pending-tap cancel must run before the new-tab open, not after"
    )
    assert "_lastTapTime=0" in window.replace(" ", "")
    assert "_tapTimer=null" in window.replace(" ", "")
    # The row's gesture machine was armed by pointerdown before this
    # pointerup fired, and a pen drag may have painted swipe offsets
    # (mouse never paints: `_isSessionSwipeTarget` excludes mouse). The
    # branch must settle via the shared `_clearPointerDragState()` choke
    # point BEFORE the early return — parking `_gestureState` alone would
    # leave pen-painted offsets displaced and the `dragging` class stuck.
    assert "_clearPointerDragState()" in window
    assert window.index("_clearPointerDragState()") < consume_idx
    # Select/rename gate: the branch must not touch gesture state when the
    # new-tab consumer refuses the event (select mode / mid-rename), or the
    # fall-through _finishSessionGesture early-returns on 'idle' and the row
    # (de)select toggle never runs.
    compact = window.replace(" ", "")
    assert "!_sessionSelectMode" in compact
    assert "!_renamingSid" in compact


# ── Behavioral tests via Node VM ─────────────────────────────────────────────

pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")

_VM_PRELUDE = r"""
const ret = {};
const sandbox = { opened: null, openCalls: 0, stopped: 0, prevented: 0,
  _sessionSelectMode: false, _renamingSid: null,
  mkEvent: null };
sandbox.window = { open: (u, t, f) => { sandbox.opened = { u, t, f }; sandbox.openCalls++; return null; } };
sandbox.document = { baseURI: 'http://127.0.0.1:8787/' };
sandbox.location = { href: 'http://127.0.0.1:8787/', pathname: '/', search: '', hash: '', origin: 'http://127.0.0.1:8787' };
sandbox.window.location = sandbox.location;
sandbox.URL = URL; sandbox.URLSearchParams = URLSearchParams; sandbox.encodeURIComponent = encodeURIComponent;
sandbox.mkEvent = (over) => Object.assign(
  { button: 0, ctrlKey: false, metaKey: false, target: null,
    preventDefault() { sandbox.prevented++; }, stopPropagation() { sandbox.stopped++; } }, over || {});
const vm = require('vm');
vm.createContext(sandbox);
vm.runInContext(params.helpers, sandbox);
vm.runInContext('var mkEvent = this.mkEvent; var window = this.window; var document = this.document;', sandbox);
const sid = 'test-session-123';
"""


def _run_node(payload: dict) -> dict:
    js = (
        "const params = " + json.dumps(payload) + ";\n"
        + _VM_PRELUDE
        + payload["driver"]
    )
    r = subprocess.run([NODE, "-e", js], capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        raise RuntimeError(f"node failed: {r.stderr}")
    return json.loads(r.stdout.strip().splitlines()[-1])


def _helpers() -> str:
    return "\n".join(
        _extract_function(SESSIONS_JS, name)
        for name in (
            "_sessionUrlForSid",
            "_markSessionUrlExact",
            "_sessionUrlRequestsExactTarget",
            "_newTabOwningProfileAllowed",
            "_openSessionUrlInNewTab",
            "_consumeSessionNewTabClick",
        )
    )


def _pointerup_branch() -> str:
    """The Ctrl/Cmd branch of the top-level onpointerup, verbatim."""
    idx = SESSIONS_JS.index("if((e.ctrlKey||e.metaKey)")
    end = SESSIONS_JS.index(
        "if(_finishSessionGesture(e.clientX,e.clientY,e.target,e.pointerType))",
        idx,
    )
    return SESSIONS_JS[idx:end]


class TestNewTabBehavior:
    def test_helper_opens_deep_link_blank(self):
        out = _run_node({
            "helpers": _helpers(),
            "driver": r"""
const opened0 = vm.runInContext(`_openSessionUrlInNewTab("test-session-123")`, sandbox);
ret.opened = sandbox.opened; ret.calls = sandbox.openCalls; ret.ret = opened0;
console.log(JSON.stringify(ret));
""",
        })
        assert out["calls"] == 1
        assert out["opened"]["u"] == "/session/test-session-123"
        assert out["opened"]["t"] == "_blank"

    def test_middle_click_consumed_plain_click_passes_through(self):
        out = _run_node({
            "helpers": _helpers(),
            "driver": r"""
const mid = vm.runInContext(
  `_consumeSessionNewTabClick(mkEvent({button:1,target:null}), "test-session-123")`, sandbox);
const midOpened = sandbox.opened;
sandbox.opened = null; sandbox.openCalls = 0;
const plain = vm.runInContext(
  `_consumeSessionNewTabClick(mkEvent({button:0,target:null}), "test-session-123")`, sandbox);
ret.mid = mid; ret.midOpened = !!midOpened; ret.plain = plain; ret.plainOpened = !!sandbox.opened;
console.log(JSON.stringify(ret));
""",
        })
        assert out["mid"] is True and out["midOpened"] is True
        assert out["plain"] is False and out["plainOpened"] is False

    def test_ctrl_click_consumed_select_mode_and_menu_blocked(self):
        """Ctrl/Cmd+click opens; select mode and the ⋮ action menu refuse.

        The action-target guard is the real ``.session-actions`` class in the
        shared exclusion list — the per-row ``_isSessionActionTarget`` predicate
        is closure-local and unreachable from the helper — so a target whose
        ``closest('.session-actions', …)`` matches must not open a tab.
        """
        out = _run_node({
            "helpers": _helpers(),
            "driver": r"""
const ctrl = vm.runInContext(
  `_consumeSessionNewTabClick(mkEvent({button:0,ctrlKey:true,target:null}), "test-session-123")`, sandbox);
const ctrlOpened = !!sandbox.opened;
sandbox.opened = null;
vm.runInContext(`_sessionSelectMode = true;`, sandbox);
const blockedSelect = vm.runInContext(
  `_consumeSessionNewTabClick(mkEvent({button:1,target:null}), "test-session-123")`, sandbox);
vm.runInContext(`_sessionSelectMode = false;`, sandbox);
sandbox.__menuTarget = { closest: (sel) => (String(sel).indexOf('session-actions') >= 0 ? {} : null) };
const blockedMenu = vm.runInContext(
  `_consumeSessionNewTabClick(mkEvent({button:1,target:__menuTarget}), "test-session-123")`, sandbox);
ret.ctrl = ctrl; ret.ctrlOpened = ctrlOpened;
ret.blockedSelect = blockedSelect; ret.blockedMenu = blockedMenu;
ret.stillClosed = !sandbox.opened;
console.log(JSON.stringify(ret));
""",
        })
        assert out["ctrl"] is True and out["ctrlOpened"] is True
        assert out["blockedSelect"] is False
        assert out["blockedMenu"] is False
        assert out["stillClosed"] is True

    def test_modified_pointerup_cancels_pending_tap(self):
        """Execute the real pointerup branch: pending tap cleared, tab opened,
        and the same-tab gesture finisher never runs.

        The branch ends in a bare ``return`` (it lives inside the row's
        ``onpointerup`` closure), so the harness rewrites the verbatim
        ``if(_consume...) return;`` tail to capture the observation object
        into ``globalThis`` *before* returning, then observes the timer ref,
        opened tab, and finisher flag. The branch and helpers travel via
        temp files (instead of nested ``json.dumps`` string concatenation)
        to keep quoting levels manageable.
        """
        branch = _pointerup_branch()
        consume = _extract_function(SESSIONS_JS, "_consumeSessionNewTabClick")
        opener = _extract_function(SESSIONS_JS, "_openSessionUrlInNewTab")
        urlfn = _extract_function(SESSIONS_JS, "_sessionUrlForSid")
        # The branch under test runs inside the row's gesture closure, so the
        # harness must stub every closure free-var the branch touches:
        # _tapTimer/_lastTapTime (pending tap), _clearPointerDragState
        # (gesture settle choke point), el (row node), and the new-tab choke
        # point. Missing stubs surface as ReferenceError here by design —
        # that is exactly the CI failure the maintainer reported.
        assert "_clearPointerDragState()" in branch
        driver = (
            "const fs = require('fs');\n"
            "const branchSrc = fs.readFileSync("
            + json.dumps("BRANCH_FILE") + ", 'utf8');\n"
            "const params = { urlSrc: fs.readFileSync("
            + json.dumps("URL_FILE") + ", 'utf8'),\n"
            "  openSrc: fs.readFileSync("
            + json.dumps("OPEN_FILE") + ", 'utf8'),\n"
            "  consumeSrc: fs.readFileSync("
            + json.dumps("CONSUME_FILE") + ", 'utf8') };\n"
            + r"""
const ret = {};
const ref = { v: 'PENDING-TAP' };
const runnerSrc =
  'let _tapTimer = ref.v; let _lastTapTime = 111;' +
  'const clearTimeout = (id) => { if (id === _tapTimer) { _tapTimer = null; ref.v = null; } };' +
  'const e = { button: 0, ctrlKey: true, metaKey: false, target: null, preventDefault() {}, stopPropagation() {} };' +
  'const s = { session_id: "test-session-123" };' +
  'const _sessionUrlForSid = ' + params.urlSrc + ';' +
  'const _openSessionUrlInNewTab = ' + params.openSrc + ';' +
  'const _consumeSessionNewTabClick = ' + params.consumeSrc + ';' +
  'let opened = null; const window = { open: (u,t,f) => { opened = {u,t,f}; return null; } };' +
  'window.location = { href: "http://127.0.0.1:8787/", pathname: "/", search: "", hash: "", origin: "http://127.0.0.1:8787" };' +
  'const doc = { baseURI: "http://127.0.0.1:8787/" };' +
  'const _sessionSelectMode = false; const _renamingSid = null;' +
  // Row is owned by the active profile (single-profile path): the new-tab
  // owning-profile gate is a no-op here; it is exercised in
  // TestMaintainerFollowUps.
  'const _newTabOwningProfileAllowed = () => true;' +
  'let finisherRan = false;' +
  'const _finishSessionGesture = () => { finisherRan = true; return false; };' +
  // Pen-drag-painted row state: the gesture is mid-drag with swipe tracking
  // on (i.e. _paintSessionSwipe already ran and set the offset CSS vars).
  // The choke-point stub below mirrors the shipped _clearPointerDragState
  // (idle + long-press disarm + settle swipe paint when a drag was in
  // flight) so the test observes the same settlement the row gets.
  'let _gestureState = "dragging"; let _swipeTracking = true;' +
  'let _longPressMenuOpened = false;' +
  'let longPressCleared = false; let settleCalls = 0;' +
  'const removedClasses = [];' +
  'const _clearLongPressTimer = () => { longPressCleared = true; };' +
  'const _settleSessionSwipePaint = () => { settleCalls++; removedClasses.push("dragging"); };' +
  'const _clearPointerDragState = () => {' +
  '  const wasDragging = _gestureState === "dragging" || _swipeTracking;' +
  '  _gestureState = "idle"; _clearLongPressTimer();' +
  '  if(wasDragging){ settleCalls++; removedClasses.push("dragging"); }' +
  '};' +
  'let loadingRemoved = false;' +
  'const el = { classList: { remove(c) { if(c === "loading") loadingRemoved = true; } } };' +
  branchSrc.replace(/(\W)document(\W)/g, '$1doc$2')
  .replace(/if\(_consumeSessionNewTabClick\(e, s\.session_id, s\)\) return;/,
    'if(_consumeSessionNewTabClick(e, s.session_id, s)){ globalThis.__capture = { tapTimer: _tapTimer, ref: ref.v, lastTap: _lastTapTime, opened: opened, loadingRemoved: loadingRemoved, finisherRan: finisherRan, gestureState: _gestureState, settleCalls: settleCalls, longPressCleared: longPressCleared }; }') +
  '; globalThis.__capture = globalThis.__capture || { tapTimer: _tapTimer, ref: ref.v, lastTap: _lastTapTime, opened: opened, loadingRemoved: loadingRemoved, finisherRan: finisherRan, gestureState: _gestureState, settleCalls: settleCalls, longPressCleared: longPressCleared };';
try {
  new Function('ref', runnerSrc)(ref);
  ret.out = globalThis.__capture;
  delete globalThis.__capture;
} catch (err) { ret.error = String(err && err.message || err); }
console.log(JSON.stringify(ret));
"""
        )
        with tempfile.TemporaryDirectory() as tmp:
            files = {
                "BRANCH_FILE": branch,
                "URL_FILE": urlfn,
                "OPEN_FILE": opener,
                "CONSUME_FILE": consume,
            }
            concreto = driver
            for key, content in files.items():
                path = str(Path(tmp) / (key.lower() + ".js"))
                Path(path).write_text(content, encoding="utf-8")
                concreto = concreto.replace(json.dumps(key), json.dumps(path))
            r = subprocess.run([NODE, "-e", concreto],
                               capture_output=True, text=True, timeout=30)
            if r.returncode != 0:
                raise RuntimeError(f"node failed: {r.stderr}")
            out = json.loads(r.stdout.strip().splitlines()[-1])
        assert "error" not in out, out.get("error")
        assert out["out"]["tapTimer"] is None
        assert out["out"]["ref"] is None
        assert out["out"]["lastTap"] == 0
        assert out["out"]["opened"] == {"u": "/session/test-session-123", "t": "_blank", "f": "noopener"}
        assert out["out"]["loadingRemoved"] is True
        assert out["out"]["finisherRan"] is False
        # Gesture settled via the shared choke point before the return: parked
        # to idle, swipe-paint settle ran for the in-flight pen drag,
        # long-press disarmed. (Row starts `dragging` + swipe-tracking to
        # emulate the pen-drag-painted state the maintainer identified.)
        assert out["out"]["gestureState"] == "idle"
        assert out["out"]["settleCalls"] >= 1
        assert out["out"]["longPressCleared"] is True

    def test_select_mode_ctrl_click_skips_branch_and_runs_finisher(self):
        """Select-mode regression: Ctrl+click must NOT mutate gesture state.

        _consumeSessionNewTabClick refuses select mode, so the pointerup
        branch must be skipped entirely — gesture stays 'pressing' and the
        fall-through _finishSessionGesture runs (row toggles). Before the
        select/rename gate, the branch parked state to 'idle' first and the
        finisher early-returned, breaking Ctrl+click (de)select.
        """
        branch = _pointerup_branch()
        consume = _extract_function(SESSIONS_JS, "_consumeSessionNewTabClick")
        opener = _extract_function(SESSIONS_JS, "_openSessionUrlInNewTab")
        urlfn = _extract_function(SESSIONS_JS, "_sessionUrlForSid")
        # The branch under test runs inside the row's gesture closure, so the
        # harness must stub every closure free-var the branch touches.
        # Select mode ON: _consumeSessionNewTabClick refuses the event, so
        # the branch must be skipped — no choke, no timer touch — and the
        # appended verbatim finisher tail must run with state intact.
        assert "_sessionSelectMode" in branch
        driver = (
            "const fs = require('fs');\n"
            "const branchSrc = fs.readFileSync("
            + json.dumps("BRANCH_FILE") + ", 'utf8');\n"
            "const params = { urlSrc: fs.readFileSync("
            + json.dumps("URL_FILE") + ", 'utf8'),\n"
            "  openSrc: fs.readFileSync("
            + json.dumps("OPEN_FILE") + ", 'utf8'),\n"
            "  consumeSrc: fs.readFileSync("
            + json.dumps("CONSUME_FILE") + ", 'utf8') };\n"
            + r"""
const ret = {};
const ref = { v: null };
const runnerSrc =
  'let _tapTimer = null; let _lastTapTime = 0;' +
  'const clearTimeout = (id) => {};' +
  'const e = { button: 0, ctrlKey: true, metaKey: false, clientX: 10, clientY: 20, pointerType: "mouse", target: null, preventDefault() {}, stopPropagation() {} };' +
  'const s = { session_id: "test-session-123" };' +
  'const _sessionUrlForSid = ' + params.urlSrc + ';' +
  'const _openSessionUrlInNewTab = ' + params.openSrc + ';' +
  'const _consumeSessionNewTabClick = ' + params.consumeSrc + ';' +
  'let opened = null; const window = { open: (u,t,f) => { opened = {u,t,f}; return null; } };' +
  'window.location = { href: "http://127.0.0.1:8787/", pathname: "/", search: "", hash: "", origin: "http://127.0.0.1:8787" };' +
  'const doc = { baseURI: "http://127.0.0.1:8787/" };' +
  'const _sessionSelectMode = true; const _renamingSid = null;' +
  'const _newTabOwningProfileAllowed = () => true;' +
  'let finisherRan = false;' +
  'const _finishSessionGesture = () => { finisherRan = true; return true; };' +
  'let _gestureState = "pressing"; let _swipeTracking = false;' +
  'let _longPressMenuOpened = false;' +
  'let chokeRan = false;' +
  'const _clearLongPressTimer = () => {};' +
  'const _settleSessionSwipePaint = () => {};' +
  'const _clearPointerDragState = () => { chokeRan = true; _gestureState = "idle"; };' +
  'const el = { classList: { remove(c) {} } };' +
  // Verbatim branch (ends before the finisher tail), then the real
  // fall-through tail re-attached so the skip path is exercised for real.
  branchSrc.replace(/(\W)document(\W)/g, '$1doc$2') +
  '; if(_finishSessionGesture(e.clientX,e.clientY,e.target,e.pointerType)) { globalThis.__stopCalled = true; }' +
  '; globalThis.__capture = { opened: opened, finisherRan: finisherRan, gestureState: _gestureState, chokeRan: chokeRan, stopCalled: !!globalThis.__stopCalled };';
try {
  new Function('ref', runnerSrc)(ref);
  ret.out = globalThis.__capture;
  delete globalThis.__capture;
  delete globalThis.__stopCalled;
} catch (err) { ret.error = String(err && err.message || err); }
console.log(JSON.stringify(ret));
"""
        )
        with tempfile.TemporaryDirectory() as tmp:
            files = {
                "BRANCH_FILE": branch,
                "URL_FILE": urlfn,
                "OPEN_FILE": opener,
                "CONSUME_FILE": consume,
            }
            concreto = driver
            for key, content in files.items():
                path = str(Path(tmp) / (key.lower() + ".js"))
                Path(path).write_text(content, encoding="utf-8")
                concreto = concreto.replace(json.dumps(key), json.dumps(path))
            r = subprocess.run([NODE, "-e", concreto],
                               capture_output=True, text=True, timeout=30)
            if r.returncode != 0:
                raise RuntimeError(f"node failed: {r.stderr}")
            out = json.loads(r.stdout.strip().splitlines()[-1])
        assert "error" not in out, out.get("error")
        assert out["out"]["opened"] is None
        assert out["out"]["finisherRan"] is True
        assert out["out"]["gestureState"] == "pressing"
        assert out["out"]["chokeRan"] is False

    def test_wirer_opens_on_auxclick_and_swallows_mousedown(self):
        """The shared wirer (single choke point for all row kinds): auxclick
        button-1 opens the tab; mousedown button-1 preventDefaults (no
        autoscroll) without opening; other buttons ignored."""
        wire = _extract_function(SESSIONS_JS, "_wireSessionNewTabListeners")
        out = _run_node({
            "helpers": _helpers(),
            "driver": (
                "const wireSrc = " + json.dumps(wire) + r""";
const seen = {};
const node = { addEventListener: (t, fn) => { seen[t] = fn; } };
const getSid = () => "test-session-123";
const mkLocalEvent = (over) => Object.assign(
  { button: 0, ctrlKey: false, metaKey: false, target: null,
    preventDefault() { sandbox.prevented++; }, stopPropagation() { sandbox.stopped++; } }, over || {});
const runner = new Function('node', 'getSid', 'window', 'document',
  '_sessionSelectMode', '_renamingSid',
  '_openSessionUrlInNewTab', '_sessionUrlForSid', '_consumeSessionNewTabClick',
  wireSrc + '; _wireSessionNewTabListeners(node, getSid);');
runner(node, getSid, sandbox.window, sandbox.document, false, null,
  sandbox._openSessionUrlInNewTab, sandbox._sessionUrlForSid, sandbox._consumeSessionNewTabClick);
ret.hasAux = typeof seen['auxclick'] === 'function';
ret.hasDown = typeof seen['mousedown'] === 'function';
// auxclick middle button opens
seen['auxclick'](mkLocalEvent({ button: 1, target: null }));
ret.auxOpened = !!sandbox.opened;
sandbox.opened = null; sandbox.openCalls = 0; sandbox.prevented = 0;
// mousedown middle button only swallows default
seen['mousedown'](mkLocalEvent({ button: 1, target: null }));
ret.downPrevented = sandbox.prevented === 1;
ret.downOpened = !!sandbox.opened;
// left auxclick ignored
seen['auxclick'](mkLocalEvent({ button: 0, target: null }));
ret.leftIgnored = sandbox.openCalls === 0;
console.log(JSON.stringify(ret));
"""
            ),
        })
        assert out["hasAux"] is True and out["hasDown"] is True
        assert out["auxOpened"] is True
        assert out["downPrevented"] is True and out["downOpened"] is False
        assert out["leftIgnored"] is True


# ── Maintainer follow-ups (nesquena-hermes CHANGES_REQUESTED, 2026-10-06) ─────
#
# The review gated head 85e6bf0fc and asked for one CORE fix (another profile's
# session must not open in a new tab) and three SHOULD-FIX gesture edges closed
# by a single extra condition on the Ctrl/Cmd pointerup branch. Each test below
# is a red-before lock: it executes the *shipped* branch (or the shipped
# handler text) verbatim, so it fails against the pre-follow-up revision.


def test_pointerup_branch_gates_on_gesture_origin_and_long_press():
    """[SHOULD-FIX] The Ctrl branch must require a press that began on this row
    (`_gestureState!=='idle'`) and no already-open pen long-press menu
    (`!_longPressMenuOpened`). Red-before: without both, a Ctrl-release over an
    untouched row, or on top of an open long-press menu, opened a tab."""
    idx = SESSIONS_JS.index("if((e.ctrlKey||e.metaKey)")
    condition = SESSIONS_JS[idx:SESSIONS_JS.index("{", idx)].replace(" ", "")
    assert "_sessionSelectMode" in condition
    assert "_renamingSid" in condition
    assert "_gestureState!=='idle'" in condition
    assert "!_longPressMenuOpened" in condition


def test_ctrl_double_click_does_not_start_rename():
    """[SHOULD-FIX #1] A Ctrl/Cmd+double-click is two modified clicks (two
    tabs); the dblclick handler must bail outside select mode instead of also
    renaming in the current tab. Red-before: it renamed."""
    idx = SESSIONS_JS.index("el.ondblclick=(e)=>{")
    handler = SESSIONS_JS[idx:idx + 500].replace(" ", "")
    assert "if((e.ctrlKey||e.metaKey)&&!_sessionSelectMode)return;" in handler


def test_open_session_url_consults_owning_profile():
    """[CORE] Opening a new tab is gated on the row's owning profile, and every
    call site threads the concrete session through so the gate can see it."""
    opener = _extract_function(SESSIONS_JS, "_openSessionUrlInNewTab")
    assert "_newTabOwningProfileAllowed(session)" in opener
    assert "session_new_tab_other_profile" in opener
    assert "_profileMatchesActiveProfile" in _extract_function(
        SESSIONS_JS, "_newTabOwningProfileAllowed")
    # Every row kind passes its session to the choke points; child rows also
    # mark their deep link exact so the new tab lands on the child.
    assert "_consumeSessionNewTabClick(e, s.session_id, s)" in SESSIONS_JS
    assert "_consumeSessionNewTabClick(e, child.session_id, child, {exact:true})" in SESSIONS_JS
    assert "_consumeSessionNewTabClick(e, seg.session_id, seg)" in SESSIONS_JS
    assert "_openSessionUrlInNewTab(childSession.session_id, childSession, {exact:true})" in SESSIONS_JS
    assert "_wireSessionNewTabListeners(el, ()=>s.session_id, ()=>s)" in SESSIONS_JS


# Executes the verbatim Ctrl/Cmd pointerup branch with caller-controlled
# closure state. Every free-var the branch reads is either a `ref` field
# (locals declared below) or a sandbox global passed in as a parameter.
_POINTERUP_BRANCH_BODY = (
    "let _tapTimer = ref.tapTimer; let _lastTapTime = ref.lastTap;"
    "let _gestureState = ref.gestureState; let _swipeTracking = ref.swipeTracking;"
    "let _longPressMenuOpened = ref.longPress;"
    "const clearTimeout = (id) => { if (id === _tapTimer) { _tapTimer = null; ref.tapTimer = null; } };"
    "const el = { classList: { remove(c) { ref.loadingRemoved = true; } } };"
    "const _clearLongPressTimer = () => { ref.longPressCleared = true; };"
    "const _settleSessionSwipePaint = () => { ref.settleCalls = (ref.settleCalls || 0) + 1; };"
    "const _clearPointerDragState = () => {"
    " const wasDragging = _gestureState === 'dragging' || _swipeTracking;"
    " _gestureState = 'idle'; _clearLongPressTimer(); ref.chokeRan = true;"
    " if (wasDragging) { _settleSessionSwipePaint(); } };"
    "const _finishSessionGesture = () => { ref.finisherRan = true; return !!ref.finisherRet; };"
    "const e = Object.assign({ button: ref.button, ctrlKey: ref.ctrlKey, metaKey: false,"
    " clientX: 10, clientY: 20, pointerType: 'mouse', target: null,"
    " preventDefault() {}, stopPropagation() {} }, ref.eventOver || {});"
    "const s = ref.session;"
    "__BRANCH__"
    "; ref.gestureStateAfter = _gestureState;"
    "; if (_finishSessionGesture(e.clientX, e.clientY, e.target, e.pointerType)) { ref.stopCalled = true; }"
)


def _run_branch_variant(*, gesture_state="dragging", swipe_tracking=True,
                        long_press=False, ctrl=True, select_mode=False,
                        renaming=None, finisher_ret=False, session=None,
                        show_all_profiles=None, active_profile="default",
                        event_over=None, scope=None,
                        new_tab_supported=None, shell=False):
    """Drive the verbatim Ctrl/Cmd pointerup branch with a chosen closure state.

    ``new_tab_supported`` mirrors the shipped ``_newTabOpenSupported()`` result:
    ``None`` leaves the helper undefined (the branch's ``typeof`` guard then
    treats the environment as capable — matching every pre-existing harness),
    while ``False``/``True`` define it and so exercise the native-shell decline.
    ``shell`` also puts the WKWebView message handlers on ``window`` so the
    opener itself declines too.
    """
    body = _POINTERUP_BRANCH_BODY.replace("__BRANCH__", _pointerup_branch())
    nt_arg = "undefined" if new_tab_supported is None else (
        "() => true" if new_tab_supported else "() => false")
    shell_line = (
        "sandbox.window.webkit = { messageHandlers: { hermesNotify: function(){},"
        " hermesTheme: function(){} } };\n" if shell else "")
    all_on = bool(show_all_profiles)
    if scope is None:
        scope = {"profile": active_profile, "allProfiles": all_on}
    env = [
        "var _sessionSelectMode = %s;" % ("true" if select_mode else "false"),
        "var _renamingSid = %s;" % json.dumps(renaming),
        # The shipped gate reads these globals on every call; always declare
        # them (matching production) so the verbatim source never hits a
        # ReferenceError path that only exists in the harness.
        "var _showAllProfiles = %s;" % ("true" if all_on else "false"),
        "var S = { activeProfile: %s };" % json.dumps(active_profile),
        "var _profileMatchesActiveProfile = function(p, a){"
        " var n = (typeof p === 'string' && p.trim()) ? p.trim() : 'default';"
        " var m = (typeof a === 'string' && a.trim()) ? a.trim() : 'default';"
        " return n === m; };",
        "var _sidebarSessionProfileName = function(x){"
        " return (x && typeof x.profile === 'string') ? x.profile.trim() : ''; };",
        "var showToast = function(msg){ globalThis.__toast = msg; };",
        "var t = function(k){ return 'T:' + k; };",
        "var _allSessionsScope = %s;" % json.dumps(scope),
    ]
    ref = {
        "tapTimer": "PENDING-TAP", "lastTap": 111,
        "gestureState": gesture_state, "swipeTracking": swipe_tracking,
        "longPress": long_press, "button": 0, "ctrlKey": ctrl,
        "finisherRet": finisher_ret,
        "session": session or {"session_id": "test-session-123"},
    }
    if event_over:
        ref["eventOver"] = event_over
    driver = (
        "const makeRunner = new Function("
        "'window','document','_sessionUrlForSid','_newTabOwningProfileAllowed',"
        "'_openSessionUrlInNewTab','_consumeSessionNewTabClick',"
        "'_sessionSelectMode','_renamingSid','ref',"
        "'_newTabOpenSupported',"
        + json.dumps(body) + ");\n"
        "vm.runInContext(" + json.dumps("\n".join(env)) + ", sandbox);\n"
        "const ref = " + json.dumps(ref) + ";\n"
        "sandbox.opened = null; sandbox.openCalls = 0;\n"
        + shell_line +
        "try {\n"
        "  makeRunner(sandbox.window, sandbox.document, sandbox._sessionUrlForSid,\n"
        "    sandbox._newTabOwningProfileAllowed, sandbox._openSessionUrlInNewTab,\n"
        "    sandbox._consumeSessionNewTabClick,\n"
        "    vm.runInContext('_sessionSelectMode', sandbox),\n"
        "    vm.runInContext('_renamingSid', sandbox),\n"
        "    ref, " + nt_arg + ");\n"
        "} catch (err) { ret.error = String(err && err.message || err); }\n"
        "ret.opened = sandbox.opened; ret.openCalls = sandbox.openCalls;\n"
        "ret.toast = (typeof sandbox.__toast !== 'undefined') ? sandbox.__toast : null;\n"
        "ret.ref = ref;\n"
        "console.log(JSON.stringify(ret));\n"
    )
    return _run_node({"helpers": _helpers(), "driver": driver})


class TestMaintainerFollowUps:
    def test_ctrl_release_over_untouched_row_opens_nothing(self):
        """[SHOULD-FIX #2] A press that never began on this row (idle gesture)
        must not open a tab; the branch is skipped and the verbatim finisher
        early-returns. Red-before: any Ctrl-release over a row opened a tab."""
        out = _run_branch_variant(gesture_state="idle", swipe_tracking=False)
        assert "error" not in out, out.get("error")
        assert out["openCalls"] == 0 and out["opened"] is None
        assert out["ref"].get("chokeRan") is not True
        assert out["ref"]["gestureStateAfter"] == "idle"
        assert out["ref"]["finisherRan"] is True

    def test_pen_long_press_menu_then_ctrl_release_opens_nothing(self):
        """[SHOULD-FIX #3] With the pen long-press menu already open, a
        Ctrl-release must not stack a tab on top of it (Greptile P1).
        Red-before: the branch fired regardless of `_longPressMenuOpened`."""
        out = _run_branch_variant(gesture_state="pressing",
                                  swipe_tracking=False, long_press=True)
        assert "error" not in out, out.get("error")
        assert out["openCalls"] == 0 and out["opened"] is None
        assert out["ref"].get("chokeRan") is not True

    def test_ctrl_click_with_press_still_opens_one_tab(self):
        """Guard against over-blocking: a real Ctrl+click (press began, no
        long-press menu) still opens exactly one new tab."""
        out = _run_branch_variant(gesture_state="pressing", swipe_tracking=False)
        assert "error" not in out, out.get("error")
        assert out["openCalls"] == 1
        assert out["opened"]["u"] == "/session/test-session-123"
        assert out["opened"]["f"] == "noopener"
        assert out["ref"]["chokeRan"] is True

    def test_foreign_profile_session_refused_in_new_tab(self):
        """[CORE] With "show all profiles" on, a row owned by another profile is
        refused (no cookie switch, source tab stays valid): the gesture is
        consumed with a notice. Red-before: it opened a tab and 409'd the
        source tab's next /api/chat/start."""
        out = _run_branch_variant(
            gesture_state="pressing", swipe_tracking=False,
            show_all_profiles=True, active_profile="alpha",
            session={"session_id": "test-session-123", "profile": "beta"})
        assert "error" not in out, out.get("error")
        assert out["openCalls"] == 0 and out["opened"] is None
        assert out["toast"] == "T:session_new_tab_other_profile"
        assert out["ref"]["chokeRan"] is True  # consumed, not fallen through

    def test_same_profile_session_opens_in_new_tab(self):
        """[CORE] A row owned by the active profile still opens normally."""
        out = _run_branch_variant(
            gesture_state="pressing", swipe_tracking=False,
            show_all_profiles=True, active_profile="alpha",
            session={"session_id": "test-session-123", "profile": "alpha"})
        assert "error" not in out, out.get("error")
        assert out["openCalls"] == 1
        assert out["toast"] is None

    def test_unknown_profile_owner_refused_when_merging_profiles(self):
        """[CORE] An unverifiable owner (no profile field while merging
        profiles) is refused rather than guessed."""
        out = _run_branch_variant(
            gesture_state="pressing", swipe_tracking=False,
            show_all_profiles=True, active_profile="alpha",
            session={"session_id": "test-session-123"})
        assert "error" not in out, out.get("error")
        assert out["openCalls"] == 0
        assert out["toast"] == "T:session_new_tab_other_profile"

    # ── Round 2 (nesquena-hermes CHANGES_REQUESTED, 2026-10-07) ──────────────

    def test_foreign_row_refused_after_show_all_toggled_off(self):
        """[CORE round 2] Switching "show sessions from all profiles" off flips
        `_showAllProfiles` immediately, but the sidebar keeps rendering the
        previous scope's foreign rows until the refetch lands. A retained row
        with a KNOWN foreign owner must still be refused: the shared cookie
        switch would 409 the source tab. Red-before: the toggle-based
        `!_showAllProfiles` shortcut waved it through and opened a tab."""
        out = _run_branch_variant(
            gesture_state="pressing", swipe_tracking=False,
            show_all_profiles=False, active_profile="alpha",
            session={"session_id": "test-session-123", "profile": "beta"})
        assert "error" not in out, out.get("error")
        assert out["openCalls"] == 0 and out["opened"] is None
        assert out["toast"] == "T:session_new_tab_other_profile"
        assert out["ref"]["chokeRan"] is True  # consumed, not fallen through

    def test_unknown_owner_refused_when_scope_is_all_profiles(self):
        """[CORE round 2] An ownerless row is only trustworthy when the loaded
        sidebar cache is a single-profile scope; a scope loaded with
        `allProfiles:true` leaves the owner unverifiable, so the gesture is
        refused. Red-before: the toggle shortcut allowed it."""
        out = _run_branch_variant(
            gesture_state="pressing", swipe_tracking=False,
            show_all_profiles=False, active_profile="alpha",
            scope={"profile": "alpha", "allProfiles": True},
            session={"session_id": "test-session-123"})
        assert "error" not in out, out.get("error")
        assert out["openCalls"] == 0 and out["opened"] is None
        assert out["toast"] == "T:session_new_tab_other_profile"

    def test_unknown_owner_allowed_when_single_profile_scope_matches(self):
        """Guard against over-blocking: with show-all off and a single-profile
        scope for the active profile, an ownerless row (e.g. a lineage segment
        that has been attributed to its row) still opens."""
        out = _run_branch_variant(
            gesture_state="pressing", swipe_tracking=False,
            show_all_profiles=False, active_profile="alpha",
            scope={"profile": "alpha", "allProfiles": False},
            session={"session_id": "test-session-123"})
        assert "error" not in out, out.get("error")
        assert out["openCalls"] == 1
        assert out["opened"]["u"] == "/session/test-session-123"
        assert out["toast"] is None

    def test_toggle_off_does_not_short_circuit_profile_gate(self):
        """[CORE round 2] The gate must not contain a `!_showAllProfiles`
        unconditional allow: a known owner is always compared to the active
        profile, and an unknown owner consults the loaded cache scope."""
        gate = _extract_function(SESSIONS_JS, "_newTabOwningProfileAllowed")
        flat = gate.replace(" ", "")
        assert "!_showAllProfiles)returntrue" not in flat
        assert "_allSessionsScope" in gate
        assert "_profileMatchesActiveProfile" in gate


# ── Round 3 (nesquena-hermes CHANGES_REQUESTED, 2026-10-07T10:12) ─────────────
#
# Two items gated the exact head `a6b93caa`:
#  1. a modified/middle click on a historical CLI lineage segment opened its
#     *continuation* in the new tab (a silent regression vs master), and
#  2. the macOS WKWebView shell silently drops `window.open`, so the gesture
#     was a dead click there (hermes-swift-mac#102).
# Each test below is a red-before lock against the pre-fix revision.


def test_lineage_segment_excluded_from_new_tab_choke_points():
    """[Round 3 item 1] A modified/middle click on a lineage segment must keep
    master's same-tab load: the deep-link page drops `skipLineageResolve`, so
    the new tab re-resolved the segment to its continuation. Both new-tab
    choke points must refuse a `.session-lineage-segment` target."""
    consume = _extract_function(SESSIONS_JS, "_consumeSessionNewTabClick")
    wire = _extract_function(SESSIONS_JS, "_wireSessionNewTabListeners")
    assert ".session-lineage-segment" in consume
    # auxclick + mousedown exclusion lists both carry the segment class.
    assert wire.count(".session-lineage-segment") >= 2
    # The segment's own handler still carries the same-tab load + the flag.
    assert "await _openSidebarSession(seg, {skipLineageResolve:true});" in SESSIONS_JS
    assert "if(!opts.skipLineageResolve" in SESSIONS_JS


def test_open_helper_declines_in_macos_shell():
    """[Round 3 item 2] The native macOS shell cannot open a second window, so
    `_openSessionUrlInNewTab` must decline (return false) there and let every
    tap path fall back to its same-tab load."""
    opener = _extract_function(SESSIONS_JS, "_openSessionUrlInNewTab")
    assert "window.webkit" in opener
    assert "messageHandlers" in opener
    assert "hermesNotify" in opener and "hermesTheme" in opener


def test_new_tab_supported_helper_gates_state_mutating_callers():
    """[Round 4] The shell capability check is a shared helper consulted by both
    state-mutating callers *before* they mutate: the Ctrl/Cmd pointerup branch
    (before `_clearPointerDragState`) and the modified-double-click suppression.
    Red-before: neither consulted it, so on the shell the pointerup branch
    parked the gesture to idle and the fall-through finisher early-returned,
    turning the click into a dead click (0 tabs, 0 same-tab loads)."""
    helper = _extract_function(SESSIONS_JS, "_newTabOpenSupported")
    assert "window.open" in helper
    assert "window.webkit" in helper
    assert "hermesNotify" in helper and "hermesTheme" in helper
    idx = SESSIONS_JS.index("if((e.ctrlKey||e.metaKey)")
    condition = SESSIONS_JS[idx:SESSIONS_JS.index("{", idx)].replace(" ", "")
    assert "_newTabOpenSupported" in condition
    # Modified-double-click suppression is skipped on a shell too (master
    # renamed there, so the shell must keep the rename path).
    didx = SESSIONS_JS.index("el.ondblclick=(e)=>{")
    handler = SESSIONS_JS[didx:didx + 700].replace(" ", "")
    assert "typeof_newTabOpenSupported!=='function'||_newTabOpenSupported()" in handler


class TestRound3FollowUps:
    def test_segment_target_refused_by_consume_choke_point(self):
        """[Round 3 item 1] `_consumeSessionNewTabClick` must not open a tab
        when the event target is inside a lineage segment. Red-before: the
        exclusion list lacked `.session-lineage-segment`, so it opened."""
        out = _run_node({
            "helpers": _helpers(),
            "driver": r"""
sandbox.__seg = { closest: (sel) => (String(sel).indexOf('session-lineage-segment') >= 0 ? {} : null) };
const mid = vm.runInContext(
  `_consumeSessionNewTabClick(mkEvent({button:1,target:__seg}), "test-session-123")`, sandbox);
const midOpened = !!sandbox.opened;
sandbox.opened = null; sandbox.openCalls = 0;
const ctrl = vm.runInContext(
  `_consumeSessionNewTabClick(mkEvent({button:0,ctrlKey:true,target:__seg}), "test-session-123")`, sandbox);
ret.mid = mid; ret.midOpened = midOpened; ret.ctrl = ctrl; ret.ctrlOpened = !!sandbox.opened;
console.log(JSON.stringify(ret));
""",
        })
        assert out["mid"] is False and out["midOpened"] is False
        assert out["ctrl"] is False and out["ctrlOpened"] is False

    def test_segment_target_refused_by_wirer_auxclick(self):
        """[Round 3 item 1] The shared wirer's auxclick must no-op for a
        segment target instead of opening a tab. Red-before: it opened."""
        wire = _extract_function(SESSIONS_JS, "_wireSessionNewTabListeners")
        out = _run_node({
            "helpers": _helpers(),
            "driver": (
                "const wireSrc = " + json.dumps(wire) + r""";
const seen = {};
const node = { addEventListener: (t, fn) => { seen[t] = fn; } };
const getSid = () => "test-session-123";
const segTarget = { closest: (sel) => (String(sel).indexOf('session-lineage-segment') >= 0 ? {} : null) };
const runner = new Function('node', 'getSid', 'window', 'document',
  '_sessionSelectMode', '_renamingSid',
  '_openSessionUrlInNewTab', '_sessionUrlForSid', '_consumeSessionNewTabClick',
  wireSrc + '; _wireSessionNewTabListeners(node, getSid);');
runner(node, getSid, sandbox.window, sandbox.document, false, null,
  sandbox._openSessionUrlInNewTab, sandbox._sessionUrlForSid, sandbox._consumeSessionNewTabClick);
const ev = { button: 1, ctrlKey: false, metaKey: false, target: segTarget,
  preventDefault() {}, stopPropagation() {} };
seen['auxclick'](ev);
ret.segmentOpened = !!sandbox.opened;
const ev2 = { button: 1, ctrlKey: false, metaKey: false, target: null,
  preventDefault() {}, stopPropagation() {} };
seen['auxclick'](ev2);
ret.plainOpened = !!sandbox.opened;
console.log(JSON.stringify(ret));
"""
            ),
        })
        assert out["segmentOpened"] is False
        assert out["plainOpened"] is True

    def test_macos_shell_declines_new_tab(self):
        """[Round 3 item 2] With the shell's message handlers present, the
        opener declines and the gesture is not consumed, so the caller's
        same-tab path runs. Red-before: it opened a tab."""
        out = _run_node({
            "helpers": _helpers(),
            "driver": r"""
sandbox.window.webkit = { messageHandlers: { hermesNotify: function(){}, hermesTheme: function(){} } };
const opened = vm.runInContext(`_openSessionUrlInNewTab("test-session-123")`, sandbox);
const calls = sandbox.openCalls;
const consumed = vm.runInContext(
  `_consumeSessionNewTabClick(mkEvent({button:0,ctrlKey:true,target:null}), "test-session-123")`, sandbox);
ret.ret = opened; ret.calls = calls; ret.consumed = consumed;
console.log(JSON.stringify(ret));
""",
        })
        assert out["ret"] is False
        assert out["calls"] == 0
        assert out["consumed"] is False

    def test_plain_browser_still_opens_new_tab(self):
        """Guard against over-blocking: with no shell handlers present, the
        opener still opens exactly one noopener tab."""
        out = _run_node({
            "helpers": _helpers(),
            "driver": r"""
ret.ret = vm.runInContext(`_openSessionUrlInNewTab("test-session-123")`, sandbox);
ret.calls = sandbox.openCalls; ret.opened = sandbox.opened;
console.log(JSON.stringify(ret));
""",
        })
        assert out["ret"] is True
        assert out["calls"] == 1
        assert out["opened"]["u"] == "/session/test-session-123"
        assert out["opened"]["f"] == "noopener"

    # ── Round 4 (nesquena-hermes CHANGES_REQUESTED, 2026-10-07T22:04) ─────────

    def test_shell_ctrl_click_falls_back_to_same_tab_load(self):
        """[Round 4 CORE] On the native macOS shell the Ctrl/Cmd pointerup
        branch must be skipped *before* gesture cleanup, so the gesture stays
        live and the fall-through `_finishSessionGesture` runs master's
        same-tab load. Red-before: the branch parked state to idle, the opener
        declined, and the finisher early-returned — a dead click (0 tabs opened
        AND 0 same-tab loads)."""
        out = _run_branch_variant(gesture_state="pressing", swipe_tracking=False,
                                  new_tab_supported=False, shell=True)
        assert "error" not in out, out.get("error")
        assert out["openCalls"] == 0 and out["opened"] is None
        assert out["ref"].get("chokeRan") is not True  # branch skipped pre-cleanup
        assert out["ref"]["finisherRan"] is True       # same-tab load ran
        assert out["ref"]["gestureStateAfter"] == "pressing"

    def test_capable_environment_ctrl_click_still_opens_one_tab(self):
        """[Round 4] Guard against over-blocking: where a second window *is*
        available the branch still fires and opens exactly one noopener tab."""
        out = _run_branch_variant(gesture_state="pressing", swipe_tracking=False,
                                  new_tab_supported=True)
        assert "error" not in out, out.get("error")
        assert out["openCalls"] == 1
        assert out["opened"]["u"] == "/session/test-session-123"
        assert out["opened"]["f"] == "noopener"
        assert out["ref"]["chokeRan"] is True


# ── Round 5 (nesquena-hermes CHANGES_REQUESTED, 2026-10-08T04:22) ─────────────
#
# A nested child row's new-tab link loads `/session/<child>`. Boot resolves the
# URL session through `_resolveSessionIdFromSidebarLineage`, which maps an id
# found in a lineage-like row's `_child_sessions` back to that row — so once the
# sidebar had loaded, a child of a compressed parent opened its PARENT in the new
# tab. A plain child-row same-tab click already avoids this with
# `skipLineageResolve:true`; the fix carries the same choice through the link
# (`?exact=1`) and boot honors it. Each test below is a red-before lock against
# the pre-fix revision `9f44e8d2`.


def test_child_rows_mark_new_tab_link_exact():
    """Child-row new-tab call sites brand the deep link exact so boot loads the
    child itself, not its compressed parent's lineage row. Red-before: none of
    them passed the marker, so the new tab resolved to the parent."""
    # Every child-row path carries {exact:true}: the Ctrl/Cmd choke point (fork
    # main button + plain child button), the auxclick wirer (fork row body, fork
    # main button, plain child button) and the programmatic path.
    assert SESSIONS_JS.count(
        "_consumeSessionNewTabClick(e, child.session_id, child, {exact:true})") == 2
    assert SESSIONS_JS.count(
        "_wireSessionNewTabListeners(row, ()=>child.session_id, ()=>child, {exact:true})") == 2
    assert "_wireSessionNewTabListeners(mainBtn, ()=>child.session_id, ()=>child, {exact:true})" in SESSIONS_JS
    assert "_openSessionUrlInNewTab(childSession.session_id, childSession, {exact:true})" in SESSIONS_JS
    # Top-level rows and lineage segments keep the ordinary (tip-resolving) deep
    # link — the review asked not to change boot for those.
    assert "_consumeSessionNewTabClick(e, s.session_id, s)" in SESSIONS_JS
    assert "_consumeSessionNewTabClick(e, seg.session_id, seg)" in SESSIONS_JS
    assert "_wireSessionNewTabListeners(row, ()=>seg.session_id, ()=>seg)" in SESSIONS_JS
    assert "_wireSessionNewTabListeners(el, ()=>s.session_id, ()=>s)" in SESSIONS_JS


def test_exact_marker_helper_preserves_query_and_fragment():
    """`_markSessionUrlExact` appends the marker before the fragment, keeps an
    existing query string, and is idempotent."""
    out = _run_node({
        "helpers": _helpers(),
        "driver": r"""
const plain = vm.runInContext(`_markSessionUrlExact('/session/abc')`, sandbox);
const withQuery = vm.runInContext(`_markSessionUrlExact('/session/abc?keep=1')`, sandbox);
const withHash = vm.runInContext(`_markSessionUrlExact('/session/abc#frag')`, sandbox);
const both = vm.runInContext(`_markSessionUrlExact('/session/abc?keep=1#frag')`, sandbox);
const idem = vm.runInContext(`_markSessionUrlExact('/session/abc?exact=1')`, sandbox);
ret.plain = plain; ret.withQuery = withQuery; ret.withHash = withHash;
ret.both = both; ret.idem = idem;
console.log(JSON.stringify(ret));
""",
    })
    assert out["plain"] == "/session/abc?exact=1"
    assert out["withQuery"] == "/session/abc?keep=1&exact=1"
    assert out["withHash"] == "/session/abc?exact=1#frag"
    assert out["both"] == "/session/abc?keep=1&exact=1#frag"
    assert out["idem"] == "/session/abc?exact=1"


def test_exact_target_helper_reads_the_marker():
    """`_sessionUrlRequestsExactTarget` is true only for `?exact=1`."""
    out = _run_node({
        "helpers": _helpers(),
        "driver": r"""
vm.runInContext(`window.location.search = '?exact=1';`, sandbox);
ret.on = vm.runInContext(`_sessionUrlRequestsExactTarget()`, sandbox);
vm.runInContext(`window.location.search = '';`, sandbox);
ret.off = vm.runInContext(`_sessionUrlRequestsExactTarget()`, sandbox);
vm.runInContext(`window.location.search = '?exact=0';`, sandbox);
ret.zero = vm.runInContext(`_sessionUrlRequestsExactTarget()`, sandbox);
console.log(JSON.stringify(ret));
""",
    })
    assert out["on"] is True
    assert out["off"] is False
    assert out["zero"] is False


def test_boot_skips_lineage_resolve_only_for_exact_deep_link():
    """Boot passes `skipLineageResolve` only when the deep link is marked exact,
    so an ordinary deep link (e.g. an old lineage-segment URL) still lands on
    its lineage tip. Red-before: boot always resolved through the lineage row."""
    exact_call = "await loadSession(saved, {preserveActiveInput:true, skipLineageResolve:true})"
    assert exact_call in BOOT_JS
    # The marker condition guards the exact call (within its preceding lines).
    idx = BOOT_JS.index(exact_call)
    guard = BOOT_JS[idx - 700:idx]
    assert "_sessionUrlRequestsExactTarget()" in guard
    assert "urlSession" in guard
    # An ordinary deep link keeps the plain restore call.
    assert "await loadSession(saved, {preserveActiveInput:true});" in BOOT_JS


def test_child_new_tab_opens_child_deep_link_exact():
    """Driving the shipped opener for a child row yields the exact-marked child
    URL, while a top-level row yields the plain URL. Red-before: the child URL
    carried no marker, so boot folded it into the compressed parent."""
    out = _run_node({
        "helpers": _helpers(),
        "driver": r"""
const child = vm.runInContext(`_openSessionUrlInNewTab("child-1", undefined, {exact:true})`, sandbox);
const childUrl = sandbox.opened && sandbox.opened.u;
sandbox.opened = null; sandbox.openCalls = 0;
const plain = vm.runInContext(`_openSessionUrlInNewTab("top-1")`, sandbox);
const plainUrl = sandbox.opened && sandbox.opened.u;
ret.child = child; ret.childUrl = childUrl; ret.plain = plain; ret.plainUrl = plainUrl;
console.log(JSON.stringify(ret));
""",
    })
    assert out["child"] is True
    assert out["childUrl"] == "/session/child-1?exact=1"
    assert out["plain"] is True
    assert out["plainUrl"] == "/session/top-1"


def test_action_menu_target_refused_by_wirer_auxclick():
    """[cleanup] The shared wirer refuses a target inside the row's
    `.session-actions` container — the real class check, not the closure-local
    `_isSessionActionTarget` the top-level helper could never see."""
    wire = _extract_function(SESSIONS_JS, "_wireSessionNewTabListeners")
    out = _run_node({
        "helpers": _helpers(),
        "driver": (
            "const wireSrc = " + json.dumps(wire) + r""";
const seen = {};
const node = { addEventListener: (t, fn) => { seen[t] = fn; } };
const getSid = () => "test-session-123";
const menuTarget = { closest: (sel) => (String(sel).indexOf('session-actions') >= 0 ? {} : null) };
const runner = new Function('node', 'getSid', 'window', 'document',
  '_sessionSelectMode', '_renamingSid',
  '_openSessionUrlInNewTab', '_sessionUrlForSid', '_consumeSessionNewTabClick',
  wireSrc + '; _wireSessionNewTabListeners(node, getSid);');
runner(node, getSid, sandbox.window, sandbox.document, false, null,
  sandbox._openSessionUrlInNewTab, sandbox._sessionUrlForSid, sandbox._consumeSessionNewTabClick);
const ev = { button: 1, ctrlKey: false, metaKey: false, target: menuTarget,
  preventDefault() {}, stopPropagation() {} };
seen['auxclick'](ev);
ret.menuOpened = !!sandbox.opened;
console.log(JSON.stringify(ret));
"""
        ),
    })
    assert out["menuOpened"] is False

def test_refresh_keeps_exact_marker_on_same_session_but_drops_it_on_switch():
    """`loadSession()` rewrites the URL through `_setActiveSessionUrl`. On the
    child's own new tab (`/session/<child>?exact=1`) that rewrite must keep the
    marker, otherwise a refresh resolves the child to its compressed parent;
    switching to another session must still drop it (#7429 re-gate c22)."""
    helpers = "\n".join(
        _extract_function(SESSIONS_JS, name)
        for name in (
            "_sessionIdFromLocation",
            "_sessionUrlForSid",
            "_markSessionUrlExact",
            "_sessionUrlRequestsExactTarget",
            "_setActiveSessionUrl",
        )
    )
    out = _run_node({
        "helpers": helpers,
        "driver": r"""
const setLoc = (path, search) => vm.runInContext(
  `window.location.pathname = ${JSON.stringify(path)};
   window.location.search = ${JSON.stringify(search)};
   window.location.hash = '';
   window.location.href = 'http://127.0.0.1:8787' + ${JSON.stringify(path)} + ${JSON.stringify(search)};`, sandbox);
vm.runInContext(`window.history = { calls: [], pushState(s,t,u){ this.calls.push(u); }, replaceState(s,t,u){ this.calls.push(u); } };`, sandbox);
setLoc('/session/child-1', '?exact=1');
vm.runInContext(`_setActiveSessionUrl('child-1')`, sandbox);
ret.sameCalls = vm.runInContext(`window.history.calls.slice()`, sandbox);
setLoc('/session/child-1', '?exact=1');
vm.runInContext(`window.history.calls = []; _setActiveSessionUrl('other-2')`, sandbox);
ret.switchCalls = vm.runInContext(`window.history.calls.slice()`, sandbox);
setLoc('/session/old-seg', '');
vm.runInContext(`window.history.calls = []; _setActiveSessionUrl('tip-3')`, sandbox);
ret.plainCalls = vm.runInContext(`window.history.calls.slice()`, sandbox);
console.log(JSON.stringify(ret));
""",
    })
    # Same session: the URL already matches (marker kept), so nothing is pushed.
    assert out["sameCalls"] == []
    # Switching sessions drops the one-shot marker.
    assert out["switchCalls"] == ["/session/other-2"]
    # Ordinary deep links never gain the marker.
    assert out["plainCalls"] == ["/session/tip-3"]

def test_exact_child_tab_skips_lineage_folding_for_back_and_refresh():
    """Back to the child tab's history entry and same-session refreshes call
    `loadSession(child)` without `skipLineageResolve`; while the URL names that
    child with `?exact=1`, lineage folding must leave it alone. Other sessions
    and unmarked URLs fold as before (#7429 release review, Greptile P1)."""
    helpers = "\n".join(
        _extract_function(SESSIONS_JS, name)
        for name in (
            "_sessionIdFromLocation",
            "_sessionUrlRequestsExactTarget",
            "_sessionUrlTargetsExactSid",
        )
    )
    out = _run_node({
        "helpers": helpers,
        "driver": r"""
const setLoc = (path, search) => vm.runInContext(
  `window.location.pathname = ${JSON.stringify(path)}; window.location.search = ${JSON.stringify(search)};`, sandbox);
const exact = (sid) => vm.runInContext(`_sessionUrlTargetsExactSid(${JSON.stringify(sid)})`, sandbox);
setLoc('/session/child-1', '?exact=1');
ret.sameSession = exact('child-1');
ret.otherSession = exact('parent-9');
setLoc('/session/child-1', '');
ret.unmarked = exact('child-1');
console.log(JSON.stringify(ret));
""",
    })
    assert out["sameSession"] is True
    assert out["otherSession"] is False
    assert out["unmarked"] is False


def test_load_session_keeps_the_exact_child_when_folding_lineage():
    """`loadSession()` consults the exact-target check inside its lineage block,
    so boot, popstate (Back) and same-session refreshes all keep the child; the
    `typeof` guard keeps harnesses that extract loadSession alone working."""
    body = _extract_function(SESSIONS_JS, "loadSession")
    assert "if(!opts.skipLineageResolve && typeof _resolveSessionIdFromSidebarLineage==='function'){" in body
    assert ("const resolvedSid=(typeof _sessionUrlTargetsExactSid==='function' && _sessionUrlTargetsExactSid(sid))"
            " ? sid : _resolveSessionIdFromSidebarLineage(sid);") in body
