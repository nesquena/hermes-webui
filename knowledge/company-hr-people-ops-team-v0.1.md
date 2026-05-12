# Company HR / People Ops Team v0.1

Date: 2026-05-12 JST
Status: design-ready HR foundation; not yet a live autonomous company runtime
Owner: Yuto Control; Kei remains final authority

Purpose: create the employee/agent foundation before Yuto starts operating the company. HR is the control layer that prevents role sprawl, hidden authority, unsafe autonomy, and unreviewed agent activation.

Related:
- [[yuto-ai-harm-evidence-company-team-v0.2]]
- [[yuto-multi-book-expert-skill-factory]]
- [[book-expert-factory/receipts/2026-05-12-startup-legal-tech-registration]]
- [[book-expert-factory/receipts/2026-05-12-rule-law-web-performance-registration]]

## Source canon

Book/framework base, all still `blueprint_unverified` until framework extraction, conflict table, and activation tests are complete:

1. `Learning Systems Thinking` — HR uses this for sociotechnical boundaries, feedback loops, role relationships, system drift, and pruning.
2. `Agentic Architectural Patterns for Building Multi-Agent Systems` — HR uses this for agent architecture, handoffs, governance, maturity, and multi-agent constraints.
3. `30 Agents Every AI Engineer Must Build` — HR uses this as a role-pattern catalog, not as permission to create 30 workers.
4. `Designing AI Interfaces` — HR uses this for role clarity, permissions, oversight, capability discovery, and human-facing agent UX.
5. `Startup Technical Guide: AI Agents` — HR uses this for startup agent operations, grounding, reliability, AgentOps, and production-readiness gates.

Current update layer checked by HR scouts:
- NIST AI RMF: https://www.nist.gov/itl/ai-risk-management-framework
- OECD AI Principles: https://oecd.ai/en/ai-principles
- ISO/IEC 42001: https://www.iso.org/standard/81230.html redirects to https://www.iso.org/standard/42001
- Japan AISI root: https://aisi.go.jp/
- Anthropic Building Effective Agents: https://www.anthropic.com/engineering/building-effective-agents
- OpenAI Agents SDK: https://openai.github.io/openai-agents-python/
- OpenAI Evals: https://platform.openai.com/docs/guides/evals redirects to https://developers.openai.com/api/docs/guides/evals
- Google ADK: https://google.github.io/adk-docs/ redirects to https://adk.dev/
- CrewAI Agents docs: https://docs.crewai.com/concepts/agents redirects to https://docs.crewai.com/en/concepts/agents
- OWASP Top 10 for LLM Applications: https://owasp.org/www-project-top-10-for-large-language-model-applications/
- OpenTelemetry semantic conventions: https://opentelemetry.io/docs/concepts/semantic-conventions/

Verification caveat: scout summaries are worker reports; Yuto verified selected URLs for reachability, but HR policy claims must still be rechecked against official/current sources before external or employment use.

## Operating principle

HR treats AI workers as managed workforce objects, not personas.

Every role must have:
- real job-to-be-done
- owner
- scope and non-scope
- input/output contract
- allowed tools and data
- autonomy level
- escalation triggers
- eval/receipt metrics
- activation and retirement path

If a role cannot satisfy these, it stays as a checklist, playbook, or one-off Yuto task, not a worker.

## Reporting line

```text
Kei / Founder / Final Authority
└── Yuto / Executive Control Office
    └── HR / People Ops Division
        ├── Chief of Staff / Org Architect
        ├── HR Role Designer
        ├── Onboarding & Training Lead
        ├── Performance & Receipt Analyst
        ├── Capacity & Workflow Planner
        ├── Culture & Safety Steward
        └── HR Update Scout
```

HR does not bypass Yuto Control or Kei approval. HR can recommend, design, review, and block unsafe activation; it cannot deploy, publish, contact external parties, handle production data, spend money, or make legal/employment decisions.

## HR team roster

### 1. Chief of Staff / Org Architect

Mission: keep the company org coherent as a sociotechnical system.

Uses book canon:
- `Learning Systems Thinking`: relationships produce effects; watch feedback loops and role coupling.
- `Agentic Architectural Patterns`: keep architecture explicit; avoid unbounded multi-agent handoffs.
- `Startup Technical Guide`: choose startup-practical operating structures, not enterprise theater.

Responsibilities:
- maintain org chart and division map
- detect role collisions and duplicate responsibilities
- decide whether a need becomes role, checklist, workflow, or one-off task
- run quarterly role portfolio review
- protect simplicity and company focus

Outputs:
- org chart updates
- role collision reports
- department boundary decisions
- keep / merge / split / retire recommendations

