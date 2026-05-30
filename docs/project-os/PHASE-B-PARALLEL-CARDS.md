# Project OS Phase B Parallel Work Cards

_Last updated: 2026-05-30T12:08:08Z_

## Purpose
이 문서는 Project OS / Hermes WebUI의 **Phase B 병렬 개발/리뷰 착수 기준**을 repo truth 기준으로 고정한다.

핵심 원칙:
- Project OS는 **Hermes-native thin layer**여야 한다.
- 새 실행 엔진 / shadow activity subsystem / shadow backlog truth를 만들지 않는다.
- 병렬화는 가능하지만, **같은 dirty tree를 여러 Codex가 동시에 건드리게 하면 안 된다.**
- Codex 병렬 실행은 **파일 ownership 분리 + separate worktree + bounded acceptance criteria** 조건에서만 안전하다.

---

## Can this be run with Codex via cron?
### Short answer
**Yes, but not as "many Codex jobs on one working tree".**

### Safe operating model
1. **One lane = one bounded card**
2. **One lane = one separate worktree or isolated repo copy**
3. **One lane = one explicit owned-file area**
4. **One cron = supervision/recall loop**, not blind overlapping edits on the same checkout
5. Parent/orchestrator must still verify:
   - changed files
   - test results
   - continuity updates

### Good fits for Codex+cron lanes
- isolated extension/test slice
- isolated API/delivery slice
- read-only review lane
- continuity refresh lane

### Bad fits
- two Codex workers editing `extensions/project-os/project-os-extension.js` at the same time
- one lane changing `api/routes.py` while another lane changes the same runtime path without a merge plan
- broad "finish Phase B" prompts with no owned files

---

## Board truth at planning time
Board: `pux-import-e2e-1780010472`

Relevant cards:
- `t_5bdf7695` — done — stale-running import completion fix is closed on canonical board truth
- `t_f145cc08` — done — same-thread WebUI origin delivery fix is closed on canonical board truth
- `t_8dcfd135` — done — root-seed guardrail and clean-board proof are both closed on canonical board truth
- `t_3017cbac` — done — Phase B import MVP execution feed is closed on fresh browser proof
- `t_d288a08e` — done — Phase B design intake — required UX / optional UX / forbidden scope / follow-up owned-file split are canonically recorded below and in `docs/project-os/PLAN.md`
- `t_efb7ae49` — done — canonical MVP execution-surface design note for kanban workers and cron jobs is now landed and linked from this doc
- `t_a982118d` — todo/reference-only — Target B / Claude parity carry-forward umbrella; do not treat as the next ready lane
- `t_132c99ea` — todo — AUTOPILOT backlog seed / drift reconcile entry point (decompose before execution)

Current queue truth:
- canonical board is now `triage 0 / todo 16 / ready 0 / running 0 / blocked 14 / done 24`
- there is **no open ready card** to auto-release from this planning doc
- next move is explicit bounded replanning, not another automatic thaw from `todo` or `blocked`

### Deferred artifact-backed tracks (do not promote into the immediate queue)
- `t_59d4d6e5` — dedicated-session runtime-enforcement parent track; keep deferred until the current reliability gates are closed, even though useful source/tests already exist
- `t_358df935` — `/goal` integration proposal track; treat the proposal artifact as input for a later owned-file child slice, not as a current execution lane
- `t_ea52a3f4` — harness-triage doc/test track; keep as QA follow-up evidence, not hot-path product work
- `t_2e9300e6` — end-to-end workflow completion umbrella; preserve as a broad carry-forward reference, not a near-term implementation lane
- `t_03fb9d7e` — automation-level scaffold track; keep behind the current reliability and proof gates

---

## Concrete lane/worktree mapping

- **Lane A / `t_5bdf7695`**
  - Worktree: `/Users/parantoux/Andy/worktrees/hermes-webui-t5bdf7695`
  - Branch: `codex/t5bdf7695-stale-running-state`
  - Mode: implementation
  - Owned area: `extensions/project-os/project-os-extension.js`, `tests/test_project_os_extension_regressions.py`
- **Lane B / `t_f145cc08`**
  - Worktree: `/Users/parantoux/Andy/worktrees/hermes-webui-tf145cc08`
  - Branch: `codex/tf145cc08-origin-delivery`
  - Mode: implementation
  - Owned area: `api/routes.py` + related delivery tests
- **Lane C / `t_d288a08e`**
  - Worktree: `/Users/parantoux/Andy/worktrees/hermes-webui-td288a08e`
  - Branch: `codex/td288a08e-transparency-intake`
  - Mode: review/design
  - Owned area: `docs/project-os/PHASE-B-PARALLEL-CARDS.md`, `docs/project-os/PLAN.md`, continuity docs if explicitly needed
- **Lane D / supervision**
  - Runs from repo root: `/Users/parantoux/Andy/workspace/hermes-webui`
  - Mode: Hermes review/supervision
  - Role: verify lane claims, maintain continuity, summarize progress

### Gating note
- `t_3017cbac` also wants `project-os-extension.js`, so it should **not** run in parallel with `t_5bdf7695` as an active implementation lane.
- Treat `t_3017cbac` as the **next extension lane after Lane A**, unless ownership is re-split further.

