# OpenHuman-Inspired Capy/Hermes/Spaces Roadmap Implementation Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task. This is a planning deliverable only; do not implement code from this document until Brendan asks to execute.

**Goal:** Update the Capy/Hermes/Spaces project plans and execute a clean-room roadmap that adopts the best OpenHuman patterns — Memory Tree, auto-fetch, token compression, autonomy policy, prompt-injection preflight, model routing hints, and progress events — without pivoting away from Hermes/Capy or copying GPLv3 code.

**Architecture:** Keep Hermes as the persistent autonomous gateway/tool/cron/subagent layer, keep Capy Spaces as the safe metadata-only production workspace/canvas, and add an OpenHuman-inspired local context layer around them. The new layer should ingest source records into a local memory tree, compact tool output before model context, expose freshness/provenance in WebUI/Spaces, and make autonomy/security/model-routing choices product-visible.

**Tech Stack:** Python/FastAPI-style WebUI backend, static JS/CSS WebUI, Hermes Agent Python core/config/tooling, SQLite, Markdown vault files, pytest, browser QA harnesses, existing MCP/knowledge tooling, existing Telegram/gateway/cron infrastructure.

**Source Evidence:** `/tmp/openhuman-review.md` reviewed OpenHuman commit `f9de38d6f9bc252501ef79f772b96aedf3926a4d`. Key constraints: OpenHuman local `LICENSE` is GPLv3, so this roadmap is clean-room concept adoption only; do not copy code, tests, or implementation text from OpenHuman.

---

## 0. Non-negotiable decisions

1. **No platform pivot.** Do not rewrite Hermes/Capy as Rust/Tauri and do not replace Hermes WebUI with OpenHuman.
2. **Clean-room only.** Use OpenHuman as competitive/product reference. Do not copy source code, code comments, test fixtures, rule files, or schema definitions from `/tmp/repo-eval-openhuman`.
3. **Spaces safety stays primary.** Do not weaken metadata-only widget contracts, generated-code-disabled defaults, sandbox preview gates, visual-QA gates, revision recovery, or approval gates.
4. **BYO/local-first model routing stays primary.** OpenHuman’s one-subscription backend is optional inspiration, not a replacement for Brendan’s OpenAI/xAI/LM Studio setup.
5. **Every implementation slice uses TDD + visual QA when UI-visible.** Follow `capy-spaces-development` validation patterns.
6. **Plan docs are living source-of-truth.** Update project plans before code implementation so scheduled Capy Spaces sprints choose the right next slice.

---

## 1. Plan files to update

### Source plan files

- Existing: `/Users/bschmidy10/hermes-webui/.hermes/plans/capy-spaces-space-agent-parity.md`
- Existing: `/Users/bschmidy10/hermes-webui/.hermes/plans/capy-spaces-video-demo-parity-checklist.md`
- New source-of-truth: `/Users/bschmidy10/hermes-webui/.hermes/plans/capy-openhuman-inspired-roadmap.md`
- This implementation handoff: `/Users/bschmidy10/hermes-webui/.hermes/plans/2026-05-17_123717-openhuman-inspired-capy-roadmap.md`

### Plan update objectives

- Add an **OpenHuman-inspired roadmap** section to the Space Agent parity plan.
- Make the new roadmap file the canonical strategy document for Memory Tree / auto-fetch / TokenJuice-style compression / autonomy policy / model routing / progress events.
- Add demo/checklist acceptance items for memory freshness, source ingestion, compaction evidence, autonomy indicators, and progress event panels.
- Preserve the current Space Agent parity plan’s detailed completed-slice history rather than replacing it.

---

## 2. Updated product strategy

### Product thesis

**Capy = persistent agent operating system.**

- Hermes supplies persistent identity, memory, tools, messaging surfaces, cron, subagents, and skills.
- Capy Spaces supplies a safe production workspace/canvas with metadata-only widgets, recovery, revision history, and visual QA.
- The OpenHuman-inspired layer supplies ambient context: local source ingestion, structured memory trees, auto-fetch, compaction, and product-visible trust controls.

### Differentiated positioning

Capy should not chase “desktop assistant with many OAuth cards” as the core product. It should instead become:

> **A self-hosted autonomous agent that remembers your real work, builds safe interactive Spaces around it, and can operate through Telegram/WebUI/cron while staying inspectable and locally controlled.**

### Capy advantage to preserve

