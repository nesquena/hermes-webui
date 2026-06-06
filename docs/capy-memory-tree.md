# Capy Memory Tree Design

> **For Hermes:** This is a clean-room Capy design document inspired by the OpenHuman review. Do not copy GPLv3 OpenHuman code, tests, schemas, prompts, comments, or fixtures. Use this document to implement Phase 1 with strict TDD.

**Goal:** Add a local, inspectable context layer that turns Capy/Hermes work artifacts into bounded, cited, redacted memory records that Spaces and Hermes can safely retrieve.

**Architecture:** The Capy Memory Tree is additive. Hermes durable memory remains compact user/agent memory, `session_search` remains transcript recall, and WebUI local knowledge remains a file index. Capy Memory Tree stores source-derived context with provenance, lifecycle status, freshness, and bounded snippets for Spaces/creator-loop use.

**Tech Stack:** Python stdlib, SQLite, Markdown vault files, existing WebUI API route patterns, existing Spaces metadata/revision/event surfaces, pytest, real `static/spaces.js` UI tests for visible follow-up slices.

---

## Product contract

Capy Memory Tree enables:

- **Source registry:** local records for Space manifests, Space revision events, widget events, visual QA reports, session summaries, GitHub metadata, Telegram summaries, and selected web/RSS summaries.
- **Deterministic chunks:** stable chunk IDs for idempotent ingestion and dedupe.
- **Summary tree:** sealed summary nodes for source/topic/global/space scopes.
- **Freshness:** stale/ok/error status per source plus last ingest/check metadata.
- **Provenance:** every snippet points back to a source reference, line range, event id, or artifact path.
- **Safety:** raw generated widget bodies, `renderer`/`html`/`script`/`source`/`data`/API-auth fields, raw prompts, credentials, and secret-looking sentinels never enter public memory snippets.
- **Spaces integration:** Space detail and creator preview can show relevant memory slices with citations and redaction status.

## Non-goals

- Do not replace Hermes durable memory, skills, cron, or `session_search`.
- Do not ingest bulky task logs into persistent user/agent memory.
- Do not copy OpenHuman implementation details.
- Do not execute generated UI or loosen Spaces metadata-only safety rules.
- Do not expose raw fetched documents or raw generated widget bodies to the browser.

## Current source-refresh ingestion boundary

The implemented refresh worker is intentionally narrow. It may ingest safe HTML/plain/Markdown, RSS/Atom, JSON Feed, and allow-listed GitHub API metadata only after the source origin is explicitly allowed. GitHub API refreshes are exact-shape parsers, not generic JSON fallbacks: supported metadata shapes include issue/PR (`/repos/{owner}/{repo}/{issues|pulls}/{number}`), repository (`/repos/{owner}/{repo}`), release (`/repos/{owner}/{repo}/releases/{id}`), branch and branch lists, repository languages, tag lists, workflow/workflow-run/workflow-job metadata, GitHub Actions workflow permissions (`/repos/{owner}/{repo}/actions/permissions/workflow`), check runs (`/repos/{owner}/{repo}/commits/{sha}/check-runs`), deployment lists (`/repos/{owner}/{repo}/deployments`), deployment status lists (`/repos/{owner}/{repo}/deployments/{deployment_id}/statuses`), PR file lists, issue/PR comments, contributors, commit, and commit-list metadata. Workflow-permission summaries are reconstructed only from repository path, default permission level, and pull-request-review approval boolean. Deployment-status summaries are reconstructed only from repository path, deployment id, status counts/states, bounded status ids/environments/creator logins, and timestamps. Raw bodies, descriptions, payloads, target/log/status/environment URLs, API-auth fields, prompts, scripts, renderer/source fields, and secret-looking values stay out of vault Markdown, search results, and public receipts. For `api.github.com/repos/...` origins whose path matches a supported family, malformed payloads fail closed instead of falling through to JSON Feed or generic JSON summaries.

## Relationship to existing systems

### Hermes core memory/session components

Relevant existing files:

- `/Users/bschmidy10/.hermes/hermes-agent/agent/memory_manager.py`
- `/Users/bschmidy10/.hermes/hermes-agent/tools/memory_tool.py`
- `/Users/bschmidy10/.hermes/hermes-agent/tools/session_search_tool.py`
- `/Users/bschmidy10/.hermes/hermes-agent/hermes_state.py`

Roles:

