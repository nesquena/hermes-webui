#!/usr/bin/env python3
"""Harness Engineering quality gate for hermes-webui.

The gate is a technical routing and fast-check entry point. It classifies a diff,
points agents at the contracts they must read, recommends verification, and can
run bounded local checks via --run-fast. It intentionally does not generate or
validate PR-body evidence; contributor PRs should describe release-note-worthy
changes in the PR body and leave CHANGELOG.md to the release workflow.
"""
from __future__ import annotations

import argparse
import json
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BASE = "origin/master"

PY_SUFFIXES = {".py"}
FRONTEND_SUFFIXES = {".js", ".mjs", ".css", ".html"}
DOC_SUFFIXES = {".md", ".rst"}
UI_MARKERS = ("static/", "templates/", "DESIGN.md", "docs/UIUX-GUIDE.md")
SETUP_MARKERS = (
    "bootstrap.py",
    "Dockerfile",
    "docker-compose",
    "start.sh",
    "ctl.sh",
    "onboarding",
    "troubleshooting",
)
STATE_MARKERS = (
    "session",
    "state",
    "stream",
    "journal",
    "lineage",
    "compress",
    "compaction",
    "compact",
    "replay",
    "sidebar",
    "workspace",
)
SECURITY_MARKERS = (
    "auth",
    "token",
    "secret",
    "credential",
    "password",
    "cookie",
    "csrf",
    "csp",
    "tls",
    "approval",
    "permission",
    "sandbox",
    "hook",
    "preflight",
    "toctou",
)
HARNESS_CONTEXT_MARKERS = (
    "agents.md",
    "contributing.md",
    "contract",
    "contracts",
    "context",
    "prompt",
    "memory",
    "skill",
    "mcp",
    "plugin",
    "harness",
    "preflight",
    "kanban",
    "evidence",
    "cron",
)
HARNESS_COMPANION_DOCS = (
    "docs/harness-engineering.md",
    "docs/harness-engineering-cn.md",
)


@dataclass
class HarnessAnalysis:
    files: list[str]
    categories: set[str] = field(default_factory=set)
    contract_docs: list[str] = field(default_factory=list)
    recommended_checks: list[str] = field(default_factory=list)
    routing_notes: list[str] = field(default_factory=list)
    fast_check_results: list[str] = field(default_factory=list)


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def _normalize_file(path: str) -> str:
    normalized = path.strip().replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized


def _test_command_for(files: list[str]) -> str:
    tests = [f for f in files if f.startswith("tests/") and f.endswith(".py")]
    if tests:
        return "./scripts/test.sh " + " ".join(tests)
    return "./scripts/test.sh tests/"


