# Unified SessionDB Adapter Spike

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

## Authoritative Fields And Open Questions

The JSON sidecar remains authoritative for `messages`, `tool_calls`, metadata
display fields, profile/project ownership, archive and pin state, token/cost
totals, pending stream recovery fields, worktree metadata, and composer draft
state during this spike.

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
