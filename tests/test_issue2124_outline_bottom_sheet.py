"""Behavioural regression coverage for the conversation outline bottom sheet.

The outline was desktop-only: `_outlineAllowed()` rejected every viewport under
the 900px breakpoint, and the compact media query hard-hid the toggle and the
panel. The narrow-viewport form is now a bottom sheet — the same jump list,
re-shaped — and a bottom sheet with a dimming, pointer-blocking backdrop is a
modal surface. So the compact form must behave like one (dialog semantics,
focus in on open, Tab contained, Escape to close, focus back to the toggle)
and it must yield to an approval or clarification, which are time-sensitive
safety surfaces rendered underneath it.

Asserting that on source strings is what the previous revision of this change
did, and it can pass while every interaction above is broken. This file drives
the REAL `static/outline.js` in a jsdom-free DOM shim under Node (the same
harness style as `tests/test_issue4658_transparent_collapsed_preview.py`) and
observes open/close, Escape, Tab containment, focus restore, breakpoint
crossing, approval/clarification preemption and post-jump collapse.

Against the desktop-only implementation these observations fail: the toggle
does nothing below 900px, so the sheet never opens in the first place.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent.resolve()
OUTLINE_JS_PATH = REPO_ROOT / "static" / "outline.js"

NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")


_DRIVER = r"""
'use strict';
const fs = require('fs');
const vm = require('vm');

// ── Minimal DOM shim ────────────────────────────────────────────────────────
// Only what static/outline.js touches: ids, classes, attributes, hidden, focus,
// a keydown registry on document, a resize registry on window, matchMedia.
const REG = {};
let FOCUS_DISPATCHING = false;
let LAST_FOCUS_OPTS = null;
let VIEWPORT = 390;
const winListeners = {};

function matches(el, sel) {
  return String(sel).split(',').map(s => s.trim()).some(part => {
    if (!part) return false;
    if (part.charAt(0) === '.') return el._classes.has(part.slice(1));
    if (part === 'button') return el.tagName === 'BUTTON';
    if (part === 'textarea') return el.tagName === 'TEXTAREA';
    if (part === 'input') return el.tagName === 'INPUT';
    if (part === 'select') return el.tagName === 'SELECT';
    if (part === 'summary') return el.tagName === 'SUMMARY';
    if (part === 'a[href]') return el.tagName === 'A' && el.hasAttribute('href');
    if (part.indexOf('[tabindex') === 0) return el.hasAttribute('tabindex');
    return false;
  });
}

