# Capy OpenHuman-Inspired Roadmap

> **For Hermes:** Use this as the canonical strategy file for OpenHuman-inspired Capy/Hermes/Spaces work. Execute it with `subagent-driven-development` task-by-task, with strict TDD for code changes and visual/browser QA for UI-visible slices.

**Decision:** Adopt selected OpenHuman product/architecture ideas clean-room. Do **not** pivot to OpenHuman, do **not** rewrite Hermes/Capy as Rust/Tauri, and do **not** copy GPLv3 OpenHuman code, schemas, tests, comments, or fixtures.

**Goal:** Extend Hermes + Capy Spaces with a local context operating layer: Memory Tree, auto-fetch/freshness, output compaction, source provenance, autonomy/security policy visibility, model-routing hints, and structured progress events.

**Architecture:** Hermes remains the persistent autonomous gateway/tool/cron/subagent layer. Capy Spaces remains the safe metadata-only production workspace/canvas with recovery, revision history, and visual QA. The new context layer is additive: it ingests trusted summaries of local/remote work into local storage and exposes bounded, redacted, cited context back to Spaces and Hermes.

**Primary evidence:** `/tmp/openhuman-review.md` and `.hermes/plans/2026-05-17_123717-openhuman-inspired-capy-roadmap.md` summarize the OpenHuman review at commit `f9de38d6f9bc252501ef79f772b96aedf3926a4d`. Treat those files as evidence and design input, not implementation source.

---

## Non-negotiables

1. **Clean-room only:** No copying from GPLv3 OpenHuman source, tests, schemas, prompts, or comments.
2. **No platform pivot:** Hermes/Capy stays Python/server/WebUI/Telegram/local-first, not Rust/Tauri desktop-first.
3. **Spaces safety stays primary:** Generated widget execution remains disabled by default. Metadata-only rendering, sandbox preview, visual QA, approval gates, and revision rollback remain required.
4. **Memory is untrusted context:** Retrieved or auto-fetched memory can inform the agent, but cannot bypass prompt-injection checks, creator-loop gates, sandbox checks, approval gates, or recovery quarantine.
5. **Local-first storage:** User/private source records and memory-tree vault content live in local state paths, not Git-tracked repo directories.
6. **Visible trust controls:** Autonomy mode, freshness, provenance, model-routing hints, prompt-injection preflight status, and progress events should be visible in the product UI where relevant.

---

## Phase roadmap summary

### Phase 0 — Plan hygiene and architecture spec

Status: **complete**.

Delivered:
- This canonical roadmap file exists and is referenced by scheduled Capy Spaces sprints.
- `capy-spaces-space-agent-parity.md` includes the OpenHuman-inspired expansion track.
- `capy-spaces-video-demo-parity-checklist.md` includes visible context-layer demo criteria.
- The appendix below inventories reusable components and current gaps.

### Phase 1 — Capy Memory Tree design/MVP

Status: **implemented as a local-first MVP; continue hardening ingestion automation**.

Delivered:
- `docs/capy-memory-tree.md` defines the clean-room schema, storage, provenance, redaction, and TDD plan.
- `api/capy_memory.py` implements deterministic source/chunk canonicalization, SQLite tables, redacted Markdown vault writes, status/search APIs, relevant-memory lookup, and metadata-only source refresh-job registration.
- `tests/test_capy_memory_tree.py` covers hostile Space manifests, revision events, widget events, visual-QA reports, storage/search/status routes, source refresh registration, deterministic IDs, bounded traversal, and leak prevention.

Remaining:
- Wire more live Spaces artifacts and local-knowledge sources into automatic `canonicalize_*` + `ingest_source(...)` flows instead of relying on direct helper/API calls.
- Keep all memory as advisory untrusted context that cannot bypass creator, approval, recovery, or prompt-injection gates.

### Phase 2 — TokenJuice-style output compaction

Status: **backend helper and run-all demo receipt implemented; broader execution-path integration remains**.