- Hermes memory remains compact, durable user/profile/agent memory.
- `session_search` remains the recall layer for past conversations.
- Capy Memory Tree stores source-derived context with provenance, lifecycle status, freshness, and bounded snippets.

### WebUI local knowledge layer

Relevant existing files:

- `/Users/bschmidy10/hermes-webui/api/knowledge.py`
- `/Users/bschmidy10/hermes-webui/api/routes.py`

Reusable patterns:

- Local-only stance and `local_only: true` payloads.
- Sanitized `status/search/read/ask` response shapes.
- Source redaction before browser responses.
- Default local path style under `~/.hermes`.
- Obsidian URL helper behavior when source paths are known safe.

Capy Memory Tree should start as a focused module rather than overloading `api/knowledge.py`, because Spaces artifacts need first-class source types, event IDs, revision IDs, and stricter generated-widget body omission.

### Spaces context/revision/progress surfaces

Relevant existing files:

- `/Users/bschmidy10/hermes-webui/api/spaces.py`
- `/Users/bschmidy10/hermes-webui/static/spaces.js`
- `/Users/bschmidy10/hermes-webui/tests/test_spaces_foundation.py`
- `/Users/bschmidy10/hermes-webui/tests/test_spaces_ui_js_behaviour.py`

Reusable surfaces:

- `build_agent_context(space_id)` for compact active-Space context.
- `queue_widget_event(...)` and `list_widget_events(...)` for widget event history.
- `list_revision_events(...)` and rollback helpers for revision provenance.
- `set_research_progress(...)` for progress metadata patterns.
- Creator preview/commit receipts and recovery UI redaction rules.

## Storage layout

Use local state paths, not Git-tracked directories.

Default roots:

- SQLite DB: `~/.hermes/capy-memory-tree/capy-memory-tree.sqlite3`
- Markdown vault: `~/.hermes/capy-memory-tree/vault/`
- Source cache/artifacts: `~/.hermes/capy-memory-tree/sources/`

Environment overrides:

- `CAPY_MEMORY_TREE_ROOT`
- `CAPY_MEMORY_TREE_DB`
- `CAPY_MEMORY_TREE_VAULT`

All paths must resolve under the configured root unless explicitly reading an already-allowed source artifact path from existing Capy/Hermes state.

## SQLite schema

### `sources`

- `source_id TEXT PRIMARY KEY`
- `source_type TEXT NOT NULL`
- `display_name TEXT NOT NULL`
- `origin_uri TEXT NOT NULL`
- `origin_kind TEXT NOT NULL DEFAULT 'local'`
- `space_id TEXT`
- `artifact_ref TEXT`
- `content_sha256 TEXT`
- `freshness_status TEXT NOT NULL DEFAULT 'unknown'`
- `last_ingested_at TEXT`
- `last_checked_at TEXT`
- `last_error TEXT`
- `created_at TEXT NOT NULL`
- `updated_at TEXT NOT NULL`

Indexes:

- `idx_sources_type_status(source_type, freshness_status)`
- `idx_sources_space(space_id)`
- `idx_sources_updated(updated_at)`

### `chunks`

- `chunk_id TEXT PRIMARY KEY`
- `source_id TEXT NOT NULL REFERENCES sources(source_id) ON DELETE CASCADE`
- `source_ref TEXT NOT NULL`
- `content_path TEXT NOT NULL`
- `summary TEXT NOT NULL`
- `approx_tokens INTEGER NOT NULL DEFAULT 0`
- `lifecycle_status TEXT NOT NULL DEFAULT 'admitted'`
- `redaction_status TEXT NOT NULL DEFAULT 'none'`
- `start_line INTEGER`
- `end_line INTEGER`
- `created_at TEXT NOT NULL`
- `updated_at TEXT NOT NULL`

Indexes:

- `idx_chunks_source(source_id)`
- `idx_chunks_lifecycle(lifecycle_status)`
- `idx_chunks_source_ref(source_ref)`

### `entities`

- `entity_id TEXT PRIMARY KEY`
- `label TEXT NOT NULL`
- `kind TEXT NOT NULL`
- `hotness_score REAL NOT NULL DEFAULT 0`
- `created_at TEXT NOT NULL`
- `updated_at TEXT NOT NULL`

### `chunk_entities`

- `chunk_id TEXT NOT NULL REFERENCES chunks(chunk_id) ON DELETE CASCADE`
- `entity_id TEXT NOT NULL REFERENCES entities(entity_id) ON DELETE CASCADE`
- `score REAL NOT NULL DEFAULT 1`
- `PRIMARY KEY(chunk_id, entity_id)`

