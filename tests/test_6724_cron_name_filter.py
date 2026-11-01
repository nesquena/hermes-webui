"""Regression coverage for the Scheduled Jobs sidebar name filter (#6724).

The markup/CSS checks are static. Everything about filtering, load ordering,
the refresh button, the paused disclosure and the selected detail is driven
through the REAL loadCrons()/filterCrons()/clearCronSearch() extracted from
static/panels.js and run under node against a minimal fake DOM, asserting
observable state (rendered rows, button state, request log), not source text.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PANELS_JS_PATH = ROOT / "static" / "panels.js"
PANELS_JS = PANELS_JS_PATH.read_text(encoding="utf-8")
INDEX_HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
I18N_JS = (ROOT / "static" / "i18n.js").read_text(encoding="utf-8")
STYLE_CSS = (ROOT / "static" / "style.css").read_text(encoding="utf-8")

NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="node not on PATH")


def test_cron_search_input_uses_the_shared_sidebar_search_pattern():
    """The cron filter must reuse .sidebar-search, not a bespoke input."""
    assert 'id="cronSearch"' in INDEX_HTML, "input removed -> no way to type a filter query"
    assert '<div class="cron-search sidebar-search">' in INDEX_HTML, (
        "not using the shared sidebar-search wrapper -> loses existing CSS, breaks parity with "
        "Chat/Kanban/Skills filters"
    )
    assert 'type="search"' in INDEX_HTML, "wrong input type -> loses native clear (x) button"
    assert 'oninput="filterCrons()"' in INDEX_HTML, (
        "missing oninput handler -> typing does not filter the list live"
    )
    assert 'data-1p-ignore data-lpignore="true" data-bwignore="true"' in INDEX_HTML, (
        "missing password-manager opt-out attrs -> browsers may offer to autofill credentials "
        "into a search box"
    )


def test_cron_search_has_an_explicit_clear_control():
    """The shared CSS hides the native WebKit clear (x), so an explicit control is required."""
    assert 'id="cronSearchClear"' in INDEX_HTML, (
        "no explicit clear button -> the native WebKit clear (x) is disabled by "
        ".sidebar-search input::-webkit-search-cancel-button"
    )
    assert 'onclick="clearCronSearch()"' in INDEX_HTML
    assert 'data-i18n-title="clear_job_filter" data-i18n-aria-label="clear_job_filter"' in INDEX_HTML, (
        "clear button tooltip/aria-label must go through the locale pass, not stay English"
    )
    assert I18N_JS.count("clear_job_filter:") == I18N_JS.count("filter_jobs:"), (
        "every locale that ships filter_jobs must also ship clear_job_filter"
    )
    assert '<div class="cron-search-field">' in INDEX_HTML
    assert ".cron-search-clear" in STYLE_CSS


def test_cron_search_clear_has_a_44px_touch_target_on_coarse_pointers():
    """The glyph stays small, but the touch hit area must reach 44x44 CSS px."""
    rule = next(
        (line for line in STYLE_CSS.splitlines()
         if "@media (pointer:coarse)" in line and ".cron-search-clear" in line),
        "",
    )
    assert rule, "no coarse-pointer rule for the cron clear button -> 24x24 touch target"
    assert "width:44px" in rule and "height:44px" in rule, rule
    assert ".cron-search input{padding-right:44px;}" in rule, (
        "input must reserve the larger hit area so typed text doesn't run under it"
    )


def test_cron_filter_never_uses_locale_lowercasing():
    """toLocaleLowerCase() maps 'I' to dotless 'ı' under Turkish, breaking matches."""
    assert "toLocaleLowerCase" not in PANELS_JS


def test_cron_filter_strings_exist_in_english_locale():
    assert "filter_jobs: 'Filter jobs...'" in I18N_JS
    assert "cron_no_matching_jobs: 'No jobs match your search.'" in I18N_JS


# ── Behaviour tests (node) ────────────────────────────────────────────────

_DRIVER_SRC = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8');
const scenario = process.argv[3];

function extractFunc(name) {
  const re = new RegExp('(?:async\\s+)?function\\s+' + name + '\\s*\\(');
  const m = re.exec(src);
  if (!m) throw new Error('function not found: ' + name);
  // Find the body brace after the signature's closing paren (defaults may contain braces).
  let depthP = 1, j = m.index + m[0].length;
  while (depthP) { const c = src[j++]; if (c === '(') depthP++; else if (c === ')') depthP--; }
  const i = src.indexOf('{', j);
  let depth = 0;
  for (let k = i; k < src.length; k++) {
    if (src[k] === '{') depth++;
    else if (src[k] === '}' && --depth === 0) return src.slice(m.index, k + 1);
  }
  throw new Error('unterminated function: ' + name);
}

// ---- minimal fake DOM ----
let focused = null;
class El {
  constructor(tag) {
    this.tagName = String(tag).toUpperCase();
    this.children = []; this._html = ''; this.style = {}; this.hidden = false;
    this.value = ''; this.disabled = false; this.textContent = ''; this.className = '';
    this.listeners = {}; this._open = false; this.parentNode = null;
    const cls = new Set();
    this.classList = { add: c => cls.add(c), contains: c => cls.has(c) };
  }
  set innerHTML(v) { this.children = []; this._html = String(v); }
  get innerHTML() { return this._html; }
  set open(v) {
    const next = !!v;
    if (next === this._open) return;
    this._open = next;
    // Browsers fire 'toggle' asynchronously for script-driven changes too.
    setTimeout(() => (this.listeners.toggle || []).forEach(f => f()), 0);
  }
  get open() { return this._open; }
  appendChild(c) { c.parentNode = this; this.children.push(c); return c; }
  addEventListener(t, f) { (this.listeners[t] = this.listeners[t] || []).push(f); }
  focus() { focused = this; }
}
const els = {};
for (const id of ['cronList', 'cronRefreshBtn', 'cronSearch', 'cronSearchClear']) {
  els[id] = new El(id === 'cronSearch' ? 'input' : 'div');
}
els.cronSearchClear.hidden = true;
global.document = { createElement: tag => new El(tag) };
const $ = id => els[id] || null;
const _lsStore = new Map();
global.localStorage = {
  getItem: k => (_lsStore.has(k) ? _lsStore.get(k) : null),
  setItem: (k, v) => { _lsStore.set(k, String(v)); },
};

// ---- collaborators loadCrons() reaches; network is controllable ----
const requests = [];
const pending = [];
function api(url) {
  requests.push(url);
  if (url.startsWith('/api/crons/history')) return Promise.resolve({ runs: [] });
  return new Promise((resolve, reject) => pending.push({ url, resolve, reject }));
}
const STRINGS = {
  cron_no_jobs: 'No scheduled jobs found.',
  cron_no_matching_jobs: 'No jobs match your search.',
  cron_status_paused: 'paused',
};
const t = k => STRINGS[k] || k;
const esc = s => String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
async function loadCronProfiles() { return []; }
function loadCronGatewayNotice() {}
function _isCronNeedsAttention() { return false; }
function _isCronScheduleError() { return false; }
let detailRenders = 0;
function _renderCronDetail(job) {
  detailRenders++;
  api('/api/crons/history?job_id=' + job.id + '&limit=50');
}
function _clearCronDetail() {}
function openCronDetail() {}

let _cronList = null;
let _currentCronDetail = null;
let _currentCronDetailKey = '';
let _cronMode = 'empty';
let _showAllCronProfiles = false;
let _cronOtherProfileCount = 0;
const _cronNewJobIds = new Set();
let _cronLoadGeneration = 0;
let _cronAcceptedLoadGeneration = 0;
let _cronLatestLoadSettled = Promise.resolve();
let _cronDetailRefreshKey = null;
let _currentPanel = 'tasks';

eval([
  '_cronStatusMeta', '_cronProfileName', '_cronProfileLabel', '_cronOwnerProfileName',
  '_cronJobKey', '_cronItemId', '_appendCronProfileToggle',
  '_renderCronList', 'syncCronSearchClear', 'filterCrons', 'clearCronSearch', 'loadCrons',
].map(extractFunc).join('\n'));

// ---- helpers for scenarios ----
const tick = () => new Promise(r => setTimeout(r, 0));
const settle = async () => { for (let i = 0; i < 5; i++) await tick(); };
function respond(index, jobs) { pending[index].resolve({ jobs, other_profile_count: 0 }); }
function type(text) { els.cronSearch.value = text; filterCrons(); }
function rows() {
  const out = [];
  const walk = (node, hiddenByDisclosure) => {
    for (const c of node.children) {
      const inClosed = hiddenByDisclosure || (c.tagName === 'DETAILS' && !c.open);
      if (c.className === 'cron-item') {
        const m = /class="cron-name"[^>]*>([^<]*)</.exec(c.innerHTML);
        out.push({ name: m ? m[1] : '?', visible: !hiddenByDisclosure });
      }
      walk(c, inClosed);
    }
  };
  walk(els.cronList, false);
  return out;
}
const visibleNames = () => rows().filter(r => r.visible).map(r => r.name).sort();
const btn = () => ({ disabled: els.cronRefreshBtn.disabled, opacity: els.cronRefreshBtn.style.opacity || '' });
const job = (id, name, extra) => Object.assign({ id, name, enabled: true }, extra || {});

const SCENARIOS = {
  async deferred_first_load_type_then_clear() {
    const load = loadCrons();
    await tick();
    type('alp');
    const clearVisibleWhilePending = !els.cronSearchClear.hidden;
    clearCronSearch();
    respond(0, [job('a', 'alpha'), job('b', 'beta'), job('c', 'gamma')]);
    await load; await settle();
    return { clearVisibleWhilePending, names: visibleNames(),
             clearHiddenAfter: els.cronSearchClear.hidden, listRequests: requests.filter(u => u === '/api/crons').length };
  },
  async deferred_first_load_type_only() {
    const load = loadCrons();
    await tick();
    type('ALP');
    const clearVisibleWhilePending = !els.cronSearchClear.hidden;
    respond(0, [job('a', 'alpha'), job('b', 'beta')]);
    await load; await settle();
    return { clearVisibleWhilePending, clearVisibleAfter: !els.cronSearchClear.hidden,
             names: visibleNames(), query: els.cronSearch.value };
  },
  async typing_does_not_rerender_selected_detail() {
    const load = loadCrons();
    await tick();
    respond(0, [job('aa11', 'daily-digest'), job('bb22', 'weekly')]);
    await load; await settle();
    _currentCronDetail = _cronList[0];
    _currentCronDetailKey = _cronJobKey(_cronList[0]);
    _cronMode = 'read';
    for (const q of ['d', 'da', 'dai', 'dail', 'daily']) type(q);
    await settle();
    const afterTyping = { renders: detailRenders, history: requests.filter(u => u.startsWith('/api/crons/history')).length,
                          names: visibleNames() };
    const refresh = loadCrons(true);
    await tick();
    respond(1, _cronList);
    await refresh; await settle();
    return { afterTyping, rendersAfterNetworkRefresh: detailRenders };
  },
  async superseded_manual_refresh_keeps_button_disabled() {
    const a = loadCrons(true);
    await tick();
    const b = loadCrons();
    await tick();
    const bothPending = btn();
    respond(0, [job('old', 'stale-a')]);
    await a; await settle();
    const staleSettled = btn();
    const namesAfterStale = visibleNames();
    respond(1, [job('new', 'fresh-b')]);
    await b; await settle();
    return { bothPending, staleSettled, namesAfterStale, final: btn(), finalNames: visibleNames() };
  },
  async paused_matches_are_visible_while_searching() {
    const load = loadCrons();
    await tick();
    respond(0, [
      job('1', 'alpha'), job('2', 'paused-only', { state: 'paused' }),
      job('3', 'mix-active'), job('4', 'mix-paused', { state: 'paused' }),
    ]);
    await load; await settle();
    const noQuery = rows();
    type('paused-only'); await settle();
    const pausedOnly = rows();
    type('alpha'); await settle();
    const activeOnly = rows();
    type('mix'); await settle();
    const mixed = rows();
    type(''); await settle();
    const cleared = rows();
    return { noQuery, pausedOnly, activeOnly, mixed, cleared,
             savedPref: localStorage.getItem('cron-paused-collapsed') };
  },
  async empty_list_vs_no_match() {
    const l1 = loadCrons();
    await tick();
    respond(0, []);
    await l1; await settle();
    const emptyHtml = els.cronList.innerHTML;
    const l2 = loadCrons();
    await tick();
    respond(1, [job('1', 'alpha')]);
    await l2; await settle();
    type('zzz'); await settle();
    const noMatchHtml = els.cronList.innerHTML;
    const listRequests = requests.filter(u => u === '/api/crons').length;
    return { emptyHtml, noMatchHtml, listRequests };
  },
};

SCENARIOS[scenario]().then(
  r => { process.stdout.write(JSON.stringify(r)); },
  e => { process.stderr.write(String(e && e.stack || e)); process.exit(1); },
);
"""


