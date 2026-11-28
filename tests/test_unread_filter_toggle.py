"""Composed DOM coverage for the sidebar "Show unread only" toggle (#3910 / PR #6590).

The first review round rejected the original harness because it copied the
filter/empty-state logic into a standalone Node string and asserted source
substrings: that stays green when ``renderSessionListFromCache()`` regresses,
when the i18n DOM updater stops touching the checkbox, or when a locale switch
leaves the visible label / ARIA name stale.

This module drives the shipped code instead:

* the toggle markup is parsed from the real ``static/index.html`` (element
  attributes are read from the file, never retyped here);
* ``static/icons.js``, ``static/i18n.js`` and ``static/sessions.js`` are
  evaluated as they ship inside a Node VM with a small DOM shim;
* the i18n cases run the production ``setLocale()`` + ``applyLocaleToDOM()``
  over that markup;
* the list cases run the production ``renderSessionListFromCache()`` over the
  production payload global (``_allSessions``) and assert on the rendered
  ``.session-item`` rows and ``.session-empty-note`` text.

Every behavioural assertion fails if the production filter condition is
reverted or the locale keys are dropped.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[1]
I18N_JS = REPO / "static" / "i18n.js"
INDEX_HTML = REPO / "static" / "index.html"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")

# The label markup that ships in the sidebar, so a rename in index.html reaches
# this harness instead of being silently retyped.
_TOGGLE_MARKUP_RE = re.compile(r"<label class=\"session-unread-toggle\".*?</label>", re.S)


def _toggle_markup() -> str:
    html = INDEX_HTML.read_text(encoding="utf-8")
    match = _TOGGLE_MARKUP_RE.search(html)
    assert match, "the sidebar unread toggle markup is gone from static/index.html"
    return match.group(0)


_LOCALE_DECL_RE = re.compile(r"^  (?:'(?P<quoted>[^']+)'|(?P<plain>[a-z]{2,3})): \{", re.M)


def _locale_decls() -> list[dict]:
    """Locale declarations in static/i18n.js, in file order (quoted keys included)."""
    source = I18N_JS.read_text(encoding="utf-8")
    origin = source.index("const LOCALES = {")
    decls = []
    for match in _LOCALE_DECL_RE.finditer(source):
        if match.start() < origin:
            continue
        decls.append({"name": match.group("quoted") or match.group("plain"), "start": match.start()})
    return decls


_HARNESS = r"""
const fs = require('fs');
const vm = require('vm');
const path = require('path');

const REPO = %(repo)r;
const MARKUP = %(markup)r;