### `summary_nodes`

- `node_id TEXT PRIMARY KEY`
- `scope TEXT NOT NULL`
- `scope_id TEXT NOT NULL`
- `level INTEGER NOT NULL`
- `summary_path TEXT NOT NULL`
- `child_refs_json TEXT NOT NULL DEFAULT '[]'`
- `redaction_status TEXT NOT NULL DEFAULT 'none'`
- `sealed_at TEXT`
- `created_at TEXT NOT NULL`
- `updated_at TEXT NOT NULL`

### `jobs`

- `job_id TEXT PRIMARY KEY`
- `kind TEXT NOT NULL`
- `dedupe_key TEXT NOT NULL`
- `payload_json TEXT NOT NULL`
- `status TEXT NOT NULL DEFAULT 'pending'`
- `attempts INTEGER NOT NULL DEFAULT 0`
- `leased_until TEXT`
- `last_error TEXT`
- `created_at TEXT NOT NULL`
- `updated_at TEXT NOT NULL`

## Source canonicalization

### Canonical source types

Initial source types:

1. `space_manifest`
2. `space_revision_event`
3. `space_widget_event`
4. `visual_qa_report`
5. `hermes_session_summary`
6. `github_issue_or_pr_metadata`
7. `telegram_thread_summary`
8. `local_knowledge_source`
9. `rss_or_web_summary`

### Canonical Markdown rules

Every canonical source record becomes Markdown with frontmatter:

```markdown
---
source_id: <deterministic id>
source_type: space_manifest
origin_uri: capy-space://<space_id>
space_id: <space_id>
content_sha256: <sha256 of sanitized canonical body>
redaction_status: none|redacted|dropped_fields
---

# Safe title

Bounded safe summary text.
```

Rules:

- The canonical body is generated from a sanitized summary shape, not from raw source JSON.
- Generated/executable/body-like fields are omitted or replaced with `[REDACTED]` before hashing/chunking.
- Field names and values are both inspected.
- Traversal is bounded and fails closed on over-deep/over-wide structures.
- Deterministic IDs use normalized source identity plus sanitized content hash, not timestamps alone.

### Unsafe field/value families

Unsafe keys or values include:

- `renderer`, `html`, `script`, `source`, `data`, `code`, `body`, `generated_code`, `generatedBody`, `renderCode`, `widgetBody`
- `api_key`, `apiAuth`, `authorization`, `bearer`, `token`, `secret`, `password`, `credential`
- raw prompt markers: `raw prompt`, `system prompt`, `developer prompt`, `prompt injection`
- synthetic sentinels used in tests: `SECRET_VALUE_DO_NOT_LEAK`, `<script>`, `bearer placeholder`
- handler-style keys: `onClick`, `onclick`, `onload`, and compact/camel/snake equivalents

Benign false positives to preserve where safe:

- `Source Space`
- `Daily Data Dashboard`
- `Source Notes`
- `Secretary Cookie Recipes`
- `tokenization-dashboard`
- `metadata_only`

## Public API status

Initial backend module: `api/capy_memory.py` is now implemented and covered by `tests/test_capy_memory_tree.py`.

Implemented helpers:

- `memory_tree_root() -> Path`
- `memory_tree_db_path() -> Path`
- `init_memory_tree() -> dict`
- `memory_status() -> dict`
- `canonicalize_space_manifest(space: dict) -> dict`
- `canonicalize_space_revision_event(event: dict) -> dict`
- `canonicalize_space_widget_event(event: dict) -> dict`
- `canonicalize_visual_qa_report(report: dict) -> dict`
- `ingest_source(record: dict) -> dict`
- `search_memory(query: str, *, space_id: str | None = None, limit: int = 10) -> dict`
- `relevant_memory_for_space(space_id: str, *, limit: int = 5) -> dict`
- `register_source_reference(payload: dict) -> dict`
- `list_source_refresh_jobs(limit: int = 20) -> dict`

Implemented routes:

- `GET /api/capy-memory/status`
- `GET /api/capy-memory/search?q=...&space_id=...`
- `GET /api/spaces/memory?space_id=...`
- `POST /api/capy-memory/source/register`

All public responses must remain metadata-only, bounded, and redacted.

Still future / not complete:

