# Company Ops Tick 0001 — Internal Startup

Date: 2026-05-12
Generated at: 2026-05-12T06:53:37Z
Status: partial_started — internal-only company machine is on, but not autonomous or external-facing.
Owner: Yuto Control
Final authority: Kei

## Startup gate

Fact from live check before this tick:
- `python tools/company_hr_roles.py --json --summary-receipts` returned `ok=true` with 3 HR role manifests.
- `python tools/memory_scout.py --session-limit 1` reported HR validator `ok=true` and HR role manifest count `3`.

## Operating mode opened

The company is now running in **Phase 0 internal pilot mode**:

Allowed:
- internal org design;
- role manifests;
- validators;
- synthetic/design receipts;
- Yuto-verified scout reports;
- no-risk research planning.

Not allowed without Kei approval:
- external messaging or outreach;
- publishing;
- production access;
- spending;
- secrets;
- real victim/case data;
- legal, forensic, security, or compliance final claims.

## Roles invoked in this tick

### Chief of Staff / Org Architect

Decision: start only as internal operating tick. Do not open Intelligence, Legal, Forensic, Security, or Case Ops yet.

Reason: company needs a governance/expert gate before specialized departments can safely operate.

### HR Role Designer

Decision: next role to draft is `Compliance / Safety / Expert Network Lead`.

Reason: it is the first department lead needed to define red lines, expert review gates, and approval routing.

### Culture & Safety Steward

Safety decision: pass with limits.

Limits:
- no external actions;
- no sensitive or production data;
- no real case/victim data;
- no public/legal/forensic/compliance claims;
- Kei approval required before crossing any of those gates.

## Tick decision

Start the company machine in low-autonomy, internal-only mode.

Next operating task:
1. Draft `Compliance / Safety / Expert Network Lead` manifest.
2. Validate it with a role/department validator.
3. Append a controlled receipt.
4. Re-run Scout and Second Brain checks.

## Receipt

Machine-readable receipt appended to:

`/Users/kei/kei-jarvis/knowledge/company-operating-receipts.jsonl`