- Always-on server/gateway rather than desktop-only app.
- Telegram-first remote control.
- Scheduled autonomous sprint cycles.
- Skills and self-improving procedural memory.
- Tool ecosystem including MCP, browser, terminal, computer use, GitHub, Google Workspace, etc.
- Capy Spaces safety and revision/recovery system.

### OpenHuman patterns to absorb

- Memory Tree / Obsidian-style local vault.
- Auto-fetch connectors with freshness visibility.
- TokenJuice-like tool-output compaction.
- User-visible autonomy/risk modes.
- Prompt-injection preflight for high-risk boundaries.
- Model-routing hint prefixes.
- Structured progress-event stream.

---

## 3. Roadmap phases

## Phase 0 — Plan hygiene and architecture spec

**Target duration:** 1 sprint / 1-2 days.

**Goal:** Update existing project plans and create a concrete technical architecture before any code changes.

### Task 0.1: Create canonical roadmap plan

**Objective:** Add a short canonical strategy file that scheduled sprint cycles can reference.

**Files:**
- Create: `/Users/bschmidy10/hermes-webui/.hermes/plans/capy-openhuman-inspired-roadmap.md`

**Content requirements:**
- Executive decision: adopt ideas, do not pivot.
- Phase roadmap summary.
- Clean-room/GPL constraints.
- Links to `/tmp/openhuman-review.md` as evidence, not implementation source.
- First three implementation slices recommended for scheduled sprints.

**Verification:**
- `read_file` confirms the new file exists.
- `grep`/search equivalent via `search_files` finds “clean-room” and “Memory Tree”.

### Task 0.2: Update Space Agent parity plan status

**Objective:** Append OpenHuman-inspired roadmap context without disturbing completed-slice history.

**Files:**
- Modify: `/Users/bschmidy10/hermes-webui/.hermes/plans/capy-spaces-space-agent-parity.md`

**Change:**
- Add a new top-level section near “Current Implementation Status”:
  - `## OpenHuman-Inspired Expansion Track`
  - Decision summary.
  - What to adopt.
  - What not to pivot.
  - Link to canonical roadmap file.

**Verification:**
- Existing status history remains intact.
- New section includes “Memory Tree”, “auto-fetch”, “TokenJuice-style”, “autonomy modes”, and “model routing”.

### Task 0.3: Update demo/video parity checklist

**Objective:** Add visible acceptance criteria for context/memory/autonomy improvements.

**Files:**
- Modify: `/Users/bschmidy10/hermes-webui/.hermes/plans/capy-spaces-video-demo-parity-checklist.md`

**Change:**
- Add a section: `## OpenHuman-Inspired Context Layer Demo Criteria`
- Criteria:
  - Memory freshness card visible.
  - Space detail shows relevant memory slices.
  - Run-all smoke receipt includes compaction/ingestion status where applicable.
  - Progress panel shows structured tool/subagent events.
  - Autonomy mode visible and explained.

**Verification:**
- Checklist remains a demo checklist, not a long implementation spec.

### Task 0.4: Inventory current memory/search/knowledge surfaces

**Objective:** Identify what exists before designing `capy-memory-tree`.

**Files to inspect:**
- `/Users/bschmidy10/.hermes/hermes-agent` for memory/session_search/knowledge paths.
- `/Users/bschmidy10/hermes-webui/api/` for existing Spaces/context endpoints.
- `/Users/bschmidy10/hermes-webui/static/spaces.js` for current UI surfaces.
- Any local MCP knowledge SQLite path surfaced by Capy knowledge tools.

**Deliverable:**
- Add an appendix to `capy-openhuman-inspired-roadmap.md` listing existing reusable components and gaps.

**Verification:**
- Appendix has exact candidate files/modules, not generic descriptions.

---

## Phase 1 — Capy Memory Tree design

**Target duration:** 1 week.

**Goal:** Define and implement the first local memory tree backbone as a clean-room Capy design.

### Epic 1.1: Schema and storage design

**Objective:** Define the minimum viable `capy-memory-tree` schema.

**Likely files:**
- Create: `/Users/bschmidy10/hermes-webui/docs/capy-memory-tree.md` or plan-adjacent design doc if docs directory convention differs.
- Create/modify later: backend module under `/Users/bschmidy10/hermes-webui/api/` such as `api/capy_memory.py` or a package directory if existing code style prefers it.
- Tests later: `/Users/bschmidy10/hermes-webui/tests/test_capy_memory_tree.py`

**Data model:**

- `sources`
  - `source_id`
  - `source_type`
  - `display_name`
  - `origin_uri`
  - `created_at`
  - `updated_at`
  - `last_ingested_at`
  - `freshness_status`
