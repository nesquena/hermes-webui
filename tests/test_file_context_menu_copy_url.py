"""File tree context menu: "Copy file URL" copies the absolute raw-file URL.

The behavioural tests build the real menu in Node: the route helpers from
static/workspace.js plus _workspaceContextMenuItem/_showFileContextMenu from
static/ui.js, over a minimal DOM stub, under a subpath document.baseURI.
"""
import json
import re
import shutil
import subprocess
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
        {"item": {"name": "a b.py", "path": "src/a b.py", "type": "file"}},
        {"item": {"name": "Page.HTM", "path": "docs/Page.HTM", "type": "file"}},
        {"item": {"name": "r.pdf", "path": "r.pdf", "type": "file"}},
        # The server types a symlink by its resolved target's suffix.
        {"item": {"name": "index", "path": "index", "type": "symlink",
                  "target": "/ws/build/index.html", "is_dir": False}},
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
        {"item": {"name": "f.txt", "path": "ext/f.txt", "type": "file"}, "grantRoot": "ext"},
        {"item": {"name": "f.txt", "path": "ext/f.txt", "type": "file"}, "grantAfterOpen": "ext"},
        # Without an http(s) base the route stays "/api/..." — not a usable URL.
        {"item": {"name": "f.txt", "path": "f.txt", "type": "file"}, "baseURI": "about:blank"},
    ])
    assert external == NOT_OFFERED
    assert escaped == NOT_OFFERED
    failed = {"offered": True, "copied": [], "toasts": ["file_url_copy_failed"]}
    assert late_grant == failed
    assert no_http == failed


def test_copy_file_url_strings_exist_in_every_locale():
    blocks = re.split(r"^  '?[a-zA-Z-]+'?: \{$", I18N_JS, flags=re.M)[1:]
    assert len(blocks) >= 15
    for block in blocks:
        for key in ("copy_file_url", "file_url_copied", "file_url_copy_failed"):
            assert len(re.findall(rf"^\s+{key}: '", block, re.M)) == 1, key
