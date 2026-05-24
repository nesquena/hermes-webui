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

Status: **implemented as a local-first MVP; continue hardening advisory/context enforcement**.

Delivered:
- `docs/capy-memory-tree.md` defines the clean-room schema, storage, provenance, redaction, and TDD plan.
- `api/capy_memory.py` implements deterministic source/chunk canonicalization, SQLite tables, redacted Markdown vault writes, status/search APIs, relevant-memory lookup, and metadata-only source refresh-job registration.
- `tests/test_capy_memory_tree.py` covers hostile Space manifests, revision events, widget events, visual-QA reports, storage/search/status routes, source refresh registration, deterministic IDs, bounded traversal, and leak prevention.
- Live Spaces boundaries now auto-ingest metadata-only Space manifests/revision events, widget events, repair events, rollback/restore revision anchors, and creator visual-QA reports into the Memory Tree. Local knowledge sources are registered as source records with provenance/freshness metadata rather than copied into prompts.

Remaining:
- Keep all memory as advisory untrusted context that cannot bypass creator, approval, recovery, or prompt-injection gates.

### Phase 2 — TokenJuice-style output compaction

Status: **backend helper and product-visible metadata receipts implemented for the first creator/context/demo/progress boundaries; broader high-volume tool/subagent/browser/development integration remains**.

Delivered:
- `api/capy_compaction.py` implements `compact_output(...)` with bounded receipts, unsafe-marker redaction, path collapsing, repeated-line dedupe, approval-prompt preservation, error-block preservation, invalid-cap rejection, and `rules_applied` metadata.
- `tests/test_capy_output_compaction.py` verifies size accounting, error/approval preservation, redaction status, cap enforcement, and unsafe-marker omission.
- `space_demo_run_all()` now emits a metadata-only `output_compaction` receipt built from allow-listed demo-suite summary lines, so the existing Spaces UI can show original/compacted character counts, redaction status, and allow-listed rules without rendering raw output, prompts, widget bodies, or credentials.
- Individual demo smokes, creator preview/commit receipts, active-space context receipts, and scoped progress status receipts now reuse the same product-visible compaction evidence pattern without rendering raw prompts, generated bodies, renderer/source/API-auth fields, scripts, credentials, or exception text.

Remaining:
- Extend compaction receipts to remaining long external tool/subagent/browser/development/recovery-output boundaries where they add product value.
- Preserve safety-relevant prompts, failures, approval prompts, and artifact handles/citations when broader execution paths adopt compaction.

### Phase 3 — Auto-fetch source registry and freshness

Status: **source registry/status UI, local-knowledge bridge, source-refresh job queueing, and safe metadata-only refresh worker implemented; broader autonomous scheduling/trigger coverage and source-specific fetcher breadth remain**.

Delivered:
- `api/capy_memory.py` registers source references idempotently, queues metadata-only `source.refresh` jobs, strips credential/query/fragment markers from public `origin_uri`, requeues terminal jobs, and preserves active leased payloads.
- `run_source_refresh_jobs(...)` leases queued source-refresh jobs, enforces allow-listed HTTP(S) origins, runs `auto_fetched_source` prompt preflight, persists only sanitized advisory summaries, and emits metadata-only `memory.ingest.*` progress events; the manual Memory refresh trigger now returns a metadata-only action-policy receipt with destructive-external-action approval and `hint:summarize` routing evidence.
- `GET /api/capy-memory/status` returns local-only source/chunk/stale/error/refresh-job counts.
- `static/spaces.js` renders the product-home Memory freshness card from `api/capy-memory/status` with hostile fields ignored/redacted.
- `api/knowledge.py` / Memory Tree bridge registers local knowledge sources as metadata-only Memory Tree source records with local provenance/freshness status.

Remaining:
- Broaden scheduler/cron coverage for due refresh jobs and source-specific fetchers while keeping existing manual-trigger receipts and all public UI metadata-only.

### Phase 4 — Spaces-aware memory integration

Status: **Space detail, creator preview, production artifact auto-ingest, and active-space context advisory/preflight surfaces implemented; broader advisory enforcement remains**.

