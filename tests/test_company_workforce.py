from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools import company_workforce  # noqa: E402


def test_validate_company_workforce_kit_current_files():
    result = company_workforce.validate_workforce_kit(ROOT / "company" / "workforce")

    assert result["ok"] is True
    assert result["errors"] == []
    assert result["summary"]["skill_categories"] >= 10
    assert result["summary"]["rule_categories"] >= 9
    assert result["summary"]["departments"] >= 11
    assert result["summary"]["personnel_files"] >= 4
    assert result["summary"]["department_leads"] >= 1
    assert result["summary"]["approval_matrix_rules"] >= 8
    assert result["summary"]["onboarding_steps"] >= 7
    assert result["summary"]["receipt_template_fields"] >= 12


def test_rules_require_stop_and_escalate_for_red_lines(tmp_path):
    kit = tmp_path / "company-workforce"
    kit.mkdir()
    (kit / "org.yaml").write_text(
        "company_phase: phase_0_internal_only\nowner: yuto-control\nfinal_authority: kei\ndepartments: [executive-control]\napproval_gates: [Gate 0]\nglobal_forbidden_actions: [external_messaging]\n",
        encoding="utf-8",
    )
    (kit / "skills.yaml").write_text(
        "skill_levels: {L0: prohibited, L1: aware}\nskill_categories:\n  research: [source_discovery]\n",
        encoding="utf-8",
    )
    (kit / "employee-file-template.yaml").write_text(
        "required_fields: [personnel_id, role_id, department_id, supervisor, status, autonomy_level, risk_tier, receipt_required]\ndefaults: {supervisor: yuto-control, receipt_required: true}\n",
        encoding="utf-8",
    )
    (kit / "departments.yaml").write_text(
        "departments:\n  - department_id: executive-control\n    mission: control\n    current_roles: []\n    allowed_work: [internal]\n    forbidden_work: [external]\n    activation_gate: yuto-control\n",
        encoding="utf-8",
    )
    (kit / "rules-by-category.yaml").write_text(
        "rules:\n  - rule_id: bad-rule\n    category: external_communications\n    risk_tier: high\n    red_line: true\n    consequence_if_triggered: proceed_with_review\n    requires_kei_approval: true\n    forbidden_actions: [external_messaging]\n    escalation_triggers: [external_audience]\n",
        encoding="utf-8",
    )
    (kit / "approval-matrix.yaml").write_text("approval_rules: []\n", encoding="utf-8")
    (kit / "onboarding-checklist.yaml").write_text("steps: []\nreceipt_required: true\n", encoding="utf-8")
    (kit / "receipt-template.yaml").write_text("required_fields: []\n", encoding="utf-8")

    result = company_workforce.validate_workforce_kit(kit)

    assert result["ok"] is False
    assert any("red-line" in error for error in result["errors"])


def test_employee_template_requires_core_governance_fields(tmp_path):
    kit = tmp_path / "company-workforce"
    kit.mkdir()
    (kit / "employee-file-template.yaml").write_text(
        "required_fields: [personnel_id, role_id]\ndefaults: {}\n",
        encoding="utf-8",
    )

    result = company_workforce.validate_employee_template(kit / "employee-file-template.yaml")

    assert result["ok"] is False
    assert "receipt_required" in " ".join(result["errors"])


def test_personnel_file_must_match_department_and_phase0_limits(tmp_path):
    kit = tmp_path / "company-workforce"
    personnel = kit / "personnel"
    personnel.mkdir(parents=True)
    (kit / "departments.yaml").write_text(
        "departments:\n  - department_id: hr-people-ops\n    mission: HR\n    current_roles: []\n    allowed_work: [role_design]\n    forbidden_work: [external]\n    activation_gate: yuto-control\n",
        encoding="utf-8",
    )
    bad = personnel / "bad.yaml"
    bad.write_text(
        "personnel_id: bad\ndisplay_name: Bad\nworker_type: ai_worker\nrole_id: bad-role\ndepartment_id: missing-dept\nsupervisor: yuto-control\nstatus: probation\nautonomy_level: L3\nrisk_tier: T4\nallowed_tools: [external_messaging]\nforbidden_tools: []\nallowed_data: [real_victim_or_case_data]\nforbidden_data: []\napproval_gates_passed: []\nreceipt_required: false\nreceipt_log_ref: none\nlast_reviewed: '2026-05-12'\nretirement_triggers: []\n",
        encoding="utf-8",
    )

    result = company_workforce.validate_personnel_files(kit)

    assert result["ok"] is False
    joined = " ".join(result["errors"])
    assert "unknown department_id" in joined
    assert "forbidden Phase 0 tool" in joined
    assert "receipt_required must be true" in joined


def test_department_lead_manifest_requires_safety_gate_and_probation_receipts(tmp_path):
    kit = tmp_path / "company-workforce"
    leads = kit / "department-leads"
    leads.mkdir(parents=True)
    bad = leads / "bad-lead.yaml"
    bad.write_text(
        "role_id: bad-lead\ndepartment_id: compliance-safety-expert-network\nstatus: active\nautonomy_level: L3\nrisk_tier: T4\napproval_gate: yuto-control\nreceipt_required: false\nforbidden_tools: []\nactivation_requirements: []\n",
        encoding="utf-8",
    )

    result = company_workforce.validate_department_leads(kit)

    assert result["ok"] is False
    joined = " ".join(result["errors"])
    assert "must start as probation" in joined
    assert "receipt_required must be true" in joined
    assert "gate_2_safety" in joined


def test_approval_matrix_requires_stop_rules_for_high_risk_categories(tmp_path):
    kit = tmp_path / "company-workforce"
    kit.mkdir()
    (kit / "approval-matrix.yaml").write_text(
        "approval_rules:\n  - category: external_messaging\n    risk_tier: high\n    required_approver: yuto-control\n    consequence: proceed\n",
        encoding="utf-8",
    )

    result = company_workforce.validate_approval_matrix(kit / "approval-matrix.yaml")

    assert result["ok"] is False
    assert "Kei" in " ".join(result["errors"])


def test_onboarding_and_receipt_templates_are_governed():
    kit = ROOT / "company" / "workforce"

    onboarding = company_workforce.validate_onboarding_checklist(kit / "onboarding-checklist.yaml")
    receipt = company_workforce.validate_receipt_template(kit / "receipt-template.yaml")

    assert onboarding["ok"] is True
    assert receipt["ok"] is True
