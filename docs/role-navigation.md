# Daily work navigation

## Role workspaces

`/api/governance/me` also returns `workspace_navigation`: a `kind` and the
permitted `primary` and `secondary` panel IDs. This is presentation derived
from resolved roles and effective permissions, not a second access policy.

- Members start with daily work. A blacklist policy alone does not put every
  permitted technical destination on the primary rail.
- Operators additionally get Insights when permitted.
- Engineers additionally get technical workspace destinations when permitted.
  An elevated access level selects the engineering presentation, while the
  existing whitelist/legacy availability contract still applies.
- Administrators retain all existing destinations. Explicit capability denies
  in blacklist mode still apply.

The existing `nav_audience` remains `member` for operators and engineers; this
does not change ownership, approval authority, or administrative identity.
Named engineer/operator roles extend the daily presentation only after each
feature's permission check. Unrecognized roles fall back to the member view.

Additional permitted destinations can be disclosed with **More tools** on both
desktop and mobile. This choice is saved per signed-in email. The active
destination remains visible when the disclosure closes; denied or module-gated
destinations never become visible through this preference. Administrators do
not need the disclosure. Technical roles default to expanded composer controls;
an explicit personal choice in Appearance still wins.

The workspace's visual shell uses Herm-inspired neutral surfaces, rounded
panels, compact controls and restrained navigation. SynthPulse identity and the
existing theme/skin choices remain. No Herm artwork, executable code, Mac-only
integration, or runtime was imported. Chat rendering, bots, projects, scheduled
tasks, approvals, files, artifacts, split views and integrations keep their
existing handlers and state owners. Projects is the single navigation destination
for projects and spaces. The existing merged-project folders and My work view
remain authoritative. Spaces and Kanban have no standalone rail, mobile-menu or
quick-navigation entries, including for administrators. Their internal routes
and backend permissions are retained; this change runs no data migration.

Validation includes `tests/test_workspace_navigation.py` and the isolated
`WORKSPACE SHELL` browser cases, alongside the existing blacklist navigation,
chat and bot roster cases. Browser evidence uses synthetic users, conversations
and a deterministic local model provider, never production customer data.

### Quick navigation

**Go to…** (Ctrl/Cmd+Shift+P) searches the existing allowed destinations, including
additional permitted tools while More tools is collapsed. Searches for spaces
or folders lead to Projects. This searches functions, not document content.
The dialog supports arrow keys, Enter, Escape and focus restoration. It uses
the existing panel-switch guard, so unsaved settings still prevent navigation.
Ctrl/Cmd+K keeps its existing new-conversation behavior.

The navigator fails closed until governance navigation is available, omits
hidden/disabled/module-gated destinations, and checks availability again when
opening a result. It does not issue its own API requests or store search terms.

### Conversation overview and session files

The grid button beside **New conversation** opens a full overview of the loaded
conversation list. It follows the existing bot, project, source and archive
filters, with additional local search and attention/running/unread/pinned
filters. Cards show bot and project attribution, read-only/current-chat labels
and the existing session status signals. No idle card is described as a
completed task. Counts explicitly describe matching loaded conversations;
the overview is not a search of the entire archive. Results are shown in
batches of 36 without additional requests. Cards are grouped by Needs attention,
Working, Unread and Recent, in that precedence order, preserving the canonical
order within each group. Filter counts use the loaded rows matching the search;
group counts describe the displayed cards. Overlapping signals remain visible.

`sessions.js` owns the filtered, lineage-folded projection. The overview receives
its sorted rows before date-group collapsing and virtual rendering, and opens
them through `_openSidebarSession`, preserving source import, profile resolution,
extension vetoes and backend authorization. A profile-switch skeleton clears
the overview immediately. Refresh errors show a stale-data notice and use the
existing retry path. No separate session polling or local history is added.
Returning to chat preserves the composer DOM and draft; opening a card or
starting a new conversation exits the overview. Keyboard focus survives live
row updates, and mobile navigation closes the drawer to reveal the overview.

The Artifacts tab now searches detected file paths and filters file extensions
into documents, images, code, data/spreadsheets and other files. It keeps using
`collectSessionArtifacts` and `openArtifactPath`, including the existing
50-file bound and existence/preview checks. The displayed count explains that
files come from loaded messages, not a complete filesystem scan. Filters survive
streaming redraws but reset when session, bot or workspace changes. They make
no API calls. The controls remain mounted so typing does not lose focus.

`conversation-workbench.spec.ts` exercises persisted conversation selection,
draft retention, live activity, keyboard focus, profile-switch isolation,
failed refresh/retry, user isolation, file filtering and real previews, including
desktop and mobile evidence from the isolated engine fixture.

## Existing access and navigation contracts

### Bot workspace

