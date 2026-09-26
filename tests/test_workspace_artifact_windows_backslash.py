"""Regression: artifact clicks with Windows backslash paths open correctly.

Windows artifact paths are absolute backslash paths, e.g.
``D:\\proj\\dir\\file.md``, while ``openArtifactPath()`` /
``_workspacePathExists()`` split on ``/``. Before the fix:

* the workspace-prefix strip (``rel.startsWith(normWs)``) never matched, so the
  full absolute path was passed down, and
* ``_workspacePathExists()``'s ``split('/')`` treated the whole backslash path
  as a single filename, so ``/api/list`` never matched and the UI reported
  "cannot open file" (``file_open_failed``).

The fix normalizes both ``rel`` and the session workspace to ``/`` separators
before stripping. This drives the ACTUAL functions from static/workspace.js via
node (same pattern as test_issue3262_artifact_path_canonicalization.py) so the
test cannot drift from the real frontend code.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
WORKSPACE_JS = (REPO / "static" / "workspace.js").read_text(encoding="utf-8")
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")


def _extract_function(source: str, name: str) -> str:
    start = source.index(f"async function {name}(" if f"async function {name}(" in source else f"function {name}(")
    brace = source.index("{", start)
    depth = 0
    for pos in range(brace, len(source)):
        if source[pos] == "{":
            depth += 1
        elif source[pos] == "}":
            depth -= 1
            if depth == 0:
                return source[start : pos + 1]
    raise AssertionError(f"could not extract {name}")


def _run_artifact_open(path: str, workspace: str, entries_by_dir: dict) -> dict:
    """Run the real openArtifactPath + _workspacePathExists against stubbed deps.

    ``entries_by_dir`` maps a workspace-relative dir ('.' for root) to the list
    of entry names /api/list would return, so the existence check is exercised
    with realistic data. Returns the recorded calls (openFile / setStatus).
    """
    open_artifact = _extract_function(WORKSPACE_JS, "openArtifactPath")
    path_exists = _extract_function(WORKSPACE_JS, "_workspacePathExists")
    entries_json = json.dumps(entries_by_dir)
    driver = f"""
const S = {{ session: {{ session_id: 't', workspace: {json.dumps(workspace)} }} }};
const calls = [];
function switchWorkspacePanelTab() {{}}
function t(key) {{ return key; }}
function setStatus(msg) {{ calls.push({{status: msg}}); }}
function openFile(rel) {{ calls.push({{open: rel}}); }}
const ENTRIES_BY_DIR = {entries_json};
async function api(url) {{
  const m = url.match(/[?&]path=([^&]*)/);
  const dir = m ? decodeURIComponent(m[1]) : '.';
  calls.push({{api: dir}});
  return {{ entries: (ENTRIES_BY_DIR[dir] || []).map(name => ({{name, path: name}})) }};
}}
{path_exists}
{open_artifact}
(async () => {{
  await openArtifactPath({json.dumps(path)});
  process.stdout.write(JSON.stringify(calls));
}})();
"""
    r = subprocess.run(
        [NODE, "-e", driver],
        capture_output=True, text=True, timeout=15,
    )
    assert r.returncode == 0, f"node failed: {r.stderr}"
    return json.loads(r.stdout)


WS = r"D:\proj"


def test_windows_backslash_subdir_artifact_opens():
    """Subdir artifact with backslash absolute path strips to rel path and opens."""
    calls = _run_artifact_open(
        r"D:\proj\src\report.pdf",
        WS,
        {".": ["readme.md"], "src": ["report.pdf"]},
    )
    assert calls[-1] == {"open": "src/report.pdf"}, calls


def test_windows_backslash_root_artifact_opens():
    """Root-level artifact with backslash absolute path opens."""
    calls = _run_artifact_open(
        r"D:\proj\readme.md",
        WS,
        {".": ["readme.md"], "src": ["report.pdf"]},
    )
    assert calls[-1] == {"open": "readme.md"}, calls


def test_forward_slash_relative_behavior_unchanged():
    """Existing POSIX-style relative artifact clicks keep working (no regression)."""
    calls = _run_artifact_open(
        "./src/report.pdf",
        WS,
        {".": ["readme.md"], "src": ["report.pdf"]},
    )
    assert calls[-1] == {"open": "src/report.pdf"}, calls


def test_missing_artifact_still_reports_failure():
    """A genuinely missing artifact still surfaces file_open_failed."""
    calls = _run_artifact_open(
        r"D:\proj\src\missing.md",
        WS,
        {".": ["readme.md"], "src": ["report.pdf"]},
    )
    assert calls[-1] == {"status": "file_open_failed"}, calls