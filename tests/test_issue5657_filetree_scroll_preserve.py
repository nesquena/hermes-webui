"""Regression coverage for #5657 — workspace file-tree preserves scroll on re-render.

`renderFileTree()` clears its scroll container with ``box.innerHTML=''`` and
rebuilds every row. That detaches the rows, collapses ``scrollHeight``, and the
browser clamps ``scrollTop`` to 0 — so every folder expand/collapse, breadcrumb
nav, refresh, and hidden-files toggle that re-runs the renderer teleported the
reader to the top of a long tree.

Fix: capture ``scrollTop`` before the wipe and restore it after the normal
render tail. A plain scrollTop restore is sufficient (expand/collapse insert or
remove rows BELOW the clicked disclosure, so the clicked row keeps its offset) —
the reporter's getBoundingClientRect anchor sketch is deliberately NOT used, and
the ``.file-item`` rows carry no ``data-path`` for it anyway.
"""

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
UI_JS = (REPO_ROOT / "static" / "ui.js").read_text(encoding="utf-8")


def _render_file_tree_body() -> str:
    start = UI_JS.index("function renderFileTree()")
    end = UI_JS.index("\nfunction ", start + 1)
    return UI_JS[start:end]


def _render_file_tree_code() -> str:
    """Body with // comment lines stripped, so assertions anchor on real code."""
    lines = [ln for ln in _render_file_tree_body().splitlines() if not ln.strip().startswith("//")]
    return "\n".join(lines)


def test_render_file_tree_captures_and_restores_scrolltop():
    body = _render_file_tree_code()
    # It must still wipe the container (the behavior that loses scroll)...
    assert "box.innerHTML=''" in body or 'box.innerHTML = ""' in body
    # ...and it must capture scrollTop BEFORE the wipe and restore it after.
    assert "scrollTop" in body, (
        "renderFileTree must capture + restore scrollTop around the innerHTML wipe (#5657)"
    )
    capture_idx = body.index("prevScrollTop=box")
    wipe_idx = body.index("box.innerHTML=''")
    assert capture_idx < wipe_idx, (
        "scrollTop must be captured BEFORE box.innerHTML='' so it isn't already clamped to 0"
    )
    render_idx = body.index("_renderTreeItems(box")
    restore_idx = body.rindex("box.scrollTop=")
    assert restore_idx > render_idx, (
        "scrollTop must be restored AFTER _renderTreeItems repaints the tree"
    )


def test_early_return_paths_still_reset_scroll():
    # The no-workspace and empty-dir early returns legitimately want scroll reset
    # (box hidden / nothing to scroll) — the restore must live on the normal tail
    # only, i.e. AFTER _renderTreeItems, not before the early returns.
    body = _render_file_tree_code()
    render_idx = body.index("_renderTreeItems(box")
    tail = body[render_idx:]
    assert "box.scrollTop=" in tail or "box.scrollTop =" in tail, (
        "the scrollTop restore must be on the normal render tail, after _renderTreeItems"
    )
