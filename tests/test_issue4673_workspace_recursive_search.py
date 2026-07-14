"""Functional regression coverage for backend-backed workspace search (#4673)."""

import os
import json
import io
import shutil
import subprocess
from pathlib import Path
from urllib.parse import urlparse

import pytest
import api.routes as routes
from api.workspace import search_workspace

NODE = shutil.which("node")


def test_search_finds_unopened_descendants_with_relative_paths(tmp_path):
    root = tmp_path / "workspace"
    (root / "one" / "two").mkdir(parents=True)
    (root / "one" / "two" / "needle.txt").write_text("ok", encoding="utf-8")

    payload = search_workspace(root, "NEEDLE")

    assert [row["path"] for row in payload["results"]] == ["one/two/needle.txt"]
    assert payload["truncated"] is False


def test_search_prunes_dot_and_cruft_entries_by_default(tmp_path):
    root = tmp_path / "workspace"
    (root / ".git" / "needle").mkdir(parents=True)
    (root / ".private").mkdir(parents=True)
    (root / ".private" / ".env").write_text("secret", encoding="utf-8")
    (root / "src" / "needle").mkdir(parents=True)

    visible = search_workspace(root, "needle")["results"]
    assert [row["path"] for row in visible] == ["src/needle"]
    hidden = search_workspace(root, "needle", include_hidden=True)["results"]
    assert {row["path"] for row in hidden} == {".git/needle", "src/needle"}
    assert [row["path"] for row in search_workspace(root, "env", include_hidden=True)["results"]] == [
        ".private/.env"
    ]


def test_search_rejects_traversal_and_outside_file_symlinks(tmp_path):
    root = tmp_path / "workspace"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (outside / "needle.txt").write_text("secret", encoding="utf-8")
    try:
        (root / "escape.txt").symlink_to(outside / "needle.txt")
    except (OSError, NotImplementedError):
        return

    assert search_workspace(root, "needle")["results"] == []
    assert search_workspace(root, "../")["results"] == []


def test_search_rejects_matching_name_outside_directory_symlinks(tmp_path):
    root = tmp_path / "workspace"
    outside = tmp_path / "outside"
    root.mkdir()
    (outside / "needle-child").mkdir(parents=True)
    (outside / "needle-child" / "needle.txt").write_text("secret", encoding="utf-8")
    try:
        (root / "needle-alias").symlink_to(outside / "needle-child", target_is_directory=True)
    except (OSError, NotImplementedError):
        return

    paths = {row["path"] for row in search_workspace(root, "needle")["results"]}
    assert paths == set()


def test_search_rejects_symlink_replaced_after_containment(tmp_path, monkeypatch):
    root = tmp_path / "workspace"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    inside = root / "inside.txt"
    inside.write_text("ok", encoding="utf-8")
    (outside / "needle.txt").write_text("secret", encoding="utf-8")
    alias = root / "needle-alias"
    try:
        alias.symlink_to(inside)
    except (OSError, NotImplementedError):
        return

    original = __import__("api.workspace", fromlist=["safe_resolve_ws"]).safe_resolve_ws
    swapped = False

    def resolve(workspace, requested):
        nonlocal swapped
        result = original(workspace, requested)
        if requested == "needle-alias" and not swapped:
            alias.unlink()
            alias.symlink_to(outside / "needle.txt")
            swapped = True
        return result

    monkeypatch.setattr("api.workspace.safe_resolve_ws", resolve)
    assert search_workspace(root, "needle")["results"] == []


def test_search_returns_in_workspace_file_symlinks_without_following_directories(tmp_path):
    root = tmp_path / "workspace"
    (root / "real").mkdir(parents=True)
    (root / "real" / "needle.txt").write_text("ok", encoding="utf-8")
    try:
        (root / "alias.txt").symlink_to(root / "real" / "needle.txt")
        (root / "alias-dir").symlink_to(root / "real", target_is_directory=True)
        (root / "real" / "cycle").symlink_to(root, target_is_directory=True)
    except (OSError, NotImplementedError):
        return

    paths = {row["path"] for row in search_workspace(root, "needle")["results"]}
    assert paths == {"real/needle.txt"}
    assert {row["path"] for row in search_workspace(root, "alias")["results"]} == {
        "alias-dir",
        "alias.txt",
    }
    assert all("cycle" not in path for path in paths)


