"""Forward-looking ruff lint gate — in-suite enforcement.

The Python twin of ``tests/test_static_js_runtime_lint.py`` (the ESLint runtime
guard). Issue #3273.

What this asserts:

1. The whole tree is free of E9 (real syntax / IO / runtime-error) findings. This
   is the one ruleset we hold green tree-wide — there is no backlog of E9 errors,
   and a new one is always a genuine bug. (The F/B families have a known cosmetic
   backlog in the existing tree, deliberately NOT reformatted — see #3273 — so we
   do NOT assert those are tree-clean; they are enforced on NEW code only by
   ``scripts/ruff_lint.py --diff``, which runs in CI and the pre-release gate.)

2. The curated ruff config is present and shaped as intended (E9 + F + B selected,
   no global ignore of the F/B rules that would blind the new-code gate).

3. The line-scoped gate runner imports and its diff machinery works.

Skips cleanly when ruff is not installed, so a contributor without the dev tool is
never blocked — CI installs ruff so enforcement holds there. Same contract as the
ESLint guard.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _ruff_argv():
    """Resolve a runnable ruff invocation, or None."""
    if shutil.which("ruff"):
        return ["ruff"]
    probe = subprocess.run(
        [sys.executable, "-m", "ruff", "--version"], capture_output=True, text=True
    )
    if probe.returncode == 0:
        return [sys.executable, "-m", "ruff"]
    if shutil.which("uvx"):
        probe = subprocess.run(["uvx", "ruff", "--version"], capture_output=True, text=True)
        if probe.returncode == 0:
            return ["uvx", "ruff"]
    return None


RUFF = _ruff_argv()
ruff_required = pytest.mark.skipif(RUFF is None, reason="ruff not installed (dev-only tool; CI installs it)")


def test_pyproject_ruff_config_present_and_shaped():
    """The curated [tool.ruff] config exists and selects the intended families."""
    path = os.path.join(REPO_ROOT, "pyproject.toml")
    assert os.path.exists(path), "pyproject.toml with [tool.ruff] config is required (#3273)"
    text = open(path, encoding="utf-8").read()
    assert "[tool.ruff]" in text, "[tool.ruff] section missing"
    assert "[tool.ruff.lint]" in text, "[tool.ruff.lint] section missing"
    # The curated correctness-leaning set: pyflakes + syntax + bugbear.
    assert '"E9"' in text and '"F"' in text and '"B"' in text, (
        "ruff select must include E9, F, B (curated correctness ruleset)"
    )
    # Guard the design intent: no GLOBAL ignore of F401/F841 etc. A blanket ignore
    # would defeat the new-code gate (a stray unused import is the single most
    # common new-code defect). Per-file-ignores for tests are fine.
    lint_section = text.split("[tool.ruff.lint]", 1)[1]
    before_subtables = lint_section.split("[tool.ruff.lint.", 1)[0]
    assert "ignore = [" not in before_subtables and "ignore=[" not in before_subtables, (
        "do not globally ignore F/B rules — that blinds the forward gate; "
        "use per-file-ignores or scoped # noqa instead"
    )


@ruff_required
def test_tree_is_E9_clean():
    """No real syntax / IO / runtime-error (E9) findings anywhere in the tree.

    E9 is the one ruleset held green tree-wide: there is no backlog, and any new
    E9 finding is a genuine bug (the kind that bricks a module on import).
    """
    proc = subprocess.run(
        RUFF + ["check", "--select", "E9", "--output-format", "json", "--no-cache", "."],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    findings = json.loads(proc.stdout) if proc.stdout.strip() else []
    if findings:
        lines = [
            f"  {os.path.relpath(f['filename'], REPO_ROOT)}:{f['location']['row']}  "
            f"{f.get('code')}  {f.get('message', '').strip()}"
            for f in findings
        ]
        pytest.fail("ruff E9 (syntax/runtime) findings present:\n" + "\n".join(lines))


@ruff_required
def test_known_F821_false_positives_are_noqa_suppressed():
    """The two known F821 false positives carry scoped # noqa, keeping the tree F821-clean.

    Regression guard: if someone strips a noqa or reintroduces the bare pattern,
    this catches it. (#3273 documented exactly two F821 hits, both false positives.)
    """
    proc = subprocess.run(
        RUFF + ["check", "--select", "F821", "--output-format", "json", "--no-cache", "."],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    findings = json.loads(proc.stdout) if proc.stdout.strip() else []
    locs = [f"{os.path.relpath(f['filename'], REPO_ROOT)}:{f['location']['row']}" for f in findings]
    assert not findings, f"unexpected F821 findings (annotate genuine false positives with # noqa): {locs}"


def test_ruff_lint_runner_importable_and_has_modes():
    """scripts/ruff_lint.py imports and exposes the --diff / --all entry points."""
    scripts_dir = os.path.join(REPO_ROOT, "scripts")
    sys.path.insert(0, scripts_dir)
    try:
        import ruff_lint  # noqa: PLC0415  intentional local import for test
    finally:
        sys.path.remove(scripts_dir)
    assert hasattr(ruff_lint, "run_diff")
    assert hasattr(ruff_lint, "run_all")
    assert hasattr(ruff_lint, "_added_lines")
    # The hunk-line parser is the gate's safety-critical core (only NEW lines gated).
    # Smoke-test it against a synthetic unified diff via the module's regex.
    assert ruff_lint._HUNK_RE.match("@@ -1,0 +5,3 @@ def f():").group(1) == "5"