function El(tag) {
  const el = {
    tagName: String(tag || 'div').toUpperCase(),
    _id: '',
    _attrs: {},
    _classes: new Set(),
    children: [],
    parentNode: null,
    hidden: false,
    disabled: false,
    tabIndex: 0,
    dataset: {},
    textContent: '',
    offsetWidth: 0,
    listeners: {},
    style: { setProperty() {} },
  };
  el.classList = {
    add(c) { el._classes.add(c); },
    remove(c) { el._classes.delete(c); },
    contains(c) { return el._classes.has(c); },
    toggle(c, on) {
      const want = (on === undefined) ? !el._classes.has(c) : !!on;
      if (want) el._classes.add(c); else el._classes.delete(c);
    },
    get length() { return el._classes.size; },
  };
  Object.defineProperty(el, 'id', {
    get() { return el._id; },
    set(v) { el._id = String(v); REG[el._id] = el; },
  });
  Object.defineProperty(el, 'className', {
    get() { return Array.from(el._classes).join(' '); },
    set(v) { el._classes = new Set(String(v).split(/\s+/).filter(Boolean)); },
  });
  Object.defineProperty(el, 'innerHTML', {
    get() { return el._html || ''; },
    set(v) {
      el._html = String(v);
      // The outline writes its rows as markup. Re-create the <button> children
      // so querySelectorAll()/focus() see the same focusable list a browser
      // would, and so Tab containment is exercised over real entries.
      el.children = el.children.filter(c => c.tagName !== 'BUTTON');
      const re = /<button[\s\S]*?>/g;
      let m;
      while ((m = re.exec(el._html))) {
        const b = El('button');
        b.parentNode = el;
        el.children.push(b);
      }
    },
  });
  el.setAttribute = (k, v) => { el._attrs[k] = String(v); };
  el.getAttribute = (k) => (k in el._attrs ? el._attrs[k] : null);
  el.hasAttribute = (k) => k in el._attrs;
  el.removeAttribute = (k) => { delete el._attrs[k]; };
  el.addEventListener = (t, f) => { (el.listeners[t] = el.listeners[t] || []).push(f); };
  el.removeEventListener = (t, f) => {
    const l = el.listeners[t] || [];
    const i = l.indexOf(f);
    if (i >= 0) l.splice(i, 1);
  };
  el.appendChild = (c) => { c.parentNode = el; el.children.push(c); return c; };
  el.remove = () => {
    if (el.parentNode) {
      const i = el.parentNode.children.indexOf(el);
      if (i >= 0) el.parentNode.children.splice(i, 1);
    }
    if (el._id && REG[el._id] === el) delete REG[el._id];
  };
  el.querySelectorAll = (sel) => {
    const out = [];
    (function walk(node) {
      node.children.forEach((c) => { if (matches(c, sel)) out.push(c); walk(c); });
    })(el);
    return out;
  };
  el.querySelector = (sel) => el.querySelectorAll(sel)[0] || null;
  el.contains = (n) => { let p = n; while (p) { if (p === el) return true; p = p.parentNode; } return false; };
  el.focus = (opts) => {
    el._focusOpts = opts === undefined ? null : opts;
    LAST_FOCUS_OPTS = el._focusOpts;
    document.activeElement = el;
    // A real focus fires focusin up to document; outline.js guards containment
    // against focus stolen by the prompt poller.
    if (!FOCUS_DISPATCHING) {
      const hs = (document._listeners.focusin || []).slice();
      if (hs.length) {
        FOCUS_DISPATCHING = true;
        try { hs.forEach(r => r.fn({ target: el })); } finally { FOCUS_DISPATCHING = false; }
      }
    }
  };
  el.scrollIntoView = () => {};
  return el;
}

const document = {
  documentElement: El('html'),
  body: El('body'),
  activeElement: null,
  _listeners: {},
  // Listeners are recorded with their phase: outline.js binds its sheet keys in
  // the CAPTURE phase so Escape and the app chords are settled before the
  // global handlers act on them, and the shim must be able to tell them apart.
  addEventListener(t, f, opts) {
    const capture = opts === true || !!(opts && opts.capture);
    (this._listeners[t] = this._listeners[t] || []).push({ fn: f, capture });
  },
  removeEventListener(t, f, opts) {
    const capture = opts === true || !!(opts && opts.capture);
    const l = this._listeners[t] || [];
    const i = l.findIndex(r => r.fn === f && r.capture === capture);
    if (i >= 0) l.splice(i, 1);
  },
  createElement(tag) { return El(tag); },
  getElementById(id) { return REG[id] || null; },
  // The outline only reads .rightpanel / main.main for desktop positioning.
  querySelector() { return null; },
};
document.activeElement = document.body;

// ── Page markup the outline binds to ────────────────────────────────────────
const toggle = El('button');
toggle.id = 'outlineToggleBtn';
toggle.hidden = true;
document.body.appendChild(toggle);

const wrapper = El('div');
wrapper.id = 'outlinePanelWrapper';
wrapper.hidden = true;
wrapper.setAttribute('role', 'navigation');
wrapper.setAttribute('aria-label', 'Conversation outline');
const header = El('div');
header.className = 'outline-header';
const closeBtn = El('button');
closeBtn.className = 'outline-close-btn';
header.appendChild(closeBtn);
wrapper.appendChild(header);
const panel = El('div');
panel.id = 'outlinePanel';
wrapper.appendChild(panel);
document.body.appendChild(wrapper);

const approvalCard = El('div');
approvalCard.id = 'approvalCard';
approvalCard.hidden = true;
document.body.appendChild(approvalCard);