class El {
  constructor(tag) {
    this.tagName = String(tag || 'div').toUpperCase();
    this.children = [];
    this.attrs = {};
    this.id = '';
    this._cls = new Set();
    this._text = '';
    this._html = '';
    this.value = '';
    this.checked = false;
    this.scrollTop = 0;
    this.scrollHeight = 0;
    this.clientHeight = 600;
    this.parentNode = null;
    this.isConnected = true;
    this.dataset = {};
    this.href = '';
    this.style = new Proxy({ cssText: '' }, {
      get: (t, k) => (k === 'setProperty' || k === 'removeProperty' ? () => {} : (k in t ? t[k] : '')),
      set: (t, k, v) => { t[k] = v; return true; },
    });
    const self = this;
    this.classList = {
      add: (...c) => c.forEach((x) => self._cls.add(x)),
      remove: (...c) => c.forEach((x) => self._cls.delete(x)),
      toggle: (c, on) => {
        const has = self._cls.has(c);
        const want = on === undefined ? !has : !!on;
        if (want) self._cls.add(c); else self._cls.delete(c);
        return want;
      },
      contains: (c) => self._cls.has(c),
      get length() { return self._cls.size; },
    };
  }
  get className() { return Array.from(this._cls).join(' '); }
  set className(v) { this._cls = new Set(String(v || '').split(/\s+/).filter(Boolean)); }
  get textContent() {
    return this._text + this.children.map((c) => c.textContent || '').join('');
  }
  set textContent(v) { this._text = String(v === null || v === undefined ? '' : v); this.children = []; }
  get innerHTML() { return this._html; }
  set innerHTML(v) {
    this._html = String(v === null || v === undefined ? '' : v);
    if (this._html === '') this.children = [];
  }
  appendChild(c) { if (c) { c.parentNode = this; this.children.push(c); } return c; }
  append(...cs) { cs.forEach((c) => this.appendChild(c)); }
  insertBefore(c, ref) {
    const i = ref ? this.children.indexOf(ref) : -1;
    c.parentNode = this;
    if (i < 0) this.children.push(c); else this.children.splice(i, 0, c);
    return c;
  }
  removeChild(c) { const i = this.children.indexOf(c); if (i >= 0) this.children.splice(i, 1); return c; }
  remove() { if (this.parentNode) this.parentNode.removeChild(this); }
  setAttribute(k, v) { this.attrs[k] = String(v); if (k === 'id') this.id = String(v); }
  getAttribute(k) { return Object.prototype.hasOwnProperty.call(this.attrs, k) ? this.attrs[k] : null; }
  removeAttribute(k) { delete this.attrs[k]; }
  hasAttribute(k) { return Object.prototype.hasOwnProperty.call(this.attrs, k); }
  addEventListener() {}
  removeEventListener() {}
  dispatchEvent() { return true; }
  focus() {}
  blur() {}
  click() {}
  closest() { return null; }
  matches() { return false; }
  contains(n) { return this.children.indexOf(n) >= 0; }
  scrollIntoView() {}
  insertAdjacentHTML() {}
  querySelector(sel) { const out = this._collect(sel); return out[0] || null; }
  querySelectorAll(sel) { return this._collect(sel); }
  _collect(sel) {
    const out = [];
    const wanted = String(sel || '').split(',').map((s) => s.trim()).filter(Boolean);
    const hitOne = (el, one) => {
      let rest = one;
      const attrs = [];
      const attrRe = /\[([^\]]+)\]/g;
      let m;
      while ((m = attrRe.exec(one))) attrs.push(m[1]);
      rest = one.replace(attrRe, '').trim();
      for (const a of attrs) { if (!el.hasAttribute(a)) return false; }
      if (!rest) return true;
      if (rest.startsWith('.')) return rest.split('.').filter(Boolean).every((c) => el._cls.has(c));
      if (rest.startsWith('#')) return el.id === rest.slice(1);
      return el.tagName === rest.toUpperCase();
    };
    const hit = (el) => wanted.some((one) => hitOne(el, one));
    const walk = (el) => { for (const c of el.children) { if (hit(c)) out.push(c); walk(c); } };
    walk(this);
    return out;
  }
  getBoundingClientRect() { return { top: 0, left: 0, width: 300, height: 20, bottom: 20, right: 300 }; }
  get firstChild() { return this.children[0] || null; }
  get lastChild() { return this.children[this.children.length - 1] || null; }
  set title(v) { this.attrs.title = v; }
  get title() { return this.attrs.title || ''; }
}

function parseAttrs(raw) {
  const attrs = {};
  const re = /([A-Za-z_:][-A-Za-z0-9_:.]*)\s*=\s*"([^"]*)"/g;
  let m;
  while ((m = re.exec(raw))) attrs[m[1]] = m[2];
  return attrs;
}

function buildToggleTree() {
  const label = new El('label');
  Object.assign(label.attrs, parseAttrs(MARKUP.match(/<label\b([^>]*)>/)[1]));
  label.className = label.attrs.class || '';
  const input = new El('input');
  Object.assign(input.attrs, parseAttrs(MARKUP.match(/<input\b([^>]*)>/)[1]));
  input.id = input.attrs.id || '';
  label.appendChild(input);
  const span = new El('span');
  Object.assign(span.attrs, parseAttrs(MARKUP.match(/<span\b([^>]*)>/)[1]));
  const spanText = MARKUP.match(/<span\b[^>]*>([\s\S]*?)<\/span>/);
  span.textContent = spanText ? spanText[1] : '';
  label.appendChild(span);
  return { label, input, span };
}

const toggle = buildToggleTree();
const roots = {
  sessionList: new El('div'),
  sessionSearch: new El('input'),
  sessionSearchClear: new El('button'),
  batchActionBar: new El('div'),
  sessionUnreadToggle: toggle.input,
};
roots.sessionList.id = 'sessionList';
roots.sessionSearch.value = '';

