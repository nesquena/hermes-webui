# Native project read-through compatibility

## Purpose and current behavior

PR1 adds a compatibility **read path** from Hermes WebUI to Hermes Agent's
per-profile native project store. It makes active-profile native projects visible
through the existing project list and classifies ordinary imported Agent sessions
from their persisted working directory (`cwd`). It does not migrate project data,
create native projects, or add any native write path.

The backend read-through and the UI/API safety rules described below are present
in this source slice. Native rows are visible and filterable, but they do not gain
access to legacy project mutations or session-assignment paths.

## Authority and identity model

The two project stores have separate authority in PR1:

- `projects.json` remains the WebUI-owned, writable registry for legacy session
  groups and their assignments.
- Each profile's existing `projects.db` is the read authority for native project
  visibility and path classification. WebUI does not own or modify this database.
- A persisted `state.db.sessions.cwd` is evidence used to classify an imported
  session. It is not rewritten when classification occurs.

A native row keeps the Agent project ID in both `project_id` and
`native_project_id`, and carries `project_source: "hermes-agent"` plus
`read_only: true`. Merge identity is `(canonical profile, project_id)`; root-profile
aliases canonicalize to the same profile identity. A legacy row with the same
identity wins.

Folder matching is not reimplemented in WebUI. The adapter delegates each
non-empty path to `hermes_cli.projects_db.project_for_path()`, so the Agent's
matching and longest-folder rules remain authoritative.

## Read path and data flow

### Project list

1. `GET /api/projects` reads legacy rows from `projects.json` and scopes them to
   the active profile.
2. `api/projects_db_adapter.py` validates the requested profile and existing
   `projects.db`, imports `hermes_cli.projects_db` optionally, and opens one fresh
   SQLite connection for the operation.
3. The connection uses SQLite URI `mode=ro`, `PRAGMA query_only=ON`, and one
   explicit transaction so the project rows and folders come from one snapshot.
4. Active, non-archived Agent DTOs are mapped to the WebUI response shape.
5. Native rows that do not collide are appended after legacy rows. Legacy order is
   preserved.

### Imported-session membership

1. `api/agent_sessions.py` checks the installed `state.db` schema. When the
   `sessions.cwd` column exists, it projects that value into importable rows; when
   it does not, `cwd` is safely projected as `None`.
2. Continuation/compression projection carries the selected importable tip's
   `cwd`, including an explicit `None`, rather than retaining a stale head value.
3. `api/models.py` batches distinct, non-empty `cwd` values from ordinary imported
   sessions and resolves them in one adapter operation.
4. Valid matcher results populate the projected session's `project_id`. Cron and
   webhook grouping keeps its existing precedence, Kanban remains unassigned by
   this path, and system-only passes skip native matching.
5. All-profile scans disable native membership mapping because PR1 exposes native
   state only for the active profile.

The single-profile imported-session cache key includes a stat-only fingerprint of
`projects.db` and a non-empty `projects.db-wal`. It does not open SQLite, excludes
`projects.db-shm`, ignores an empty WAL, and does not follow symlink targets. The
existing content-plus-stat fingerprint behavior for `state.db` is unchanged.

## Profile, path, and failure-safety boundaries

- Profile names must pass the existing profile identifier validation. Isolated
  mode accepts only its pinned profile.
- Named-profile homes must resolve to the expected entry inside the profiles root.
  A profile symlink may resolve within that root, but not outside it.
- The `projects.db` leaf must already exist, be a regular file contained by the
  resolved profile home, and not itself be a symlink.
- Reads use a short-lived read-only/query-only connection and one transaction per
  list or path-batch operation. The adapter never calls an upstream connection
  helper that could initialize or migrate the store.
- A missing Agent module, missing database, busy/corrupt/incompatible database,
  incompatible DTO, matcher failure, or malformed matcher result fails closed.
  Legacy projects and imported sessions remain available; affected ordinary
  sessions remain unassigned.
- Path-matching failures do not log submitted paths. A failed batch discards
  partial matches rather than returning a mixed result.
- PR1 does not create `projects.db`, migrate a schema, write project records,
  update folder mappings, or rewrite historical `cwd` values.

These checks protect the intended profile boundary and avoid following a crafted
DB leaf. They are not a claim to eliminate every same-user filesystem race.

## Compatibility behavior

- Legacy rows retain their existing order and win native collisions for the same
  canonical profile and project ID.
- Native rows are read only for the active profile. `all_profiles=1` remains a
  legacy-only aggregate, and `other_profile_count` remains derived only from
  legacy rows.
- No `projects.db`, an older Agent without `hermes_cli.projects_db`, an unsupported
  database/schema, or an unavailable backend leaves existing legacy behavior in
  place.
- Older `state.db` schemas without `sessions.cwd` continue to import sessions with
  no cwd-derived native assignment.
- Older supported Agent project APIs that omit the `include_archived` keyword are
  called with their compatible signature. Archived native projects are not
  exposed by PR1.