def test_search_reports_entry_result_depth_time_and_directory_caps(tmp_path):
    root = tmp_path / "workspace"
    (root / "deep" / "child").mkdir(parents=True)
    for index in range(4):
        (root / f"needle-{index}").write_text("ok", encoding="utf-8")

    assert search_workspace(root, "needle", max_entries=2)["truncated"] is True
    assert search_workspace(root, "needle", max_results=2)["truncated"] is True
    assert search_workspace(root, "needle", max_depth=0)["truncated"] is True
    assert search_workspace(root, "needle", max_seconds=0)["truncated"] is True
    assert search_workspace(root, "needle", max_directory_entries=2)["truncated"] is True


def test_search_reports_truncated_when_entry_metadata_disappears(tmp_path, monkeypatch):
    root = tmp_path / "workspace"
    root.mkdir()

    class DisappearingEntry:
        name = "needle.txt"

        def stat(self, *, follow_symlinks=True):
            raise OSError("raced away")

        def is_symlink(self):
            return False

    class FakeScan:
        def __enter__(self):
            return self

        def __iter__(self):
            return iter([DisappearingEntry()])

        def __exit__(self, *_):
            return False

    monkeypatch.setattr("api.workspace._DIR_FD_OK", False)
    monkeypatch.setattr(os, "scandir", lambda _directory: FakeScan())

    payload = search_workspace(root, "needle")

    assert payload["results"] == []
    assert payload["truncated"] is True


