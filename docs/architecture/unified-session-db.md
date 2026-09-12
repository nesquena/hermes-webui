# Unified SessionDB — Scope, Source of Truth, and Issue #498 Shadow Slice

Maintainer response: https://github.com/nesquena/hermes-webui/issues/498#issuecomment-5385677557

## Accepted scope of this PR (#498 shadow slice)

This PR is the dormant, disabled-by-default first slice for unified `state.db`
lifecycle metadata. It adds a pure, aggregate-only, read-only shadow comparison
path plus an offline aggregate audit. It does not change runtime behavior,
ordering, filtering, caching, events, or streaming.

## Full source of truth split (current rule for this slice)

In this slice the JSON sidecar is the comparison input/local existing representation — it is
not an authority that overrides `state.db` for lifecycle where a row is matched and provenance-
verified. For matched provenance-verified core-backed rows, `state.db` is authoritative for its
lifecycle fields (`pinned`/`archived`). Transcripts and WebUI-only metadata remain JSON-owned.
There is one unambiguous rule, not a dual-authority merge.

| Field / concern | Current authoritative owner in this slice | Notes |
|---|---|---|
| `messages`, `tool_calls`, `message_count` | WebUI JSON sidecar | Transcript authority stays with sidecars in this slice. |
| `title`, `workspace`, `profile`, `project_id`, `model`, token/cost, `pending_*`, `active_stream_id`, `worktree_*`, `composer_draft`, `anchor_activity_scenes` | WebUI JSON sidecar | Display/metadata authority. Only `pinned`/`archived` are compared in this slice; other sidecar fields are not diagnosed. |
| `pinned`, `archived` (lifecycle) — comparison input | WebUI JSON sidecar (input/existing representation) | Read via established read-only sidecar prefix path; tri-state (absent vs explicit false vs explicit true). This is the local view being compared, not an override authority for matched rows. |
| `pinned`, `archived` — lifecycle authority (matched, provenance-verified only) | `state.db` `sessions` table | Read via `open_state_db_readonly` URI; tri-state via column presence + NULL vs explicit 0/1. Authoritative for matched, provenance-verified core-backed rows (`source`/`session_source` classified as WebUI via `normalize_agent_session_source`). Foreign/unknown core-only rows are outside the eligible comparison domain and contribute to no totals; a matched pair with an unverified source is `blocked` (fail-closed). |
| `parent_session_id`, `ended_at`, `end_reason`, `source`/`session_source` lineage facts | `state.db` `sessions` table | Consumed only through the repository's canonical continuation predicate `api.agent_sessions._is_continuation_session` (requires `ended_at` + `end_reason` in {compression, cli_close}, exact source match, `session_source == 'fork'` exclusion; `started_at` is never used as an end boundary). `state.db` is authoritative for `parent_session_id` wherever a core row exists; a sidecar `parent_session_id` establishes lineage only for sidecar-only candidates and never overwrites a core candidate's canonical parent. The union of exact parent references is used solely for blocked-anchor descendant propagation. |

Identity handling (exact raw strings only):

- `session_id` (sidecar filename stem, payload `session_id`, core `id`), `profile`, and `parent_session_id` are compared as raw exact strings after only validating `isinstance(x, str)` and non-empty/non-whitespace. Non-strings are malformed/blocked; whitespace-only is malformed. No `.strip()` normalization into an admitted/comparison identity and no `str(...)` coercion of non-strings. Trailing/leading spaces remain distinct values and never alias their unpadded counterpart. Ordinary categorical normalization of `source`/`session_source` is outside this requirement.

Lifecycle tri-state encodings (applies to both the comparison input and the SQLite reader):

- Permitted: actual `bool`, exact integer `0`/`1`, exact float `0.0`/`1.0` → `False`/`True`.
- Blocked (unknown/`None`, fail-closed): every string — including `"true"`, `"false"`, `""`, whitespace, `yes`/`no`/`on`/`off`, arbitrary strings — plus objects, arrays, `null`, and any non-`0`/`1` numeric value (`2`, `-1`, `2.5`, …). No generic truthiness is used for untrusted lifecycle inputs.

The hypothetical planner that reasons about a unified outcome is pure:
`archived = json_archived or core_archived`; `pinned = (json_pinned or core_pinned) and not archived`. Inputs are first strict-normalized as above; unknown (`None`) is then treated as `False` for the hypothetical. There is no write-through.

## Explicit default-off, aggregate-only, read-only restriction

- Master gate: `experimental.unified_session_db` defaults to `false`.
- Mode: `experimental.unified_session_metadata_mode` defaults to `off` (`off | shadow | sync`). Invalid/missing/master-disabled fails closed to current behavior (`off`).
- `sync` mode does not write in this PR; it is reserved for a future slice.
- Shadow mode (`unified_session_db: true` + `unified_session_metadata_mode: shadow`) is an intentionally dormant config surface in this remediation: the flag/mode keys remain valid and default-off, but `all_sessions()` performs no automatic comparison, state-db scan, event, cache invalidation, or stream/poll refresh. Diagnostics are available only through the explicit offline entry points `api.session_metadata_sync.compute_aggregate_diagnostics` / `shadow_compare` and `scripts/audit_session_metadata_sync.py` (which fail closed without an explicit non-empty `--profile`). This removes the prior `all_sessions()` hot-path read on the `static/sessions.js:startStreamingPoll()` 30 s poll that violated the no-stream-driven-refresh contract.
- The offline audit (`scripts/audit_session_metadata_sync.py`) is the only external surface for diagnostics. It requires explicit `--session-dir` and `--state-db` fixture/test paths (fail-closed); it never targets live user state by default. Default output is aggregate-only human-readable text; `--json` emits machine-readable aggregate JSON. Explicit invocations never output IDs, titles, prompts, messages, or transcript text. Its `--help` contains no `--apply`/`--yes`.
- There is no `apply`/`yes`/`migration`/`repair`/`tombstone`/`delete`/`write-through` command/mode/behavior in this PR (no prohibited execution path; the document mentions migration only to negate it).

