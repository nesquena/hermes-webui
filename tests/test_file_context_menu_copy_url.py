"""File tree context menu: "Copy file URL" copies the absolute raw-file URL.

The behavioural tests build the real menu in Node: the route helpers from
static/workspace.js plus _workspaceContextMenuItem/_showFileContextMenu from
static/ui.js, over a minimal DOM stub, under a subpath document.baseURI.
"""
import json
import os
import re
import shutil
import socket
import subprocess
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
UI_JS = (ROOT / "static" / "ui.js").read_text(encoding="utf-8")
WORKSPACE_JS = (ROOT / "static" / "workspace.js").read_text(encoding="utf-8")
I18N_JS = (ROOT / "static" / "i18n.js").read_text(encoding="utf-8")
NODE = shutil.which("node")


def _function_source(src: str, name: str) -> str:
    start = src.index(f"function {name}(")
    depth = 0
    # Body starts at "){", past default parameters such as opts={}.
    for idx in range(src.index("){", start) + 1, len(src)):
        if src[idx] == "{":
            depth += 1
        elif src[idx] == "}":
            depth -= 1
            if depth == 0:
                return src[start : idx + 1]
    raise AssertionError(f"{name} body did not close")


WORKSPACE_FUNCS = (
    "_escapeGrantStore", "_normalizeWorkspaceRelPath", "_isSameOrChildPath",
    "_workspaceEscapeGrantForPath", "_workspaceRouteForPath", "_workspaceRouteForPathRel",
    "fileExt",
)
UI_FUNCS = ("_workspaceContextMenuItem", "_workspaceParentDir", "_showFileContextMenu")


def _menu_sources() -> str:
    html_exts = re.search(r"^const HTML_EXTS\s*=.*$", WORKSPACE_JS, re.M).group(0)
    funcs = [_function_source(WORKSPACE_JS, n) for n in WORKSPACE_FUNCS]
    funcs += [_function_source(UI_JS, n) for n in UI_FUNCS]
    return "\n".join([html_exts, *funcs])


HARNESS = r"""
const [sources, cases] = JSON.parse(require('fs').readFileSync(0, 'utf8'));
function el(tag){
  return {tag, children: [], style: {}, textContent: '', className: '',
    appendChild(c){ this.children.push(c); return c; }, remove(){}, setAttribute(){},
    getBoundingClientRect(){ return {left: 0, top: 0, right: 0, bottom: 0, width: 0, height: 0}; }};
}
const window = {innerWidth: 1000, innerHeight: 800};
const run = new Function(
  'S', 'document', 'window', 't', 'showToast', '_copyTextWithFallback', 'setTimeout',
  sources + '\nreturn {_showFileContextMenu};'
);
(async () => {
  const out = [];
  for (const c of cases) {
    const S = {session: {session_id: 'sess 1'}, currentDir: '.', _escapeGrants: Object.create(null)};
    const grant = {sessionId: 'sess 1', token: 'SECRET', expiresAt: 0};
    if (c.grantRoot) S._escapeGrants[c.grantRoot] = grant;
    const document = {
      baseURI: c.baseURI || 'https://host.example/hermes/',
      body: el('body'),
      createElement: el,
      querySelectorAll: () => [],
      addEventListener(){}, removeEventListener(){},
    };
    const copied = [];
    const toasts = [];
    const api = run(S, document, window, k => k, m => toasts.push(m),
      async (text) => { copied.push(text); }, () => {});
    api._showFileContextMenu({clientX: 10, clientY: 10}, c.item);
    const entry = document.body.children[0].children.find(x => x.textContent === 'copy_file_url');
    // A grant stored while the menu is open must still keep its token out.
    if (c.grantAfterOpen) S._escapeGrants[c.grantAfterOpen] = grant;
    if (entry) await entry.onclick();
    out.push({offered: !!entry, copied, toasts});
  }
  process.stdout.write(JSON.stringify(out));
})();
"""


def _run_menu(cases):
    payload = json.dumps([_menu_sources(), cases])
    proc = subprocess.run([NODE, "-e", HARNESS], input=payload, capture_output=True,
                          text=True, timeout=30, check=False)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


BASE = "https://host.example/hermes/api/file/raw?session_id=sess%201&path="
NOT_OFFERED = {"offered": False, "copied": [], "toasts": []}


