"""Regression: _normalizeArtifactPath() canonicalizes ./ and ~/ prefixes (#3262).

The workspace preview reload-on-mutation tracking (#3262) compares the open
preview path against the set of paths the agent's tools touched during the
turn. File-tree opens record a bare workspace-relative path ("foo.md"), but a
tool argument can arrive as "./foo.md" or "~/foo.md". Before the fix,
_normalizeArtifactPath() did not strip those prefixes, so "./foo.md" != "foo.md"
in _turnMutatedPreviewPaths and an agent edit via a ./-prefixed path left the
open preview stale (pre-release Codex regression-gate finding).

This drives the ACTUAL _normalizeArtifactPath() from static/workspace.js via
node so it can't drift from a Python mirror.
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


def _extract(decl_regex: str) -> str:
    m = re.search(decl_regex, WORKSPACE_JS)
    assert m, f"definition not found: {decl_regex}"
    return m.group(0)


def _normalize_via_node(paths):
    ignore_re = _extract(r"const ARTIFACT_IGNORE_RE = /.*?/;")
    # Extract the full function body by brace-matching.
    start = WORKSPACE_JS.index("function _normalizeArtifactPath(")
    brace = WORKSPACE_JS.index("{", start)
    depth = 0
    end = None
    for i in range(brace, len(WORKSPACE_JS)):
        c = WORKSPACE_JS[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    fn = WORKSPACE_JS[start:end]
    driver = (
        ignore_re + "\n" + fn + "\n"
        + "const out = JSON.parse(process.argv[1]).map(_normalizeArtifactPath);\n"
        + "process.stdout.write(JSON.stringify(out));\n"
    )
    r = subprocess.run(
        [NODE, "-e", driver, json.dumps(paths)],
        capture_output=True, text=True, timeout=15,
    )
    assert r.returncode == 0, f"node failed: {r.stderr}"
    return json.loads(r.stdout)


def test_dot_slash_and_tilde_prefixes_canonicalize_to_bare_path():
    out = _normalize_via_node(["foo.md", "./foo.md", "~/foo.md", "././foo.md"])
    assert out == ["foo.md", "foo.md", "foo.md", "foo.md"], (
        f"./ and ~/ prefixes must canonicalize to the bare workspace-relative "
        f"path so mutation tracking matches a file-tree open (#3262); got {out}"
    )


def test_nested_relative_path_prefix_canonicalizes():
    out = _normalize_via_node(["sub/dir/x.py", "./sub/dir/x.py", "~/sub/dir/x.py"])
    assert out == ["sub/dir/x.py", "sub/dir/x.py", "sub/dir/x.py"], (
        f"prefix canonicalization must apply to nested paths too (#3262); got {out}"
    )


def test_canonicalization_preserves_ignore_and_url_rejection():
    # Canonicalization must not weaken the existing rejections.
    out = _normalize_via_node(["./node_modules/x.js", "https://e.com/a", "./"])
    assert out == ["", "", ""], (
        f"ignore-dir, URL, and empty-after-strip rejections must still hold "
        f"after prefix canonicalization (#3262); got {out}"
    )