def analyze_changed_files(files: list[str]) -> HarnessAnalysis:
    normalized = [f for f in (_normalize_file(p) for p in files) if f]
    analysis = HarnessAnalysis(files=normalized)

    contracts = ["docs/CONTRACTS.md"]
    checks: list[str] = []
    notes: list[str] = []

    for rel in normalized:
        path = Path(rel)
        suffix = path.suffix.lower()
        lowered = rel.lower()

        if suffix in PY_SUFFIXES:
            analysis.categories.add("python")
        if suffix in FRONTEND_SUFFIXES:
            analysis.categories.add("frontend")
        if suffix in DOC_SUFFIXES or rel.startswith("docs/"):
            analysis.categories.add("docs")
        if rel == "CHANGELOG.md":
            analysis.categories.add("changelog")
        if rel.startswith("tests/"):
            analysis.categories.add("tests")
        if any(rel.startswith(marker) or marker in rel for marker in UI_MARKERS):
            analysis.categories.add("ui_ux")
        if any(marker in lowered for marker in SETUP_MARKERS):
            analysis.categories.add("setup_onboarding")
        if any(marker in lowered for marker in STATE_MARKERS):
            analysis.categories.add("runtime_state")
        if any(marker in lowered for marker in SECURITY_MARKERS):
            analysis.categories.add("security")
        if any(marker in lowered for marker in HARNESS_CONTEXT_MARKERS):
            analysis.categories.add("harness_context")
        if "compress" in lowered or "compact" in lowered or "memory" in lowered or "context" in lowered:
            analysis.categories.add("harness_context_lifecycle")
        if "approval" in lowered or "permission" in lowered or "sandbox" in lowered or "preflight" in lowered or "hook" in lowered:
            analysis.categories.add("harness_permissions")

    if "python" in analysis.categories:
        checks.append("python3 scripts/ruff_lint.py --diff origin/master")
        checks.append(_test_command_for(normalized))
        contracts.append("TESTING.md")
    if "frontend" in analysis.categories:
        checks.append("npm run lint:runtime")
        checks.append("python tests/browser_smoke.py")
        contracts.append("TESTING.md")
    if "ui_ux" in analysis.categories:
        contracts.append("docs/UIUX-GUIDE.md")
        contracts.append("DESIGN.md")
        notes.append("UI/UX change: verify desktop, narrow, and mobile states with visual evidence or browser notes.")
        if "python tests/browser_smoke.py" not in checks:
            checks.append("python tests/browser_smoke.py")
    if "setup_onboarding" in analysis.categories:
        contracts.append("docs/onboarding-agent-checklist.md")
        contracts.append("docs/onboarding.md")
        contracts.append("docs/troubleshooting.md")
        notes.append("Setup/onboarding change: use isolated HERMES_HOME and HERMES_WEBUI_STATE_DIR for trials.")
    if "runtime_state" in analysis.categories:
        contracts.append("docs/rfcs/README.md")
        notes.append("Runtime-state change: name the mutated state layer and prove replay/recovery/sidebar invariants.")
    if "security" in analysis.categories:
        notes.append("Security-sensitive change: include a negative test or manual abuse case.")
    if "harness_context" in analysis.categories:
        contracts.extend(HARNESS_COMPANION_DOCS)
        notes.append("Harness context change: verify prompt/context routing, skill or memory retention layer, and visible-vs-model-facing message boundaries.")
    if "harness_context_lifecycle" in analysis.categories:
        contracts.append("docs/rfcs/README.md")
        checks.append("python3 scripts/harness_quality_gate.py --files <changed-files> --format json")
        notes.append("Context lifecycle change: prove compaction/memory/replay behavior with state-layer evidence, not only source inspection.")
    if "harness_permissions" in analysis.categories:
        notes.append("Harness permission change: fail closed on unknown approval/preflight/sandbox state and include a negative denial or bypass attempt.")
    if "docs" in analysis.categories or "changelog" in analysis.categories:
        notes.append("Docs/release-note routing: do not edit CHANGELOG.md in ordinary contributor PRs; put release-note wording in the PR body when needed.")

    if not checks:
        checks.append("No runtime checks inferred; run docs/review checks appropriate to the touched files.")

    analysis.contract_docs = _dedupe(contracts)
    analysis.recommended_checks = _dedupe(checks)
    analysis.routing_notes = _dedupe(notes)
    return analysis


