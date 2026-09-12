"""Pagination for workspace directory listings (#6645).

list_dir() used to silently stop at 200 entries with no signal in the
response, so the Workspace → Files pane rendered incomplete listings with
no indication anything was missing. It now returns a page dict with
entries / total / has_more / limit / offset, and the route exposes
offset/limit query params.

The file also carries the production-function Node/DOM lifecycle regression
requested in review: it loads the REAL ``_appendWsLoadMoreRow()`` from
``static/ui.js`` together with the REAL ``_wsStoreDirListing()`` /
``_wsLoadMoreDirEntries()`` (and route helpers) from ``static/workspace.js``,
renders the load-more row into a minimal DOM, clicks it, and proves the
request / append / dedupe / re-render / subsequent-page / error-recovery
lifecycle — the regression that catches the original one-line
loading-ownership bug (#6685 re-gate rounds).
"""

import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse

import pytest

from api import routes
from api.workspace import dir_signature, list_dir

REPO_ROOT = Path(__file__).resolve().parents[1]
UI_JS = REPO_ROOT / "static" / "ui.js"
WORKSPACE_JS = REPO_ROOT / "static" / "workspace.js"
NODE = shutil.which("node")


def _make_dir(tmp_path, names):
    d = tmp_path / "ws"
    d.mkdir(exist_ok=True)
    for n in names:
        (d / n).write_text("x", encoding="utf-8")
    return d


def test_list_dir_reports_total_and_has_more(tmp_path):
    d = _make_dir(tmp_path, [f"f{i:03}.txt" for i in range(5)])
    result = list_dir(d, ".")
    assert len(result["entries"]) == 5
    assert result["total"] == 5
    assert result["has_more"] is False
    assert result["offset"] == 0
    assert result["limit"] == 200


def test_list_dir_paginates_with_offset_and_limit(tmp_path):
    d = _make_dir(tmp_path, [f"f{i:03}.txt" for i in range(5)])
    page1 = list_dir(d, ".", limit=2, offset=0)
    assert [e["name"] for e in page1["entries"]] == ["f000.txt", "f001.txt"]
    assert page1["total"] == 5
    assert page1["has_more"] is True

    page2 = list_dir(d, ".", limit=2, offset=2)
    assert [e["name"] for e in page2["entries"]] == ["f002.txt", "f003.txt"]
    assert page2["has_more"] is True

    page3 = list_dir(d, ".", limit=2, offset=4)
    assert [e["name"] for e in page3["entries"]] == ["f004.txt"]
    assert page3["has_more"] is False

    past = list_dir(d, ".", limit=2, offset=9)
    assert past["entries"] == []
    assert past["has_more"] is False
    assert past["total"] == 5


def test_list_dir_default_cap_surfaces_truncation(tmp_path):
    """The default page is still bounded at 200, but truncation is explicit."""
    d = _make_dir(tmp_path, [f"f{i:04}.txt" for i in range(205)])
    result = list_dir(d, ".")
    assert len(result["entries"]) == 200
    assert result["total"] == 205
    assert result["has_more"] is True

    tail = list_dir(d, ".", offset=200)
    assert len(tail["entries"]) == 5
    assert tail["has_more"] is False


def test_list_dir_limit_none_returns_full_listing(tmp_path):
    d = _make_dir(tmp_path, [f"f{i:04}.txt" for i in range(205)])
    result = list_dir(d, ".", limit=None)
    assert len(result["entries"]) == 205
    assert result["total"] == 205
    assert result["has_more"] is False


def test_list_dir_clamps_bad_params(tmp_path):
    d = _make_dir(tmp_path, ["a.txt"])
    assert list_dir(d, ".", offset=-3)["offset"] == 0
    # negative limit falls back to the default page size
    assert list_dir(d, ".", limit=-1)["limit"] == 200


def test_dir_signature_covers_full_listing_when_entries_omitted(tmp_path):
    """dir_signature() without entries hashes the whole directory (no 200 cap),
    so a change past the first page still invalidates the signature."""
    d = _make_dir(tmp_path, [f"f{i:04}.txt" for i in range(205)])
    full = list_dir(d, ".", limit=None)["entries"]
    assert len(full) == 205
    assert dir_signature(d, ".") == dir_signature(d, ".", full)
    # the default page (first 200) hashes to a DIFFERENT signature
    assert dir_signature(d, ".") != dir_signature(d, ".", list_dir(d, ".")["entries"])


