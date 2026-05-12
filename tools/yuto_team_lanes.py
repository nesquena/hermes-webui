#!/usr/bin/env python3
"""Validate Yuto Team Lane manifests.

This is a lightweight harness-side lint for the reusable lane contracts under
`knowledge/yuto-team-lanes/`. It intentionally validates structure and safety
properties, not business correctness.
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
DEFAULT_LANES_DIR = ROOT / "knowledge" / "yuto-team-lanes"
DEFAULT_SWARM = Path.home() / "hermes-workspace" / "swarm.yaml"
DEFAULT_RECEIPT_LOG = ROOT / "knowledge" / "yuto-team-lane-receipts.jsonl"

REQUIRED_LANE_KEYS = {
    "id",
    "name",
    "purpose",
    "owner",
    "runtime_options",
    "when_to_use",
    "when_not_to_use",
    "allowed_tools",
    "forbidden_tools",
    "input_schema",
    "output_schema",
    "safety_rules",
    "verification_gate",
    "handoff_allowed_to",
    "receipt_required",
    "human_gate",
}

DANGEROUS_TOOLS = {
    "send_message",
    "deploy",
    "external_commitment",
    "purchase",
    "production_data",
    "secrets_reading",
    "destructive_ops_without_approval",
    "original_evidence_mutation",
}

WRITE_TOOLS = {"write_file", "patch"}
RAW_UNTRUSTED_MARKERS = {"read_raw_untrusted_docs"}

REQUIRED_RECEIPT_KEYS = {
    "task_id",
    "date",
    "lane_set",
    "runtime",
    "artifact_created",
    "verification_status",
    "rework_count",
    "saved_time_estimate",
    "quality_gain",
    "safety_gain",
    "reuse_recommendation",
    "notes",
}
GAIN_VALUES = {"none", "low", "medium", "high"}
STATUS_VALUES = {"pass", "partial", "fail"}
REUSE_VALUES = {"keep", "modify", "drop"}


@dataclass(frozen=True)
class ValidationResult:
    ok: bool
    errors: list[str]
    warnings: list[str]
    lane_ids: list[str]
    files_checked: int


def load_yaml(path: Path) -> Any:
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ValueError(f"YAML parse failed: {exc}") from exc


def as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def validate_schema(schema: Any, label: str, errors: list[str]) -> None:
    if not isinstance(schema, dict):
        errors.append(f"{label}: schema must be an object")
        return
    if schema.get("type") != "object":
        errors.append(f"{label}: schema.type must be object")
    if "required" not in schema or not isinstance(schema.get("required"), list):
        errors.append(f"{label}: schema.required must be a list")
    if "properties" not in schema or not isinstance(schema.get("properties"), dict):
        errors.append(f"{label}: schema.properties must be an object")
    # Output schemas cross worker boundaries; keep them strict to reduce prompt-injection/noise.
    if label.endswith("output_schema") and schema.get("additionalProperties") is not False:
        errors.append(f"{label}: output_schema.additionalProperties must be false")


def validate_lane(path: Path, lane: Any, all_lane_ids: set[str] | None = None) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    prefix = path.name

    if not isinstance(lane, dict):
        return [f"{prefix}: lane manifest must be a mapping"], warnings

    missing = sorted(REQUIRED_LANE_KEYS - set(lane))
    if missing:
        errors.append(f"{prefix}: missing required keys: {', '.join(missing)}")

    lane_id = lane.get("id")
    if not isinstance(lane_id, str) or not lane_id:
        errors.append(f"{prefix}: id must be a non-empty string")
    elif path.stem != lane_id:
        errors.append(f"{prefix}: filename stem must match id ({lane_id})")

    for key in ["runtime_options", "when_to_use", "when_not_to_use", "allowed_tools", "forbidden_tools", "safety_rules", "handoff_allowed_to"]:
        if key in lane and not isinstance(lane[key], list):
            errors.append(f"{prefix}: {key} must be a list")

    allowed = set(str(x) for x in as_list(lane.get("allowed_tools")))
    forbidden = set(str(x) for x in as_list(lane.get("forbidden_tools")))
    overlap = sorted(allowed & forbidden)
    if overlap:
        errors.append(f"{prefix}: tools cannot be both allowed and forbidden: {', '.join(overlap)}")

    if lane.get("write_holder") is not True and allowed & WRITE_TOOLS:
        errors.append(f"{prefix}: non-write-holder lane allows write tools: {', '.join(sorted(allowed & WRITE_TOOLS))}")

    if lane.get("write_holder") is True and not (allowed & WRITE_TOOLS):
        errors.append(f"{prefix}: write_holder lane must allow at least one write tool")

    if lane.get("human_gate") is True and lane.get("receipt_required") is not True:
        errors.append(f"{prefix}: human_gate lanes must require receipts")

    if allowed & DANGEROUS_TOOLS:
        errors.append(f"{prefix}: dangerous tools must not be directly allowed: {', '.join(sorted(allowed & DANGEROUS_TOOLS))}")

    if lane.get("write_holder") is True and not (forbidden & RAW_UNTRUSTED_MARKERS):
        warnings.append(f"{prefix}: write_holder should forbid read_raw_untrusted_docs")

    validate_schema(lane.get("input_schema"), f"{prefix}: input_schema", errors)
    validate_schema(lane.get("output_schema"), f"{prefix}: output_schema", errors)

    gate = lane.get("verification_gate")
    if isinstance(gate, dict):
        if not gate.get("required_by"):
            errors.append(f"{prefix}: verification_gate.required_by is required")
        if not isinstance(gate.get("checks"), list) or not gate.get("checks"):
            errors.append(f"{prefix}: verification_gate.checks must be a non-empty list")
    elif "verification_gate" in lane:
        errors.append(f"{prefix}: verification_gate must be an object")

    if all_lane_ids is not None:
        for target in as_list(lane.get("handoff_allowed_to")):
            if target != "yuto-control" and target not in all_lane_ids:
                errors.append(f"{prefix}: handoff target does not exist: {target}")

    return errors, warnings


def lane_files(lanes_dir: Path) -> list[Path]:
    return sorted(p for p in lanes_dir.glob("*.yaml") if p.name != "steering-examples.yaml")


def validate_steering(lanes_dir: Path, lane_ids: set[str]) -> tuple[list[str], list[str]]:
    path = lanes_dir / "steering-examples.yaml"
    errors: list[str] = []
    warnings: list[str] = []
    if not path.exists():
        errors.append("steering-examples.yaml: missing")
        return errors, warnings
    data = load_yaml(path)
    examples = data.get("examples") if isinstance(data, dict) else None
    if not isinstance(examples, list) or not examples:
        errors.append("steering-examples.yaml: examples must be a non-empty list")
        return errors, warnings
    for idx, example in enumerate(examples):
        label = f"steering-examples.yaml: examples[{idx}]"
        if not isinstance(example, dict):
            errors.append(f"{label}: must be an object")
            continue
        for key in ["id", "request", "lanes", "stop_condition", "human_gate"]:
            if key not in example:
                errors.append(f"{label}: missing {key}")
        lanes = example.get("lanes")
        if not isinstance(lanes, list):
            errors.append(f"{label}: lanes must be a list")
            continue
        for lane in lanes:
            if lane not in lane_ids:
                errors.append(f"{label}: unknown lane {lane}")
    return errors, warnings


def validate_swarm(swarm_path: Path, lane_ids: set[str]) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    if not swarm_path.exists():
        warnings.append(f"swarm: not found at {swarm_path}")
        return errors, warnings
    data = load_yaml(swarm_path)
    workers = data.get("workers") if isinstance(data, dict) else None
    if not isinstance(workers, list) or not workers:
        errors.append("swarm: workers must be a non-empty list")
        return errors, warnings
    ids = [w.get("id") for w in workers if isinstance(w, dict)]
    if len(ids) != len(set(ids)):
        errors.append("swarm: worker ids must be unique")
    for worker in workers:
        if not isinstance(worker, dict):
            errors.append("swarm: each worker must be an object")
            continue
        wid = worker.get("id", "<unknown>")
        lane_contract = worker.get("laneContract")
        if wid not in {"yuto-control", "scope-planner"}:
            if not lane_contract:
                errors.append(f"swarm: worker {wid} missing laneContract")
            elif str(lane_contract).endswith(".yaml") and Path(str(lane_contract)).stem not in lane_ids:
                errors.append(f"swarm: worker {wid} laneContract target not in lane ids: {lane_contract}")
        if worker.get("writeHolder") is True:
            forbidden = set(str(x) for x in as_list(worker.get("forbiddenTools")))
            if "send_message" not in forbidden or "deploy" not in forbidden:
                errors.append(f"swarm: writeHolder {wid} must forbid send_message and deploy")
    return errors, warnings


def validate_all(lanes_dir: Path = DEFAULT_LANES_DIR, swarm_path: Path | None = DEFAULT_SWARM) -> ValidationResult:
    errors: list[str] = []
    warnings: list[str] = []
    if not lanes_dir.exists():
        return ValidationResult(False, [f"lanes dir not found: {lanes_dir}"], [], [], 0)

    files = lane_files(lanes_dir)
    loaded: dict[Path, Any] = {}
    lane_ids: set[str] = set()
    for path in files:
        try:
            data = load_yaml(path)
        except ValueError as exc:
            errors.append(f"{path.name}: {exc}")
            continue
        loaded[path] = data
        if isinstance(data, dict) and isinstance(data.get("id"), str):
            if data["id"] in lane_ids:
                errors.append(f"{path.name}: duplicate lane id {data['id']}")
            lane_ids.add(data["id"])

    for path, data in loaded.items():
        e, w = validate_lane(path, data, lane_ids)
        errors.extend(e)
        warnings.extend(w)

    try:
        e, w = validate_steering(lanes_dir, lane_ids)
        errors.extend(e)
        warnings.extend(w)
    except ValueError as exc:
        errors.append(f"steering-examples.yaml: {exc}")

    if swarm_path is not None:
        try:
            e, w = validate_swarm(swarm_path, lane_ids)
            errors.extend(e)
            warnings.extend(w)
        except ValueError as exc:
            errors.append(f"swarm: {exc}")

    return ValidationResult(
        ok=not errors,
        errors=errors,
        warnings=warnings,
        lane_ids=sorted(lane_ids),
        files_checked=len(files) + (1 if (lanes_dir / "steering-examples.yaml").exists() else 0),
    )


def validate_receipt(receipt: Any, lane_ids: set[str]) -> list[str]:
    errors: list[str] = []
    if not isinstance(receipt, dict):
        return ["receipt must be an object"]
    missing = sorted(REQUIRED_RECEIPT_KEYS - set(receipt))
    if missing:
        errors.append(f"receipt missing required keys: {', '.join(missing)}")
    lane_set = receipt.get("lane_set")
    if not isinstance(lane_set, list):
        errors.append("receipt lane_set must be a list")
    else:
        for lane in lane_set:
            if lane != "yuto-control" and lane not in lane_ids:
                errors.append(f"receipt unknown lane: {lane}")
    if receipt.get("verification_status") not in STATUS_VALUES:
        errors.append("receipt verification_status must be pass|partial|fail")
    for key in ["saved_time_estimate", "quality_gain", "safety_gain"]:
        if receipt.get(key) not in GAIN_VALUES:
            errors.append(f"receipt {key} must be one of {sorted(GAIN_VALUES)}")
    if receipt.get("reuse_recommendation") not in REUSE_VALUES:
        errors.append("receipt reuse_recommendation must be keep|modify|drop")
    if not isinstance(receipt.get("artifact_created"), bool):
        errors.append("receipt artifact_created must be boolean")
    if not isinstance(receipt.get("rework_count"), int) or receipt.get("rework_count", -1) < 0:
        errors.append("receipt rework_count must be a non-negative integer")
    if not isinstance(receipt.get("task_id"), str) or not receipt.get("task_id"):
        errors.append("receipt task_id must be a non-empty string")
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


def summarize_receipts(receipts: list[dict[str, Any]]) -> dict[str, Any]:
    status_counts = {k: 0 for k in sorted(STATUS_VALUES)}
    reuse_counts = {k: 0 for k in sorted(REUSE_VALUES)}
    lane_counts: dict[str, int] = {}
    gain_counts = {"saved_time_estimate": {}, "quality_gain": {}, "safety_gain": {}}
    total_rework = 0
    for receipt in receipts:
        status = receipt.get("verification_status")
        if status in status_counts:
            status_counts[status] += 1
        reuse = receipt.get("reuse_recommendation")
        if reuse in reuse_counts:
            reuse_counts[reuse] += 1
        total_rework += int(receipt.get("rework_count", 0))
        for lane in receipt.get("lane_set", []):
            lane_counts[lane] = lane_counts.get(lane, 0) + 1
        for key in gain_counts:
            value = str(receipt.get(key, "none"))
            gain_counts[key][value] = gain_counts[key].get(value, 0) + 1
    return {
        "count": len(receipts),
        "status_counts": status_counts,
        "reuse_counts": reuse_counts,
        "lane_counts": dict(sorted(lane_counts.items())),
        "average_rework": (total_rework / len(receipts)) if receipts else 0,
        "gain_counts": gain_counts,
        "needs_more_data": len(receipts) < 10,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate and measure Yuto Team Lane manifests")
    parser.add_argument("--lanes-dir", type=Path, default=DEFAULT_LANES_DIR)
    parser.add_argument("--swarm", type=Path, default=DEFAULT_SWARM)
    parser.add_argument("--no-swarm", action="store_true")
    parser.add_argument("--receipt-log", type=Path, default=DEFAULT_RECEIPT_LOG)
    parser.add_argument("--append-receipt", type=Path, help="Append one JSON receipt after validation")
    parser.add_argument("--summary-receipts", action="store_true", help="Summarize the receipt JSONL log")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    result = validate_all(args.lanes_dir, None if args.no_swarm else args.swarm)
    payload = {
        "ok": result.ok,
        "files_checked": result.files_checked,
        "lane_ids": result.lane_ids,
        "errors": result.errors,
        "warnings": result.warnings,
    }
    if not result.ok:
        print(json.dumps(payload, ensure_ascii=False, indent=2) if args.json else "FAIL\n" + json.dumps(payload, ensure_ascii=False, indent=2))
        return 1

    lane_ids = set(result.lane_ids)
    if args.append_receipt:
        try:
            receipt = json.loads(args.append_receipt.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            print(f"invalid receipt JSON: {exc}", file=sys.stderr)
            return 1
        errors = validate_receipt(receipt, lane_ids)
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
        # Validate historical receipts too, so summary cannot hide malformed data.
        receipt_errors: list[str] = []
        for idx, receipt in enumerate(receipts, start=1):
            for error in validate_receipt(receipt, lane_ids):
                receipt_errors.append(f"receipt[{idx}]: {error}")
        payload["receipt_summary"] = summarize_receipts(receipts)
        if receipt_errors:
            payload["ok"] = False
            payload["receipt_errors"] = receipt_errors
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 1

    if args.json or args.append_receipt or args.summary_receipts:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print("OK")
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