- `chunks`
  - `chunk_id` deterministic hash
  - `source_id`
  - `source_ref`
  - `content_path`
  - `summary`
  - `approx_tokens`
  - `created_at`
  - `lifecycle_status`: `pending`, `admitted`, `buffered`, `sealed`, `dropped`
- `entities`
  - `entity_id`
  - `label`
  - `kind`
  - `hotness_score`
- `chunk_entities`
  - `chunk_id`
  - `entity_id`
- `summary_nodes`
  - `node_id`
  - `scope`: `source`, `topic`, `global`, `space`
  - `scope_id`
  - `level`
  - `summary_path`
  - `child_refs`
  - `sealed_at`
- `jobs`
  - `job_id`
  - `kind`
  - `dedupe_key`
  - `payload_json`
  - `status`
  - `attempts`
  - `leased_until`
  - `created_at`
  - `updated_at`

**Storage layout:**

- SQLite: likely under Hermes/Capy state directory, not repo.
- Markdown vault: local user data path, not repo; design should document the exact default path.
- Avoid writing user/private data into Git-tracked directories.

**Acceptance criteria:**
- Design doc names exact table fields and indexes.
- Design doc explains provenance and redaction policy.
- Design doc explains how current Hermes memory/session_search/knowledge MCP remain compatible.

### Epic 1.2: Deterministic source canonicalization

**Objective:** Define adapters that transform inputs into safe canonical Markdown plus provenance metadata.

**First source types:**

1. `space_manifest`
2. `space_revision_event`
3. `space_widget_event`
4. `visual_qa_report`
5. `hermes_session_summary`
6. `github_issue_or_pr_metadata`
7. `telegram_thread_summary`

**Acceptance criteria:**
- No raw renderer/html/script/source/API-auth/generated-body fields enter canonical Markdown.
- Metadata keeps enough provenance to fetch original safe summaries.
- Chunks are deterministic and idempotent.

### Epic 1.3: Minimal retrieval surface

**Objective:** Expose memory search/read APIs that Spaces and Hermes tools can call.

**Likely API actions/endpoints:**

- `space.memory.ingest`
- `space.memory.search`
- `space.memory.digest`
- `space.memory.sources`
- `space.memory.freshness`

**Acceptance criteria:**
- Space detail can ask for relevant memory using `space_id` without exposing unsafe raw fields.
- Search returns chunk summaries, source metadata, and provenance references.
- Retrieval is bounded and redacted.

---

## Phase 2 — TokenJuice-style output compaction

**Target duration:** 1 week, can run partly in parallel with Phase 1 after schema is accepted.

**Goal:** Add a clean-room rule-based compaction layer for high-volume tool and Spaces outputs.

### Epic 2.1: Compaction rule spec

**Objective:** Define builtin/user/project rule layers.

**Likely files:**
- Design doc: `/Users/bschmidy10/hermes-webui/docs/capy-output-compaction.md`
- Rule examples later: `.capy/compaction-rules/` or project-local equivalent only after deciding repository convention.

**Rule model:**

- `name`
- `match.tool`
- `match.command_regex` or `match.output_type`
- `actions`
  - `drop_lines_matching`
  - `dedupe_repeated_lines`
  - `collapse_paths`
  - `cap_section_chars`
  - `preserve_error_blocks`
  - `summarize_counts`
- `safety`
  - never hide nonzero exit status
  - never hide first/last error block
  - never hide user approval prompts

**Acceptance criteria:**
- Rule spec is not copied from OpenHuman/TokenJuice.
- It has examples for pytest, npm/pnpm, git diff/status, browser console, large Spaces event lists, and cron logs.

### Epic 2.2: Backend compactor MVP

**Objective:** Implement compaction before selected outputs enter model context or UI receipts.

**Likely files:**
- Hermes Agent side: locate exact tool result processing path before implementation.
- WebUI side: `api/spaces.py` for Spaces receipt summaries, if the slice starts there.
- Tests: appropriate Hermes/WebUI test files after inventory.

**Acceptance criteria:**
- Original raw output is either not persisted or is persisted only in an explicitly bounded debug artifact outside model context.
- Compaction receipt includes `original_chars`, `compacted_chars`, and `rules_applied` where safe.
- Failure output still preserves actionable error content.

### Epic 2.3: UI evidence

**Objective:** Show compaction in product without adding debug clutter.

**Likely UI:**
- Capy Spaces detail/progress card: “Output compacted: 18k → 3k chars”.
- Cron/sprint report summary includes compaction stats when available.

