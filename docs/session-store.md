# Session store: SQLite backend, migration, and rollback

WebUI keeps every chat in a *session store*. The default backend is one JSON
sidecar per chat (`<session_dir>/<session_id>.json`). This document covers the
optional SQLite backend, which moves the same data into a single
`<session_dir>/sessions.db`, how it is activated, how to migrate an existing
directory, how the two representations coexist, and how to roll back.

The backend contract itself lives in [`api/session_store.py`](../api/session_store.py);
the implementations are [`api/webui_session_db.py`](../api/webui_session_db.py)
(JSON), [`api/webui_session_sqlite.py`](../api/webui_session_sqlite.py) (SQLite),
and [`api/webui_session_postgres.py`](../api/webui_session_postgres.py)
(Postgres).

## What the SQLite backend changes

- Everything one chat needs lives in one SQLite database: the session row, its
  messages, tool calls, context messages, and the durable writer token
  (`generation`, `incarnation`). Database-level write fencing replaces the JSON
  backend's last-writer-wins files.
- Draft autosave (`POST /api/session/draft`) writes only the session **row**, not
  the transcript, so typing in a large chat no longer serializes the whole
  conversation on every debounce tick.
- A chat that has not been migrated still resolves to its JSON sidecar; the two
  representations coexist per session (see [Mixed directories](#mixed-directories)).

Nothing changes for a default install: with no `sessions.db` present the JSON
backend keeps serving every session exactly as before.

## Backend selection

`get_session_store()` ([`api/models.py`](../api/models.py)) resolves the backend
on each access, in this order:

1. **Postgres** when `HERMES_SESSION_STORE_PG_DSN` is set and the configured
   database carries valid cutover markers. If the DSN is set but the database is
   inactive or unreadable, WebUI refuses to silently fall back to a different
   backend — it raises instead, so a misconfigured deployment cannot write to the
   wrong place.
2. **SQLite** when `<session_dir>/sessions.db` exists *and* is active.
3. **JSON sidecars** otherwise.

SQLite activity is fail-closed and marker-based
([`WebUISqliteSessionDB.is_active`](../api/webui_session_sqlite.py)):

- `created_by = app` or `created_by = legacy` → active.
- `created_by = migration` → active **only** after `migration_complete = 1`.
- Missing, unreadable, or unrecognized markers → inactive.

That is why an interrupted migration cannot leak authority: a half-written
`sessions.db` is ignored until the migration stamps completion.

## Migrating a JSON session directory

Run the migration while WebUI is stopped. It is resumable and fails closed at
every step, and the default mode is a **dry run that does not touch your state**.

```bash
# 1. Dry run: stages into a temporary directory and verifies the copy.
python3 scripts/migrate_sessions_to_sqlite.py <session_dir>

# 2. Execute: copy, verify, publish, then retire the migrated sidecars.
python3 scripts/migrate_sessions_to_sqlite.py <session_dir> --commit
```

`<session_dir>` is the sessions directory inside the WebUI state directory
(`$HERMES_WEBUI_STATE_DIR/sessions` unless you overrode it). `--db-name` renames
the database (default `sessions.db`).

What `--commit` does, in order, and why each step is safe:

1. **Copies** every `*.json` sidecar into a staged database `<db-name>.migrating`.
   Each sidecar is identified by a content fingerprint taken at copy time, so a
   sidecar that changed underneath the migration is skipped rather than silently
   importing stale bytes.
2. **Verifies** the staged copy against the source (row counts, message counts,
   fingerprints), then stamps `migration_complete`. A staged database without
   that stamp has no authority.
3. **Publishes** with an atomic rename to `<db-name>` while holding the migration
   lock. WebUI writers take a *shared* cutover lock around each sidecar write, and
   the publish window takes the same lock *exclusively*, so no writer can slip a
   write between the final source check and the publication.
4. **Retires** each migrated sidecar into `<session_dir>/json-backup/<session_id>.json`
   (dedicated composer drafts go to `<session_id>.draft.json`). Retired files are
   the rollback source, not garbage.
5. **Restarts clean**: the next WebUI start sees an active `sessions.db` and
   serves the migrated sessions from it.

This tool migrates JSON → SQLite only. The Postgres backend is selected through
`HERMES_SESSION_STORE_PG_DSN` (see [Postgres notes](#postgres-notes)), but there is
no JSON → Postgres path here: provision that database with your own procedure and
verify it against the contract tests in
[`tests/test_session_store_contract.py`](../tests/test_session_store_contract.py).

## Mixed directories

Both representations are valid at once, and each session resolves independently:

- A session with a row in the active store is served from the store.
- A session with no row is served from its JSON sidecar, exactly as it was
  before — this is what makes an interrupted or partial migration safe, and what
  lets you migrate a large directory without a flag day.
- Loads prefer the store; **writes follow the same authority a load would use**.
  A draft written for a session that lives only in a sidecar goes to that
  sidecar, never into a store row it does not own.

The one thing to avoid is editing a sidecar after its session has been migrated:
the store row is authoritative and the sidecar is stale. The migration moves
migrated sidecars into `json-backup/` precisely so that cannot happen by
accident.

## Rollback

Rollback is per directory, and it reverses the publish rather than a live DB:

1. Stop WebUI.
2. Move `sessions.db` (and its `-wal`/`-shm` siblings) out of the sessions
   directory — e.g. `mv sessions.db sessions.db.rollback`. With no active
   database, the selector falls back to JSON on the next start.
3. Restore the sidecars: move `<session_dir>/json-backup/*.json` back into
   `<session_dir>/`. Draft overlays live in `<session_dir>/.drafts/`; a retired
   `<sid>.draft.json` is the overlay for `<sid>`, so copy it in as
   `.drafts/<sid>.json`.
4. Start WebUI and confirm the sidebar lists your chats.

Sessions that were *created* after the migration have no `json-backup/` entry.
They live only in the database, so export or copy them before deleting
`sessions.db` if you want to keep them.

## Postgres notes

- Activate by setting `HERMES_SESSION_STORE_PG_DSN` to a database WebUI owns.
- The same cutover markers and the same fail-closed rule apply: an inactive or
  unreadable database raises rather than falling back.
- Rollback is the same shape: unset the DSN (or point it elsewhere), restore the
  JSON sidecars, restart.

## Verifying a change to this subsystem

```bash
./scripts/test.sh tests/test_webui_session_sqlite.py \
  tests/test_session_store_contract.py \
  tests/test_migration_staged.py \
  tests/test_composer_draft_sidecar_regressions.py
```

Contract-level behavior (leases, phases, writer tokens, fallback semantics) is
documented in the `SessionStore` docstrings; behavior changes here should add a
regression test that fails before the fix.