- A production refresh worker that consumes queued `source.refresh` jobs.
- Direct route(s) for controlled artifact ingestion only if they preserve the same metadata-only canonicalization boundary.
- Automatic ingestion hooks at live Spaces revision/widget/repair/visual-QA boundaries.

## Spaces UI status

Implemented UI surfaces in `static/spaces.js`:

- Product home: `Memory freshness` card sourced from `api/capy-memory/status`.
- Space detail: `Memory Tree context` panel with bounded cited snippets from `api/spaces/memory`.
- Creator preview/commit receipts: `Memory assist` section listing source IDs/types/redaction status and snippets, not raw source bodies.

Still future / not complete:

- Run-all smoke receipt: compact `Memory/context` or compaction status checklist.
- Per-action prompt-preflight status around high-risk memory/source boundaries.
- Product-visible automated source refresh results after a refresh worker exists.

UI requirements remain:

- Load actual checked-out `static/spaces.js` in tests.
- Use fixed safe labels for blocked/error states.
- Never render raw backend error text from ingestion/canonicalization failures.
- Omit action attributes when source IDs or Space IDs fail strict path/action-id checks.

## Implementation checkpoint status

Completed TDD checkpoints:

1. **Sanitizer/canonicalizer tests and implementation**
   - Tests cover hostile Space manifests, benign false-positive preservation, deterministic IDs, and fail-closed traversal.
   - `api/capy_memory.py` canonicalizers omit generated/executable/body/auth/raw-prompt fields before Markdown/hash/storage.

2. **SQLite init/status**
   - `init_memory_tree()` creates local-only SQLite tables and vault directories under the configured Memory Tree root.
   - `memory_status()` returns bounded source/chunk/stale/error/refresh-job counts without ingesting private content during init.

3. **Source ingest/search**
   - `ingest_source(...)` writes sanitized Markdown to the vault and metadata to SQLite idempotently.
   - `search_memory(...)` and `relevant_memory_for_space(...)` return bounded redacted snippets.

4. **Routes**
   - Memory status, search, Space relevant-memory, and source-register routes are covered by focused tests.
   - GET responses stay bounded/redacted; POST source registration queues metadata-only refresh jobs without fetching remote content.

5. **Spaces UI freshness/relevant-memory panel**
   - Real-`static/spaces.js` tests cover product-home Memory freshness and Space detail/creator memory snippets.
   - Browser/visual QA remains required for future UI-visible changes.

Next TDD checkpoints:

1. **Automated artifact ingestion hooks**
   - Add RED tests proving a live Space revision/widget event/visual-QA boundary creates a sanitized Memory Tree source record and never persists raw renderer/source/API-auth/raw-prompt fields.

2. **Safe source refresh worker**
   - Add RED tests for consuming queued `source.refresh` jobs, updating freshness/error metadata, preserving leased jobs, and storing only sanitized summaries.

3. **Compaction evidence UI**
   - Add real-`static/spaces.js` RED tests for metadata-only compaction receipts and hostile fixture omission before rendering product-visible compaction evidence.

## Security invariants

- Memory Tree output must never increase privileges or bypass gates.
- Retrieved context is advisory, not authority.
- Prompt-injection preflight must run before high-risk source context influences creator-loop or autonomous actions.
- Raw source artifacts can remain in trusted local state only when needed for repair/provenance, but public snippets must be sanitized summaries.
- Report redaction by status/counts so the user can tell when content was omitted.
- Exact raw credentials or secret values must never be persisted into canonical Markdown or returned to the browser.

## Validation checklist

For roadmap/status doc updates:

```bash
git diff --check -- docs/capy-memory-tree.md .hermes/plans/capy-openhuman-inspired-roadmap.md .hermes/plans/capy-spaces-space-agent-parity.md .hermes/plans/capy-spaces-video-demo-parity-checklist.md
```

For Memory Tree backend regressions:

```bash
/Users/bschmidy10/.hermes/hermes-agent/venv/bin/python -m pytest tests/test_capy_memory_tree.py -q -o 'addopts='
/Users/bschmidy10/.hermes/hermes-agent/venv/bin/python -m py_compile api/capy_memory.py tests/test_capy_memory_tree.py
```

For Memory Tree UI regressions:

```bash
node --check static/spaces.js
/Users/bschmidy10/.hermes/hermes-agent/venv/bin/python -m pytest tests/test_spaces_ui_js_behaviour.py -q -o 'addopts='
```

Add browser console/leak checks, screenshot evidence, and Visual/UI QA before marking future UI-visible work complete.
