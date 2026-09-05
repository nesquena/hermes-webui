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
    assert "_wireSessionNewTabListeners(el, ()=>s.session_id)" in SESSIONS_JS
    assert SESSIONS_JS.count("_wireSessionNewTabListeners(row, ()=>child.session_id)") == 2
    assert "_wireSessionNewTabListeners(mainBtn, ()=>child.session_id)" in SESSIONS_JS
    assert "_wireSessionNewTabListeners(row, ()=>seg.session_id)" in SESSIONS_JS
    assert SESSIONS_JS.count("_wireSessionNewTabListeners(") >= 6  # def + 5 call sites
    # All opens route through the two choke points with the concrete sid.
    assert "_openSessionUrlInNewTab(getSid())" in SESSIONS_JS
    assert "_openSessionUrlInNewTab(sid)" in SESSIONS_JS
    assert "_openSessionUrlInNewTab(childSession.session_id)" in SESSIONS_JS


def test_middle_mousedown_prevents_autoscroll():
    """`mousedown` on button 1 preventDefaults so the browser doesn't autoscroll."""
    assert "addEventListener('auxclick'" in SESSIONS_JS
    assert "addEventListener('mousedown'" in SESSIONS_JS
    wire = _extract_function(SESSIONS_JS, "_wireSessionNewTabListeners")
    assert "button" in wire and "1" in wire
    assert "preventDefault()" in wire
    # Ctrl/Cmd+click on the tap paths also routes to the new-tab opener.
    assert "_consumeSessionNewTabClick(e, child.session_id)" in SESSIONS_JS
    assert "_consumeSessionNewTabClick(e, s.session_id)" in SESSIONS_JS


def test_ctrl_click_opens_new_tab():
    """Ctrl/Cmd+left-click on a row opens the deep link in a new tab."""
    assert "e.ctrlKey||e.metaKey" in SESSIONS_JS.replace(" ", "")


def test_action_menu_and_select_mode_untouched():
    """New-tab must not fire from the ⋮ menu, checkboxes, or select mode."""
    consume = _extract_function(SESSIONS_JS, "_consumeSessionNewTabClick")
    assert "_isSessionActionTarget" in consume
    assert "_sessionSelectMode" in consume
    assert "_renamingSid" in consume
    wire = _extract_function(SESSIONS_JS, "_wireSessionNewTabListeners")
    assert "_isSessionActionTarget" in wire
    assert "session-actions" in wire


def test_openChildSession_new_tab_flag():
    """Child-row programmatic path supports open-in-new-tab without a same-tab switch."""
    idx = SESSIONS_JS.index("const openChildSession=async(childSession,")
    window = SESSIONS_JS[idx:idx + 400]
    assert "newTab" in window
    assert "_openSessionUrlInNewTab(childSession.session_id)" in window


def test_modified_click_cancels_pending_tap_before_new_tab():
    """P1 (#7429 review): the Ctrl/Cmd+click branch must clear the pending
    single-tap timer *before* opening the new tab.

    Without this, the deferred single-tap opener from the FIRST click of a
    fast modified double-click fires after the new tab opens and switches the
    current tab anyway — the exact stale-state class the review flagged.
    """
    idx = SESSIONS_JS.index("if(e.ctrlKey||e.metaKey){")
    window = SESSIONS_JS[idx:idx + 900]
    assert "_consumeSessionNewTabClick(e, s.session_id)" in window
    clear_idx = window.index("clearTimeout(_tapTimer)")
    consume_idx = window.index("_consumeSessionNewTabClick(e, s.session_id)")
    assert clear_idx < consume_idx, (
        "pending-tap cancel must run before the new-tab open, not after"
    )
    assert "_lastTapTime=0" in window.replace(" ", "")
    assert "_tapTimer=null" in window.replace(" ", "")
    # The row's gesture machine was armed by pointerdown before this
    # pointerup fired: the long-press timer must be disarmed and the
    # gesture parked back to idle BEFORE the early return, or a later
    # pointermove promotes the stale `pressing` state to `dragging` and a
    # pending pen long-press opens the action menu behind the new tab.
    nose = window.replace(" ", "")
    assert "_clearLongPressTimer()" in window
    assert "_gestureState='idle'" in nose
    assert window.index("_clearLongPressTimer()") < consume_idx
    assert window.index("_gestureState='idle'") < consume_idx


