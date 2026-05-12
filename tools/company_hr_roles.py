#!/usr/bin/env python3
"""Validate Company HR / People Ops role manifests and pilot receipts."""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError as exc:  # pragma: no cover
    raise SystemExit("requires pyyaml") from exc

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ROLES_DIR = ROOT / "knowledge" / "company-hr-roles"
DEFAULT_RECEIPT_LOG = ROOT / "knowledge" / "company-hr-receipts.jsonl"

REQUIRED_ROLE_KEYS = {
    "role_id",
    "role_name",
    "owner",
    "status",
    "mission",
    "internal_customer",
    "source_canon",
    "scope",
    "non_scope",
    "inputs",
    "outputs",
    "allowed_tools",
    "forbidden_tools",
    "allowed_data",
    "forbidden_data",
    "autonomy_level",
    "risk_tier",
    "handoff_allowed_to",
    "escalation_triggers",
    "acceptance_criteria",
    "eval_cases",
    "first_3_probation_tasks",
    "receipt_required",
    "metrics",
    "activation_gate",
    "retirement_triggers",
    "last_reviewed",
}

LIST_KEYS = {
    "source_canon",
    "scope",
    "non_scope",
    "inputs",
    "outputs",
    "allowed_tools",
    "forbidden_tools",
    "allowed_data",
    "forbidden_data",
    "handoff_allowed_to",
    "escalation_triggers",
    "acceptance_criteria",
    "eval_cases",
    "first_3_probation_tasks",
    "metrics",
    "activation_gate",
    "retirement_triggers",
}

STATUS_VALUES = {"proposed", "designing", "prototype", "probation", "active", "needs_review", "deprecated", "retired"}
AUTONOMY_VALUES = {"L0", "L1", "L2", "L3", "L4", "L5"}
RISK_VALUES = {"T0", "T1", "T2", "T3", "T4", "T5"}
DANGEROUS_TOOLS = {"external_messaging", "production_access", "spending", "secrets_reading", "deploy", "send_message"}
REQUIRED_FORBIDDEN = {"external_messaging", "production_access", "spending", "secrets_reading"}

REQUIRED_RECEIPT_KEYS = {
    "task_id",
    "date",
    "role_id",
    "task_type",
    "task_risk_level",
    "autonomy_level",
    "artifact_created",
    "verification_status",
    "acceptance_criteria",
    "evidence_refs",
    "verifier_type",
    "policy_checks_passed",
    "policy_violations",
    "human_intervention_count",
    "lifecycle_recommendation",
    "lifecycle_reason",
}
RECEIPT_STATUS = {"pass", "partial", "fail"}
LIFECYCLE_VALUES = {"keep", "modify", "retire"}


@dataclass(frozen=True)
class ValidationResult:
    ok: bool
    errors: list[str]
    warnings: list[str]
    role_ids: list[str]
    files_checked: int


def as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def load_yaml(path: Path) -> Any:
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ValueError(f"YAML parse failed: {exc}") from exc


def role_files(roles_dir: Path) -> list[Path]:
    return sorted(roles_dir.glob("*.yaml"))


def tier_num(value: str, prefix: str) -> int:
    if not isinstance(value, str) or not value.startswith(prefix):
        return -1
    try:
        return int(value[1:])
    except ValueError:
        return -1


def validate_role(path: Path, role: Any, role_ids: set[str] | None = None) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    prefix = path.name
    if not isinstance(role, dict):
        return [f"{prefix}: role manifest must be a mapping"], warnings

    missing = sorted(REQUIRED_ROLE_KEYS - set(role))
    if missing:
        errors.append(f"{prefix}: missing required keys: {', '.join(missing)}")

    role_id = role.get("role_id")
    if not isinstance(role_id, str) or not role_id:
        errors.append(f"{prefix}: role_id must be a non-empty string")
    elif path.stem != role_id:
        errors.append(f"{prefix}: filename stem must match role_id ({role_id})")

    for key in LIST_KEYS:
        if key in role and not isinstance(role[key], list):
            errors.append(f"{prefix}: {key} must be a list")

    if role.get("owner") != "yuto-control":
        errors.append(f"{prefix}: owner must be yuto-control")
    if role.get("status") not in STATUS_VALUES:
        errors.append(f"{prefix}: status must be one of {sorted(STATUS_VALUES)}")
    if role.get("autonomy_level") not in AUTONOMY_VALUES:
        errors.append(f"{prefix}: autonomy_level must be L0-L5")
    if role.get("risk_tier") not in RISK_VALUES:
        errors.append(f"{prefix}: risk_tier must be T0-T5")
    if role.get("receipt_required") is not True:
        errors.append(f"{prefix}: receipt_required must be true")

    allowed = set(str(x) for x in as_list(role.get("allowed_tools")))
    forbidden = set(str(x) for x in as_list(role.get("forbidden_tools")))
    overlap = sorted(allowed & forbidden)
    if overlap:
        errors.append(f"{prefix}: tools cannot be both allowed and forbidden: {', '.join(overlap)}")
    if allowed & DANGEROUS_TOOLS:
        errors.append(f"{prefix}: dangerous tools must not be directly allowed: {', '.join(sorted(allowed & DANGEROUS_TOOLS))}")
    missing_forbidden = sorted(REQUIRED_FORBIDDEN - forbidden)
    if missing_forbidden:
        errors.append(f"{prefix}: missing required forbidden tools: {', '.join(missing_forbidden)}")

    autonomy = str(role.get("autonomy_level"))
    risk = str(role.get("risk_tier"))
    if tier_num(autonomy, "L") >= 4 and tier_num(risk, "T") < 3:
        errors.append(f"{prefix}: autonomy_level {autonomy} requires risk_tier T3 or higher")

    if role_ids is not None:
        for target in as_list(role.get("handoff_allowed_to")):
            if target != "yuto-control" and target not in role_ids:
                errors.append(f"{prefix}: handoff target does not exist: {target}")

    if len(as_list(role.get("first_3_probation_tasks"))) < 3:
        errors.append(f"{prefix}: first_3_probation_tasks must contain at least 3 tasks")
    if not as_list(role.get("source_canon")):
        errors.append(f"{prefix}: source_canon must not be empty")

    return errors, warnings


