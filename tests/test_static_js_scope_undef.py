"""Scope / undefined-reference guard for the static JS bundle (issue #3696).

Why this exists: #3696 was a brick-class regression — `_sessionAttentionState`
was declared *inside* `renderSessionListFromCache()` but called (un-guarded) from
a separate top-level function `_sidebarRowHasVisibleMessages`. Function hoisting is
scoped to the enclosing function, so the call threw
`ReferenceError: _sessionAttentionState is not defined` on every sidebar
cache-render and the session list went blank (v0.51.269, regressed by #3672).

Nothing caught it: `node --check` is a syntax check (a nested function IS valid
syntax), source-presence tests asserted the strings existed (they did — in the
wrong scope), and the existing `no-const-assign` runtime gate only covers
const-reassign / import-assign. This is a DIFFERENT runtime-error class:
referencing a name that isn't in scope.

`scripts/scope_undef_gate.py` models the WebUI's classic-`<script>` shared global
scope (all top-level symbols across every static file become one namespace), then
runs ESLint `no-undef` per file. Cross-file globals resolve; a function defined
only nested and called from a sibling scope is flagged. See that script's header
for the full design + the verified dynamic-global allowlist.

Graceful skip: if node or eslint isn't available the test SKIPS (the release gate
runs where eslint IS installed — see TESTING.md), so toolchain-free envs aren't
blocked.
"""
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
GATE = REPO / "scripts" / "scope_undef_gate.py"


@pytest.mark.skipif(not GATE.exists(), reason="scope_undef_gate.py missing")
def test_static_js_has_no_undefined_references():
    if shutil.which("eslint") is None:
        pytest.skip(
            "eslint not installed — install with "
            "`npm install --no-save --before=<48h-ago> eslint` to enforce the "
            "scope/undef guard locally (CI/release env has it). See TESTING.md."
        )
    if shutil.which("node") is None:
        pytest.skip("node not available")

    proc = subprocess.run(
        [sys.executable, str(GATE), str(REPO)],
        capture_output=True, text=True, timeout=180,
    )
    # Exit 0 = clean or eslint-skip; 1 = real finding; 2 = setup error.
    assert proc.returncode != 1, (
        "scope_undef_gate found undefined reference(s) that throw at runtime in the "
        "browser (brick-class, see #3696):\n" + proc.stdout + proc.stderr
    )
    if proc.returncode == 2:
        pytest.skip(f"scope_undef_gate setup issue (not a code failure): {proc.stdout[:300]}")