def _run(scenario, tmp_path):
    driver = tmp_path / "driver.js"
    driver.write_text(_DRIVER_SRC, encoding="utf-8")
    proc = subprocess.run(
        [NODE, str(driver), str(PANELS_JS_PATH), scenario],
        capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


@needs_node
def test_deferred_first_load_then_type_then_clear_renders_complete_list(tmp_path):
    r = _run("deferred_first_load_type_then_clear", tmp_path)
    assert r["clearVisibleWhilePending"], "typing before the first load must reveal the clear button"
    assert r["names"] == ["alpha", "beta", "gamma"], "a pre-load clear must not leave a filtered list"
    assert r["clearHiddenAfter"]
    assert r["listRequests"] == 1, "type/clear must not issue extra /api/crons fetches"


@needs_node
def test_typing_before_first_load_keeps_clear_visible_after_settlement(tmp_path):
    r = _run("deferred_first_load_type_only", tmp_path)
    assert r["clearVisibleWhilePending"] and r["clearVisibleAfter"], r
    assert r["names"] == ["alpha"], "first fetch must honour the pending (case-insensitive) query"
    assert r["query"] == "ALP"


@needs_node
def test_filter_keystrokes_do_not_rerender_detail_or_refetch_history(tmp_path):
    r = _run("typing_does_not_rerender_selected_detail", tmp_path)
    assert r["afterTyping"] == {"renders": 0, "history": 0, "names": ["daily-digest"]}, r
    assert r["rendersAfterNetworkRefresh"] == 1, "a real network refresh must still refresh the detail"


@needs_node
def test_superseded_manual_refresh_does_not_reenable_button(tmp_path):
    r = _run("superseded_manual_refresh_keeps_button_disabled", tmp_path)
    assert r["bothPending"] == {"disabled": True, "opacity": "0.5"}
    assert r["staleSettled"] == {"disabled": True, "opacity": "0.5"}, (
        "stale refresh A settling while B is pending must not restore the button"
    )
    assert r["namesAfterStale"] == [], "stale response must not render"
    assert r["final"] == {"disabled": False, "opacity": ""}, (
        "current owner B must restore the button even though it began non-animated"
    )
    assert r["finalNames"] == ["fresh-b"]


@needs_node
def test_paused_matches_are_visible_while_query_active_without_persisting(tmp_path):
    r = _run("paused_matches_are_visible_while_searching", tmp_path)

    def visible(rows):
        return sorted(x["name"] for x in rows if x["visible"])

    assert visible(r["noQuery"]) == ["alpha", "mix-active"], "paused stays collapsed by default"
    assert visible(r["pausedOnly"]) == ["paused-only"], "paused-only match hidden in a collapsed group"
    assert [x["name"] for x in r["activeOnly"]] == ["alpha"]
    assert visible(r["mixed"]) == ["mix-active", "mix-paused"]
    assert visible(r["cleared"]) == ["alpha", "mix-active"], "clearing returns to the saved collapsed state"
    assert r["savedPref"] is None, "query-forced open must not be persisted as the user's preference"


@needs_node
def test_empty_list_and_no_match_states_differ_and_filter_skips_network(tmp_path):
    r = _run("empty_list_vs_no_match", tmp_path)
    assert "No scheduled jobs found." in r["emptyHtml"]
    assert "No jobs match your search." in r["noMatchHtml"]
    assert "No scheduled jobs found." not in r["noMatchHtml"]
    assert r["listRequests"] == 2, "filtering must reuse the cached list, not refetch"
