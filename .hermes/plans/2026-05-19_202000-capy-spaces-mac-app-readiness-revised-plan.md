# Capy Spaces Mac App Readiness Revised Project Plan

> **For Hermes:** Use `subagent-driven-development` skill to implement this plan task-by-task after the user approves execution. Keep strict TDD for code changes and mandatory browser/visual QA for UI-visible slices.

**Goal:** Move Capy Spaces from a strong local WebUI MVP into an installable/testable Mac app candidate without prematurely claiming full Space Agent parity.

**Architecture:** Keep Hermes/Capy as the Python server + WebUI + Telegram/local-first autonomous layer. Treat a native Mac app as a wrapper/launcher around the proven local WebUI first, not a rewrite. Close the Phase 1-6 integration gaps and demo acceptance blockers before investing deeply in DMG/notarization polish.

**Tech Stack:** Python/Hermes WebUI, Flask-style API modules under `api/`, vanilla JS Spaces UI under `static/spaces.js`, pytest, Node syntax/static-JS tests, macOS LaunchAgent/service integration, later Tauri/Electron/native wrapper evaluation.

**Model Strategy:** This plan synthesizes GPT-5.5's current implementation-grounded audit with an independent Grok 4.3 architecture critique. GPT-5.5 should own implementation, tests, and verification. Grok 4.3 should be used as an outside reviewer for architecture, scope control, packaging strategy, and gate sign-off.

---

## Current Verified Baseline

Verified on 2026-05-19:

- Repo: `/Users/bschmidy10/hermes-webui`
- Branch: `feat/capy-spaces-foundation`
- Latest commit at audit: `2aaa9af feat(spaces): surface creator commit compaction`
- Live local WebUI health: `http://127.0.0.1:8787/health` returns `status: ok`
- Focused Spaces validation: `673 passed`
  - `tests/test_spaces_foundation.py`
  - `tests/test_spaces_ui_js_behaviour.py`
- Syntax validation passed:
  - `node --check static/spaces.js`
  - `python -m py_compile api/spaces.py api/routes.py api/capy_memory.py api/capy_compaction.py api/capy_policy.py api/capy_progress.py`
- Native Mac packaging artifacts currently absent:
  - no `.app`
  - no `.dmg`
  - no `Info.plist`
  - no `tauri.conf.json`
  - no Electron/package-builder config

Current readiness estimate:

- Overall product completion: ~60%
- Core roadmap MVP completion: ~70%
- Space Agent-style parity: ~50-55%
- Local WebUI test readiness: ~75%
- Native Mac app install readiness: ~5-10%

Weighted release-readiness baseline: ~51% using this blend:

- Core roadmap: 30%
- Demo parity: 30%
- Mac packaging: 20%
- QA/release stability: 20%

---

## Revised Strategy

### Principle 1: Integration before packaging

Do not start serious Mac app packaging until the key Phase 1-6 loops are demonstrably end-to-end. A native wrapper around an incomplete product will create install polish but not real readiness.

### Principle 2: Mac app starts as a launcher/wrapper, not a rewrite

The first `.app` should:

1. Ensure/launch the existing local WebUI service.
2. Open an embedded or external WebView to `http://127.0.0.1:8787`.
3. Expose health/status and quit/restart affordances.
4. Avoid changing the underlying WebUI architecture.

Defer deeper sidecar/native connectors until after the install/test loop works.

### Principle 3: Demo parity must be proven by golden flows

Metadata-only MVPs and smokes count as progress, but parity requires repeatable flows that survive reload/restart and have tests/screenshots.

### Principle 4: Dual-model workflow

Use GPT-5.5 for high-context implementation and verification. Use Grok 4.3 as an independent challenger at gates and before irreversible packaging choices.

---

## 4-6 Week Roadmap

### Week 1: Integration closure sprint A — context, preflight, compaction

**Target date:** 2026-05-26

**Target readiness:**

- Core roadmap: 78%
- Demo parity: 55%
- Mac packaging: 8%
- QA/release stability: 70%

**Focus:** Close the highest-risk Phase 2/4/5 gaps before expanding demos.

#### Slice 1.1 — Advisory memory/context preflight enforcement

**Objective:** Memory Tree context can influence creator/agent behavior only after explicit prompt-injection preflight and provenance labeling.

**Likely files:**