## Parallel lane model

### Lane A — Session stale-state bugfix
**Card:** `t_5bdf7695`

**Why now**
- dedicated project session 신뢰도에 직접 영향
- broad UX work보다 먼저 안정화 가치가 큼

**Recommended executor**
- Codex implementation lane
- parent Hermes verification lane

**Owned files**
- `extensions/project-os/project-os-extension.js`
- `tests/test_project_os_extension_regressions.py`

**Do not touch first**
- `api/routes.py`
- `static/ui.js`
- `static/sessions.js`
- cron/product-surface docs

**Scope**
- `refreshProjectSession`
- `sendProjectSessionPrompt`
- `updateSubmitLifecycle`
- import/resume-specific stale-running reproduction and guard logic

**Acceptance criteria**
- import/sync dispatch 후 dedicated session이 실제로 running이면 false timeout으로 떨어지지 않는다.
- `stalled_running` / `waiting` / `resolved` / `timed_out` 전이가 source-level test로 고정된다.
- unrelated host timeout banner가 보여도 project-session truth를 우선한다.
- 변화가 `project-os-extension.js` + its regression test 범위를 크게 넘지 않는다.
- targeted test command가 parent session에서도 재통과한다.

**Verification**
- `pytest -q tests/test_project_os_extension_regressions.py`
- 가능하면 import/resume path browser repro 1회

---

### Lane B — WebUI origin delivery reliability
**Card:** `t_f145cc08`

**Why now**
- cron/kanban 결과가 active WebUI thread로 자연스럽게 돌아와야 Hermes-native seamlessness가 성립함

**Recommended executor**
- Codex implementation lane
- Hermes review lane

**Owned files**
- `api/routes.py`
- related session/origin delivery tests
- 필요 시 small supporting runtime tests only

**Do not touch first**
- `extensions/project-os/project-os-extension.js`
- Project OS UI rendering details

**Acceptance criteria**
- WebUI에서 시작한 cron/worker follow-up이 same-thread 복귀 경로를 유지한다.
- `deliver=origin`의 product truth가 workaround가 아니라 실제 behavior로 검증된다.
- failure 시 silent success가 아니라 observable fallback/evidence가 남는다.
- repo continuity에서 workaround (`t_3ce742e0`)와 product fix (`t_f145cc08`)가 분리 유지된다.

**Verification**
- targeted delivery tests
- 실제 WebUI thread에서 1회 end-to-end 확인

---

### Lane C — Execution feed second slice
**Card:** `t_3017cbac`

**Status note**
- first MVP slice is already partially landed in repo-local truth:
  - Feed chip
  - Execution feed card section
  - existing artifact opener reuse

**Recommended executor**
- Codex implementation lane
- Hermes thin-layer review lane

**Owned files**
- `extensions/project-os/project-os-extension.js`
- `tests/test_project_os_extension_regressions.py`
- optional tiny CSS adjustments only if required

**Do not touch first**
- new backend endpoints
- new shadow activity storage
- `api/routes.py`
- generic session runtime

**Scope**
- existing Hermes surfaces를 더 잘 보이게 정리
- reply / artifact / session-open flow를 더 timeline-like하게 정돈
- new activity engine 금지

**Acceptance criteria**
- feed는 existing Hermes reply/artifact/session-open surfaces를 계속 재사용한다.
- new API endpoint나 shadow execution log를 만들지 않는다.
- artifact click/open flow는 existing workspace surface를 계속 탄다.
- UI는 "summary/trace visibility" 개선에 머물고 독자 run-state truth를 만들지 않는다.
- source-level regression test가 증가하거나 유지된다.

**Verification**
- `pytest -q tests/test_project_os_extension_regressions.py`
- browser QA: feed visibility + artifact open + open session path

---

### Lane D — Transparency/cell design intake
**Card:** `t_d288a08e`

**Why this is not a first Codex build lane**
- 아직은 구현보다 설계 intake가 먼저다.
- 여기서 잘못 가면 Project OS가 Hermes-native wrapper가 아니라 별도 execution UI로 커진다.

**Recommended executor**
- Hermes review/spec lane
- 필요 시 read-only Codex analysis lane

**Owned files**
- `docs/project-os/PHASE-B-PARALLEL-CARDS.md`
- `docs/project-os/PLAN.md`
- 필요 시 continuity docs

**Acceptance criteria**
- subagent transparency와 code-execution cells를 existing Hermes activity surface 위에 어떻게 매핑할지 설명한다.
- "새 subsystem을 만들지 않는 선택"이 명시된다.
- required UX, optional UX, forbidden scope가 구분된다.
- follow-up 구현 카드가 file ownership 기준으로 다시 쪼개진다.

