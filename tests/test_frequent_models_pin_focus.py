"""Focus and scroll behaviour around a pin toggle (#8622).

Two Greptile P2 findings from the 2026-11-17 review, both still valid at head
``4b4473409``:

1. **Starring loses keyboard focus** (discussion_r4596348891).
   ``_refreshOpenFrequent()`` preserved focus only for ``.model-search-input``;
   a keyboard user who activated a star had the focused button destroyed by the
   rebuild, with focus falling back to ``<body>``.  Focus must be restored on
   the REPLACEMENT star identified by the same model+provider identity, and a
   user who moved focus elsewhere during the save must not have it stolen back.

2. **Changed rows stay offscreen** (discussion_r4596348896).
   ``_scrollFrequentRowIntoView()`` compared the caller's JSON key
   (``["p","model-a"]``) against the row's visible id text with plain /
   colon-suffix matching, so it could never match and ``scrollIntoView`` was
   never reached.  Matching must use the row's own model+provider identity
   through the SAME key function the caller used — never the visible text (the
   same id can appear under different providers) and never a selector built
   from the key (JSON/quote escaping would be an injection vector).

Test layers:

* Node-driven cases run the REAL helpers sliced verbatim out of
  ``static/ui.js`` (repo's own anchors), so CI without a browser still covers
  the pure identity/matching logic and the rendered star markup.
* Playwright cases run the same helpers in Chromium with a real DOM and
  real focus semantics. ``scrollIntoView`` is a call recorder, not a claim
  about viewport visibility. ``renderModelDropdown`` is a rebuild stand-in
  using the production star markup and bindings; full-page behavior is
  exercised separately by the existing end-to-end suite.
"""

from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
NODE = shutil.which("node")
UI = ROOT / "static" / "ui.js"

BLOCK_START = "// ── Frequently-used models + server-backed pin state"
BLOCK_END = "function renderModelDropdown(){"

# ── Node driver: real helper block, minimal DOM stand-ins ───────────────────

NODE_PRELUDE = """
const fs = require('fs');
const ui = fs.readFileSync(__UI__, 'utf8');
const block = ui.split(__BLOCK_START__)[1].split(__BLOCK_END__)[0];
if (!block || block.length < 1000) throw new Error('helper block not found');
const body = block.slice(block.indexOf(String.fromCharCode(10)) + 1);
if (!body || body.length < 1000) throw new Error('helper body not found');
const S = {activeProfile: 'default'};
const t = (k) => k;
const showToast = () => {};
const $ = () => ({});
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => (
  {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const li = (name, size) => '<svg data-li="' + name + '"></svg>';
let apiImpl = null;
function api(path, opts) { return apiImpl(path, opts); }
eval(body + `
globalThis.__hooks = {
  get cache() { return _modelFrequentCache; },
  set cache(v) { _modelFrequentCache = v; },
  key: _frequentModelKey,
  scroll: _scrollFrequentRowIntoView,
  starHtml: _rowPinStarHtml,
};
`);
"""

# Rows whose ONLY identity is the data-attribute pair the production
# _rowPinStarHtml renders. scrollIntoView records the row that was targeted.
NODE_SCROLL_CASE = """
function makeRow(model, provider, idText) {
  const btn = {getAttribute: (k) => k === 'data-pin-model' ? model
                                     : k === 'data-pin-provider' ? provider : null};
  return {
    querySelector: (sel) => sel === '.model-pin-star' ? btn
                           : sel === '.model-opt-id' ? {textContent: idText} : null,
    scrollIntoView: () => { globalThis.__scrolled.push({model, provider}); },
    classList: {add: () => {}},
  };
}
function run(key) {
  globalThis.__scrolled = [];
  const rows = [
    makeRow('model-a', 'p', 'model-a'),              // frequent-group row
    makeRow('model-a', 'p', 'model-a'),              // same row in its provider group
    makeRow('model-a', 'q', 'model-a'),              // same visible id, other provider
    makeRow('openrouter/auto', 'openrouter', 'openrouter/auto'), // namespaced model
  ];
  const dd = {querySelectorAll: (sel) => sel === '.model-opt' ? rows : []};
  __hooks.scroll(dd, key);
  return globalThis.__scrolled;
}
const out = {
  p: run(__hooks.key('model-a', 'p')),
  q: run(__hooks.key('model-a', 'q')),
  ns: run(__hooks.key('openrouter/auto', 'openrouter')),
  missing: run(__hooks.key('missing', 'p')),
  // Legacy plain id and a hostile string must not throw and must not match.
  hostile: run('model-a'),
  hostile2: run('["p","x\\" onload=alert(1)"]'),
};
console.log(JSON.stringify(out));
"""