**Acceptance criteria:**
- Static JS tests prove no raw unsafe fixture strings leak.
- Browser harness screenshot shows the compact status card.

---

## Phase 3 — Auto-fetch and memory freshness

**Target duration:** 1-2 weeks.

**Goal:** Turn memory ingestion from manual/scheduled one-offs into source freshness loops.

### Epic 3.1: Source registry

**Objective:** Define active/inactive memory sources with freshness status.

**Source states:**
- `not_configured`
- `connected`
- `syncing`
- `fresh`
- `stale`
- `error`
- `disabled`

**Initial sources:**
- Capy Spaces
- GitHub repo metadata through existing `gh` setup
- Hermes session summaries
- Telegram recent summaries if existing safe access path is available
- Google Workspace later, after credential/privacy review

**Acceptance criteria:**
- Registry stores metadata and status only, not credentials.
- User can disable a source.
- Freshness survives restart.

### Epic 3.2: Auto-fetch scheduler

**Objective:** Add a bounded scheduler that enqueues memory ingest jobs.

**Policy:**
- Default interval: conservative, e.g. 20-60 minutes depending source.
- Backoff on errors.
- Max concurrent source ingests.
- No destructive actions.
- No credential echo in logs.

**Acceptance criteria:**
- Unit tests cover dedupe, lease expiry, retry, disabled sources, and stale status.
- Manual trigger exists for development/QA.

### Epic 3.3: WebUI freshness panel

**Objective:** Make ambient context visible and trustworthy.

**Likely files:**
- `/Users/bschmidy10/hermes-webui/static/spaces.js`
- `/Users/bschmidy10/hermes-webui/static/spaces.css`
- WebUI API route file after inventory.
- Tests: `/Users/bschmidy10/hermes-webui/tests/test_spaces_ui_js_behaviour.py`

**UI acceptance criteria:**
- Shows source name, status, latest ingest time, chunk count, error summary if any.
- Does not show credentials, raw emails, raw messages, renderer/source/html/script fields, or secret-looking values.
- Has manual `Run ingest` for safe sources.

---

## Phase 4 — Spaces-aware memory integration

**Target duration:** 1 week.

**Goal:** Make Capy Spaces a first-class source and consumer of memory.

### Epic 4.1: Ingest Spaces artifacts

**Artifacts to ingest:**
- Space manifests, sanitized.
- Widget metadata, sanitized.
- Revision events and restore diffs, metadata-only.
- Widget events, bounded and sanitized.
- Creator preview/commit receipts.
- Recovery/admin events.
- Visual QA reports and screenshot captions.

**Acceptance criteria:**
- Every artifact has provenance back to Space ID/revision/event ID.
- Recovery-disabled or unsafe widgets are represented as quarantined metadata, not raw evidence.
- Ingestion is idempotent.

### Epic 4.2: Space detail memory panel

**Objective:** Let a Space show relevant context without model hallucination.

**UI acceptance criteria:**
- “Relevant memory” card on Space detail.
- Search/filter by source/topic/global scope.
- Shows concise summaries and provenance links.
- Offers `Digest this Space` action.
- No unsafe marker leak in hostile fixtures.

### Epic 4.3: Creator-loop memory assist

**Objective:** Feed safe memory summaries into creator-loop previews and revisions.

**Rules:**
- Memory summaries are context, not executable widget source.
- Creator preview stays non-persisted and metadata-only.
- Creator commit still requires sandbox preview, visual QA, and explicit approval.

**Acceptance criteria:**
- Tests prove memory context does not bypass creator gates.
- UI clearly labels when memory informed a draft.

---

## Phase 5 — Autonomy policy, prompt-injection preflight, and model routing

**Target duration:** 1-2 weeks.

**Goal:** Productize trust controls around the new context layer.

### Epic 5.1: Visible autonomy modes

**Modes:**

- `Supervised`
  - Ask before write/mutation/network side effects beyond safe reads.
- `SemiAutonomous`
  - Allow bounded safe reads, memory ingest, tests, and non-destructive local operations.
  - Ask for destructive, sudo, external post/send, Space creator commits, package import/export.
- `Autonomous`
  - Allow scheduled safe workflows within configured caps.
  - Still require approval for sudo, destructive external actions, credential changes, payments, and generated-code execution.

**Likely integration points:**
- Hermes config/profile layer.
- Capy WebUI settings/status surface.
- Cron job prompt scaffolding.
- Spaces creator commit gates.
- Tool permission checks.