## No write/delete/migration in this PR

No UI/API output is added. No sidecar/index/`state.db` writes, lifecycle events, cache invalidation, stream refresh changes, title/transcript sync, migration, repair, tombstone, delete, or write-through behavior exists in this slice. Unknown or ambiguous field/schema/profile/active-state facts are classified as aggregate `blocked` categories (fail closed). In this slice the single current rule is: JSON is the comparison input/local existing representation; for matched provenance-verified core-backed lifecycle rows `state.db` is authoritative for `pinned`/`archived`; transcripts and WebUI-only compatibility state stay JSON-owned; `all_sessions()` runtime behavior is otherwise unchanged.

## Dormant adapter note — superseded/ historical (retained for context)

The historical adapter description below is retained for context and is superseded by the
current slice's authority rule above. Where it says "JSON sidecar remains authoritative
for ... archive and pin state," that is the pre-498 general spike framing; the current
498 shadow slice narrows it: JSON is the comparison input, and for matched
provenance-verified core-backed rows `state.db` is authoritative for `pinned`/`archived`.
`api.webui_session_db.WebUIJsonSessionDB` remains dormant; the feature flag above still
gates any runtime use.

---

# Unified SessionDB Adapter Spike (prior — historical, superseded by § "Full source of truth split (current rule for this slice)")

WebUI currently persists conversations as JSON files under the WebUI session
directory, while the CLI uses its own session database. The first safe slice of
unification is a dormant adapter that presents a small SessionDB-shaped API over
the existing WebUI JSON files without changing runtime call sites or file
format.

## Adapter Contract

`api.webui_session_db.WebUIJsonSessionDB` exposes:

- `list_sessions()` returns compact metadata rows for persisted WebUI JSON
  sessions.
- `read_session(sid)` returns a full session JSON payload or `None`.
- `update_metadata(sid, fields)` writes only allowlisted metadata fields and
  rejects unsafe keys such as `session_id`, `messages`, `tool_calls`, and
  `message_count`.
- `archive(sid, archived=True)` is a convenience metadata update for the
  archived flag.
- `write_session(session)` exists for tests and migration experiments that need
  to materialize a complete JSON payload.

Read operations must not call `Session.load()` or `all_sessions()`, because
those paths can repair indexes or transcripts. Metadata writes must load the
complete JSON payload, verify that a real `messages` list is present, update only
safe fields, recompute `message_count`, and atomically replace the file. The
adapter must never write a metadata-only stub that could drop transcript
messages.

## Why JSON-Backed And Dormant

The selected first slice is infrastructure only. Keeping the adapter backed by
the current JSON sidecars validates the API shape while preserving all current
WebUI behavior, backups, and import paths. The feature flag defaults to:

```yaml
experimental:
  unified_session_db: false
```

No UI exposes this flag, and no runtime session route switches to the adapter in
this slice.

## Runtime Wiring Preconditions

Before any route uses this adapter for live metadata changes, a follow-up PR must
prove parity with the existing `Session.save()` path:

- take the same per-session mutation locks used by streaming and session routes,
  so metadata writes cannot replace a newer transcript with a stale copy;
- refresh or invalidate the in-memory `Session` cache and `_index.json`, so
  sidebar rows and later `Session.save()` calls cannot overwrite adapter changes;
- match `Session.compact()` sidebar semantics for pending first turns,
  `has_pending_user_message`, `pending_started_at`, and real non-tool
  `last_message_at` ordering.

Until those invariants are implemented, `update_metadata()` and `archive()` are
test/migration helpers, not runtime persistence replacements.

## Planned Migration Sequence

1. Land the dormant JSON adapter and contract tests.
2. Add parity tests that compare adapter reads with existing WebUI sidebar and
   session payloads.
3. Introduce an opt-in dual-read or shadow-read mode for development builds.
4. Add a migration path that can write unified SessionDB records without
   deleting or rewriting JSON sidecars.
5. Switch selected call sites behind the flag only after parity and rollback
   behavior are proven.
6. Make the unified store authoritative in a later release after import,
   archive, pin, profile, project, and recovery semantics match WebUI JSON.

## Authoritative Fields And Open Questions — historical spike framing (superseded for lifecycle)

This section is historical spike framing, superseded for lifecycle by the current rule above
(§ "Full source of truth split (current rule for this slice)"). The historical text said:

The JSON sidecar remains authoritative for `messages`, `tool_calls`, metadata
display fields, profile/project ownership, archive and pin state, token/cost
totals, pending stream recovery fields, worktree metadata, and composer draft
state during this spike.

In the current 498 slice that archival sentence is narrowed: for matched provenance-verified
core-backed rows, `state.db` lifecycle (`pinned`/`archived`) authority from the current table
applies; JSON remains authoritative for transcripts and WebUI-only metadata as described there.

Open questions for later slices:

- Whether `updated_at` should reflect metadata-only changes such as archive and
  pin operations or only transcript changes.
- How to resolve conflicts when CLI and WebUI update titles, archive state, or
  project/profile ownership concurrently.
- Whether imported CLI sessions remain read-only projections or become editable
  unified records.
- How unified records should preserve WebUI recovery safeguards such as backup
  creation before transcript shrinkage.
- Which store owns sidebar ordering once JSON and SessionDB records coexist.

## Out Of Scope

This spike does not switch runtime WebUI call sites, migrate existing session
files, expose a UI setting, alter CLI storage, change session import behavior, or
remove any JSON sidecars. It is a contract and safety test bed for future
migration work.