const clarifyCard = El('div');
clarifyCard.id = 'clarifyCard';
clarifyCard.hidden = true;
document.body.appendChild(clarifyCard);

for (let i = 0; i < 3; i++) {
  const row = El('div');
  row.id = 'msg-user-' + i;
  // The jump matches rows by absolute transcript index, not by id alone.
  row.dataset.sessionMsgIdx = String(i);
  document.body.appendChild(row);
}

// ── Globals static/outline.js expects from the app ──────────────────────────
const mediaLists = [];
globalThis.window = globalThis;
globalThis.document = document;
globalThis.matchMedia = (q) => {
  const isCompactQuery = /max-width:\s*900px/.test(String(q));
  const mql = {
    media: String(q),
    listeners: [],
    // Live answer, like a real MediaQueryList.
    get matches() { return isCompactQuery ? VIEWPORT <= 900 : true; },
    addEventListener(type, fn) { if (type === 'change') mql.listeners.push(fn); },
    removeEventListener(type, fn) {
      const i = mql.listeners.indexOf(fn);
      if (i >= 0) mql.listeners.splice(i, 1);
    },
  };
  mediaLists.push(mql);
  return mql;
};
globalThis.getComputedStyle = () => ({ display: 'block', visibility: 'visible' });
// The upstream jump re-arms scroll ownership inside requestAnimationFrame.
globalThis.requestAnimationFrame = (fn) => { fn(); return 1; };
globalThis.MutationObserver = class { constructor(cb) { this.cb = cb; } observe() {} disconnect() {} };
globalThis.ResizeObserver = class { constructor(cb) { this.cb = cb; } observe() {} disconnect() {} };
globalThis.addEventListener = (t, f) => { (winListeners[t] = winListeners[t] || []).push(f); };
globalThis.removeEventListener = (t, f) => {
  const l = winListeners[t] || [];
  const i = l.indexOf(f);
  if (i >= 0) l.splice(i, 1);
};
// Loader/index collaborators the upstream jump path (c4be210c0) relies on:
// a jump asks the shared loader for the full transcript instead of fetching a
// bare /api/session URL, and addresses rows by absolute transcript index.
globalThis._messageSessionIndexForRawIdx = (n) => n;
globalThis._messagesTruncated = false;
globalThis._ensureAllMessagesLoaded = () => Promise.resolve(false);
globalThis._cancelBottomSettle = () => {};
globalThis._beginMessageJumpScroll = () => {};
globalThis._currentMessageRenderWindowSize = () => 30;
globalThis._messageRenderableMessageCount = () => 3;
globalThis._messageRenderWindowSize = 30;
globalThis._showConversationOutline = true;
globalThis._currentPanel = 'chat';
globalThis.t = (k) => String(k);
globalThis.api = () => Promise.resolve({});
globalThis.renderMessages = function () {};
globalThis.S = {
  session: { session_id: 's1' },
  messages: [
    { role: 'user', content: 'hello there' },
    { role: 'assistant', content: 'hi' },
    { role: 'user', content: 'second question' },
  ],
  busy: false,
};

// Prompt entry points, mirroring what messages.js does when one opens: unhide
// the card, then add .visible. outline.js is expected to wrap these.
function openCard(card) { card.hidden = false; card.classList.add('visible'); }
globalThis.showApprovalCard = function () { openCard(approvalCard); };
globalThis.showClarifyCard = function () { openCard(clarifyCard); };

// The app-level keydown handler boot.js installs on document (bubble phase).
// Recorded so the driver can prove the sheet settles its keys in the CAPTURE
// phase and that Escape never reaches the global handler (which would clear the
// sidebar search behind the sheet).
const appKeyEvents = [];
document.addEventListener('keydown', (ev) => { appKeyEvents.push(ev.key); });

// ── Load the real module and let it initialise ──────────────────────────────
vm.runInThisContext(fs.readFileSync(process.argv[2], 'utf8'), { filename: 'outline.js' });
(document._listeners.DOMContentLoaded || []).slice().forEach((r) => r.fn());

// ── Helpers ─────────────────────────────────────────────────────────────────
const tick = () => new Promise((r) => setTimeout(r, 0));