**Acceptance criteria:**
- Current effective mode is visible in WebUI/Spaces.
- Mode changes are audited.
- Existing safety gates are stricter than or equal to mode policy.

### Epic 5.2: Prompt-injection preflight

**Objective:** Add a backend-enforced guard before high-risk prompt/tool boundaries.

**Initial guarded boundaries:**
- Creator preview/commit prompts.
- Widget runtime `capy:agent:prompt` payloads.
- Auto-fetched source text before summary/model calls.
- External web-fetch/source ingestion.
- Tool instructions that request secrets, system prompt disclosure, role override, or unsafe tool coercion.

**Logging:**
- Store category, severity, source boundary, timestamp, and prompt hash.
- Do not store raw hostile prompt text unless explicitly quarantined as raw evidence with strict recovery controls.

**Acceptance criteria:**
- False positives fail safely with fixed metadata-only messages.
- Tests include obfuscated attack strings and benign false-positive strings.
- No raw hostile strings leak to DOM/API receipts.

### Epic 5.3: Model routing hints

**Hint names:**
- `hint:fast`
- `hint:reasoning`
- `hint:vision`
- `hint:summarize`
- `hint:code`
- `hint:local`

**Initial mappings for Brendan’s setup:**
- `hint:reasoning`: current Hermes OpenAI GPT-5.5 profile.
- `hint:fast`: cheaper/faster configured provider if available; otherwise current default.
- `hint:summarize`: strong but cost-aware model, possibly local LM Studio for low-risk summaries after quality checks.
- `hint:code`: current code-capable provider.
- `hint:vision`: existing browser/vision tool path.
- `hint:local`: LM Studio profile when explicitly safe.

**Acceptance criteria:**
- Hints resolve through config, not hardcoded model names in product code.
- Unknown hints fail or fallback in a documented way.
- Logs show resolved provider/model without exposing keys.

---

## Phase 6 — Structured progress events

**Target duration:** 1 week.

**Goal:** Make agent/subagent/tool progress visible in WebUI and Spaces using a stable event schema.

### Event taxonomy

- `run.started`
- `run.completed`
- `run.failed`
- `thinking.delta`
- `text.delta`
- `tool.started`
- `tool.args.delta`
- `tool.completed`
- `tool.failed`
- `subagent.spawned`
- `subagent.progress`
- `subagent.completed`
- `taskboard.updated`
- `memory.ingest.started`
- `memory.ingest.completed`
- `memory.ingest.failed`
- `space.visual_qa.started`
- `space.visual_qa.completed`

### Acceptance criteria

- Event payloads are bounded and metadata-only by default.
- Tool results pass through compaction before event display.
- Spaces can render a progress panel from this event stream.
- Scheduled sprint reports can cite event receipts and screenshots.

---

## Phase 7 — Optional integration catalog / sidecar exploration

**Target duration:** after Phases 1-6, not before.

**Goal:** Decide whether to add a broad OAuth/integration catalog or OpenHuman sidecar bridge.

### Decision gates

Do not proceed until:

- Memory tree MVP works with existing sources.
- Token compaction is live.
- Autonomy policy is visible.
- Security review of third-party token custody is complete.
- GPL process-boundary rules are documented.

### Options

1. **Native-only connectors**
   - Lowest vendor risk.
   - Slower breadth.

2. **Composio optional connector layer**
   - Fast breadth and OAuth convenience.
   - Requires privacy/cost/vendor review.

3. **OpenHuman sidecar bridge**
   - Treat OpenHuman as separate GPL process.
   - Bridge only exported memory/status APIs.
   - Not recommended until auth/socket/network concerns are resolved or isolated.

### Acceptance criteria

- Written decision memo before implementation.
- No code copied from OpenHuman.
- User can disable any connector.

---

## 4. Execution sequencing

### First 5 implementation slices after plan approval

1. **Plan docs update slice**
   - Create canonical roadmap file.
   - Patch Space Agent parity plan.
   - Patch video demo checklist.
   - Validation: read/search only.

2. **Memory Tree design doc slice**
   - Inventory existing memory/search surfaces.
   - Write schema/storage/job design.
   - No production code yet.

3. **Spaces artifact ingester RED/GREEN slice**
   - Add tests for deterministic sanitized Space artifact canonicalization.
   - Implement minimal canonicalizer and chunk ID function.

4. **Compaction MVP RED/GREEN slice**
   - Add tests for pytest/git/browser/Spaces compaction cases.
   - Implement safe reducer and stats receipt.

