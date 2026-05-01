#!/usr/bin/env python3
"""Lightweight evaluator for Yuto's RLM-style research/control tasks.

Usage:
  python3 tools/rlm_eval.py validate entry.json
  python3 tools/rlm_eval.py append entry.json knowledge/yuto-rlm-task-log.jsonl
  python3 tools/rlm_eval.py summary knowledge/yuto-rlm-task-log.jsonl
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

CHECK_MIN = 4
THRESHOLDS = {
    "source_grounding": 2.4,
    "context_efficiency": 2.2,
    "answer_usefulness": 2.2,
    "verification_closure": 2.4,
}
VALID_MODES = {"THINK", "RESEARCH", "PLAN", "EXECUTE"}
SCORE_FIELDS = tuple(THRESHOLDS.keys())


@dataclass(frozen=True)
class ValidationResult:
    ok: bool
    errors: list[str]
    warnings: list[str]
    rlm_style: bool


def load_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Invalid JSON in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise SystemExit(f"Entry must be a JSON object: {path}")
    return data


def validate_entry(entry: dict[str, Any]) -> ValidationResult:
    errors: list[str] = []
    warnings: list[str] = []

    for field in ("date", "task", "mode", "external_context", "rlm_style_checks", "evaluator", "evidence_link"):
        if field not in entry:
            errors.append(f"missing required field: {field}")

    mode = entry.get("mode")
    if mode is not None and mode not in VALID_MODES:
        errors.append(f"mode must be one of {sorted(VALID_MODES)}, got {mode!r}")

    checks = entry.get("rlm_style_checks", [])
    if not isinstance(checks, list):
        errors.append("rlm_style_checks must be a list of integers 1..6")
        checks = []
    else:
        bad_checks = [c for c in checks if not isinstance(c, int) or c < 1 or c > 6]
        if bad_checks:
            errors.append(f"rlm_style_checks contains invalid values: {bad_checks}")

    unique_checks = sorted(set(checks))
    rlm_style = len(unique_checks) >= CHECK_MIN
    if not rlm_style:
        warnings.append(f"not RLM-style by threshold: {len(unique_checks)}/{CHECK_MIN} checks")

    for field in SCORE_FIELDS:
        value = entry.get(field)
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            errors.append(f"{field} must be a number 0..3")
        elif value < 0 or value > 3:
            errors.append(f"{field} must be in range 0..3, got {value}")

    rework = entry.get("rework_count")
    if rework is None:
        errors.append("missing required field: rework_count")
    elif not isinstance(rework, int) or isinstance(rework, bool) or rework < 0:
        errors.append("rework_count must be a non-negative integer")

    context = entry.get("external_context")
    if isinstance(context, str) and not context.strip():
        warnings.append("external_context is empty")

    evaluator = entry.get("evaluator")
    if not isinstance(evaluator, str) or not evaluator.strip():
        errors.append("evaluator must be a non-empty string")

    evidence_link = entry.get("evidence_link")
    if not isinstance(evidence_link, str) or not evidence_link.strip():
        errors.append("evidence_link must be a non-empty source/file/log/session pointer")
    elif evidence_link.strip().lower() in {"todo", "tbd", "none", "n/a"}:
        errors.append("evidence_link must not be a placeholder")

    return ValidationResult(ok=not errors, errors=errors, warnings=warnings, rlm_style=rlm_style)


def read_log(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    entries: list[dict[str, Any]] = []
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError as exc:
            raise SystemExit(f"Invalid JSONL at {path}:{line_no}: {exc}") from exc
        if not isinstance(item, dict):
            raise SystemExit(f"Invalid JSONL at {path}:{line_no}: entry must be object")
        result = validate_entry(item)
        if not result.ok:
            raise SystemExit(f"Invalid entry at {path}:{line_no}: {result.errors}")
        entries.append(item)
    return entries


def summarize(entries: list[dict[str, Any]]) -> dict[str, Any]:
    if not entries:
        return {
            "count": 0,
            "rlm_style_count": 0,
            "averages": {field: None for field in SCORE_FIELDS},
            "average_rework_count": None,
            "evaluator_counts": {},
            "tasks_remaining_to_effective_review": 10,
            "thresholds_met": False,
            "status": "no_data",
        }

    averages = {
        field: round(sum(float(e[field]) for e in entries) / len(entries), 3)
        for field in SCORE_FIELDS
    }
    avg_rework = round(sum(int(e["rework_count"]) for e in entries) / len(entries), 3)
    evaluator_counts = dict(sorted((
        (name, sum(1 for e in entries if e.get("evaluator") == name))
        for name in {str(e.get("evaluator")) for e in entries}
    )))
    rlm_style_count = sum(1 for e in entries if validate_entry(e).rlm_style)
    threshold_scores_met = all(averages[field] >= target for field, target in THRESHOLDS.items())
    rework_met = avg_rework <= 1.0
    enough_data = len(entries) >= 10
    thresholds_met = enough_data and threshold_scores_met and rework_met

    if not enough_data:
        status = "collect_more_data"
    elif thresholds_met:
        status = "effective"
    else:
        status = "needs_workflow_patch"

    return {
        "count": len(entries),
        "rlm_style_count": rlm_style_count,
        "rlm_style_rate": round(rlm_style_count / len(entries), 3),
        "averages": averages,
        "average_rework_count": avg_rework,
        "evaluator_counts": evaluator_counts,
        "tasks_remaining_to_effective_review": max(0, 10 - len(entries)),
        "thresholds": THRESHOLDS,
        "thresholds_met": thresholds_met,
        "status": status,
    }


def cmd_validate(args: argparse.Namespace) -> int:
    entry = load_json(Path(args.entry))
    result = validate_entry(entry)
    print(json.dumps({
        "ok": result.ok,
        "errors": result.errors,
        "warnings": result.warnings,
        "rlm_style": result.rlm_style,
    }, ensure_ascii=False, indent=2))
    return 0 if result.ok else 1


def cmd_append(args: argparse.Namespace) -> int:
    entry_path = Path(args.entry)
    log_path = Path(args.log)
    entry = load_json(entry_path)
    result = validate_entry(entry)
    if not result.ok:
        print(json.dumps({"ok": False, "errors": result.errors}, ensure_ascii=False, indent=2))
        return 1
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")
    entries = read_log(log_path)
    print(json.dumps({"ok": True, "summary": summarize(entries), "warnings": result.warnings}, ensure_ascii=False, indent=2))
    return 0


def cmd_summary(args: argparse.Namespace) -> int:
    entries = read_log(Path(args.log))
    print(json.dumps(summarize(entries), ensure_ascii=False, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    validate = sub.add_parser("validate", help="validate one JSON task entry")
    validate.add_argument("entry")
    validate.set_defaults(func=cmd_validate)

    append = sub.add_parser("append", help="validate and append one JSON entry to a JSONL log")
    append.add_argument("entry")
    append.add_argument("log")
    append.set_defaults(func=cmd_append)

    summary = sub.add_parser("summary", help="summarize a JSONL task log")
    summary.add_argument("log")
    summary.set_defaults(func=cmd_summary)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
