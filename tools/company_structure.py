#!/usr/bin/env python3
"""Validate Yuto company operational folder structure.

This checks that operational company/team artifacts live under company/ while
knowledge/ remains the source-trail vault and legacy fallback during migration.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
COMPANY = ROOT / "company"
WORKFORCE = COMPANY / "workforce"
DEPARTMENTS = COMPANY / "departments"
PROGRAMS = COMPANY / "programs"


def read_yaml(path: Path) -> Any:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def validate_company_structure(company_dir: Path = COMPANY) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    summary = {
        "departments": 0,
        "department_dirs": 0,
        "role_dirs": 0,
        "employee_files_in_roles": 0,
        "programs": 0,
        "migration_manifests": 0,
    }
    if not company_dir.exists():
        return {"ok": False, "errors": [f"missing company dir: {company_dir}"], "warnings": warnings, "summary": summary}
    for required in ["README.md", "org.yaml", "workforce", "departments", "programs", "migration"]:
        if not (company_dir / required).exists():
            errors.append(f"company missing required path: {required}")

    workforce = company_dir / "workforce"
    departments_yaml = workforce / "departments.yaml"
    if not departments_yaml.exists():
        errors.append("company/workforce missing departments.yaml")
        departments = []
    else:
        departments = read_yaml(departments_yaml).get("departments") or []
    summary["departments"] = len(departments)
    if len(departments) < 10:
        errors.append("company workforce must define at least 10 departments")

    for dept in departments:
        did = dept.get("department_id")
        if not did:
            errors.append("department entry missing department_id")
            continue
        dept_dir = company_dir / "departments" / did
        if not dept_dir.exists():
            errors.append(f"missing department dir: {did}")
            continue
        summary["department_dirs"] += 1
        for required_sub in ["roles", "employees", "references", "team-library", "receipts", "runs", "benchmarks"]:
            if not (dept_dir / required_sub).exists():
                errors.append(f"department {did} missing subdir: {required_sub}")
        if not (dept_dir / "department.yaml").exists():
            errors.append(f"department {did} missing department.yaml")
        for role_id in dept.get("current_roles") or []:
            role_dir = dept_dir / "roles" / role_id
            if not role_dir.exists():
                errors.append(f"department {did} missing role dir: {role_id}")
                continue
            summary["role_dirs"] += 1
            for required_sub in ["employees", "references", "team-library", "receipts", "runs", "benchmarks"]:
                if not (role_dir / required_sub).exists():
                    errors.append(f"role {role_id} missing subdir: {required_sub}")
            if not (role_dir / "role.yaml").exists():
                errors.append(f"role {role_id} missing role.yaml")
            summary["employee_files_in_roles"] += len(list((role_dir / "employees").glob("*.yaml"))) if (role_dir / "employees").exists() else 0

    phd = company_dir / "programs" / "phd-research-program"
    if not phd.exists():
        errors.append("missing PhD program operational folder")
    else:
        summary["programs"] += 1
        for required in ["README.md", "program.yaml", "references", "team-library", "receipts", "milestones", "departments"]:
            if not (phd / required).exists():
                errors.append(f"phd-research-program missing {required}")
        program = read_yaml(phd / "program.yaml") if (phd / "program.yaml").exists() else {}
        refs = program.get("knowledge_refs") or []
        if len(refs) < 5:
            errors.append("phd-research-program must reference at least 5 knowledge notes")
        for ref in refs:
            if not (ROOT / ref).exists():
                errors.append(f"phd knowledge_ref missing: {ref}")

    migration_dir = company_dir / "migration"
    summary["migration_manifests"] = len(list(migration_dir.glob("*.json"))) if migration_dir.exists() else 0
    if summary["migration_manifests"] < 1:
        errors.append("company migration must include at least one manifest")

    # Legacy operational folders may remain during compatibility period but should not be the preferred target.
    for legacy in [ROOT / "knowledge" / "company-workforce", ROOT / "knowledge" / "digital-forensic-lab"]:
        if legacy.exists():
            warnings.append(f"legacy operational folder still present as fallback: {legacy.relative_to(ROOT)}")
    return {"ok": not errors, "errors": errors, "warnings": warnings, "summary": summary}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate Yuto company operational structure")
    parser.add_argument("company_dir", nargs="?", default=str(COMPANY))
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    result = validate_company_structure(Path(args.company_dir))
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
