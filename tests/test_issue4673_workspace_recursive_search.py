"""Functional regression coverage for backend-backed workspace search (#4673)."""

import os
import json
import io
import re
import shutil
import subprocess
from pathlib import Path
from urllib.parse import urlparse

import pytest
import api.routes as routes
from api.workspace import WorkspaceSearchUnavailableError, _DIR_FD_OK, search_workspace

NODE = shutil.which("node")
descriptor_search = pytest.mark.skipif(not _DIR_FD_OK, reason="descriptor-relative traversal unavailable")


def _track_workspace_fds(monkeypatch):
    workspace_module = __import__("api.workspace", fromlist=["os"])
    real_open = workspace_module.os.open
    real_dup = workspace_module.os.dup
    real_close = workspace_module.os.close
    live = {}

    def _remember(fd):
        live[fd] = live.get(fd, 0) + 1
        return fd

    def tracked_open(*args, **kwargs):
        return _remember(real_open(*args, **kwargs))

    def tracked_dup(*args, **kwargs):
        return _remember(real_dup(*args, **kwargs))

    def tracked_close(fd, *args, **kwargs):
        count = live.get(fd, 0)
        if count > 1:
            live[fd] = count - 1
        elif count == 1:
            del live[fd]
        return real_close(fd, *args, **kwargs)

    monkeypatch.setattr(workspace_module.os, "open", tracked_open)
    monkeypatch.setattr(workspace_module.os, "dup", tracked_dup)
    monkeypatch.setattr(workspace_module.os, "close", tracked_close)
    return live


@descriptor_search
def test_search_finds_unopened_descendants_with_relative_paths(tmp_path):
    root = tmp_path / "workspace"
    (root / "one" / "two").mkdir(parents=True)
    (root / "one" / "two" / "needle.txt").write_text("ok", encoding="utf-8")

    payload = search_workspace(root, "NEEDLE")

    assert [row["path"] for row in payload["results"]] == ["one/two/needle.txt"]
    assert payload["truncated"] is False


@descriptor_search
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


@descriptor_search
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

    rows = search_workspace(root, "escape")["results"]
    assert rows == [{
        "name": "escape.txt",
        "path": "escape.txt",
        "type": "symlink",
        "is_dir": False,
        "target_outside_workspace": True,
        "mtime_ns": rows[0]["mtime_ns"],
    }]
    assert search_workspace(root, "../")["results"] == []


@descriptor_search
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
    assert paths == {"needle-alias"}
    row = search_workspace(root, "needle")["results"][0]
    assert row["target_outside_workspace"] is True
    assert row["is_dir"] is False
    assert "size" not in row
    assert "target" not in row


@descriptor_search
def test_search_drops_symlink_swapped_after_held_fd_open(tmp_path, monkeypatch):
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

    workspace_module = __import__("api.workspace", fromlist=["os"])
    original = workspace_module.os.stat
    swapped = False
    lstat_calls = 0

    def stat_entry(name, *args, **kwargs):
        nonlocal lstat_calls, swapped
        result = original(name, *args, **kwargs)
        if name == "needle-alias" and kwargs.get("follow_symlinks") is False:
            lstat_calls += 1
        if lstat_calls == 1 and not swapped:
            alias.unlink()
            alias.symlink_to(outside / "needle.txt")
            swapped = True
        return result

    monkeypatch.setattr(workspace_module.os, "stat", stat_entry)
    rows = search_workspace(root, "needle")["results"]
    assert [row["path"] for row in rows] == ["needle-alias"]
    assert rows[0]["type"] == "symlink"
    assert rows[0]["target_outside_workspace"] is True
    assert rows[0]["is_dir"] is False
    assert "size" not in rows[0]
    assert "target" not in rows[0]
    assert swapped is True


