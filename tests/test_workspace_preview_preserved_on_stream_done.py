"""Regression: workspace file preview must survive background file-tree refresh on chat done."""

from pathlib import Path


REPO = Path(__file__).resolve().parent.parent
MESSAGES_JS = (REPO / "static" / "messages.js").read_text(encoding="utf-8")
WORKSPACE_JS = (REPO / "static" / "workspace.js").read_text(encoding="utf-8")


def _function_block(src: str, name: str) -> str:
    marker = f"function {name}("
    start = src.find(marker)
    assert start != -1, f"{name}() not found"
    params_end = src.find("){", start)
    assert params_end != -1, f"{name}() body not found"
    brace = params_end + 1
    depth = 0
    for idx in range(brace, len(src)):
        ch = src[idx]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return src[start : idx + 1]
    raise AssertionError(f"{name}() body did not close")


def _done_block() -> str:
    start = MESSAGES_JS.find("source.addEventListener('done'")
    assert start != -1, "done handler not found in messages.js"
    end = MESSAGES_JS.find("source.addEventListener('stream_end'", start)
    assert end != -1, "stream_end handler not found after done handler"
    return MESSAGES_JS[start:end]


def test_stream_done_refreshes_workspace_without_clearing_preview():
    """Chat completion should refresh the tree but not exit an open file preview."""
    done_block = _done_block()
    assert "preservePreview:true" in done_block.replace(" ", ""), (
        "The done handler must refresh the workspace file tree without calling the "
        "directory-navigation clearPreview path in loadDir()."
    )


def test_load_dir_supports_preserve_preview_option():
    block = _function_block(WORKSPACE_JS, "loadDir")
    assert "preservePreview" in block, "loadDir() must accept a preservePreview option"
    assert "if(!preservePreview&&typeofclearPreview" in block.replace(" ", ""), (
        "loadDir() should skip clearPreview() when preservePreview is requested"
    )


def test_load_dir_still_clears_preview_for_directory_navigation():
    """#1785: explicit directory navigation must still switch preview back to browse mode."""
    block = _function_block(WORKSPACE_JS, "loadDir")
    assert "clearPreview({keepPanelOpen:true})" in block.replace(" ", ""), (
        "Directory navigation must still clear previews when preservePreview is not set"
    )