5. **Memory freshness panel UI slice**
   - Add static JS fake-DOM tests and backend mock route.
   - Implement metadata-only source freshness card.
   - Run browser harness and screenshot QA.

---

## 5. Cross-cutting validation bundle

### Backend validation

Run targeted tests first, then broader suites when touching shared Spaces paths:

```bash
cd /Users/bschmidy10/hermes-webui
/Users/bschmidy10/.hermes/hermes-agent/venv/bin/python -m pytest tests/test_spaces_foundation.py -q -o 'addopts='
/Users/bschmidy10/.hermes/hermes-agent/venv/bin/python -m pytest tests/test_spaces_ui_js_behaviour.py tests/test_spaces_demo_parity.py -q -o 'addopts='
/Users/bschmidy10/.hermes/hermes-agent/venv/bin/python -m py_compile api/spaces.py api/routes.py
node --check static/spaces.js
git diff --check
```

### New memory/compaction tests

Add as implementation creates files:

```bash
/Users/bschmidy10/.hermes/hermes-agent/venv/bin/python -m pytest tests/test_capy_memory_tree.py -q -o 'addopts='
/Users/bschmidy10/.hermes/hermes-agent/venv/bin/python -m pytest tests/test_capy_output_compaction.py -q -o 'addopts='
```

### Browser/visual QA

For UI-visible changes:

- Use live WebUI when auth/flags allow.
- Otherwise create `/tmp/capy-spaces-progress/<slice>-harness/index.html` loading actual checked-out `static/spaces.js`, `static/style.css`, and `static/spaces.css`.
- Capture screenshot and console status.
- Final sprint report must include visual/UI QA result with screenshot evidence.

### Safety regression checks

Every slice touching memory, Spaces, or UI must include hostile fixtures for:

- `renderer`
- `source`
- `html`
- `<script>`
- `api_key`
- `api_auth`
- bearer/secret-looking strings
- raw prompt / generated body markers

Expected: public API receipts and DOM do not expose these values, except where raw evidence is intentionally quarantined in trusted backend-only storage and never shown publicly.

---

## 6. Risks and mitigations

### Risk: Scope explosion

**Mitigation:** Ship in vertical slices. Do not build 118 integrations. Start with Spaces + GitHub + Hermes sessions.

### Risk: Memory tree becomes another untrusted prompt-injection vector

**Mitigation:** Treat all ingested source text as untrusted. Store provenance. Summaries are advisory. High-risk actions still require policy gates.

### Risk: Token compaction hides important failures

**Mitigation:** Preserve exit status, first/last error blocks, tracebacks, and approval prompts. Record compaction stats.

### Risk: GPL contamination

**Mitigation:** Do not copy OpenHuman code/tests/schemas. Use only high-level product ideas documented in our own report. Keep `/tmp/repo-eval-openhuman` as reference evidence, not a source file dependency.

### Risk: UI turns into debug dashboard

**Mitigation:** Product surfaces show concise cards: freshness, relevant memory, autonomy mode, progress. Detailed debug only behind admin/recovery controls.

### Risk: Model routing surprises user

**Mitigation:** Show resolved route in receipts/settings; keep override controls; default to current Hermes provider until explicit configuration exists.

---

## 7. Definition of done for the full roadmap

The OpenHuman-inspired roadmap is complete when:

- Project plans explicitly reflect the decision to adopt patterns but not pivot.
- Capy has a local memory tree MVP with SQLite metadata and Markdown/provenance store.
- Capy Spaces artifacts ingest into memory idempotently and safely.
- WebUI shows memory source freshness and relevant memory in Space detail.
- Tool/Spaces outputs pass through a configurable compaction layer before model context.
- Autonomy/risk mode is visible and actually gates key side effects.
- Prompt-injection preflight protects high-risk boundaries with hashed/non-raw logs.
- Model routing hints resolve through config.
- Progress events power a visible Spaces/WebUI progress panel.
- Scheduled Capy Spaces sprint reports include memory/progress/autonomy status where relevant.
- No GPL code copied from OpenHuman.

---

## 8. Handoff note

Plan complete. Recommended next execution request:

> “Execute Phase 0 of `/Users/bschmidy10/hermes-webui/.hermes/plans/2026-05-17_123717-openhuman-inspired-capy-roadmap.md` using subagent-driven-development.”

Phase 0 is intentionally plan-doc-only and low risk. It should be done before implementation sprints so scheduled Capy Spaces work chooses roadmap-aligned slices.
