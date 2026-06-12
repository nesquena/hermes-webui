#!/usr/bin/env python3
"""Audit WebUI dependencies on the hermes-agent source tree.

This report is deterministic and repo-relative so migration PRs can compare the
same dependency classes without relying on brittle exact line fixtures.
"""
from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


AGENT_MODULE_ROOTS = (
    "agent",
    "cron",
    "hermes_cli",
    "hermes_constants",
    "hermes_state",
    "run_agent",
    "tools",
)


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    kind: str
    anchor: str
    text: str


@dataclass(frozen=True)
class DependencyClass:
    class_id: str
    title: str
    current_dependency: str
    replacement_surface: str
    findings: tuple[Finding, ...]


def _repo_root_from_script() -> Path:
    return Path(__file__).resolve().parents[1]


def _line_text(path: Path, line_number: int) -> str:
    try:
        return path.read_text(encoding="utf-8").splitlines()[line_number - 1].strip()
    except (IndexError, OSError, UnicodeDecodeError):
        return ""


def _iter_text_matches(
    root: Path,
    paths: Iterable[str],
    patterns: Iterable[tuple[str, str]],
) -> list[Finding]:
    findings: list[Finding] = []
    compiled = [(kind, re.compile(pattern)) for kind, pattern in patterns]
    for rel in sorted(paths):
        path = root / rel
        if not path.is_file():
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except UnicodeDecodeError:
            continue
        for line_number, line in enumerate(lines, start=1):
            for kind, pattern in compiled:
                match = pattern.search(line)
                if match:
                    findings.append(
                        Finding(
                            path=rel.replace("\\", "/"),
                            line=line_number,
                            kind=kind,
                            anchor=match.group(0),
                            text=line.strip(),
                        )
                    )
                    break
    return findings


def _iter_python_files(root: Path) -> list[Path]:
    paths: set[Path] = set()
    api_dir = root / "api"
    if api_dir.is_dir():
        paths.update(api_dir.rglob("*.py"))
    for filename in ("server.py", "bootstrap.py"):
        path = root / filename
        if path.is_file():
            paths.add(path)
    return sorted(paths)


def _module_root(module_name: str) -> str:
    return module_name.split(".", 1)[0]


def _import_kind(module_name: str) -> str:
    if module_name == "run_agent" or module_name.startswith("run_agent."):
        return "runtime_agent_execution_import"
    if module_name == "tools" or module_name.startswith("tools."):
        return "runtime_tools_import"
    if module_name == "cron" or module_name.startswith("cron."):
        return "runtime_cron_import"
    if module_name == "hermes_state" or module_name.startswith("hermes_state."):
        return "state_import"
    if module_name == "hermes_constants" or module_name.startswith("hermes_constants."):
        return "constants_import"
    if module_name.startswith("hermes_cli.runtime_provider"):
        return "runtime_provider_import"
    if module_name.startswith(("agent.auxiliary_client", "agent.model_metadata", "agent.models_dev")):
        return "auxiliary_model_metadata_import"
    if module_name.startswith(("hermes_cli.models", "agent.account_usage")):
        return "provider_model_catalog_import"
    if module_name.startswith(("hermes_cli.auth", "hermes_cli.config", "agent.credential_pool")):
        return "auth_config_credential_import"
    if module_name.startswith(("agent.skill_utils", "hermes_cli.plugins", "hermes_cli.profiles", "hermes_cli.goals")):
        return "profiles_skills_plugins_import"
    if module_name.startswith("agent.anthropic_adapter"):
        return "gateway_adapter_import"
    return "agent_source_import"


def _runtime_import_findings(root: Path) -> list[Finding]:
    findings: list[Finding] = []
    for path in _iter_python_files(root):
        rel = path.relative_to(root).as_posix()
        try:
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=rel)
        except (SyntaxError, UnicodeDecodeError):
            continue
        for node in ast.walk(tree):
            module_name = ""
            if isinstance(node, ast.ImportFrom) and node.module:
                module_name = node.module
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if _module_root(alias.name) in AGENT_MODULE_ROOTS:
                        findings.append(
                            Finding(
                                path=rel,
                                line=node.lineno,
                                kind=_import_kind(alias.name),
                                anchor=alias.name,
                                text=_line_text(path, node.lineno),
                            )
                        )
                continue

            if module_name and _module_root(module_name) in AGENT_MODULE_ROOTS:
                findings.append(
                    Finding(
                        path=rel,
                        line=node.lineno,
                        kind=_import_kind(module_name),
                        anchor=module_name,
                        text=_line_text(path, node.lineno),
                    )
                )
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                findings.extend(_embedded_python_import_findings(rel, node.lineno, node.value.value))
    return sorted(findings, key=lambda item: (item.path, item.line, item.anchor))


