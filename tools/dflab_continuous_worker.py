#!/usr/bin/env python3
"""Continuous synthetic-only Digital Forensic Lab practice worker.

This worker is intentionally local, bounded, and evidence-first:
- no real case/device/user data;
- no external network/scanning/offensive work;
- optional local Qwen mentor notes only when an already-running local endpoint works;
- one synthetic practice lab per cycle with receipts and heartbeat.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import urllib.request
import time
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
PREFERRED_LAB = ROOT / "company" / "departments" / "digital-forensic-lab"
LEGACY_LAB = ROOT / "knowledge" / "digital-forensic-lab"
LAB = PREFERRED_LAB if PREFERRED_LAB.exists() else LEGACY_LAB
WL = LAB / "working-library"
QUEUE = WL / "practice-labs" / "practice-lab-queue.yaml"
RUNS = WL / "autonomous-runs"
RECEIPTS = WL / "autonomous-learning-receipts.jsonl"
HEARTBEAT = WL / "continuous-worker-heartbeat.json"
LOG = WL / "continuous-worker.log"
VALIDATOR = ROOT / "tools" / "digital_forensic_lab.py"

FORBIDDEN_TERMS = [
    "victim data",
    "client data",
    "employee data",
    "production log",
    "browser history",
    "credential dump",
    "password dump",
    "exploit code",
    "scan target",
    "malware sample",
]


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def now_local_slug() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def slug(value: str) -> str:
    return "".join(ch.lower() if ch.isalnum() else "-" for ch in value).strip("-")[:80]


def read_yaml(path: Path) -> Any:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def write_yaml(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")


def append_jsonl(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def log(message: str) -> None:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(f"{now_utc()} {message}\n")


def run_command(command: list[str], timeout: int = 120) -> dict[str, Any]:
    proc = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=timeout)
    return {
        "command": " ".join(command),
        "exit_code": proc.returncode,
        "stdout_tail": proc.stdout[-2000:],
        "stderr_tail": proc.stderr[-2000:],
    }


def validator_ok() -> tuple[bool, dict[str, Any]]:
    result = run_command(["python", str(VALIDATOR.relative_to(ROOT)), "--json"], timeout=180)
    try:
        data = json.loads(result["stdout_tail"])
    except json.JSONDecodeError:
        data = {"ok": False, "parse_error": result}
    return bool(data.get("ok")), data


def completed_lab_counts() -> dict[str, int]:
    counts: dict[str, int] = {}
    if not RECEIPTS.exists():
        return counts
    for line in RECEIPTS.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        lab_id = row.get("lab_id")
        if lab_id and row.get("pass") is True:
            counts[lab_id] = counts.get(lab_id, 0) + 1
    return counts


def choose_lab(queue: dict[str, Any]) -> dict[str, Any]:
    labs = queue.get("labs") or []
    if not labs:
        raise RuntimeError("practice lab queue has no labs")
    counts = completed_lab_counts()
    return sorted(labs, key=lambda lab: (counts.get(lab.get("lab_id", ""), 0), lab.get("lab_id", "")))[0]


def local_qwen_note(lab: dict[str, Any], exercise: dict[str, Any]) -> dict[str, Any]:
    """Use already-running Ollama Qwen if available. Never starts/downloads a model."""
    compact_payload = {
        "lab_id": lab.get("lab_id"),
        "objective": lab.get("objective"),
        "deliverable_keys": sorted(exercise.keys()),
        "safety_boundary": exercise.get("safety_boundary"),
        "conclusion_status": exercise.get("conclusion_status"),
    }
    prompt = (
        "/no_think\n"
        "You are a concise reviewer for a synthetic-only digital forensics practice lab. "
        "Return compact JSON only with keys: strengths, risks, improvement. "
        "Max 120 words total. No real-case instructions, no external scanning, no offensive steps, "
        "no final forensic/legal/security claims.\n"
        + json.dumps(compact_payload, ensure_ascii=False)
    )
    try:
        payload = {
            "model": "qwen3.6:27b",
            "prompt": prompt,
            "stream": False,
            "think": False,
            "options": {"num_predict": 180, "temperature": 0, "num_ctx": 2048},
        }
        req = urllib.request.Request(
            "http://127.0.0.1:11434/api/generate",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=90) as response:
            data = json.loads(response.read().decode("utf-8"))
        output = str(data.get("response") or "").strip()
        return {
            "used": bool(output),
            "backend": "ollama_qwen3.6_27b_local_api",
            "returncode": 0,
            "raw": output[:4000],
        }
    except Exception as exc:  # noqa: BLE001 - local mentor is optional
        return {"used": False, "backend": "ollama_qwen3.6_27b_local_api", "error": type(exc).__name__ + ": " + str(exc)[:300]}


def build_deliverable(lab: dict[str, Any], run_id: str) -> dict[str, Any]:
    lab_id = lab["lab_id"]
    base = {
        "schema_version": 1,
        "run_id": run_id,
        "lab_id": lab_id,
        "synthetic_only": True,
        "real_case_data_present": False,
        "external_actions_taken": [],
        "conclusion_status": "preliminary_internal_non_final",
        "source_ids": lab.get("source_ids") or [],
        "objective": lab.get("objective"),
        "safety_boundary": [
            "generated toy artifacts only",
            "no real user/device/case/client/employee/production data",
            "no external network/scanning/offensive actions",
            "no final forensic/legal/security claims",
        ],
    }
    if "hash-and-custody" in lab_id:
        toy = f"synthetic evidence for {run_id}\n"
        base["toy_files"] = [
            {"path": "sandbox/toy-note.txt", "sha256": hashlib.sha256(toy.encode()).hexdigest(), "content_description": "generated toy text"}
        ]
        base["custody_events"] = [
            {"actor": "dflab-continuous-worker", "action": "created generated toy file", "timestamp": now_utc(), "receipt_ref": run_id}
        ]
    elif "decision-tree" in lab_id or "acquisition" in lab_id:
        base["decision_tree"] = [
            {"condition": "volatile toy fact would be lost", "action": "record need for human-approved live acquisition in real case; synthetic drill only", "boundary": "no host acquisition"},
            {"condition": "stable generated artifact", "action": "prefer preservation copy and hash in sandbox", "boundary": "generated files only"},
        ]
    elif "registry" in lab_id:
        base["synthetic_registry_like_entries"] = [
            {"key": "HKCU\\Software\\SyntheticApp", "value": "LastOpened=2026-05-12T00:00:00Z", "classification": "generated_fact"},
            {"key": "HKCU\\Software\\SyntheticApp", "value": "UserIntent=unknown", "classification": "unknown_not_inference"},
        ]
    elif "taxonomy" in lab_id or "iot" in lab_id or "smart" in lab_id or "uav" in lab_id:
        base["artifact_taxonomy"] = [
            {"device_type": "fictional smart sensor", "artifact_class": "timestamp", "preservation_question": "Was the generated timestamp copied without modification?"},
            {"device_type": "fictional wearable", "artifact_class": "mock location", "preservation_question": "Is location explicitly fictional and not tied to a person?"},
        ]
    elif "threat" in lab_id:
        base["preservation_questions"] = [
            "What generated logs would need integrity protection?",
            "What chain-of-custody step prevents overclaiming?",
            "Which actions would become external/offensive and must be blocked?",
        ]
    elif "ethics" in lab_id:
        base["stop_gates"] = [
            {"trigger": "rights-impacting conclusion", "action": "stop and require human legal/forensic expert review"},
            {"trigger": "real person/device data", "action": "block in Phase 0"},
        ]
    else:
        base["checklist"] = [
            "define custody station",
            "define non-modification control",
            "record expert-review gate",
            "record no-real-data boundary",
        ]
    return base


def safety_scan(data: Any) -> list[str]:
    text = json.dumps(data, ensure_ascii=False).lower()
    return [term for term in FORBIDDEN_TERMS if term in text]


def run_one(use_qwen: bool = True) -> dict[str, Any]:
    ok, validator = validator_ok()
    queue = read_yaml(QUEUE)
    lab = choose_lab(queue)
    run_id = f"{now_local_slug()}-{lab['lab_id']}"
    run_dir = RUNS / run_id
    run_dir.mkdir(parents=True, exist_ok=False)

    objective = {
        "schema_version": 1,
        "run_id": run_id,
        "lab_id": lab["lab_id"],
        "objective": lab.get("objective"),
        "source_ids": lab.get("source_ids") or [],
        "safety_boundary": queue.get("safety_boundary") or [],
        "expected_learning": lab.get("success_criteria") or [],
        "baseline_validator_ok": ok,
    }
    exercise = build_deliverable(lab, run_id)
    qwen = local_qwen_note(lab, exercise) if use_qwen else {"used": False, "backend": "disabled"}
    if qwen.get("used") or qwen.get("error"):
        (run_dir / "local-qwen-note.json").write_text(json.dumps(qwen, ensure_ascii=False, indent=2), encoding="utf-8")

    violations = safety_scan(exercise)
    passed = not violations and exercise.get("synthetic_only") is True and exercise.get("real_case_data_present") is False
    evaluation = {
        "schema_version": 1,
        "run_id": run_id,
        "lab_id": lab["lab_id"],
        "pass": passed,
        "rubric_results": [
            {"criterion": "synthetic_only", "status": "pass" if exercise.get("synthetic_only") is True else "fail"},
            {"criterion": "real_case_data_absent", "status": "pass" if exercise.get("real_case_data_present") is False else "fail"},
            {"criterion": "no_external_actions", "status": "pass" if exercise.get("external_actions_taken") == [] else "fail"},
            {"criterion": "forbidden_term_scan", "status": "pass" if not violations else "fail", "violations": violations},
        ],
        "qwen_note_used": qwen.get("used") is True,
        "next_recommended_objective": "continue least-completed practice lab queue",
        "conclusion_status": "preliminary_internal_non_final",
    }
    receipt = {
        "run_id": run_id,
        "timestamp_utc": now_utc(),
        "lab_id": lab["lab_id"],
        "objective": lab.get("objective"),
        "source_grounded": True,
        "source_ids": lab.get("source_ids") or [],
        "synthetic_only": True,
        "real_case_data_present": False,
        "external_actions_taken": [],
        "policy_violations": violations,
        "conclusion_status": "preliminary_internal_non_final",
        "pass": passed,
        "local_llm_used": qwen.get("used") is True,
        "local_llm_backend": qwen.get("backend"),
        "artifacts_created": [],
    }

    deliverable_name = lab.get("deliverable") or "deliverable.yaml"
    write_yaml(run_dir / "objective.yaml", objective)
    write_yaml(run_dir / "exercise.yaml", exercise)
    write_yaml(run_dir / deliverable_name, exercise)
    write_yaml(run_dir / "evaluation.yaml", evaluation)
    receipt["artifacts_created"] = [str((run_dir / name).relative_to(ROOT)) for name in ["objective.yaml", "exercise.yaml", deliverable_name, "evaluation.yaml", "receipt.json"]]
    (run_dir / "receipt.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    append_jsonl(RECEIPTS, receipt)

    post_ok, post_validator = validator_ok()
    pytest_result = run_command(["python", "-m", "pytest", "tests/test_digital_forensic_lab.py", "-q"], timeout=240)
    heartbeat = {
        "status": "alive",
        "updated_at_utc": now_utc(),
        "last_run_id": run_id,
        "last_lab_id": lab["lab_id"],
        "last_pass": passed,
        "validator_ok": post_ok,
        "pytest_exit_code": pytest_result["exit_code"],
        "local_llm_used": qwen.get("used") is True,
        "local_llm_backend": qwen.get("backend"),
    }
    HEARTBEAT.write_text(json.dumps(heartbeat, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    log(f"completed {run_id} pass={passed} validator_ok={post_ok} pytest={pytest_result['exit_code']} qwen={qwen.get('used')}")
    return heartbeat


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--sleep-seconds", type=int, default=1200)
    parser.add_argument("--no-qwen", action="store_true")
    args = parser.parse_args()
    log("worker_start")
    while True:
        try:
            result = run_one(use_qwen=not args.no_qwen)
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        except Exception as exc:  # noqa: BLE001 - worker must record and continue
            err = {"status": "error", "updated_at_utc": now_utc(), "error": type(exc).__name__ + ": " + str(exc)}
            HEARTBEAT.write_text(json.dumps(err, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
            log("ERROR " + err["error"])
            if args.once:
                return 1
        if args.once:
            return 0
        time.sleep(max(60, args.sleep_seconds))


if __name__ == "__main__":
    raise SystemExit(main())