@descriptor_search
def test_search_does_not_traverse_visible_symlink_into_hidden_directory(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    hidden = root / ".private"
    hidden.mkdir()
    (hidden / "needle.txt").write_text("secret", encoding="utf-8")
    try:
        (root / "alias").symlink_to(hidden, target_is_directory=True)
    except (OSError, NotImplementedError):
        return

    assert search_workspace(root, "needle")["results"] == []
    alias_rows = search_workspace(root, "alias")["results"]
    assert [row["path"] for row in alias_rows] == ["alias"]
    assert [row["path"] for row in search_workspace(root, "needle", include_hidden=True)["results"]] == [
        ".private/needle.txt"
    ]


@descriptor_search
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
    assert {row["path"] for row in search_workspace(root, "alias")["results"]} == {"alias.txt", "alias-dir"}
    assert all("cycle" not in path for path in paths)


@descriptor_search
def test_search_returns_in_workspace_symlink_chains(tmp_path):
    root = tmp_path / "workspace"
    (root / "real-dir").mkdir(parents=True)
    (root / "real-dir" / "needle.txt").write_text("ok", encoding="utf-8")
    try:
        (root / "file-chain-1.txt").symlink_to(root / "real-dir" / "needle.txt")
        (root / "file-chain-2.txt").symlink_to(root / "file-chain-1.txt")
        (root / "dir-chain-1").symlink_to(root / "real-dir", target_is_directory=True)
        (root / "dir-chain-2").symlink_to(root / "dir-chain-1", target_is_directory=True)
    except (OSError, NotImplementedError):
        return

    file_rows = {row["path"]: row for row in search_workspace(root, "file-chain")["results"]}
    assert set(file_rows) == {"file-chain-1.txt", "file-chain-2.txt"}
    assert file_rows["file-chain-1.txt"]["is_dir"] is False
    assert file_rows["file-chain-2.txt"]["is_dir"] is False

    dir_rows = {row["path"]: row for row in search_workspace(root, "dir-chain")["results"]}
    assert set(dir_rows) == {"dir-chain-1", "dir-chain-2"}
    assert dir_rows["dir-chain-1"]["is_dir"] is True
    assert dir_rows["dir-chain-2"]["is_dir"] is True


def test_search_fails_closed_when_descriptor_traversal_is_unavailable(tmp_path, monkeypatch):
    root = tmp_path / "workspace"
    root.mkdir()
    workspace_module = __import__("api.workspace", fromlist=["_DIR_FD_OK"])
    monkeypatch.setattr(workspace_module, "_DIR_FD_OK", False)
    with pytest.raises(WorkspaceSearchUnavailableError):
        search_workspace(root, "needle")


@descriptor_search
def test_search_does_not_hang_on_in_workspace_fifo_symlink(tmp_path):
    import threading

    if not hasattr(os, "mkfifo"):
        return

    root = tmp_path / "workspace"
    root.mkdir()
    target = root / "pipe-target"
    alias = root / "pipe-alias"
    try:
        os.mkfifo(target)
        alias.symlink_to(target)
    except (OSError, NotImplementedError):
        return

    result_box = {}

    def _run():
        result_box["r"] = search_workspace(root, "pipe")

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    t.join(timeout=10)
    assert not t.is_alive(), "search_workspace hung on an in-workspace FIFO symlink"
    assert isinstance(result_box["r"]["results"], list)


@descriptor_search
def test_search_closes_descriptors_on_success(tmp_path, monkeypatch):
    root = tmp_path / "workspace"
    (root / "real-dir").mkdir(parents=True)
    (root / "real-dir" / "needle.txt").write_text("ok", encoding="utf-8")
    try:
        (root / "chain-1.txt").symlink_to(root / "real-dir" / "needle.txt")
        (root / "chain-2.txt").symlink_to(root / "chain-1.txt")
    except (OSError, NotImplementedError):
        return

    live = _track_workspace_fds(monkeypatch)

    payload = search_workspace(root, "chain")

    assert {row["path"] for row in payload["results"]} == {"chain-1.txt", "chain-2.txt"}
    assert live == {}


@descriptor_search
def test_search_closes_descriptors_on_cap_truncation(tmp_path, monkeypatch):
    root = tmp_path / "workspace"
    root.mkdir()
    for index in range(3):
        (root / f"needle-{index}.txt").write_text("ok", encoding="utf-8")

    live = _track_workspace_fds(monkeypatch)

    payload = search_workspace(root, "needle", max_results=1)

    assert payload["truncated"] is True
    assert live == {}


@descriptor_search
def test_search_truncates_when_queued_directory_swaps_to_symlink(tmp_path, monkeypatch):
    root = tmp_path / "workspace"
    outside = tmp_path / "outside"
    queued = root / "queued"
    parked = root / "queued-hidden"
    root.mkdir()
    outside.mkdir()
    queued.mkdir()
    (queued / "needle.txt").write_text("ok", encoding="utf-8")
    (outside / "needle.txt").write_text("secret", encoding="utf-8")
    try:
        probe = root / "probe"
        probe.symlink_to(outside, target_is_directory=True)
        probe.unlink()
    except (OSError, NotImplementedError):
        return

    workspace_module = __import__("api.workspace", fromlist=["os"])
    original = workspace_module.os.stat
    lstat_calls = 0
    swapped = False

    def stat_entry(name, *args, **kwargs):
        nonlocal lstat_calls, swapped
        result = original(name, *args, **kwargs)
        if name == "queued" and kwargs.get("follow_symlinks") is False:
            lstat_calls += 1
            if lstat_calls == 2 and not swapped:
                queued.rename(parked)
                (root / "queued").symlink_to(outside, target_is_directory=True)
                swapped = True
        return result

    monkeypatch.setattr(workspace_module.os, "stat", stat_entry)
    live = _track_workspace_fds(monkeypatch)

    payload = search_workspace(root, "needle")

    assert payload["results"] == []
    assert payload["truncated"] is True
    assert swapped is True
    assert live == {}


@descriptor_search
def test_search_root_descriptor_open_race_raises_not_found(tmp_path, monkeypatch):
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "needle.txt").write_text("ok", encoding="utf-8")
    workspace_module = __import__("api.workspace", fromlist=["os"])
    original = workspace_module.os.open
    root_path = str(root.resolve())

    def open_entry(path, flags, *args, **kwargs):
        if path == root_path and kwargs.get("dir_fd") is None:
            raise OSError("raced root")
        return original(path, flags, *args, **kwargs)

    monkeypatch.setattr(workspace_module.os, "open", open_entry)

    with pytest.raises(FileNotFoundError):
        search_workspace(root, "needle")