NODE_STAR_HTML_CASE = """
const out = {
  plain: _rowPinStarHtml({value: '@p:model-a', providerId: 'p'}),
  ns: _rowPinStarHtml({value: 'openrouter/auto', providerId: 'openrouter'}),
  quoted: _rowPinStarHtml({value: 'a"b<c', providerId: 'p'}),
  noProvider: _rowPinStarHtml({value: 'x', providerId: ''}),
};
console.log(JSON.stringify(out));
"""


def _node_run(case_js: str) -> dict:
    assert NODE is not None, "node is required for the frequent-model helper driver"
    script = (
        NODE_PRELUDE.replace("__UI__", json.dumps(str(UI)))
        .replace("__BLOCK_START__", json.dumps(BLOCK_START))
        .replace("__BLOCK_END__", json.dumps(BLOCK_END))
        + case_js
    )
    env = dict(os.environ)
    env.pop("NODE_CHANNEL_FD", None)
    env.pop("NODE_OPTIONS", None)
    env.pop("NODE_CHANNEL_SERIALIZATION_MODE", None)
    proc = subprocess.run(
        [NODE, "-e", script], cwd=str(ROOT), text=True, capture_output=True,
        check=False, env=env,
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout
    return json.loads(proc.stdout.strip().splitlines()[-1])


@pytest.mark.skipif(NODE is None, reason="node is required for the frequent-model helper driver")
def test_changed_row_is_located_by_identity_not_visible_text():
    out = _node_run(NODE_SCROLL_CASE)
    assert out["p"] == [{"model": "model-a", "provider": "p"}], out
    assert out["q"] == [{"model": "model-a", "provider": "q"}], out
    assert out["ns"] == [{"model": "openrouter/auto", "provider": "openrouter"}], out
    assert out["missing"] == [], out
    assert out["hostile"] == [], out
    assert out["hostile2"] == [], out


@pytest.mark.skipif(NODE is None, reason="node is required for the frequent-model helper driver")
def test_star_markup_carries_the_pin_identity():
    out = _node_run(NODE_STAR_HTML_CASE)
    assert 'data-pin-model="@p:model-a"' in out["plain"], out
    assert 'data-pin-provider="p"' in out["plain"], out
    assert 'data-pin-model="openrouter/auto"' in out["ns"], out
    assert 'data-pin-model="a&quot;b&lt;c"' in out["quoted"], out
    assert out["noProvider"] == "", out


# ── Playwright: the same real helpers in a real browser ─────────────────────

PLAYWRIGHT_PRELUDE = """
const S = {activeProfile: 'default'};
const $ = id => document.getElementById(id);
const t = k => k;
const showToast = () => {};
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => (
  {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const li = (name, size) => '<svg data-li="' + name + '"></svg>';
const closeModelDropdown = () => {};
const closeSettingsModelDropdown = () => {};
const selectSettingsModelFromDropdown = () => {};
// Rebuild stand-in for the picker renderer: it reproduces the production
// teardown (dd.innerHTML='') and re-creates rows through the PRODUCTION
// _rowPinStarHtml / _bindRowStar from window.__rows. The full renderer needs
// the whole picker/catalog state; what these tests exercise is the focus and
// scroll behaviour of the real _refreshOpenFrequent around that rebuild.
window.renderModelDropdown = (opts) => {
  const dd = $((opts && opts.dropdownId) || 'composerModelDropdown');
  if (!dd) return;
  const specs = (window.__rows && window.__rows[dd.id]) || [];
  dd.innerHTML = '';
  const si = document.createElement('input');
  si.className = 'model-search-input';
  si.value = window.__term || '';
  dd.appendChild(si);
  for (const m of specs) {
    const row = document.createElement('div');
    row.className = 'model-opt';
    row.innerHTML = '<div class="model-opt-top">' + _rowPinStarHtml(m)
      + '</div><span class="model-opt-id">' + esc(m.id || m.value) + '</span>';
    if (typeof _bindRowStar === 'function') _bindRowStar(row, m);
    dd.appendChild(row);
  }
};
// Observe the target selected by the production helper; layout is not measured.
window.__scrolls = [];
Element.prototype.scrollIntoView = function () {
  const star = this.querySelector && this.querySelector('.model-pin-star');
  window.__scrolls.push({
    model: star && star.getAttribute('data-pin-model'),
    provider: star && star.getAttribute('data-pin-provider'),
    idText: (this.querySelector('.model-opt-id') || {}).textContent || '',
  });
};
"""


@pytest.fixture
def picker_page():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=['--no-sandbox', '--disable-dev-shm-usage'])
        page = browser.new_page()
        page.set_content(
            '<div id="composerModelDropdown" class="open"></div>'
            '<div id="settingsModelDropdown" class="open"></div>'
        )
        helpers = UI.read_text().split(BLOCK_START)[1].split(BLOCK_END)[0]
        page.add_script_tag(content=PLAYWRIGHT_PRELUDE + "\n// extracted production helper block\n//" + helpers)
        yield page
        browser.close()


