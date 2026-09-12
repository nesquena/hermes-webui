from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
WORKSPACE_JS = (REPO_ROOT / "static" / "workspace.js").read_text(encoding="utf-8")


def test_root_load_does_not_wait_for_restored_expanded_directory_prefetch():
    """A slow restored child directory must not block the root workspace view."""
    load_start = WORKSPACE_JS.index("async function loadDir(path, opts={})")
    load_end = WORKSPACE_JS.index("function refreshWorkspacePanel", load_start)
    load_body = WORKSPACE_JS[load_start:load_end]

    assert "void _prefetchExpandedWorkspaceDirs" in load_body
    assert "await Promise.all(pending.map(dirPath=>" not in load_body


def test_restored_directory_prefetch_is_bounded_and_silent_on_timeout():
    """Background tree hydration must use a bounded, non-toast API request."""
    start = WORKSPACE_JS.index("async function _prefetchExpandedWorkspaceDirs")
    end = WORKSPACE_JS.index("async function loadDir", start)
    body = WORKSPACE_JS[start:end]

    assert "const _WORKSPACE_PREFETCH_TIMEOUT_MS=8000" in WORKSPACE_JS
    assert "timeoutToast:false" in body
    assert "retries:0" in body
    assert "Promise.all" in body
