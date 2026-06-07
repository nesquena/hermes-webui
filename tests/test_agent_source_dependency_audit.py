from __future__ import annotations

import json
import importlib.util
import subprocess
import sys
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
AUDIT_SCRIPT = REPO / "scripts" / "audit_agent_source_dependencies.py"


def _audit_module():
    spec = importlib.util.spec_from_file_location("audit_agent_source_dependencies", AUDIT_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _run_audit() -> dict[str, object]:
    proc = subprocess.run(
        [sys.executable, str(AUDIT_SCRIPT), str(REPO)],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(proc.stdout)


def _run_markdown_audit() -> str:
    proc = subprocess.run(
        [sys.executable, str(AUDIT_SCRIPT), str(REPO), "--format", "markdown"],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout


def _class_by_id(report: dict[str, object]) -> dict[str, dict[str, object]]:
    classes = report["dependency_classes"]
    assert isinstance(classes, list)
    return {item["id"]: item for item in classes}


def _anchors(dep_class: dict[str, object]) -> set[tuple[str, str]]:
    findings = dep_class["findings"]
    assert isinstance(findings, list)
    return {
        (str(finding["path"]), str(finding["anchor"]))
        for finding in findings
    }


def _texts(dep_class: dict[str, object]) -> list[str]:
    findings = dep_class["findings"]
    assert isinstance(findings, list)
    return [str(finding["text"]) for finding in findings]


def _findings(dep_class: dict[str, object]) -> list[dict[str, object]]:
    findings = dep_class["findings"]
    assert isinstance(findings, list)
    return findings


def test_audit_reports_expected_dependency_classes():
    report = _run_audit()

    assert report["schema_version"] == 1
    classes = _class_by_id(report)
    assert set(classes) == {
        "docker_agent_source_volume",
        "startup_dependency_install",
        "runtime_agent_execution",
        "runtime_auxiliary_model_metadata",
        "runtime_session_state",
        "runtime_gateway_provider",
        "webui_local_or_client_package",
    }
    for dep_class in classes.values():
        assert dep_class["finding_count"] > 0
        assert dep_class["findings"]
        assert dep_class["replacement_surface"]


def test_audit_reports_compose_source_volume_anchors():
    classes = _class_by_id(_run_audit())
    anchors = _anchors(classes["docker_agent_source_volume"])
    texts = _texts(classes["docker_agent_source_volume"])

    assert ("docker-compose.two-container.yml", "hermes-agent-src") in anchors
    assert ("docker-compose.three-container.yml", "hermes-agent-src") in anchors
    assert any("hermes-agent-src:/opt/hermes" in text for text in texts)
    assert any(
        "hermes-agent-src:/home/hermeswebui/.hermes/hermes-agent:ro" in text
        for text in texts
    )


def test_audit_reports_startup_install_dependencies():
    classes = _class_by_id(_run_audit())
    anchors = _anchors(classes["startup_dependency_install"])
    texts = _texts(classes["startup_dependency_install"])

    assert ("api/startup.py", "HERMES_WEBUI_AGENT_DIR") in anchors
    assert ("bootstrap.py", "HERMES_WEBUI_AGENT_DIR") in anchors
    assert ("start.ps1", "HERMES_WEBUI_AGENT_DIR") in anchors
    assert ("api/startup.py", "auto_install_agent_deps") in anchors
    assert ("server.py", "auto_install_agent_deps") in anchors
    assert any("uv pip install" in text and "[all]" in text for text in texts)


def test_audit_reports_runtime_agent_execution_imports():
    classes = _class_by_id(_run_audit())
    anchors = _anchors(classes["runtime_agent_execution"])

    assert ("api/streaming.py", "run_agent") in anchors
    assert ("api/routes.py", "tools.skills_tool") in anchors
    assert ("api/streaming.py", "tools.approval") in anchors
    assert ("api/routes.py", "cron.jobs") in anchors


def test_audit_reports_runtime_auxiliary_and_model_metadata_imports():
    classes = _class_by_id(_run_audit())
    anchors = _anchors(classes["runtime_auxiliary_model_metadata"])

    assert ("api/streaming.py", "agent.auxiliary_client") in anchors
    assert ("api/streaming.py", "agent.model_metadata") in anchors
    assert ("api/config.py", "hermes_cli.models") in anchors
    assert ("api/providers.py", "agent.account_usage") in anchors


def test_audit_embedded_worker_import_line_anchors_are_source_lines():
    classes = _class_by_id(_run_audit())
    provider_findings = _findings(classes["runtime_auxiliary_model_metadata"])
    account_usage = next(
        finding
        for finding in provider_findings
        if finding["path"] == "api/providers.py" and finding["anchor"] == "agent.account_usage"
    )

    assert account_usage["line"] == 168
    assert account_usage["text"] == "from agent.account_usage import fetch_account_usage"


def test_audit_reports_runtime_state_and_provider_imports():
    classes = _class_by_id(_run_audit())
    state_anchors = _anchors(classes["runtime_session_state"])
    provider_anchors = _anchors(classes["runtime_gateway_provider"])

    assert ("api/streaming.py", "hermes_state") in state_anchors
    assert ("api/state_sync.py", "hermes_state") in state_anchors
    assert ("api/streaming.py", "hermes_cli.runtime_provider") in provider_anchors
    assert ("api/routes.py", "hermes_cli.runtime_provider") in provider_anchors


def test_runtime_import_scan_includes_root_python_entrypoints():
    root = AUDIT_SCRIPT.parents[1]
    audit_module = _audit_module()
    paths = {
        path.relative_to(root).as_posix()
        for path in audit_module._iter_python_files(root)
    }

    assert "api/routes.py" in paths
    assert "server.py" in paths
    assert "bootstrap.py" in paths


def test_audit_keeps_client_package_candidates_visible():
    classes = _class_by_id(_run_audit())
    anchors = _anchors(classes["webui_local_or_client_package"])

    assert ("api/streaming.py", "hermes_constants") in anchors
    assert ("api/routes.py", "agent.skill_utils") in anchors
    assert ("api/routes.py", "hermes_cli.plugins") in anchors
    assert ("api/providers.py", "agent.credential_pool") in anchors


def test_markdown_output_is_utf8_safe_on_windows_stdout():
    markdown = _run_markdown_audit()

    assert "# Agent Source Dependency Audit" in markdown
    assert "runtime_agent_execution" in markdown


def test_contract_index_links_agent_api_contract():
    contracts = (REPO / "docs" / "CONTRACTS.md").read_text(encoding="utf-8")

    assert "docs/architecture/agent-api-contract.md" in contracts
    assert "issue #2491" in contracts
    assert "source mounts can be removed" in contracts
