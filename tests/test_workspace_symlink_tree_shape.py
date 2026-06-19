"""Tests for #4226 — symlinks in the workspace file tree must display correctly.

The renderer _renderTreeItems must use isDirLike (type==='dir' || (type==='symlink'
&& is_dir)) for all expand/navigate/delete gates rather than type==='dir' alone.
"""
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
UI_JS = (REPO_ROOT / "static" / "ui.js").read_text(encoding="utf-8")
ICONS_JS = (REPO_ROOT / "static" / "icons.js").read_text(encoding="utf-8")
I18N_JS = (REPO_ROOT / "static" / "i18n.js").read_text(encoding="utf-8")
WS_JS = (REPO_ROOT / "static" / "workspace.js").read_text(encoding="utf-8")


def _render_block() -> str:
    start = UI_JS.find("function _renderTreeItems(container, entries, depth)")
    assert start >= 0, "_renderTreeItems not found in static/ui.js"
    # Capture to end of function (next top-level async/function declaration)
    end = UI_JS.find("\nasync function deleteWorkspaceDir", start)
    assert end >= 0, "end of _renderTreeItems not found"
    return UI_JS[start:end]


class TestIsDirLikeLocals:
    def test_isLk_local_declared(self):
        block = _render_block()
        assert "const isLk = item.type === 'symlink';" in block, \
            "isLk local must be declared inside the per-item loop"

    def test_isDirLike_local_declared(self):
        block = _render_block()
        assert "const isDirLike = item.type === 'dir' || (isLk && item.is_dir);" in block, \
            "isDirLike local must be declared inside the per-item loop"

    def test_isFileLike_local_declared(self):
        block = _render_block()
        assert "const isFileLike = !isDirLike;" in block, \
            "isFileLike local must be declared for file-like symlink handling"

    def test_wsIsDir_dataset_set(self):
        block = _render_block()
        assert "el.dataset.wsIsDir = String(isDirLike);" in block, \
            "data-ws-is-dir must be set on every file-item row"


class TestExpandToggle:
    def test_toggle_uses_isDirLike(self):
        block = _render_block()
        assert "if(isDirLike){" in block, \
            "expand toggle must branch on isDirLike, not item.type==='dir'"

    def test_recursive_render_uses_isDirLike(self):
        block = _render_block()
        assert "if(isDirLike&&S._expandedDirs.has(item.path)){" in block, \
            "recursive child render must branch on isDirLike"


class TestLinkIcon:
    def test_link_icon_path_exists(self):
        assert "'link':" in ICONS_JS, \
            "'link' key must be added to LI_PATHS in static/icons.js"

    def test_icon_dispatch_uses_li_link(self):
        block = _render_block()
        assert "li('link', 14)" in block, \
            "icon dispatch must emit li('link', 14) for symlink rows"


class TestSymlinkTooltip:
    def test_symlink_link_to_key_in_i18n(self):
        count = I18N_JS.count("symlink_link_to:")
        assert count >= 5, \
            f"symlink_link_to key must appear in multiple locale blocks; found {count}"

    def test_elideMiddle_utility_present(self):
        assert "function elideMiddle(" in UI_JS, \
            "elideMiddle() utility must be defined in static/ui.js"

    def test_tooltip_uses_symlink_link_to(self):
        block = _render_block()
        assert "t('symlink_link_to')" in block, \
            "symlink rows must set a tooltip using the symlink_link_to i18n key"


class TestDragDropGuard:
    def test_workspace_drag_guard_widened(self):
        assert '[data-ws-is-dir="true"]' in WS_JS, \
            "workspace.js drag-over guard must also exclude [data-ws-is-dir='true'] rows"


class TestSymlinkFileAffordances:
    def test_size_badge_uses_isFileLike(self):
        block = _render_block()
        assert "if(isFileLike&&item.size){" in block, \
            "symlink-to-file rows must render the file-size badge"

    def test_delete_button_uses_isFileLike(self):
        block = _render_block()
        assert "if(isFileLike){" in block, \
            "symlink-to-file rows must render the inline delete button"
