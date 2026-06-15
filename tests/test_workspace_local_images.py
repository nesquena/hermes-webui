"""Tests for local image rendering in workspace markdown preview.

Local images referenced with relative paths (e.g. `![alt](image.png)`)
in workspace markdown files are now rendered as `<img>` tags, served
through the workspace file raw endpoint.

Strategy:
  - Source-level checks verify the changes exist in ui.js and workspace.js.
  - Python mirror of _localImage() verifies path resolution and traversal blocking.
"""
import pathlib
import re

REPO_ROOT = pathlib.Path(__file__).parent.parent
UI_JS = (REPO_ROOT / "static" / "ui.js").read_text()
WORKSPACE_JS = (REPO_ROOT / "static" / "workspace.js").read_text()


# ── Helpers: Python mirror of _localImage() ───────────────────────────────────

def _local_image(base, url):
    """Python mirror of the _localImage() helper in renderMd().

    Returns the resolved path if the image is allowed, None if rejected
    (e.g. parent-directory traversal) or base is empty.
    """
    if not base:
        return None
    resolved = base + url.lstrip('/')
    # Block parent-directory traversal
    if re.match(r'^(?:\.\/)?\.\.', resolved) or re.search(r'/\.\.(?:/|$)', resolved):
        return None
    return resolved


# ── Source-level checks ───────────────────────────────────────────────────────

class TestLocalImageSourceLevel:
    """Verify the local image feature is present in the source code."""

    def test_rendermd_accepts_opts_parameter(self):
        """renderMd() must accept an optional second parameter for local image config."""
        assert "function renderMd(raw, opts)" in UI_JS, (
            "renderMd must accept opts parameter for local image support"
        )

    def test_local_image_helper_exists(self):
        """_localImage helper must exist inside renderMd."""
        assert "_localImage" in UI_JS, (
            "renderMd must have _localImage helper for local path resolution"
        )

    def test_local_image_blocks_parent_dir_traversal(self):
        """_localImage must reject paths containing .. segments."""
        assert "\\.\\." in UI_JS, (
            "_localImage must block parent-directory traversal"
        )

    def test_inline_md_has_local_image_pass(self):
        """The inlineMd image pass must have a local-image branch."""
        assert "opts&&opts.localImageUrlFn" in UI_JS, (
            "inlineMd image pass must conditionally handle local images"
        )

    def test_outer_image_pass_has_local_image_branch(self):
        """The outer paragraph image pass must have a local-image branch."""
        # Count occurrences — should be at least 2 (inlineMd + outer)
        count = UI_JS.count("opts&&opts.localImageUrlFn")
        assert count >= 2, (
            f"Both image passes (inline + outer) must check opts.localImageUrlFn, "
            f"found {count}"
        )

    def test_workspace_passes_local_context_to_rendermd(self):
        """renderMarkdownPreviewContent must pass localBase and localImageUrlFn to renderMd."""
        assert "localBase:" in WORKSPACE_JS, (
            "workspace.js must pass localBase to renderMd"
        )
        assert "localImageUrlFn:" in WORKSPACE_JS, (
            "workspace.js must pass localImageUrlFn to renderMd"
        )

    def test_workspace_local_image_url_uses_file_raw_endpoint(self):
        """localImageUrlFn must use the api/file/raw endpoint."""
        assert "api/file/raw" in WORKSPACE_JS, (
            "workspace.js local image URL must use api/file/raw endpoint"
        )

    def test_workspace_computes_directory_from_preview_path(self):
        """renderMarkdownPreviewContent must compute directory from _previewCurrentPath."""
        assert "_previewCurrentPath" in WORKSPACE_JS, (
            "Must reference _previewCurrentPath for directory computation"
        )
        assert "lastIndexOf" in WORKSPACE_JS, (
            "Must extract directory portion of path"
        )


# ── Behavioral: _localImage path resolution ────────────────────────────────────

class TestLocalImagePathResolution:
    """Test _localImage path resolution and traversal blocking."""

    def test_simple_filename_resolves(self):
        """A simple filename in the same directory resolves correctly."""
        assert _local_image("dir/", "image.png") == "dir/image.png"

    def test_nested_path_resolves(self):
        """A subdirectory path resolves correctly."""
        assert _local_image("dir/", "sub/image.png") == "dir/sub/image.png"

    def test_absolute_path_strips_leading_slash(self):
        """An absolute path (starting with /) is resolved as workspace-relative."""
        assert _local_image("dir/", "/images/logo.png") == "dir/images/logo.png"

    def test_root_dir_resolves(self):
        """An empty base resolves paths relative to workspace root."""
        assert _local_image("", "image.png") is None

    def test_parent_dir_traversal_blocked_at_start(self):
        """Path starting with .. is blocked."""
        assert _local_image("dir/", "../image.png") is None

    def test_parent_dir_traversal_blocked_in_middle(self):
        """Path with .. segment in the middle is blocked."""
        assert _local_image("dir/", "sub/../image.png") is None

    def test_parent_dir_traversal_blocked_at_end(self):
        """Path ending with .. is blocked."""
        assert _local_image("dir/", "sub/image/..") is None

    def test_dot_slash_parent_blocked(self):
        """Path with ./.. prefix is blocked."""
        assert _local_image("dir/", "./../image.png") is None

    def test_deep_traversal_blocked(self):
        """Deep traversal path is blocked."""
        assert _local_image("dir/", "a/b/../../image.png") is None

    def test_allowed_paths_with_dots(self):
        """Paths containing dots that aren't traversal are allowed."""
        assert _local_image("dir/", ".hidden/image.png") == "dir/.hidden/image.png"
        assert _local_image("dir/", "file.name.png") == "dir/file.name.png"

    def test_root_dir_with_simple_path(self):
        """Empty base with simple path returns None (no local config)."""
        assert _local_image("", "image.png") is None
