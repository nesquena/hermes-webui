# Project Context MCP Contract

Hermes WebUI exposes a read-only MCP tool named `recent_project_messages` for
external agents that need bounded context from an existing WebUI project. The
tool is generic: it reads projects and sessions already managed by WebUI and has
no product-specific UI or mutation path.

## Scope and setup

Run `mcp_server.py` as documented in its module header. The MCP process is
scoped to one active Hermes profile. Use `--profile <name>` to choose a profile
when starting the process; callers cannot use a tool invocation to switch to or
read another profile.

The tool accepts:

- `project_id` (required): an ID returned by `list_projects` in the active
  profile.
- `profile` (optional): an assertion that must match the MCP process's active
  profile. A mismatch returns the same `Project not found` error as an absent
  project.
- `roles` (optional): `user` and/or `assistant`; defaults to `user`. `tool` and
  `system` are rejected so raw tool payloads and system prompts are never part
  of this contract.
- `limit` (optional): defaults to 5 and is capped at 20.
- `before` (optional): the opaque `next_before` value from a prior response.
- `include_archived` (optional): defaults to `false`. Set it to `true` to include
  sessions whose authoritative sidecar metadata is archived.

## Response and ordering

The response includes project/profile scope, selected roles, archive policy,
`partial`, classifier version, count-only diagnostics, `next_before`, and a
`messages` array. Every message contains only:

- `timestamp`
- `role`
- `content`
- `session_id`
- `session_title`
- `workspace`
- `profile`
- `project_id`

Messages are selected globally across every eligible project session, not by
choosing one recent session first. Results are newest-first using the stable
order `timestamp DESC, session_id DESC, message row ID DESC`. The database row
ID is used only inside the opaque cursor and is not exposed. Pass `next_before`
back as `before` to continue strictly after the last returned row in that order.

## Genuine-message classifier

Classifier `project_context_v1` excludes:

- sessions whose `state.db.sessions.source` is `cron`, `subagent`, or
  `delegation`;
- blank content;
- the Hermes max-tool-iteration summary request;
- user-visible synthetic prefixes for background-process wakeups, asynchronous
  delegation delivery, context compaction/compression, session-arc summaries,
  and `[System: ...]` scaffolding.

Only `user` and `assistant` content columns are selected; tool-call columns are
never read or returned. For each session, the classifier examines a hard-bounded
tail of `max(20, limit × 5)` rows plus one saturation sentinel. Synthetic rows
do not consume the requested result limit within that window. If the window is
all or mostly synthetic and older rows may remain, the response fails bounded:
it returns the genuine rows it can prove, sets `partial=true`, and increments
the count-only `classifier_scan_saturated_sessions` diagnostic rather than
scanning the full transcript.
The selected message content and session title then pass through the standard
credential redactor with redaction forced on for this external-agent surface.

## Membership, isolation, and partial reads

Project ownership must match the active profile. A session is eligible only
when all of these agree:

1. `projects.json` assigns the project to the active profile.
2. `sessions/_index.json` assigns the session to that project/profile.
3. The session sidecar exists and its bounded metadata prefix confirms the same
   session ID, project, and profile.
4. The active profile's `state.db.sessions` contains that session.

Missing, malformed, deleted, foreign-profile, unassigned, or conflicting rows
fail closed and contribute only count diagnostics. Diagnostics never contain
excluded session IDs or content. `partial` is true when a candidate cannot be
confirmed because a sidecar, database row/schema, read, bounded classifier
window, or timestamp is unavailable.
Missing or malformed session indexes are likewise partial, never a silently
complete empty result. Opaque cursors are bound to the project, profile, roles,
and archive policy that created them; cross-scope reuse is rejected.

The read path never mutates WebUI state, repairs indexes, loads `Session`
objects, or parses sidecar transcript arrays. It opens `state.db` in SQLite
read-only mode. After metadata confirmation, it issues one indexed, hard-capped
tail query per eligible session, then merges up to the requested number of
genuine candidates per session into the global result. This avoids N-session full
transcript scans while preserving correct global ordering.