The Bots destination opens an avatar-led catalog with cards/list layouts,
local search over names, descriptions, roles, categories and models, and a
Favorites filter. Favorites and layout preferences are stored per signed-in
email in browser storage; they never alter the profile, visibility or grants.
Only profiles already returned by the authorized catalog are displayed.
Search, favorites and layout controls stay mounted in the main library while
the cards refresh. The redundant desktop sidebar is hidden in this library and
returns for bot details; the user's sidebar preference is not changed. Cards
offer **Open chat** through the existing bot-switch handler. This can resume a
bot's conversation and does not send a message or guarantee a new session.
Favorites also sort first in the chat bot picker. Hidden profiles remain
excluded there unless they are the active profile, following the existing rule.

Bot details separate Overview, Work, Identity and Configuration. Existing forms,
task lists, work results and Scout controls are moved into tab panels without
rebuilding their DOM when switching tabs, so unsaved inputs survive. The Overview
provides contextual entry cards for the existing instructions, memory, skills
and tools editors. “Chat with this bot” uses the existing profile-switch path;
it does not submit a message. Selected-for-chat labels are not live status.

The searchable chat picker keeps its query and keyboard focus across catalog
refreshes, supports arrow-key navigation and returns focus to its trigger on
Escape. Searches do not issue new requests. `bot-workbench.spec.ts` covers
identity-separated preferences, draft retention, mobile layouts, picker refresh
and real engine dispatch from the bot detail action.

SynPulse members have seven primary destinations: Chats, Bots, Projects,
Scheduled tasks, Skills library, Connections and My approvals. A destination
is omitted when its feature permission is absent. Settings remains a utility
for personal preferences and request history.

Navigation is derived from the caller's effective access returned by
`/api/governance/me`. Wildcard, `governance:write` or `governance:admin` grants
select the complete administrator navigation. Explicit owner/admin roles and
bootstrap administrators follow the same administrative identity as ownership.
Read-only governance access and wildcard route allowlists do not select the
administrator interface. Policy mode alone never promotes a signed-in member;
auth-disabled local single-user mode keeps the complete interface. Backend
feature permission checks remain independent.

This intentionally replaces permission-only navigation: ordinary members no
longer see every technical panel for which they happen to hold read access.
If effective access cannot be read, navigation falls back to the member surface;
backend calls remain independently authorized. Optional diagnostics and gateway
health checks do not run for callers missing their respective grants.

My approvals reads only `/api/governance/approvals/mine`, whose backend scopes
requests to the authenticated caller. Its primary view contains pending
requests; history remains available under Settings. It never fetches the
administrator approval queue.

Bot cards support keyboard activation. The detail view presents identity and
configuration links first, with technical bindings under a disclosure. Editing
identity requires the effective `profiles:admin` grant; the backend retains its
own grant and bot-scope checks. Narrow screens stack the editor fields and keep
configuration buttons touch-sized.

Frontend labels use SynPulse. Engine import names, URLs, storage keys, locale
registration hooks and the existing synthpulse skin value remain compatible.

Validation: `./scripts/test.sh tests/test_synpulse_role_navigation.py
tests/test_governance_nav_visibility.py tests/test_synpulse_own_approvals_ui.py`.
Browser checks should use distinct signed-in member and administrator accounts,
at desktop and mobile widths. Verify that a member cannot see another person's
approval requests, and that refreshing retains the correct navigation.

## Conversation controls

Members start with the bot, files, workspace and conversation members in reach.
Model, reasoning, mode, toolsets and provider quota controls are disclosed through
Settings > Appearance > Show advanced chat controls. This preference is stored
per signed-in email in the browser and does not change backend permissions or
the selected model. Administrators retain all composer controls.

The CLI/WebUI source filter remains an administrative control. Members use the
WebUI conversation list, with the same server-side ownership filtering. A stale
CLI-only preference from another account is reset when member navigation loads.
Navigation labels are always visible for members, so the redundant Show labels
toggle is hidden.

Bot lists and pickers request /api/profiles?fast=1: cold skill counts are omitted while the server computes them in its bounded background worker. Pending counts are never rendered as zero; reopening the list refreshes counts without polling.

Personal memory is an eighth daily-use area after My approvals. It requires
chat:use, matching the private memory API. It does not require or grant access
to shared memory administration, logs or governance. Existing admin navigation
remains unchanged.

Apps is a separate navigation destination for authenticated members and admins.
It is independent of the Modules overview: the backend returns only permitted
links, and unavailable settings keep the destination hidden. The default member
order places Apps at the bottom, just above Settings, including when an older
saved tab order places it earlier. Desktop and mobile share the same gate.
Opening an app uses a new browser tab and does not change the current chat draft.

Files, Logs and Todos are omitted from the left navigation for all roles,
including the mobile navigation. Workspace files and optional Todos remain
available in the right workspace panel; underlying panel routes are retained.