def test_handle_list_dir_exposes_pagination(monkeypatch, tmp_path):
    ws = _make_dir(tmp_path, [f"f{i:04}.txt" for i in range(205)])
    session = SimpleNamespace(session_id="sess-page", workspace=str(ws), profile=None)
    monkeypatch.setattr(routes, "get_session", lambda _sid: session)
    monkeypatch.setattr(
        routes,
        "resolve_implicit_workspace_with_recovery",
        lambda candidate, _fallback: (Path(str(candidate)), False),
    )
    monkeypatch.setattr(routes, "j", lambda _handler, payload, **_kwargs: payload)

    payload = routes._handle_list_dir(
        object(), urlparse("/api/list?session_id=sess-page&path=.")
    )
    assert len(payload["entries"]) == 200
    assert payload["total"] == 205
    assert payload["has_more"] is True
    assert payload["offset"] == 0

    page2 = routes._handle_list_dir(
        object(), urlparse("/api/list?session_id=sess-page&path=.&offset=200")
    )
    assert len(page2["entries"]) == 5
    assert page2["total"] == 205
    assert page2["has_more"] is False
    assert page2["offset"] == 200

    # bad offset values fall back to page 1 instead of erroring
    safe = routes._handle_list_dir(
        object(), urlparse("/api/list?session_id=sess-page&path=.&offset=notanumber")
    )
    assert safe["offset"] == 0
    assert len(safe["entries"]) == 200


# ── Production-function Node/DOM lifecycle regression (#6645 re-gate) ─────────
# The maintainer review requires a regression that CLICKS the rendered
# load-more row and executes the REAL production functions together:
#   _appendWsLoadMoreRow()      (static/ui.js)
#   _wsStoreDirListing()        (static/workspace.js)
#   _wsLoadMoreDirEntries()     (static/workspace.js)
#   _workspaceRouteForPath*()   (static/workspace.js — normal/escape parity)
# The blocks below are sliced straight out of the shipped files at test time,
# so the lifecycle under test is the production code, not a copy.


def _read_ui_js() -> str:
    with open(UI_JS, encoding="utf-8") as f:
        return f.read()


def _read_workspace_js() -> str:
    with open(WORKSPACE_JS, encoding="utf-8") as f:
        return f.read()


def _ws_pagination_block() -> str:
    """REAL _WS_PAGE_DEFAULT_LIMIT + _wsStoreDirListing + _wsLoadMoreDirEntries."""
    src = _read_workspace_js()
    start = src.find("const _WS_PAGE_DEFAULT_LIMIT = 200;")
    assert start >= 0, "_WS_PAGE_DEFAULT_LIMIT not found in static/workspace.js"
    end = src.find("if(typeof window !== 'undefined'){", start)
    assert end >= 0, "pagination block end not found in static/workspace.js"
    return src[start:end]


def _ws_route_block() -> str:
    """REAL _workspaceRouteForPath + _workspaceRouteForPathRel (route parity)."""
    src = _read_workspace_js()
    start = src.find("function _workspaceRouteForPath(path, kind, opts={}){")
    assert start >= 0, "_workspaceRouteForPath not found in static/workspace.js"
    end = src.find("async function authorizeWorkspaceEscapeNavigation", start)
    assert end >= 0, "route block end not found in static/workspace.js"
    return src[start:end]