def _changed_files_from_git(base: str) -> list[str]:
    merge_base = base
    mb = subprocess.run(
        ["git", "merge-base", base, "HEAD"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if mb.returncode == 0 and mb.stdout.strip():
        merge_base = mb.stdout.strip()
    proc = subprocess.run(
        ["git", "diff", "--name-only", "--diff-filter=ACMR", merge_base, "HEAD"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    staged = subprocess.run(
        ["git", "diff", "--name-only", "--diff-filter=ACMR", "--cached"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    unstaged = subprocess.run(
        ["git", "diff", "--name-only", "--diff-filter=ACMR"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    untracked = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    files: list[str] = []
    for result in (proc, staged, unstaged, untracked):
        if result.returncode == 0:
            files.extend(result.stdout.splitlines())
    return _dedupe([_normalize_file(f) for f in files if f.strip()])


def _fast_check_commands(files: list[str]) -> list[list[str]]:
    commands: list[list[str]] = [["git", "diff", "--check"]]
    python_files = [path for path in files if path.endswith(".py")]
    if python_files:
        commands.append(["python3", "-m", "py_compile", *python_files])
    return commands


def run_fast_checks(analysis: HarnessAnalysis) -> int:
    """Run bounded local checks and store one-line results on the analysis."""
    results: list[str] = []
    worst_rc = 0
    for command in _fast_check_commands(analysis.files):
        proc = subprocess.run(
            command,
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        label = " ".join(command)
        if proc.returncode == 0:
            results.append(f"PASS `{label}`")
        else:
            worst_rc = proc.returncode if worst_rc == 0 else worst_rc
            output = (proc.stderr or proc.stdout).strip().splitlines()
            detail = output[0] if output else f"exit {proc.returncode}"
            results.append(f"FAIL `{label}`: {detail}")
    analysis.fast_check_results = results
    return worst_rc


def render_markdown(analysis: HarnessAnalysis, base: str = DEFAULT_BASE) -> str:
    categories = ", ".join(sorted(analysis.categories)) if analysis.categories else "unclassified"
    mode = "advisory with bounded fast checks" if analysis.fast_check_results else "advisory routing only"
    lines = [
        "## Harness Technical Gate",
        "",
        f"Mode: {mode}",
        f"Base: `{base}`",
        f"Task categories: {categories}",
        "",
        "Contract routing:",
    ]
    lines.extend(f"- `{doc}`" for doc in analysis.contract_docs)
    lines.extend(["", "Changed files:"])
    lines.extend(f"- `{path}`" for path in analysis.files) if analysis.files else lines.append("- No changed files detected.")
    lines.extend(["", "Recommended verification:"])
    lines.extend(f"- `{cmd}`" for cmd in analysis.recommended_checks)
    if analysis.fast_check_results:
        lines.extend(["", "Fast check results:"])
        lines.extend(f"- {result}" for result in analysis.fast_check_results)
    if analysis.routing_notes:
        lines.extend(["", "Routing notes:"])
        lines.extend(f"- {note}" for note in analysis.routing_notes)
    return "\n".join(lines) + "\n"


def render_json(analysis: HarnessAnalysis, base: str = DEFAULT_BASE) -> str:
    payload = asdict(analysis)
    payload["categories"] = sorted(analysis.categories)
    payload["base"] = base
    payload["mode"] = "advisory"
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def render_output(analysis: HarnessAnalysis, *, base: str = DEFAULT_BASE, output_format: str = "markdown") -> str:
    if output_format == "markdown":
        return render_markdown(analysis, base=base)
    if output_format == "json":
        return render_json(analysis, base=base)
    raise ValueError(f"unsupported output format: {output_format}")


def _parse_file_arg(value: str | None) -> list[str] | None:
    if value is None:
        return None
    return [part.strip() for part in value.replace("\n", ",").split(",") if part.strip()]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--base", default=DEFAULT_BASE, help="Git diff base (default: origin/master).")
    parser.add_argument(
        "--files",
        help="Comma- or newline-separated file list. If omitted, changed files are read from git.",
    )
    parser.add_argument(
        "--format",
        choices=("markdown", "json"),
        default="markdown",
        help="Output format: markdown technical gate summary or JSON for automation.",
    )
    parser.add_argument(
        "--run-fast",
        action="store_true",
        help="Run bounded fast checks (git diff --check and py_compile for changed Python files) and include results.",
    )
    args = parser.parse_args(argv)

    files = _parse_file_arg(args.files)
    if files is None:
        files = _changed_files_from_git(args.base)

    analysis = analyze_changed_files(files)
    fast_rc = run_fast_checks(analysis) if args.run_fast else 0
    print(render_output(analysis, base=args.base, output_format=args.format), end="")
    return fast_rc


if __name__ == "__main__":
    raise SystemExit(main())