def _install_rows(page, dropdown_id, rows, *, term=''):
    page.evaluate(
        """(args) => {
          window.__rows = {};
          window.__rows[args.id] = args.rows;
          window.__term = args.term;
          renderModelDropdown(args.id === 'settingsModelDropdown'
            ? {dropdownId: 'settingsModelDropdown', selectId: 'settingsModel',
               forceOpenKey: 'settingsModel', closeDropdown: closeSettingsModelDropdown,
               selectModel: selectSettingsModelFromDropdown}
            : {});
        }""",
        {'id': dropdown_id, 'rows': rows, 'term': term},
    )


def _pending_post(page, pins):
    """POST stand-in whose completion the test controls explicitly."""
    page.evaluate(
        """(pins) => {
          window.__pinPostCount = 0;
          window.api = (path, opts) => new Promise((res) => {
            window.__pinPostCount++;
            window.__resolvePost = () => res({profile: 'default', pins, usage: []});
          });
        }""",
        pins,
    )


def test_star_markup_carries_the_pin_identity_in_a_real_browser(picker_page):
    page = picker_page
    out = page.evaluate("""() => {
      const wrap = document.createElement('div');
      wrap.innerHTML = _rowPinStarHtml({value: '@p:model-a', providerId: 'p'})
        + _rowPinStarHtml({value: 'openrouter/auto', providerId: 'openrouter'})
        + _rowPinStarHtml({value: 'a"b<c', providerId: 'p'});
      const btns = wrap.querySelectorAll('.model-pin-star');
      const read = (i) => ({model: btns[i].getAttribute('data-pin-model'),
                            provider: btns[i].getAttribute('data-pin-provider')});
      return {first: read(0), ns: read(1), quoted: read(2),
              sameKey: _frequentModelKey(read(0).model, read(0).provider)
                       === _frequentModelKey('model-a', 'p'),
              nsKey: _frequentModelKey(read(1).model, read(1).provider)
                     === _frequentModelKey('openrouter/auto', 'openrouter')};
    }""")
    assert out['first'] == {'model': '@p:model-a', 'provider': 'p'}, out
    assert out['ns'] == {'model': 'openrouter/auto', 'provider': 'openrouter'}, out
    assert out['quoted'] == {'model': 'a"b<c', 'provider': 'p'}, out
    assert out['sameKey'] is True, out
    assert out['nsKey'] is True, out


def test_changed_row_is_scrolled_into_view_after_a_reorder(picker_page):
    page = picker_page
    _install_rows(page, 'composerModelDropdown', [
        {'value': 'model-a', 'id': 'model-a', 'providerId': 'p', 'name': 'model-a'},
        # The same model again under the same provider (frequent + provider
        # group duplicates) and under a DIFFERENT provider with the same id.
        {'value': 'model-a', 'id': 'model-a', 'providerId': 'p', 'name': 'model-a'},
        {'value': 'model-a', 'id': 'model-a', 'providerId': 'q', 'name': 'model-a'},
        {'value': 'openrouter/auto', 'id': 'openrouter/auto',
         'providerId': 'openrouter', 'name': 'auto'},
    ])
    page.evaluate("() => window.__scrolls = []")
    page.evaluate("() => _scrollFrequentRowIntoView($('composerModelDropdown'),"
                  " _frequentModelKey('model-a', 'p'))")
    assert page.evaluate("() => window.__scrolls") == [
        {'model': 'model-a', 'provider': 'p', 'idText': 'model-a'}]

    page.evaluate("() => {window.__scrolls = [];"
                  " _scrollFrequentRowIntoView($('composerModelDropdown'),"
                  " _frequentModelKey('model-a', 'q'));}")
    assert page.evaluate("() => window.__scrolls") == [
        {'model': 'model-a', 'provider': 'q', 'idText': 'model-a'}]

    page.evaluate("() => {window.__scrolls = [];"
                  " _scrollFrequentRowIntoView($('composerModelDropdown'),"
                  " _frequentModelKey('openrouter/auto', 'openrouter'));}")
    assert page.evaluate("() => window.__scrolls") == [
        {'model': 'openrouter/auto', 'provider': 'openrouter',
         'idText': 'openrouter/auto'}]

    page.evaluate("() => {window.__scrolls = [];"
                  " _scrollFrequentRowIntoView($('composerModelDropdown'),"
                  " _frequentModelKey('missing', 'p'));}")
    assert page.evaluate("() => window.__scrolls") == []

    out = page.evaluate("""() => {
      window.__scrolls = [];
      let threw = null;
      try {
        _scrollFrequentRowIntoView($('composerModelDropdown'), 'model-a');
        _scrollFrequentRowIntoView($('composerModelDropdown'), '["p","x\\" onload=alert(1)"]');
      } catch (e) { threw = String(e); }
      return {threw, scrolls: window.__scrolls};
    }""")
    assert out == {'threw': None, 'scrolls': []}, out


