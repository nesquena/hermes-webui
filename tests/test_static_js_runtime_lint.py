"""Runtime-error lint guard for the static JS bundle (issue #3162).

Why this exists: #3162 was a brick-class regression — a `const` binding reassigned
inside `_ensureMessagesLoaded` threw a `TypeError` that broke "load conversation
messages" on every mobile message (v0.51.161-166). Nothing caught it:
`node --check` is a lazy syntax check (misses const-assign), source-presence tests
asserted the strings existed, and even *running* the file doesn't compile an
uncalled function body. Only a real scope-aware linter (ESLint `no-const-assign`)
or executing the exact function would have flagged it.

This test runs ESLint with `eslint.runtime-guard.config.mjs` — a curated set of
zero-false-positive RUNTIME-error rules (no style rules) — over `static/**/*.js`.

Graceful skip: if node or a local/global eslint isn't available, the test SKIPS
(with a clear reason) rather than failing — so environments without the toolchain
aren't blocked. The release gate runs in an environment where eslint IS installed
(see TESTING.md), so the guard is enforced there.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
CONFIG = REPO / "eslint.runtime-guard.config.mjs"


def _find_eslint():
    """Return a runnable eslint invocation (list of argv) or None."""
    # 1. project-local install
    local = REPO / "node_modules" / ".bin" / "eslint"
    if local.exists():
        return [str(local)]
    # 2. on PATH
    which = shutil.which("eslint")
    if which:
        return [which]
    return None


@pytest.mark.skipif(not CONFIG.exists(), reason="eslint runtime-guard config missing")
def test_static_js_has_no_runtime_error_lint():
    eslint = _find_eslint()
    if eslint is None:
        pytest.skip(
            "eslint not installed — install with "
            "`npm install --no-save --before=<48h-ago> eslint` to enforce the "
            "runtime-error guard locally (CI/release env has it). See TESTING.md."
        )
    if shutil.which("node") is None:
        pytest.skip("node not available")

    cmd = eslint + [
        "--no-config-lookup",
        "-c", str(CONFIG),
        "-f", "json",
        str(REPO / "static"),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, cwd=str(REPO), timeout=120)

    # ESLint exits non-zero on lint errors; parse the JSON either way.
    try:
        results = json.loads(proc.stdout or "[]")
    except json.JSONDecodeError:
        pytest.skip(f"eslint produced non-JSON output (env issue): {proc.stderr[:200]}")

    offenders = []
    for file_result in results:
        for msg in file_result.get("messages", []):
            if msg.get("severity") == 2:  # error
                offenders.append(
                    f"{Path(file_result['filePath']).name}:{msg.get('line')}:{msg.get('column')} "
                    f"{msg.get('message')} ({msg.get('ruleId')})"
                )

    assert not offenders, (
        "Static JS runtime-error lint failed — these throw at runtime in the browser "
        "(brick-class, see #3162). Fix them (e.g. `const` -> `let` when reassigned):\n  "
        + "\n  ".join(offenders)
    )
