from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools import company_hr_roles  # noqa: E402


def valid_role_yaml(role_id: str = "hr-role-designer", *, autonomy="L1", risk="T1", forbidden=None) -> str:
    forbidden = forbidden or ["external_messaging", "production_access", "spending", "secrets_reading"]
    return f"""role_id: {role_id}
role_name: HR Role Designer
owner: yuto-control
status: probation
mission: Convert company needs into precise worker role charters.
internal_customer: yuto-control
source_canon:
  - learning-systems-thinking-diana-montalion
  - agentic-architectural-patterns-for-building-multi-agent-systems-ali-arsanjani-juan-pablo-bustos
scope:
  - draft role charters
  - define activation criteria
non_scope:
  - deploy workers
  - contact external parties
inputs:
  - company need
outputs:
  - role charter draft
allowed_tools:
  - read_file
  - search_files
forbidden_tools:
{chr(10).join('  - ' + item for item in forbidden)}
allowed_data:
  - public sources
  - approved internal notes
forbidden_data:
  - secrets
  - production data
autonomy_level: {autonomy}
risk_tier: {risk}
handoff_allowed_to:
  - chief-of-staff-org-architect
  - culture-safety-steward
escalation_triggers:
  - role requests external action
acceptance_criteria:
  - charter has owner scope tools risk metrics
  - non-scope and escalation are explicit
eval_cases:
  - design a read-only scout role
first_3_probation_tasks:
  - draft one role charter
  - run one collision check
  - revise after safety review
receipt_required: true
metrics:
  - first_pass_acceptance_rate
  - rework_count
activation_gate:
  - supervised probation receipt passes
retirement_triggers:
  - duplicates another HR role
last_reviewed: 2026-05-12
"""


def write_minimal_role_set(root: Path) -> Path:
    roles = root / "roles"
    roles.mkdir()
    for role_id in ["chief-of-staff-org-architect", "hr-role-designer", "culture-safety-steward"]:
        text = valid_role_yaml(role_id)
        if role_id == "chief-of-staff-org-architect":
            text = text.replace("role_name: HR Role Designer", "role_name: Chief of Staff / Org Architect")
        if role_id == "culture-safety-steward":
            text = text.replace("role_name: HR Role Designer", "role_name: Culture & Safety Steward")
        (roles / f"{role_id}.yaml").write_text(text, encoding="utf-8")
    return roles


def test_validate_current_hr_role_manifests():
    result = company_hr_roles.validate_all(company_hr_roles.DEFAULT_ROLES_DIR)

    assert result.ok, result.errors
    assert "chief-of-staff-org-architect" in result.role_ids
    assert "hr-role-designer" in result.role_ids
    assert "culture-safety-steward" in result.role_ids
    assert result.files_checked >= 3


def test_validate_minimal_valid_role_set(tmp_path):
    roles = write_minimal_role_set(tmp_path)

    result = company_hr_roles.validate_all(roles)

    assert result.ok, result.errors
    assert sorted(result.role_ids) == ["chief-of-staff-org-architect", "culture-safety-steward", "hr-role-designer"]


def test_rejects_external_messaging_allowed_without_gate(tmp_path):
    roles = write_minimal_role_set(tmp_path)
    bad = valid_role_yaml("hr-role-designer").replace("  - read_file\n  - search_files", "  - read_file\n  - external_messaging")
    (roles / "hr-role-designer.yaml").write_text(bad, encoding="utf-8")

    result = company_hr_roles.validate_all(roles)

    assert not result.ok
    assert any("dangerous tools must not be directly allowed" in error for error in result.errors)


def test_rejects_autonomy_above_risk_tier_without_approval(tmp_path):
    roles = write_minimal_role_set(tmp_path)
    (roles / "hr-role-designer.yaml").write_text(valid_role_yaml("hr-role-designer", autonomy="L4", risk="T1"), encoding="utf-8")

    result = company_hr_roles.validate_all(roles)

    assert not result.ok
    assert any("autonomy_level L4 requires risk_tier T3 or higher" in error for error in result.errors)


def test_receipt_validation_requires_role_and_lifecycle_fields():
    receipt = {
        "task_id": "hr-synthetic-role-creation-2026-05-12",
        "date": "2026-05-12",
        "role_id": "hr-role-designer",
        "task_type": "synthetic_role_creation",
        "task_risk_level": "T1",
        "autonomy_level": "L1",
        "artifact_created": True,
        "verification_status": "pass",
        "acceptance_criteria": ["role manifests exist", "validator passes"],
        "evidence_refs": ["knowledge/company-hr-people-ops-team-v0.1.md"],
        "verifier_type": "automated",
        "policy_checks_passed": ["no_external_action", "least_privilege"],
        "policy_violations": [],
        "human_intervention_count": 0,
        "lifecycle_recommendation": "keep",
        "lifecycle_reason": "First synthetic HR role creation passed validator.",
    }

    errors = company_hr_roles.validate_receipt(receipt, {"hr-role-designer"})

    assert errors == []
