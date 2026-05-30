## 핵심 요약
- **사용자는 kanban worker/cron/project session 진행 상황을 숨은 세션을 뒤져서 찾지 않아도, 현재 thread · Project OS panel · 전역 activity surface 중 맞는 자리에서 바로 볼 수 있어야 합니다.**
- **MVP는 새 실행 엔진이나 shadow transcript를 만드는 게 아니라, 이미 있는 Hermes session/activity/artifact/kanban/cron surfaces를 얇게 다시 묶는 것입니다.**
- **현재 thread에는 ‘최종 요약/의사결정’만 올리고, live progress는 Project OS panel과 전역 activity에서 보조적으로 보여주는 3-surface 분리가 기본입니다.**
- **same-thread delivery가 실패해도 조용히 성공처럼 보이면 안 되고, panel/activity에서 실패와 fallback 경로(`Open session`, `Open report`, 최근 worker output 요약)를 바로 보여줘야 합니다.**

# Project OS execution surface MVP

## Scope
Card: `t_efb7ae49`  
Board: `pux-import-e2e-1780010472`  
Purpose: define the minimum Hermes-native execution surface for kanban workers, cron jobs, and dedicated Project OS sessions without introducing a parallel execution product.

## Product goal
Background work should feel continuous in WebUI even when the actual execution happens in:
- a dedicated Project OS session,
- a kanban worker lane,
- a cron job,
- or a linked artifact/report path.

The user should not have to hunt through hidden sessions to answer four basic questions:
1. Is something running right now?
2. What just finished?
3. Does this need my decision?
4. If delivery back to this thread failed, where do I go next?

## Non-goals
This slice must **not** introduce:
- a new Project OS-only execution engine,
- a shadow activity database or run ledger,
- a duplicate Project OS transcript separate from Hermes sessions,
- a Project OS-only artifact viewer,
- or a broad chat/sidebar/composer redesign.

## Reused Hermes-native surfaces
### 1. Current thread
Use for:
- operator-facing final summaries,
- decision-needed outcomes,
- completion/failure closeouts when origin delivery succeeds,
- concise fallback summaries when origin delivery fails but a manual summary is still available.

Do **not** use the current thread as the full live log for every worker heartbeat.

### 2. Project OS panel / board drawer
Use as the **project-local live readout** for:
- active/running state,
- latest execution source (`project session`, `cron`, `kanban worker`),
- latest meaningful progress text,
- quick open actions (`Open evidence thread`, `Open session`, `Open report`, artifact open path),
- and delivery/fallback status.

This is the main place where the user should understand ongoing work without leaving the project context.

### 3. Global Hermes activity surface
Use for:
- cross-project scanning,
- recent completed background work,
- recent failed runs,
- and quick navigation into the owning project/session.

This surface should stay quiet and list-oriented. It is not the canonical project truth; it is the inbox/outbox view.

### 4. Canonical board / task detail
Use for:
- workflow truth,
- queue order,
- blocked vs ready vs done semantics,
- and task-local comments/evidence links.

Board truth remains the canonical workflow source. The execution surface may summarize board-linked activity, but must not replace board state.

## State model
The MVP should distinguish five user-meaningful states.

### A. Running
Meaning:
- work is actively executing somewhere now.

Minimum UI:
- source label (`Dedicated session`, `Cron`, `Kanban worker`),
- running chip,
- relative last-update timestamp,
- one short latest progress line,
- one open action into the owning session/run.

### B. Settled success
Meaning:
- work completed and produced a stable output.

Minimum UI:
- completed chip,
- one-line result summary,
- links to the owning session/artifact/report,
- whether the result was delivered back to the current thread.

### C. Needs decision
Meaning:
- the system is blocked on approval, clarify, or explicit operator choice.

Minimum UI:
- decision-needed chip,
- short reason text,
- action link into the owning thread/session/task.

This state is allowed to be visually stronger than ordinary progress because it is action-required.

### D. Failed but inspectable
Meaning:
- the run failed, but evidence exists.

Minimum UI:
- failed chip,
- short error summary,
- `Open session` / `Open report` / task comment path,
- no silent disappearance.

### E. Delivery failed, work still real
Meaning:
- the run completed or progressed, but the intended return path to the current thread did not succeed.

Minimum UI:
- explicit fallback status such as `Result not delivered to this thread`,
- open action into the owning session or cron output,
- latest worker output snippet if available,
- board/task comment remains the durable fallback.