function keydown(key, extra) {
  if (extra === true) extra = { shiftKey: true };
  extra = extra || {};
  const ev = {
    key,
    shiftKey: !!extra.shiftKey,
    ctrlKey: !!extra.ctrlKey,
    metaKey: !!extra.metaKey,
    altKey: !!extra.altKey,
    defaultPrevented: false,
    propagationStopped: false,
    preventDefault() { this.defaultPrevented = true; },
    stopPropagation() { this.propagationStopped = true; },
    stopImmediatePropagation() { this.propagationStopped = true; },
  };
  const all = (document._listeners.keydown || []).slice();
  const ordered = all.filter((r) => r.capture).concat(all.filter((r) => !r.capture));
  for (const r of ordered) {
    r.fn(ev);
    if (ev.propagationStopped) break;
  }
  return ev;
}
function mousedownEl(el) {
  const ev = { defaultPrevented: false, preventDefault() { this.defaultPrevented = true; } };
  if (el) (el.listeners.mousedown || []).slice().forEach((f) => f(ev));
  return ev;
}
function clickEl(el) { if (el) (el.listeners.click || []).slice().forEach((f) => f({ preventDefault() {} })); }
function focusEl(el) { if (el && typeof el.focus === 'function') el.focus(); }
function resize(width) {
  VIEWPORT = width;
  (winListeners.resize || []).slice().forEach((f) => f());
}
// A breakpoint change with NO window resize event — devtools responsive mode
// and some orientation changes behave this way.
function mediaChange(width) {
  VIEWPORT = width;
  mediaLists.forEach((mql) => (mql.listeners || []).slice().forEach((f) => f(mql)));
}
function sheetFocusables() {
  return wrapper.querySelectorAll('a[href], button, textarea, input, select, summary, [tabindex]:not([tabindex="-1"])');
}
function label(el) {
  if (!el) return null;
  if (el === toggle) return 'outlineToggleBtn';
  if (el === closeBtn) return 'outlineCloseBtn';
  if (el === document.body) return 'body';
  const i = sheetFocusables().indexOf(el);
  if (i >= 0) return 'sheet-focusable-' + i;
  return el.tagName;
}
function snap(extra) {
  return Object.assign({
    open: !wrapper.hidden,
    backdrop: !!document.getElementById('outlineBackdrop'),
    role: wrapper.getAttribute('role'),
    ariaModal: wrapper.getAttribute('aria-modal'),
    // Only the sheet's own capture-phase handler counts; the app-level bubble
    // handler registered by the driver stays on document for the whole run.
    keydownHandlers: (document._listeners.keydown || []).filter((r) => r.capture).length,
    focus: label(document.activeElement),
    entryCount: panel.querySelectorAll('button').length,
  }, extra || {});
}