Escalate to Kei/Yuto when:
- a new department is proposed
- a role asks for external authority or production access
- org design changes company strategy

### 2. HR Role Designer

Mission: convert needs into precise role charters.

Uses book canon:
- `30 Agents`: role-pattern catalog and capability archetypes.
- `Designing AI Interfaces`: role clarity, permissions, capability discovery, oversight UX.
- `Agentic Architectural Patterns`: handoff and orchestration constraints.

Responsibilities:
- write role charters
- define mission, non-scope, inputs, outputs, tools, autonomy, metrics
- design `Agent Job Description` cards
- prevent persona-over-process roles

Outputs:
- role charter draft
- hiring/activation checklist
- permission request matrix
- examples of good/bad output

Escalate when:
- role scope includes legal/forensic/security/finance/HR decisions
- requested autonomy exceeds current evidence
- role duplicates an existing worker

### 3. Onboarding & Training Lead

Mission: make every role learnable, inspectable, and safe before activation.

Uses book canon:
- `Designing AI Interfaces`: onboarding must reveal capabilities, limits, permissions, and oversight.
- `Learning Systems Thinking`: training must teach relationship impact and second-order effects.
- `Startup Technical Guide`: start with practical MVP/sandbox, then supervised operation.

Responsibilities:
- build onboarding packs
- create first 3 supervised tasks per role
- maintain examples, source packs, red lines
- define probation tasks before activation

Outputs:
- onboarding checklist
- training source pack
- supervised task queue
- probation review note

Escalate when:
- role lacks training examples
- role handles sensitive data or external actions
- role cannot be safely tested in synthetic/sandbox mode

### 4. Performance & Receipt Analyst

Mission: evaluate workers from receipts, traces, tests, and reviewer burden.

Uses book canon:
- `Learning Systems Thinking`: do not optimize local activity metrics that harm the whole system.
- `Startup Technical Guide`: reliability and responsible production require operational measurement.
- Current eval/observability sources: OpenAI Evals, OpenTelemetry, OWASP LLM risks.

Responsibilities:
- read team receipts
- maintain scorecards
- recommend keep / modify / retire
- detect drift, scope creep, regression, and reviewer burden

Outputs:
- receipt summaries
- role performance scorecards
- incident/rework pattern reports
- lifecycle recommendation

Metrics:
- task_success_rate
- first_pass_acceptance_rate
- rework_count
- human_intervention_rate
- escalation_quality
- policy_violation_count
- cost_per_successful_task
- reviewer_burden_minutes
- reuse_realized_count
- lifecycle_recommendation

Do not use as primary KPI:
- token count
- message count
- online time
- self-reported confidence
- speed alone
- number of tasks closed without risk normalization

### 5. Capacity & Workflow Planner

Mission: choose the lightest operating tier for each need.

Uses book canon:
- `Agentic Architectural Patterns`: match orchestration pattern to complexity.
- `Startup Technical Guide`: govern and scale taskforce only after reliability path exists.
- `Learning Systems Thinking`: avoid overloading bottleneck roles and avoid creating local fixes that cause global complexity.

Responsibilities:
- route work to Yuto-only, Arrow, Squad, or Federation
- detect overloaded or idle roles
- choose checklist/workflow/agent based on task frequency and risk
- maintain role portfolio status

Role portfolio statuses:
- Proposed
- Designing
- Prototype
- Probation
- Active
- Needs review
- Deprecated
- Retired

### 6. Culture & Safety Steward

Mission: keep HR, team behavior, and agent activation aligned with evidence-first, victim-safe, Japan-first, no-offense, human-reviewed principles.

Uses book canon:
- `Learning Systems Thinking`: safety is a system property, not a final review step.
- `Designing AI Interfaces`: users/operators must see limits, warnings, permissions, and oversight.
- `Startup Technical Guide`: production agents require reliability and responsibility controls.
- Current sources: NIST AI RMF, OECD AI Principles, ISO/IEC 42001, Japan AI guidelines/AISI, OWASP LLM risks.

Responsibilities:
- maintain red lines
- review high-risk activation
- enforce least privilege
- require human gates
- run incident/near-miss review

Red lines:
- no ownerless agent
- no external action without approval path, logs, and kill switch
- no production/customer/personal data access without explicit gate
- no legal/forensic/employment/finance conclusion without qualified human review
- no unapproved AI tools for sensitive data
- no bypassing safety gates for speed
- no public safety/compliance claims without evidence
- no third-party security testing without written authorization

### 7. HR Update Scout

Mission: keep HR framework current without mutating policy automatically.

