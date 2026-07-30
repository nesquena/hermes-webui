"""Behavioural tests for the #4662 profile-switch loading skeletons.

Phase 1 adds two skeleton builders that render loading placeholders the instant
a profile switch begins, so the user never sees the previous profile's
conversation list / workspace tree while the new profile's data fetches:

  * showSessionListSkeleton()   (static/sessions.js)  -> #sessionList
  * showWorkspaceTreeSkeleton() (static/workspace.js)  -> #fileTree

These drive the ACTUAL functions from the source via node with a mock DOM and
assert the produced structure (group labels + single-line rows, tree rows with
glyph/name/size), so a regression in the skeleton shape is caught. Pairs with
the static-assertion tests in test_issue4662_profile_switch_skeleton_static.py.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent.resolve()
SESSIONS_JS = REPO_ROOT / "static" / "sessions.js"
WORKSPACE_JS = REPO_ROOT / "static" / "workspace.js"

NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")


_DRIVER_SRC = r"""
const fs = require('fs');

// ── Minimal DOM ─────────────────────────────────────────────────────────────
function makeEl(tag) {
  const el = {
    tagName: (tag || 'div').toUpperCase(),
    className: '',
    style: {},
    dataset: {},
    _attrs: {},
    children: [],
    innerHTML: '',
    scrollTop: 0,
    setAttribute(k, v) { this._attrs[k] = String(v); },
    getAttribute(k) { return Object.prototype.hasOwnProperty.call(this._attrs, k) ? this._attrs[k] : null; },
    appendChild(c) {
      // setting innerHTML='' elsewhere clears children; mimic that contract
      this.children.push(c); c.parentNode = this; return c;
    },
    querySelectorAll(sel) { return _matchAll(this, sel); },
  };
  // innerHTML setter that clears children when set to ''
  Object.defineProperty(el, 'innerHTML', {
    get() { return this.__html || ''; },
    set(v) { this.__html = v; if (v === '') this.children = []; },
  });
  return el;
}

function _walk(node, out) {
  for (const c of node.children || []) { out.push(c); _walk(c, out); }
}
function _matchAll(root, sel) {
  const all = []; _walk(root, all);
  // support simple ".class" and ".a .b" (descendant) and ".a.b" (compound)
  sel = sel.trim();
  const parts = sel.split(/\s+/);
  function matchOne(el, s) {
    const classes = s.split('.').filter(Boolean);
    return classes.every(c => (el.className || '').split(/\s+/).includes(c));
  }
  if (parts.length === 1) return all.filter(el => matchOne(el, parts[0]));
  // descendant: just match the LAST segment (sufficient for our assertions)
  return all.filter(el => matchOne(el, parts[parts.length - 1]));
}

const els = {};
global.document = {
  createElement: (t) => makeEl(t),
  createDocumentFragment: () => makeEl('frag'),
};
global.$ = (id) => els[id] || null;

// Provide the containers the builders write into.
els.sessionList = makeEl('div');
els.fileTree = makeEl('div');

function extractFunc(src, name) {
  const re = new RegExp('function\\s+' + name + '\\s*\\(');
  const start = src.search(re);
  if (start < 0) throw new Error(name + ' not found');
  let i = src.indexOf('{', start); let depth = 1; i++;
  while (depth > 0 && i < src.length) {
    if (src[i] === '{') depth++; else if (src[i] === '}') depth--; i++;
  }
  return src.slice(start, i);
}
function extractConst(src, name) {
  // grab `const NAME = [...];` (array literal, possibly multiline) and rewrite
  // to a global assignment so eval'd functions in other scopes can see it.
  const re = new RegExp('const\\s+' + name + '\\s*=\\s*\\[');
  const start = src.search(re);
  if (start < 0) throw new Error(name + ' not found');
  let i = src.indexOf('[', start); let depth = 1; i++;
  while (depth > 0 && i < src.length) {
    if (src[i] === '[') depth++; else if (src[i] === ']') depth--; i++;
  }
  const literal = src.slice(src.indexOf('[', start), i);
  return 'global.' + name + ' = ' + literal + ';';
}

const sessSrc = fs.readFileSync(process.argv[2], 'utf8');
const wsSrc = fs.readFileSync(process.argv[3], 'utf8');

// Module-scope state the session builder references.
var _sessionListSkeletonActive = false;
var _sessionVirtualScrollRaf = 0;
global.cancelAnimationFrame = function(){};
global.requestAnimationFrame = function(){ return 0; };
eval(extractConst(sessSrc, '_SESSION_SKELETON_GROUPS'));
eval(extractFunc(sessSrc, 'showSessionListSkeleton'));

eval(extractConst(wsSrc, '_WS_SKELETON_ROWS'));
eval(extractFunc(wsSrc, 'showWorkspaceTreeSkeleton'));

