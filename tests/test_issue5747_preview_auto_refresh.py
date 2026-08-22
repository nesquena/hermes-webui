"""Regression (complement to #5752): file preview auto-refreshes after
terminal/execute_code mutations, and extension-less root files (Makefile,
Dockerfile) survive mutation tracking (#5747).

#5752 already fixes the absolute-vs-relative workspace prefix mismatch and the
half-screen layout split. This PR covers the two parts #5752 explicitly scoped
out as follow-ups:

1. Tools whose mutation target lives in command/code TEXT rather than a
   structured path arg — `terminal` (sed -i, shell redirects) and
   `execute_code` (python open(...,'w')) were invisible to
   ARTIFACT_MUTATION_TOOLS. We scan their args + result text for path-like
   tokens and for a direct mention of the open preview's path/basename.
2. Extension-less root files like `Makefile`/`Dockerfile` were dropped by the
   `[./]` filter in _normalizeArtifactPath() even when passed as structured
   tool args — {allowBare:true} lets structured args through while keeping the
   strict filter for text-mined candidates (so prose like "hello" can't become
   an artifact).

Drives the ACTUAL static/workspace.js helpers via node so it can't drift from
a Python mirror.
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


def _normalize_via_node(paths, allow_bare=False):
    ignore_re = _extract(r"const ARTIFACT_IGNORE_RE = /.*?/;")
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
    opts = "{allowBare:true}" if allow_bare else "undefined"
    driver = (
        ignore_re + "\n" + fn + "\n"
        + f"const out = JSON.parse(process.argv[1]).map(p=>_normalizeArtifactPath(p,{opts}));\n"
        + "process.stdout.write(JSON.stringify(out));\n"
    )
    r = subprocess.run(
        [NODE, "-e", driver, json.dumps(paths)],
        capture_output=True, text=True, timeout=15,
    )
    assert r.returncode == 0, f"node failed: {r.stderr}"
    return json.loads(r.stdout)


def test_extensionless_root_file_kept_when_allow_bare():
    """#5747: write_file(path='Makefile') must survive the [./] filter."""
    out = _normalize_via_node(["Makefile", "Dockerfile", "README"], allow_bare=True)
    assert out == ["Makefile", "Dockerfile", "README"], (
        f"extension-less root files must survive with allowBare (#5747); got {out}"
    )


def test_extensionless_root_file_still_rejected_for_text_mining():
    """The strict filter must remain for text-mined candidates (#5747)."""
    out = _normalize_via_node(["Makefile", "Dockerfile", "README"], allow_bare=False)
    assert out == ["", "", ""], (
        f"text-mined bare words must stay filtered so prose can't become an "
        f"artifact (#5747); got {out}"
    )


def test_allow_bare_does_not_weaken_existing_rejections():
    out = _normalize_via_node(
        ["./node_modules/x.js", "https://e.com/a", "./", "foo/bar.py"],
        allow_bare=True,
    )
    assert out == ["", "", "", "foo/bar.py"], (
        f"allowBare must not weaken ignore-dir/URL/empty rejections (#5747); got {out}"
    )


def test_text_mutation_tools_are_tracked():
    """#5747: terminal and execute_code must be scanned for preview paths."""
    assert "ARTIFACT_TEXT_MUTATION_TOOLS" in WORKSPACE_JS
    decl = _extract(r"const ARTIFACT_TEXT_MUTATION_TOOLS = new Set\(\[.*?\]\);")
    assert "terminal" in decl and "execute_code" in decl, decl
    block = _function_block(WORKSPACE_JS, "noteWorkspaceMutationsFromToolCall")
    assert "ARTIFACT_TEXT_MUTATION_TOOLS" in block, (
        "noteWorkspaceMutationsFromToolCall() must scan text-based mutation tools (#5747)"
    )


def test_text_path_tokens_extracts_dotted_paths():
    """Path-like tokens in terminal/execute_code text must be extractable."""
    start = WORKSPACE_JS.index("function _textPathTokens(")
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
    ignore_re = _extract(r"const ARTIFACT_IGNORE_RE = /.*?/;")
    norm_start = WORKSPACE_JS.index("function _normalizeArtifactPath(")
    norm_brace = WORKSPACE_JS.index("{", norm_start)
    depth = 0
    norm_end = None
    for i in range(norm_brace, len(WORKSPACE_JS)):
        c = WORKSPACE_JS[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                norm_end = i + 1
                break
    norm_fn = WORKSPACE_JS[norm_start:norm_end]
    driver = (
        ignore_re + "\n" + norm_fn + "\n" + fn + "\n"
        + "const out = _textPathTokens(process.argv[1]);\n"
        + "process.stdout.write(JSON.stringify(out));\n"
    )
    text = "sed -i 's/a/b/' static/style.css && echo done"
    r = subprocess.run(
        [NODE, "-e", driver, text],
        capture_output=True, text=True, timeout=15,
    )
    assert r.returncode == 0, f"node failed: {r.stderr}"
    assert "static/style.css" in json.loads(r.stdout), (
        f"terminal text must yield the mutated path token (#5747)"
    )


def test_preview_refresh_still_uses_open_file_with_bust_cache():
    block = _function_block(WORKSPACE_JS, "refreshOpenPreviewIfMutated")
    assert "openFile(_previewCurrentPath,{bustCache:true})" in block.replace(" ", ""), (
        "mutated preview must reload through openFile() with cache busting (#5747)"
    )
