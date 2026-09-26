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