Delivered:
- `GET /api/spaces/memory?space_id=...` returns bounded relevant Memory Tree slices with metadata-only snippets.
- `static/spaces.js` renders a Space detail `Memory Tree context` card and creator-preview `Memory assist` evidence without leaking renderer/source/API-auth/raw-prompt markers.
- New artifact canonicalizers cover Space manifests, revision events, widget events, and visual/UI QA reports as searchable local artifacts.
- Production boundaries now call the canonicalizers automatically for Space manifest/revision writes (including rollback/restore anchors), widget events, recovery/repair events, and creator visual-QA commit reports.
- Active-space context now includes cited advisory relevant-memory slices only through a prompt-preflighted, metadata-only envelope with compaction, context status, progress-event, and action-policy evidence.

Remaining:
- Extend the same preflighted advisory-memory envelope to remaining source/tool/browser/development boundaries where memory can influence actions.

### Phase 5 — Autonomy policy, prompt-injection preflight, model-routing hints

Status: **metadata-only status surface plus creator preview/commit, active-space instruction/context, repair prompt, source-refresh preflight, camera-stream tool receipts, package policy receipts, and safe model-route resolution receipts implemented; broader per-action enforcement and actual model invocation routing remain**.

Delivered:
- `api/capy_policy.py` exposes allow-listed autonomy modes, approval gates, prompt-preflight status, model-routing hints, and bounded/deduplicated action-policy receipts without echoing raw env/provider/model secrets.
- `static/spaces.js` renders the product-home Autonomy policy card from `api/capy-policy/status`.
- Creator preview responses now include a metadata-only `autonomy_policy` receipt with approval gates, prompt-preflight status, and model-route hint evidence, and the creator-preview UI renders this Action policy card without exposing raw prompts, generated bodies, renderer/source fields, API-auth fields, or credentials.
- Source-style layout repair (`space.spaces.repairLayout`) now returns metadata-only action-policy evidence beside its structured repair progress receipt, so automated repair boundaries expose approval/preflight/model-route safety metadata without raw layout/source/renderer leakage.
- Creator commit responses now carry forward the preview prompt-preflight receipt and add a commit-scoped `autonomy_policy` receipt after the sandbox preview, visual-QA, and explicit approval gates pass, giving the revisioned-commit result metadata-only policy evidence without persisting raw prompts, generated bodies, renderer/source fields, API-auth fields, credentials, or unsafe screenshot paths.
- Direct active-space instruction aliases (`space.current.agentInstructions` / `space.current.specialInstructions`) now run `active_space_instructions` prompt preflight, return metadata-only action-policy receipts, and withhold hostile instruction text before it can be injected into agent context.
- Space metadata mutation aliases (`space.spaces.saveSpaceMeta` / `space.current.saveMeta`) now preflight `agentInstructions`/`specialInstructions` before persistence, block hostile instruction writes without creating revisions, and return metadata-only action-policy receipts for passing instruction updates.
- Camera-stream tool actions (`space.camera.add_stream` / `camera.add_stream`) now return metadata-only autonomy-policy receipts requiring prompt preflight and the `destructive_external_action` gate with a `hint:vision` model-route hint, while approved private stream references remain url-digest/host-class metadata only.
- Shared data slot delete tool actions (`space.data.delete` / `space.current.data.delete`) now return metadata-only `space.shared_slot.delete` autonomy-policy receipts with creator-commit approval, required prompt-preflight status, and `hint:summarize` route evidence while preserving existing redacted progress telemetry.
- Space Agent package export responses now include a metadata-only `space.agent.export` autonomy-policy receipt with supervised-mode approval gates, required prompt-preflight status, and `hint:reasoning` model-route evidence; the export UI renders the policy beside package progress without displaying package YAML, widget bodies, renderer/source/API-auth fields, prompts, scripts, archives, or credentials.
- Space Agent package import tool aliases now return per-invocation metadata-only autonomy-policy receipts, so `space.import`, `space.package.import`, and `space.agent.import` can be distinguished in product/tool evidence while preserving prompt-preflight status, approval gates, model-route hints, and import quarantine redaction.
- Action-policy receipts now include a metadata-only `model_route_resolution` decision derived from safe configured route fields or deterministic default fallbacks, and Spaces UI receipts render the selected route plus fallback reason without exposing raw provider config, API-auth fields, renderer/source/script markers, prompts, or credentials.
- Active-space context receipts, repair prompts and empty-prompt whole-Space/widget/module repair queues, widget-runtime prompts (including direct sandbox queue-status UI receipts), source-style widget definition/blueprint preview/source helpers, and source-refresh ingestion paths now use protected prompt-preflight boundaries before advisory context or user-provided instructions can influence an agent/tool action.