const body = new El('body');
body.appendChild(toggle.label);
body.appendChild(roots.sessionList);

const doc = {
  documentElement: new El('html'),
  head: new El('head'),
  body,
  readyState: 'complete',
  createElement: (t) => new El(t),
  createTextNode: (t) => { const e = new El('text'); e.textContent = t; return e; },
  createDocumentFragment: () => new El('fragment'),
  getElementById: (id) => roots[id] || null,
  querySelector: (sel) => body.querySelector(sel),
  querySelectorAll: (sel) => body.querySelectorAll(sel),
  addEventListener() {},
  removeEventListener() {},
  dispatchEvent() { return true; },
  createRange: () => ({ selectNodeContents() {}, setStart() {}, setEnd() {}, toString() { return ''; } }),
  fonts: { ready: Promise.resolve() },
};

const store = {};
const LS = {
  getItem: (k) => (Object.prototype.hasOwnProperty.call(store, k) ? store[k] : null),
  setItem: (k, v) => { store[k] = String(v); },
  removeItem: (k) => { delete store[k]; },
  key: (i) => Object.keys(store)[i] || null,
  get length() { return Object.keys(store).length; },
};

const sandbox = {
  console,
  document: doc,
  localStorage: LS,
  sessionStorage: LS,
  navigator: { language: 'en', languages: ['en'], userAgent: 'node', clipboard: {}, platform: 'Win32', maxTouchPoints: 0 },
  location: { href: 'http://127.0.0.1:8787/', origin: 'http://127.0.0.1:8787', search: '', pathname: '/', hash: '', protocol: 'http:', host: '127.0.0.1:8787' },
  history: { replaceState() {}, pushState() {} },
  setTimeout, clearTimeout, setInterval, clearInterval, queueMicrotask,
  requestAnimationFrame: (fn) => setTimeout(() => fn(Date.now()), 0),
  cancelAnimationFrame: (h) => clearTimeout(h),
  fetch: () => Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({}), text: () => Promise.resolve(''), headers: { get: () => null } }),
  EventSource: class { constructor() { this.readyState = 1; } addEventListener() {} close() {} },
  WebSocket: class { constructor() { this.readyState = 1; } addEventListener() {} send() {} close() {} },
  IntersectionObserver: class { observe() {} unobserve() {} disconnect() {} },
  ResizeObserver: class { observe() {} unobserve() {} disconnect() {} },
  MutationObserver: class { observe() {} disconnect() {} },
  matchMedia: () => ({ matches: false, addEventListener() {}, removeEventListener() {} }),
  getComputedStyle: () => ({ getPropertyValue: () => '' }),
  alert() {},
  confirm: () => true,
  prompt: () => null,
  innerWidth: 1280,
  innerHeight: 800,
  devicePixelRatio: 1,
  open: () => null,
  close() {},
  focus() {},
  addEventListener() {},
  removeEventListener() {},
  dispatchEvent() { return true; },
};
sandbox.window = sandbox;
sandbox.globalThis = sandbox;
sandbox.self = sandbox;
sandbox.top = sandbox;
sandbox.parent = sandbox;

const ctx = vm.createContext(sandbox);
for (const f of ['static/icons.js', 'static/i18n.js', 'static/sessions.js']) {
  vm.runInContext(fs.readFileSync(path.join(REPO, f), 'utf8'), ctx, { filename: f, timeout: 60000 });
}

// Globals the shipped files expect from ui.js / boot.js.
const run = (code) => vm.runInContext(code, ctx, { timeout: 60000 });
run(`
  S = { session: null, messages: [], sessionId: null };
  $ = (id) => document.getElementById(id);
  _contentSearchResults = null;
  _allSessions = [];
`);

function reset(rows, activeSid) {
  run(`
    try { localStorage.clear(); } catch (_e) {}
    S = { session: ${JSON.stringify(activeSid ? { session_id: activeSid } : null)}, messages: [], sessionId: null };
    _sessionSourceFilter = 'webui';
    _activeProject = null;
    _showArchived = false;
    window._showCliSessions = true;
    _showUnreadOnly = false;
    _allSessions = ${JSON.stringify(rows)};
  `);
}