(async function main() {
  const out = {};

  // A. Compact: the toggle opens the sheet, chromed as a modal dialog.
  resize(390);
  out.toggleVisibleOnCompact = !toggle.hidden;
  window.toggleOutlinePanel();
  await tick();
  out.compactOpen = snap();

  // B. Tab containment over the real rendered entries.
  const entries = sheetFocusables().slice(1);
  focusEl(entries[entries.length - 1]);
  keydown('Tab');
  out.tabWrapsForward = label(document.activeElement);
  focusEl(closeBtn);
  keydown('Tab', true);
  out.tabWrapsBackward = label(document.activeElement);

  // C. Escape closes and hands focus back to the toggle.
  const esc = keydown('Escape');
  out.escapePrevented = esc.defaultPrevented === true;
  out.afterEscape = snap();

  // D. Backdrop tap closes it too, without leaving a stray layer behind.
  window.toggleOutlinePanel();
  await tick();
  clickEl(document.getElementById('outlineBackdrop'));
  out.afterBackdropTap = snap();

  // E. Breakpoint crossing while open: compact chrome in, desktop out, and back.
  window.toggleOutlinePanel();
  await tick();
  resize(1280);
  out.wideWhileOpen = snap();
  const wideEsc = keydown('Escape');
  out.wideEscapeKeepsOpen = snap({ escapePrevented: wideEsc.defaultPrevented === true }).open;
  window._outlineJump(0);
  out.wideJumpKeepsOpen = snap().open;
  resize(390);
  out.crossBackToCompact = snap();
  window.toggleOutlinePanel();
  out.closedAfterCrossing = snap();

  // F. Desktop: the plain toggle still opens the non-modal panel, no backdrop.
  resize(1280);
  window.toggleOutlinePanel();
  await tick();
  out.desktopOpen = snap();
  window.toggleOutlinePanel();

  // G. An approval preempts the open compact sheet.
  resize(390);
  window.toggleOutlinePanel();
  await tick();
  window.showApprovalCard();
  out.approvalPreemptsCompact = snap();
  approvalCard.hidden = true;
  approvalCard.classList.remove('visible');

  // H. A clarification preempts it too.
  window.toggleOutlinePanel();
  await tick();
  window.showClarifyCard();
  out.clarifyPreemptsCompact = snap();
  clarifyCard.hidden = true;
  clarifyCard.classList.remove('visible');

  // I. Desktop keeps its panel when a prompt opens (non-modal).
  resize(1280);
  window.toggleOutlinePanel();
  await tick();
  window.showClarifyCard();
  out.desktopPanelSurvivesPrompt = snap().open;
  clarifyCard.hidden = true;
  clarifyCard.classList.remove('visible');
  window.toggleOutlinePanel();

  // J. A successful jump collapses the compact sheet but not the desktop panel.
  resize(390);
  window.toggleOutlinePanel();
  await tick();
  window._outlineJump(0);
  out.compactJumpCollapses = snap();
  resize(1280);
  window.toggleOutlinePanel();
  await tick();
  window._outlineJump(0);
  out.desktopJumpKeepsOpen = snap().open;
  window.toggleOutlinePanel();

  // K. A breakpoint change with no window resize event (devtools responsive
  // mode) must still re-shape an open panel.
  resize(1280);
  window.toggleOutlinePanel();
  await tick();
  mediaChange(390);
  out.mediaChangeToCompact = snap();
  mediaChange(1280);
  out.mediaChangeBackToDesktop = snap();

  // L. A sheet the user deliberately reopens over a PENDING prompt survives the
  //    approval poller, which redraws the same visible card every 1.5 s.
  resize(1280);
  if (snap().open) window.toggleOutlinePanel();   // known-closed baseline
  resize(390);
  openCard(approvalCard);              // prompt up first (no sheet to yield)
  window.toggleOutlinePanel();         // the user reopens the outline anyway
  await tick();
  out.reopenedOverPendingPrompt = snap().open;
  window.showApprovalCard();           // poller tick: same card, still visible
  out.survivesPollerRedraw = snap().open;
  out.pollerFocusStaysInSheet = label(document.activeElement);
  focusEl(document.body);              // the prompt's own 50 ms re-focus
  out.focusInGuardPullsBack = label(document.activeElement);
  approvalCard.hidden = true;
  approvalCard.classList.remove('visible');
  if (snap().open) window.toggleOutlinePanel();

  // M. Narrowing under an open DESKTOP panel while a prompt is showing must
  //    yield instead of building a sheet on top of the safety surface.
  resize(1280);
  if (snap().open) window.toggleOutlinePanel();
  window.toggleOutlinePanel();
  await tick();
  openCard(approvalCard);
  out.desktopPanelWithPrompt = snap().open;
  resize(390);                          // breakpoint crossing = gate refresh
  out.narrowedUnderPrompt = snap();
  approvalCard.hidden = true;
  approvalCard.classList.remove('visible');
  window.toggleOutlinePanel();

  // N. App chords (Ctrl+K New Chat, Ctrl+/ focus composer) settle the sheet
  //    first, then still reach the app handler.
  resize(390);
  if (snap().open) window.toggleOutlinePanel();
  window.toggleOutlinePanel();
  await tick();
  appKeyEvents.length = 0;
  const chordComposer = keydown('/', { ctrlKey: true });
  out.ctrlSlashClosesSheet = snap().open;
  out.ctrlSlashReachesApp = appKeyEvents.slice();
  out.ctrlSlashNotPrevented = chordComposer.defaultPrevented;
  window.toggleOutlinePanel();
  await tick();
  appKeyEvents.length = 0;
  const chordNewChat = keydown('k', { ctrlKey: true });
  out.ctrlKClosesSheet = snap().open;
  out.ctrlKReachesApp = appKeyEvents.slice();
  out.ctrlKNotPrevented = chordNewChat.defaultPrevented;

  // O. Tab navigation must scroll: plain focus(), no preventScroll.
  if (snap().open) window.toggleOutlinePanel();
  window.toggleOutlinePanel();
  await tick();
  const tabEntries = sheetFocusables().slice(1);
  focusEl(tabEntries[tabEntries.length - 1]);
  LAST_FOCUS_OPTS = 'unset';
  keydown('Tab');
  out.tabFocusOpts = LAST_FOCUS_OPTS;
  out.tabFocusTarget = label(document.activeElement);

  // P. A backdrop tap keeps focus inside the sheet long enough to hand it back
  //    to the toggle (mousedown must not move focus to <body>).
  const backdropEl = document.getElementById('outlineBackdrop');
  out.backdropMousedownPrevented = mousedownEl(backdropEl).defaultPrevented;

  // Q. Escape is settled in the capture phase: the app handler never sees it.
  appKeyEvents.length = 0;
  const escQ = keydown('Escape');
  out.escapePropagationStopped = escQ.propagationStopped === true;
  out.appSawEscape = appKeyEvents.slice();
  out.afterEscapeQ = snap();

  process.stdout.write(JSON.stringify(out));
})().catch((err) => {
  process.stderr.write(String(err && err.stack || err));
  process.exit(1);
});
"""


@pytest.fixture(scope="module")
def observations(tmp_path_factory):
    driver = tmp_path_factory.mktemp("t2124") / "outline_driver.js"
    driver.write_text(_DRIVER, encoding="utf-8")
    proc = subprocess.run(
        [NODE, str(driver), str(OUTLINE_JS_PATH)],
        capture_output=True, text=True, timeout=60,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"node driver failed: {proc.stderr}")
    return json.loads(proc.stdout)


def test_compact_toggle_opens_the_bottom_sheet(observations):
    """Below 900px the toggle reaches the outline and the sheet is a modal
    dialog with a dimming backdrop and the jump list rendered."""
    o = observations["compactOpen"]
    assert observations["toggleVisibleOnCompact"] is True, (
        "the outline toggle must be reachable on narrow viewports"
    )
    assert o["open"] is True, "the toggle must open the outline below 900px"
    assert o["backdrop"] is True, "the compact sheet needs its dimming backdrop"
    assert o["role"] == "dialog", f"compact sheet must expose dialog semantics: {o}"
    assert o["ariaModal"] == "true", f"compact sheet must be aria-modal: {o}"
    assert o["entryCount"] >= 2, f"the sheet must render the jump list: {o}"


def test_compact_sheet_moves_focus_into_itself(observations):
    """Opening moves focus into the sheet, so keyboard and screen-reader users
    are not left in the composer behind the backdrop."""
    assert observations["compactOpen"]["focus"] == "outlineCloseBtn", (
        f"focus must move into the sheet on open: {observations['compactOpen']}"
    )


def test_tab_is_contained_inside_the_compact_sheet(observations):
    """Tab cycles within the sheet instead of escaping to the page behind it."""
    assert observations["tabWrapsForward"] == "outlineCloseBtn", (
        f"Tab from the last entry must wrap to the first control: {observations}"
    )
    assert observations["tabWrapsBackward"] == "sheet-focusable-2", (
        f"Shift+Tab from the first control must wrap to the last entry: {observations}"
    )


def test_escape_closes_the_compact_sheet_and_restores_focus(observations):
    """Escape closes the sheet, tears the chrome down, and returns focus to the
    toggle that opened it."""
    o = observations["afterEscape"]
    assert observations["escapePrevented"] is True, "Escape must be handled, not leaked"
    assert o["open"] is False, f"Escape must close the compact sheet: {o}"
    assert o["backdrop"] is False, f"the backdrop must be removed on Escape: {o}"
    assert o["role"] == "navigation", f"dialog semantics are compact-only: {o}"
    assert o["ariaModal"] is None, f"aria-modal must be dropped on close: {o}"
    assert o["keydownHandlers"] == 0, f"no keydown handler may outlive the sheet: {o}"
    assert o["focus"] == "outlineToggleBtn", f"focus must return to the toggle: {o}"


def test_backdrop_tap_closes_the_compact_sheet(observations):
    o = observations["afterBackdropTap"]
    assert o["open"] is False, f"tapping the backdrop must close the sheet: {o}"
    assert o["backdrop"] is False, f"the backdrop must remove itself: {o}"
    assert o["focus"] == "outlineToggleBtn", f"focus must return to the toggle: {o}"


def test_breakpoint_crossing_swaps_the_two_forms(observations):
    """Widening while the sheet is open must degrade it to the desktop panel
    (non-modal, no backdrop, no Escape handler) and narrowing must restore the
    sheet chrome — the JS gate and the CSS breakpoint must not disagree."""
    wide = observations["wideWhileOpen"]
    assert wide["open"] is True, "crossing to desktop must not close the panel"
    assert wide["backdrop"] is False, f"desktop never gets a backdrop: {wide}"
    assert wide["role"] == "navigation", f"desktop stays non-modal navigation: {wide}"
    assert wide["ariaModal"] is None, f"desktop must not be aria-modal: {wide}"
    assert wide["keydownHandlers"] == 0, f"desktop must not keep the Escape handler: {wide}"
    assert observations["wideEscapeKeepsOpen"] is True, "Escape is compact-only"
    assert observations["wideJumpKeepsOpen"] is True, (
        "the desktop panel is non-modal, so a jump must not collapse it"
    )

    back = observations["crossBackToCompact"]
    assert back["open"] is True and back["backdrop"] is True, (
        f"narrowing back must re-shape the open panel into the sheet: {back}"
    )
    assert back["role"] == "dialog" and back["ariaModal"] == "true", back
    assert observations["closedAfterCrossing"]["open"] is False
    assert observations["closedAfterCrossing"]["role"] == "navigation"


def test_desktop_panel_is_unchanged(observations):
    """The desktop form keeps its non-modal, backdrop-free behavior."""
    o = observations["desktopOpen"]
    assert o["open"] is True, f"the toggle must still open the desktop panel: {o}"
    assert o["backdrop"] is False, f"no backdrop on desktop: {o}"
    assert o["role"] == "navigation", f"desktop stays navigation: {o}"
    assert o["ariaModal"] is None, f"desktop is not a modal dialog: {o}"
    assert o["entryCount"] >= 2, f"the jump list still renders: {o}"


def test_incoming_prompts_preempt_the_compact_sheet(observations):
    """An approval or clarification opens underneath the sheet, so the sheet
    must yield instead of hiding a time-sensitive safety prompt."""
    approval = observations["approvalPreemptsCompact"]
    assert approval["open"] is False, f"an approval must preempt the sheet: {approval}"
    assert approval["backdrop"] is False, (
        f"the pointer-blocking backdrop must be gone for the approval: {approval}"
    )
    assert approval["keydownHandlers"] == 0, approval

    clarify = observations["clarifyPreemptsCompact"]
    assert clarify["open"] is False, f"a clarification must preempt the sheet: {clarify}"
    assert clarify["backdrop"] is False, clarify


def test_prompt_preemption_is_compact_only(observations):
    """Desktop's panel does not cover the composer prompt area, so it stays."""
    assert observations["desktopPanelSurvivesPrompt"] is True, observations