def _append_ws_load_more_row_block() -> str:
    """REAL _appendWsLoadMoreRow() from static/ui.js (balanced-brace slice)."""
    src = _read_ui_js()
    marker = "function _appendWsLoadMoreRow(container, dirPath, meta, depth){"
    start = src.find(marker)
    assert start >= 0, "_appendWsLoadMoreRow not found in static/ui.js"
    i = start + len(marker) - 1  # index of the opening '{'
    depth = 1
    while depth:
        i += 1
        c = src[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
    return src[start : i + 1]


def _run_load_more_lifecycle(mode: str = "happy") -> dict:
    """Drive the production load-more lifecycle in a Node VM.

    mode="happy":   click → page 2 (with a duplicate) → click → page 3 → done.
    mode="failure": click → request rejects → retry click → page 2.
    mode="escape":  as happy, but with an active escape grant (route parity).
    """
    payload = {
        "wsBlock": _ws_pagination_block(),
        "routeBlock": _ws_route_block(),
        "uiBlock": _append_ws_load_more_row_block(),
        "mode": mode,
    }
    js = (
        "const params = " + json.dumps(payload) + ";\n"
        + r"""
const wsBlock = params.wsBlock;
const routeBlock = params.routeBlock;
const uiBlock = params.uiBlock;
const mode = params.mode;

// Minimal DOM shim — the production functions only need createElement/appendChild.
const document = {
  createElement: (tag) => {
    const el = {
      tagName: String(tag).toUpperCase(),
      className: '', textContent: '', title: '', type: '', disabled: false,
      style: {}, children: [], parentNode: null,
      appendChild(child) { this.children.push(child); child.parentNode = this; return child; },
    };
    return el;
  },
};

// renderFileTree mirrors the real re-render: it re-reads FRESH S._dirMeta and
// re-appends the load-more row while has_more is still true (this is what the
// real tree does after each page, and it is what makes a subsequent page
// clickable after _wsStoreDirListing replaced the metadata object).
let renderCalls = 0;
const treeBox = { children: [] };
treeBox.appendChild = (child) => { treeBox.children.push(child); child.parentNode = treeBox; return child; };
const renderFileTree = () => {
  renderCalls++;
  treeBox.children = [];
  const meta = (S._dirMeta || {})[S.currentDir || '.'];
  if (meta && meta.has_more) apiFns._appendWsLoadMoreRow(treeBox, S.currentDir || '.', meta, 0);
};

// State under test: page 1 already rendered (2 of 6 entries, limit 2).
const page1 = [
  { path: 'a.txt', name: 'a.txt', type: 'file' },
  { path: 'b.txt', name: 'b.txt', type: 'file' },
];
const S = {
  session: { session_id: 'sess-1' },
  currentDir: '.',
  entries: page1.slice(),
  _dirCache: { '.': page1.slice() },
  _dirMeta: { '.': { total: 6, has_more: true, offset: 0, limit: 2, loading: false } },
  _escapeGrants: Object.create(null),
};

const apiCalls = [];
const deferreds = [];
const api = (route) => {
  apiCalls.push(route);
  return new Promise((resolve, reject) => deferreds.push({ resolve, reject }));
};
const toasts = [];
const showToast = (...args) => toasts.push(args);
const t = (key) => key;
const _visibleWorkspaceEntries = (entries) => entries;
const _normalizeWorkspaceRelPath = (p) =>
  p === '.' ? '.' : String(p || '').replace(/\\/g, '/').replace(/^\/+/, '');
const _workspaceEscapeGrantForPath = () => (mode === 'escape' ? { token: 'tok-9' } : null);

const runner = new Function(
  'S', 'api', 'showToast', 't', 'document', 'renderFileTree',
  '_visibleWorkspaceEntries', '_normalizeWorkspaceRelPath', '_workspaceEscapeGrantForPath',
  wsBlock + '\n' + routeBlock + '\n' + uiBlock + '\n'
    + '; return { _wsStoreDirListing, _wsLoadMoreDirEntries, _appendWsLoadMoreRow };'
);
const apiFns = runner(
  S, api, showToast, t, document, renderFileTree,
  _visibleWorkspaceEntries, _normalizeWorkspaceRelPath, _workspaceEscapeGrantForPath
);

const currentButton = () => {
  const row = treeBox.children[0];
  return row && row.children[0];
};
const clickCurrentRow = () => {
  const btn = currentButton();
  if (!btn) throw new Error('no load-more button rendered');
  btn.onclick({ stopPropagation() {} });
  return btn;
};
const flush = () => new Promise((resolve) => setTimeout(resolve, 0));

(async () => {
  const out = {};

  // Render the load-more row with the production function and click it.
  const meta0 = S._dirMeta['.'];
  apiFns._appendWsLoadMoreRow(treeBox, '.', meta0, 0);
  const btn1 = clickCurrentRow();
  out.click1 = { buttonDisabled: btn1.disabled, buttonText: btn1.textContent };

  // Click again while in flight: the callee's loading guard must suppress the
  // duplicate — exactly one request survives the double click.
  clickCurrentRow();
  out.apiCallsAfterClick1 = apiCalls.slice();

  if (mode === 'failure') {
    deferreds[0].reject(new Error('boom'));
  } else {
    // Page 2: c, d appended; b arrives again and must be deduplicated.
    deferreds[0].resolve({
      entries: [
        { path: 'c.txt', name: 'c.txt', type: 'file' },
        { path: 'd.txt', name: 'd.txt', type: 'file' },
        { path: 'b.txt', name: 'b.txt', type: 'file' },
      ],
      total: 6, has_more: true, offset: 2, limit: 2,
    });
  }
  await flush();
  await flush();

  out.afterFirstResolve = {
    apiCalls: apiCalls.slice(),
    entries: S.entries.map((e) => e.path),
    cache: (S._dirCache['.'] || []).map((e) => e.path),
    // Snapshot the meta object: after a LATER click, _wsLoadMoreDirEntries sets
    // loading=true on the current object, and the pre-replacement object that
    // was live here is allowed to keep a stale flag (the real tree discards it
    // on re-render). A live reference would serialize the later mutation.
    meta: Object.assign({}, S._dirMeta['.']),
    renderCalls: renderCalls,
    toasts: toasts.slice(),
  };

  if (mode === 'failure') {
    // Failed request must restore retryable state: loading cleared, tree
    // re-rendered with an enabled button, and a new click issues a NEW request.
    out.retry = { buttonDisabled: currentButton().disabled, renderCalls: renderCalls };
    clickCurrentRow();
    out.retryClick = { apiCalls: apiCalls.slice() };
    deferreds[1].resolve({
      entries: [
        { path: 'c.txt', name: 'c.txt', type: 'file' },
        { path: 'd.txt', name: 'd.txt', type: 'file' },
      ],
      total: 6, has_more: true, offset: 2, limit: 2,
    });
    await flush();
    await flush();
    out.afterRetryResolve = {
      apiCalls: apiCalls.slice(),
      entries: S.entries.map((e) => e.path),
      meta: Object.assign({}, S._dirMeta['.']),
    };
  } else {
    // Subsequent page after metadata replacement — the original lifecycle
    // drift: the OLD captured meta kept loading=true forever, so no further
    // page could ever be requested. The fresh row must issue offset=4.
    const btn2 = clickCurrentRow();
    out.click2 = { buttonDisabled: btn2.disabled, apiCalls: apiCalls.slice() };
    deferreds[1].resolve({
      entries: [
        { path: 'e.txt', name: 'e.txt', type: 'file' },
        { path: 'f.txt', name: 'f.txt', type: 'file' },
      ],
      total: 6, has_more: false, offset: 4, limit: 2,
    });
    await flush();
    await flush();
    out.afterSecondResolve = {
      apiCalls: apiCalls.slice(),
      entries: S.entries.map((e) => e.path),
      meta: Object.assign({}, S._dirMeta['.']),
      renderCalls: renderCalls,
    };
    // Last page loaded: the row is gone and no further request can fire.
    out.afterLastPage = {
      hasRow: treeBox.children.length > 0,
      apiCalls: apiCalls.slice(),
    };
  }

  console.log(JSON.stringify(out));
})().catch((err) => {
  console.error(err && err.stack ? err.stack : String(err));
  process.exit(1);
});
"""
    )
    r = subprocess.run([NODE, "-e", js], capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise RuntimeError(f"node failed: {r.stderr}\nstdout: {r.stdout}")
    return json.loads(r.stdout.strip().splitlines()[-1])


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
class TestWsLoadMoreNodeDOMLifecycle:
    """Click the rendered load-more row; prove the full page lifecycle.

    This is the regression requested by the maintainer: it executes the real
    ``_appendWsLoadMoreRow()`` together with the real
    ``_wsLoadMoreDirEntries()`` / ``_wsStoreDirListing()``, so the original
    one-line loading-ownership bug (click pre-set ``meta.loading`` → callee
    bailed → zero requests) cannot return unnoticed.
    """

    def test_click_issues_exactly_one_request_with_next_offset(self):
        out = _run_load_more_lifecycle("happy")
        assert out["apiCallsAfterClick1"] == [
            "/api/list?session_id=sess-1&path=.&offset=2"
        ], (
            "clicking the rendered load-more row must issue EXACTLY ONE request "
            "with the expected next offset; the pre-fix click pre-set "
            "meta.loading and issued ZERO requests. Got: "
            + str(out["apiCallsAfterClick1"])
        )
        # the button paints its loading state while the request is in flight
        assert out["click1"]["buttonDisabled"] is True
        assert out["click1"]["buttonText"] == "workspace_dir_loading"

    def test_entries_appended_and_deduplicated_then_tree_rerenders(self):
        out = _run_load_more_lifecycle("happy")
        after = out["afterFirstResolve"]
        assert after["entries"] == ["a.txt", "b.txt", "c.txt", "d.txt"], (
            "incoming page-2 entries must be appended to the listing and the "
            "duplicate 'b.txt' dropped. Got: " + str(after["entries"])
        )
        assert after["cache"] == ["a.txt", "b.txt", "c.txt", "d.txt"]
        assert after["meta"]["offset"] == 2
        assert after["meta"]["has_more"] is True
        assert after["meta"]["loading"] is False, (
            "metadata must be cleared after the page lands so the next click "
            "is not swallowed by a stale loading guard"
        )
        assert after["renderCalls"] >= 1, "tree must re-render after appending"

    def test_subsequent_page_requestable_after_metadata_replacement(self):
        out = _run_load_more_lifecycle("happy")
        assert out["click2"]["apiCalls"] == [
            "/api/list?session_id=sess-1&path=.&offset=2",
            "/api/list?session_id=sess-1&path=.&offset=4",
        ], (
            "after _wsStoreDirListing() replaced S._dirMeta['.'], a subsequent "
            "page must still be requestable at the next offset (the old "
            "captured-meta drift kept loading=true forever). Got: "
            + str(out["click2"]["apiCalls"])
        )
        after = out["afterSecondResolve"]
        assert after["entries"] == [
            "a.txt", "b.txt", "c.txt", "d.txt", "e.txt", "f.txt"
        ]
        assert after["meta"]["has_more"] is False
        assert after["meta"]["loading"] is False
        assert after["renderCalls"] >= 2
        # last page loaded → load-more row disappears and no further request fires
        assert out["afterLastPage"]["hasRow"] is False
        assert out["afterLastPage"]["apiCalls"] == out["click2"]["apiCalls"]

    def test_failed_request_restores_retryable_ui_state(self):
        out = _run_load_more_lifecycle("failure")
        after = out["afterFirstResolve"]
        assert after["meta"]["loading"] is False, (
            "a failed request must clear the loading guard so the row is retryable"
        )
        assert after["entries"] == ["a.txt", "b.txt"], "failed page must not append entries"
        assert after["renderCalls"] >= 1, "tree must re-render after the failure"
        assert after["toasts"] and after["toasts"][0][0] == "file_open_failed", (
            "failed request must surface the error toast. Got: " + str(after["toasts"])
        )
        # the re-rendered row is enabled and a new click issues a NEW request
        # (apiCalls accumulates the failed request + exactly one retry)
        assert out["retry"]["buttonDisabled"] is False
        assert out["retryClick"]["apiCalls"] == [
            "/api/list?session_id=sess-1&path=.&offset=2",
            "/api/list?session_id=sess-1&path=.&offset=2",
        ], (
            "after a failed request the loading guard must be cleared so a "
            "new click issues exactly ONE new request (not zero, not two). Got: "
            + str(out["retryClick"]["apiCalls"])
        )
        assert out["afterRetryResolve"]["entries"] == [
            "a.txt", "b.txt", "c.txt", "d.txt"
        ]
        assert out["afterRetryResolve"]["meta"]["loading"] is False

    def test_escape_route_parity_preserved(self):
        out = _run_load_more_lifecycle("escape")
        assert out["apiCallsAfterClick1"] == [
            "/api/escape/list?session_id=sess-1&path=.&token=tok-9&offset=2"
        ], (
            "the load-more flow must keep using the escape route when an escape "
            "grant is active. Got: " + str(out["apiCallsAfterClick1"])
        )
        # and the escape flow still appends + re-renders
        assert out["afterFirstResolve"]["entries"] == [
            "a.txt", "b.txt", "c.txt", "d.txt"
        ]
        assert out["afterFirstResolve"]["meta"]["loading"] is False
