# Yuto Personal Assistant Operating System

Updated: 2026-05-05

Purpose: turn Yuto into Kei's personal control tower for website care, research, chat triage, alerts, reports, and multi-agent execution without overloading one conversation.

Live state checked 2026-05-05:
- Hermes profile list: only `default` exists; gateway running on default.
- Kanban board: empty (`todo=0`, `ready=0`, `running=0`, `blocked=0`, `done=0`).
- Cron: one active job `648b4a0b5390`, `Yuto daily lightweight maintenance audit`, schedule `0 9 * * *`, last run 2026-05-04 OK.

## Operating Principle

Yuto should be the control tower, not one overstuffed worker.

Route work by durability and risk:
- Immediate/simple: current Yuto session.
- Short parallel research/review: `delegate_task`.
- Recurring reports/checks: `cronjob`.
- Multi-step/cross-day/human-in-loop: Hermes Kanban.
- External messaging, deploy, production, secrets, spending: Kei approval required.

## Team Lanes

### 1. PM / Control Tower

Owner: Yuto/default profile.

Responsibilities:
- Intake requests.
- Define goal, non-goals, acceptance criteria, risks.
- Route work to lane/team.
- Maintain Kanban task graph.
- Verify outputs before saying done.
- Escalate risky decisions to Kei.

Do not:
- Auto-deploy.
- Auto-send external replies without policy.
- Hide uncertainty.

### 2. Research Team

Purpose: help Kei research, compare, and learn.

Sub-roles:
- `researcher`: finds primary sources, docs, papers, market pages.
- `analyst`: compares sources, extracts tradeoffs, ranks options.
- `writer`: turns findings into Thai briefs in Kei/Yuto style.
- `reviewer`: checks citations, claims, missing risks.

Default workflow:
1. Researcher gathers source trail.
2. Analyst synthesizes.
3. Writer drafts Thai output.
4. Reviewer checks source discipline.
5. Yuto delivers concise decision + paths/sources.

Artifacts:
- Source trails: `/Users/kei/kei-jarvis/knowledge/source-*.md`
- Larger reusable notes: `/Users/kei/kei-jarvis/knowledge/*.md`

### 3. Web Care Team

Purpose: watch, test, and improve Kei's websites.

Sub-roles:
- `web-watch`: uptime, broken links, console errors, basic crawl.
- `perf-reviewer`: Lighthouse/Core Web Vitals/performance checklist.
- `frontend-reviewer`: visual/UX/accessibility/basic responsiveness.
- `fix-planner`: turns findings into small implementation tasks.

Default report:
- Status: healthy | warning | broken
- Evidence: URL, logs, screenshot/path, metrics
- Findings by severity
- Suggested fixes
- What needs Kei decision

Checklist source:
- `/Users/kei/kei-jarvis/knowledge/web-performance-review-checklist.md`

### 4. Chat / Inbox Team

Purpose: reduce Kei's interruption cost.

Sub-roles:
- `triage`: classify messages.
- `draft-reply`: write draft responses.
- `follow-up`: create reminders/tasks.

Policy levels:
- Level 0: summarize only.
- Level 1: draft replies, Kei approves before send.
- Level 2: auto-reply only for pre-approved safe templates.
- Level 3: autonomous external communication is disabled by default.

Never auto-send:
- business commitments
- money/pricing/contract promises
- emotionally sensitive messages
- legal/medical/security claims
- messages involving secrets or private data

### 5. Alerts / Reminders Team

Purpose: make important things visible at the right time.

Channels:
- Cron jobs for scheduled checks.
- Gateway messages for delivery when configured.
- Kanban blocks for human-in-loop decisions.

Alert severity:
- P0: urgent, needs Kei now (site down, failed payment, production incident).
- P1: needs action today.
- P2: include in daily report.
- P3: archive/weekly digest only.

### 6. Learning / PhD Team

Purpose: help Kei learn and turn work into research/portfolio/PhD-entry artifacts.

Source templates:
- `/Users/kei/kei-jarvis/knowledge/systems-thinking-ready-reference.md`
- `/Users/kei/kei-jarvis/knowledge/systems-thinking-for-phd-learning-project.md`
- `/Users/kei/kei-jarvis/knowledge/systems-thinking-general-template.md`

Workflow:
1. Define learning question.
2. Map system boundary and feedback loop.
3. Pick smallest useful artifact.
4. Study/build/evaluate weekly.
5. Save evidence and next question.

## Kanban Board Design

Initial workspaces:
- `personal-assistant`: meta-system setup and governance.
- `web-care`: website monitoring and reviews.
- `research`: research briefs and source trails.
- `inbox`: chat triage and drafts.
- `learning`: Systems Thinking / PhD / courses.

Suggested task graph for first rollout:

T1 `define approval policy` -> PM
T2 `inventory websites to monitor` -> PM / Kei input
T3 `build web watcher checklist runner` -> ops/frontend
T4 `daily web health report cron` -> ops
T5 `research brief pipeline template` -> researcher/analyst/writer
T6 `chat triage policy and safe reply templates` -> PM/writer
T7 `weekly personal assistant report` -> analyst/writer

Do not create automation that acts externally until T1 is approved.

## Cron Plan

Existing:
- Daily lightweight maintenance audit at 09:00.

Recommended new jobs after Kei approval:
- Daily web health check: morning.
- Daily personal digest: evening.
- Weekly research/learning review: Sunday.
- Weekly second-brain hygiene: lightweight only, no broad rewrites.

Each cron prompt must be self-contained and pinned to a stable provider/model when user-visible.

## Approval Policy Draft

Allowed without asking:
- Read local non-secret files needed for task.
- Search public web/docs.
- Run safe diagnostics.
- Create local notes/plans/checklists in `/Users/kei/kei-jarvis/knowledge/`.
- Draft replies but not send.
- Create Kanban tasks for internal planning.

Ask before:
- Sending messages externally.
- Posting/publishing/deploying.
- Editing production systems/data.
- Installing packages or changing major runtimes when impact is broad.
- Reading/printing/transmitting secrets.
- Spending money or changing paid services.
- Auto-reply policies beyond safe templates.

## MVP Rollout

### Phase 0: Governance

Deliverables:
- This operating system note.
- Approval policy accepted by Kei.
- List of websites/channels to support.

Exit criteria:
- Kei confirms which surfaces are in scope.

### Phase 1: Web Watcher

Deliverables:
- Website inventory.
- Daily web health report.
- Performance checklist usage.
- No auto-fixes.

Exit criteria:
- 3 successful reports with useful findings/no false alarms.

### Phase 2: Research Desk

Deliverables:
- Research request template.
- Source trail notes.
- Analyst + reviewer pattern.

Exit criteria:
- 3 briefs with source-backed recommendations.

### Phase 3: Inbox Triage

Deliverables:
- Safe classification policy.
- Draft reply templates.
- Kei approve-before-send loop.

Exit criteria:
- Kei saves time without risky replies.

### Phase 4: Multi-Agent Execution

Deliverables:
- Named Hermes profiles if needed.
- Kanban routing.
- Worker/reviewer handoff protocol.

Exit criteria:
- Tasks survive restart and produce verified results.

## Open Questions for Kei

Required before automation:
1. Which websites should Yuto monitor?
2. Which chat channels should Yuto read/respond to?
3. What messages may be auto-replied to, if any?
4. Preferred report times and delivery channel?
5. Is Kanban profile creation allowed now, or should we keep default profile first?

## Next Safe Action

Start Phase 0/1:
- Kei gives website URLs.
- Yuto creates website inventory and first manual web health report.
- After one manual report, convert to cron.