def test_keyboard_activated_star_keeps_focus_on_the_rebuilt_star(picker_page):
    page = picker_page
    _install_rows(page, 'composerModelDropdown', [
        {'value': 'model-a', 'id': 'model-a', 'providerId': 'p', 'name': 'model-a'},
        {'value': 'model-b', 'id': 'model-b', 'providerId': 'p', 'name': 'model-b'},
    ], term='mod')
    _pending_post(page, [{'model': 'model-a', 'provider': 'p'}])

    star = page.locator('#composerModelDropdown .model-pin-star').first
    star.focus()
    assert page.evaluate("() => document.activeElement.className") == 'model-pin-star'
    star.press(' ')  # keyboard activation of the switch role
    page.wait_for_function("() => window.__resolvePost")
    page.evaluate("() => window.__resolvePost()")
    page.wait_for_function(
        "() => document.activeElement"
        " && document.activeElement.classList.contains('model-pin-star')")

    out = page.evaluate("""() => ({
      cls: document.activeElement.className,
      model: document.activeElement.getAttribute('data-pin-model'),
      provider: document.activeElement.getAttribute('data-pin-provider'),
      checked: document.activeElement.getAttribute('aria-checked'),
      term: document.querySelector('.model-search-input').value,
      stillInPicker: document.activeElement.closest('#composerModelDropdown') !== null,
    })""")
    assert out['model'] == 'model-a', out
    assert out['provider'] == 'p', out
    assert out['checked'] == 'true', out
    assert out['term'] == 'mod', out
    assert out['stillInPicker'] is True, out


def test_star_focus_is_not_stolen_when_the_user_moved_focus_during_the_save(picker_page):
    page = picker_page
    _install_rows(page, 'composerModelDropdown', [
        {'value': 'model-a', 'id': 'model-a', 'providerId': 'p', 'name': 'model-a'},
        {'value': 'model-b', 'id': 'model-b', 'providerId': 'p', 'name': 'model-b'},
    ])
    _pending_post(page, [{'model': 'model-a', 'provider': 'p'}])

    star = page.locator('#composerModelDropdown .model-pin-star').first
    star.focus()
    star.press(' ')
    page.wait_for_function("() => window.__resolvePost")
    # The user moves focus elsewhere while the save is in flight.
    page.evaluate("() => document.querySelector('.model-search-input').focus()")
    page.evaluate("() => window.__resolvePost()")
    # Wait until the rebuild has happened (the pin is reflected on the row).
    page.wait_for_function(
        "() => {const s = document.querySelector('.model-pin-star');"
        " return s && s.getAttribute('aria-checked') === 'true';}")

    out = page.evaluate("""() => ({
      isSearch: document.activeElement.classList.contains('model-search-input'),
      isStar: document.activeElement.classList.contains('model-pin-star'),
    })""")
    assert out == {'isSearch': True, 'isStar': False}, out


def test_search_focus_and_term_survive_a_refresh_in_both_pickers(picker_page):
    page = picker_page
    for dropdown_id in ('composerModelDropdown', 'settingsModelDropdown'):
        _install_rows(page, dropdown_id, [
            {'value': 'model-a', 'id': 'model-a', 'providerId': 'p', 'name': 'model-a'},
        ], term='mod')
        page.evaluate(
            "() => document.querySelector('.model-search-input').focus()")
        page.evaluate("() => _refreshOpenFrequent()")
        out = page.evaluate("""() => ({
          isSearch: document.activeElement.classList.contains('model-search-input'),
          term: document.activeElement.value,
          inPicker: document.activeElement.closest('.open') !== null,
        })""")
        assert out == {'isSearch': True, 'term': 'mod', 'inPicker': True}, out


def test_pending_star_save_keeps_focus_and_rejects_repeat_activation(picker_page):
    page = picker_page
    _install_rows(page, 'composerModelDropdown', [
        {'value': 'model-a', 'id': 'model-a', 'providerId': 'p', 'name': 'model-a'},
    ])
    _pending_post(page, [{'model': 'model-a', 'provider': 'p'}])
    star = page.locator('#composerModelDropdown .model-pin-star').first
    star.focus()
    page.keyboard.press('Enter')
    page.wait_for_function('window.__pinPostCount === 1')
    assert star.evaluate('(el) => !el.disabled && document.activeElement === el')
    assert star.get_attribute('aria-disabled') == 'true'
    page.keyboard.press('Enter')
    assert page.evaluate('window.__pinPostCount') == 1
    page.evaluate('window.__resolvePost()')
    page.wait_for_function("document.activeElement?.getAttribute('aria-checked') === 'true'")
    assert star.get_attribute('aria-disabled') is None