function markViewed(sid, count) {
  run(`_setSessionViewedCount(${JSON.stringify(sid)}, ${Number(count)}, 0);`);
}

function sidebar() {
  return run(`(() => {
    const list = document.getElementById('sessionList');
    const items = list.querySelectorAll('.session-item');
    const notes = list.querySelectorAll('.session-empty-note').map((e) => e.textContent.trim());
    const titles = items.map((e) => {
      const t = e.querySelector('.session-title');
      return t ? t.textContent.trim() : '';
    });
    return JSON.stringify({ rows: items.length, notes, titles });
  })()`);
}

const out = {};

// ── i18n: production setLocale + applyLocaleToDOM over the shipped markup ──
out.locale = (function () {
  const result = {};
  for (const lang of %(locale_probe)r) {
    run(`setLocale(${JSON.stringify(lang)}); applyLocaleToDOM();`);
    result[lang] = {
      visible: run('document.querySelector("span[data-i18n]").textContent.trim()'),
      aria: run('document.getElementById("sessionUnreadToggle").getAttribute("aria-label")'),
      attr_key: run('document.getElementById("sessionUnreadToggle").getAttribute("data-i18n-aria-label")'),
      text_key: run('document.querySelector("span[data-i18n]").getAttribute("data-i18n")'),
    };
  }
  result.__en = run('LOCALES.en.show_unread_only || null');
  result.__pt = run('LOCALES.pt.show_unread_only || null');
  result.__de = run('LOCALES.de.show_unread_only || null');
  result.__note_en = run('LOCALES.en.no_unread_conversations || null');
  result.__note_pt = run('LOCALES.pt.no_unread_conversations || null');
  return result;
})();

// ── (d) nonempty list, every row read: filter hides rows + locale note ─────
reset([
  { session_id: 'read1', title: 'Alpha', message_count: 4, updated_at: Date.now() / 1000, session_source: 'webui' },
  { session_id: 'read2', title: 'Beta', message_count: 2, updated_at: Date.now() / 1000, session_source: 'webui' },
], null);
markViewed('read1', 4);
markViewed('read2', 2);
run('setLocale("pt"); applyLocaleToDOM();');
run('_setUnreadOnly(false);');
out.all_read_filter_off = JSON.parse(sidebar());
run('_setUnreadOnly(true);');
out.all_read_filter_on = JSON.parse(sidebar());
out.note_pt = run('LOCALES.pt.no_unread_conversations || null');

// ── (b) CLI source selected with zero rows: single CLI note ────────────────
reset([], null);
run('_sessionSourceFilter = "cli"; window._showCliSessions = true;');
run('renderSessionListFromCache();');
out.cli_zero = JSON.parse(sidebar());
run('_setUnreadOnly(true);');
out.cli_zero_unread = JSON.parse(sidebar());

// ── (c) project filter with zero rows: single project note ─────────────────
reset([], null);
run('_activeProject = "proj-6590";');
run('renderSessionListFromCache();');
out.project_zero = JSON.parse(sidebar());

// ── (e1) active session is READ but must stay visible ──────────────────────
reset([
  { session_id: 'act', title: 'Active', message_count: 3, updated_at: Date.now() / 1000, session_source: 'webui' },
  { session_id: 'other', title: 'Other', message_count: 3, updated_at: Date.now() / 1000, session_source: 'webui' },
], 'act');
markViewed('act', 3);
markViewed('other', 3);
run('_setUnreadOnly(true);');
out.active_read_retained = JSON.parse(sidebar());

// ── (e2) read parent with an unread child keeps the parent row ─────────────
reset([
  { session_id: 'parent', title: 'Parent', message_count: 5, updated_at: Date.now() / 1000, session_source: 'webui' },
  { session_id: 'childrow', title: 'Child', message_count: 5, parent_session_id: 'parent', relationship_type: 'child_session', updated_at: Date.now() / 1000, session_source: 'webui' },
], null);
markViewed('parent', 5);
markViewed('childrow', 1);
run('_setUnreadOnly(true);');
out.child_unread_parent_retained = JSON.parse(sidebar());