- Modify: `api/spaces.py`
- Modify: `api/capy_memory.py`
- Modify: `api/capy_policy.py`
- Modify: `static/spaces.js`
- Test: `tests/test_spaces_foundation.py`
- Test: `tests/test_spaces_ui_js_behaviour.py`

**Behavior to add:**

- Active-space/creator context injection includes cited Memory Tree snippets only when:
  - prompt-preflight status is pass/warn and visible,
  - source provenance is present,
  - snippets are bounded/redacted,
  - unsafe raw source, renderer, prompt, API-auth, script, and secret-like fields are omitted.
- Blocked preflight must prevent advisory context from being injected.

**Validation:**

```bash
/Users/bschmidy10/.hermes/hermes-agent/venv/bin/python -m pytest \
  tests/test_spaces_foundation.py \
  tests/test_spaces_ui_js_behaviour.py -q
node --check static/spaces.js
```

#### Slice 1.2 — Broader creator/tool/subagent/browser output compaction receipts

**Objective:** Extend `api/capy_compaction.py` beyond demo/creator receipts to product-visible long-output boundaries.

**Likely files:**

- Modify: `api/capy_compaction.py`
- Modify: `api/spaces.py`
- Modify: `api/routes.py`
- Modify: `static/spaces.js`
- Test: `tests/test_capy_output_compaction.py`
- Test: `tests/test_spaces_foundation.py`
- Test: `tests/test_spaces_ui_js_behaviour.py`

**Behavior to add:**

- Long output receipts show:
  - original length,
  - compacted length,
  - rules applied,
  - redaction status,
  - retained artifact handles/citations.
- Safety-relevant failures, approval prompts, and artifact handles must not be compacted away.

**Validation:**

```bash
/Users/bschmidy10/.hermes/hermes-agent/venv/bin/python -m pytest \
  tests/test_capy_output_compaction.py \
  tests/test_spaces_foundation.py::test_space_creator_preview* \
  tests/test_spaces_foundation.py::test_space_creator_commit* -q
```

#### Slice 1.3 — Grok architecture review gate

**Objective:** After Slices 1.1-1.2 pass, ask Grok 4.3 to critique the enforcement/compaction design before moving into demos.

**Grok prompt focus:**

- Are prompt-preflight gates actually before context influence?
- Can compaction hide unsafe or decision-critical evidence?
- Are receipts useful enough for a user to trust the system?

**Expected output:** pass / needs revision / block.

---

### Week 2: Integration closure sprint B — refresh workers and progress producers

**Target date:** 2026-06-02

**Gate 1 target:** Core roadmap reaches ~85%; local WebUI test readiness reaches ~80%.

#### Slice 2.1 — Safe source refresh scheduling/fetcher breadth

**Objective:** Consume metadata-only `source.refresh` jobs safely and reflect status in UI/progress without exposing raw content.

**Likely files:**

- Modify: `api/capy_memory.py`
- Modify: `api/capy_progress.py`
- Modify: `api/knowledge.py`
- Modify: `static/spaces.js`
- Test: `tests/test_capy_memory_tree.py`
- Test: `tests/test_spaces_foundation.py`
- Test: `tests/test_spaces_ui_js_behaviour.py`

**Behavior to add:**

- Allow-listed local/known source refresh jobs can run from queued → active → completed/failed.
- Terminal jobs can be retried safely.
- UI shows stale/error/last-run status.
- Progress emits metadata-only `memory.ingest.*` events.

#### Slice 2.2 — Progress producers for browser/development/repair workflows

**Objective:** Structured progress cards should reflect real long-running workflows, not only Research/refresh/demo paths.

**Likely files:**

- Modify: `api/capy_progress.py`
- Modify: `api/spaces.py`
- Modify: `api/routes.py`
- Modify: `static/spaces.js`
- Test: `tests/test_spaces_foundation.py`
- Test: `tests/test_spaces_ui_js_behaviour.py`

**Producer families:**

- `browser.*`
- `repair.*`
- `development.*`
- `space.visual_qa.*`
- `tool.*`
- `subagent.*`

**Safety rule:** Progress events must never persist raw prompts, generated bodies, renderer source, source URLs with credentials, exception text containing secrets, or filesystem secrets.

#### Gate 1 — Integration Gate

