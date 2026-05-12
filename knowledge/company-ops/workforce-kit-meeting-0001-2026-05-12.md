# Workforce Kit Meeting 0001 — HR Foundation

Date: 2026-05-12
Status: verified; Phase 0 internal-only workforce kit prepared
Owner: Yuto Control
Participants: Chief of Staff / Org Architect, HR Role Designer, Culture & Safety Steward, Yuto Control

## Scope

Prepare employee/personnel files, skills taxonomy, rules/prohibitions by category, approval gates, onboarding, receipts, and the minimum workforce kit before adding more company workers.

## Decisions

1. Keep current HR trio as probation workforce roles:
   - `chief-of-staff-org-architect`
   - `hr-role-designer`
   - `culture-safety-steward`
2. Add `Yuto Scout / Memory Scout` as read-only probation personnel in Knowledge Infrastructure.
3. Create the first probation department lead manifest:
   - `compliance-safety-expert-network-lead`
4. Keep this as a thin workforce layer, not a full HRIS/runtime.
5. Use machine-readable YAML plus a validator.
6. Treat all other departments as categories/manifests first, not live workers.
7. Require receipts and safety gates before any worker activation.

## Files prepared

Core kit:
- `knowledge/company-workforce/org.yaml`
- `knowledge/company-workforce/employee-file-template.yaml`
- `knowledge/company-workforce/skills.yaml`
- `knowledge/company-workforce/departments.yaml`
- `knowledge/company-workforce/rules-by-category.yaml`
- `knowledge/company-workforce/approval-matrix.yaml`
- `knowledge/company-workforce/onboarding-checklist.yaml`
- `knowledge/company-workforce/receipt-template.yaml`

Personnel:
- `knowledge/company-workforce/personnel/personnel-chief-of-staff-org-architect-001.yaml`
- `knowledge/company-workforce/personnel/personnel-hr-role-designer-001.yaml`
- `knowledge/company-workforce/personnel/personnel-culture-safety-steward-001.yaml`
- `knowledge/company-workforce/personnel/personnel-yuto-scout-001.yaml`

Department lead:
- `knowledge/company-workforce/department-leads/compliance-safety-expert-network-lead.yaml`

Validation:
- `tools/company_workforce.py`
- `tests/test_company_workforce.py`

## Non-negotiable Phase 0 limits

No external messaging, publishing, production deployment, spending, secrets access, real employee personal data, real victim/case data, final legal/forensic/security/compliance claims, offensive cyber activity, covert monitoring, or automated employment decisions.

## Verified validator metrics

`python tools/company_workforce.py --json`:

- ok=true
- approval_gates=8
- approval_matrix_rules=8
- departments=11
- department_leads=1
- personnel_files=4
- employee_required_fields=18
- onboarding_steps=8
- receipt_template_fields=22
- rule_categories=9
- skill_categories=10

## Next action

Use this workforce kit to run the first probation task for `Compliance / Safety / Expert Network Lead`:

1. review workforce kit red lines;
2. draft department activation checklist;
3. review the next proposed department lead for Phase 0 limits.

No live external/company operation is authorized by this meeting.