**Required UX**
- dedicated project session reply, worker/subagent progress, and code-execution evidence should appear as a thin Project OS readout over existing Hermes session/activity surfaces.
- transparency should stay attached to the owning Hermes session/turn, kanban card, cron run, or artifact instead of becoming a parallel Project OS transcript.
- artifact visibility should keep using the existing workspace/artifact open path instead of inventing a Project OS-only artifact viewer.
- if same-thread origin delivery is not yet proven, the UI/story must keep a visible fallback path to open the underlying session or summarize the latest worker output in chat.
- approval, clarify, failed execution, and missing-evidence states must remain visible and actionable.
- settled history may collapse noisy internals by default, but must preserve an inspect path for subagent, command, artifact, and output evidence.

**Optional UX**
- compact timeline grouping or chips that make existing Hermes activity easier to scan from the Project OS panel.
- lightweight labels that distinguish dedicated project-session replies from worker/cron follow-ups without creating a separate run-state truth.
- source/status/count badges, existing-event filters, and copy/open affordances are allowed when they reuse current Hermes activity and workspace behavior.

**Forbidden scope**
- no shadow Project OS activity store, queue, or run-state ledger.
- no duplicate transcript model inside Project OS.
- no new backend execution engine or Project OS-specific scheduler/orchestrator for this slice.
- no new API endpoint just to mirror data already available through Hermes session, kanban, cron, tool, or artifact surfaces.
- no broad redesign of chat rendering, composer chrome, sidebar behavior, or workspace browsing as part of this intake lane.
- no runtime JS/API edits in this design lane; implementation follow-ups must land in owned-file child cards.

**Follow-up owned-file split**
- Project OS panel/feed presentation: `extensions/project-os/project-os-extension.js`, `tests/test_project_os_extension_regressions.py`
- Generic Hermes activity rendering polish: relevant `static/` activity UI files plus focused UI tests in a separate card.
- Artifact open-path continuity: existing workspace/artifact files plus focused tests in a separate card.
- API evidence support only when a proven field is missing: existing API module/tests, with no new Project OS storage layer.

**Verification**
- review readout
- continuity/docs alignment

---

### Lane E — Seamless execution surface umbrella design
**Card:** `t_efb7ae49`

**Role**
- execution surface의 북극성 정의
- 구현 lane이 아니라 umbrella/design lane으로 유지

**Canonical design note**
- `docs/project-os/EXECUTION-SURFACE-MVP.md`

**Design decisions fixed by this lane**
- current thread = final summary / decision-needed / explicit fallback summary surface
- Project OS panel = project-local live execution readout surface
- global Hermes activity = cross-project scan surface, not workflow truth
- canonical kanban/task detail = workflow truth and durable fallback evidence
- same-thread delivery failure must stay visible as fallback state, never silent success

**Acceptance criteria**
- kanban worker / cron / project session / artifacts / tool activity가 어떤 Hermes-native surfaces를 재사용할지 명확하다.
- replacement subsystem 금지 원칙이 명시된다.
- 구현 카드는 lane별 owned files로 다시 분리된다.

---

## Recommended execution order
1. `t_5bdf7695` — bounded bugfix
2. `t_f145cc08` — origin delivery reliability
3. `t_8dcfd135` — clean-board proof for the already-landed root-seed guardrail
4. `t_3017cbac` second slice — execution feed refinement, only after the three proof/reliability gates above stay trustworthy
5. `t_efb7ae49` — umbrella execution-surface shaping after the extension/API reliability queue settles

### Priority interpretation rule
- `t_a4c1c8c8`, `t_3017cbac`, and `t_efb7ae49` were conditional queue-follow-up cards; they are now all closed.
- `t_d288a08e` is already done and should stay closed as design guidance, not re-enter the active order.
- `t_a982118d` stays a todo/reference umbrella and must not be reported as a ready implementation lane.
- `t_132c99ea` stays in backlog reconciliation; do not let the presence of child todo cards turn it into ad-hoc execution work.

---

## Recommended cron/supervision shape
### Option A — safest
- **Cron 1:** every 10m supervision/review summary
- **Implementation lanes:** launch Codex in separate worktrees/background processes, not as overlapping edits on one checkout

### Option B — durable autonomous lanes
- **Cron per lane** with self-contained prompt
- each lane pinned to:
  - one worktree
  - one card
  - one owned-file area
  - one verify command
- one parent review cron summarizes all lanes back into the active thread

### Hard rule
If we use Codex in parallel for the remaining dev items, we should first define:
- lane ↔ card mapping
- lane ↔ worktree mapping
- lane ↔ owned files
- lane ↔ verify command

Without that, cron parallelism will increase drift faster than throughput.

---

## Parent review checklist
Before calling a lane "done", parent Hermes must verify:
1. changed files actually contain the claimed behavior
2. tests rerun in parent session pass
3. continuity docs did not drift
4. owned-file boundaries were respected
5. no new shadow subsystem slipped in

---

## Decision summary
- **Yes:** 나머지 개발 아이템도 Codex+cron 병렬화 후보가 될 수 있다.
- **But only if:** same dirty tree 직접 동시수정이 아니라, lane/worktree/ownership를 먼저 고정한다.
- **Immediate next planning truth:** the proof queue, execution-feed MVP, and seamless execution-surface design note are all closed; do not auto-thaw deferred/reference or review-required follow-ups without an explicit new bounded slice.
