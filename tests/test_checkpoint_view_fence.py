"""Run the node-level view-fence suite (tests/test_view_fence_node.mjs).

PR #7075 review item #5 (async view fence): a restore started on view A must
never apply its projection to view B; a stale modal must not read another
session's ``S.messages`` slot; ``S.restoreInFlight`` needs an owner.  The
behaviour lives in the FENCE-BLOCK range of ``static/ui.js``; the .mjs test
evals that exact shipped source range against a fake ``S`` — no duplicated
logic that could drift from the browser file.

This wrapper keeps the node suite inside the default pytest run so CI (which
sets up Node 20 for the ESLint gate) executes it on every change.  Graceful
skip mirrors ``tests/test_static_js_runtime_lint.py``: without node on PATH
the test skips instead of failing environments that only run pytest.
"""
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "tests" / "test_view_fence_node.mjs"
NODE = shutil.which("node")


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
@pytest.mark.skipif(not SCRIPT.exists(), reason="fence node suite missing")
def test_view_fence_node_suite():
    proc = subprocess.run(
        [NODE, "--test", str(SCRIPT)],
        capture_output=True,
        text=True,
        cwd=str(REPO),
        timeout=120,
    )
    tail = ((proc.stdout or "") + (proc.stderr or ""))[-1200:]
    assert proc.returncode == 0, (
        f"node --test failed (rc={proc.returncode})\n--- node output tail ---\n{tail}"
    )
