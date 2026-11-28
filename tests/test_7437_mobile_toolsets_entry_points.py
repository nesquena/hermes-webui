"""Behavioral tests for the mobile toolsets/MCP entry points (PR #7437, issue #1431).

The composer footer collapses in stages (_fitComposerFooter): full labels ->
`.cf-icons` -> `.cf-icons.cf-burger`. The toolsets picker has a different entry
point in each collapsed stage:

  * `.cf-icons`  — the footer chip, rendered as a 44px icon
  * `.cf-burger` — the chip is hidden; the mobile config panel action drives it

These tests drive the real `toggleToolsetsDropdown()` / `closeToolsetsDropdown()`
/ `_positionToolsetsDropdown()` sources from ui.js inside a Node VM against a
minimal DOM stub, asserting observable behavior rather than source strings:

  1. the picker opens from BOTH entry points,
  2. `aria-expanded` tracks the dropdown on whichever trigger opened it,
  3. the picker escapes the footer's containing block with viewport-relative
     geometry, in every collapsed stage and at tablet widths too,
  4. tapping the mobile action is not treated as an outside click, while a
     genuine outside click still closes.

Each assertion fails against sources predating the fix it covers: the toggle
gated on the chip's `offsetParent` (dead action in burger mode); the dropdown
stayed inside `.composer-footer`, whose `container-type: inline-size` makes it
the containing block for `position: fixed`; the collapse stage was inferred from
a width media query even though `_fitComposerFooter()` picks it by available
space; and the outside-click handler did not know the mobile action existed.
"""
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

NODE = shutil.which("node")
REPO = Path(__file__).parent.parent
# Overridable so the suite can be pointed at a pre-fix ui.js to confirm these
# tests actually fail before the change (AGENTS.md: "A test must fail before
# your fix and pass after it").
UI_JS_PATH = Path(os.environ.get("HERMES_TEST_UI_JS", REPO / "static" / "ui.js"))

pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")


