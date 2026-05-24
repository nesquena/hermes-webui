# Capy Spaces OpenHuman + Space Agent Feature Audit

> **For Hermes:** Use this as a current competitive-feature checklist for Capy Spaces sprint planning. Implement via `subagent-driven-development` task-by-task, with strict TDD and visual/browser QA for every UI-visible slice.

**Created:** 2026-05-24 12:32 CDT  
**Goal:** Ensure Capy Spaces adopts the best product/architecture ideas from OpenHuman and Space Agent while preserving Capy's own strengths: Hermes gateway, Telegram, cron autonomy, local-first safety, metadata-only Spaces, recovery, and visual QA.

**Architecture:** Capy should not clone either project. Use a clean-room feature-parity matrix: adopt concepts natively in Hermes WebUI/Capy Spaces, keep generated-code execution gated/quarantined, and expose trust/provenance/model/freshness receipts product-side.

**Source evidence reviewed:**
- OpenHuman repo/docs: `tinyhumansai/openhuman`, GPL-3.0, latest release `v0.54.0`, ~26.9k stars / ~2.5k forks, pushed 2026-05-24.
- OpenHuman product docs: local Memory Tree + Obsidian-style vault, OAuth integrations, ~20-minute sync cadence, managed-vs-local settings, desktop onboarding.
- Space Agent repo/docs: `agent0ai/space-agent`, MIT, latest release `v0.66`, ~1.2k stars / 282 forks.
- Local Space Agent reference: `/Users/bschmidy10/workspace/space-agent-reference`.
- Current Capy plans: `.hermes/plans/capy-openhuman-inspired-roadmap.md` and `.hermes/plans/capy-spaces-space-agent-parity.md`.

---

## Non-negotiables

1. **Clean-room OpenHuman only.** OpenHuman is GPLv3; use product concepts, not code, tests, schemas, prompts, comments, or fixtures.
2. **No platform pivot.** Keep Capy as Hermes WebUI + Telegram/gateway + cron/subagents + local server, not a Rust/Tauri rewrite.
3. **Spaces safety remains primary.** Generated widget/runtime code remains disabled by default unless sandbox, approval, visual QA, and recovery gates prove safe.
4. **Metadata-only receipts.** Public APIs/UI show summaries, ids, status, provenance, and policy receipts — not raw prompts, code, renderer/source bodies, API-auth fields, tokens, or secrets.
5. **TDD + browser evidence.** Every sprint slice needs failing tests first and visual/layout QA evidence for UI-visible changes.

---

## Competitive feature matrix

### A. OpenHuman-inspired features

**Already underway / partially implemented in Capy**
- Local Memory Tree with SQLite/Markdown-like local storage and provenance.
- Memory/search/status surfaces connected to Spaces.
- Auto-fetch source registry foundation and source-refresh job queue.
- Output compaction receipts for demo/creator/context/progress boundaries.
- Autonomy policy, prompt-preflight, model-route hints, and structured progress events.
- Local/remote model posture through Hermes providers, OpenAI Codex, xAI, and LM Studio profile.

**Best features to finish next**
1. **Connector catalog + typed tools**
   - Product UX: a visible catalog of connectable sources/tools with status, permissions, sync cadence, and last-sync time.
   - Backend: source-specific fetchers/adapters for Gmail/Calendar/GitHub/Drive/Slack/Linear/Notion where credentials already exist or can be safely added.
   - Safety: each connector exposes least-privilege typed actions and metadata-only receipts; no connector can inject raw content into agent context without memory/context preflight.

2. **Actual model-route invocation plumbing**
   - Capy already renders model-route decisions; next step is to make selected route hints influence real invocations where safe.
   - Workloads: fast UI summaries, reasoning-heavy planning, vision/browser QA, local/private summarization, and long-context synthesis.
   - Must preserve Brendan's provider-agnostic setup and avoid exposing provider config or secrets.

3. **Cross-session / cross-chat context retrieval**
   - Productize a bounded “Relevant prior work” layer from Hermes session_search + Capy Memory Tree.
   - Show citations, dates, and freshness; keep it advisory and untrusted.

4. **Folder-of-files / NotebookLM-style ingestion**
   - Add a safe UI and backend path to ingest selected local folders/repositories into Memory Tree as summaries/provenance, not raw prompt stuffing.
   - Tie into existing local knowledge MCP and `api/knowledge.py`.

5. **Local voice and meeting/audio roadmap**
   - Later-stage: local STT/TTS/voice briefings and meeting transcript ingestion.
   - Keep this lower priority than connector/catalog/model-routing because Telegram already gives Capy a strong mobile control surface.

6. **Onboarding/settings polish**
   - Add a product-home “setup path” showing: models configured, memory freshness, connectors, autonomy mode, progress stream, and recovery status.

7. **Security hardening inspired by OpenHuman v0.54.0**
   - Explicitly track DNS rebinding/SSRF defenses for URL fetchers.
   - Token/bearer logging prevention tests.
   - Path traversal tests for file/folder ingestion and package import/export.
   - Guardrails for browser/proxy allow-all behavior.

### B. Space Agent-inspired features

**Already underway / partially implemented in Capy**
- Agent-shaped workspace/canvas with metadata-only widgets.
- Space Agent-style tool aliases for Spaces/widgets/recovery/revisions/packages.
- Revision history, rollback/restore, checkpoints, recovery panels.
- Package import/export and quarantine.
- Progress streams and safety receipts around agent-driven mutations.
- Visual/browser QA as acceptance gate.

