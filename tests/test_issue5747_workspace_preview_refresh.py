"""Regression: workspace preview auto-refresh + half-screen layout bug (#5747).

Two root causes fixed in static/workspace.js:

1. _normalizeArtifactPath() did not strip the workspace prefix from absolute
   paths. Tools like write_file/patch pass absolute paths
   ("/Users/x/ws/foo.py") while the file-tree preview records a bare relative
   path ("foo.py"). The two never compared equal, so mutation tracking never
   fired and the preview stayed stale.

   The fix reuses the proven prefix-strip pattern from openArtifactPath()
   (workspace.js:585-593): after stripping ~/ and ./ prefixes, strip the
   S.session.workspace prefix from absolute paths. Crucially, this strip
   happens BEFORE the `if(!/[./]/.test(path))` guard so that extension-less
   root files like "Makefile" are not dropped.

   Note: terminal/execute_code tools also modify files but their args are
   shell/script bodies, not structured file paths. Tracking mutations from
   those tools is scoped as a separate follow-up issue, not this fix.

2. loadDir() calls renderFileTree() which unconditionally sets
   box.style.display='' (ui.js), restoring the fileTree to visible. When
   preservePreview=true and a preview is open, this left both fileTree and
   previewArea visible (each flex:1 in a flex-direction:column right panel),
   producing a half-screen layout. Fix: after renderFileTree(), re-hide
   fileTree when preservePreview && _previewCurrentPath. This guard is
   independent of the path-normalization fix — it holds even when mutation
   detection fails, preventing the half-screen flicker regardless.

Drives the ACTUAL functions from static/workspace.js via node so it can't
drift from a Python mirror.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
WORKSPACE_JS = (REPO / "static" / "workspace.js").read_text(encoding="utf-8")
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")


def _extract_const(name: str) -> str:
    m = re.search(rf"const {name} = .*?;", WORKSPACE_JS)
    assert m, f"const {name} not found"
    return m.group(0)


def _extract_fn(name: str) -> str:
    start = WORKSPACE_JS.index(f"function {name}(")
    # Find the function body's opening brace by locating "){" — this avoids
    # matching braces inside default parameter values (e.g. opts={}).
    params_end = WORKSPACE_JS.index("){", start)
    brace = params_end + 1
    depth = 0
    for i in range(brace, len(WORKSPACE_JS)):
        c = WORKSPACE_JS[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return WORKSPACE_JS[start : i + 1]
    raise AssertionError(f"function {name} did not close")


def _normalize_via_node(paths, workspace="/Users/test/ws"):
    """Drive _normalizeArtifactPath with a stubbed S.session.workspace."""
    ignore_re = _extract_const("ARTIFACT_IGNORE_RE")
    fn = _extract_fn("_normalizeArtifactPath")
    driver = (
        f"const S = {{ session: {{ workspace: {json.dumps(workspace)} }} }};\n"
        + ignore_re + "\n" + fn + "\n"
        + "const out = JSON.parse(process.argv[1]).map(_normalizeArtifactPath);\n"
        + "process.stdout.write(JSON.stringify(out));\n"
    )
    r = subprocess.run(
        [NODE, "-e", driver, json.dumps(paths)],
        capture_output=True, text=True, timeout=15,
    )
    assert r.returncode == 0, f"node failed: {r.stderr}"
    return json.loads(r.stdout)


def _candidates_via_node(tc, workspace="/Users/test/ws"):
    """Drive _artifactCandidatesFromToolCall with a stubbed S.session.workspace."""
    consts = _extract_const("ARTIFACT_IGNORE_RE") + "\n" + _extract_const("ARTIFACT_MUTATION_TOOLS")
    fns = "\n".join(
        _extract_fn(n)
        for n in (
            "_normalizeArtifactPath",
            "_artifactCandidatesFromText",
            "_artifactCandidatesFromToolCall",
        )
    )
    driver = (
        f"const S = {{ session: {{ workspace: {json.dumps(workspace)} }} }};\n"
        + consts + "\n" + fns + "\n"
        + "const out = _artifactCandidatesFromToolCall(JSON.parse(process.argv[1]));\n"
        + "process.stdout.write(JSON.stringify(out.map(x => x.path)));\n"
    )
    r = subprocess.run(
        [NODE, "-e", driver, json.dumps(tc)],
        capture_output=True, text=True, timeout=15,
    )
    assert r.returncode == 0, f"node failed: {r.stderr}"
    return json.loads(r.stdout)


# ---------------------------------------------------------------------------
# Fix 1: _normalizeArtifactPath strips workspace prefix from absolute paths
# ---------------------------------------------------------------------------

class TestNormalizeArtifactPathAbsolute:
    def test_absolute_path_with_workspace_prefix_strips_to_relative(self):
        """Absolute path /Users/test/ws/foo/bar.py → foo/bar.py."""
        ws = "/Users/test/ws"
        out = _normalize_via_node(
            [f"{ws}/foo/bar.py", "foo/bar.py"],
            workspace=ws,
        )
        assert out[0] == out[1] == "foo/bar.py", (
            f"Absolute path with workspace prefix must canonicalize to the "
            f"bare relative path so mutation tracking matches the preview path; "
            f"got {out}"
        )

    def test_absolute_path_without_workspace_prefix_stays_empty(self):
        """Absolute path not under workspace should return '' (can't match preview)."""
        ws = "/Users/test/ws"
        out = _normalize_via_node(
            ["/Users/other/project/foo.py"],
            workspace=ws,
        )
        assert out == [""], (
            f"Absolute path outside workspace should not produce a false "
            f"positive match; got {out}"
        )

    def test_absolute_path_with_trailing_slash_in_workspace(self):
        """Workspace with trailing slash should still match."""
        out = _normalize_via_node(
            ["/Users/test/ws/sub/deep.py"],
            workspace="/Users/test/ws/",
        )
        assert out == ["sub/deep.py"], (
            f"Workspace path with trailing slash must still strip prefix; got {out}"
        )

    def test_relative_paths_still_work(self):
        """Existing relative path canonicalization must not regress."""
        out = _normalize_via_node(
            ["foo.md", "./foo.md", "~/foo.md"],
            workspace="/Users/test/ws",
        )
        assert out == ["foo.md", "foo.md", "foo.md"], (
            f"Relative path canonicalization must not regress; got {out}"
        )

    def test_windows_absolute_path_with_workspace_prefix_strips_to_relative(self):
        """Windows absolute path C:\\Users\\test\\ws\\foo\\bar.py → foo/bar.py.

        Greptile P2 finding: Windows-style absolute paths were not treated as
        absolute because the code only checked for a leading '/'. The raw path
        was returned, preventing preview-match on Windows runtimes (#5747).
        """
        ws = "C:\\Users\\test\\ws"
        out = _normalize_via_node(
            [f"{ws}\\foo\\bar.py", "foo/bar.py"],
            workspace=ws,
        )
        assert out[0] == out[1] == "foo/bar.py", (
            f"Windows absolute path with workspace prefix must canonicalize to "
            f"the bare relative path so mutation tracking matches the preview "
            f"path; got {out}"
        )

    def test_windows_absolute_path_with_forward_slashes(self):
        """Windows path with forward slashes C:/Users/test/ws/foo.py → foo.py."""
        ws = "C:\\Users\\test\\ws"
        out = _normalize_via_node(
            ["C:/Users/test/ws/foo.py"],
            workspace=ws,
        )
        assert out == ["foo.py"], (
            f"Windows absolute path with forward slashes must strip workspace "
            f"prefix; got {out}"
        )

    def test_windows_absolute_path_without_workspace_prefix_stays_empty(self):
        """Windows absolute path not under workspace should return ''."""
        ws = "C:\\Users\\test\\ws"
        out = _normalize_via_node(
            ["C:\\Users\\other\\project\\foo.py"],
            workspace=ws,
        )
        assert out == [""], (
            f"Windows absolute path outside workspace should not produce a "
            f"false positive match; got {out}"
        )

    def test_no_workspace_session(self):
        """When S.session has no workspace, absolute paths should return ''."""
        # Pass empty workspace — the guard should skip prefix stripping
        out = _normalize_via_node(
            ["/Users/test/ws/foo.py"],
            workspace="",
        )
        assert out == [""], (
            f"Without a workspace, absolute paths should not match; got {out}"
        )

    def test_write_file_absolute_path_matches_preview(self):
        """write_file with absolute path must produce a candidate that
        matches the relative preview path."""
        ws = "/Users/test/ws"
        tc = {
            "name": "write_file",
            "args": {"path": f"{ws}/src/main.py"},
        }
        paths = _candidates_via_node(tc, workspace=ws)
        assert "src/main.py" in paths, (
            f"write_file with absolute path must produce a relative candidate "
            f"that matches the preview path; got {paths}"
        )

    def test_patch_absolute_path_matches_preview(self):
        """patch with absolute path must produce a candidate that
        matches the relative preview path."""
        ws = "/Users/test/ws"
        tc = {
            "name": "patch",
            "args": {"path": f"{ws}/config/settings.json"},
        }
        paths = _candidates_via_node(tc, workspace=ws)
        assert "config/settings.json" in paths, (
            f"patch with absolute path must produce a relative candidate "
            f"that matches the preview path; got {paths}"
        )


# ---------------------------------------------------------------------------
# Fix 2 (layout guard): loadDir re-hides fileTree when preservePreview
# and preview is open — independent of mutation detection
# ---------------------------------------------------------------------------

class TestLoadDirPreservePreviewLayout:
    def test_load_dir_rehides_filetree_when_preserve_preview(self):
        """loadDir must re-hide fileTree after renderFileTree() when
        preservePreview=true and a preview is open, preventing the
        half-screen layout bug."""
        block = _extract_fn("loadDir")
        # The fix should be present: after renderFileTree(), if preservePreview
        # and _previewCurrentPath, hide fileTree.
        # We check for the presence of the guard pattern.
        compact = block.replace(" ", "")
        assert "preservePreview" in compact, "loadDir must use preservePreview option"
        # The fix: after renderFileTree(), re-hide fileTree when preview is open
        assert "_previewCurrentPath" in compact, (
            "loadDir must reference _previewCurrentPath for the layout guard"
        )
        # Check the actual hide logic exists
        assert "fileTree" in compact and "display" in compact and "none" in compact, (
            "loadDir must hide fileTree (display=none) when preservePreview "
            "and a preview is open to prevent the half-screen layout bug"
        )

    def test_load_dir_guard_after_render_file_tree(self):
        """The fileTree hide must come AFTER renderFileTree() call, not before."""
        block = _extract_fn("loadDir")
        render_idx = block.find("renderFileTree()")
        assert render_idx != -1, "renderFileTree() call not found in loadDir"
        # Find the fileTree hide after renderFileTree
        after_render = block[render_idx:]
        assert "_previewCurrentPath" in after_render, (
            "The preservePreview layout guard must come after renderFileTree() "
            "so it can counteract renderFileTree's unconditional display='' restore"
        )

    def test_render_file_tree_still_unconditional_display(self):
        """renderFileTree in ui.js should still restore display='' — the fix
        is in loadDir, not in renderFileTree (方案A, minimal change)."""
        ui_js = (REPO / "static" / "ui.js").read_text(encoding="utf-8")
        fn_start = ui_js.index("function renderFileTree()")
        brace = ui_js.index("{", fn_start)
        depth = 0
        fn_end = None
        for i in range(brace, len(ui_js)):
            c = ui_js[i]
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    fn_end = i + 1
                    break
        fn = ui_js[fn_start:fn_end]
        assert "box.style.display=''" in fn, (
            "renderFileTree must still unconditionally restore display='' — "
            "the half-screen fix is in loadDir (方案A), not in renderFileTree"
        )