def _copied(url):
    return {"offered": True, "copied": [url], "toasts": []}


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_copy_file_url_copies_absolute_subpath_url_for_files_only():
    file_res, html_res, pdf_res, html_link_res, dir_res, dir_link_res = _run_menu([
        {"item": {"name": "a b.py", "path": "src/a b.py", "type": "file", "is_regular": True}},
        {"item": {"name": "Page.HTM", "path": "docs/Page.HTM", "type": "file", "is_regular": True}},
        {"item": {"name": "r.pdf", "path": "r.pdf", "type": "file", "is_regular": True}},
        # The server types a symlink by its resolved target's suffix.
        {"item": {"name": "index", "path": "index", "type": "symlink",
                  "target": "/ws/build/index.html", "is_dir": False, "is_regular": True}},
        {"item": {"name": "src", "path": "src", "type": "dir"}},
        {"item": {"name": "lnk", "path": "lnk", "type": "symlink", "is_dir": True}},
    ])
    assert file_res == _copied(BASE + "src%2Fa%20b.py")
    # HTML gets inline=1 so the raw route renders it (sandboxed) instead of
    # downloading it; other types stay without the sandbox CSP.
    assert html_res == _copied(BASE + "docs%2FPage.HTM&inline=1")
    assert pdf_res == _copied(BASE + "r.pdf")
    assert html_link_res == _copied(BASE + "index&inline=1")
    assert dir_res == NOT_OFFERED
    assert dir_link_res == NOT_OFFERED


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_copy_file_url_never_copies_a_failing_relative_or_token_url():
    external, escaped, late_grant, no_http = _run_menu([
        # Symlink to a file outside the workspace: /api/file/raw cannot serve it.
        {"item": {"name": "ext", "path": "ext", "type": "symlink", "target_outside_workspace": True}},
        # Inside an escape grant: the URL would carry the grant token.
        {"item": {"name": "f.txt", "path": "ext/f.txt", "type": "file", "is_regular": True}, "grantRoot": "ext"},
        {"item": {"name": "f.txt", "path": "ext/f.txt", "type": "file", "is_regular": True}, "grantAfterOpen": "ext"},
        # Without an http(s) base the route stays "/api/..." — not a usable URL.
        {"item": {"name": "f.txt", "path": "f.txt", "type": "file", "is_regular": True}, "baseURI": "about:blank"},
    ])
    assert external == NOT_OFFERED
    assert escaped == NOT_OFFERED
    failed = {"offered": True, "copied": [], "toasts": ["file_url_copy_failed"]}
    assert late_grant == failed
    assert no_http == failed


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
@pytest.mark.skipif(not hasattr(os, "mkfifo") or not hasattr(socket, "AF_UNIX"),
                    reason="needs FIFOs and Unix sockets")
def test_copy_file_url_offered_exactly_where_the_raw_route_serves(tmp_path):
    """Rows come from the real listing and are checked against the real raw
    resolver: a FIFO or socket is listed as type 'file' and a link to one as
    !is_dir, but /api/file/raw serves none of them, so none may offer the item."""
    # AF_UNIX paths are capped at ~104 bytes and macOS's tmp_path is long, so
    # this workspace lives in a short directory.
    short = Path(tempfile.mkdtemp(prefix="cfu", dir="/tmp" if os.path.isdir("/tmp") else None))
    try:
        _check_against_raw_resolver(tmp_path, short / "ws")
    finally:
        shutil.rmtree(short, ignore_errors=True)