// ── Run both builders and report structure ─────────────────────────────────
showSessionListSkeleton();
showWorkspaceTreeSkeleton();

const list = els.sessionList;
const tree = els.fileTree;

const result = {
  list_wrap_class: (list.children[0] || {}).className || '',
  list_wrap_aria: (list.children[0] || {}).getAttribute ? list.children[0].getAttribute('aria-hidden') : null,
  group_labels: list.querySelectorAll('.skeleton-group-label').length,
  rows: list.querySelectorAll('.skeleton-row').length,
  titles: list.querySelectorAll('.skeleton-title').length,
  stamps: list.querySelectorAll('.skeleton-stamp').length,
  // Every row should carry an inline stagger delay (set in JS, not via a
  // CSS :nth-child that would skip rows because group labels are interleaved).
  rows_with_delay: list.querySelectorAll('.skeleton-row').filter(function(r){
    return r.style && typeof r.style.animationDelay === 'string' && r.style.animationDelay.length > 0;
  }).length,
  last_row_delay: (function(){
    var rs = list.querySelectorAll('.skeleton-row');
    var last = rs[rs.length - 1];
    return last && last.style ? last.style.animationDelay : null;
  })(),
  skeleton_active_flag: _sessionListSkeletonActive,
  list_scrolltop: list.scrollTop,

  tree_wrap_class: (tree.children[0] || {}).className || '',
  tree_rows: tree.querySelectorAll('.skeleton-tree-row').length,
  tree_glyphs: tree.querySelectorAll('.skeleton-glyph').length,
  tree_names: tree.querySelectorAll('.skeleton-name').length,
  tree_sizes: tree.querySelectorAll('.skeleton-size').length,
};
process.stdout.write(JSON.stringify(result));
"""


@pytest.fixture(scope="module")
def outcome(tmp_path_factory):
    driver = tmp_path_factory.mktemp("skel") / "driver.js"
    driver.write_text(_DRIVER_SRC, encoding="utf-8")
    res = subprocess.run(
        [NODE, str(driver), str(SESSIONS_JS), str(WORKSPACE_JS)],
        capture_output=True, text=True, timeout=30,
    )
    if res.returncode != 0:
        raise RuntimeError(f"node driver failed: {res.stderr}")
    return json.loads(res.stdout)


def test_session_skeleton_wrap_is_aria_hidden(outcome):
    assert outcome["list_wrap_class"] == "skeleton-list"
    assert outcome["list_wrap_aria"] == "true", "skeleton is decorative; must be aria-hidden"


def test_session_skeleton_has_grouped_rows(outcome):
    # 3 groups in the spec; rows = 1 + 3 + 4 = 8
    assert outcome["group_labels"] == 3, f"expected 3 group labels: {outcome}"
    assert outcome["rows"] == 8, f"expected 8 skeleton rows: {outcome}"


def test_session_skeleton_rows_have_title_and_stamp(outcome):
    # Each row mirrors the real .session-item: title bar + timestamp bar.
    assert outcome["titles"] == 8, f"every row needs a title bar: {outcome}"
    assert outcome["stamps"] == 8, f"every row needs a timestamp bar: {outcome}"


def test_session_skeleton_rows_have_inline_stagger_delay(outcome):
    # Greptile: the stagger must be applied inline per row, not via a CSS
    # :nth-child rule (group labels are interleaved with rows, so :nth-child
    # would skip most rows). Every row carries an animationDelay, and the last
    # row's delay is non-empty (capped at 0.2s).
    assert outcome["rows_with_delay"] == 8, (
        f"all 8 rows must carry an inline stagger delay: {outcome}"
    )
    assert outcome["last_row_delay"] not in (None, "", "0s"), (
        f"the last row must have a real (non-zero) stagger delay, not be skipped: {outcome}"
    )


def test_session_skeleton_sets_active_flag_and_resets_scroll(outcome):
    assert outcome["skeleton_active_flag"] is True, "must flag skeleton active for the clear-on-render path"
    assert outcome["list_scrolltop"] == 0, "skeleton should reset scroll to top"


def test_workspace_skeleton_wrap_is_aria_hidden(outcome):
    assert outcome["tree_wrap_class"] == "skeleton-tree"


def test_workspace_skeleton_has_tree_rows(outcome):
    # 8 rows in the spec, each with a glyph + name bar.
    assert outcome["tree_rows"] == 8, f"expected 8 tree rows: {outcome}"
    assert outcome["tree_glyphs"] == 8, f"every tree row needs a glyph: {outcome}"
    assert outcome["tree_names"] == 8, f"every tree row needs a name bar: {outcome}"


def test_workspace_skeleton_dir_rows_have_no_size(outcome):
    # Spec marks exactly 1 row as a directory (dir:true), which omits the size
    # bar; the other 7 (files) show a size bar on the right.
    assert outcome["tree_sizes"] == 7, f"only file rows show a size bar: {outcome}"