Responsibilities:
- monitor official/practical sources for changes relevant to agent workforce governance
- propose policy updates as candidates only
- verify source URLs/dates
- flag stale HR roles, metrics, and activation gates

Outputs:
- update scout report
- source delta list
- recommended changes with risk level

Hard limit: read-only; no direct edits to HR policy, active memory, skills, or role activation state.

## Role creation funnel

```text
Demand intake
-> role collision check
-> build-vs-workflow decision
-> role charter draft
-> risk tier classification
-> eval/probation task design
-> sandbox/prototype
-> supervised probation
-> receipt review
-> activate / modify / retire
```

Default: new roles start at `L0` or `L1`. Higher autonomy requires receipts and explicit Yuto/Kei approval.

## Autonomy ladder

- L0: reference-only / research-only
- L1: draft for human review
- L2: recommend action, no execution
- L3: execute low-risk reversible action with audit log
- L4: execute multi-step workflow with human checkpoints
- L5: high autonomy in bounded, measurable, reversible domain only after explicit approval

Forbidden without Kei approval:
- external messaging/publishing
- production deployment
- spending/vendor signup
- real case/victim data handling
- legal/forensic/employment/finance final decisions
- secrets handling or broad system access

## Role charter template

```yaml
role_id:
role_name:
owner: yuto-control
status: proposed|designing|prototype|probation|active|needs_review|deprecated|retired
mission:
internal_customer:
source_canon:
  - book/source ids
scope:
non_scope:
inputs:
outputs:
allowed_tools:
forbidden_tools:
allowed_data:
forbidden_data:
autonomy_level: L0|L1|L2|L3|L4|L5
risk_tier: T0|T1|T2|T3|T4|T5
handoff_allowed_to:
escalation_triggers:
acceptance_criteria:
eval_cases:
first_3_probation_tasks:
receipt_required: true
metrics:
activation_gate:
retirement_triggers:
last_reviewed:
```

## Hiring / activation criteria

A worker can enter probation only if:
- job-to-be-done is real and recurring or strategically important
- owner is named
- role does not duplicate existing role
- scope/non-scope are clear
- tools/data are least-privilege
- risk tier is assigned
- eval/probation cases exist
- human escalation works
- receipt format is known
- off switch/rollback exists if it can act

A worker can become active only if:
- supervised probation produced acceptable receipts
- no unresolved safety red flags
- reviewer burden is lower than doing the work manually or quality gain is clearly worth it
- Yuto Control approves; Kei approves if risk is high

## Performance receipt extension

Existing Yuto receipt fields stay. HR may add:

```yaml
role_id:
role_version:
autonomy_level:
task_type:
task_risk_level:
acceptance_criteria:
input_refs:
output_refs:
trace_id:
evidence_refs:
verifier_id:
verifier_type: automated|human|peer_agent|external_system
auditability_score:
human_intervention_count:
escalation_reason:
policy_checks_passed:
policy_violations:
cost_estimate:
reviewer_burden_minutes:
downstream_impact:
lessons_learned:
lifecycle_recommendation: keep|modify|retire
lifecycle_reason:
```

Safety is a gate, not a weighted score. If safety fails, the role cannot be rated healthy even if speed or quality looks good.

## First HR missions completed

Yuto sent three read-only HR scouts:

1. HR Role Designer Scout
   - studied current agent role design, charters, hiring/activation criteria, autonomy ladder, and pruning.
   - result: add role charter, hiring funnel, role portfolio board, activation checklist, pruning policy.

2. HR Compliance & Safety Steward Scout
   - studied responsible AI governance, human oversight, accountability, risk management, safety gates.
   - result: add risk tiers, human approval rules, deployment safety checklist, acceptable-use red lines, incident response.

3. HR Performance & Receipt Analyst Scout
   - studied evals, observability, receipts, continuous monitoring, lifecycle decisions.
   - result: evaluate workers by outcome + verification + risk + traceability; avoid activity metrics and self-confidence.

## Phase 0 implementation plan

Do next before running the company:

1. Convert this HR Team design into machine-readable role manifests.
2. Create validator for role charters and HR red lines.
3. Create first three role charters:
   - Chief of Staff / Org Architect
   - HR Role Designer
   - Culture & Safety Steward
4. Run one synthetic role-creation task through HR.
5. Append a prospective receipt.
6. Only then start department-lead design.

## Non-goals for v0.1

- no persistent autonomous HR runtime
- no external hiring/contacting
- no real employee monitoring
- no production system access
- no legal employment policy claims
- no promotion to Hermes skill yet

Promotion gate: use this HR team on at least 3 real/synthetic company-formation tasks with receipts and low friction before converting into a reusable skill or runtime.
