# Matrix Session Management Design

## Goal

Keep high-volume Matrix conversations out of the default Chat sidebar while
preserving a deliberate Matrix filter, and let users organize imported Matrix
sessions without mutating Hermes Agent's external `state.db` transcript store.
Add usable bulk conversation management through a persistent sidebar
multiselect dock.

## Constraints

- Matrix sessions are external/read-only conversations. WebUI must not start a
  writable chat turn or mutate their agent-owned transcript/state rows.
- WebUI-owned organization metadata may be persisted in the existing WebUI
  session sidecar store.
- The default Matrix visibility setting is off for new and existing users unless
  they explicitly enable it.
- An explicit Matrix source filter overrides the default hide, just as the
  existing Cron, Webhook, and Kanban filters do.
- Existing CLI, Cron, Webhook, Kanban, subagent, and other read-only ownership
  rules remain unchanged.
- The implementation stays within Python stdlib plus the existing vanilla
  JavaScript/CSS structure; no dependencies or build step are added.

## User experience

The Chat sidebar keeps its current source/project chips. Matrix receives a
dedicated source chip when Matrix rows are available. With the new preference
disabled, Matrix rows do not appear in the default All view, but selecting the
Matrix chip reveals them. The preference is subordinate to the existing
Show non-WebUI sessions control.

The session list gains a persistent bottom dock outside the scrolling history.
Idle mode shows Select. Select mode replaces it with Cancel, Select
All/Deselect All, a selected count, Archive, Move, and Delete. Actions remain
visible while scrolling and are disabled when no writable/organizable row is
selected. The dock reserves layout space and works at desktop and mobile
sidebar widths.

Imported Matrix rows are eligible for WebUI-local Archive and Move actions.
Those actions materialize or update a minimal sidecar containing organization
metadata and source/profile identity only. The external transcript remains
read-only and is still loaded from the agent store for viewing.

## Data flow and ownership

1. The agent/state-db projection identifies Matrix rows and marks them
   read-only.
2. The sidebar cache builder applies `show_matrix_sessions`, while preserving
   Matrix rows needed by an explicit `source_filter=matrix` request.
3. A Matrix move/archive request resolves the exact external row and active
   profile, validates the target project against that profile, and writes only
   the WebUI sidecar metadata under the normal session lock.
4. The next sidebar projection overlays the sidecar `project_id`/`archived`
   metadata onto the external row. No agent state-db update is performed.
5. Multiselect uses the same per-row eligibility and endpoint contracts as
   individual actions. It prunes stale or non-eligible IDs when the source,
   profile, project, archive, or search scope changes.

The authoritative values are: source/profile from the external row, project
authorization from the WebUI project store, and organization state from the
WebUI sidecar. The backend remains the final authority for all mutations.

## Error handling

- Unknown Matrix session: 404, with no sidecar created.
- Invalid or cross-profile project: 404, matching the existing project
  authorization behavior.
- External session cannot be resolved as Matrix: 403 and no mutation.
- Busy session lock: 503 with the existing retryable message.
- Partial multiselect operations retain existing per-item result/error behavior
  and report failure rather than claiming a successful batch.
- Ambiguous or missing ownership metadata fails closed; it does not fall back to
  the active profile to authorize a foreign project.

## Testing and evidence

- Backend regression tests prove Matrix rows are hidden by default, revealed by
  the explicit Matrix filter, and preserve sidecar organization metadata.
- Backend mutation tests prove Matrix move/archive never writes the agent
  `state.db` and rejects non-Matrix read-only rows.
- Frontend tests prove the docked multiselect DOM contract, read-only selection
  eligibility, stale-selection pruning, and batch action wiring.
- Run the focused repository test set through `./scripts/test.sh`, JavaScript
  syntax/runtime checks, `git diff --check`, and the browser smoke/layout
  coverage available in the repository.
- Capture desktop and narrow/mobile before/after evidence for the sidebar dock
  and Matrix visibility behavior before claiming completion.

## Out of scope

- Renaming or editing imported Matrix transcripts.
- Sending follow-up messages directly in an imported Matrix session; that
  remains a separate writable-fork UX decision.
- Mutating, migrating, or adding columns to Hermes Agent's `state.db`.
- General session virtualization/performance redesign beyond the existing
  sidebar implementation and multiselect dock.