@descriptor_search
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


@descriptor_search
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

    monkeypatch.setattr("api.workspace._DIR_FD_OK", True)
    monkeypatch.setattr(os, "scandir", lambda _directory: FakeScan())

    payload = search_workspace(root, "needle")

    assert payload["results"] == []
    assert payload["truncated"] is True


@descriptor_search
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


def test_search_route_reports_descriptor_capability_failure(monkeypatch, tmp_path):
    session = type("Session", (), {"workspace": str(tmp_path)})()
    monkeypatch.setattr(routes, "get_session_for_file_ops", lambda sid: session)
    monkeypatch.setattr(routes, "resolve_trusted_workspace", lambda workspace: tmp_path)
    monkeypatch.setattr(
        routes,
        "search_workspace",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            WorkspaceSearchUnavailableError("descriptor support missing")
        ),
    )
    monkeypatch.setattr(routes, "bad", lambda handler, message, status=400: (message, status))

    payload = routes._handle_workspace_search(
        object(), urlparse("/api/workspace/search?session_id=sid&query=needle")
    )

    assert payload == ("descriptor support missing", 503)


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
const sessions = fs.readFileSync(process.argv[5], 'utf8');
const els = {workspaceSearch: {value: ''}};
global.$ = id => els[id] || null;
global.document = {title: ''};
global.assistantDisplayName = () => '';
global.t = key => key;
global.S = {session: {session_id: 'sid', workspace: 'one'}, showHiddenWorkspaceFiles: false,
  _workspaceSearchResults: [{path: 'old'}], _workspaceSearchTruncated: true, _workspaceSearchPending: true};
