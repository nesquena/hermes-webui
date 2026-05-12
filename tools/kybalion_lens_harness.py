#!/usr/bin/env python3
"""Validate Kybalion Lens team/scout integration docs.

This is a lightweight documentation harness. It enforces that the Kybalion
Lens remains a bounded operating check across Yuto team lanes, not a runtime,
persona system, or metaphysical authority.
"""
from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_KNOWLEDGE = ROOT / "knowledge"

PRACTICE_NOTE = "kybalion-yuto-practice-experiments.md"
TEAM_PLAYBOOK = "yuto-team-lanes-reuse-playbook.md"
SCOUT_NOTE = "yuto-memory-scout.md"

REQUIRED_ROLES = [
    "Yuto Control",
    "Yuto Scout / Memory Scout",
    "Researcher / Source Reader",
    "Evidence Doc Reader",
    "Compliance Checker",
    "Forensic Reviewer",
    "Report Writer / Scribe",
    "QA Critic / Reviewer",
    "Code Implementation Worker",
    "Cron/background jobs",
]

REQUIRED_GUARDRAIL_PHRASES = [
    "No worker may use the lens to override evidence",
    "Workers use only the one relevant micro-check",
    "Absence of a pattern is valid evidence",
    "Do not promote any metaphysical claim as fact",
]

SCOUT_GUARDRAIL_PHRASES = [
    "Scout reports candidates only",
    "Scout does not edit memory/KG/skills",
    "Scout does not promote metaphysical claims",
    "Scout does not force patterns onto noise",
]

MICRO_CHECKS = {
    "Mentalism",
    "Correspondence",
    "Vibration",
    "Polarity",
    "Rhythm",
    "Cause/Effect",
    "Cause and Effect",
    "Generative Duality",
    "Negative check",
    "full negative check",
}


@dataclass(frozen=True)
class HarnessResult:
    ok: bool
    errors: list[str]
    warnings: list[str]
    roles_found: list[str]
    guardrails_found: list[str]
    files_checked: list[str]


def read_required(path: Path, errors: list[str]) -> str:
    if not path.exists():
        errors.append(f"missing required file: {path.name}")
        return ""
    return path.read_text(encoding="utf-8")


def lower_contains(text: str, phrase: str) -> bool:
    return phrase.lower() in text.lower()


def extract_role_lines(text: str) -> dict[str, str]:
    role_lines: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|") or "---" in stripped or "Lane / role" in stripped:
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if len(cells) >= 2:
            role_lines[cells[0]] = stripped
    return role_lines


def role_micro_check_count(role_line: str) -> int:
    return sum(1 for check in MICRO_CHECKS if check.lower() in role_line.lower())


def validate_all(knowledge_root: Path = DEFAULT_KNOWLEDGE) -> HarnessResult:
    errors: list[str] = []
    warnings: list[str] = []
    files_checked: list[str] = []

    practice_path = knowledge_root / PRACTICE_NOTE
    playbook_path = knowledge_root / TEAM_PLAYBOOK
    scout_path = knowledge_root / SCOUT_NOTE

    practice = read_required(practice_path, errors)
    playbook = read_required(playbook_path, errors)
    scout = read_required(scout_path, errors)
    for path, text in [(practice_path, practice), (playbook_path, playbook), (scout_path, scout)]:
        if text:
            files_checked.append(path.name)

    role_lines = extract_role_lines(practice)
    roles_found = sorted(role_lines)
    for role in REQUIRED_ROLES:
        if role not in role_lines:
            errors.append(f"missing required role mapping: {role}")

    for role, line in role_lines.items():
        count = role_micro_check_count(line)
        if role != "Yuto Control" and count > 3:
            warnings.append(f"{role}: many micro-check tokens found ({count}); keep workers lightweight")

    guardrails_found: list[str] = []
    combined = "\n".join([practice, playbook])
    for phrase in REQUIRED_GUARDRAIL_PHRASES:
        if lower_contains(combined, phrase):
            guardrails_found.append(phrase)
        else:
            errors.append(f"missing guardrail: {phrase}")

    for phrase in SCOUT_GUARDRAIL_PHRASES:
        if lower_contains(scout, phrase):
            guardrails_found.append(phrase)
        else:
            errors.append(f"missing scout read-only guardrail: {phrase}")

    if "[[kybalion-yuto-practice-experiments]]" not in playbook:
        errors.append("team playbook does not link kybalion-yuto-practice-experiments")
    if "[[kybalion-yuto-practice-experiments]]" not in scout:
        errors.append("memory scout note does not link kybalion-yuto-practice-experiments")

    if "full seven-step lens" not in playbook and "full lens" not in playbook:
        errors.append("team playbook missing full-lens limit")

    return HarnessResult(
        ok=not errors,
        errors=errors,
        warnings=warnings,
        roles_found=roles_found,
        guardrails_found=guardrails_found,
        files_checked=files_checked,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate Kybalion Lens team/scout integration docs.")
    parser.add_argument("--knowledge-root", type=Path, default=DEFAULT_KNOWLEDGE)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    result = validate_all(args.knowledge_root)
    data = asdict(result)
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        status = "OK" if result.ok else "FAIL"
        print(f"Kybalion Lens harness: {status}")
        if result.errors:
            print("Errors:")
            for error in result.errors:
                print(f"- {error}")
        if result.warnings:
            print("Warnings:")
            for warning in result.warnings:
                print(f"- {warning}")
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
