"""Execute the workflow's summary command against raw step-result certificates.

The softened needs result is synthetic GitHub input, not a live Actions probe.
The executed summary and on-disk results are real production CI code/data.
"""

import json
import os
import shutil
from pathlib import Path
import subprocess
import sys

import pytest
import yaml

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None, reason="executes Linux CI shell command"
)

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / ".github/workflows/conversation-lifecycle.yml"
ROW_STEPS = {
    "normal": ("gate", "settle_frame", "missing_terminal"),
    "terminal-error": ("gate",),
    "historical-transcript-hydration": ("gate",),
    "reconnect-scene-redraw": ("gate",),
}
IDENTITY = {"run_id": "12345", "run_attempt": "2", "sha": "a" * 40}


@pytest.fixture
def certificates(tmp_path):
    directory = tmp_path / "results"
    directory.mkdir()
    for row, steps in ROW_STEPS.items():
        (directory / f"{row}.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "row": row,
                    **IDENTITY,
                    "steps": {step: "success" for step in steps},
                }
            )
        )
    return directory


def _edit(directory, row, mutate):
    path = directory / f"{row}.json"
    data = json.loads(path.read_text())
    mutate(data)
    path.write_text(json.dumps(data))


def _summary(directory, *, mutation="success"):
    wf = yaml.safe_load(WORKFLOW.read_text())
    command = [s["run"] for s in wf["jobs"]["proof-summary"]["steps"] if "run" in s][-1]
    env = {
        "PATH": str(Path(sys.executable).parent)
        + os.pathsep
        + os.environ.get("PATH", ""),
        "HOME": str(directory.parent),
        "LIFECYCLE_RESULTS_DIR": str(directory),
        "LIFECYCLE_RESULT": "success",  # a soft failed job can report success
        "MUTATION_RESULT": mutation,
        "GITHUB_RUN_ID": IDENTITY["run_id"],
        "GITHUB_RUN_ATTEMPT": IDENTITY["run_attempt"],
        "GITHUB_SHA": IDENTITY["sha"],
        "GITHUB_STEP_SUMMARY": str(directory.parent / "summary.md"),
    }
    return subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", command],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=10,
    )


def test_summary_accepts_complete_current_success(certificates):
    result = _summary(certificates)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("row", list(ROW_STEPS))
@pytest.mark.parametrize("outcome", ["failure", "cancelled", "skipped", ""])
def test_soft_job_success_cannot_hide_raw_step_failure(certificates, row, outcome):
    _edit(certificates, row, lambda d: d["steps"].update(gate=outcome))
    result = _summary(certificates)
    assert result.returncode != 0, (
        "summary accepted raw non-success behind a softened job result"
    )


@pytest.mark.parametrize("step", ["settle_frame", "missing_terminal"])
def test_normal_followup_steps_are_required(certificates, step):
    _edit(certificates, "normal", lambda d: d["steps"].update({step: "failure"}))
    assert _summary(certificates).returncode != 0


@pytest.mark.parametrize("row", list(ROW_STEPS))
def test_summary_rejects_missing_row(certificates, row):
    (certificates / f"{row}.json").unlink()
    assert _summary(certificates).returncode != 0


@pytest.mark.parametrize(
    "field,value", [("run_id", "other"), ("run_attempt", "1"), ("sha", "b" * 40)]
)
def test_summary_rejects_stale_identity(certificates, field, value):
    _edit(certificates, "normal", lambda d: d.update({field: value}))
    assert _summary(certificates).returncode != 0


@pytest.mark.parametrize(
    "bad",
    ["{invalid", "[]", "x" * 9000],
    ids=["invalid-json", "wrong-type", "oversized"],
)
def test_summary_rejects_malformed_certificate(certificates, bad):
    (certificates / "normal.json").write_text(bad)
    assert _summary(certificates).returncode != 0


@pytest.mark.parametrize("mutation", ["failure", "cancelled", "skipped", ""])
def test_canary_job_must_succeed(certificates, mutation):
    assert _summary(certificates, mutation=mutation).returncode != 0


def test_summary_rejects_missing_raw_step(certificates):
    _edit(certificates, "normal", lambda d: d["steps"].pop("missing_terminal"))
    assert _summary(certificates).returncode != 0


def test_workflow_preserves_current_matrix_and_records_raw_outcomes():
    wf = yaml.safe_load(WORKFLOW.read_text())
    job = wf["jobs"]["live-to-final"]
    assert {x["name"] for x in job["strategy"]["matrix"]["include"]} == set(ROW_STEPS)
    assert job["continue-on-error"] == "${{ matrix.name != 'reconnect-scene-redraw' }}"
    steps = {s.get("id"): s for s in job["steps"] if "id" in s}
    for step in ROW_STEPS["normal"]:
        assert step in steps, f"missing raw outcome identity: {step}"
    record = steps["proof_record"]
    assert record["if"] == "always()"
    for name, step in (
        ("GATE_OUTCOME", "gate"),
        ("SETTLE_FRAME_OUTCOME", "settle_frame"),
        ("MISSING_TERMINAL_OUTCOME", "missing_terminal"),
    ):
        assert record["env"][name] == "${{ steps." + step + ".outcome }}"
    upload = next(s for s in job["steps"] if s.get("name") == "Upload raw proof result")
    assert upload["if"] == "always()"
    assert upload["with"]["if-no-files-found"] == "error"
    assert "github.run_attempt" in upload["with"]["name"]
    summary = wf["jobs"]["proof-summary"]
    assert summary["if"] == "always()"
    assert set(summary["needs"]) == {"live-to-final", "mutation-canary"}
    assert not summary.get("continue-on-error", False)


@pytest.mark.parametrize("row", list(ROW_STEPS))
def test_record_command_preserves_raw_outcomes(certificates, row):
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(certificates.parent),
        "GITHUB_RUN_ID": IDENTITY["run_id"],
        "GITHUB_RUN_ATTEMPT": IDENTITY["run_attempt"],
        "GITHUB_SHA": IDENTITY["sha"],
        "LIFECYCLE_RESULTS_DIR": str(certificates),
        "GATE_OUTCOME": "failure",
        "SETTLE_FRAME_OUTCOME": "success",
        "MISSING_TERMINAL_OUTCOME": "success",
    }
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/lifecycle_proof_summary.py"),
            "record",
            "--row",
            row,
        ],
        env=env,
        text=True,
        capture_output=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    recorded = json.loads((certificates / f"{row}.json").read_text())
    assert recorded["steps"]["gate"] == "failure"
    assert set(recorded["steps"]) == set(ROW_STEPS[row])
    assert _summary(certificates).returncode != 0


def test_certificate_boolean_version_is_not_accepted(certificates):
    _edit(certificates, "normal", lambda d: d.update(version=True))
    assert _summary(certificates).returncode != 0