def test_jump_collapses_only_the_compact_sheet(observations):
    """The sheet covers the transcript it just jumped into, so it closes; the
    desktop panel stays open."""
    assert observations["compactJumpCollapses"]["open"] is False, observations
    assert observations["desktopJumpKeepsOpen"] is True, observations


def test_breakpoint_change_without_a_resize_event_resyncs(observations):
    """Devtools responsive mode and some orientation changes move the breakpoint
    without firing window resize; the gate must still follow it."""
    compact = observations["mediaChangeToCompact"]
    assert compact["open"] is True and compact["backdrop"] is True, (
        f"crossing into compact must re-shape the open panel into the sheet: {compact}"
    )
    assert compact["role"] == "dialog" and compact["ariaModal"] == "true", compact

    wide = observations["mediaChangeBackToDesktop"]
    assert wide["open"] is True, "crossing back must keep the panel open"
    assert wide["backdrop"] is False and wide["role"] == "navigation", (
        f"crossing to desktop must drop the sheet chrome: {wide}"
    )
    assert wide["keydownHandlers"] == 0, wide


def test_reopened_sheet_survives_the_approval_poller(observations):
    """A sheet the user deliberately reopens over a pending prompt must not be
    torn down by the 1.5 s poller that redraws the SAME visible card."""
    assert observations["reopenedOverPendingPrompt"] is True, (
        f"the toggle must still open the sheet over a pending prompt: {observations}"
    )
    assert observations["survivesPollerRedraw"] is True, (
        f"a poller redraw of an already-visible card must not close it: {observations}"
    )
    assert observations["pollerFocusStaysInSheet"] == "outlineCloseBtn", observations