Remaining:
- Extend pass/warn/block preflight and action-policy receipts to additional high-risk browser/development/recovery/tool boundaries beyond the current creator/context/repair/source-refresh/camera/package-export coverage.
- Wire model-routing resolution into actual Capy/Hermes invocation selection while preserving Brendan's provider-agnostic OpenAI/xAI/LM Studio setup.

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
- The metadata-only Space demo smoke suite now emits bounded `run.started` / `run.completed` progress events under a fixed safe run id, so demo-suite compaction/context evidence also appears in the product progress stream without storing raw demo output, prompts, widget bodies, renderers, sources, or credentials.
- Individual Browser Surface demo smoke runs now emit metadata-only `run.started` / `run.completed` / `run.failed` progress events under safe `space-demo:<demo>` run ids, so the product progress stream reflects targeted browser parity smokes without storing raw browser output, prompts, widget bodies, renderer/source fields, exception text, or credentials.
- Source-style `space.spaces.repairLayout` now emits metadata-only `tool.completed` progress events with Space-scoped run ids and fallback-safe receipts, giving the progress stream its first direct layout-repair producer without exposing renderer/source/API-auth fields, prompts, script markers, or exception text.
- Creator-loop sandbox previews now emit metadata-only `tool.completed` progress events keyed by their opaque preview receipt ids, so the product progress stream reflects creator preview activity before commit without storing raw prompts, generated widget bodies, renderer/source fields, API-auth data, script markers, or credentials.
- Shared data slot set/delete tool actions now emit metadata-only `tool.completed` progress events using safe `shared-slot.*` run ids, so Space cooperation/data handoff activity appears in Space-scoped progress without exposing slot values, renderer/source fields, API-auth fields, prompts, or credentials.
- Source-style widget upsert, renderWidget, defineWidget, and widget-blueprint preview/source tool actions now emit metadata-only `tool.completed` progress events with safe `widget.upsert:<space_id>`, `widget.render:<space_id>`, `widget.blueprint.define:<space_id>`, `widget.blueprint.create:<space_id>`, and `widget.blueprint.preview:<space_id>` run ids, so agent-driven Space construction and generated-widget quarantine/preview/render boundaries appear in Space-scoped progress without storing widget bodies, renderer/source/API-auth fields, prompts, scripts, or credentials.
- Source-style layout rearrange/toggle actions now emit metadata-only `tool.completed` progress events with safe `layout.rearrange:<space_id>` and `layout.toggle:<space_id>` run ids, so Space-scoped progress reflects agent-driven layout mutations without storing renderer/html/source/API-auth fields, prompts, scripts, or credentials.
- Recovery widget quarantine/enable tool actions now emit metadata-only `tool.completed` progress events with safe `recovery.widget.*:<space_id>` run ids, so Space-scoped progress reflects recovery admin widget toggles without exposing disabled widget bodies, renderer/source fields, prompts, scripts, or credentials.
- Space Agent package import/export boundaries now emit metadata-only `tool.completed` progress events with safe `package.import:<space_id>` and `package.export:<space_id>` run ids after successful sanitized package operations, without storing package YAML, widget bodies, renderer/source/API-auth fields, prompts, scripts, or credentials; unsupported export formats fail before recording false completion telemetry.
- Recovery revision restores now return metadata-only autonomy-policy evidence and `tool.completed` progress receipts with `recovery.restore:<space_id>` run ids, so rollback/time-travel actions surface in Space-scoped progress without exposing stored renderer/source/API-auth fields, prompts, scripts, disabled widget bodies, or secret-looking fixture markers.
- Space checkpoint anchors now return metadata-only autonomy-policy evidence and `tool.completed` progress receipts with `checkpoint:<space_id>` run ids alongside the revision event id, and the Space detail checkpoint receipt now renders the returned Action policy plus Checkpoint progress evidence without rendering generated widget bodies or exposing hostile reason text, renderer/source/API-auth fields, prompts, scripts, or secret-looking fixture markers.
- Source-style Space deletion/removal aliases now return metadata-only autonomy-policy receipts and `tool.completed` progress receipts with safe `space.delete:<space_id>` run ids after successful revisioned deletion, without exposing hostile renderer/source/html/API-auth fields, scripts, tokens, or secret-looking fixture markers.
- Source-style Space duplicate/clone aliases now return metadata-only autonomy-policy receipts and `tool.completed` progress receipts with safe `space.duplicate:<space_id>` run ids after successful sanitized Space creation, without exposing hostile renderer/source/html/API-auth fields, prompts, scripts, tokens, or secret-looking fixture markers.

