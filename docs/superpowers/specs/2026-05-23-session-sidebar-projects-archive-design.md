# Session Sidebar Projects And Archive Design

Date: 2026-05-23

## Goal

Redesign the Chat sidebar session list into a global session index organized by
workspace projects, with a separate Chats section for general conversations.
The sidebar should make cross-agent session history easy to browse without
making startup or long-term usage progressively noisier.

## User Requirements

- The sidebar must show sessions across agents/profiles, not only sessions for
  the active agent.
- Opening a session must continue with that session's saved agent/profile. The
  active agent is only the default for creating new sessions.
- Workspace-backed sessions belong under Projects, grouped by workspace.
- General sessions that are not tied to a workspace/project belong under Chats.
- Agent identity in session rows should be shown by the existing agent avatar,
  not by visible profile-name badges.
- Projects must be collapsible.
- Each Project and the Chats section should show a flat, newest-first list of
  current sessions.
- The only session subsection inside a Project or Chats should be Archive.
- Sessions untouched for 7 or more days should move into the Archive display
  subsection by default.
- The age-Archive cutoff should be configurable from Preferences, defaulting to
  7 days.
- Archive must be collapsed by default and lazy-loaded so old histories remain
  reachable without loading every row at boot.
- The UI should reuse existing WebUI assets, styles, avatars, and collapsible
  subsection treatment.

## Approved UX Direction

Use a Codex-like sidebar structure with Hermes styling:

- Top-level section: `Projects`.
- Each Project row represents a workspace path with a friendly display name and
  folder affordance.
- Project rows are collapsible and persist their expansion state per browser.
- Expanded Projects show current sessions directly beneath the project row.
- Top-level section: `Chats`.
- Chats shows current general sessions directly beneath the section label.
- Each Project and Chats may have one collapsible `Archive` subsection.
- Archive uses the existing session date-group header/caret styling, not a
  second folder-like row.
- Session rows show:
  - agent avatar on the left,
  - session title in the middle,
  - relative age on the right.

Do not add Today, Yesterday, This week, or Last week subsections inside the new
Project/Chats grouping. The relative age shown on each row already carries that
information.

## Terminology

`Project`

: A sidebar group derived from a workspace path. It is not the same as the
  existing manual WebUI Project label.

`Chats`

: The sidebar group for conversations without workspace/project grouping.

`Archive`

: A virtual display subsection for old sessions in a Project or Chats group.
  This must not set or depend on the persisted `session.archived` flag.

`Current session row`

: A non-archived-by-age session shown directly under a Project or Chats group.

`Age-archive row`

: A session row hidden under the virtual Archive subsection because it has not
  been interacted with for the configured Archive cutoff.

## Session Grouping Rules

Group identity is based on explicit session workspace state:

- A session with a non-empty workspace path belongs to the Project for that
  normalized workspace path.
- A session without workspace grouping belongs to Chats.
- Workspace display names should prefer saved workspace names when available.
- If no saved name exists, use the workspace directory basename.
- If two workspaces would display the same name, disambiguate with parent path
  context in the UI tooltip or secondary metadata.
- Missing or inaccessible workspace paths should still appear as Projects if
  sessions reference them. Do not hide or mutate sessions because the path is
  currently unavailable.

Existing manual WebUI Projects should be preserved as labels or secondary
metadata. They should not be the primary source of the new Projects section.

## General Chat Creation

The new sidebar should make the user's intent explicit:

- Creating from a Project starts a session grouped to that workspace.
- Creating from Chats starts a general session without workspace grouping.
- Creating from the global `+` defaults to Chats unless the UI is clearly scoped
  to a specific Project.
- The active agent/profile supplies defaults for new sessions only.

Implementation may still resolve a runtime working directory internally when an
agent requires one, but a general chat should not be grouped into a workspace
Project merely because the active profile has a default workspace.

## Agent/Profile Behavior

The sidebar is a global index across profiles:

- Browsing the Chat sidebar should not be filtered by active profile.
- Opening a session uses the session's persisted `profile`.
- Sending the next message in an opened session continues with that saved
  profile unless the session is explicitly retagged by an existing supported
  flow.
- The active profile remains the default for brand-new sessions.
- The existing all-profiles toggle should not be needed for this sidebar view.

Use existing profile avatar rendering helpers for row identity. Profile names
may remain in tooltips, accessibility labels, and detailed metadata, but not as
visible row badges.

## Archive Rules

Archive is a virtual age bucket:

- Compute session activity from `last_message_at`, falling back to `updated_at`,
  then `created_at`.