// ── control: an unread row stays visible with the filter on ────────────────
reset([
  { session_id: 'unread1', title: 'Unread', message_count: 6, updated_at: Date.now() / 1000, session_source: 'webui' },
  { session_id: 'readagain', title: 'Read', message_count: 2, updated_at: Date.now() / 1000, session_source: 'webui' },
], null);
markViewed('unread1', 1);
markViewed('readagain', 2);
run('_setUnreadOnly(true);');
out.unread_visible = JSON.parse(sidebar());

process.stdout.write(JSON.stringify(out));
"""


def _run_harness(locale_probe: list[str]) -> dict:
    assert NODE is not None
    script = _HARNESS % {
        "repo": str(REPO),
        "markup": _toggle_markup(),
        "locale_probe": locale_probe,
    }
    result = subprocess.run(
        [NODE, "-e", script],
        cwd=str(REPO),
        capture_output=True,
        text=True,
        timeout=240,
    )
    assert result.returncode == 0, f"node harness failed: {result.stderr[-4000:]}"
    return json.loads(result.stdout)


@pytest.fixture(scope="module")
def harness():
    return _run_harness(["en", "pt", "de"])


def test_every_locale_block_defines_the_toggle_strings():
    decls = _locale_decls()
    assert len(decls) >= 15, f"expected the full locale set, found {[d['name'] for d in decls]}"
    source = I18N_JS.read_text(encoding="utf-8")
    missing = []
    for index, decl in enumerate(decls):
        end = decls[index + 1]["start"] if index + 1 < len(decls) else len(source)
        block = source[decl["start"]:end]
        for field in ("show_unread_only", "no_unread_conversations"):
            if f"{field}:" not in block:
                missing.append(f"{decl['name']}.{field}")
    assert not missing, f"locale blocks missing the toggle strings: {missing}"


def test_locale_switch_updates_visible_label_and_aria_label(harness):
    en = harness["locale"]["en"]
    pt = harness["locale"]["pt"]
    de = harness["locale"]["de"]
    assert en["text_key"] == "show_unread_only"
    assert en["attr_key"] == "show_unread_only"
    assert en["visible"] == harness["locale"]["__en"]
    assert en["aria"] == harness["locale"]["__en"]

    # A locale switch must re-stamp BOTH the visible label and the ARIA name.
    assert harness["locale"]["__pt"], "LOCALES.pt.show_unread_only is missing"
    assert pt["visible"] == harness["locale"]["__pt"], "the visible label did not follow the locale switch"
    assert pt["aria"] == harness["locale"]["__pt"], "the checkbox aria-label did not follow the locale switch"

    assert harness["locale"]["__de"], "LOCALES.de.show_unread_only is missing"
    assert de["visible"] == harness["locale"]["__de"]
    assert de["aria"] == harness["locale"]["__de"]


def test_all_read_list_is_filtered_and_shows_the_locale_empty_note(harness):
    before = harness["all_read_filter_off"]
    after = harness["all_read_filter_on"]
    assert before["rows"] == 2, "control: with the filter off both rows must render"
    assert after["rows"] == 0, "the unread-only filter must hide every read row"
    assert harness["note_pt"], "LOCALES.pt.no_unread_conversations is missing"
    assert harness["note_pt"] in after["notes"], f"expected the localized empty note, got {after['notes']}"


def test_cli_source_with_zero_rows_keeps_only_the_cli_note(harness):
    for phase in ("cli_zero", "cli_zero_unread"):
        out = harness[phase]
        assert out["rows"] == 0, phase
        assert out["notes"] == ["No CLI sessions found."], f"{phase}: {out['notes']}"


def test_project_filter_with_zero_rows_keeps_only_the_project_note(harness):
    out = harness["project_zero"]
    assert out["rows"] == 0
    assert out["notes"] == ["No sessions in this project yet."], out["notes"]


def test_active_session_is_retained_even_when_read(harness):
    out = harness["active_read_retained"]
    assert out["titles"] == ["Active"], f"the active session must stay visible, got {out['titles']}"


def test_read_parent_with_unread_child_is_retained(harness):
    out = harness["child_unread_parent_retained"]
    assert out["titles"] == ["Parent"], f"expected only the parent row, got {out['titles']}"


def test_unread_row_stays_visible_with_the_filter_on(harness):
    out = harness["unread_visible"]
    assert out["titles"] == ["Unread"], f"expected only the unread row, got {out['titles']}"