def test_search_over_200_matches_is_bounded_and_truncated(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    for index in range(201):
        (root / f"needle-{index:03d}").write_text("ok", encoding="utf-8")

    payload = search_workspace(root, "needle")

    assert len(payload["results"]) == 200
    assert payload["truncated"] is True


def test_search_route_uses_file_ops_session_and_trusted_workspace(monkeypatch, tmp_path):
    calls = []
    session = type("Session", (), {"workspace": str(tmp_path)})()

    monkeypatch.setattr(routes, "get_session_for_file_ops", lambda sid: calls.append(("session", sid)) or session)
    monkeypatch.setattr(
        routes,
        "resolve_trusted_workspace",
        lambda workspace: calls.append(("trust", workspace)) or tmp_path,
    )
    monkeypatch.setattr(routes, "search_workspace", lambda root, query, **kwargs: {"results": [], "truncated": False})
    monkeypatch.setattr(routes, "j", lambda handler, payload, **kwargs: payload)
    monkeypatch.setattr(routes, "bad", lambda handler, message, status=400: {"error": message, "status": status})

    payload = routes._handle_workspace_search(
        object(), urlparse("/api/workspace/search?session_id=sid&query=needle")
    )

    assert payload["query"] == "needle"
    assert calls == [("session", "sid"), ("trust", str(tmp_path))]


def test_search_route_rejects_missing_or_foreign_sessions(monkeypatch):
    monkeypatch.setattr(routes, "get_session_for_file_ops", lambda sid: (_ for _ in ()).throw(KeyError(sid)))
    monkeypatch.setattr(routes, "bad", lambda handler, message, status=400: (message, status))

    payload = routes._handle_workspace_search(
        object(), urlparse("/api/workspace/search?session_id=foreign&query=needle")
    )

    assert payload == ("Session not found", 404)


def test_search_route_rejects_empty_workspace_before_trust_fallback(monkeypatch):
    session = type("Session", (), {"workspace": ""})()
    calls = []

    monkeypatch.setattr(routes, "get_session_for_file_ops", lambda sid: session)
    monkeypatch.setattr(routes, "resolve_trusted_workspace", lambda workspace: calls.append(workspace))
    monkeypatch.setattr(routes, "search_workspace", lambda *args, **kwargs: calls.append("search"))
    monkeypatch.setattr(routes, "bad", lambda handler, message, status=400: (message, status))

    payload = routes._handle_workspace_search(
        object(), urlparse("/api/workspace/search?session_id=sid&query=needle")
    )

    assert payload == ("Workspace not configured", 404)
    assert calls == []


def test_search_route_rejects_untrusted_workspace(monkeypatch, tmp_path):
    session = type("Session", (), {"workspace": str(tmp_path / "workspace")})()
    monkeypatch.setattr(routes, "get_session_for_file_ops", lambda sid: session)
    monkeypatch.setattr(routes, "resolve_trusted_workspace", lambda workspace: (_ for _ in ()).throw(ValueError("blocked")))
    monkeypatch.setattr(routes, "bad", lambda handler, message, status=400: (message, status))

    payload = routes._handle_workspace_search(
        object(), urlparse("/api/workspace/search?session_id=sid&query=needle")
    )

    assert payload[1] == 404


def test_search_route_dispatch_runs_visibility_guard_and_handler(monkeypatch, tmp_path):
    session = type("Session", (), {"workspace": str(tmp_path), "profile": "default"})()
    calls = []

    class Handler:
        command = "GET"
        headers = {}
        rfile = io.BytesIO(b"")
        wfile = io.BytesIO()
        client_address = ("127.0.0.1", 12345)

    monkeypatch.setattr(routes, "get_session", lambda sid, metadata_only=False: session)
    monkeypatch.setattr(routes, "_session_visible_to_active_profile", lambda profile, handler: calls.append(("guard", profile)) or True)
    monkeypatch.setattr(routes, "get_session_for_file_ops", lambda sid: calls.append(("file_ops", sid)) or session)
    monkeypatch.setattr(routes, "resolve_trusted_workspace", lambda workspace: calls.append(("trust", workspace)) or tmp_path)
    monkeypatch.setattr(routes, "search_workspace", lambda root, query, **kwargs: calls.append(("search", str(root), query)) or {"results": [], "truncated": False})
    monkeypatch.setattr(routes, "j", lambda handler, payload, **kwargs: payload)
    monkeypatch.setattr(routes, "bad", lambda handler, message, status=400: {"error": message, "status": status})

    payload = routes.handle_get(
        Handler(), urlparse("/api/workspace/search?session_id=sid&query=needle")
    )

    assert payload["query"] == "needle"
    assert calls == [
        ("guard", "default"),
        ("file_ops", "sid"),
        ("trust", str(tmp_path)),
        ("search", str(tmp_path), "needle"),
    ]


def test_load_dir_clears_active_search_before_changing_directory():
    source = Path("static/workspace.js").read_text(encoding="utf-8")
    load_dir = source.split("async function loadDir(path, opts={}){", 1)[1].split(
        "function _workspaceRouteForPath", 1
    )[0]

    assert "nextDir!==(S.currentDir||'.')" in load_dir
    assert load_dir.index("_clearWorkspaceSearch();") < load_dir.index("S.currentDir=nextDir;")


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_search_state_preserves_short_query_and_renders_empty_truncation(tmp_path):
    driver = tmp_path / "driver.js"
    driver.write_text(
        r'''
const fs = require('fs');
async function main() {
const ws = fs.readFileSync(process.argv[2], 'utf8');
const ui = fs.readFileSync(process.argv[3], 'utf8');
const panels = fs.readFileSync(process.argv[4], 'utf8');
const els = {workspaceSearch: {value: ''}};
global.$ = id => els[id] || null;
global.S = {session: {session_id: 'sid', workspace: 'one'}, showHiddenWorkspaceFiles: false,
  _workspaceSearchResults: [{path: 'old'}], _workspaceSearchTruncated: true, _workspaceSearchPending: true};
global.renderFileTree = () => {};
let deferred = null;
global.api = () => new Promise((resolve, reject) => { deferred = {resolve, reject}; });
global.setTimeout = fn => { global.timer = fn; return 1; };
global.clearTimeout = () => {};
let _workspaceSearchTimer = null, _workspaceSearchRequest = 0, _workspaceSearchQuery = '', _wsTreeGen = 0;
function extract(source, name) {
  const start = source.search(new RegExp('function\\s+' + name + '\\s*\\('));
  let i = source.indexOf('{', start), depth = 1; i++;
  while (depth) { if (source[i] === '{') depth++; else if (source[i] === '}') depth--; i++; }
  return source.slice(start, i);
}
eval(extract(ws, '_workspaceSearchActive'));
eval(extract(ws, '_resetWorkspaceSearch'));
eval(extract(ws, '_clearWorkspaceSearch'));
eval(extract(ws, 'requestWorkspaceSearch'));
eval(extract(ws, 'bumpWorkspaceTreeGen'));
requestWorkspaceSearch('a');
if (_workspaceSearchQuery !== 'a' || els.workspaceSearch.value !== '' || S._workspaceSearchPending || S._workspaceSearchResults.length) throw new Error('short query reset');
requestWorkspaceSearch('ab');
if (_workspaceSearchQuery !== 'ab' || !S._workspaceSearchPending || !global.timer) throw new Error('recursive query scheduling');
const firstTimer = global.timer;
S.session.workspace = 'two';
bumpWorkspaceTreeGen();
const firstRun = firstTimer();
deferred.resolve({results: [{path: 'stale'}], truncated: true});
await firstRun;
if (_workspaceSearchQuery !== '' || S._workspaceSearchPending || S._workspaceSearchResults.length || S._workspaceSearchTruncated) throw new Error('stale success cleanup');
requestWorkspaceSearch('cd');
const secondTimer = global.timer;
S.session.workspace = 'three';
bumpWorkspaceTreeGen();
const secondRun = secondTimer();
deferred.reject(new Error('stale failure'));
await secondRun;
if (_workspaceSearchQuery !== '' || S._workspaceSearchPending || S._workspaceSearchResults.length || S._workspaceSearchTruncated) throw new Error('stale failure cleanup');
if (!/S\.session\.workspace=path;\s*if\s*\(\s*typeof bumpWorkspaceTreeGen === 'function'\s*\) bumpWorkspaceTreeGen\(\);/.test(panels)) throw new Error('workspace switch invalidation');
if (!/S\.session\.workspace = data\.default_workspace;\s*if\s*\(\s*typeof bumpWorkspaceTreeGen === 'function'\s*\) bumpWorkspaceTreeGen\(\);/.test(panels)) throw new Error('profile workspace invalidation');
const box = {innerHTML: '', children: [], appendChild(node) { this.children.push(node); }};
S._workspaceSearchTruncated = true;
global._visibleWorkspaceEntries = rows => rows;
global.esc = value => value;
global.t = key => key;
global.li = () => '';
global.document = {createElement: () => ({type: '', className: '', textContent: ''})};
eval(extract(ui, '_renderWorkspaceSearchResults'));
_renderWorkspaceSearchResults(box);
if (box.children.length !== 1 || box.children[0].className !== 'workspace-search-truncated') throw new Error('empty truncation');
process.stdout.write(JSON.stringify({query: _workspaceSearchQuery, pending: S._workspaceSearchPending}));
}
main().catch(err => { console.error(err && err.stack || err); process.exit(1); });
''',
        encoding="utf-8",
    )
    result = subprocess.run([NODE, str(driver), str(Path("static/workspace.js")), str(Path("static/ui.js")), str(Path("static/panels.js"))], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"query": "", "pending": False}
    source = Path("static/workspace.js").read_text(encoding="utf-8")
    ui = Path("static/ui.js").read_text(encoding="utf-8")
    assert "S.session.workspace!==workspace" in source
    assert "if(S._workspaceSearchTruncated)" in ui
    assert ui.index("if(S._workspaceSearchTruncated)") > ui.index("if(!results.length)")
