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

## Public API draft

Initial backend module: `api/capy_memory.py`.

Suggested pure functions before routes:

- `memory_tree_root() -> Path`
- `memory_tree_db_path() -> Path`
- `init_memory_tree(db_path: Path | None = None) -> dict`
- `canonicalize_space_manifest(space: dict) -> dict`
- `canonicalize_revision_event(event: dict) -> dict`
- `canonicalize_widget_event(event: dict) -> dict`
- `ingest_source(record: dict) -> dict`
- `search_memory(query: str, *, space_id: str | None = None, limit: int = 10) -> dict`
- `relevant_memory_for_space(space_id: str, *, limit: int = 5) -> dict`
- `memory_status() -> dict`

Future routes, after backend tests:

- `GET /api/capy-memory/status`
- `GET /api/capy-memory/search?q=...&space_id=...`
- `GET /api/spaces/memory?space_id=...`
- `POST /api/capy-memory/ingest-space`

All route responses must be metadata-only, bounded, and redacted.

## Spaces UI draft

Future UI surfaces in `static/spaces.js`:

- Product home / side panel: `Memory freshness` card.
- Space detail: `Relevant memory` panel with cited snippets.
- Creator preview card: optional `Context used` section listing source IDs/citations, not raw source bodies.
- Run-all smoke receipt: compact `Memory/context` status checklist.

UI requirements:

- Load actual checked-out `static/spaces.js` in tests.
- Use fixed safe labels for blocked/error states.
- Never render raw backend error text from ingestion/canonicalization failures.
- Omit action attributes when source IDs or Space IDs fail strict path/action-id checks.

## TDD implementation plan

### Task 1: Create failing sanitizer/canonicalizer tests

**Objective:** Specify safe canonicalization before production code exists.

**Files:**

- Create: `tests/test_capy_memory_tree.py`
- Later create: `api/capy_memory.py`

**Step 1: Write failing tests**

Required RED tests:

- `test_canonicalize_space_manifest_omits_generated_body_fields`
- `test_canonicalize_space_manifest_preserves_benign_metadata_labels`
- `test_canonical_chunk_ids_are_deterministic`
- `test_canonicalizer_fails_closed_on_over_deep_metadata`

**Step 2: Run test to verify failure**

```bash
/Users/bschmidy10/.hermes/hermes-agent/venv/bin/python -m pytest tests/test_capy_memory_tree.py -q -o 'addopts='
```

Expected: FAIL because `api.capy_memory` or canonicalizer functions are missing.

**Step 3: Implement minimal canonicalizer**

Create `api/capy_memory.py` with enough code to pass Task 1 only.

**Step 4: Verify pass**

```bash
/Users/bschmidy10/.hermes/hermes-agent/venv/bin/python -m pytest tests/test_capy_memory_tree.py -q -o 'addopts='
/Users/bschmidy10/.hermes/hermes-agent/venv/bin/python -m py_compile api/capy_memory.py tests/test_capy_memory_tree.py
git diff --check
```

**Step 5: Commit**

```bash
git add api/capy_memory.py tests/test_capy_memory_tree.py
git commit -m "feat: add Capy memory tree canonicalizer"
```

### Task 2: SQLite init/status

**Objective:** Create the local database schema and status payload without ingesting content.

**Files:**

- Modify: `api/capy_memory.py`
- Modify: `tests/test_capy_memory_tree.py`

RED tests:

- `test_init_memory_tree_creates_expected_tables`
- `test_memory_status_returns_local_only_counts`

GREEN requirements:

- Create DB under configured path.
- Return `local_only: true`, `source_count`, `chunk_count`, and `stale_source_count`.
- Do not ingest private content during init.

### Task 3: Source ingest/search

**Objective:** Store sanitized Markdown and provide bounded search results.

**Files:**

- Modify: `api/capy_memory.py`
- Modify: `tests/test_capy_memory_tree.py`

RED tests:

- `test_ingest_source_is_idempotent_by_source_id_and_hash`
- `test_search_memory_returns_bounded_redacted_snippets`
- `test_relevant_memory_for_space_filters_by_space_id`

GREEN requirements:

- Store sanitized Markdown under the configured vault.
- Store metadata in SQLite.
- Use SQLite FTS or simple LIKE search as the smallest first version.
- Keep response shape ready for UI cards.

### Task 4: Routes

**Objective:** Expose metadata-only Memory Tree API responses.

**Files:**

- Modify: `api/routes.py`
- Modify: route tests currently covering local knowledge or Spaces routes.

Routes:

- `GET /api/capy-memory/status`
- `GET /api/capy-memory/search?q=...&space_id=...`
- `GET /api/spaces/memory?space_id=...`

Validation:

- CSRF remains unchanged for POST routes.
- GET responses are bounded and redacted.
- Empty/unavailable state is explicit and safe.

### Task 5: Spaces UI freshness/relevant-memory panel

**Objective:** Make memory status visible in the product.

**Files:**

- Modify: `static/spaces.js`
- Modify: `tests/test_spaces_ui_js_behaviour.py`

Required tests:

- Freshness card renders local-only counts and stale/error states.
- Relevant memory panel renders citations/snippets and omits unsafe raw fields.
- Failed memory fetch renders a fixed safe unavailable label, not raw error text.

Validation:

```bash
node --check static/spaces.js
/Users/bschmidy10/.hermes/hermes-agent/venv/bin/python -m pytest tests/test_spaces_ui_js_behaviour.py -q -o 'addopts='
```

Then run browser/visual QA before claiming product-visible completion.

## Security invariants

- Memory Tree output must never increase privileges or bypass gates.
- Retrieved context is advisory, not authority.
- Prompt-injection preflight must run before high-risk source context influences creator-loop or autonomous actions.
- Raw source artifacts can remain in trusted local state only when needed for repair/provenance, but public snippets must be sanitized summaries.
- Report redaction by status/counts so the user can tell when content was omitted.
- Exact raw credentials or secret values must never be persisted into canonical Markdown or returned to the browser.

## Validation checklist

For this design/doc-only change:

```bash
git diff --check -- docs/capy-memory-tree.md
```

For first backend implementation:

```bash
/Users/bschmidy10/.hermes/hermes-agent/venv/bin/python -m pytest tests/test_capy_memory_tree.py -q -o 'addopts='
/Users/bschmidy10/.hermes/hermes-agent/venv/bin/python -m py_compile api/capy_memory.py tests/test_capy_memory_tree.py
git diff --check
```

For first UI implementation:

```bash
node --check static/spaces.js
/Users/bschmidy10/.hermes/hermes-agent/venv/bin/python -m pytest tests/test_spaces_ui_js_behaviour.py -q -o 'addopts='
```

Add browser console/leak checks, screenshot evidence, and Visual/UI QA before marking UI-visible work complete.