def validate_all(roles_dir: Path = DEFAULT_ROLES_DIR) -> ValidationResult:
    errors: list[str] = []
    warnings: list[str] = []
    if not roles_dir.exists():
        return ValidationResult(False, [f"roles dir not found: {roles_dir}"], [], [], 0)

    files = role_files(roles_dir)
    loaded: dict[Path, Any] = {}
    ids: set[str] = set()
    for path in files:
        try:
            data = load_yaml(path)
        except ValueError as exc:
            errors.append(f"{path.name}: {exc}")
            continue
        loaded[path] = data
        if isinstance(data, dict) and isinstance(data.get("role_id"), str):
            if data["role_id"] in ids:
                errors.append(f"{path.name}: duplicate role_id {data['role_id']}")
            ids.add(data["role_id"])

    for path, data in loaded.items():
        e, w = validate_role(path, data, ids)
        errors.extend(e)
        warnings.extend(w)

    return ValidationResult(not errors, errors, warnings, sorted(ids), len(files))


def validate_receipt(receipt: Any, role_ids: set[str]) -> list[str]:
    errors: list[str] = []
    if not isinstance(receipt, dict):
        return ["receipt must be an object"]
    missing = sorted(REQUIRED_RECEIPT_KEYS - set(receipt))
    if missing:
        errors.append(f"receipt missing required keys: {', '.join(missing)}")
    role_id = receipt.get("role_id")
    if role_id not in role_ids:
        errors.append(f"receipt unknown role_id: {role_id}")
    if receipt.get("verification_status") not in RECEIPT_STATUS:
        errors.append("receipt verification_status must be pass|partial|fail")
    if receipt.get("lifecycle_recommendation") not in LIFECYCLE_VALUES:
        errors.append("receipt lifecycle_recommendation must be keep|modify|retire")
    if receipt.get("autonomy_level") not in AUTONOMY_VALUES:
        errors.append("receipt autonomy_level must be L0-L5")
    if receipt.get("task_risk_level") not in RISK_VALUES:
        errors.append("receipt task_risk_level must be T0-T5")
    for key in ["acceptance_criteria", "evidence_refs", "policy_checks_passed", "policy_violations"]:
        if key in receipt and not isinstance(receipt[key], list):
            errors.append(f"receipt {key} must be a list")
    if not isinstance(receipt.get("artifact_created"), bool):
        errors.append("receipt artifact_created must be boolean")
    if not isinstance(receipt.get("human_intervention_count"), int) or receipt.get("human_intervention_count", -1) < 0:
        errors.append("receipt human_intervention_count must be a non-negative integer")
    return errors


def load_receipts(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    receipts: list[dict[str, Any]] = []
    for idx, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path}:{idx}: invalid JSONL: {exc}") from exc
        if not isinstance(item, dict):
            raise ValueError(f"{path}:{idx}: receipt must be object")
        receipts.append(item)
    return receipts


def append_receipt(path: Path, receipt: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(receipt, ensure_ascii=False, sort_keys=True) + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate Company HR role manifests and receipts")
    parser.add_argument("--roles-dir", type=Path, default=DEFAULT_ROLES_DIR)
    parser.add_argument("--receipt-log", type=Path, default=DEFAULT_RECEIPT_LOG)
    parser.add_argument("--append-receipt", type=Path)
    parser.add_argument("--summary-receipts", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    result = validate_all(args.roles_dir)
    payload: dict[str, Any] = {
        "ok": result.ok,
        "files_checked": result.files_checked,
        "role_ids": result.role_ids,
        "errors": result.errors,
        "warnings": result.warnings,
    }
    if not result.ok:
        print(json.dumps(payload, ensure_ascii=False, indent=2) if args.json else "FAIL\n" + json.dumps(payload, ensure_ascii=False, indent=2))
        return 1

    role_ids = set(result.role_ids)
    if args.append_receipt:
        try:
            receipt = json.loads(args.append_receipt.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            print(f"invalid receipt JSON: {exc}", file=sys.stderr)
            return 1
        errors = validate_receipt(receipt, role_ids)
        if errors:
            print(json.dumps({"ok": False, "errors": errors}, ensure_ascii=False, indent=2))
            return 1
        append_receipt(args.receipt_log, receipt)
        payload["receipt_appended"] = str(args.receipt_log)

    if args.summary_receipts:
        try:
            receipts = load_receipts(args.receipt_log)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        payload["receipt_summary"] = {"count": len(receipts), "role_ids": sorted({r.get("role_id", "") for r in receipts})}

    if args.json or args.append_receipt or args.summary_receipts:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print("OK")
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