def test_focus_stolen_by_a_prompt_is_pulled_back_into_the_sheet(observations):
    """The approval card re-focuses its own button after a redraw; while the
    sheet chrome is up, that focus must not park underneath the backdrop."""
    assert observations["focusInGuardPullsBack"] == "outlineCloseBtn", observations


def test_narrowing_under_a_visible_prompt_yields(observations):
    """Master closes the outline when a prompt is showing. Narrowing under an
    open desktop panel must do the same instead of building a sheet on top."""
    assert observations["desktopPanelWithPrompt"] is True, (
        "the desktop panel is non-modal, so a prompt leaves it open"
    )
    o = observations["narrowedUnderPrompt"]
    assert o["open"] is False, f"the sheet must not cover a visible prompt: {o}"
    assert o["backdrop"] is False, f"no pointer-blocking backdrop over a prompt: {o}"
    assert o["role"] == "navigation", f"dialog semantics must not be applied: {o}"
    assert o["keydownHandlers"] == 0, o


def test_app_chords_close_the_sheet_first(observations):
    """Ctrl+/ (focus composer) and Ctrl+K (New Chat) open things BEHIND the
    sheet, so it must close and let the chord through."""
    assert observations["ctrlSlashClosesSheet"] is False, observations
    assert observations["ctrlSlashReachesApp"] == ["/"], (
        f"the chord must still reach the app handler: {observations}"
    )
    assert observations["ctrlSlashNotPrevented"] is False, observations
    assert observations["ctrlKClosesSheet"] is False, observations
    assert observations["ctrlKReachesApp"] == ["k"], observations
    assert observations["ctrlKNotPrevented"] is False, observations


def test_tab_focus_scrolls_the_entry_into_view(observations):
    """Tab must not use preventScroll: in a long outline that parks focus on an
    off-screen entry."""
    opts = observations["tabFocusOpts"]
    assert opts is None, f"Tab must use a plain focus(), got {opts!r}"
    assert observations["tabFocusTarget"] == "outlineCloseBtn", observations


def test_backdrop_mousedown_keeps_focus_in_the_sheet(observations):
    """A mousedown on the backdrop would move focus to <body> before the click,
    leaving nothing for the close path to hand back to the toggle."""
    assert observations["backdropMousedownPrevented"] is True, observations


def test_escape_does_not_reach_the_app_handler(observations):
    """Closing the sheet must not also run the app-wide Escape handler, which
    clears the sidebar search behind it."""
    assert observations["escapePropagationStopped"] is True, observations
    assert observations["appSawEscape"] == [], (
        f"the global Escape handler must not see the sheet's Escape: {observations}"
    )
    assert observations["afterEscapeQ"]["open"] is False, observations