def _check_against_raw_resolver(tmp_path, ws):
    from types import SimpleNamespace

    from api.routes import _file_raw_target
    from api.workspace import list_dir, serialize_workspace_entries_for_browser

    (ws / "sub").mkdir(parents=True)
    (ws / "notes.txt").write_text("probe", encoding="utf-8")
    (ws / "page.html").write_text("<p>probe</p>", encoding="utf-8")
    os.mkfifo(ws / "worker.pipe")
    (ws / "pipe-link").symlink_to("worker.pipe")
    (ws / "notes-link").symlink_to("notes.txt")
    (ws / "sub-link").symlink_to("sub")
    outside = tmp_path / "outside.txt"
    outside.write_text("probe", encoding="utf-8")
    (ws / "outside-link").symlink_to(outside)
    sock = socket.socket(socket.AF_UNIX)
    sock.bind(str(ws / "svc.sock"))
    (ws / "sock-link").symlink_to("svc.sock")

    rows = {e["name"]: e for e in serialize_workspace_entries_for_browser(list_dir(ws, "."))}
    names = sorted(rows)
    assert names == ["notes-link", "notes.txt", "outside-link", "page.html",
                     "pipe-link", "sock-link", "sub", "sub-link", "svc.sock", "worker.pipe"]
    assert rows["worker.pipe"]["type"] == "file"  # why type alone is not enough
    assert rows["pipe-link"]["is_dir"] is False

    try:
        results = _run_menu([{"item": rows[n]} for n in names])
    finally:
        sock.close()
    session = SimpleNamespace(workspace=str(ws))
    for name, res in zip(names, results):
        served = _file_raw_target(session, "no-such-session", name) is not None
        assert res["offered"] is served, (name, rows[name], res)
        assert res["toasts"] == [], (name, res)
    offered = {n for n, r in zip(names, results) if r["offered"]}
    assert offered == {"notes-link", "notes.txt", "page.html"}
    assert dict(zip(names, results))["page.html"] == _copied(BASE + "page.html&inline=1")


def _http(path, body=None):
    from tests._pytest_port import BASE as SERVER

    req = urllib.request.Request(
        SERVER + path,
        data=None if body is None else json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs FIFOs")
def test_copy_file_url_matches_live_raw_responses(tmp_path):
    """Over real HTTP: rows from /api/list, and each offered URL's path
    returns 200 from /api/file/raw while every withheld one returns 404."""
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "notes.txt").write_text("probe", encoding="utf-8")
    os.mkfifo(ws / "worker.pipe")
    (ws / "pipe-link").symlink_to("worker.pipe")
    (ws / "notes-link").symlink_to("notes.txt")
    _http("/api/workspaces/add", {"path": str(ws)})
    sid = None
    try:
        status, body = _http("/api/session/new", {"workspace": str(ws)})
        assert status == 200, body
        sid = json.loads(body)["session"]["session_id"]
        _check_live_rows(sid)
    finally:
        # Leave no session or workspace pointing at a deleted tmp dir.
        if sid:
            _http("/api/session/delete", {"session_id": sid})
        _http("/api/workspaces/remove", {"path": str(ws)})


def _check_live_rows(sid):
    status, body = _http(f"/api/list?session_id={sid}&path=.")
    assert status == 200, body
    rows = sorted(json.loads(body)["entries"], key=lambda e: e["name"])
    assert [r["name"] for r in rows] == ["notes-link", "notes.txt", "pipe-link", "worker.pipe"]

    results = _run_menu([{"item": r} for r in rows])
    seen = {}
    for row, res in zip(rows, results):
        status, body = _http("/api/file/raw?session_id=" + urllib.parse.quote(sid)
                             + "&path=" + urllib.parse.quote(row["path"], safe=""))
        seen[row["name"]] = (res["offered"], status)
        if res["offered"]:
            assert res["copied"] == [BASE + urllib.parse.quote(row["path"], safe="")]
            assert body == b"probe"
    assert seen == {"notes-link": (True, 200), "notes.txt": (True, 200),
                    "pipe-link": (False, 404), "worker.pipe": (False, 404)}


def test_listing_marks_only_regular_files_and_links_to_them(tmp_path):
    from api.workspace import list_dir

    (tmp_path / "a.txt").write_text("x", encoding="utf-8")
    (tmp_path / "d").mkdir()
    try:
        (tmp_path / "a-link").symlink_to("a.txt")
        (tmp_path / "d-link").symlink_to("d")
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlink unavailable: {exc}")
    flags = {e["name"]: e.get("is_regular") for e in list_dir(tmp_path, ".")}
    assert flags == {"a.txt": True, "a-link": True, "d": False, "d-link": False}


def test_copy_file_url_strings_exist_in_every_locale():
    blocks = re.split(r"^  '?[a-zA-Z-]+'?: \{$", I18N_JS, flags=re.M)[1:]
    assert len(blocks) >= 15
    for block in blocks:
        for key in ("copy_file_url", "file_url_copied", "file_url_copy_failed"):
            assert len(re.findall(rf"^\s+{key}: '", block, re.M)) == 1, key


