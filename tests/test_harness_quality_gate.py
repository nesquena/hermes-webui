from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "harness_quality_gate.py"


def load_module():
    spec = importlib.util.spec_from_file_location("harness_quality_gate", SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_classify_changed_files_routes_contracts_and_checks():
    gate = load_module()

    assert gate._normalize_file("./.github/workflows/tests.yml") == ".github/workflows/tests.yml"

    analysis = gate.analyze_changed_files(
        [
            "server.py",
            "static/app.js",
            "static/styles.css",
            "docs/onboarding.md",
            "CHANGELOG.md",
            "tests/test_example.py",
        ]
    )

    assert analysis.categories == {
        "python",
        "frontend",
        "ui_ux",
        "docs",
        "tests",
        "changelog",
        "setup_onboarding",
    }
    assert "docs/CONTRACTS.md" in analysis.contract_docs
    assert "TESTING.md" in analysis.contract_docs
    assert "docs/UIUX-GUIDE.md" in analysis.contract_docs
    assert "docs/onboarding-agent-checklist.md" in analysis.contract_docs
    assert "python3 scripts/ruff_lint.py --diff origin/master" in analysis.recommended_checks
    assert "./scripts/test.sh tests/test_example.py" in analysis.recommended_checks
    assert "npm run lint:runtime" in analysis.recommended_checks
    assert "python tests/browser_smoke.py" in analysis.recommended_checks


def test_render_markdown_is_technical_gate_not_pr_body_template():
    gate = load_module()
    analysis = gate.analyze_changed_files([
        "api/routes/session.py",
        "tests/test_session_tail_payload.py",
    ])

    block = gate.render_markdown(analysis, base="origin/master")

    assert block.startswith("## Harness Technical Gate")
    assert "Contract routing:" in block
    assert "Changed files:" in block
    assert "api/routes/session.py" in block
    assert "Recommended verification:" in block
    assert "`./scripts/test.sh tests/test_session_tail_payload.py`" in block
    assert "Runtime-state change:" in block
    assert "PR body" not in block
    assert "### Release Notes" not in block
    assert "TODO" not in block


def test_main_prints_advisory_gate_for_explicit_files(capsys):
    gate = load_module()

    rc = gate.main([
        "--files",
        "docs/harness-engineering.md,CHANGELOG.md",
        "--base",
        "origin/master",
    ])

    captured = capsys.readouterr()
    assert rc == 0
    assert "advisory routing only" in captured.out
    assert "docs/harness-engineering.md" in captured.out
    assert "CHANGELOG.md" in captured.out
    assert "Docs/release-note routing" in captured.out
    assert "do not edit CHANGELOG.md" in captured.out


def test_json_format_is_machine_readable(capsys):
    gate = load_module()

    rc = gate.main([
        "--files",
        "api/streaming.py,tests/test_harness_quality_gate.py",
        "--base",
        "origin/master",
        "--format",
        "json",
    ])

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert rc == 0
    assert payload["base"] == "origin/master"
    assert payload["mode"] == "advisory"
    assert payload["categories"] == ["harness_context", "python", "runtime_state", "tests"]
    assert payload["files"] == ["api/streaming.py", "tests/test_harness_quality_gate.py"]
    assert "TESTING.md" in payload["contract_docs"]
    assert "routing_notes" in payload
    assert "evidence_notes" not in payload


def test_harness_pillar_routing_for_context_permission_and_compaction(capsys):
    gate = load_module()

    rc = gate.main([
        "--files",
        "api/preflight_permissions.py,api/context_compaction_memory.py,docs/harness-engineering.md",
        "--format",
        "json",
    ])

    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert "harness_context" in payload["categories"]
    assert "harness_context_lifecycle" in payload["categories"]
    assert "harness_permissions" in payload["categories"]
    assert "docs/harness-engineering.md" in payload["contract_docs"]
    assert "docs/rfcs/README.md" in payload["contract_docs"]
    assert "python3 scripts/harness_quality_gate.py --files <changed-files> --format json" in payload["recommended_checks"]
    assert any("visible-vs-model-facing" in note for note in payload["routing_notes"])
    assert any("unknown approval/preflight/sandbox state must fail closed" in note for note in payload["routing_notes"])
    assert any("negative denial or bypass attempt" in note for note in payload["routing_notes"])


def test_permission_contract_routes_unknown_state_to_fail_closed_evidence():
    gate = load_module()

    analysis = gate.analyze_changed_files([
        "api/approval_preflight.py",
        "static/sandbox-permissions.js",
    ])

    assert "harness_permissions" in analysis.categories
    assert "security" in analysis.categories
    assert "python3 scripts/harness_quality_gate.py --files <changed-files> --format json" in analysis.recommended_checks
    assert any("unknown approval/preflight/sandbox state must fail closed" in note for note in analysis.routing_notes)
    assert any("negative denial or bypass attempt" in note for note in analysis.routing_notes)


def test_pr_body_format_is_not_supported(capsys):
    gate = load_module()

    with pytest.raises(SystemExit) as excinfo:
        gate.main(["--files", "api/streaming.py", "--format", "pr-body"])

    captured = capsys.readouterr()
    assert excinfo.value.code == 2
    assert "invalid choice" in captured.err


def test_run_fast_records_successful_bounded_checks(monkeypatch, capsys):
    gate = load_module()
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(gate.subprocess, "run", fake_run)

    rc = gate.main([
        "--files",
        "scripts/harness_quality_gate.py,docs/harness-engineering.md",
        "--format",
        "json",
        "--run-fast",
    ])

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert rc == 0
    assert [call[0] for call in calls] == [
        ["git", "diff", "--check"],
        ["python3", "-m", "py_compile", "scripts/harness_quality_gate.py"],
    ]
    assert all(call[1]["cwd"] == gate.REPO_ROOT for call in calls)
    assert payload["fast_check_results"] == [
        "PASS `git diff --check`",
        "PASS `python3 -m py_compile scripts/harness_quality_gate.py`",
    ]


def test_run_fast_returns_nonzero_but_still_prints_gate(monkeypatch, capsys):
    gate = load_module()

    def fake_run(command, **kwargs):
        if command[:3] == ["python3", "-m", "py_compile"]:
            return SimpleNamespace(returncode=1, stdout="", stderr="compile failed\nsecond line")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(gate.subprocess, "run", fake_run)

    rc = gate.main([
        "--files",
        "scripts/harness_quality_gate.py",
        "--run-fast",
    ])

    captured = capsys.readouterr()
    assert rc == 1
    assert "## Harness Technical Gate" in captured.out
    assert "Fast check results:" in captured.out
    assert "Mode: advisory with bounded fast checks" in captured.out
    assert "FAIL `python3 -m py_compile scripts/harness_quality_gate.py`: compile failed" in captured.out