**Must pass before demo expansion becomes primary:**

- Focused Spaces suites green.
- New tests for preflight/context/compaction/refresh/progress green.
- Browser visual QA for product-home and Space-detail cards.
- Grok 4.3 independent review returns pass or minor revisions only.

**Gate 1 target readiness:**

- Core roadmap: 85%
- Demo parity: 58-60%
- Mac packaging: 8-10%
- QA/release stability: 75-80%

---

### Week 3: Demo parity sprint A — the top product demos

**Target date:** 2026-06-09

**Focus:** Make the demos Brendan will actually feel: Weather, Research, Kanban, Notes foundation.

#### Slice 3.1 — Weather from blank Space

**Objective:** A blank Space can produce a weather answer and persistent widget from natural language.

**Likely files:**

- Modify: `api/spaces.py`
- Modify: `api/routes.py`
- Modify: `static/spaces.js`
- Test: `tests/test_spaces_foundation.py`
- Test: `tests/test_spaces_ui_js_behaviour.py`

**Acceptance:**

- Blank Space → weather request → safe data fetch/fixture fallback → persistent weather widget.
- Survives reload/restart.
- Unsafe source/credential markers absent.

#### Slice 3.2 — Research Harness golden flow

**Objective:** Research Harness supports widget-to-agent trigger, live progress, markdown artifact, and PDF/export-ready metadata.

**Likely files:**

- Modify: `api/spaces.py`
- Modify: `api/routes.py`
- Modify: `static/spaces.js`
- Test: `tests/test_spaces_foundation.py`
- Test: `tests/test_spaces_ui_js_behaviour.py`

**Acceptance:**

- Trigger from widget.
- Progress updates visible.
- Markdown artifact persisted as metadata/file handle.
- PDF/export event queued safely.
- All outputs bounded/redacted.

#### Slice 3.3 — Kanban persistence and edit baseline

**Objective:** Kanban supports persistent columns/cards and basic edit/rename flow.

**Acceptance:**

- Add/edit/rename card metadata.
- Persistent after reload.
- Revision event created.
- Rollback can restore prior state.

---

### Week 4: Demo parity sprint B — advanced/high-risk demos

**Target date:** 2026-06-16

**Gate 2 target:** Space Agent-style parity reaches ~75%.

#### Slice 4.1 — Notes app foundation

**Objective:** Notes demo supports folders, editor state, markdown view, and attachment/image metadata persistence.

**Important scope control:** Do not build a full WYSIWYG editor if metadata-backed markdown/edit persistence proves the product loop first.

**Likely files:**

- Modify: `api/spaces.py`
- Modify: `api/routes.py`
- Modify: `static/spaces.js`
- Test: `tests/test_spaces_foundation.py`
- Test: `tests/test_spaces_ui_js_behaviour.py`

#### Slice 4.2 — Camera dashboard safe streams

**Objective:** Camera dashboard renders allowed stream/image metadata and rejects unsafe/private URLs unless explicitly approved.

**Safety:** This must be allowlist-first. No private camera URL handling without an explicit approval path.

#### Slice 4.3 — Local service / Agent Zero dashboard + browser co-control foundation

**Objective:** A local service dashboard can show health/API metadata and a browser panel/control surface without unsafe generated code execution.

**Likely files:**

- Modify: `api/spaces.py`
- Modify: `api/routes.py`
- Modify: `static/spaces.js`
- Potential new file: `api/capy_browser_surface.py` only if isolation is clearly needed.

#### Gate 2 — Demo Parity Gate

**Must pass before Mac wrapper work becomes primary:**

- Weather golden flow passes.
- Research Harness golden flow passes.
- Kanban baseline persists and rolls back.
- Notes foundation persists and reloads.
- Camera safe rules tested.
- Browser/local-service foundation has safe metadata-only contract.
- Onboarding reset works.
- Time travel/rollback works after demo edits.
- Safe admin/recovery can disable/repair broken widgets.
- Visual QA screenshots captured for key flows.
- Grok 4.3 critiques demo scope and packaging readiness.

---

### Week 5: Mac app wrapper evaluation and prototype

**Target date:** 2026-06-23

**Focus:** Build the smallest installable Mac test shell around the proven WebUI.

#### Slice 5.1 — Packaging decision spike