PAGE_SCRIPT = r"""
const S = {session: {session_id: 's'}, currentDir: '.', _escapeGrants: Object.create(null)};
const t = k => k + (window.__labelSuffix || '');
function showToast(){}
async function _copyTextWithFallback(){}
""" + "\n"


def _chromium():
    try:
        from playwright.sync_api import sync_playwright
    except Exception:  # pragma: no cover - dependency missing path
        pytest.skip("playwright is unavailable")
    playwright = sync_playwright().start()
    try:
        browser = playwright.chromium.launch(
            headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"]
        )
    except Exception as exc:  # pragma: no cover - browser binary missing
        playwright.stop()
        pytest.skip(f"chromium is unavailable: {exc}")
    return playwright, browser


# Right-click points near the bottom edge (the reviewer's measurements), a
# window too short for the whole menu, which must scroll instead, and the right
# edge with labels as long as the longest locales' (wider than the old guess);
# the bottom-right corner, where a plain clamp would put the pointer on Delete.
@pytest.mark.parametrize("width,height,x,y,suffix", [
    (1440, 900, 1184, 600, ""),
    (1440, 900, 1400, 880, ""),
    (900, 700, 700, 650, ""),
    (390, 844, 200, 800, ""),
    (1440, 320, 400, 300, ""),
    (390, 844, 385, 400, " des Dateibaums"),
])
def test_file_menu_near_bottom_edge_keeps_delete_reachable(width, height, x, y, suffix):
    style_css = (ROOT / "static" / "style.css").read_text(encoding="utf-8")
    playwright, browser = _chromium()
    try:
        page = browser.new_page(viewport={"width": width, "height": height})
        page.set_content(
            f"<!doctype html><html><head><style>{style_css}</style></head><body>"
            f"<script>{PAGE_SCRIPT}{_menu_sources()}</script></body></html>"
        )
        state = page.evaluate(
            """([x, y, suffix]) => {
              window.__labelSuffix = suffix;
              _showFileContextMenu({clientX: x, clientY: y},
                                   {name: 'notes.txt', path: 'docs/notes.txt', type: 'file', is_regular: true});
              const menu = document.querySelector('.file-ctx-menu');
              const last = menu.lastElementChild;
              const underPointer = document.elementFromPoint(x, y);
              const pointerOnDelete = !!underPointer && last.contains(underPointer);
              const before = last.getBoundingClientRect();
              last.scrollIntoView({block: 'nearest'});
              const after = last.getBoundingClientRect();
              const m = menu.getBoundingClientRect();
              // Every label on one line and none clipped: the menu got its full width.
              const wide = [...menu.children].filter(c => c.tagName !== 'HR').every(c => {
                const r = document.createRange();
                r.selectNodeContents(c);
                const tops = new Set([...r.getClientRects()].map(q => Math.round(q.top)));
                return tops.size === 1 && c.scrollWidth <= c.clientWidth;
              });
              return {pointerOnDelete, left: m.left, right: m.right, vw: innerWidth, wide,label: last.textContent, items: menu.children.length,
                      before: [before.top, before.bottom], after: [after.top, after.bottom],
                      menu: [m.top, m.bottom], scrolls: menu.scrollHeight > menu.clientHeight,
                      vh: innerHeight};
            }""",
            [x, y, suffix],
        )
    finally:
        browser.close()
        playwright.stop()
    vh = state["vh"]
    assert state["label"] == "delete_title" + suffix, state
    # The whole menu stays inside the viewport at every size.
    assert 0 <= state["menu"][0] and state["menu"][1] <= vh, state
    assert 0 <= state["left"] and state["right"] <= state["vw"], state
    assert state["wide"], state
    # The menu opens away from the pointer, so a stray click cannot hit Delete.
    assert not state["pointerOnDelete"], state
    if state["scrolls"]:
        # Too tall to fit: reachable by scrolling the menu itself.
        assert state["menu"][0] <= state["after"][0] and state["after"][1] <= state["menu"][1], state
    else:
        # Fits: Delete is visible with no scrolling at all.
        assert 0 <= state["before"][0] and state["before"][1] <= vh, state