## User-visible limitations in PR1

PR1 treats native projects as read-only compatibility rows:

- They remain selectable as project filters.
- They cannot be renamed, recolored, deleted, assigned to sessions, or used by
  quick-create flows that mutate legacy project state.
- Selecting a native filter does not stamp that native project ID onto a newly
  created session. PR1 classification applies to imported Agent sessions through
  persisted `cwd` only.
- New-session assignment is checked in both the browser and
  `POST /api/session/new`. The server accepts only a canonical project ID for a
  known, writable legacy row in the active request profile. Native-only,
  read-only, malformed, unknown, and foreign-profile IDs fail before workspace,
  worktree, memory-lifecycle, or session-creation side effects. An absent or
  explicit `null` project ID retains unassigned-session compatibility.

## Staged migration and follow-ups

### PR1: read-through compatibility

Keep both stores in place. Read active-profile native projects and classify
ordinary imported sessions without changing either native or legacy records.

### PR2: native project-backed creation

Add explicit new-session creation from a native project using one of that
project's **backend-visible folder paths** as the session workspace/runtime `cwd`.
Membership should then be derived by the same upstream path matcher. Do not infer
that a browser-visible or host-only path is valid inside a remote/container
backend.

### PR3: native writes and explicit migration

Add native create/update/archive/folder operations only through an Agent-owned
write API with explicit capability and version checks. Any legacy-to-native
migration must be user-invoked, previewable, idempotent, profile-scoped, and
restart-safe, with a durable record of completed mappings. Reads must never
implicitly trigger migration.

## Migration preflight principles

Before PR2 or PR3 changes authority or placement:

- Do not map `/opt/data/workspace` (or another broad workspace root) as one project
  folder. Register the narrow backend-visible project directory, such as
  `/srv/projects/widget`, to avoid classifying unrelated sessions together.
- Do not automatically rewrite historical `state.db.sessions.cwd` values. Preserve
  history; offer an explicit, reviewed repair only when path translation is known.
- Do not mass-move workspace directories without auditing references and
  dependencies, including session sidecars, worktrees, cron/webhook/Kanban jobs,
  repository remotes, mounts, automation, and external tooling.
- Verify every proposed folder from the runtime that will execute the session.
  Host-visible and backend-visible paths are not interchangeable.

## Rollback

PR1 is reversible because it does not mutate native or legacy project records:

1. Downgrade or remove the read-through adapter/route/session-classification code.
2. Restart the WebUI and, if operationally necessary, clear only derived in-memory
   session/project caches so legacy projections rebuild immediately.
3. Keep `projects.json`; it remains the intact legacy writable registry.
4. Do **not** delete, rename, truncate, or recreate `projects.db` as rollback. It is
   Agent-owned state and may predate WebUI compatibility.
5. Do not rewrite `state.db` or historical session `cwd` values; PR1 did not change
   them.

After rollback, native-only filters and cwd-derived membership disappear from the
WebUI, while legacy project groups and assignments continue unchanged.

## Verification matrix

| Contract | Focused coverage |
|---|---|
| Read-only adapter, DTO mapping, profile/path containment, symlink rejection, snapshot behavior, upstream matcher delegation, no store creation, fail-closed logging | `tests/test_issue5763_projects_db_adapter.py` |
| Active-profile route merge, legacy order/collision precedence, legacy-only all-profile response and count | `tests/test_issue5763_native_project_route.py` |
| Optional `state.db` cwd projection and continuation-tip cwd selection | `tests/test_issue5763_agent_session_cwd.py` |
| Batched ordinary-session mapping, system-project precedence, malformed/failing matcher behavior, all-profile disablement, path-log privacy | `tests/test_issue5763_native_project_membership.py` |
| Stat-only `projects.db`/non-empty-WAL cache fingerprint, SHM exclusion, no SQLite open/creation, no symlink following; unchanged `state.db` fingerprint regression coverage | `tests/test_cli_sessions_cache_fingerprint.py` |
| Read-only native chips and mutation/assignment guards, exact project-list provenance, canonical IDs and profiles, explicit/implicit new-session behavior, and profile-switch races | `tests/test_issue5763_native_project_ui.py`, `tests/test_issue5763_final_authorization_boundaries.py` |
| Server-side new-session project authorization, active-profile binding, legacy collision precedence, and pre-side-effect rejection | `tests/test_issue5763_server_project_authorization.py` |

Run the focused compatibility matrix with:

```bash
./scripts/test.sh \
  tests/test_issue5763_projects_db_adapter.py \
  tests/test_issue5763_native_project_route.py \
  tests/test_issue5763_agent_session_cwd.py \
  tests/test_issue5763_native_project_membership.py \
  tests/test_issue5763_native_project_ui.py \
  tests/test_issue5763_final_authorization_boundaries.py \
  tests/test_issue5763_server_project_authorization.py \
  tests/test_cli_sessions_cache_fingerprint.py
```
