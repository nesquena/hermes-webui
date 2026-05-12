#!/usr/bin/env python3
"""Validate Phase 0 internal-only Digital Forensic Lab artifacts."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
PREFERRED_LAB = ROOT / "company" / "departments" / "digital-forensic-lab"
LEGACY_LAB = ROOT / "knowledge" / "digital-forensic-lab"
PREFERRED_WORKFORCE = ROOT / "company" / "workforce"
LEGACY_WORKFORCE = ROOT / "knowledge" / "company-workforce"
DEFAULT_LAB = PREFERRED_LAB if PREFERRED_LAB.exists() else LEGACY_LAB
DEFAULT_WORKFORCE = PREFERRED_WORKFORCE if PREFERRED_WORKFORCE.exists() else LEGACY_WORKFORCE
REQUIRED_ARTIFACTS = [
    "lab-charter.yaml",
    "red-lines.yaml",
    "evidence-register-schema.yaml",
    "chain-of-custody-template.yaml",
    "preservation-playbook.yaml",
    "intake-template.yaml",
    "evidence-register.yaml",
    "forensic-lab-receipts.jsonl",
    "knowledge-source-map.yaml",
    "learning-roadmap.md",
    "model-registry.yaml",
    "team-stack-routing.yaml",
    "autonomy-improvement-policy.yaml",
    "benchmark-suite.yaml",
]
REQUIRED_WORKING_LIBRARY_FILES = [
    "working-library/library-index.yaml",
    "working-library/learning-loop-receipts.jsonl",
    "working-library/scout-report.yaml",
]
FORBIDDEN_CONCLUSION_STATUSES = {"final", "forensic_final", "legal_final", "court_ready"}


def read_yaml(path: Path) -> Any:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path.exists():
        return rows
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def validate_knowledge_bindings(lab_dir: Path = DEFAULT_LAB, root: Path = ROOT) -> dict[str, Any]:
    errors: list[str] = []
    summary = {"knowledge_sources": 0, "learning_modules": 0, "growth_loop_steps": 0}
    map_path = lab_dir / "knowledge-source-map.yaml"
    if not map_path.exists():
        return {"ok": False, "errors": ["missing knowledge-source-map.yaml"], "summary": summary}
    data = read_yaml(map_path)
    sources = data.get("sources") or []
    modules = data.get("learning_modules") or []
    growth_loop = data.get("growth_loop") or []
    summary["knowledge_sources"] = len(sources)
    summary["learning_modules"] = len(modules)
    summary["growth_loop_steps"] = len(growth_loop)
    if len(sources) < 5:
        errors.append("knowledge-source-map must bind at least 5 sources")
    if len(modules) < 5:
        errors.append("knowledge-source-map must define at least 5 learning modules")
    if len(growth_loop) < 5:
        errors.append("knowledge-source-map must define at least 5 growth loop steps")
    for source in sources:
        source_file = source.get("source_file")
        if not source_file or not (root / source_file).exists():
            errors.append(f"missing source_file for {source.get('source_id')}: {source_file}")
    charter_path = lab_dir / "lab-charter.yaml"
    charter = read_yaml(charter_path) if charter_path.exists() else {}
    def rel_or_abs(path: Path) -> str:
        try:
            return str(path.relative_to(root))
        except ValueError:
            return str(path)

    expected_map = rel_or_abs(lab_dir / "knowledge-source-map.yaml")
    expected_library = rel_or_abs(lab_dir / "working-library" / "library-index.yaml")
    if charter.get("knowledge_source_map") != expected_map:
        errors.append("lab charter must reference knowledge-source-map")
    if charter.get("working_library") != expected_library:
        errors.append("lab charter must reference working library index")
    if not charter.get("growth_loop"):
        errors.append("lab charter must define growth_loop")
    return {"ok": not errors, "errors": errors, "summary": summary}


def validate_working_library(lab_dir: Path = DEFAULT_LAB, root: Path = ROOT) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    summary = {
        "working_library_source_extracts": 0,
        "working_library_controls": 0,
        "learning_cycle_receipts": 0,
        "source_extracts_sha_verified": 0,
        "source_extracts_with_anchors": 0,
        "practice_labs": 0,
        "practice_labs_with_sandbox_policy": 0,
    }
    for name in REQUIRED_WORKING_LIBRARY_FILES:
        if not (lab_dir / name).exists():
            errors.append(f"missing required working library artifact: {name}")
    index_path = lab_dir / "working-library" / "library-index.yaml"
    if not index_path.exists():
        return {"ok": False, "errors": errors or ["missing working library index"], "warnings": warnings, "summary": summary}
    index = read_yaml(index_path)
    extracts = index.get("source_extracts") or []
    controls = index.get("controls") or []
    cycles = index.get("learning_cycles") or []
    practice_lab_queue = index.get("practice_lab_queue")
    summary["working_library_source_extracts"] = len(extracts)
    summary["working_library_controls"] = len(controls)
    if len(extracts) < 10:
        errors.append("working library must include at least 10 source extracts")
    if len(controls) < 5:
        errors.append("working library must include at least 5 controls")
    if len(cycles) < 3:
        errors.append("working library must include at least 3 learning cycles")
    forbidden = " ".join(index.get("safety_boundary") or []).lower()
    for required in ["synthetic", "no real", "no final", "no external", "no offensive"]:
        if required not in forbidden:
            errors.append(f"working library safety_boundary missing {required}")
    for rel in extracts:
        path = lab_dir / rel
        if not path.exists():
            errors.append(f"missing source extract: {rel}")
            continue
        data = read_yaml(path)
        sid = data.get("source_id", rel)
        source_file = data.get("source_file")
        if not source_file or not (root / source_file).exists():
            errors.append(f"source extract {sid} missing registered source_file")
        if data.get("source_asset_exists") is not True:
            errors.append(f"source extract {sid} source_asset_exists must be true")
        if data.get("sha256_verified") is not True:
            errors.append(f"source extract {sid} sha256_verified must be true")
        else:
            summary["source_extracts_sha_verified"] += 1
        if data.get("promoted_to_authority") is not False:
            errors.append(f"source extract {sid} must not be promoted to authority in Phase 0")
        if len(data.get("evidence_anchors") or []) < 2:
            errors.append(f"source extract {sid} must include at least 2 evidence anchors")
        else:
            summary["source_extracts_with_anchors"] += 1
        if len(data.get("framework_questions") or []) < 3:
            errors.append(f"source extract {sid} must include at least 3 framework questions")
        if not data.get("working_library_contributions"):
            errors.append(f"source extract {sid} must declare working library contributions")
    for rel in controls:
        path = lab_dir / rel
        if not path.exists():
            errors.append(f"missing working library control: {rel}")
            continue
        data = read_yaml(path)
        if len(data.get("requires") or []) < 3:
            errors.append(f"working library control {rel} must define at least 3 requirements")
        if not data.get("source_extracts"):
            errors.append(f"working library control {rel} must reference source extracts")
    if not practice_lab_queue:
        errors.append("working library must reference a practice_lab_queue")
    else:
        queue_path = lab_dir / practice_lab_queue
        if not queue_path.exists():
            errors.append(f"missing practice lab queue: {practice_lab_queue}")
        else:
            queue = read_yaml(queue_path)
            labs = queue.get("labs") or []
            summary["practice_labs"] = len(labs)
            if len(labs) < 10:
                errors.append("practice lab queue must include at least 10 labs")
            queue_boundary = " ".join(queue.get("safety_boundary") or []).lower()
            for required in ["synthetic", "no real", "no external", "no final"]:
                if required not in queue_boundary:
                    errors.append(f"practice lab queue safety_boundary missing {required}")
            sandbox_policy = queue.get("sandbox_policy") or {}
            if not sandbox_policy.get("allowed") or not sandbox_policy.get("forbidden"):
                errors.append("practice lab queue must define allowed/forbidden sandbox_policy")
            for lab in labs:
                lab_id = lab.get("lab_id", "<unknown>")
                if not lab.get("source_ids"):
                    errors.append(f"practice lab {lab_id} must reference source_ids")
                if not lab.get("objective"):
                    errors.append(f"practice lab {lab_id} must define objective")
                if not lab.get("deliverable"):
                    errors.append(f"practice lab {lab_id} must define deliverable")
                if len(lab.get("success_criteria") or []) < 3:
                    errors.append(f"practice lab {lab_id} must define at least 3 success criteria")
                criteria = " ".join(lab.get("success_criteria") or []).lower()
                if any(term in criteria for term in ["no real", "synthetic", "generated", "fictional", "no host", "no external"]):
                    summary["practice_labs_with_sandbox_policy"] += 1
            if labs and summary["practice_labs_with_sandbox_policy"] < len(labs):
                errors.append("every practice lab must include an explicit synthetic/no-real-data sandbox criterion")
    receipt_path = lab_dir / "working-library" / "learning-loop-receipts.jsonl"
    receipts = read_jsonl(receipt_path)
    summary["learning_cycle_receipts"] = len(receipts)
    if len(receipts) < 3:
        errors.append("working library must include at least 3 learning loop receipts")
    for receipt in receipts:
        rid = receipt.get("receipt_id", "<unknown>")
        if receipt.get("source_grounded") is not True:
            errors.append(f"learning loop receipt {rid} must be source_grounded")
        if receipt.get("synthetic_only") is not True:
            errors.append(f"learning loop receipt {rid} must be synthetic_only")
        if receipt.get("external_actions_taken") not in ([], None):
            errors.append(f"learning loop receipt {rid} external_actions_taken must be []")
        if receipt.get("policy_violations") not in ([], None):
            errors.append(f"learning loop receipt {rid} policy_violations must be []")
        if receipt.get("conclusion_status") in FORBIDDEN_CONCLUSION_STATUSES:
            errors.append(f"learning loop receipt {rid} has forbidden final conclusion status")
    return {"ok": not errors, "errors": errors, "warnings": warnings, "summary": summary}


def validate_model_stack(lab_dir: Path = DEFAULT_LAB) -> dict[str, Any]:
    errors: list[str] = []
    summary = {
        "model_registry_entries": 0,
        "custom_workers": 0,
        "blocked_pending_source": 0,
        "benchmark_packs": 0,
        "lane_routes": 0,
    }
    registry_path = lab_dir / "model-registry.yaml"
    routing_path = lab_dir / "team-stack-routing.yaml"
    autonomy_path = lab_dir / "autonomy-improvement-policy.yaml"
    benchmark_path = lab_dir / "benchmark-suite.yaml"
    for path in [registry_path, routing_path, autonomy_path, benchmark_path]:
        if not path.exists():
            errors.append(f"missing model stack artifact: {path.name}")
    if errors:
        return {"ok": False, "errors": errors, "summary": summary}

    registry = read_yaml(registry_path)
    policy = registry.get("policy") or {}
    if policy.get("no_auto_download") is not True:
        errors.append("model registry policy must set no_auto_download true")
    if policy.get("no_real_case_data") is not True:
        errors.append("model registry policy must set no_real_case_data true")
    if policy.get("no_offensive_use") is not True:
        errors.append("model registry policy must set no_offensive_use true")
    categories = [
        "main",
        "legal_rag",
        "evidence_rag",
        "cyber",
        "privacy",
        "visual_deepfake",
        "japanese",
        "custom_workers",
    ]
    required_ids = {
        "qwen36_27b_main",
        "kanon2_embedder",
        "kanon2_reranker",
        "qwen3_reranker_4b_legal",
        "qwen3_embedding_06b",
        "qwen3_embedding_4b",
        "qwen3_reranker_06b",
        "qwen3_reranker_4b_evidence",
        "cybersecqwen_4b",
        "security_slm_unsloth_15b",
        "securebert_family",
        "loglm_worker",
        "gliner2_pii",
        "openai_privacy_filter",
        "openmed_privacy_filter_multilingual",
        "qwen3_vl_embedding",
        "qwen3_vl_reranker",
        "c2pa_verifier",
        "deepfake_detector_ensemble",
        "audio_spoofing_detector_ensemble",
        "llm_jp_4_8b",
        "llm_jp_3_72b",
        "cloud_audit_log_worker",
        "forensic_artifact_worker",
        "evidence_timeline_builder",
        "overclaim_detector",
        "evidence_support_checker",
        "osint_attribution_confidence_scorer",
    }
    seen: set[str] = set()
    for category in categories:
        entries = registry.get(category) or []
        if not entries:
            errors.append(f"model registry missing category entries: {category}")
        for entry in entries:
            eid = entry.get("id")
            if eid:
                seen.add(eid)
            summary["model_registry_entries"] += 1
            status = str(entry.get("status", ""))
            if "blocked_pending_exact_source" in status:
                summary["blocked_pending_source"] += 1
            if category == "custom_workers":
                summary["custom_workers"] += 1
            if not entry.get("allowed_uses"):
                errors.append(f"model registry entry {eid} missing allowed_uses")
            if not entry.get("forbidden_uses"):
                errors.append(f"model registry entry {eid} missing forbidden_uses")
    missing = sorted(required_ids - seen)
    if missing:
        errors.append("model registry missing required ids: " + ", ".join(missing))

    routing = read_yaml(routing_path)
    route_policy = routing.get("routing_policy") or {}
    if route_policy.get("every_claim_needs_evidence_id") is not True:
        errors.append("team stack routing must require every_claim_needs_evidence_id")
    if route_policy.get("final_human_review_required") is not True:
        errors.append("team stack routing must require final_human_review_required")
    lane_routes = routing.get("lane_routes") or {}
    summary["lane_routes"] = len(lane_routes)
    if len(lane_routes) < 14:
        errors.append("team stack routing must define at least 14 fixed-team lane routes")

    autonomy = read_yaml(autonomy_path)
    if autonomy.get("autonomy_level") != "L1_bounded_internal_synthetic_only":
        errors.append("autonomy policy must remain L1_bounded_internal_synthetic_only")
    forbidden_autonomy = " ".join(autonomy.get("forbidden_autonomous_actions") or []).lower()
    for required in ["download large models", "real victim", "scan external", "final legal"]:
        if required not in forbidden_autonomy:
            errors.append(f"autonomy forbidden actions missing {required}")

    benchmark = read_yaml(benchmark_path)
    packs = benchmark.get("benchmark_packs") or []
    summary["benchmark_packs"] = len(packs)
    if len(packs) < 6:
        errors.append("benchmark suite must include at least 6 benchmark packs")
    boundary = " ".join(benchmark.get("safety_boundary") or []).lower()
    for required in ["synthetic", "no real", "no external", "no offensive", "no final"]:
        if required not in boundary:
            errors.append(f"benchmark suite safety_boundary missing {required}")
    return {"ok": not errors, "errors": errors, "summary": summary}


def validate_lab(lab_dir: Path = DEFAULT_LAB) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    summary: dict[str, Any] = {"lab_artifacts": 0, "synthetic_evidence_items": 0, "custody_events": 0, "receipts": 0}

    if not lab_dir.exists():
        return {"ok": False, "errors": [f"missing lab dir: {lab_dir}"], "warnings": warnings, "summary": summary}

    for name in REQUIRED_ARTIFACTS:
        if not (lab_dir / name).exists():
            errors.append(f"missing required lab artifact: {name}")
    summary["lab_artifacts"] = len([p for p in lab_dir.iterdir() if p.is_file()]) if lab_dir.exists() else 0

    charter = read_yaml(lab_dir / "lab-charter.yaml") if (lab_dir / "lab-charter.yaml").exists() else {}
    if charter.get("phase") != "phase_0_internal_only":
        errors.append("lab charter phase must be phase_0_internal_only")
    if charter.get("synthetic_only") is not True:
        errors.append("lab charter synthetic_only must be true")

    red_lines = set((read_yaml(lab_dir / "red-lines.yaml") if (lab_dir / "red-lines.yaml").exists() else {}).get("red_lines") or [])
    for required in ["real_case_data", "final_forensic_claim", "external_action", "evidence_modification"]:
        if required not in red_lines:
            errors.append(f"red-lines missing {required}")

    register = read_yaml(lab_dir / "evidence-register.yaml") if (lab_dir / "evidence-register.yaml").exists() else {}
    items = register.get("evidence_items") or []
    summary["synthetic_evidence_items"] = len(items)
    for item in items:
        eid = item.get("evidence_id", "<unknown>")
        if item.get("synthetic_only") is not True:
            errors.append(f"evidence {eid} synthetic_only must be true")
        if item.get("real_case_data_present") is not False:
            errors.append(f"evidence {eid} real_case_data_present must be false")
        if item.get("conclusion_status") in FORBIDDEN_CONCLUSION_STATUSES:
            errors.append(f"evidence {eid} has forbidden final conclusion status")
        custody = item.get("custody_events") or []
        if not custody:
            errors.append(f"evidence {eid} must include at least one custody event")
        summary["custody_events"] += len(custody)
        for event in custody:
            if not event.get("receipt_ref"):
                errors.append(f"evidence {eid} custody event missing receipt_ref")

    receipts = read_jsonl(lab_dir / "forensic-lab-receipts.jsonl")
    summary["receipts"] = len(receipts)
    for receipt in receipts:
        rid = receipt.get("task_id", receipt.get("receipt_id", "<unknown>"))
        if receipt.get("external_actions_taken") not in ([], None):
            errors.append(f"receipt {rid} external_actions_taken must be []")
        if receipt.get("conclusion_status") in FORBIDDEN_CONCLUSION_STATUSES:
            errors.append(f"receipt {rid} has forbidden final conclusion status")
        if receipt.get("policy_violations") not in ([], None):
            errors.append(f"receipt {rid} policy_violations must be [] for usable Phase 0 receipt")

    knowledge = validate_knowledge_bindings(lab_dir, ROOT)
    if not knowledge["ok"]:
        errors.extend(knowledge["errors"])
    summary.update(knowledge["summary"])

    working_library = validate_working_library(lab_dir, ROOT)
    if not working_library["ok"]:
        errors.extend(working_library["errors"])
    warnings.extend(working_library["warnings"])
    summary.update(working_library["summary"])

    model_stack = validate_model_stack(lab_dir)
    if not model_stack["ok"]:
        errors.extend(model_stack["errors"])
    summary.update(model_stack["summary"])

    return {"ok": not errors, "errors": errors, "warnings": warnings, "summary": summary}


def validate_workforce_links(workforce_dir: Path = DEFAULT_WORKFORCE) -> dict[str, Any]:
    errors: list[str] = []
    summary = {"digital_forensic_personnel": 0, "digital_forensic_department_leads": 0}
    personnel_dir = workforce_dir / "personnel"
    lead_dir = workforce_dir / "department-leads"
    if personnel_dir.exists():
        for path in personnel_dir.glob("*.yaml"):
            data = read_yaml(path)
            if data.get("department_id") == "digital-forensic-lab":
                summary["digital_forensic_personnel"] += 1
                if data.get("receipt_required") is not True:
                    errors.append(f"digital forensic personnel {data.get('personnel_id')} must require receipts")
                if data.get("status") != "probation":
                    errors.append(f"digital forensic personnel {data.get('personnel_id')} must be probation")
                if data.get("autonomy_level") not in {"L0", "L1"}:
                    errors.append(f"digital forensic personnel {data.get('personnel_id')} must be L0/L1 in Phase 0")
    if lead_dir.exists():
        for path in lead_dir.glob("*.yaml"):
            data = read_yaml(path)
            if data.get("department_id") == "digital-forensic-lab":
                summary["digital_forensic_department_leads"] += 1
                if data.get("role_id") != "digital-forensic-lab-lead":
                    errors.append("digital forensic department lead role_id must be digital-forensic-lab-lead")
                if data.get("receipt_required") is not True:
                    errors.append("digital forensic department lead must require receipts")
    if summary["digital_forensic_personnel"] < 3:
        errors.append("digital forensic lab must include at least 3 personnel")
    if summary["digital_forensic_department_leads"] < 1:
        errors.append("digital forensic lab must include one department lead")
    return {"ok": not errors, "errors": errors, "summary": summary}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate Digital Forensic Lab Phase 0 artifacts")
    parser.add_argument("lab_dir", nargs="?", default=str(DEFAULT_LAB))
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    lab = validate_lab(Path(args.lab_dir))
    links = validate_workforce_links(DEFAULT_WORKFORCE)
    result = {
        "ok": lab["ok"] and links["ok"],
        "lab": lab,
        "workforce_links": links,
        "errors": lab["errors"] + links["errors"],
        "warnings": lab["warnings"],
    }
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print("ok=" + str(result["ok"]).lower())
        for error in result["errors"]:
            print("ERROR", error)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