def _embedded_python_import_findings(rel: str, base_line: int, source: str) -> list[Finding]:
    if "import " not in source:
        return []
    try:
        tree = ast.parse(source, filename=f"{rel}:embedded")
    except SyntaxError:
        return []
    lines = source.splitlines()
    findings: list[Finding] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if _module_root(alias.name) in AGENT_MODULE_ROOTS:
                    line = base_line + node.lineno - 1
                    findings.append(
                        Finding(
                            path=rel,
                            line=line,
                            kind=_import_kind(alias.name),
                            anchor=alias.name,
                            text=lines[node.lineno - 1].strip() if node.lineno - 1 < len(lines) else "",
                        )
                    )
        elif isinstance(node, ast.ImportFrom) and node.module and _module_root(node.module) in AGENT_MODULE_ROOTS:
            line = base_line + node.lineno - 1
            findings.append(
                Finding(
                    path=rel,
                    line=line,
                    kind=_import_kind(node.module),
                    anchor=node.module,
                    text=lines[node.lineno - 1].strip() if node.lineno - 1 < len(lines) else "",
                )
            )
    return findings


def _findings_by_kind(findings: Iterable[Finding], kinds: set[str]) -> tuple[Finding, ...]:
    selected = [finding for finding in findings if finding.kind in kinds]
    return tuple(sorted(selected, key=lambda item: (item.path, item.line, item.anchor)))