- A session is age-archived when its activity time is at least the configured
  Archive cutoff older than server now.
- The age cutoff is a display decision only. It must not write
  `session.archived=true`.
- Pinned, unread, streaming/running, and currently open sessions stay visible in
  the current list even when older than the Archive cutoff.
- Manual archived sessions keep existing manual archive semantics. They should
  remain outside the normal current/age-archive lists unless the UI explicitly
  asks to show manually archived sessions.

Archive collapse state should be keyed by group:

- `workspace:<normalized path>` for Projects.
- `chats` for Chats.

## Settings Contract

Keep settings changes narrow. This feature should not become a broad Preferences
or Appearance redesign.

Existing settings to preserve:

- `show_cli_sessions`: the new session index should respect this before
  grouping. When enabled, non-WebUI sessions should group by workspace if their
  metadata provides one, otherwise under Chats.
- `show_previous_messaging_sessions`: apply the existing messaging-session
  replacement/dedupe behavior before grouping.
- `pinned_sessions_limit`: keep the current global pin limit. Pinned sessions
  remain current-list exceptions even when older than the Archive cutoff.
- `sidebar_density`: keep controlling row metadata density. Compact rows show
  avatar, title, and relative age. Detailed rows may show message count, model,
  source, lineage, manual project label, or read-only state, but must not show
  visible profile-name badges. Profile names may remain in tooltips,
  accessibility labels, and diagnostic metadata.
- `session_jump_buttons` and `session_endless_scroll`: keep these scoped to
  reading a conversation transcript. They should not control sidebar Archive
  lazy-loading.
- `avatar_presence_layout`: keep scoped to where the active assistant avatar
  appears in the chat/composer. Session rows should always use the existing
  profile/avatar rendering path regardless of this setting.
- `settingsWorkspacePanelOpen`: keep scoped to workspace/file-panel visibility
  for newly opened sessions. It should not decide whether a session belongs to
  Projects or Chats.

Add one new Preferences setting:

```text
session_archive_after_days
```

UI label:

```text
Archive inactive sessions after
```

Allowed values:

```text
7, 14, 30, 90
```

Default:

```text
7
```

Recommended helper copy:

```text
Older sessions move into the collapsed Archive section in the sidebar. This
does not manually archive or delete conversations.
```

Place this near the existing session/sidebar controls in Preferences, alongside
Sidebar density, pinned conversations limit, and non-WebUI session visibility.
Do not add settings for active-profile-only browsing, auto-loading Archive
contents, hiding Projects, or replacing avatar identity with visible agent
names.

## Sidebar Data Contract

Prefer a dedicated sidebar index endpoint rather than changing the historical
profile-scoped behavior of `/api/sessions`.

Suggested endpoint:

```text
GET /api/session-index
```

Suggested response shape:

```json
{
  "groups": [
    {
      "group_id": "workspace:/home/user/project",
      "kind": "project",
      "name": "project",
      "workspace": "/home/user/project",
      "current_count": 5,
      "archive_count": 31,
      "sessions": []
    },
    {
      "group_id": "chats",
      "kind": "chats",
      "name": "Chats",
      "current_count": 8,
      "archive_count": 44,
      "sessions": []
    }
  ],
  "server_time": 1779560000.0,
  "server_tz": "+0200"
}
```

Each `sessions` item should be compact sidebar metadata only. It must include
enough data to render and open the row:

- `session_id`
- `title`
- `profile`
- `workspace`
- `last_message_at`
- `updated_at`
- `created_at`
- `message_count`
- `pinned`
- `archived`
- effective `session_archive_after_days` or enough server-side classification
  data for the UI to understand the current cutoff
- runtime state needed for streaming/running indicators
- unread state if currently available to the sidebar

The initial index response should include:

- all Project/Chats group summaries,
- current rows for each expanded/default-visible group,
- current-row counts,
- archive counts,
- active/running/pinned/unread exception rows needed to keep context visible.

It should not include every old archive row.

## Lazy Archive Loading

Expanding an Archive subsection fetches older rows for only that group.

Suggested endpoint:

```text
GET /api/session-index/archive?group_id=<id>&limit=50&cursor=<cursor>
```

The response should include compact session metadata, `next_cursor`, and a total
or remaining count. Cursor pagination should be based on activity timestamp plus
session id so inserts or updates do not make paging unstable.

Archive expansion behavior:

- First click fetches the first page for that group.
- Additional pages load through a compact `Load more` row.
- Collapse keeps already loaded rows in memory for the current page session.
- A refresh can discard loaded archive rows and keep only counts; correctness
  matters more than preserving old expanded DOM.
- If the active session is inside Archive, that Archive subsection should open
  or include the row so the user does not lose their place.

## Rendering Design

Frontend rendering should be an evolution of `static/sessions.js`, not a new UI
framework.

Reuse:

- existing sidebar colors, spacing, typography, and hover behavior,
- existing profile avatar markup and reactive/avatar shape helpers,
- existing `.session-item` row behavior where practical,
- existing collapsible section header/caret styling for Archive,
- existing session actions, rename, pin, manual archive, delete, and batch flows.

Change:

- Replace the project chip filter bar with workspace Project grouping.
- Stop relying on active-profile filtering for the Chat sidebar index.
- Render a flat current list under each expanded Project/Chats group.
- Render only Archive as a nested collapsible subsection.

Persisted UI state:

- Project collapsed state in localStorage, keyed by normalized workspace path.
- Archive collapsed state in localStorage, keyed by group id.
- Existing user row interactions such as rename and action menus should continue
  to defer or protect list re-renders.

## Search And Access

Search should preserve access to old sessions without forcing archive rows into
the first payload.

Default behavior:

- Search current loaded rows immediately.
- Show a clear affordance to search Archive when the query has no current match
  or when the user explicitly includes archived history.

Archive search behavior:

- Search should query compact metadata first.
- Content search can remain a separate deeper operation if it is already more
  expensive.
- Search results from Archive should show their Project/Chats group and agent
  avatar, and opening one should keep the saved profile.

## State Layers And Invariants

State read by this feature:

- session metadata index,
- in-memory active session/runtime state,
- profile metadata for avatars,
- workspace names from saved workspace lists where available.

State mutated by this feature:

- localStorage collapse preferences,
- session workspace grouping only when the user creates or explicitly moves a
  session between Project and Chats,
- no automatic mutation for the age-based Archive bucket.

Invariants:

- Age Archive must not write `session.archived`.
- Opening a cross-profile row must not switch or rewrite the session profile.
- Manual archived sessions retain existing restore/archive semantics.
- The initial sidebar fetch must stay bounded by current rows and counts, not by
  total historical session count.
- Full session messages still load through the existing session-open path, not
  through the sidebar index.

## Error Handling

- If archive lazy loading fails, keep the Archive subsection visible with a retry
  affordance and do not clear current rows.
- If avatar metadata is missing, use the existing fallback avatar behavior.
- If a workspace path cannot be normalized, group by the stored path string and
  keep the session openable.
- If counts become stale after a session update, the next session-list refresh or
  SSE update should reconcile the group counts.
- If an old session becomes active, unread, pinned, or streaming, it should move
  out of Archive on the next sidebar refresh.

## Testing And Validation

Automated backend tests should cover:

- session index groups rows across profiles,
- sessions open with their saved profile data,
- `session_archive_after_days` defaults to 7 and accepts only the supported
  cutoff values,
- workspace sessions group under Projects,
- no-workspace sessions group under Chats,
- manual `session.archived` is not used for age Archive,
- the configured cutoff uses `last_message_at`, then `updated_at`, then
  `created_at`,
- pinned/unread/running/active exceptions remain in current rows,
- archive counts are returned without archive row payloads,
- archive pagination returns stable pages.

Automated frontend or source-level tests should cover:

- active profile is not used as a sidebar visibility filter in the new index
  path,
- project collapse state is keyed by workspace group,
- Archive is the only subsection inside a group,
- Archive uses existing collapsible subsection classes/patterns,
- avatar rendering uses existing profile avatar helpers,
- cross-profile session rows do not render profile-name badges.
- transcript settings such as `session_endless_scroll` do not affect sidebar
  Archive lazy-loading.

Manual/browser validation should cover:

- desktop, narrow, and mobile sidebar layouts,
- multiple Projects with collapsed and expanded states,
- Chats with current and archived rows,
- Archive lazy load and pagination,
- opening sessions from another agent/profile,
- creating from Project versus creating from Chats,
- missing workspace path display,
- large synthetic history where startup does not render or fetch hundreds of old
  rows.

## Out Of Scope

- Deleting old sessions automatically.
- Automatically setting or clearing the existing manual `session.archived` flag.
- Full redesign of content search.
- Rewriting historical sessions to guess whether their workspace was intentional.
- Removing existing manual project labels in this change.
- Adding a frontend framework, build step, or new dependency.