Remaining:
- Emit progress events from more real long-running browser, development, repair, and creator workflows.

### Phase 7 — Optional integration catalog/sidecar exploration

Status: **not started; hold until remaining Phase 1-6 integration work is proven end-to-end**.

Only after the remaining Phase 1-6 integration items are working:
- Evaluate connector catalog UX.
- Explore sidecar/native app integration only if it clearly improves Capy's existing self-hosted server product.

---

## Next implementation slices for autonomous sprints

1. **Remaining prompt-preflight + memory/context enforcement**
   - Extend pass/warn/block receipts from the implemented creator preview/commit, active-space instruction/context, repair prompt, widget-runtime prompt, source-style widget definition/blueprint preview/source helpers, and source-refresh paths to remaining high-risk browser/development/recovery/tool boundaries.
   - Keep Memory Tree content untrusted advisory context that cannot bypass creator gates, approval gates, sandbox preview, visual QA, or rollback/recovery controls.

2. **Broader compaction producers**
   - Extend the implemented run-all, individual-demo, creator preview/commit, active-context, and scoped-progress `output_compaction` receipt pattern to remaining long tool/subagent/browser/development/recovery-output boundaries where it adds product value.
   - Preserve failures, approval prompts, and artifact handles/citations; never compact away safety-relevant evidence.

3. **Broader source-refresh scheduling/fetcher coverage**
   - Expand due-job scheduler/cron coverage and source-specific fetchers now that the safe metadata-only refresh worker and manual trigger path exist; keep raw fetched content out of public receipts/UI.

4. **Progress producer expansion**
   - Record structured events from browser/development/repair flows so the product-home and Space-detail streams reflect real autonomous work. Research Harness progress updates, creator visual-QA commit gates, Memory Tree source-refresh ingest workers, the demo smoke suite, individual browser demo smokes, source-style layout-repair actions, shared-data/widget-upsert actions, and recovery widget toggles now cover the first workflow/gate/ingest/run/browser/repair/cooperation/construction/recovery producers.

5. **Model-route invocation plumbing**
   - Reuse the new metadata-only `model_route_resolution` receipts as the visible decision envelope, then wire safe configured hints into actual Capy/Hermes invocation selection without exposing provider config or weakening Brendan's provider-agnostic OpenAI/xAI/LM Studio setup.

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

The first roadmap pass has now delivered the Memory Tree module, source/job schema, safe refresh worker, compaction helper, memory freshness card, relevant-memory UI, policy card, active-context advisory/preflight receipts, and product-home plus Space-detail progress cards. Current remaining work is narrower:

- **Automated artifact ingestion:** Core Space manifest/revision, rollback/restore anchor, repair, widget-event, and visual-QA production boundaries now auto-ingest metadata-only Memory Tree records; keep future artifact producers on the same canonicalizer path.
- **Local knowledge bridge:** Local knowledge now registers metadata-only Memory Tree source records with freshness/provenance rather than copying raw files wholesale into prompts; next work is safe refresh scheduling/fetcher breadth.
- **Advisory active context:** Active-Space context and creator preview now use cited, redacted Memory Tree snippets through metadata-only/preflighted envelopes; remaining source/tool/browser/development boundaries should use the same pattern before memory can influence actions.
- **Compaction execution path:** `api/capy_compaction.py` now backs run-all, individual-demo, creator preview/commit, active-context, and scoped-progress receipts; remaining long tool/subagent/browser/development/recovery outputs still need product-visible compaction integration where useful.
- **Source refresh worker:** Metadata-only `source.refresh` jobs can now be consumed safely and emit progress events; remaining work is broadening production scheduler/cron coverage and source-specific fetchers around the existing manual trigger path.
- **Per-action policy/preflight:** Product-home policy visibility exists, and creator/context/repair/runtime/source-refresh boundaries have initial pass/warn/block receipts; remaining high-risk browser/development/recovery/tool boundaries need equivalent action-policy evidence.
- **Progress producers:** Structured progress recording/status/card exist, and Research Harness progress updates, creator visual-QA commits, Memory Tree source-refresh ingest, demo-suite run events, source-style layout-repair events, shared-data/widget-upsert actions, recovery widget toggles, and Space Agent package import/export operations now emit metadata-only events; more real long-running browser/development/repair workflows should emit events.

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
