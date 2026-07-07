"""Regression: workspace preview auto-refresh + half-screen layout bug (#5747).

Three root causes fixed in static/workspace.js:

1. _normalizeArtifactPath() did not strip the workspace prefix from absolute
   paths. Tools like terminal/execute_code pass absolute paths
   ("/Users/x/ws/foo.py") while the file-tree preview records a bare relative
   path ("foo.py"). The two never compared equal, so mutation tracking never
   fired and the preview stayed stale.

2. ARTIFACT_MUTATION_TOOLS did not include 'terminal' or 'execute_code', so
   file edits made through those tools were invisible to mutation tracking.
   Additionally, terminal/execute_code args don't have standard path/file_path
   fields — their args are {command:"..."} / {code:"..."} — so the existing
   structured-arg extraction can't find paths. A new heuristic mines file-path
   tokens from the command/code text.

3. loadDir() calls renderFileTree() which unconditionally sets
   box.style.display='' (ui.js), restoring the fileTree to visible. When
   preservePreview=true and a preview is open, this left both fileTree and
   previewArea visible (each flex:1 in a flex-direction:column right panel),
   producing a half-screen layout. Fix: after renderFileTree(), re-hide
   fileTree when preservePreview && _previewCurrentPath.

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
            "_artifactPathsFromShellCommand",
            "_artifactPathsFromPythonCode",
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


# ---------------------------------------------------------------------------
# Fix 2: terminal/execute_code detected as mutation tools with path extraction
# ---------------------------------------------------------------------------

class TestTerminalExecuteCodeMutationTracking:
    def test_terminal_in_artifact_mutation_tools(self):
        """terminal must be in ARTIFACT_MUTATION_TOOLS set."""
        src = _extract_const("ARTIFACT_MUTATION_TOOLS")
        assert "'terminal'" in src, (
            "terminal must be in ARTIFACT_MUTATION_TOOLS so file edits via "
            "shell commands (sed, echo >, tee) are tracked for preview refresh"
        )

    def test_execute_code_in_artifact_mutation_tools(self):
        """execute_code must be in ARTIFACT_MUTATION_TOOLS set."""
        src = _extract_const("ARTIFACT_MUTATION_TOOLS")
        assert "'execute_code'" in src, (
            "execute_code must be in ARTIFACT_MUTATION_TOOLS so file edits "
            "via Python scripts are tracked for preview refresh"
        )

    def test_terminal_sed_command_extracts_path(self):
        """terminal with `sed -i 's/old/new/' foo.py` should extract foo.py."""
        tc = {
            "name": "terminal",
            "args": {"command": "sed -i 's/old/new/g' foo.py"},
        }
        paths = _candidates_via_node(tc)
        assert "foo.py" in paths, (
            f"terminal sed -i command must extract the target file path for "
            f"mutation tracking; got {paths}"
        )

    def test_terminal_redirect_command_extracts_path(self):
        """terminal with `echo "x" > bar.py` should extract bar.py."""
        tc = {
            "name": "terminal",
            "args": {"command": 'echo "content" > bar.py'},
        }
        paths = _candidates_via_node(tc)
        assert "bar.py" in paths, (
            f"terminal redirect (>) command must extract the target file; got {paths}"
        )

    def test_terminal_append_command_extracts_path(self):
        """terminal with `echo "x" >> bar.py` should extract bar.py."""
        tc = {
            "name": "terminal",
            "args": {"command": 'echo "line" >> bar.py'},
        }
        paths = _candidates_via_node(tc)
        assert "bar.py" in paths, (
            f"terminal append (>>) command must extract the target file; got {paths}"
        )

    def test_terminal_tee_command_extracts_path(self):
        """terminal with `echo "x" | tee foo.py` should extract foo.py."""
        tc = {
            "name": "terminal",
            "args": {"command": 'echo "content" | tee foo.py'},
        }
        paths = _candidates_via_node(tc)
        assert "foo.py" in paths, (
            f"terminal tee command must extract the target file; got {paths}"
        )

    def test_terminal_absolute_path_extracts_relative(self):
        """terminal with absolute path under workspace extracts relative path."""
        ws = "/Users/test/ws"
        tc = {
            "name": "terminal",
            "args": {"command": f"sed -i 's/a/b/' {ws}/src/main.py"},
        }
        paths = _candidates_via_node(tc, workspace=ws)
        assert "src/main.py" in paths, (
            f"terminal with absolute path under workspace must extract the "
            f"relative path so it matches the preview path; got {paths}"
        )

    def test_execute_code_writes_file(self):
        """execute_code with code that writes a file should extract the path."""
        tc = {
            "name": "execute_code",
            "args": {"code": "from hermes_tools import write_file\nwrite_file('output.py', 'print(1)')"},
        }
        paths = _candidates_via_node(tc)
        assert "output.py" in paths, (
            f"execute_code with write_file call must extract the target file; got {paths}"
        )

    def test_execute_code_open_write(self):
        """execute_code with open('file', 'w') should extract the path."""
        tc = {
            "name": "execute_code",
            "args": {"code": "with open('config.json', 'w') as f: f.write('{}')"},
        }
        paths = _candidates_via_node(tc)
        assert "config.json" in paths, (
            f"execute_code with open(..., 'w') must extract the target file; got {paths}"
        )

    def test_terminal_readonly_command_does_not_extract(self):
        """terminal with `cat foo.py` (read-only) should NOT extract foo.py.

        We only want to track files that are actually modified, not just read.
        cat/ls/grep are read-only and should not trigger preview refresh.
        """
        tc = {
            "name": "terminal",
            "args": {"command": "cat foo.py"},
        }
        paths = _candidates_via_node(tc)
        assert "foo.py" not in paths, (
            f"Read-only commands (cat) must not produce mutation candidates; got {paths}"
        )

    def test_terminal_ls_command_does_not_extract(self):
        """terminal with `ls` should not produce any file candidates."""
        tc = {
            "name": "terminal",
            "args": {"command": "ls -la"},
        }
        paths = _candidates_via_node(tc)
        assert paths == [], (
            f"ls command must not produce mutation candidates; got {paths}"
        )


# ---------------------------------------------------------------------------
# Fix 3: loadDir re-hides fileTree when preservePreview and preview is open
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
