"""Regression: workspace preview auto-refresh + half-screen layout bug (#5747).

Two root causes fixed in static/workspace.js:

1. _normalizeArtifactPath() did not strip the workspace prefix from absolute
   paths. Tools like write_file/patch pass absolute paths
   ("/Users/x/ws/foo.py") while the file-tree preview records a bare relative
   path ("foo.py"). The two never compared equal, so mutation tracking never
   fired and the preview stayed stale.

   The fix reuses the proven prefix-strip pattern from openArtifactPath()
   (workspace.js:585-593): after stripping ~/ and ./ prefixes, strip the
   S.session.workspace prefix from absolute paths. The strip is platform-aware
   (maintainer review #5752):
     - Windows-style absolutes (drive letter / backslash UNC) fold backslashes
       and strip the workspace prefix case-insensitively;
     - POSIX absolutes keep backslash and colon as legal filename characters
       and strip exact-case;
     - a forward-slash "//" prefix is NOT treated as Windows evidence (POSIX
       permits "//" at path start);
     - extension-less root files (Makefile/LICENSE/Dockerfile) are no longer
       rejected by a bare-name guard, so structured mutator args and patch
       headers naming them stay trackable.

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
    # Keep an "async " prefix when the source declares one (e.g.
    # refreshOpenPreviewIfMutated) so extracted bodies with `await` stay legal.
    if start > 0 and WORKSPACE_JS[start - 6 : start] == "async ":
        start -= 6
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
    helpers = "\n".join(
        _extract_fn(n) for n in ("_isWindowsStylePath", "_stripWorkspacePrefix", "_canonicalizeRelativePath")
    )
    driver = (
        f"const S = {{ session: {{ workspace: {json.dumps(workspace)} }} }};\n"
        + ignore_re + "\n" + helpers + "\n" + fn + "\n"
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
            "_isWindowsStylePath",
            "_stripWorkspacePrefix",
            "_canonicalizeRelativePath",
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


def _note_mutations_via_node(tc, workspace="", preview="foo.py"):
    """Drive the production caller chain: noteWorkspaceMutationsFromToolCall
    → _artifactCandidatesFromToolCall → _normalizeArtifactPath, then check
    whether the open preview is considered mutated."""
    assert NODE is not None  # module is skipped when node is missing
    consts = _extract_const("ARTIFACT_IGNORE_RE") + "\n" + _extract_const("ARTIFACT_MUTATION_TOOLS")
    fns = "\n".join(
        _extract_fn(n)
        for n in (
            "_isWindowsStylePath",
            "_stripWorkspacePrefix",
            "_canonicalizeRelativePath",
            "_normalizeArtifactPath",
            "_artifactCandidatesFromText",
            "_artifactCandidatesFromToolCall",
            "noteWorkspaceMutationsFromToolCall",
            "_isOpenPreviewPathMutated",
        )
    )
    driver = (
        f"const S = {{ session: {{ workspace: {json.dumps(workspace)} }} }};\n"
        + "const _turnMutatedPreviewPaths = new Set();\n"
        + f"let _previewCurrentPath = {json.dumps(preview)};\n"
        + consts + "\n" + fns + "\n"
        + "noteWorkspaceMutationsFromToolCall(JSON.parse(process.argv[1]));\n"
        + "const out = _isOpenPreviewPathMutated();\n"
        + "process.stdout.write(JSON.stringify({mutated: [..._turnMutatedPreviewPaths], openMutated: out}));\n"
    )
    r = subprocess.run(
        [NODE, "-e", driver, json.dumps(tc)],
        capture_output=True, text=True, timeout=15,
    )
    assert r.returncode == 0, f"node failed: {r.stderr}"
    return json.loads(r.stdout)


def _refresh_chain_via_node(tc, workspace="", preview="foo.py"):
    """Drive the FULL production chain through the refresh sink:

    _artifactCandidatesFromToolCall → noteWorkspaceMutationsFromToolCall →
    _isOpenPreviewPathMutated → refreshOpenPreviewIfMutated → openFile spy.

    openFile is spied (it touches the DOM); everything upstream is the real
    extracted function. Returns the list of openFile(path, opts) calls."""
    assert NODE is not None  # module is skipped when node is missing
    consts = _extract_const("ARTIFACT_IGNORE_RE") + "\n" + _extract_const("ARTIFACT_MUTATION_TOOLS")
    fns = "\n".join(
        _extract_fn(n)
        for n in (
            "_isWindowsStylePath",
            "_stripWorkspacePrefix",
            "_canonicalizeRelativePath",
            "_normalizeArtifactPath",
            "_artifactCandidatesFromText",
            "_artifactCandidatesFromToolCall",
            "noteWorkspaceMutationsFromToolCall",
            "_isOpenPreviewPathMutated",
            "refreshOpenPreviewIfMutated",
        )
    )
    driver = (
        f"const S = {{ session: {{ workspace: {json.dumps(workspace)} }} }};\n"
        + "const _turnMutatedPreviewPaths = new Set();\n"
        + f"let _previewCurrentPath = {json.dumps(preview)};\n"
        + "let _previewDirty = false;\n"
        + "const openFileCalls = [];\n"
        + "const openFile = async (path, opts) => { openFileCalls.push([path, opts]); };\n"
        + consts + "\n" + fns + "\n"
        + "noteWorkspaceMutationsFromToolCall(JSON.parse(process.argv[1]));\n"
        + "(async () => { await refreshOpenPreviewIfMutated(); "
        + "process.stdout.write(JSON.stringify(openFileCalls)); })();\n"
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
# Maintainer review #5752: platform-aware path canonicalization edges
# (drive-letter / backslash-UNC only, POSIX keeps \ and :, no bare-name
# rejection for extension-less root files)
# ---------------------------------------------------------------------------

class TestNormalizeArtifactPathPlatformEdges:
    def test_extensionless_root_file_absolute_stays_visible(self):
        """Absolute /Users/test/ws/Makefile must normalize to bare 'Makefile'.

        Maintainer edge 1: the old `if(!/[./]/.test(path))` bare-name guard
        dropped extension-less root files (Makefile/LICENSE/Dockerfile) after
        prefix stripping, so their previews could never match a mutation.
        """
        ws = "/Users/test/ws"
        out = _normalize_via_node(
            [f"{ws}/Makefile", f"{ws}/LICENSE", f"{ws}/Dockerfile"],
            workspace=ws,
        )
        assert out == ["Makefile", "LICENSE", "Dockerfile"], (
            f"Extension-less workspace-root files must survive normalization; "
            f"got {out}"
        )

    def test_extensionless_root_file_bare_relative_stays_visible(self):
        """A bare relative 'Makefile' preview path must not be rejected."""
        out = _normalize_via_node(["Makefile", "LICENSE"], workspace="/Users/test/ws")
        assert out == ["Makefile", "LICENSE"], (
            f"Bare relative extension-less names must survive normalization; "
            f"got {out}"
        )

    def test_posix_forward_slash_prefix_not_treated_as_windows(self):
        """POSIX '//'-prefixed absolute must not be treated as a Windows UNC.

        Maintainer edge 2: a generic forward-slash '//' is a legal POSIX path
        start, not evidence of Windows. Such a path outside the workspace must
        fail closed ('') rather than be folded or mis-stripped.
        """
        ws = "/Users/test/ws"
        out = _normalize_via_node(["//server/share/foo.py", "//Users/test/ws/foo.py"], workspace=ws)
        assert out == ["", ""], (
            f"POSIX //-prefixed paths must fail closed, not be treated as "
            f"Windows UNC; got {out}"
        )

    def test_posix_backslash_kept_as_filename_character(self):
        """POSIX file names may legally contain '\\' — must not be folded.

        Maintainer edge 3: folding backslashes unconditionally would turn a
        file literally named 'a\\b.py' into 'a/b.py', breaking preview-match.
        """
        ws = "/Users/test/ws"
        out = _normalize_via_node(
            [f"{ws}/a\\b.py", "a\\b.py"],
            workspace=ws,
        )
        assert out == ["a\\b.py", "a\\b.py"], (
            f"POSIX backslash must survive as a filename character; got {out}"
        )

    def test_windows_unc_absolute_strips_workspace_prefix(self):
        """Backslash UNC absolute \\\\server\\share\\ws\\foo.py → foo.py.

        Maintainer edge 4: canonical backslash UNC roots are Windows absolutes
        and must strip the workspace prefix.
        """
        ws = "\\\\server\\share\\ws"
        out = _normalize_via_node(
            [f"{ws}\\foo.py", "foo/bar.py"],
            workspace=ws,
        )
        assert out == ["foo.py", "foo/bar.py"], (
            f"UNC absolute path with workspace prefix must canonicalize; got {out}"
        )

    def test_windows_unc_strips_case_insensitively(self):
        """UNC workspace \\\\SERVER\\Share\\WS must match \\\\server\\share\\ws\\…."""
        ws = "\\\\SERVER\\Share\\WS"
        out = _normalize_via_node(
            ["\\\\server\\share\\ws\\foo.py"],
            workspace=ws,
        )
        assert out == ["foo.py"], (
            f"UNC workspace prefix must strip case-insensitively; got {out}"
        )

    def test_windows_drive_letter_strips_case_insensitively(self):
        """Drive-letter workspace C:\\Users\\Test\\WS must match c:\\users\\test\\ws\\…."""
        ws = "C:\\Users\\Test\\WS"
        out = _normalize_via_node(
            ["c:\\users\\test\\ws\\foo.py"],
            workspace=ws,
        )
        assert out == ["foo.py"], (
            f"Drive-letter workspace prefix must strip case-insensitively; got {out}"
        )

    def test_windows_path_outside_workspace_fails_closed(self):
        """Windows absolute outside the workspace must return '' (no false positive)."""
        ws = "C:\\Users\\test\\ws"
        out = _normalize_via_node(
            ["D:\\other\\project\\foo.py"],
            workspace=ws,
        )
        assert out == [""], (
            f"Windows absolute outside workspace must fail closed; got {out}"
        )

    def test_write_file_extensionless_root_matches_preview(self):
        """write_file with absolute /Users/test/ws/Makefile must produce the
        bare 'Makefile' candidate so the root-file preview can refresh."""
        ws = "/Users/test/ws"
        tc = {
            "name": "write_file",
            "args": {"path": f"{ws}/Makefile"},
        }
        paths = _candidates_via_node(tc, workspace=ws)
        assert "Makefile" in paths, (
            f"write_file on an extension-less root file must produce a "
            f"matching candidate; got {paths}"
        )

    def test_strip_workspace_prefix_returns_original_when_outside(self):
        """_stripWorkspacePrefix must return the input unchanged when the path
        is outside the workspace — display keeps the raw path, mutation
        tracking fails closed via its caller."""
        ws = "/Users/test/ws"
        assert NODE is not None  # module is skipped when node is missing
        ignore_re = _extract_const("ARTIFACT_IGNORE_RE")
        helpers = "\n".join(
            _extract_fn(n) for n in ("_isWindowsStylePath", "_stripWorkspacePrefix", "_canonicalizeRelativePath")
        )
        driver = (
            f"const S = {{ session: {{ workspace: {json.dumps(ws)} }} }};\n"
            + helpers + "\n"
            + "const out = JSON.parse(process.argv[1]).map(_stripWorkspacePrefix);\n"
            + "process.stdout.write(JSON.stringify(out));\n"
        )
        r = subprocess.run(
            [NODE, "-e", driver, json.dumps([f"{ws}/foo.py", "/outside/x.py", "rel.py"])],
            capture_output=True, text=True, timeout=15,
        )
        assert r.returncode == 0, f"node failed: {r.stderr}"
        out = json.loads(r.stdout)
        assert out == ["foo.py", "/outside/x.py", "rel.py"], (
            f"_stripWorkspacePrefix must strip under-workspace paths and leave "
            f"others untouched; got {out}"
        )

    def test_posix_drive_lookalike_relative_survives_repeated_normalization(self):
        """POSIX workspace: /Users/test/ws/C:/foo.py and bare C:/foo.py are
        legal POSIX names and must survive, including a second normalization
        pass (composed candidate → mutation-set → open-preview re-normalizes
        the same value — maintainer re-gate #5752, blocker 1)."""
        ws = "/Users/test/ws"
        out = _normalize_via_node(
            [f"{ws}/C:/foo.py", "C:/foo.py"],
            workspace=ws,
        )
        assert out == ["C:/foo.py", "C:/foo.py"], (
            f"POSIX drive-lookalike relative names must survive normalization; "
            f"got {out}"
        )
        # Idempotency: a second pass over the normalized value must not drop it.
        out2 = _normalize_via_node(out, workspace=ws)
        assert out2 == out, (
            f"Repeated normalization must be idempotent for drive lookalikes; "
            f"got {out2} after {out}"
        )

    def test_posix_workspace_root_with_literal_backslash(self):
        """A POSIX workspace root may itself contain a literal backslash
        (/tmp/a\\b) — it must not be folded before flavor is decided
        (maintainer re-gate #5752, blocker 3)."""
        ws = "/tmp/a\\b"
        out = _normalize_via_node(
            [f"{ws}/file.py"],
            workspace=ws,
        )
        assert out == ["file.py"], (
            f"POSIX workspace root with a literal backslash must still strip; "
            f"got {out}"
        )

    def test_posix_dotdot_escape_fails_closed(self):
        """Absolute path that lexically starts under the workspace but resolves
        outside it must fail closed (maintainer re-gate #5752, blocker 2)."""
        ws = "/Users/test/ws"
        out = _normalize_via_node(
            [f"{ws}/../outside/foo.py", f"{ws}/a/../../x.py"],
            workspace=ws,
        )
        assert out == ["", ""], (
            f"Dot-dot escapes from the workspace must fail closed; got {out}"
        )

    def test_windows_dotdot_escape_fails_closed(self):
        """Windows absolute with '..' escaping the workspace must fail closed."""
        ws = "C:\\Users\\test\\ws"
        out = _normalize_via_node(
            [f"{ws}\\..\\outside\\foo.py"],
            workspace=ws,
        )
        assert out == [""], (
            f"Windows dot-dot escape must fail closed; got {out}"
        )

    def test_in_workspace_dotdot_canonicalizes(self):
        """In-workspace '..' segments canonicalize so they still match the
        resolved preview path (/ws/sub/../x.py → x.py)."""
        ws = "/Users/test/ws"
        out = _normalize_via_node(
            [f"{ws}/sub/../x.py", f"{ws}/./x.py"],
            workspace=ws,
        )
        assert out == ["x.py", "x.py"], (
            f"In-workspace dot-dot segments must canonicalize, not drop; got {out}"
        )

    def test_sibling_prefix_workspace_not_confused(self):
        """/Users/test/ws2 must not match a /Users/test/ws workspace prefix and
        vice versa (sibling-prefix boundary)."""
        out = _normalize_via_node(
            ["/Users/test/ws2/x.py", "/Users/test/ws/x.py"],
            workspace="/Users/test/ws",
        )
        assert out == ["", "x.py"], (
            f"Sibling workspace prefixes must not cross-match; got {out}"
        )
        out2 = _normalize_via_node(
            ["/Users/test/ws/x.py"],
            workspace="/Users/test/ws2",
        )
        assert out2 == [""], (
            f"Workspace /ws2 must not strip /ws paths; got {out2}"
        )

    def test_write_file_drive_lookalike_candidate_survives(self):
        """write_file with absolute /Users/test/ws/C:/foo.py must produce the
        'C:/foo.py' candidate (legal POSIX name), not drop it as a bogus
        Windows absolute."""
        ws = "/Users/test/ws"
        tc = {
            "name": "write_file",
            "args": {"path": f"{ws}/C:/foo.py"},
        }
        paths = _candidates_via_node(tc, workspace=ws)
        assert "C:/foo.py" in paths, (
            f"write_file on a POSIX drive-lookalike name must produce a "
            f"matching candidate; got {paths}"
        )

    def test_strip_workspace_prefix_windows_drive_and_unc_descendants(self):
        """Display/open parity: _stripWorkspacePrefix must strip accepted
        drive and UNC descendants on a Windows workspace (used by both
        displayPath and openArtifactPath)."""
        assert NODE is not None  # module is skipped when node is missing
        helpers = "\n".join(
            _extract_fn(n) for n in ("_isWindowsStylePath", "_stripWorkspacePrefix")
        )
        cases = [
            ("C:\\Users\\test\\ws", "C:\\Users\\test\\ws\\foo.py", "foo.py"),
            ("C:\\Users\\test\\ws", "c:\\users\\test\\ws\\sub\\x.py", "sub/x.py"),
            ("\\\\server\\share\\ws", "\\\\server\\share\\ws\\foo.py", "foo.py"),
        ]
        for ws, path, want in cases:
            driver = (
                f"const S = {{ session: {{ workspace: {json.dumps(ws)} }} }};\n"
                + helpers + "\n"
                + "const out = _stripWorkspacePrefix(JSON.parse(process.argv[1])[0]);\n"
                + "process.stdout.write(JSON.stringify(out));\n"
            )
            r = subprocess.run(
                [NODE, "-e", driver, json.dumps([path])],
                capture_output=True, text=True, timeout=15,
            )
            assert r.returncode == 0, f"node failed: {r.stderr}"
            got = json.loads(r.stdout)
            assert got == want, (
                f"_stripWorkspacePrefix({path!r}) on workspace {ws!r} must "
                f"yield {want!r}, got {got!r}"
            )

    def test_no_workspace_windows_absolutes_fail_closed(self):
        """Without workspace authority, every unambiguous absolute flavor —
        drive-forward, drive-backslash, UNC — must fail closed rather than
        survive as workspace-relative candidates (maintainer re-gate #5752,
        must-fix)."""
        out = _normalize_via_node(
            ["C:/foo.py", "C:\\foo.py", "\\\\server\\share\\foo.py"],
            workspace="",
        )
        assert out == ["", "", ""], (
            f"No-workspace Windows absolutes must fail closed; got {out}"
        )

    def test_no_workspace_relative_controls_survive(self):
        """Ordinary relative names must survive with no workspace set."""
        out = _normalize_via_node(
            ["foo.py", "sub/x.py", "foo\\bar.py"],
            workspace="",
        )
        assert out == ["foo.py", "sub/x.py", "foo\\bar.py"], (
            f"No-workspace relative names must survive; got {out}"
        )

    def test_windows_workspace_root_relative_and_drive_relative_fail_closed(self):
        """Windows workspace: root-relative ('\\foo.py') and drive-relative
        ('C:foo.py') absolutes must fail closed, not become workspace-relative
        candidates (maintainer re-gate #5752, must-fix)."""
        ws = "C:\\Users\\test\\ws"
        out = _normalize_via_node(
            ["\\foo.py", "C:foo.py"],
            workspace=ws,
        )
        assert out == ["", ""], (
            f"Windows root-relative and drive-relative inputs must fail "
            f"closed; got {out}"
        )

    def test_windows_workspace_relative_controls_survive(self):
        """Windows workspace: plain relative names and backslash-separated
        relatives still normalize to forward-slash form."""
        ws = "C:\\Users\\test\\ws"
        out = _normalize_via_node(
            ["foo.py", "foo\\bar.py"],
            workspace=ws,
        )
        assert out == ["foo.py", "foo/bar.py"], (
            f"Windows-relative names must survive and fold separators; got {out}"
        )

    def test_composed_mutation_to_preview_no_workspace_windows_absolute(self):
        """Production chain: with no workspace, a write_file on a Windows
        absolute must not enter the mutation set and must not mark the open
        preview mutated (maintainer re-gate #5752, composed row)."""
        tc = {"name": "write_file", "args": {"path": "C:/foo.py"}}
        out = _note_mutations_via_node(tc, workspace="", preview="foo.py")
        assert out == {"mutated": [], "openMutated": False}, (
            f"No-workspace Windows absolute must not pollute the mutation "
            f"set; got {out}"
        )

    def test_composed_mutation_to_preview_control_row(self):
        """Control for the composed row: a valid workspace-relative mutation
        through the same caller chain marks the open preview mutated."""
        ws = "/Users/test/ws"
        tc = {"name": "write_file", "args": {"path": f"{ws}/foo.py"}}
        out = _note_mutations_via_node(tc, workspace=ws, preview="foo.py")
        assert out == {"mutated": ["foo.py"], "openMutated": True}, (
            f"Valid mutation must flow through the caller chain and mark the "
            f"open preview; got {out}"
        )

    def test_composed_chain_no_workspace_windows_absolute_zero_open_file_calls(self):
        """Full refresh sink: a no-workspace Windows-absolute mutation must
        produce ZERO openFile calls — it never becomes a candidate, never
        marks the preview mutated, and never reaches the refresh sink
        (maintainer re-gate #5752, sink row)."""
        tc = {"name": "write_file", "args": {"path": "C:/foo.py"}}
        calls = _refresh_chain_via_node(tc, workspace="", preview="foo.py")
        assert calls == [], (
            f"No-workspace Windows absolute must not reach the refresh sink; "
            f"got {calls}"
        )

    def test_composed_chain_valid_mutation_exactly_one_open_file_call(self):
        """Full refresh sink: a valid workspace mutation must produce exactly
        one openFile('foo.py', {bustCache:true}) call — the real refresh path
        (maintainer re-gate #5752, sink row)."""
        ws = "/Users/test/ws"
        tc = {"name": "write_file", "args": {"path": f"{ws}/foo.py"}}
        calls = _refresh_chain_via_node(tc, workspace=ws, preview="foo.py")
        assert calls == [["foo.py", {"bustCache": True}]], (
            f"Valid mutation must reach the refresh sink exactly once with "
            f"bustCache; got {calls}"
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
