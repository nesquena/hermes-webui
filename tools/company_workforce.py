#!/usr/bin/env python3
"""Validate Yuto company workforce kit files.

This is a Phase 0 documentation/config harness, not a runtime authority.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
PREFERRED_KIT = ROOT / "company" / "workforce"
LEGACY_KIT = ROOT / "knowledge" / "company-workforce"
DEFAULT_KIT = PREFERRED_KIT if PREFERRED_KIT.exists() else LEGACY_KIT

REQUIRED_FILES = [
    "org.yaml",
    "employee-file-template.yaml",
    "skills.yaml",
    "rules-by-category.yaml",
    "departments.yaml",
    "approval-matrix.yaml",
    "onboarding-checklist.yaml",
    "receipt-template.yaml",
]

REQUIRED_EMPLOYEE_FIELDS = {
    "personnel_id",
    "display_name",
    "worker_type",
    "role_id",
    "department_id",
    "supervisor",
    "status",
    "autonomy_level",
    "risk_tier",
    "allowed_tools",
    "forbidden_tools",
    "allowed_data",
    "forbidden_data",
    "approval_gates_passed",
    "receipt_required",
    "receipt_log_ref",
    "last_reviewed",
    "retirement_triggers",
}

GLOBAL_FORBIDDEN_ACTIONS = {
    "external_messaging",
    "publishing",
    "production_deployment",
    "spending",
    "secrets_access",
    "real_employee_personal_data",
    "real_victim_or_case_data",
    "final_legal_claims",
    "final_forensic_claims",
    "final_security_claims",
    "final_compliance_claims",
}

PHASE0_FORBIDDEN_TOOLS = {
    "external_messaging",
    "browser_posting",
    "social_media_api",
    "spending",
    "secrets_reading",
    "production_access",
    "deployment_tools",
    "external_webhooks",
    "network_scanners",
}

PHASE0_FORBIDDEN_DATA = {
    "secrets",
    "production_data",
    "customer_data",
    "real_employee_personal_data",
    "real_victim_or_case_data",
    "identifiable_person_data",
}

REQUIRED_DEPARTMENT_LEAD_GATES = {
    "gate_0_need_to_role",
    "gate_1_manifest",
    "gate_2_safety",
    "gate_3_validator",
    "gate_4_probation",
}


def read_yaml(path: Path) -> Any:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def validate_employee_template(path: Path) -> dict[str, Any]:
    errors: list[str] = []
    if not path.exists():
        return {"ok": False, "errors": [f"missing {path.name}"]}
    data = read_yaml(path)
    required = set(data.get("required_fields") or [])
    missing = sorted(REQUIRED_EMPLOYEE_FIELDS - required)
    if missing:
        errors.append(f"employee template missing required fields: {', '.join(missing)}")
    defaults = data.get("defaults") or {}
    if defaults.get("supervisor") != "yuto-control":
        errors.append("employee template default supervisor must be yuto-control")
    if defaults.get("receipt_required") is not True:
        errors.append("employee template default receipt_required must be true")
    return {"ok": not errors, "errors": errors, "required_field_count": len(required)}


def department_ids(kit_dir: Path) -> set[str]:
    path = kit_dir / "departments.yaml"
    if not path.exists():
        return set()
    return {d.get("department_id") for d in (read_yaml(path).get("departments") or []) if d.get("department_id")}


def validate_personnel_files(kit_dir: Path = DEFAULT_KIT) -> dict[str, Any]:
    errors: list[str] = []
    personnel_dir = kit_dir / "personnel"
    ids = department_ids(kit_dir)
    files = sorted(personnel_dir.glob("*.yaml")) if personnel_dir.exists() else []
    seen: set[str] = set()
    for path in files:
        data = read_yaml(path)
        pid = data.get("personnel_id", path.stem)
        missing = sorted(REQUIRED_EMPLOYEE_FIELDS - set(data.keys()))
        if missing:
            errors.append(f"personnel {pid} missing fields: {', '.join(missing)}")
        if pid in seen:
            errors.append(f"duplicate personnel_id: {pid}")
        seen.add(pid)
        if data.get("department_id") not in ids:
            errors.append(f"personnel {pid} unknown department_id: {data.get('department_id')}")
        if data.get("supervisor") != "yuto-control":
            errors.append(f"personnel {pid} supervisor must be yuto-control")
        if data.get("receipt_required") is not True:
            errors.append(f"personnel {pid} receipt_required must be true")
        for tool in data.get("allowed_tools") or []:
            if tool in PHASE0_FORBIDDEN_TOOLS:
                errors.append(f"personnel {pid} uses forbidden Phase 0 tool: {tool}")
        for item in data.get("allowed_data") or []:
            if item in PHASE0_FORBIDDEN_DATA:
                errors.append(f"personnel {pid} uses forbidden Phase 0 data: {item}")
        if data.get("status") not in {"proposed", "designing", "prototype", "probation", "needs_review", "deprecated", "retired"}:
            errors.append(f"personnel {pid} Phase 0 status must not be active")
    return {"ok": not errors, "errors": errors, "files_checked": len(files)}


def validate_department_leads(kit_dir: Path = DEFAULT_KIT) -> dict[str, Any]:
    errors: list[str] = []
    leads_dir = kit_dir / "department-leads"
    files = sorted(leads_dir.glob("*.yaml")) if leads_dir.exists() else []
    ids = department_ids(kit_dir)
    seen: set[str] = set()
    for path in files:
        data = read_yaml(path)
        rid = data.get("role_id", path.stem)
        if rid in seen:
            errors.append(f"duplicate department lead role_id: {rid}")
        seen.add(rid)
        for field in ["role_id", "role_name", "department_id", "status", "mission", "scope", "non_scope", "allowed_tools", "forbidden_tools", "allowed_data", "forbidden_data", "autonomy_level", "risk_tier", "approval_gate", "receipt_required", "activation_requirements", "first_3_probation_tasks"]:
            if field not in data:
                errors.append(f"department lead {rid} missing {field}")
        if data.get("department_id") not in ids:
            errors.append(f"department lead {rid} unknown department_id: {data.get('department_id')}")
        if data.get("status") != "probation":
            errors.append(f"department lead {rid} must start as probation")
        if data.get("autonomy_level") not in {"L0", "L1"}:
            errors.append(f"department lead {rid} Phase 0 autonomy must be L0 or L1")
        if data.get("receipt_required") is not True:
            errors.append(f"department lead {rid} receipt_required must be true")
        requirements = set(data.get("activation_requirements") or [])
        missing_gates = sorted(REQUIRED_DEPARTMENT_LEAD_GATES - requirements)
        if missing_gates:
            errors.append(f"department lead {rid} missing activation requirements: {', '.join(missing_gates)}")
        for tool in data.get("allowed_tools") or []:
            if tool in PHASE0_FORBIDDEN_TOOLS:
                errors.append(f"department lead {rid} uses forbidden Phase 0 tool: {tool}")
        for item in data.get("allowed_data") or []:
            if item in PHASE0_FORBIDDEN_DATA:
                errors.append(f"department lead {rid} uses forbidden Phase 0 data: {item}")
        if len(data.get("first_3_probation_tasks") or []) < 3:
            errors.append(f"department lead {rid} must define first_3_probation_tasks")
    return {"ok": not errors, "errors": errors, "files_checked": len(files)}


def validate_approval_matrix(path: Path) -> dict[str, Any]:
    errors: list[str] = []
    if not path.exists():
        return {"ok": False, "errors": [f"missing {path.name}"], "rule_count": 0}
    rules = read_yaml(path).get("approval_rules") or []
    high_risk = {"high", "critical"}
    for rule in rules:
        category = rule.get("category", "<unknown>")
        if rule.get("risk_tier") in high_risk:
            approver = str(rule.get("required_approver", ""))
            if "Kei" not in approver:
                errors.append(f"approval rule {category} must require Kei for high/critical risk")
            if rule.get("consequence") != "stop_and_escalate":
                errors.append(f"approval rule {category} must stop_and_escalate")
    if len(rules) < 8:
        errors.append("approval matrix must define at least 8 rules")
    return {"ok": not errors, "errors": errors, "rule_count": len(rules)}


def validate_onboarding_checklist(path: Path) -> dict[str, Any]:
    errors: list[str] = []
    if not path.exists():
        return {"ok": False, "errors": [f"missing {path.name}"], "step_count": 0}
    data = read_yaml(path)
    steps = data.get("steps") or []
    if len(steps) < 7:
        errors.append("onboarding checklist must define at least 7 steps")
    if data.get("receipt_required") is not True:
        errors.append("onboarding checklist must require receipts")
    for required in ["gate_1_manifest_complete", "gate_2_safety_review_passed", "gate_3_validator_passed"]:
        if required not in steps:
            errors.append(f"onboarding checklist missing {required}")
    return {"ok": not errors, "errors": errors, "step_count": len(steps)}


def validate_receipt_template(path: Path) -> dict[str, Any]:
    errors: list[str] = []
    required_core = {"receipt_id", "timestamp", "personnel_id", "role_id", "department_id", "task_id", "actions_taken", "verification_performed", "status", "evidence_refs", "policy_checks_passed", "policy_violations"}
    if not path.exists():
        return {"ok": False, "errors": [f"missing {path.name}"], "field_count": 0}
    fields = set(read_yaml(path).get("required_fields") or [])
    missing = sorted(required_core - fields)
    if missing:
        errors.append(f"receipt template missing fields: {', '.join(missing)}")
    return {"ok": not errors, "errors": errors, "field_count": len(fields)}


def validate_workforce_kit(kit_dir: Path = DEFAULT_KIT) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    summary: dict[str, Any] = {}

    if not kit_dir.exists():
        return {"ok": False, "errors": [f"missing kit dir: {kit_dir}"], "warnings": [], "summary": {}}

    for filename in REQUIRED_FILES:
        if not (kit_dir / filename).exists():
            errors.append(f"missing required file: {filename}")

    if errors:
        return {"ok": False, "errors": errors, "warnings": warnings, "summary": summary}

    org = read_yaml(kit_dir / "org.yaml")
    if org.get("company_phase") != "phase_0_internal_only":
        errors.append("org company_phase must be phase_0_internal_only")
    if org.get("owner") != "yuto-control":
        errors.append("org owner must be yuto-control")
    if org.get("final_authority") != "kei":
        errors.append("org final_authority must be kei")
    forbidden = set(org.get("global_forbidden_actions") or [])
    missing_forbidden = sorted(GLOBAL_FORBIDDEN_ACTIONS - forbidden)
    if missing_forbidden:
        errors.append(f"org missing global forbidden actions: {', '.join(missing_forbidden)}")
    summary["departments_in_org"] = len(org.get("departments") or [])
    summary["approval_gates"] = len(org.get("approval_gates") or [])

    tmpl_result = validate_employee_template(kit_dir / "employee-file-template.yaml")
    if not tmpl_result["ok"]:
        errors.extend(tmpl_result["errors"])
    summary["employee_required_fields"] = tmpl_result.get("required_field_count", 0)

    skills = read_yaml(kit_dir / "skills.yaml")
    skill_categories = skills.get("skill_categories") or {}
    if not isinstance(skill_categories, dict) or len(skill_categories) < 8:
        errors.append("skills.yaml must define at least 8 skill_categories")
    if not {"L0", "L1", "L2", "L3", "L4", "L5"}.issubset(set((skills.get("skill_levels") or {}).keys())):
        errors.append("skills.yaml must define skill levels L0-L5")
    summary["skill_categories"] = len(skill_categories)

    departments = read_yaml(kit_dir / "departments.yaml").get("departments") or []
    if len(departments) < 10:
        errors.append("departments.yaml must define at least 10 departments")
    for dept in departments:
        for field in ["department_id", "mission", "allowed_work", "forbidden_work", "activation_gate"]:
            if field not in dept:
                errors.append(f"department missing {field}: {dept.get('department_id', '<unknown>')}")
    summary["departments"] = len(departments)

    rules = read_yaml(kit_dir / "rules-by-category.yaml").get("rules") or []
    if len(rules) < 8:
        errors.append("rules-by-category.yaml must define at least 8 rules/categories")
    for rule in rules:
        rid = rule.get("rule_id", "<unknown>")
        for field in ["category", "risk_tier", "forbidden_actions", "escalation_triggers", "consequence_if_triggered"]:
            if field not in rule:
                errors.append(f"rule {rid} missing {field}")
        if rule.get("red_line") is True and rule.get("consequence_if_triggered") != "stop_and_escalate":
            errors.append(f"red-line rule {rid} must use stop_and_escalate")
    summary["rule_categories"] = len(rules)

    personnel = validate_personnel_files(kit_dir)
    if not personnel["ok"]:
        errors.extend(personnel["errors"])
    summary["personnel_files"] = personnel["files_checked"]
    if personnel["files_checked"] < 4:
        errors.append("workforce kit must include at least 4 personnel files: HR trio plus Yuto Scout")

    leads = validate_department_leads(kit_dir)
    if not leads["ok"]:
        errors.extend(leads["errors"])
    summary["department_leads"] = leads["files_checked"]
    if leads["files_checked"] < 1:
        errors.append("workforce kit must include at least one probation department lead manifest")

    approval = validate_approval_matrix(kit_dir / "approval-matrix.yaml")
    if not approval["ok"]:
        errors.extend(approval["errors"])
    summary["approval_matrix_rules"] = approval["rule_count"]

    onboarding = validate_onboarding_checklist(kit_dir / "onboarding-checklist.yaml")
    if not onboarding["ok"]:
        errors.extend(onboarding["errors"])
    summary["onboarding_steps"] = onboarding["step_count"]

    receipt = validate_receipt_template(kit_dir / "receipt-template.yaml")
    if not receipt["ok"]:
        errors.extend(receipt["errors"])
    summary["receipt_template_fields"] = receipt["field_count"]

    return {"ok": not errors, "errors": errors, "warnings": warnings, "summary": summary}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate Yuto company workforce kit")
    parser.add_argument("kit_dir", nargs="?", default=str(DEFAULT_KIT))
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    result = validate_workforce_kit(Path(args.kit_dir))
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print("ok=" + str(result["ok"]).lower())
        for error in result["errors"]:
            print("ERROR", error)
        for warning in result["warnings"]:
            print("WARN", warning)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