Delivered:
- `api/capy_compaction.py` implements `compact_output(...)` with bounded receipts, unsafe-marker redaction, path collapsing, repeated-line dedupe, approval-prompt preservation, error-block preservation, invalid-cap rejection, and `rules_applied` metadata.
- `tests/test_capy_output_compaction.py` verifies size accounting, error/approval preservation, redaction status, cap enforcement, and unsafe-marker omission.
- `space_demo_run_all()` now emits a metadata-only `output_compaction` receipt built from allow-listed demo-suite summary lines, so the existing Spaces UI can show original/compacted character counts, redaction status, and allow-listed rules without rendering raw output, prompts, widget bodies, or credentials.

Remaining:
- Extend compaction receipts beyond the run-all demo suite to individual long creator/tool/subagent/browser-output boundaries where they add product value.
- Preserve safety-relevant prompts, failures, approval prompts, and artifact handles/citations when broader execution paths adopt compaction.

### Phase 3 — Auto-fetch source registry and freshness

Status: **source registry/status UI implemented; actual refresh workers remain**.

Delivered:
- `api/capy_memory.py` registers source references idempotently, queues metadata-only `source.refresh` jobs, strips credential/query/fragment markers from public `origin_uri`, requeues terminal jobs, and preserves active leased payloads.
- `GET /api/capy-memory/status` returns local-only source/chunk/stale/error/refresh-job counts.
- `static/spaces.js` renders the product-home Memory freshness card from `api/capy-memory/status` with hostile fields ignored/redacted.

Remaining:
- Implement a safe refresh worker that consumes queued jobs, fetches selected sources under allow-listed policy, writes only sanitized summaries, and updates freshness/error metadata.
- Bridge existing local knowledge sources into Memory Tree source records with provenance and freshness metadata.

### Phase 4 — Spaces-aware memory integration

Status: **Space detail and creator preview surfaces implemented; deeper automatic context wiring remains**.

Delivered:
- `GET /api/spaces/memory?space_id=...` returns bounded relevant Memory Tree slices with metadata-only snippets.
- `static/spaces.js` renders a Space detail `Memory Tree context` card and creator-preview `Memory assist` evidence without leaking renderer/source/API-auth/raw-prompt markers.
- New artifact canonicalizers cover Space manifests, revision events, widget events, and visual/UI QA reports as searchable local artifacts.

Remaining:
- Automatically ingest revision/rollback/repair/widget-event/visual-QA artifacts at their production boundaries.
- Inject cited relevant memory into active-space/creator context only after prompt-injection preflight and with explicit redaction/provenance labels.

### Phase 5 — Autonomy policy, prompt-injection preflight, model-routing hints

Status: **metadata-only status surface implemented; per-action enforcement remains**.

Delivered:
- `api/capy_policy.py` exposes allow-listed autonomy modes, approval gates, prompt-preflight status, and model-routing hints without echoing raw env/provider/model secrets.
- `static/spaces.js` renders the product-home Autonomy policy card from `api/capy-policy/status`.

Remaining:
- Surface pass/warn/block preflight receipts on high-risk creator/source/tool boundaries.
- Wire model-routing hints into actual Capy/Hermes execution decisions while preserving Brendan's provider-agnostic OpenAI/xAI/LM Studio setup.

### Phase 6 — Structured progress events

Status: **recorder/status/product-home and Space-detail cards implemented; broader producer coverage remains**.

Delivered:
- `api/capy_progress.py` records and summarizes bounded metadata-only progress events.
- `static/spaces.js` renders product-home Progress events stats, family-count chips, recent event types, timestamps, and recent stream rows without raw payload/prompt leakage.
- Space detail now loads `/api/capy-progress/status?space_id=...` and renders a Space-scoped progress card next to Memory Tree context/revision history, showing bounded active-run/recent-event metadata without unsafe event families, generated bodies, renderer/source fields, raw prompts, or credentials.
- Recent progress family counts cover conservative event families such as `run`, `tool`, `subagent`, `taskboard`, `memory.ingest`, and `space.visual_qa`.
- Research Harness progress updates now emit metadata-only structured progress events so product-home progress status can reflect real research workflow activity without storing raw prompts, source text, renderer fields, or credentials.
- Creator-loop commits that pass sandbox preview, visual QA, and explicit approval now emit `space.visual_qa.completed` progress events, giving the product-home stream real visual-QA gate producer coverage without storing prompts, generated bodies, renderers, sources, or credentials.
- Source refresh workers now emit metadata-only `memory.ingest.started` / `memory.ingest.completed` / `memory.ingest.failed` progress events from opaque source/job run ids, so Memory Tree ingest activity appears in the product progress stream without storing origin URLs, raw summaries, prompts, renderer fields, exception text, or credentials.

