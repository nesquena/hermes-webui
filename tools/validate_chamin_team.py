#!/usr/bin/env python3
"""Validate Chamin employee-team prototype manifests.

This lint checks structure and safety boundaries for the files under
`knowledge/chamin-team/`. It intentionally does not judge business quality.
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError as exc:  # pragma: no cover - environment guard
    raise SystemExit("requires pyyaml") from exc

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TEAM_ROOT = ROOT / "knowledge" / "chamin-team"

REQUIRED_EMPLOYEE_KEYS = {
    "id",
    "name",
    "role",
    "owner",
    "user_facing",
    "primary_runtime",
    "source_of_truth",
    "authority",
    "human_gate",
}

REQUIRED_LANE_KEYS = {
    "id",
    "name",
    "purpose",
    "owner_employee",
    "lane_type",
    "standing_worker",
    "when_to_use",
    "when_not_to_use",
    "input_schema",
    "output_schema",
    "allowed_tools",
    "forbidden_tools",
    "allowed_context",
    "forbidden_context",
    "safety_rules",
    "handoff_allowed_to",
    "verification_gate",
    "receipt_required",
    "human_gate",
}

REQUIRED_COOKBOOK_KEYS = {
    "id",
    "name",
    "owner_employee",
    "goal",
    "non_goals",
    "triggers",
    "default_lanes",
    "optional_lanes",
    "max_workers",
    "max_writer_lanes",
    "human_gates",
    "output_shape",
}

WRITE_TOOLS = {"write_file", "patch"}
DANGEROUS_TOOLS = {
    "send_message",
    "deploy",
    "destructive_ops",
    "production_data",
    "secrets_reading",
}


@dataclass(frozen=True)
class ValidationResult:
    ok: bool
    errors: list[str]
    warnings: list[str]
    lanes: list[str]
    cookbooks: list[str]
    files_checked: int


def load_yaml(path: Path) -> Any:
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ValueError(f"YAML parse failed: {exc}") from exc


def load_frontmatter(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        raise ValueError("missing YAML frontmatter")
    try:
        _, frontmatter, _ = text.split("---", 2)
    except ValueError as exc:
        raise ValueError("invalid YAML frontmatter delimiters") from exc
    data = yaml.safe_load(frontmatter)
    if not isinstance(data, dict):
        raise ValueError("frontmatter must be a mapping")
    return data


def as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def validate_object_schema(schema: Any, label: str, errors: list[str]) -> None:
    if not isinstance(schema, dict):
        errors.append(f"{label}: schema must be a mapping")
        return
    if schema.get("type") != "object":
        errors.append(f"{label}: schema.type must be object")
    if "required" not in schema or not isinstance(schema.get("required"), list):
        errors.append(f"{label}: schema.required must be a list")
    if "properties" not in schema or not isinstance(schema.get("properties"), dict):
        errors.append(f"{label}: schema.properties must be a mapping")
    if schema.get("additionalProperties") is not False:
        errors.append(f"{label}: schema.additionalProperties must be false")


def validate_employee(path: Path, errors: list[str], warnings: list[str]) -> dict[str, Any]:
    try:
        employee = load_frontmatter(path)
    except (OSError, ValueError) as exc:
        errors.append(f"{path.name}: {exc}")
        return {}

    missing = sorted(REQUIRED_EMPLOYEE_KEYS - set(employee))
    if missing:
        errors.append(f"{path.name}: missing required keys: {', '.join(missing)}")

    if employee.get("id") != "chamin":
        errors.append(f"{path.name}: id must be chamin")
    if employee.get("owner") != "kei":
        errors.append(f"{path.name}: owner must be kei")
    if employee.get("user_facing") is not True:
        errors.append(f"{path.name}: user_facing must be true")

    authority = employee.get("authority")
    if not isinstance(authority, dict):
        errors.append(f"{path.name}: authority must be a mapping")
    else:
        if authority.get("can_publish_or_send") is not False:
            errors.append(f"{path.name}: can_publish_or_send must be false")
        if authority.get("can_delete_or_destruct") is not False:
            errors.append(f"{path.name}: can_delete_or_destruct must be false")

    if "destructive action" not in as_list(employee.get("human_gate")):
        warnings.append(f"{path.name}: human_gate should include destructive action")

    return employee


def validate_lane(path: Path, lane: Any, all_lane_ids: set[str]) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    prefix = path.name

    if not isinstance(lane, dict):
        return [f"{prefix}: lane must be a mapping"], warnings

    missing = sorted(REQUIRED_LANE_KEYS - set(lane))
    if missing:
        errors.append(f"{prefix}: missing required keys: {', '.join(missing)}")

    lane_id = lane.get("id")
    if not isinstance(lane_id, str) or not lane_id:
        errors.append(f"{prefix}: id must be a non-empty string")
    elif path.stem != lane_id:
        errors.append(f"{prefix}: filename stem must match id ({lane_id})")

    if lane.get("owner_employee") != "chamin":
        errors.append(f"{prefix}: owner_employee must be chamin")

    for key in [
        "when_to_use",
        "when_not_to_use",
        "allowed_tools",
        "forbidden_tools",
        "allowed_context",
        "forbidden_context",
        "safety_rules",
        "handoff_allowed_to",
    ]:
        if key in lane and not isinstance(lane[key], list):
            errors.append(f"{prefix}: {key} must be a list")

    validate_object_schema(lane.get("input_schema"), f"{prefix}: input_schema", errors)
    validate_object_schema(lane.get("output_schema"), f"{prefix}: output_schema", errors)

    allowed = set(str(x) for x in as_list(lane.get("allowed_tools")))
    forbidden = set(str(x) for x in as_list(lane.get("forbidden_tools")))
    overlap = sorted(allowed & forbidden)
    if overlap:
        errors.append(f"{prefix}: tools cannot be both allowed and forbidden: {', '.join(overlap)}")

    write_holder = bool(lane.get("write_holder", False))
    if not write_holder and (allowed & WRITE_TOOLS):
        errors.append(f"{prefix}: non-write-holder lane allows write tools: {', '.join(sorted(allowed & WRITE_TOOLS))}")

    if lane.get("lane_type") in {"reviewer", "critic"} and (allowed & DANGEROUS_TOOLS):
        errors.append(f"{prefix}: reviewer/critic lane allows dangerous tools: {', '.join(sorted(allowed & DANGEROUS_TOOLS))}")

    for target in as_list(lane.get("handoff_allowed_to")):
        if target not in all_lane_ids:
            errors.append(f"{prefix}: handoff target does not exist: {target}")

    if lane.get("receipt_required") is not True:
        errors.append(f"{prefix}: receipt_required must be true")

    return errors, warnings


def validate_cookbook(path: Path, cookbook: dict[str, Any], lane_ids: set[str]) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    prefix = path.name

    missing = sorted(REQUIRED_COOKBOOK_KEYS - set(cookbook))
    if missing:
        errors.append(f"{prefix}: missing required keys: {', '.join(missing)}")

    cookbook_id = cookbook.get("id")
    if path.stem != cookbook_id:
        errors.append(f"{prefix}: filename stem must match id ({cookbook_id})")
    if cookbook.get("owner_employee") != "chamin":
        errors.append(f"{prefix}: owner_employee must be chamin")

    for key in ["non_goals", "triggers", "default_lanes", "optional_lanes", "human_gates", "output_shape"]:
        if key in cookbook and not isinstance(cookbook[key], list):
            errors.append(f"{prefix}: {key} must be a list")

    for lane in as_list(cookbook.get("default_lanes")) + as_list(cookbook.get("optional_lanes")):
        if lane not in lane_ids:
            errors.append(f"{prefix}: unknown lane reference: {lane}")

    if int(cookbook.get("max_workers", 999)) > 3:
        warnings.append(f"{prefix}: max_workers above default cap of 3")
    if int(cookbook.get("max_writer_lanes", 999)) > 1:
        errors.append(f"{prefix}: max_writer_lanes must be <= 1")

    return errors, warnings


def validate_steering(path: Path, cookbook_ids: set[str], lane_ids: set[str]) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    try:
        data = load_yaml(path)
    except (OSError, ValueError) as exc:
        return [f"{path.name}: {exc}"], warnings

    examples = data.get("examples") if isinstance(data, dict) else None
    if not isinstance(examples, list) or not examples:
        return [f"{path.name}: examples must be a non-empty list"], warnings

    seen: set[str] = set()
    for idx, ex in enumerate(examples, start=1):
        if not isinstance(ex, dict):
            errors.append(f"{path.name}: example {idx} must be a mapping")
            continue
        ex_id = ex.get("id")
        if not isinstance(ex_id, str) or not ex_id:
            errors.append(f"{path.name}: example {idx} missing id")
        elif ex_id in seen:
            errors.append(f"{path.name}: duplicate example id: {ex_id}")
        else:
            seen.add(ex_id)
        if ex.get("cookbook") not in cookbook_ids:
            errors.append(f"{path.name}: example {ex_id} references unknown cookbook: {ex.get('cookbook')}")
        for lane in as_list(ex.get("required_lanes")) + as_list(ex.get("optional_lanes")):
            if lane not in lane_ids:
                errors.append(f"{path.name}: example {ex_id} references unknown lane: {lane}")

    return errors, warnings


def validate_all(team_root: Path = DEFAULT_TEAM_ROOT) -> ValidationResult:
    errors: list[str] = []
    warnings: list[str] = []
    files_checked = 0

    if not team_root.exists():
        return ValidationResult(False, [f"team root missing: {team_root}"], [], [], [], 0)

    required_files = [
        "README.md",
        "employee.md",
        "receipt-schema.yaml",
        "handoff-schema.yaml",
        "steering-examples.yaml",
        "cookbooks/lawlabo-pr-review.md",
    ]
    for rel in required_files:
        if not (team_root / rel).exists():
            errors.append(f"missing required file: {rel}")

    if (team_root / "employee.md").exists():
        validate_employee(team_root / "employee.md", errors, warnings)
        files_checked += 1

    for schema_name in ["receipt-schema.yaml", "handoff-schema.yaml"]:
        path = team_root / schema_name
        if path.exists():
            try:
                validate_object_schema(load_yaml(path), schema_name, errors)
            except (OSError, ValueError) as exc:
                errors.append(f"{schema_name}: {exc}")
            files_checked += 1

    lanes_dir = team_root / "lanes"
    lane_paths = sorted(lanes_dir.glob("*.yaml")) if lanes_dir.exists() else []
    lane_ids = {path.stem for path in lane_paths}
    if "qa-critic" not in lane_ids:
        errors.append("lanes: qa-critic is required")

    for path in lane_paths:
        try:
            lane = load_yaml(path)
            lane_errors, lane_warnings = validate_lane(path, lane, lane_ids)
            errors.extend(lane_errors)
            warnings.extend(lane_warnings)
        except (OSError, ValueError) as exc:
            errors.append(f"{path.name}: {exc}")
        files_checked += 1

    cookbook_paths = sorted((team_root / "cookbooks").glob("*.md")) if (team_root / "cookbooks").exists() else []
    cookbook_ids: set[str] = set()
    for path in cookbook_paths:
        try:
            cookbook = load_frontmatter(path)
            if isinstance(cookbook.get("id"), str):
                cookbook_ids.add(cookbook["id"])
            cookbook_errors, cookbook_warnings = validate_cookbook(path, cookbook, lane_ids)
            errors.extend(cookbook_errors)
            warnings.extend(cookbook_warnings)
        except (OSError, ValueError) as exc:
            errors.append(f"{path.name}: {exc}")
        files_checked += 1

    steering_path = team_root / "steering-examples.yaml"
    if steering_path.exists():
        steering_errors, steering_warnings = validate_steering(steering_path, cookbook_ids, lane_ids)
        errors.extend(steering_errors)
        warnings.extend(steering_warnings)
        files_checked += 1

    return ValidationResult(
        ok=not errors,
        errors=errors,
        warnings=warnings,
        lanes=sorted(lane_ids),
        cookbooks=sorted(cookbook_ids),
        files_checked=files_checked,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate Chamin team prototype manifests")
    parser.add_argument("--team-root", type=Path, default=DEFAULT_TEAM_ROOT)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    result = validate_all(args.team_root)
    payload = {
        "ok": result.ok,
        "errors": result.errors,
        "warnings": result.warnings,
        "lanes": result.lanes,
        "cookbooks": result.cookbooks,
        "files_checked": result.files_checked,
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print("OK" if result.ok else "FAIL")
        for error in result.errors:
            print(f"ERROR: {error}")
        for warning in result.warnings:
            print(f"WARN: {warning}")
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