**Objective:** Choose Tauri, Electron, or native Swift wrapper based on lowest risk for current architecture.

**Evaluation criteria:**

- Can launch/check local `127.0.0.1:8787` WebUI.
- Can start/restart the backend service or direct users to LaunchAgent health.
- Minimal permissions.
- Straightforward `.app` build.
- Compatible with future signing/notarization.
- Does not require rewriting WebUI.

**Likely output:** `.hermes/plans/<timestamp>-mac-wrapper-decision.md`

**Grok 4.3 role:** independently evaluate tradeoffs and risks.

**GPT-5.5 role:** inspect repo, prototype candidate, validate commands.

#### Slice 5.2 — Minimal `.app` prototype

**Objective:** Create a local-only app wrapper that opens Capy WebUI and shows health/failure states.

**Possible new paths depending on wrapper choice:**

- `mac-app/`
- `mac-app/src/`
- `mac-app/README.md`
- `mac-app/Info.plist` or wrapper-generated equivalent
- `tests/test_mac_app_packaging.py` if the wrapper is script-validated

**Acceptance:**

- Build produces a `.app` locally.
- App opens WebUI.
- If WebUI is down, app shows useful recovery instructions.
- No DMG/notarization requirement yet.

---

### Week 6: Install/test hardening

**Target date:** 2026-06-30

**Gate 3 target:** Functional Mac install test candidate.

#### Slice 6.1 — Clean-user install smoke

**Objective:** Verify basic app install/launch path outside the development shell.

**Checks:**

- Copy `.app` to `/Applications` or a test Applications folder.
- Launch app.
- Verify WebUI health.
- Run one golden demo flow.
- Quit/reopen app.
- Verify persistence after restart.

#### Slice 6.2 — Release-readiness docs

**Objective:** Document what works, what is still local/dev-only, and how Brendan should test.

**Likely files:**

- Create: `mac-app/TESTING.md`
- Create or update: `.hermes/plans/capy-mac-app-test-checklist.md`
- Update: `.hermes/plans/capy-spaces-video-demo-parity-checklist.md`
- Update: `.hermes/plans/capy-spaces-space-agent-parity.md`

#### Gate 3 — Mac Test Candidate Gate

**Target readiness:**

- Core roadmap: 90%
- Demo parity: 75-80%
- Mac packaging: 35%
- QA/release stability: 80%
- Weighted release readiness: ~73%

**This is not yet a public polished app.** It is a functional Mac app test candidate.

---

## Native Mac App Readiness Plan

### Stage A — Functional wrapper

Target: Week 5-6.

Must do:

- Launch local WebUI or detect existing service.
- Show clear health status.
- Open Capy Spaces UI.
- Recover gracefully when WebUI is down.
- Preserve local-first state paths.

Do not do yet:

- Full notarization pipeline.
- Public installer polish.
- Auto-updater.
- Deep native sidecar capabilities.
- Native rewrite.

### Stage B — Installer-quality app

Target: after Gate 3.

Must add:

- DMG or pkg packaging.
- Codesigning/notarization.
- App icon, menu items, quit/restart controls.
- First-run setup screen.
- Launch-on-login optional flow.
- Uninstall/reset instructions.

### Stage C — Native sidecar/connector exploration

Target: only after Phase 1-6 + demo parity pass.

Potential additions:

- Browser co-control sidecar.
- File/camera/media permissions bridge.
- Menu bar controls.
- Local service discovery.

---

## Dual-Model Operating Model

### GPT-5.5 should own

- Implementation of code slices.
- Writing failing tests first.
- Running targeted/full validation.
- Browser/visual QA.
- Debugging regressions.
- Updating roadmap/checklist docs after each slice.
- Producing commits.

### Grok 4.3 should own

- Independent architecture critique before each gate.
- Mac wrapper tradeoff reviews.
- Risk review for prompt-injection/preflight/autonomy policy decisions.
- Scope challenge when demo work drifts into unnecessary polish.
- Final pass/fail recommendations for Gate 1, Gate 2, and Gate 3.

### Recommended gate prompts for Grok 4.3

Use `hermes-grok` with a bounded, evidence-only prompt after each gate:

```bash
hermes-grok -Q --ignore-rules -q '<gate evidence + exact question>'
```

Ask Grok to return:

- `PASS`, `PASS_WITH_MINOR_FIXES`, or `BLOCK`
- top 5 risks
- missing tests
- recommended next slice

GPT-5.5 then synthesizes Grok's critique with live test/browser evidence and performs or plans the next action.

---

## Quality Gates and Validation Commands

### Always run after code changes

```bash
cd /Users/bschmidy10/hermes-webui
node --check static/spaces.js
/Users/bschmidy10/.hermes/hermes-agent/venv/bin/python -m py_compile \
  api/spaces.py \
  api/routes.py \
  api/capy_memory.py \
  api/capy_compaction.py \
  api/capy_policy.py \
  api/capy_progress.py
/Users/bschmidy10/.hermes/hermes-agent/venv/bin/python -m pytest \
  tests/test_spaces_foundation.py \
  tests/test_spaces_ui_js_behaviour.py -q
git diff --check
```

### Run when touching Memory Tree / source refresh

```bash
/Users/bschmidy10/.hermes/hermes-agent/venv/bin/python -m pytest \
  tests/test_capy_memory_tree.py \
  tests/test_spaces_foundation.py -q
```

### Run when touching compaction

```bash
/Users/bschmidy10/.hermes/hermes-agent/venv/bin/python -m pytest \
  tests/test_capy_output_compaction.py \
  tests/test_spaces_foundation.py -q
```

### Run before declaring any UI-visible slice complete

- Browser load local WebUI.
- Capture screenshot evidence.
- Check console errors.
- Verify no unsafe fixture/source/API-auth/secret-like text appears in DOM.
- Verify responsive layout at desktop viewport.

### Run before Mac wrapper work

```bash
curl -fsS http://127.0.0.1:8787/health
/Users/bschmidy10/.hermes/hermes-agent/venv/bin/python -m pytest \
  tests/test_spaces_foundation.py \
  tests/test_spaces_ui_js_behaviour.py -q
```

---

## Non-Negotiables

1. Do not copy GPLv3 OpenHuman code, schemas, tests, comments, fixtures, or prompts.
2. Do not pivot Hermes/Capy to Rust/Tauri desktop-first architecture.
3. Do not enable arbitrary generated widget execution by default.
4. Do not let Memory Tree content bypass prompt-injection checks, approval gates, creator gates, recovery gates, or sandbox checks.
5. Do not expose raw prompts, generated bodies, renderer source, API-auth fields, credentials, unsafe screenshot paths, or secret-looking values in receipts/UI/progress events.
6. Do not start serious Mac packaging until Gate 2 is passed or explicitly waived by Brendan.
7. Keep focused Spaces suites green after every slice.
8. Every UI-visible slice ends with browser evidence and screenshot QA.
9. Every code slice follows TDD: RED → GREEN → REFACTOR → VERIFY → COMMIT.
10. Update roadmap/checklist docs after each completed slice so future cron/autonomous sprints stay aligned.

---

## Risks and Mitigations

### Risk: Scope creep from demo blockers

**Mitigation:** Treat each demo as a golden acceptance path first, not a complete app clone. For example, Notes foundation can begin with durable markdown/folder/attachment metadata before full WYSIWYG polish.

### Risk: Native wrapper hides product incompleteness

**Mitigation:** Gate Mac work behind Gate 2. First wrapper is explicitly a test candidate, not a public installer.

### Risk: Compaction hides safety-relevant evidence

**Mitigation:** Tests must prove approval prompts, failures, artifact handles, and citations survive compaction.

### Risk: Prompt-preflight becomes display-only instead of enforcement

**Mitigation:** Tests must prove blocked context cannot influence creator/agent context injection.

### Risk: Browser co-control becomes too powerful too early

**Mitigation:** Start metadata-only with inspectable action receipts. Add real click/type later behind explicit approval and recovery path.

### Risk: Full suite drift

**Mitigation:** Keep focused suites green every slice; run larger/full suite before gate closure and before packaging.

---

## Execution Recommendation

Start with Week 1 Slice 1.1: **Advisory memory/context preflight enforcement**.

Reason:

- It closes a core safety gap.
- It unlocks deeper Memory Tree usage.
- It should be validated before more demo workflows rely on advisory context.
- It is a clean TDD slice with exact backend/UI surfaces.

After Slice 1.1 and Slice 1.2, run a Grok 4.3 Gate 1 critique before continuing.