def build_report(root: Path) -> dict[str, object]:
    root = root.resolve()

    docker_findings = _iter_text_matches(
        root,
        (
            "docker-compose.two-container.yml",
            "docker-compose.three-container.yml",
            "docker_init.bash",
            "docs/docker.md",
            "docs/rfcs/agent-source-boundary.md",
        ),
        (
            ("agent_source_volume", r"hermes-agent-src"),
            ("agent_source_path", r"/opt/hermes(?:\b|[-/])?"),
        ),
    )
    startup_findings = _iter_text_matches(
        root,
        (
            "server.py",
            "bootstrap.py",
            "start.ps1",
            "api/startup.py",
            "api/config.py",
            "api/streaming.py",
            "docker_init.bash",
        ),
        (
            ("startup_install_function", r"auto_install_agent_deps"),
            ("agent_dir_env", r"HERMES_WEBUI_AGENT_DIR"),
            ("agent_source_install", r"uv pip install.*\[all\]"),
            ("agent_source_staging", r"_agent_src|_stage_src"),
        ),
    )
    runtime_findings = _runtime_import_findings(root)

    classes = (
        DependencyClass(
            class_id="docker_agent_source_volume",
            title="Docker/compose source-tree sharing",
            current_dependency=(
                "Multi-container compose files expose the agent image source via "
                "the hermes-agent-src volume and /opt/hermes."
            ),
            replacement_surface=(
                "Remove the WebUI source mount after startup install and runtime "
                "imports move to hermes-agent endpoints or a versioned client package."
            ),
            findings=_findings_by_kind(docker_findings, {"agent_source_volume", "agent_source_path"}),
        ),
        DependencyClass(
            class_id="startup_dependency_install",
            title="Startup dependency installation from agent checkout",
            current_dependency=(
                "WebUI startup discovers HERMES_WEBUI_AGENT_DIR or ~/.hermes/hermes-agent "
                "and installs the agent checkout extras."
            ),
            replacement_surface=(
                "Replace source-tree pip installs with a packaged hermes-agent client "
                "or an agent health/version endpoint that declares required WebUI client capabilities."
            ),
            findings=_findings_by_kind(
                startup_findings,
                {"startup_install_function", "agent_dir_env", "agent_source_install", "agent_source_staging"},
            ),
        ),
        DependencyClass(
            class_id="runtime_agent_execution",
            title="Runtime agent execution, tools, and cron imports",
            current_dependency=(
                "Browser chat, approvals, tools, and scheduled work import Hermes Agent "
                "execution modules directly from the source checkout."
            ),
            replacement_surface=(
                "Move run orchestration, tool approval/control, and cron execution behind "
                "Hermes Agent APIs or a versioned client package before removing source mounts."
            ),
            findings=_findings_by_kind(
                runtime_findings,
                {"runtime_agent_execution_import", "runtime_tools_import", "runtime_cron_import"},
            ),
        ),
        DependencyClass(
            class_id="runtime_auxiliary_model_metadata",
            title="Runtime auxiliary and model metadata imports",
            current_dependency=(
                "WebUI imports agent auxiliary client and model metadata helpers directly "
                "for title/compression tasks, context windows, provider catalogs, and account usage."
            ),
            replacement_surface=(
                "Use existing provider/model endpoints where they already exist; add agent "
                "endpoints or a client package for auxiliary task config, text auxiliary "
                "calls, context lengths, token estimates, provider catalog, and account usage."
            ),
            findings=_findings_by_kind(
                runtime_findings,
                {"auxiliary_model_metadata_import", "provider_model_catalog_import"},
            ),
        ),
        DependencyClass(
            class_id="runtime_session_state",
            title="Runtime SessionDB/state access",
            current_dependency=(
                "WebUI imports hermes_state.SessionDB and related state helpers directly "
                "to read or write agent session state."
            ),
            replacement_surface=(
                "Move cross-container state reads/writes behind hermes-agent session/state "
                "endpoints; keep WebUI-only presentation caches local."
            ),
            findings=_findings_by_kind(runtime_findings, {"state_import"}),
        ),
        DependencyClass(
            class_id="runtime_gateway_provider",
            title="Gateway/runtime provider calls",
            current_dependency=(
                "WebUI imports hermes_cli.runtime_provider and agent adapter helpers "
                "to resolve providers and normalize gateway calls."
            ),
            replacement_surface=(
                "Route provider resolution and gateway invocation through hermes-agent "
                "runtime/provider endpoints; keep only display formatting in WebUI."
            ),
            findings=_findings_by_kind(
                runtime_findings,
                {"runtime_provider_import", "gateway_adapter_import"},
            ),
        ),
        DependencyClass(
            class_id="webui_local_or_client_package",
            title="Config, auth, skills, profiles, plugins, and constants imports",
            current_dependency=(
                "WebUI imports hermes_cli and agent helpers for config/auth status, "
                "credential pools, skills, profiles, plugin discovery, goals, and constants."
            ),
            replacement_surface=(
                "Keep WebUI-owned display/config code local, but move shared schemas and "
                "pure helpers into a versioned client package; privileged agent data needs endpoints."
            ),
            findings=_findings_by_kind(
                runtime_findings,
                {
                    "auth_config_credential_import",
                    "profiles_skills_plugins_import",
                    "constants_import",
                    "agent_source_import",
                },
            ),
        ),
    )

    class_dicts: list[dict[str, object]] = []
    for item in classes:
        findings = [
            {
                "path": finding.path,
                "line": finding.line,
                "kind": finding.kind,
                "anchor": finding.anchor,
                "text": finding.text,
            }
            for finding in item.findings
        ]
        class_dicts.append(
            {
                "id": item.class_id,
                "title": item.title,
                "current_dependency": item.current_dependency,
                "replacement_surface": item.replacement_surface,
                "finding_count": len(findings),
                "findings": findings,
            }
        )

    return {
        "schema_version": 1,
        "repo_root": ".",
        "summary": {
            "dependency_class_count": len(class_dicts),
            "finding_count": sum(int(item["finding_count"]) for item in class_dicts),
            "dependency_class_ids": [item["id"] for item in class_dicts],
        },
        "dependency_classes": class_dicts,
    }


def _print_markdown(report: dict[str, object]) -> None:
    print("# Agent Source Dependency Audit")
    print()
    summary = report["summary"]
    assert isinstance(summary, dict)
    print(f"- Schema version: {report['schema_version']}")
    print(f"- Dependency classes: {summary['dependency_class_count']}")
    print(f"- Findings: {summary['finding_count']}")
    print()
    classes = report["dependency_classes"]
    assert isinstance(classes, list)
    for item in classes:
        assert isinstance(item, dict)
        print(f"## {item['id']}: {item['title']}")
        print()
        print(f"- Current dependency: {item['current_dependency']}")
        print(f"- Replacement surface: {item['replacement_surface']}")
        print(f"- Findings: {item['finding_count']}")
        print()
        findings = item["findings"]
        assert isinstance(findings, list)
        for finding in findings:
            print(
                f"- `{finding['path']}:{finding['line']}` "
                f"({finding['kind']}, `{finding['anchor']}`): {finding['text']}"
            )
        print()


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "repo_root",
        nargs="?",
        type=Path,
        default=_repo_root_from_script(),
        help="Path to the hermes-webui checkout; defaults to this script's repo.",
    )
    parser.add_argument(
        "--format",
        choices=("json", "markdown"),
        default="json",
        help="Output format. JSON is stable for tests and automation.",
    )
    args = parser.parse_args(argv)

    report = build_report(args.repo_root)
    if args.format == "markdown":
        _print_markdown(report)
    else:
        print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