# ── Behavioral tests via Node VM ─────────────────────────────────────────────

pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")

_VM_PRELUDE = r"""
const ret = {};
const sandbox = { opened: null, openCalls: 0, stopped: 0, prevented: 0,
  _sessionSelectMode: false, _renamingSid: null,
  _isSessionActionTarget: () => false,
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
            "_openSessionUrlInNewTab",
            "_consumeSessionNewTabClick",
        )
    )


def _pointerup_branch() -> str:
    """The Ctrl/Cmd branch of the top-level onpointerup, verbatim."""
    idx = SESSIONS_JS.index("if(e.ctrlKey||e.metaKey){")
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
vm.runInContext(`_sessionSelectMode = false; _isSessionActionTarget = () => true;`, sandbox);
const blockedMenu = vm.runInContext(
  `_consumeSessionNewTabClick(mkEvent({button:1,target:{}}), "test-session-123")`, sandbox);
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
        # _tapTimer/_lastTapTime (pending tap), _clearLongPressTimer +
        # _longPressTimer/_longPressMenuOpened (pen long-press), _gestureState
        # (pressing/dragging machine), el (row node), and the new-tab choke
        # point. Missing stubs surface as ReferenceError here by design —
        # that is exactly the CI failure the maintainer reported.
        assert "_clearLongPressTimer()" in branch and "_gestureState='idle'" in branch.replace(" ", "")
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
  'const _isSessionActionTarget = () => false;' +
  'let finisherRan = false;' +
  'const _finishSessionGesture = () => { finisherRan = true; return false; };' +
  'let _longPressTimer = "ARMED"; let _longPressMenuOpened = false; let longPressCleared = false;' +
  'const _clearLongPressTimer = () => { _longPressTimer = null; longPressCleared = true; };' +
  'let _gestureState = "pressing";' +
  'let loadingRemoved = false;' +
  'const el = { classList: { remove(c) { loadingRemoved = true; } } };' +
  branchSrc.replace(/(\W)document(\W)/g, '$1doc$2')
  .replace(/if\(_consumeSessionNewTabClick\(e, s\.session_id\)\) return;/,
    'if(_consumeSessionNewTabClick(e, s.session_id)){ globalThis.__capture = { tapTimer: _tapTimer, ref: ref.v, lastTap: _lastTapTime, opened: opened, loadingRemoved: loadingRemoved, finisherRan: finisherRan, gestureState: _gestureState, longPressTimer: _longPressTimer, longPressCleared: longPressCleared }; }') +
  '; globalThis.__capture = globalThis.__capture || { tapTimer: _tapTimer, ref: ref.v, lastTap: _lastTapTime, opened: opened, loadingRemoved: loadingRemoved, finisherRan: finisherRan, gestureState: _gestureState, longPressTimer: _longPressTimer, longPressCleared: longPressCleared };';
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
        # Gesture machine parked + pen long-press disarmed before the return.
        assert out["out"]["gestureState"] == "idle"
        assert out["out"]["longPressTimer"] is None
        assert out["out"]["longPressCleared"] is True

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
  '_sessionSelectMode', '_renamingSid', '_isSessionActionTarget',
  '_openSessionUrlInNewTab', '_sessionUrlForSid', '_consumeSessionNewTabClick',
  wireSrc + '; _wireSessionNewTabListeners(node, getSid);');
runner(node, getSid, sandbox.window, sandbox.document, false, null, () => false,
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
