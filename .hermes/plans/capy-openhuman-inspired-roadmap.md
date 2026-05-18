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

Status: **in progress / first autonomous slice**.

Deliverables:
- This canonical roadmap file.
- `capy-spaces-space-agent-parity.md` updated with the OpenHuman-inspired expansion track.
- `capy-spaces-video-demo-parity-checklist.md` updated with visible context-layer demo criteria.
- Appendix below listing reusable current components and gaps.

### Phase 1 — Capy Memory Tree design/MVP

Build a clean-room local memory tree backbone:
- Source registry with freshness/provenance.
- Deterministic chunk/source canonicalization.
- Redacted Markdown/vault summaries.
- SQLite metadata/index tables.
- Local search/read APIs usable by Spaces and Hermes.

Likely first files:
- `docs/capy-memory-tree.md` — design doc.
- `api/capy_memory.py` — backend module, after design/test plan is accepted.
- `tests/test_capy_memory_tree.py` — RED/GREEN storage and sanitization tests.

### Phase 2 — TokenJuice-style output compaction

Add a local compaction layer for long tool/subagent/browser outputs:
- Preserve citations/provenance and exact artifact handles.
- Drop or summarize noisy output before model context.
- Never hide safety-relevant errors, prompts, credentials, or generated-code markers without explicit redaction labels.

Likely first files:
- `docs/capy-output-compaction.md`
- `api/capy_compaction.py`
- `tests/test_capy_output_compaction.py`

### Phase 3 — Auto-fetch source registry and freshness

Add safe source refresh jobs:
- Track local knowledge, Spaces artifacts, GitHub/project metadata, and selected URLs/RSS as source records.
- Expose stale/source/error counts.
- Make refresh status visible in WebUI/Spaces without leaking raw fetched content.

Likely first files:
- `api/capy_memory.py` source/job tables or a focused helper module.
- `static/spaces.js` memory freshness card.
- `tests/test_spaces_ui_js_behaviour.py` UI card coverage.

### Phase 4 — Spaces-aware memory integration

Connect Spaces to the memory layer:
- Space detail shows bounded relevant memory slices.
- Creator preview includes cited, redacted relevant context.
- Revision/rollback/repair events can be summarized into memory records.
- Visual QA reports become searchable local artifacts.

### Phase 5 — Autonomy policy, prompt-injection preflight, model-routing hints

Make trust decisions product-visible:
- `Supervised`, `Semi-autonomous`, and `Autonomous` policy labels.
- Prompt-injection preflight status for high-risk source/tool boundaries.
- Model routing hints compatible with Brendan's OpenAI/xAI/LM Studio setup.
- No hidden one-subscription model backend pivot.

### Phase 6 — Structured progress events

Status: **active / product-home surfacing in progress**.

Expose agent/subagent/tool progress as structured UI events:
- Bounded event stream for long-running creator/research/development tasks.
- Progress cards in Spaces and WebUI.
- Durable enough for post-run reports; redacted enough for browser display.
- Recent progress family counts are surfaced as metadata-only product-home chips so users can distinguish run/tool/subagent/memory-ingest/visual-QA activity without raw payloads.

### Phase 7 — Optional integration catalog/sidecar exploration

Only after Phases 1-6 are working:
- Evaluate connector catalog UX.
- Explore sidecar/native app integration only if it clearly improves Capy's existing self-hosted server product.

---

## First three implementation slices for autonomous sprints

1. **Phase 0 plan-doc alignment**
   - Create this file and patch the two existing Space Agent parity/checklist plans.
   - Validate with search/read/diff checks.
   - Commit plan docs only.

2. **Phase 1 design doc: `docs/capy-memory-tree.md`**
   - Write the clean-room schema/storage/provenance/redaction design.
   - Include TDD task breakdown before code.
   - Validate with doc lint/read checks; no production code yet unless the plan explicitly escalates.

3. **Phase 1 first RED test: sanitized Space artifact canonicalizer**
   - Add tests for converting a Space manifest/revision/widget event/visual-QA report into bounded safe Markdown/source metadata.
   - Prove raw `renderer`, `html`, `script`, `source`, `data`, API-auth fields, raw prompts, and secret-looking sentinels are omitted/redacted.
   - Implement the smallest canonicalizer to pass.

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

### Gaps to close

- No canonical Capy Memory Tree schema/storage module exists yet.
- Local knowledge is file-index/search oriented; it is not yet tied to Spaces revision/widget/visual-QA artifacts as first-class source records.
- Active-Space context is compact but not yet citation-backed by a memory tree.
- No dedicated output-compaction module for long tool/subagent/browser outputs.
- No product-visible memory freshness card in Spaces.
- No product-visible relevant-memory panel on Space detail.
- Autonomy mode/prompt-injection preflight/model-routing hints are not yet exposed as a cohesive Spaces policy surface.
- Structured progress events exist in narrow demo/research paths but not as a general long-running task event stream.

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