let renderedTreeMode = 'initial';
global.renderFileTree = () => {
  if (!(S.session && S.session.workspace)) { renderedTreeMode = 'hidden'; return; }
  if (_workspaceSearchQuery.length >= 2 && !S._workspaceSearchPending) {
    renderedTreeMode = (S._workspaceSearchResults || []).map(item => item.path).join(',');
    return;
  }
  renderedTreeMode = 'tree';
};
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
eval(extract(ws, 'setWorkspaceSearchSession'));
eval(extract(ws, 'requestWorkspaceSearch'));
eval(extract(ws, 'bumpWorkspaceTreeGen'));
eval(extract(ui, 'syncTopbar'));
const newSessionIngress = sessions.match(/setWorkspaceSearchSession\(data\.session\);\s*S\.messages=data\.session\.messages\|\|\[\];/);
if (!newSessionIngress) throw new Error('newSession ingress not found');
const loadSessionIngress = sessions.match(/setWorkspaceSearchSession\(data\.session\);\s*if\(typeof _clearEmptyComposerModelOverride==='function'\) _clearEmptyComposerModelOverride\(\);/);
if (!loadSessionIngress) throw new Error('loadSession ingress not found');
requestWorkspaceSearch('a');
if (_workspaceSearchQuery !== 'a' || els.workspaceSearch.value !== '' || S._workspaceSearchPending || S._workspaceSearchResults.length) throw new Error('short query reset');
requestWorkspaceSearch('ab');
if (_workspaceSearchQuery !== 'ab' || !S._workspaceSearchPending || !global.timer) throw new Error('recursive query scheduling');
const firstTimer = global.timer;
setWorkspaceSearchSession({session_id: 'sid', workspace: 'two'});
const firstRun = firstTimer();
deferred.resolve({results: [{path: 'stale'}], truncated: true});
await firstRun;
if (_workspaceSearchQuery !== '' || S._workspaceSearchPending || S._workspaceSearchResults.length || S._workspaceSearchTruncated) throw new Error('stale success cleanup');
requestWorkspaceSearch('cd');
const secondTimer = global.timer;
setWorkspaceSearchSession({session_id: 'sid', workspace: 'three'});
const secondRun = secondTimer();
deferred.reject(new Error('stale failure'));
await secondRun;
setWorkspaceSearchSession({session_id: 'sid', workspace: 'one'});
_workspaceSearchQuery = 'new-session';
S._workspaceSearchResults = [{path: 'old'}];
S._workspaceSearchTruncated = true;
S._workspaceSearchPending = true;
els.workspaceSearch.value = 'new-session';
renderedTreeMode = 'stale-results';
let data = {session: {session_id: 'fresh-sid', workspace: 'one', messages: []}};
eval(newSessionIngress[0]);
if (_workspaceSearchQuery !== '' || els.workspaceSearch.value !== '' || S._workspaceSearchPending || S._workspaceSearchResults.length || S._workspaceSearchTruncated) throw new Error('new-session cleanup');
if (renderedTreeMode !== 'tree') throw new Error('new-session rendered cleanup');
setWorkspaceSearchSession({session_id: 'fresh-sid', workspace: 'one'});
_workspaceSearchQuery = 'same-root';
S._workspaceSearchResults = [{path: 'old'}];
S._workspaceSearchTruncated = true;
S._workspaceSearchPending = true;
els.workspaceSearch.value = 'same-root';
renderedTreeMode = 'stale-results';
data = {session: {session_id: 'new-sid', workspace: 'one', messages: []}};
eval(loadSessionIngress[0]);
if (_workspaceSearchQuery !== '' || els.workspaceSearch.value !== '' || S._workspaceSearchPending || S._workspaceSearchResults.length || S._workspaceSearchTruncated) throw new Error('same-root session cleanup');
if (renderedTreeMode !== 'tree') throw new Error('same-root rendered cleanup');
_workspaceSearchQuery = 'null-session';
S._workspaceSearchResults = [{path: 'old'}];
S._workspaceSearchTruncated = true;
S._workspaceSearchPending = true;
els.workspaceSearch.value = 'null-session';
renderedTreeMode = 'stale-results';
setWorkspaceSearchSession(null);
if (_workspaceSearchQuery !== '' || els.workspaceSearch.value !== '' || S._workspaceSearchPending || S._workspaceSearchResults.length || S._workspaceSearchTruncated) throw new Error('null-session cleanup');
if (renderedTreeMode !== 'hidden') throw new Error('null-session rendered cleanup');
if (_workspaceSearchQuery !== '' || S._workspaceSearchPending || S._workspaceSearchResults.length || S._workspaceSearchTruncated) throw new Error('stale failure cleanup');
if (!/S\.session\.workspace=path;\s*if\s*\(\s*typeof bumpWorkspaceTreeGen === 'function'\s*\) bumpWorkspaceTreeGen\(\);/.test(panels)) throw new Error('workspace switch invalidation');
if (!/S\.session\.workspace = data\.default_workspace;\s*if\s*\(\s*typeof bumpWorkspaceTreeGen === 'function'\s*\) bumpWorkspaceTreeGen\(\);/.test(panels)) throw new Error('profile workspace invalidation');
const box = {innerHTML: '', children: [], appendChild(node) { this.children.push(node); }};
S._workspaceSearchTruncated = true;
global._visibleWorkspaceEntries = rows => rows;
global.esc = value => value;
global.t = key => key;
global.li = () => '';
global.document = {createElement: () => ({type: '', className: '', textContent: '', setAttribute() {}})};
eval(extract(ui, '_renderWorkspaceSearchResults'));
_renderWorkspaceSearchResults(box);
if (box.children.length !== 1 || box.children[0].className !== 'workspace-search-truncated') throw new Error('empty truncation');
let opened = [], loaded = [];
global.openFile = path => opened.push(path);
global.loadDir = path => loaded.push(path);
S._workspaceSearchTruncated = false;
S._workspaceSearchResults = [
  {name: 'escape', path: 'escape', type: 'symlink', is_dir: false, target_outside_workspace: true},
  {name: 'file.txt', path: 'file.txt', type: 'file', is_dir: false},
  {name: 'folder', path: 'folder', type: 'dir', is_dir: true},
];
const rows = {innerHTML: '', children: [], appendChild(node) { this.children.push(node); }};
_renderWorkspaceSearchResults(rows);
if (rows.children.length !== 3 || typeof rows.children[0].onclick === 'function') throw new Error('external row is active');
rows.children[1].onclick();
rows.children[2].onclick();
if (opened.join(',') !== 'file.txt' || loaded.join(',') !== 'folder') throw new Error('internal row activation changed');
process.stdout.write(JSON.stringify({query: _workspaceSearchQuery, pending: S._workspaceSearchPending}));
}
main().catch(err => { console.error(err && err.stack || err); process.exit(1); });
''',
        encoding="utf-8",
    )
    result = subprocess.run([NODE, str(driver), str(Path("static/workspace.js")), str(Path("static/ui.js")), str(Path("static/panels.js")), str(Path("static/sessions.js"))], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"query": "", "pending": False}
    source = Path("static/workspace.js").read_text(encoding="utf-8")
    ui = Path("static/ui.js").read_text(encoding="utf-8")
    session_writers = []
    for path in sorted(Path("static").glob("*.js")):
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if line.lstrip().startswith("//"):
                continue
            if re.search(r"\bS\.session\s*=", line):
                session_writers.append(f"{path.as_posix()}:{lineno}:{line.strip()}")
    assert "S.session.workspace!==workspace" in source
    assert "function setWorkspaceSearchSession(session)" in source
    assert "if(typeof renderFileTree==='function') renderFileTree();" in source
    assert "setWorkspaceSearchSession(" in ui
    assert "setWorkspaceSearchSession(" in Path("static/sessions.js").read_text(encoding="utf-8")
    assert "setWorkspaceSearchSession(" in Path("static/messages.js").read_text(encoding="utf-8")
    assert session_writers == ["static/workspace.js:313:S.session=session;"]
    assert "if(S._workspaceSearchTruncated)" in ui
    assert ui.index("if(S._workspaceSearchTruncated)") > ui.index("if(!results.length)")