Remaining:
- Emit progress events from more real long-running browser, development, repair, and creator workflows.

### Phase 7 — Optional integration catalog/sidecar exploration

Status: **not started; hold until remaining Phase 1-6 integration work is proven end-to-end**.

Only after the remaining Phase 1-6 integration items are working:
- Evaluate connector catalog UX.
- Explore sidecar/native app integration only if it clearly improves Capy's existing self-hosted server product.

---

## Next implementation slices for autonomous sprints

1. **Local knowledge bridge into Memory Tree**
   - Register existing local knowledge sources as Memory Tree source records with provenance/freshness metadata rather than copying raw files into prompts.
   - Keep source status metadata-only and local-first.

2. **Prompt-preflight + memory/context enforcement**
   - Add per-action pass/warn/block receipts for creator/source boundaries before memory can influence agent actions.
   - Treat Memory Tree content as untrusted advisory context.

3. **Broader compaction producers**
   - Extend the run-all `output_compaction` receipt pattern to individual long creator/tool/subagent/browser-output boundaries where it adds product value.
   - Preserve failures, approval prompts, and artifact handles/citations; never compact away safety-relevant evidence.

4. **Progress producer expansion**
   - Record structured events from browser/development/repair flows so the product-home and Space-detail streams reflect real autonomous work. Research Harness progress updates, creator visual-QA commit gates, and Memory Tree source-refresh workers now cover the first workflow/gate/ingest producers.

---

## Current reusable components inventory

### Hermes core memory/session components

- `/Users/bschmidy10/.hermes/hermes-agent/agent/memory_manager.py`
  - Existing persistent memory injection/management surface.
  - Reuse conceptually for memory lifecycle boundaries; do not overload durable user/agent memory with bulky source records.
- `/Users/bschmidy10/.hermes/hermes-agent/tools/memory_tool.py`
  - User/agent memory CRUD tool; useful redaction/behavior reference for what should **not** become bulky task logs.
- `/Users/bschmidy10/.hermes/hermes-agent/tools/session_search_tool.py`
  - Existing searchable session-history recall tool; Memory Tree should complement this, not replace it.
- `/Users/bschmidy10/.hermes/hermes-agent/hermes_state.py`
  - SQLite session store and FTS search foundation.
- `/Users/bschmidy10/.hermes/hermes-agent/cron/scheduler.py`
  - Existing autonomous recurring execution; future auto-fetch/freshness jobs can integrate here or with WebUI-local jobs.
- `/Users/bschmidy10/.hermes/hermes-agent/plugins/memory/`
  - Optional external memory-provider plugins. Capy Memory Tree should remain local-first and provider-agnostic.

### Capy WebUI local knowledge surfaces

- `/Users/bschmidy10/hermes-webui/api/knowledge.py`
  - Local-only knowledge adapter with status/search/read/ask/capture note helpers, source redaction, and Obsidian URL support.
  - Strong candidate for integration or reuse when implementing Memory Tree search/read and freshness cards.
- `/Users/bschmidy10/hermes-webui/api/routes.py`
  - Existing routes: `/api/knowledge/status`, `/api/knowledge/search`, `/api/knowledge/read`, `/api/knowledge/ask`, `/api/memory`, and rollback/checkpoint routes.
  - Existing model/provider context handling around session load paths can inform model-routing hints.
- Local knowledge default paths from `api/knowledge.py`:
  - `~/.hermes/local-knowledge`
  - `~/.hermes/local-knowledge/index_config.json`
  - Obsidian vault default `~/Documents/Obsidian Vault`