**Best features to finish next**
1. **Browser/runtime API parity layer**
   - Create a stable Capy `space` runtime facade for UI widgets/templates to call metadata-safe operations.
   - Maintain strict sanitizer boundaries; the facade should not execute untrusted widget code by default.

2. **Composable applets/templates, not raw generated code**
   - Promote safe widget templates and “tool-backed applets” as the primary extension path.
   - Each applet has schema, data contract, policy gates, preview fixture, and recovery metadata.

3. **Layered customization model, Capy-native**
   - Borrow the idea of core/shared/user layers without copying implementation.
   - Suggested Capy layers: `system` templates, `workspace` shared Spaces, `user` personal overrides, `session` ephemeral drafts.
   - Add tests for precedence, rollback, and no cross-layer leakage.

4. **Share/clone/package UX**
   - Space Agent’s package/share story is central. Capy should make package import/export visible and friendly:
     - package manifest summary
     - safety scan result
     - quarantine status
     - preview screenshot/QA result
     - clone/fork action with rollback anchor

5. **Admin/time-travel polish**
   - Make rollback/checkpoint/recovery flows easier to understand visually.
   - Add diff/preview summaries that are metadata-only and safe.

6. **Frontend-first where possible**
   - Keep Capy backend-owned for security/recovery/persistence, but UI iterations should favor static JS/CSS and backend route adapters only when integrity/security needs them.

7. **Per-user/workspace sharding before multi-user scale**
   - Space Agent v0.66 added on-demand per-user file-index shards. Capy should use the same principle for local knowledge/memory indexes and multi-workspace source scans: lazy, scoped, bounded, not global startup scans.

---

## Recommended next sprint order

### Sprint 1 — Finish native widget mutation safety receipts

**Why:** Current working tree is already in this area. Finish it cleanly before broadening scope.  
**Acceptance:** widget upsert/patch/delete route/tool responses return metadata-only preflight/action-policy/progress receipts and recursively strip generated/source/raw/code/prompt/secret fields from persistence and public receipts.  
**Validation:** focused RED/GREEN tests, full Spaces foundation suite, `py_compile`, `git diff --check`, browser QA for receipt rendering/no DOM leaks.

### Sprint 2 — Connector catalog shell + source freshness UX

**Objective:** Make OpenHuman-style integrations visible without adding risky OAuth breadth yet.  
**Files likely:** `api/capy_memory.py`, `api/routes.py`, `static/spaces.js`, `tests/test_capy_memory_tree.py`, `tests/test_spaces_ui_js_behaviour.py`.  
**Deliverable:** product-home connector/freshness card listing source types, last sync, stale count, queued refresh jobs, and disabled/not-configured connectors.

### Sprint 3 — Actual model-route invocation plumbing MVP

**Objective:** Use existing `model_route_resolution` receipts to select safe invocation profiles for at least one internal Capy call path.  
**Start with:** summarization/compaction or Memory Tree source-refresh summarization, not arbitrary widget execution.  
**Acceptance:** receipts match actual selected route; unsafe or missing config falls back deterministically; no API keys/provider secrets leak.

### Sprint 4 — Capy runtime facade / applet contract

**Objective:** Create a safe Capy-native equivalent of Space Agent’s `globalThis.space` concept for approved templates/applets.  
**Acceptance:** documented facade methods, tests proving unsafe methods/fields fail closed, demo applet using only metadata-safe calls.

### Sprint 5 — Package/share/clone UX polish

**Objective:** Make Space package import/export/clone a first-class product flow with safety scan, quarantine, preview, QA, and rollback anchor visible.

### Sprint 6 — Cross-session relevant context card

**Objective:** Blend Hermes session_search + Capy Memory Tree into a bounded, cited, freshness-aware context card for active Spaces.

---

## Features to intentionally de-prioritize

- Full desktop mascot/personality clone: nice branding idea, but lower leverage than Spaces + Telegram + cron autonomy.
- Massive OAuth catalog before safety/source-refresh scaffolding is strong.
- Running arbitrary generated browser code by default.
- GPL-derived implementation reuse from OpenHuman.
- Multi-user enterprise hierarchy before Brendan's single-user/local-first workflow is excellent.

---

## Definition of “we are building the best features”

Capy Spaces should be evaluated against these product outcomes:

1. **Remembers work:** local Memory Tree + cited session/source context are visible and useful.
2. **Builds safely:** agent-created widgets/templates are metadata-only by default, recoverable, and visually QA'd.
3. **Explains trust:** every risky action shows policy/preflight/model/progress receipts.
4. **Keeps fresh:** sources/connectors show sync state and freshness.
5. **Routes intelligently:** workload-specific model routing is visible and actually applied where safe.
6. **Works everywhere Brendan uses Capy:** WebUI, Telegram, cron, and local browser QA remain first-class.
7. **Can roll back:** every material Space mutation has revision/checkpoint/recovery affordances.
8. **Scales locally:** indexes/source scans are lazy, scoped, and workspace/user-aware.

---

## Evidence-backed current conclusion

Capy is already ahead of a basic Space Agent clone because it combines Spaces with Hermes gateway, Telegram, cron, skills, subagents, MCP, local knowledge, and strong safety receipts. The highest-leverage gap is not another widget demo; it is finishing the local context operating layer: connector/source freshness, actual model routing, safe applet/runtime facade, package/share UX, and cross-session context — all with the metadata-only safety standard already established in the current Capy Spaces work.
