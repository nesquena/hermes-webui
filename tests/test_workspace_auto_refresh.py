import pathlib
import re

from api.workspace import dir_signature, list_dir

REPO = pathlib.Path(__file__).parent.parent
WORKSPACE_JS = (REPO / "static" / "workspace.js").read_text(encoding="utf-8")


def _function_body(source: str, name: str) -> str:
    match = re.search(rf"(?:async\s+)?function\s+{re.escape(name)}\s*\([^)]*\)\s*\{{", source)
    assert match, f"{name} function not found"
    start = match.end()
    depth = 1
    i = start
    while i < len(source) and depth:
        if source[i] == "{":
            depth += 1
        elif source[i] == "}":
            depth -= 1
        i += 1
    assert depth == 0, f"{name} function body did not parse"
    return source[start : i - 1]


def test_existing_local_git_auto_refresh_polls_status_without_fetching():
    body = _function_body(WORKSPACE_JS, "_autoRefreshWorkspaceGitStatus")
    assert "refreshGitStatus({auto:true,refreshBranches:false})" in body.replace(" ", "")
    assert "/api/git/fetch" not in body
    assert "runGitRemoteAction('fetch')" not in body


def test_auto_fetch_has_separate_cadence_and_posts_fetch_quietly():
    assert "const GIT_AUTO_FETCH_MS" in WORKSPACE_JS
    assert "const GIT_AUTO_FETCH_MIN_ERROR_BACKOFF_MS" in WORKSPACE_JS
    body = _function_body(WORKSPACE_JS, "_autoFetchWorkspaceGit")
    compact = body.replace(" ", "")
    assert "`/api/git/fetch`" in body
    assert "method:'POST'" in compact
    assert "showToast(_gitRemoteToastMessage" not in body
    assert "lastAutoFetchAt" in body
    assert "lastAutoFetchError" in body


def test_auto_fetch_guard_skips_unsafe_or_noisy_states():
    body = _function_body(WORKSPACE_JS, "_shouldAutoFetchWorkspaceGit")
    for token in [
        "!S.session",
        "_workspacePanelOpenForAutoRefresh",
        "!git.status||!git.status.is_git",
        "git.mutating||git.syncing||git.autoFetching",
        "git.generatingCommitMessage||git.branchMenuOpen",
        "_previewDirty",
        "GIT_AUTO_FETCH_MS",
        "GIT_AUTO_FETCH_MIN_ERROR_BACKOFF_MS",
    ]:
        assert token in body.replace(" ", "")


def test_directory_signatures_are_returned_and_change_with_entries(tmp_path):
    (tmp_path / "alpha.txt").write_text("one", encoding="utf-8")
    entries = list_dir(tmp_path, ".")
    sig1 = dir_signature(tmp_path, ".", entries)
    assert isinstance(sig1, str)
    assert len(sig1) == 64
    assert all("mtime_ns" in entry for entry in entries)

    (tmp_path / "beta.txt").write_text("two", encoding="utf-8")
    entries2 = list_dir(tmp_path, ".")
    sig2 = dir_signature(tmp_path, ".", entries2)
    assert sig2 != sig1


def test_load_dir_stores_directory_signatures_for_root_and_expanded_dirs():
    load_body = _function_body(WORKSPACE_JS, "loadDir")
    store_body = _function_body(WORKSPACE_JS, "_storeWorkspaceDirListing")
    assert "S._dirSignatures={}" in load_body.replace(" ", "")
    assert "signatures[dirPath]=data.signature" in store_body.replace(" ", "")
    assert "_storeWorkspaceDirListing(dirPath,data)" in load_body.replace(" ", "")


def test_file_tree_refresh_polls_visible_dirs_and_rerenders_only_on_signature_changes():
    visible_body = _function_body(WORKSPACE_JS, "_visibleWorkspaceDirsForRefresh")
    refresh_body = _function_body(WORKSPACE_JS, "_refreshWorkspaceTreeIfChanged")
    compact = refresh_body.replace(" ", "")
    assert "WORKSPACE_TREE_AUTO_REFRESH_MAX_DIRS" in visible_body
    assert "S.currentDir" in visible_body
    assert "S._expandedDirs" in visible_body
    assert "signatures[dir]&&signatures[dir]===nextSignature" in compact
    assert "_storeWorkspaceDirListing(dir,data)" in compact
    assert "renderFileTree()" in refresh_body
    assert "renderWorkspacePanelTabState()" in refresh_body


def test_file_tree_refresh_guard_preserves_unsaved_preview_and_avoids_git_mutation():
    body = _function_body(WORKSPACE_JS, "_shouldRefreshWorkspaceTree")
    compact = body.replace(" ", "")
    assert "!S.session" in compact
    assert "_workspacePanelOpenForAutoRefresh" in body
    assert "_previewDirty" in body
    assert "S._treeRefreshing" in body
    assert "git.mutating||git.syncing" in compact