### Capy Spaces context/revision/progress surfaces

- `/Users/bschmidy10/hermes-webui/api/spaces.py`
  - `build_agent_context(space_id)` already builds compact active-Space prompt context.
  - `queue_widget_event(...)` and `list_widget_events(...)` provide widget-event history.
  - `list_revision_events(...)`, restore helpers, checkpoint/recovery helpers, and creator preview/commit receipts provide revision/provenance anchors.
  - `set_research_progress(...)` already updates Research Harness progress metadata.
- `/Users/bschmidy10/hermes-webui/api/routes.py`
  - Existing Spaces routes include `/api/spaces/revisions`, `/api/spaces/widget/events`, creator/recovery/research routes, and local rollback endpoints.
- `/Users/bschmidy10/hermes-webui/static/spaces.js`
  - Existing rendering for creator previews, revision history, recovery/admin, widget events, smoke receipts, and Space detail canvas.
  - Future Memory Tree/freshness/relevant-memory cards should load the actual checked-out file in real-static tests and browser harnesses.
- `/Users/bschmidy10/hermes-webui/tests/test_spaces_foundation.py`
  - Main backend regression surface for Spaces metadata safety and route/tool contracts.
- `/Users/bschmidy10/hermes-webui/tests/test_spaces_ui_js_behaviour.py`
  - Main real-`static/spaces.js` fake-DOM regression surface for UI safety and behavior.

### Gaps to close / next integration targets

The first roadmap pass has now delivered the Memory Tree module, source/job schema, compaction helper, memory freshness card, relevant-memory UI, policy card, and product-home plus Space-detail progress cards. Current remaining work is narrower:

- **Automated artifact ingestion:** Space manifests, revision/rollback/repair events, widget events, and visual-QA reports have canonicalizers, but more production boundaries still need to call `canonicalize_*` + `ingest_source(...)` automatically.
- **Local knowledge bridge:** Local knowledge remains file-index/search oriented and should be registered as Memory Tree source records with freshness/provenance rather than being copied wholesale into prompts.
- **Advisory active context:** Active-Space context has display-side relevant memory and compact summaries, but agent/creator context injection should add cited Memory Tree snippets only after prompt-injection preflight.
- **Compaction execution path:** `api/capy_compaction.py` exists, but long tool/subagent/browser-output boundaries still need product-visible compaction receipts and integration.
- **Source refresh worker:** Metadata-only `source.refresh` jobs can now be consumed safely and emit progress events; remaining work is broadening production scheduling/trigger coverage and source-specific fetchers.
- **Per-action policy/preflight:** Product-home policy visibility exists; creator/source/runtime boundaries still need explicit pass/warn/block receipts where high-risk context can influence actions.
- **Progress producers:** Structured progress recording/status/card exist, and Research Harness progress updates, creator visual-QA commits, and Memory Tree source-refresh ingest now emit events; more real long-running browser/development/repair workflows should emit events.

---

## Demo acceptance additions

A roadmap slice is not complete until the product can show bounded evidence, not just backend state:

- Memory freshness card: source counts, stale counts, last run status, and local-only indicator.
- Relevant memory panel: cited, redacted memory snippets attached to a Space or creator preview.
- Compaction evidence: receipt showing original size, compacted size, retained artifact handles/citations, and redaction status.
- Autonomy mode: visible mode label and explanation for the current action/Space.
- Prompt-injection preflight: visible pass/block/warn status for high-risk ingested content.
- Progress panel: structured tool/subagent/creator events with bounded metadata and no raw prompt or secret-like leakage.

---

## Validation defaults

- Plan-doc-only slices: `read_file`, `search_files`, and `git diff --check`; commit plan files only.
- Backend code slices: strict RED/GREEN pytest, `py_compile`, targeted full regression subset, `git diff --check`.
- Frontend-visible slices: real-`static/spaces.js` tests, `node --check static/spaces.js`, browser console/leak check, screenshot, and Visual/UI QA.
- Production WebUI changes: restart `com.capy.webui` and verify local/tailnet health after commit.