def _slice_balanced(src: str, start: int) -> str:
    """Return src[start:] up to and including the brace-balanced block."""
    depth = 0
    for i in range(src.index("{", start), len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[start:i + 1]
    raise AssertionError("unbalanced braces")


def _function(src: str, name: str) -> str:
    m = re.search(r"^function %s\s*\(" % re.escape(name), src, re.M)
    assert m, f"{name}() not found in {UI_JS_PATH}"
    return _slice_balanced(src, m.start())


def _outside_click_handler(src: str) -> str:
    """Body of the document click listener that closes the toolsets dropdown."""
    marker = "// Click-outside handler for toolsets dropdown"
    i = src.find(marker)
    assert i != -1, "click-outside handler comment not found"
    j = src.index("function", i)
    return _slice_balanced(src, j)


def _run(stage: str, click_target: str | None = None, viewport_width: int = 390) -> dict:
    """Drive the real toggle/close sources in a given collapse stage.

    stage: "icons"  -> chip rendered, mobile action hidden
           "burger" -> chip hidden, mobile action rendered
    click_target: id to synthesize an outside-click against, or None to skip.
    """
    src = UI_JS_PATH.read_text(encoding="utf-8")
    payload = {
        "stage": stage,
        "clickTarget": click_target,
        "viewportWidth": viewport_width,
        "sources": "\n".join([
            _function(src, "_activeToolsetsTrigger") if "_activeToolsetsTrigger" in src else "",
            _function(src, "_restoreToolsetsDropdownHome") if "_restoreToolsetsDropdownHome" in src else "",
            "let _toolsetsDropdownHome = null;" if "_toolsetsDropdownHome" in src else "",
            "let _toolsetsOpenGeneration = 0;" if "_toolsetsOpenGeneration" in src else "",
            _function(src, "_positionToolsetsDropdown"),
            _function(src, "toggleToolsetsDropdown"),
            _function(src, "closeToolsetsDropdown"),
        ]),
        "outsideHandler": _outside_click_handler(src),
    }
    js = "const params = " + json.dumps(payload) + ";\n" + r"""
const stage = params.stage;

function makeEl(id, rendered) {
  const cls = new Set();
  return {
    id,
    // offsetParent === null is how the sources detect "hidden by CSS".
    offsetParent: rendered ? {} : null,
    // Present so the pre-fix anchoring path runs to completion. Without these
    // the icons-stage case would throw inside _positionToolsetsDropdown() and
    // "fail" for a stub reason rather than a behavioral one — that stage worked
    // before this change and its test must keep passing against both versions.
    getBoundingClientRect: () => ({ left: 44, right: 88, width: 44, height: 44 }),
    offsetWidth: 300,
    offsetHeight: 240,
    style: {},
    _attrs: {},
    classList: {
      add: (c) => cls.add(c),
      remove: (c) => cls.delete(c),
      contains: (c) => cls.has(c),
    },
    setAttribute(k, v) { this._attrs[k] = v; },
    getAttribute(k) { return this._attrs[k]; },
  };
}

const dd = makeEl('composerToolsetsDropdown', true);
const originalParent = { _kids: [], insertBefore(el) { this._kids.push(el); el.parentNode = this; },
                         appendChild(el) { this._kids.push(el); el.parentNode = this; } };
dd.parentNode = originalParent;
dd.nextSibling = null;
dd.scrollHeight = 240;
const chip = makeEl('composerToolsetsChip', stage === 'icons' || stage === 'desktop');
const action = makeEl('composerMobileToolsetsAction', stage === 'burger');
const els = {
  composerToolsetsDropdown: dd,
  composerToolsetsChip: chip,
  composerMobileToolsetsAction: action,
  composerMobileConfigBtn: makeEl('composerMobileConfigBtn', stage === 'burger'),
  toolsetsInput: null,
  toolsetsDropdownState: null,
};
const $ = (id) => els[id] || null;

// The footer exists in both collapsed stages; the sheet is fixed-positioned
// there, which is exactly what the positioning routine must respect.
const footerCls = new Set(
  params.stage === 'burger' ? ['cf-icons', 'cf-burger']
  : params.stage === 'desktop' ? []
  : ['cf-icons']);
const footer = {
  getBoundingClientRect: () => ({ left: 0, top: 700, bottom: 800 }),
  clientWidth: params.viewportWidth,
  classList: { contains: (c) => footerCls.has(c), add: (c) => footerCls.add(c), remove: (c) => footerCls.delete(c) },
};
const body = { _children: [], appendChild(el) { this._children.push(el); el.parentNode = body; } };
const document = {
  body,
  querySelector: (sel) => (sel === '.composer-footer' ? footer : null),
};
// Phone viewport: the reparenting path is what we want to exercise.
const window = {
  // Honest media query: only true when the viewport really is <= 640px. A
  // tablet run must therefore reach the floating path via the stage classes.
  matchMedia: (q) => ({ matches: params.viewportWidth <= 640 }),
  visualViewport: { width: params.viewportWidth, height: 800, offsetTop: 0, offsetLeft: 0 },
  innerWidth: params.viewportWidth,
  innerHeight: 800,
};
const getComputedStyle = () => ({ position: 'fixed' });

// Collaborators the toggle calls into; irrelevant to the behavior under test.
const closeProfileDropdown = () => {};
const closeWsDropdown = () => {};
const closeModelDropdown = () => {};
const closeReasoningDropdown = () => {};
const _syncToolsetsChip = () => {};
const _populateToolsetsDropdown = () => {};
const _renderToolsetsPresetSections = () => {};
const _loadToolsetsCatalog = () => ({ then: () => {} });
const _applySessionToolsets = () => {};
const showToast = () => {};
const t = (k) => k;

const runner = new Function(
  '$', 'document', 'window', 'getComputedStyle', 'setTimeout',
  'closeProfileDropdown', 'closeWsDropdown', 'closeModelDropdown',
  'closeReasoningDropdown', '_syncToolsetsChip', '_populateToolsetsDropdown',
  '_renderToolsetsPresetSections', '_loadToolsetsCatalog',
  '_applySessionToolsets', 'showToast', 't',
  params.sources + '\nreturn {toggleToolsetsDropdown, closeToolsetsDropdown, outside: ' + params.outsideHandler + '};'
);
const api = runner(
  $, document, window, getComputedStyle, () => {},
  closeProfileDropdown, closeWsDropdown, closeModelDropdown,
  closeReasoningDropdown, _syncToolsetsChip, _populateToolsetsDropdown,
  _renderToolsetsPresetSections, _loadToolsetsCatalog,
  _applySessionToolsets, showToast, t
);

// Seed a stale inline offset the way the pre-fix positioning routine would.
dd.style.left = '137px';

api.toggleToolsetsDropdown();

const openedBy = chip.classList.contains('active') ? 'chip'
  : action.classList.contains('active') ? 'action' : null;

let closedByOutsideClick = null;
if (params.clickTarget) {
  const target = { closest: (sel) => (sel === '#' + params.clickTarget ? {} : null) };
  api.outside({ target });
  closedByOutsideClick = !dd.classList.contains('open');
}

console.log(JSON.stringify({
  open: dd.classList.contains('open'),
  openedBy,
  reparentedToBody: dd.parentNode === body,
  floating: dd.classList.contains('composer-toolsets-dropdown--floating'),
  inlineLeft: dd.style.left,
  chipAria: chip.getAttribute('aria-expanded') || null,
  actionAria: action.getAttribute('aria-expanded') || null,
  closedByOutsideClick,
}));
"""
    r = subprocess.run([NODE, "-e", js], capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        raise RuntimeError(f"node failed: {r.stderr}")
    return json.loads(r.stdout.strip().splitlines()[-1])


class TestToolsetsEntryPoints:
    def test_opens_from_footer_chip_in_icons_stage(self):
        """`.cf-icons`: the icon chip opens the picker (pre-existing behavior)."""
        out = _run("icons")
        assert out["open"] is True, f"picker must open from the chip; got {out}"
        assert out["openedBy"] == "chip", f"chip should be the active trigger; got {out}"

    def test_opens_from_mobile_action_in_burger_stage(self):
        """`.cf-burger`: the panel action is the ONLY entry point and must work.

        Fails pre-fix: the toggle returned early on `chip.offsetParent === null`,
        which is exactly the state the chip is in during this stage.
        """
        out = _run("burger")
        assert out["open"] is True, (
            f"picker must open from the mobile config panel action; got {out}"
        )
        assert out["openedBy"] == "action", (
            f"the mobile action should be the active trigger; got {out}"
        )

    def test_aria_expanded_tracks_the_trigger_that_opened_it(self):
        """Whichever trigger opened the dropdown reports aria-expanded=true."""
        assert _run("icons")["chipAria"] == "true"
        assert _run("burger")["actionAria"] == "true"

    def test_phone_dropdown_escapes_the_footer_containing_block(self):
        """`.composer-footer` sets container-type:inline-size, which makes it the
        containing block for `position: fixed` descendants — a fixed dropdown left
        inside it resolves against the FOOTER, not the viewport, and lands below
        the fold (#6080). The picker must therefore be reparented to <body> and
        given viewport-relative coordinates, the same idiom as the model picker.

        Fails pre-fix: the dropdown stayed inside the footer and the routine wrote
        a footer-relative inline `left` over it.
        """
        for stage in ("icons", "burger"):
            out = _run(stage)
            assert out["reparentedToBody"] is True, (
                f"picker must escape the footer containing block in {stage}; got {out}"
            )
            assert out["floating"] is True, (
                f"picker must carry the floating modifier in {stage}; got {out}"
            )
            # Viewport-relative geometry, not a footer offset.
            assert out["inlineLeft"] == "8px", (
                f"left must be clamped to the viewport margin in {stage}; got {out}"
            )

    def test_tapping_the_mobile_action_is_not_an_outside_click(self):
        """The lifecycle must recognise the new trigger, or it closes instantly.

        Fails pre-fix: the handler only knew `#composerToolsetsChip` and
        `#composerToolsetsDropdown`, so the action counted as "outside".
        """
        out = _run("burger", click_target="composerMobileToolsetsAction")
        assert out["closedByOutsideClick"] is False, (
            f"clicking the mobile action must not close the picker; got {out}"
        )

    def test_outside_click_elsewhere_still_closes(self):
        """The guard must not become a blanket "never close"."""
        out = _run("burger", click_target="someUnrelatedThing")
        assert out["closedByOutsideClick"] is True, (
            f"a genuine outside click must still close the picker; got {out}"
        )

    def test_collapsed_tablet_still_escapes_the_footer(self):
        """Collapse is fit-based, so `.cf-icons` / `.cf-burger` occur above 640px.

        Keying the floating path to a width media query left collapsed layouts
        between 641px and ~900px on the anchored path, inside `.composer-left`
        whose hidden vertical overflow clips the upward-opening picker — and in
        `.cf-burger` the hidden chip made that path close the dropdown outright.
        The strategy must follow the stage, not the viewport width.
        """
        for stage in ("icons", "burger"):
            out = _run(stage, viewport_width=820)
            assert out["open"] is True, (
                f"picker must open in a collapsed {stage} tablet layout; got {out}"
            )
            assert out["reparentedToBody"] is True, (
                f"collapsed {stage} at 820px must still escape the footer; got {out}"
            )
            assert out["floating"] is True, (
                f"collapsed {stage} at 820px must carry the floating modifier; got {out}"
            )

    def test_uncollapsed_desktop_keeps_the_anchored_path(self):
        """The wide footer must behave exactly as it did on master.

        Everything this PR adds is scoped to the collapsed stages; an
        uncollapsed footer keeps the dropdown as an absolutely positioned
        `.composer-footer` child with a footer-relative inline `left`. Guards
        against the floating path leaking upward into desktop, which would
        change a surface this PR has no business touching.
        """
        out = _run("desktop", viewport_width=1440)
        assert out["open"] is True, f"desktop picker must still open; got {out}"
        assert out["openedBy"] == "chip", f"desktop opens from the chip; got {out}"
        assert out["reparentedToBody"] is False, (
            f"desktop must NOT reparent the dropdown to <body>; got {out}"
        )
        assert out["floating"] is False, (
            f"desktop must not carry the floating modifier; got {out}"
        )
        # Footer-relative offset, the master behaviour: chip.left 44 - footer.left 0.
        assert out["inlineLeft"] == "44px", (
            f"desktop must keep the anchored footer-relative offset; got {out}"
        )


# ── Panel / sheet lifecycle ──────────────────────────────────────────────────
#
# In `.cf-burger` the sheet is opened from an action that lives INSIDE the
# mobile config panel, but the sheet itself is reparented to <body>. The two are
# therefore no longer DOM relatives, and each one's dismiss logic has to know
# about the other. These tests run the real slice of ui.js that owns the panel
# lifecycle — `_syncMobileComposerConfigButton` through the toolsets Escape
# handler — with `document.addEventListener` recording every listener, then
# dispatch synthetic events at them.


def _panel_slice(src: str) -> str:
    start = src.index("function _syncMobileComposerConfigButton")
    end = src.index("window.addEventListener('resize',function(){", start)
    return src[start:end]


def _run_lifecycle(scenario: str) -> dict:
    src = UI_JS_PATH.read_text(encoding="utf-8")
    payload = {"scenario": scenario, "slice": _panel_slice(src)}
    js = "const params = " + json.dumps(payload) + ";\n" + r"""
function makeEl(id) {
  const cls = new Set();
  const attrs = {};
  return {
    id,
    classList: {
      add: (c) => cls.add(c), remove: (c) => cls.delete(c),
      contains: (c) => cls.has(c), toggle: (c, on) => (on ? cls.add(c) : cls.delete(c)),
    },
    setAttribute(k, v) { attrs[k] = v; }, getAttribute(k) { return attrs[k]; },
    focus() { focused = id; },
  };
}
let focused = null;
const panel = makeEl('composerMobileConfigPanel');
const btn = makeEl('composerMobileConfigBtn');
const dd = makeEl('composerToolsetsDropdown');
const action = makeEl('composerMobileToolsetsAction');
const els = {
  composerMobileConfigPanel: panel, composerMobileConfigBtn: btn,
  composerToolsetsDropdown: dd, composerMobileToolsetsAction: action,
};
const $ = (id) => els[id] || null;

const listeners = {};
const document = {
  addEventListener: (type, fn) => (listeners[type] = listeners[type] || []).push(fn),
  getElementById: (id) => els[id] || null,
};
const window = {};

let toolsetsClosed = 0;
const closeToolsetsDropdown = () => { toolsetsClosed++; dd.classList.remove('open'); };
const _activeToolsetsTrigger = () => action;
const noop = () => {};

// The slice's function declarations are scoped to this Function body; return
// the one a scenario calls directly (the listeners already close over it).
const api = new Function(
  '$', 'document', 'window', 'closeToolsetsDropdown', '_activeToolsetsTrigger',
  'closeWsDropdown', 'closeModelDropdown', 'closeReasoningDropdown', 'closeProfileDropdown',
  params.slice + '\nreturn { closeMobileComposerConfig };'
)($, document, window, closeToolsetsDropdown, _activeToolsetsTrigger, noop, noop, noop, noop);

// A target whose `.closest(sel)` matches only the given container ids.
const targetIn = (...ids) => ({ closest: (sel) => (ids.some((i) => sel === '#' + i) ? {} : null) });
const fire = (type, evt) => (listeners[type] || []).forEach((fn) => fn(evt));
const keyEvt = (key) => ({ key, preventDefault() {} });

// Both surfaces open, as they are after tapping the Toolsets action in burger mode.
panel.classList.add('open');
dd.classList.add('open');

if (params.scenario === 'click-inside-sheet') {
  fire('click', { target: targetIn('composerToolsetsDropdown') });
} else if (params.scenario === 'escape-with-panel') {
  fire('keydown', keyEvt('Escape'));
} else if (params.scenario === 'escape-without-panel') {
  panel.classList.remove('open');   // .cf-icons: no panel involved
  fire('keydown', keyEvt('Escape'));
} else if (params.scenario === 'panel-closed-programmatically') {
  // What the desktop resize handler does. Must NOT reach the toolsets picker,
  // or an anchored desktop picker would close on every window resize.
  api.closeMobileComposerConfig();
}

console.log(JSON.stringify({
  panelOpen: panel.classList.contains('open'),
  sheetOpen: dd.classList.contains('open'),
  burgerExpanded: btn.getAttribute('aria-expanded') || null,
  toolsetsClosed,
  focused,
}));
"""
    r = subprocess.run([NODE, "-e", js], capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        raise RuntimeError(f"node failed: {r.stderr}")
    return json.loads(r.stdout.strip().splitlines()[-1])


class TestNoEntryPointGuard:
    def test_does_not_open_when_no_entry_point_is_rendered(self):
        """The guard test_issue1431 used to pin as a source string, proven.

        toggleToolsetsDropdown() is global, so it can be called while every
        trigger is hidden by responsive CSS. It must not open then: with no
        rendered anchor the sheet would be positioned against a 0x0 rect.
        """
        out = _run("hidden")
        assert out["open"] is False, (
            f"picker must not open with no rendered entry point; got {out}"
        )
        assert out["openedBy"] is None, f"nothing may be marked active; got {out}"


class TestPanelSheetLifecycle:
    def test_clicking_inside_the_sheet_keeps_the_panel_open(self):
        """The sheet is reparented to <body>, so it is no longer inside the panel.

        Without whitelisting it, a click on a server checkbox counted as an
        outside click for the panel, which tore the panel down behind the still
        open sheet — leaving its in-panel anchor at 0x0 and the burger button
        reporting aria-expanded="false" over a visibly open popup.
        """
        out = _run_lifecycle("click-inside-sheet")
        assert out["panelOpen"] is True, f"panel must survive a click in the sheet; got {out}"
        assert out["sheetOpen"] is True, f"the sheet must stay open too; got {out}"
        assert out["burgerExpanded"] != "false", (
            f"burger must not claim collapsed while its popup is open; got {out}"
        )

    def test_escape_with_the_panel_open_closes_both(self):
        """Escape dismissed the panel but left the sheet anchored to a hidden action."""
        out = _run_lifecycle("escape-with-panel")
        assert out["panelOpen"] is False, f"Escape must close the panel; got {out}"
        assert out["sheetOpen"] is False, f"Escape must also close the sheet; got {out}"

    def test_escape_from_the_panel_does_not_drop_keyboard_focus(self):
        """Both Escape handlers fire on one keypress, in registration order.

        The panel's handler runs first and closes the sheet too, so the
        toolsets handler then sees it already closed and returns before its
        focus restoration — leaving keyboard users on <body>. The trigger that
        opened the sheet sits inside the panel that just closed and can no
        longer take focus, so focus must land on the burger button, the visible
        control that owns both surfaces.
        """
        out = _run_lifecycle("escape-with-panel")
        assert out["focused"] == "composerMobileConfigBtn", (
            f"focus must land on the burger button, not be dropped; got {out}"
        )

    def test_escape_closes_the_sheet_when_there_is_no_panel(self):
        """In `.cf-icons` there is no panel, and the sheet's only Escape binding
        lived on a text field the floating sheet hides — so nothing could take
        focus and Escape did nothing. Focus must return to the trigger."""
        out = _run_lifecycle("escape-without-panel")
        assert out["sheetOpen"] is False, f"Escape must close the sheet on its own; got {out}"
        assert out["focused"] == "composerMobileToolsetsAction", (
            f"focus must return to the trigger; got {out}"
        )

    def test_closing_the_panel_programmatically_leaves_the_picker_alone(self):
        """closeMobileComposerConfig() is also called by the desktop resize handler.

        Closing the toolsets picker from inside it would close an anchored
        desktop picker on every window resize — a desktop behaviour change this
        PR must not make. The panel's own dismiss paths close the sheet
        explicitly instead.
        """
        out = _run_lifecycle("panel-closed-programmatically")
        assert out["panelOpen"] is False, f"panel must close; got {out}"
        assert out["toolsetsClosed"] == 0, (
            f"closeMobileComposerConfig() must not reach the toolsets picker; got {out}"
        )


# ── Focus and resize ownership across the open lifetime ──────────────────────
#
# Everything above stubs the picker's renderer and catalog loader, so it cannot
# see what happens when they run LATE. These tests run the real toolsets region
# of ui.js - module state, renderer, catalog loader, and the document `change`
# and window `resize` listeners it registers - against a small DOM that keeps
# the one browser behaviour the defects live in: focus held by a node that gets
# detached falls back to <body>. Timers and the catalog request are driven by
# hand, so each test chooses the exact interleaving it is about.

_REGION_START = "// ── Session toolsets chip (#493)"
_REGION_END = "function _syncMobileComposerConfigButton"


def _toolsets_region(src: str) -> str:
    i = src.find(_REGION_START)
    j = src.find(_REGION_END, i)
    assert i != -1 and j != -1, "toolsets region markers not found in ui.js"
    return src[i:j]


_DOM_HARNESS = r"""
const params = __PARAMS__;
const docRoot = { tagName: 'HTML', children: [], parentNode: null, classList: { contains: () => false } };
let activeEl = null;
const docListeners = {};
const winListeners = {};

function matchesCompound(n, comp) {
  if (!n || !n.tagName || !n.classList) return false;
  const re = /([#.:]?)([\w-]+)/g;
  let m;
  while ((m = re.exec(comp))) {
    const kind = m[1], name = m[2];
    if (kind === '#') { if (n.id !== name) return false; }
    else if (kind === '.') { if (!n.classList.contains(name)) return false; }
    else if (kind === ':') { if (name !== 'checked' || !n.checked) return false; }
    else if (n.tagName !== name.toUpperCase()) return false;
  }
  return true;
}
function matches(n, sel) {
  return sel.split(',').some((s) => {
    const parts = s.trim().split(/\s+/);
    if (!matchesCompound(n, parts[parts.length - 1])) return false;
    let anc = n.parentNode;
    for (let i = parts.length - 2; i >= 0; i--) {
      while (anc && !matchesCompound(anc, parts[i])) anc = anc.parentNode;
      if (!anc) return false;
      anc = anc.parentNode;
    }
    return true;
  });
}
function queryAll(root, sel) {
  const out = [];
  (function walk(n) { (n.children || []).forEach((c) => { if (matches(c, sel)) out.push(c); walk(c); }); })(root);
  return out;
}
function isConnected(n) { while (n) { if (n === docRoot) return true; n = n.parentNode; } return false; }

function makeNode(tag) {
  const cls = new Set();
  const node = {
    tagName: String(tag).toUpperCase(),
    id: '', type: '', value: '', checked: false, placeholder: '',
    _text: '', style: {}, dataset: {}, children: [], parentNode: null,
    offsetParent: {}, scrollHeight: 240, offsetHeight: 240, offsetWidth: 300, clientWidth: 390,
    _attrs: {}, onkeydown: null, oninput: null,
    classList: {
      add: (c) => cls.add(c), remove: (c) => cls.delete(c), contains: (c) => cls.has(c),
      toggle: (c, f) => { const on = f === undefined ? !cls.has(c) : !!f; if (on) cls.add(c); else cls.delete(c); return on; },
    },
    get textContent() { return this._text + this.children.map((c) => c.textContent).join(''); },
    set textContent(v) { this._detachAll(); this._text = String(v); },
    get innerHTML() { return ''; },
    set innerHTML(v) {
      if (v !== '') throw new Error('stub DOM supports innerHTML = "" only');
      this._detachAll(); this._text = '';
    },
    _detachAll() { this.children.forEach((c) => { c.parentNode = null; }); this.children = []; },
    appendChild(c) { if (c.parentNode) c.parentNode.removeChild(c); c.parentNode = this; this.children.push(c); return c; },
    insertBefore(c, ref) {
      if (c.parentNode) c.parentNode.removeChild(c);
      c.parentNode = this;
      const i = ref ? this.children.indexOf(ref) : -1;
      if (i < 0) this.children.push(c); else this.children.splice(i, 0, c);
      return c;
    },
    removeChild(c) { const i = this.children.indexOf(c); if (i >= 0) this.children.splice(i, 1); c.parentNode = null; return c; },
    get nextSibling() { const p = this.parentNode; if (!p) return null; return p.children[p.children.indexOf(this) + 1] || null; },
    get isConnected() { return isConnected(this); },
    contains(o) { let n = o; while (n) { if (n === this) return true; n = n.parentNode; } return false; },
    setAttribute(k, v) { this._attrs[k] = String(v); },
    getAttribute(k) { return k in this._attrs ? this._attrs[k] : null; },
    // Browser semantics the defects depend on: focusing a detached node is a
    // no-op, and activeElement falls back to <body> once the holder detaches.
    focus() { if (isConnected(this)) activeEl = this; },
    getBoundingClientRect() { return { left: 44, right: 88, top: 740, bottom: 784, width: 44, height: 44 }; },
    querySelector(sel) { return queryAll(this, sel)[0] || null; },
    querySelectorAll(sel) { return queryAll(this, sel); },
    closest(sel) { let n = this; while (n && n !== docRoot) { if (matches(n, sel)) return n; n = n.parentNode; } return null; },
  };
  Object.defineProperty(node, 'className', {
    get() { return Array.from(cls).join(' '); },
    set(v) { cls.clear(); String(v).split(/\s+/).filter(Boolean).forEach((c) => cls.add(c)); },
  });
  return node;
}
function el(tag, id, className, parent) {
  const n = makeNode(tag);
  if (id) n.id = id;
  if (className) n.className = className;
  if (parent) parent.appendChild(n);
  return n;
}

const body = el('body', '', '', null);
docRoot.children.push(body); body.parentNode = docRoot;
const composer = el('textarea', 'msg', '', body);
const footer = el('div', '', 'composer-footer', body);
footer.getBoundingClientRect = () => ({ left: 0, top: 700, bottom: 800, right: 390 });
const chip = el('button', 'composerToolsetsChip', '', footer);
const dd = el('div', 'composerToolsetsDropdown', 'composer-toolsets-dropdown', footer);
el('div', 'toolsetsDropdownDesc', 'toolsets-dropdown-desc', dd);
el('div', 'toolsetsDropdownState', 'toolsets-dropdown-state', dd);
const inputRow = el('div', '', 'toolsets-dropdown-input-row', dd);
const input = el('input', 'toolsetsInput', 'toolsets-input', inputRow);
const actions = el('div', '', 'toolsets-dropdown-actions', dd);
el('button', 'toolsetsApplyBtn', 'toolsets-action-btn toolsets-apply-btn', actions);
el('button', 'toolsetsClearBtn', 'toolsets-action-btn toolsets-clear-btn', actions);
const panel = el('div', 'composerMobileConfigPanel', '', body);
const action = el('button', 'composerMobileToolsetsAction', '', panel);
el('button', 'composerMobileConfigBtn', '', body);

function setStage(stage) {
  footer.classList.remove('cf-icons'); footer.classList.remove('cf-burger');
  if (stage === 'icons' || stage === 'burger') footer.classList.add('cf-icons');
  if (stage === 'burger') { footer.classList.add('cf-burger'); panel.classList.add('open'); }
  else panel.classList.remove('open');
  chip.offsetParent = (stage === 'icons' || stage === 'desktop') ? {} : null;
  action.offsetParent = stage === 'burger' ? {} : null;
}
setStage(params.stage);

const document = {
  body, documentElement: docRoot,
  get activeElement() { return activeEl && isConnected(activeEl) ? activeEl : body; },
  createElement: (tag) => makeNode(tag),
  createTextNode: (s) => { const n = makeNode('#text'); n._text = String(s); return n; },
  getElementById: (id) => queryAll(docRoot, '#' + id)[0] || null,
  querySelector: (sel) => queryAll(docRoot, sel)[0] || null,
  querySelectorAll: (sel) => queryAll(docRoot, sel),
  addEventListener: (type, fn) => { (docListeners[type] = docListeners[type] || []).push(fn); },
};
const window = {
  visualViewport: { width: 390, height: 800, offsetTop: 0, offsetLeft: 0 },
  innerWidth: 390, innerHeight: 800,
  matchMedia: () => ({ matches: true }),
  addEventListener: (type, fn) => { (winListeners[type] = winListeners[type] || []).push(fn); },
};
const $ = (id) => document.getElementById(id);

const timers = [];
const fakeSetTimeout = (fn) => { timers.push(fn); return timers.length; };
function flushTimers() { while (timers.length) timers.shift()(); }

const requests = [];
const api = (url) => new Promise((resolve, reject) => requests.push({ url, resolve, reject }));
const CATALOG = { servers: [{ name: 'alpha' }, { name: 'beta' }] };
async function settleCatalog() {
  while (requests.length) requests.shift().resolve(CATALOG);
  for (let i = 0; i < 5; i++) await new Promise((r) => setImmediate(r));
}

const runner = new Function(
  '$', 'document', 'window', 'api', 't', 'S', 'showToast', 'setTimeout', 'closeModelDropdown',
  params.region + '\nreturn { toggleToolsetsDropdown, closeToolsetsDropdown, invalidateToolsetsCatalog };'
);
const ui = runner($, document, window, api, (k) => k, { session: null }, () => {}, fakeSetTimeout, () => {});

function focusReport() {
  const a = document.activeElement;
  return {
    open: dd.classList.contains('open'),
    activeId: a.id || null,
    activeTag: a.tagName,
    activeValue: a.value || null,
    activeConnected: isConnected(a) && a !== body,
    activeInsideSheet: a !== body && dd.contains(a),
  };
}
function fire(target, type, extra) {
  (docListeners[type] || []).forEach((fn) => fn(Object.assign({ target }, extra || {})));
}
function resize() { (winListeners.resize || []).forEach((fn) => fn({})); }

(async () => {
  const s = params.scenario;
  let out = {};
  if (s === 'timer-then-catalog') {
    // The re-gate's exact order: open, let the 50 ms callback focus the
    // synchronously rendered defaults button, then let the catalog settle.
    ui.toggleToolsetsDropdown();
    flushTimers();
    const before = focusReport();
    await settleCatalog();
    out = { before, after: focusReport() };
  } else if (s === 'stale-timer-after-close') {
    // Dismissed inside the 50 ms window, then the user moves on to typing.
    ui.toggleToolsetsDropdown();
    ui.closeToolsetsDropdown();
    composer.focus();
    flushTimers();
    await settleCatalog();
    out = focusReport();
  } else if (s === 'reopen-before-settlement') {
    ui.toggleToolsetsDropdown();
    ui.closeToolsetsDropdown();
    ui.toggleToolsetsDropdown();
    flushTimers();
    await settleCatalog();
    out = focusReport();
  } else if (s === 'checkbox-toggle') {
    ui.invalidateToolsetsCatalog(CATALOG);
    ui.toggleToolsetsDropdown();
    await settleCatalog();
    flushTimers();
    const beta = dd.querySelectorAll('.toolsets-server-checkbox').find((c) => c.value === 'beta');
    beta.focus();
    beta.checked = true;
    fire(beta, 'change');
    out = Object.assign(focusReport(), {
      inputValue: input.value,
      betaStillChecked: !!(document.activeElement && document.activeElement.checked),
    });
  } else if (s.startsWith('resize-')) {
    ui.invalidateToolsetsCatalog(CATALOG);
    ui.toggleToolsetsDropdown();
    const openedFloating = dd.classList.contains('composer-toolsets-dropdown--floating');
    if (s === 'resize-none-rendered') { chip.offsetParent = null; action.offsetParent = null; }
    dd.style.top = '';
    resize();
    out = {
      openedFloating,
      open: dd.classList.contains('open'),
      repositioned: dd.style.top !== '',
    };
  }
  console.log(JSON.stringify(out));
})().catch((e) => { console.error(e && e.stack || e); process.exit(1); });
"""


def _run_region(scenario: str, stage: str = "burger") -> dict:
    src = UI_JS_PATH.read_text(encoding="utf-8")
    params = {"scenario": scenario, "stage": stage, "region": _toolsets_region(src)}
    js = _DOM_HARNESS.replace("__PARAMS__", json.dumps(params))
    r = subprocess.run([NODE, "-e", js], capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        raise RuntimeError(f"node failed: {r.stderr}")
    return json.loads(r.stdout.strip().splitlines()[-1])


class TestFocusAcrossTheOpenLifetime:
    def test_focus_survives_the_catalog_settling_after_the_open_timer(self):
        """The maintainer's re-gate order: timer first, catalog second.

        The 50 ms callback focuses the synchronously rendered profile-defaults
        button; the catalog continuation then re-rendered the whole section with
        innerHTML = '', detaching that exact node and dropping focus to <body>
        while the sheet stayed open.
        """
        out = _run_region("timer-then-catalog")
        assert out["before"]["activeInsideSheet"], f"the open timer must focus inside the sheet; got {out}"
        after = out["after"]
        assert after["open"] is True, f"settling the catalog must not close the sheet; got {out}"
        assert after["activeInsideSheet"] and after["activeConnected"], (
            f"focus must still be on a live control inside the open sheet after the "
            f"catalog settles, not fall back to <body>; got {out}"
        )

    def test_a_stale_open_timer_cannot_reclaim_focus_after_close(self):
        """Dismiss inside the 50 ms window and start typing elsewhere.

        The open's timer used to run regardless: with the sheet closed it took the
        anchored branch and focused the hidden free-text field, yanking focus out
        of the composer the user had just moved to.
        """
        out = _run_region("stale-timer-after-close")
        assert out["open"] is False, f"the sheet was closed; got {out}"
        assert out["activeId"] == "msg", (
            f"a timer belonging to a closed open must not move focus; got {out}"
        )

    def test_reopening_before_the_catalog_settles_keeps_focus_in_the_sheet(self):
        """Close and reopen while the first open's callbacks are still pending."""
        out = _run_region("reopen-before-settlement")
        assert out["open"] is True, f"the second open must stay open; got {out}"
        assert out["activeInsideSheet"] and out["activeConnected"], (
            f"neither open's late callbacks may leave focus outside the sheet; got {out}"
        )

    def test_toggling_a_server_checkbox_keeps_keyboard_focus_on_it(self):
        """Pre-existing on master, same mechanism: the `change` handler re-rendered
        every checkbox, so a keyboard user lost focus after each Space press."""
        out = _run_region("checkbox-toggle")
        assert out["inputValue"] == "beta", f"the selection must still reach the input; got {out}"
        assert out["activeValue"] == "beta" and out["activeConnected"], (
            f"focus must stay on the checkbox that was just toggled; got {out}"
        )
        assert out["betaStillChecked"] is True, f"and it must still read as checked; got {out}"


class TestResizeOwnership:
    def test_resize_keeps_the_sheet_when_only_the_burger_action_is_rendered(self):
        """In `.cf-burger` the chip is hidden by design and the panel action is
        the visible anchor. The chip-only resize rule closed a valid open sheet."""
        out = _run_region("resize-burger", stage="burger")
        assert out["openedFloating"] is True, f"precondition: floating sheet; got {out}"
        assert out["open"] is True, f"resize must not close a sheet that has an anchor; got {out}"
        assert out["repositioned"] is True, f"it must be repositioned instead; got {out}"

    def test_resize_keeps_the_sheet_when_the_chip_is_rendered(self):
        out = _run_region("resize-icons", stage="icons")
        assert out["open"] is True and out["repositioned"] is True, f"got {out}"

    def test_resize_closes_the_sheet_when_no_entry_point_is_rendered(self):
        out = _run_region("resize-none-rendered", stage="burger")
        assert out["open"] is False, f"with no anchor at all the sheet must close; got {out}"