This is the critical state for seamlessness: the UI must show **work happened, delivery missed, here is the fallback**.

## Surface-by-surface rules
## Current thread
Show in-thread only when one of these is true:
1. the run is explicitly `deliver=origin` and same-thread delivery succeeds,
2. a final summary is ready and materially useful to the operator,
3. the result needs a decision now,
4. or the product is intentionally posting a fallback summary because delivery failed.

Avoid streaming every background heartbeat into the thread.

## Project OS panel / drawer
This should be the primary execution readout for Project OS work.

Recommended card stack order:
1. workflow/board truth,
2. execution feed / latest run state,
3. evidence actions,
4. closeout/decision status.

Recommended fields for the execution card:
- source type,
- status chip,
- last updated,
- short latest progress text,
- latest artifact/report if present,
- thread delivery state (`Delivered here`, `Open linked thread`, `Delivery fallback active`),
- open actions.

The card should reuse the existing execution feed direction from `t_3017cbac`, not replace it.

## Global activity surface
The global activity feed should answer: “what background work across Hermes needs my attention?”

Recommended entries:
- running items with source + project name,
- completed items with one-line result,
- failed or decision-needed items pinned higher,
- quick navigation to the owning project/session/task.

This should remain scan-first and compact; details open into the project/session surfaces.

## Distinguishing progress vs summary vs decision
### Live progress
- short, transient, panel-first,
- comes from existing session/tool/progress surfaces,
- should collapse in settled history.

### Final summary
- durable, operator-readable,
- may appear in the current thread if delivery succeeds,
- should also remain discoverable from the Project OS panel.

### Actionable decision
- visually stronger than ordinary progress,
- should appear wherever the operator is most likely to act: current thread if possible, otherwise panel + board comment fallback.

## Failure-handling contract
If delivery to the current thread fails, do **not** imply success.

Minimum fallback stack:
1. mark delivery state explicitly in the Project OS panel,
2. keep the latest evidence reachable through `Open session` / `Open evidence thread` / artifact/report open path,
3. preserve a board/task-level comment or run summary as durable truth,
4. optionally post a concise manual summary into the active chat only when operator visibility would otherwise be lost.

Fallback priority:
- first choice: same-thread delivery,
- second choice: linked session open path,
- third choice: board/task comment + project panel summary,
- fourth choice: operator-facing manual summary in chat.

## Connection to import/resume workflows
For Project OS import/resume, the execution surface must stay attached to the existing workflow steps:
- `Recover current repo` and `Refresh docs` remain the action triggers,
- the linked dedicated session remains the primary evidence thread,
- the canonical board remains workflow truth,
- reports/artifacts keep using existing open paths,
- the panel explains whether the latest meaningful work came from import/recover, cron follow-up, or kanban worker continuation.

This keeps import/resume from feeling like a separate product mode. It remains one Hermes workflow with multiple evidence surfaces.

## Recommended MVP before any large refactor
1. **Keep the current bounded Execution feed card** from `t_3017cbac` as the base.
2. Add explicit source/status/delivery-state framing instead of only raw recent execution items.
3. Show one project-local latest-result summary in the Project OS panel even when the current thread did not receive it.
4. Reuse existing open actions into linked session/artifact/report paths.
5. Add a compact global activity readout for running/completed/failed background work, but keep board truth separate.
6. Treat board comments and task detail as the durable fallback when thread delivery fails.
7. Do **not** add backend storage unless a concrete missing field is proven.

## Follow-up implementation split
### UI layer first
Owned files:
- `extensions/project-os/project-os-extension.js`
- `tests/test_project_os_extension_regressions.py`
- optional tiny CSS only if needed for chips/layout

Goal:
- refine the existing execution card so it shows source, status, last meaningful progress, and delivery fallback state.

### Generic activity surface follow-up
Owned area:
- relevant `static/*.js` activity rendering files
- focused UI tests

Goal:
- make global background activity scannable without inventing a second truth system.

### API follow-up only if proven necessary
Owned area:
- existing API modules/tests only

Rule:
- add fields only when the current session/cron/task surfaces cannot already expose the required source/status/delivery fact.

## Acceptance check for this design slice
This card is complete when repo docs clearly answer:
- where live progress should appear,
- where final summaries should appear,
- where decisions should appear,
- what happens when same-thread delivery fails,
- and what the minimum Hermes-native UX is before any larger refactor.
