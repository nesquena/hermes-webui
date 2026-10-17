"""Tests: the agent-source dependency audit is a RATCHET, not just a report.

Issue #2491. ``scripts/audit_agent_source_dependencies.py`` already enumerates
every place the WebUI couples to the hermes-agent source tree (264 findings in 7
dependency classes), but the existing coverage only asserts that findings
*exist* — nothing stops a later PR from adding NEW coupling. The maintainer's
closing note on the issue is explicit: the issue stays open until the residual
sites are "enumerated to zero or deliberately split to named owners, with a
regression gate preventing reintroduction".

So the missing deliverable is the gate itself: a committed baseline of today's
residual coupling, plus a check that fails when the audit surfaces a coupling
site that is not in that baseline.

Design invariants pinned here:
  * findings are keyed by ``path::anchor`` and NEVER by line number, so ordinary
    edits (inserting a line above an import) do not fail the gate;
  * REMOVING coupling never fails the gate — shrinking the baseline is the
    whole point, so progress is reported but tolerated;
  * only NEW coupling fails.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
GATE = REPO / "scripts" / "agent_source_dependency_gate.py"
BASELINE = REPO / "docs" / "architecture" / "agent-source-dependency-baseline.json"

# Everything the audit reads. tests/ and the giant docs blobs are excluded so the
# per-test copy stays cheap.
_COPY_IGNORE = shutil.ignore_patterns(
    ".git", "tests", "__pycache__", "*.pyc",
    "CHANGELOG.md", "ARCHITECTURE.md", "README.md", "TESTING.md", "CONTRIBUTORS.md",
)


def _run_gate(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(GATE), str(root), *args],
        cwd=root,
        capture_output=True,
        text=True,
    )


def _repo_copy(tmp_path: Path) -> Path:
    root = tmp_path / "webui"
    shutil.copytree(REPO, root, ignore=_COPY_IGNORE)
    return root


def test_gate_script_and_baseline_are_committed():
    assert GATE.is_file(), "#2491 needs a regression gate script"
    assert BASELINE.is_file(), "#2491 needs a committed coupling baseline"


def test_baseline_records_every_dependency_class():
    baseline = json.loads(BASELINE.read_text(encoding="utf-8"))

    assert baseline["schema_version"] == 1
    assert set(baseline["dependency_classes"]) == {
        "docker_agent_source_volume",
        "startup_dependency_install",
        "runtime_agent_execution",
        "runtime_auxiliary_model_metadata",
        "runtime_session_state",
        "runtime_gateway_provider",
        "webui_local_or_client_package",
    }
    for class_id, anchors in baseline["dependency_classes"].items():
        assert anchors, f"{class_id} baseline must not be empty"
        for anchor in anchors:
            assert "::" in anchor, f"malformed baseline anchor: {anchor}"


def test_gate_passes_against_the_current_tree():
    proc = _run_gate(REPO)

    assert proc.returncode == 0, f"gate failed on a clean tree:\n{proc.stdout}\n{proc.stderr}"


def test_gate_fails_when_new_agent_import_is_introduced(tmp_path):
    """Regression for the #2491 class: a NEW coupling site must not land silently."""
    root = _repo_copy(tmp_path)
    probe = root / "api" / "_coupling_probe.py"
    probe.write_text(
        "from tools.registry import registry\n\n\ndef probe():\n    return registry\n",
        encoding="utf-8",
    )

    proc = _run_gate(root)
    report = proc.stdout + proc.stderr

    assert proc.returncode == 1, "new agent-source import must fail the gate"
    assert "api/_coupling_probe.py" in report
    assert "tools.registry" in report


def test_gate_survives_pure_line_number_shifts(tmp_path):
    """Findings are keyed by path::anchor, not line number.

    An unrelated edit that shifts existing imports down the file must not be
    mistaken for new coupling.
    """
    root = _repo_copy(tmp_path)
    target = root / "api" / "routes.py"
    target.write_text(
        "# a brand new comment line that shifts every import below it down\n"
        + target.read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    proc = _run_gate(root)

    assert proc.returncode == 0, f"line shift must not fail the gate:\n{proc.stdout}"


def test_gate_tolerates_coupling_being_removed(tmp_path):
    """Shrinking the residual surface is progress — it must not block a PR."""
    root = _repo_copy(tmp_path)
    target = root / "api" / "agent_runtime.py"
    source = target.read_text(encoding="utf-8")
    marker = "from run_agent import AIAgent  # noqa: PLC0415"
    assert marker in source, "expected the run_agent import as the removal probe"
    target.write_text(source.replace(marker, "AIAgent = object  # coupling removed"), encoding="utf-8")

    proc = _run_gate(root)

    assert proc.returncode == 0, f"removing coupling must not fail the gate:\n{proc.stdout}"
    assert "resolved" in proc.stdout.lower()


def test_gate_is_wired_into_ci():
    workflow = (REPO / ".github" / "workflows" / "tests.yml").read_text(encoding="utf-8")

    assert "agent_source_dependency_gate.py" in workflow