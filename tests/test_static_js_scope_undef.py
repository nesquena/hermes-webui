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
    if shutil.which("eslint") is None and not (REPO / "node_modules" / ".bin" / "eslint").is_file():
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


def _load_gate():
    import importlib.util
    spec = importlib.util.spec_from_file_location("scope_undef_gate_under_test", GATE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _fake_eslint(bin_dir: Path, report: str = "[]") -> Path:
    bin_dir.mkdir(parents=True, exist_ok=True)
    exe = bin_dir / "eslint"
    exe.write_text("#!/bin/sh\nprintf '%s' '" + report + "'\n", encoding="utf-8")
    exe.chmod(0o755)
    return exe


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell stub")
def test_gate_runs_project_local_eslint_when_not_on_path(tmp_path, monkeypatch, capsys):
    """CI installs eslint with `npm install --no-save`, i.e. into node_modules/.bin and
    not onto PATH. The gate must find it there instead of reporting SKIPPED."""
    gate = _load_gate()
    (tmp_path / "static").mkdir()
    (tmp_path / "static" / "a.js").write_text("function a(){return 1}\n", encoding="utf-8")
    exe = _fake_eslint(tmp_path / "node_modules" / ".bin")
    monkeypatch.setattr(gate, "which", lambda _name: None)
    assert gate._find_eslint(tmp_path) == str(exe)
    assert gate.main([str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "CLEAN" in out and "SKIPPED" not in out


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell stub")
def test_gate_reports_findings_from_project_local_eslint(tmp_path, monkeypatch, capsys):
    gate = _load_gate()
    (tmp_path / "static").mkdir()
    (tmp_path / "static" / "a.js").write_text("missingName();\n", encoding="utf-8")
    report = '[{"messages":[{"ruleId":"no-undef","line":1,"message":"missingName is not defined."}]}]'
    _fake_eslint(tmp_path / "node_modules" / ".bin", report)
    monkeypatch.setattr(gate, "which", lambda _name: None)
    assert gate.main([str(tmp_path)]) == 1
    assert "a.js:1: missingName is not defined." in capsys.readouterr().out


def test_gate_picks_the_platform_shim_npm_writes(tmp_path, monkeypatch):
    """npm writes both `eslint` (a #!/bin/sh script) and `eslint.cmd` into node_modules/.bin.
    Native Windows Python can only run the .cmd shim; POSIX runs the shell one."""
    gate = _load_gate()
    bin_dir = tmp_path / "node_modules" / ".bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "eslint").write_text("#!/bin/sh\n", encoding="utf-8")
    (bin_dir / "eslint.cmd").write_text("@echo off\n", encoding="utf-8")
    monkeypatch.setattr(gate, "which", lambda _name: None)
    monkeypatch.setattr(gate.sys, "platform", "win32")
    assert gate._find_eslint(tmp_path) == str(bin_dir / "eslint.cmd")
    monkeypatch.setattr(gate.sys, "platform", "linux")
    assert gate._find_eslint(tmp_path) == str(bin_dir / "eslint")


def test_gate_prefers_a_working_path_eslint_over_a_project_local_one(tmp_path, monkeypatch):
    """A stale or broken node_modules/.bin/eslint must not displace a working
    eslint on PATH: the gate would exit 2 and the pytest wrapper treats that as
    a skip, hiding undefined references (Codex, #8588 re-review)."""
    gate = _load_gate()
    bin_dir = tmp_path / "node_modules" / ".bin"
    bin_dir.mkdir(parents=True)
    for name in ("eslint", "eslint.cmd"):
        (bin_dir / name).write_text("broken launcher\n", encoding="utf-8")
    monkeypatch.setattr(gate, "which", lambda _name: "/usr/local/bin/eslint")
    assert gate._find_eslint(tmp_path) == "/usr/local/bin/eslint"
    monkeypatch.setattr(gate, "which", lambda _name: None)
    expected = "eslint.cmd" if gate.sys.platform == "win32" else "eslint"
    assert gate._find_eslint(tmp_path) == str(bin_dir / expected)


def test_gate_fails_closed_when_eslint_required_but_missing(tmp_path, monkeypatch, capsys):
    gate = _load_gate()
    (tmp_path / "static").mkdir()
    monkeypatch.setattr(gate, "which", lambda _name: None)
    assert gate.main(["--require-eslint", str(tmp_path)]) == 2
    assert "could not run" in capsys.readouterr().out
    # Without the flag a toolchain-free local env still skips.
    assert gate.main([str(tmp_path)]) == 0


def test_ci_runs_scope_gate_with_require_eslint():
    wf = (REPO / ".github" / "workflows" / "tests.yml").read_text(encoding="utf-8")
    assert "python3 scripts/scope_undef_gate.py --require-eslint" in wf
