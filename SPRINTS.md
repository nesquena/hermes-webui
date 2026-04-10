# Hermes Web UI -- Forward Sprint Plan

> Current state: v0.36 | 433 tests | Daily driver ready
> This document plans the path from here to two targets:
>
> Target A: 1:1 feature parity with the Hermes CLI (everything you can do from the
>           terminal, you can do from the browser)
>
> Target B: 1:1 parity with Claude's reproducible features (the full Claude
>           browser UI experience, minus things only Anthropic can build)
>
> Sprints are ordered by impact. Each builds on the one before.
> Past sprint history lives in CHANGELOG.md.

---

## Where we are now (v0.36)

**CLI parity: ~95% complete.** Core agent loop, all tools visible, workspace
file ops with tree view and git detection, cron/skills/memory CRUD, session
management, streaming with rAF throttle, cancel, multi-provider models, custom
endpoint discovery, slash commands (help/clear/model/workspace/new/usage/theme/compact),
thinking/reasoning display, password auth, multi-profile support with seamless
switching, CLI session bridge (read and import from state.db), context
auto-compaction handling, self-update checker. Remaining gaps: subagent
session tree, toolset control per session, code execution cells.

**Claude parity: ~85% complete.** Chat, streaming, file browser, session
management with projects and tags, tool cards with subagent delegation,
syntax highlighting, model switching, Mermaid diagrams, mobile responsive
layout (hamburger sidebar, bottom nav, files slide-over), breadcrumb
workspace nav with tree view, slash commands, thinking/reasoning display,
auth with signed cookies, 6 pluggable UI themes (dark/light/slate/solarized/
monokai/nord), voice input (Web Speech API), collapsible date groups,
context usage indicator, token/cost display, git branch badge, Docker
support. Remaining gaps: artifacts (HTML/SVG preview), TTS playback,
sharing/public URLs, code execution inline.

---

## Sprint 11 -- Multi-Provider Models + Streaming Smoothness (COMPLETED)

**Theme:** Use any Hermes-supported model provider from the UI, and make
heavy agentic work feel fast and fluid.

**Why now:** Two high-impact gaps converge here. First, the model dropdown is
hardcoded to ~10 OpenRouter model strings. If Hermes is configured with direct
Anthropic, OpenAI, Google, or other API providers, the web UI can't use them.
This means users who set up Hermes with native API keys are locked out of
their own models in the browser. Second, the streaming render path rebuilds
the entire message list on every tool event, causing visible flicker during
heavy agentic work.

### Track A: Bugs
- Tool card DOM thrash: renderMessages() rebuilds all cards on each tool event.
  Switch to incremental append (append new card to existing group, no full rebuild).
- Scroll position lost on re-render during streaming (messages jump).

### Track B: Features
- **Multi-provider model support:** Query Hermes agent's configured providers
  and available models at startup via a new `GET /api/models` endpoint. The
  model dropdown populates dynamically from whatever providers the user has
  configured (OpenRouter, direct OpenAI, direct Anthropic, Google, DeepSeek,
  etc.). Group by provider. Fall back to the current hardcoded list if the
  agent query fails. This ensures the web UI can use any model the CLI can.
- **Incremental tool card streaming:** Instead of renderMessages() on each
  tool event, maintain a live card group element per turn and append/update
  cards in place. The assistant text row below the cards also updates
  incrementally (already does via assistantBody.innerHTML).
- **Smooth scroll:** Pin scroll to bottom during streaming unless user has
  manually scrolled up (read-back mode). Resume pinning when user scrolls
  back to bottom.

### Track C: Architecture
- `api/routes.py`: extract the 49 if/elif route handlers from server.py's
  Handler class into a dedicated routes module. server.py becomes a true
  ~50-line shell: imports, Handler stub that delegates to routes, main().
  Completes the server split started in Sprint 10.

**Tests:** ~15 new. Total: ~205.
**Hermes CLI parity impact:** High (model provider parity is a major CLI gap)
**Claude parity impact:** Low (streaming smoothness)

---

## Sprint 12 -- Settings Panel + Reliability + Session QoL (COMPLETED)

**Theme:** Persist your preferences, survive network blips, and organize sessions.

**Why now:** Three daily-driver friction points converge. First, default model
and workspace aren't persisted server-side -- every restart loses them. Second,
SSH tunnel hiccups during long agent runs silently kill the response with no
recovery. Third, after 50+ sessions the flat chronological list makes it hard
to keep important conversations accessible.

### Track A: Bugs
- Workspace validation on add doesn't check symlinks (shows as invalid when
  it's actually a valid symlink to a directory).

### Track B: Features
- **Settings panel:** A gear icon in the topbar opens a slide-in settings panel.
  Sections: Default Model, Default Workspace. Persisted server-side in
  `~/.hermes/webui-mvp/settings.json`. Server reads settings on startup and
  uses them as defaults. `GET /api/settings` + `POST /api/settings` endpoints.
- **SSE auto-reconnect:** When the EventSource connection drops mid-stream
  (network blip, SSH tunnel hiccup), auto-reconnect once using the same
  `stream_id`. The server-side queue holds undelivered events. If reconnect
  fails after 5s, show error banner. This is the #1 reliability gap for
  remote VPS usage.
- **Pin sessions:** A star icon on any session in the sidebar. Pinned sessions
  float to the top of the list above date groups. Persisted on the session
  JSON as `pinned: true`. Toggle on click. Simple and high quality-of-life.
- **Import session from JSON:** Drag a `.json` export file into the sidebar
  (or click an import button) to restore it as a new session. Mirrors the
  existing JSON export. Useful for moving sessions between machines.

### Track C: Architecture
- Settings schema: `settings.json` with typed fields, validated on load, with
  sane defaults. Served via `GET /api/settings`, written via `POST /api/settings`.
- SSE reconnect: server keeps `STREAMS[stream_id]` alive for 60s after
  client disconnect, allowing reconnect with the same stream_id.

**Tests:** ~15 new. Total: ~216.
**Hermes CLI parity impact:** Medium (settings persistence, reliability)
**Claude parity impact:** Medium (settings panel, pinned conversations)

---

## Sprint 13 -- Alerts, Session QoL, Polish (COMPLETED)

**Theme:** Know what Hermes is doing, and small quality-of-life wins.

**Why now:** Cron jobs run silently. Background errors surface nowhere. You have
no way to know a long-running task finished (or failed) while you were on another
tab. Meanwhile, a few small UX gaps (no session duplicate, no tab title) add up
to daily friction.

### Track A: Bugs
- Symlink workspace validation — confirmed already fixed (`.resolve()` follows
  symlinks before `is_dir()` check).

### Track B: Features
- **Cron completion alerts:** `GET /api/crons/recent?since=TIMESTAMP` endpoint.
  UI polls every 30s (only when tab is focused). Toast notification on each
  completion. Red badge count on Tasks nav tab, cleared when tab is opened.
- **Background agent error alerts:** When a streaming session errors out and
  the user is on a different session, show a persistent red banner above the
  message area: "Session X encountered an error." Click "View" to navigate,
  "Dismiss" to clear.
- **Session duplicate:** Copy icon on each session in the sidebar (visible on
  hover). Creates a new session with same workspace/model, titled "(copy)".
- **Browser tab title:** `document.title` updates to show the active session
  title (e.g. "My Task — Hermes"). Resets to "Hermes" when no session active.

**Tests:** ~10 new. Total: ~221.
**Hermes CLI parity impact:** Medium (cron visibility, error surfacing)
**Claude parity impact:** Low

---

## Sprint 14 -- Visual Polish + Workspace Ops + Session Organization (COMPLETED)

**Theme:** Polish the visual experience, close workspace file gaps, and
organize sessions properly.

### Track B: Features
- **Mermaid diagram rendering:** Code blocks tagged `mermaid` render as
  diagrams inline. Mermaid.js loaded lazily from CDN. Dark theme. Falls
  back to code block on parse error.
- **Message timestamps:** Subtle HH:MM time next to each role label. Full
  date/time on hover. User messages tagged with `_ts` on send.
- **Date grouping fix:** Session list uses `created_at` for groups instead
  of `updated_at`. Prevents sessions jumping between groups on auto-title.
- **File rename:** Double-click any filename in the workspace panel to
  rename inline (same pattern as session rename). `POST /api/file/rename`.
- **Folder create:** Folder icon button in workspace panel header.
  `POST /api/file/create-dir`. Prompt for folder name.
- **Session tags:** Add `#tag` to session titles. Tags extracted and shown
  as colored chips in the sidebar. Click a tag to filter the session list.
- **Session archive:** Archive button on each session (box icon). Archived
  sessions hidden from sidebar by default. "Show N archived" toggle at top
  of list. `POST /api/session/archive` endpoint.

**Tests:** ~12 new. Total: ~233.
**Hermes CLI parity impact:** Medium (file rename, folder create)
**Claude parity impact:** Medium (Mermaid, tags, archive)

---

## Sprint 15 -- Session Projects + Code Copy + Tool Card Toggle (COMPLETED)

**Theme:** Organize work the way you think, not just chronologically.
Plus two quick UX wins for code and agentic workflows.

**Why now:** After 100+ sessions the sidebar is a flat chronological list.
Finding sessions from 2 weeks ago, or keeping work separated by project,
requires the search box. Session projects are the single biggest remaining
organizational gap vs. Claude's project folders.

### Track A: Bugs
- None.

### Track B: Features
- **Session projects:** Named groups for organizing sessions. A project
  filter bar (subtle chips) sits between the search input and the session
  list. Each project has a name and color. Click a chip to filter sessions
  to that project; "All" shows everything. Create projects inline (+
  button), rename (double-click chip), delete (right-click). Assign
  sessions via folder icon button (hover-reveal) with a dropdown picker.
  Projects stored in `projects.json`. Session model gains `project_id`
  field (null = unassigned). Fully backward-compatible with existing
  sessions. Endpoints: `GET /api/projects`, `POST /api/projects/create`,
  `POST /api/projects/rename`, `POST /api/projects/delete`,
  `POST /api/session/move`.
- **Code block copy button:** Every code block gets a "Copy" button.
  Positioned in the language header bar (or top-right corner for plain
  code blocks). Click copies code to clipboard, shows "Copied!" for 1.5s.
- **Tool card expand/collapse:** When a message has 2+ tool cards, an
  "Expand all / Collapse all" toggle appears above the card group.
  Scoped per message group, not global.

### Track C: Architecture
- `projects.json` flat file storage for project list (same pattern as
  `workspaces.json` and `settings.json`).
- `project_id` field on Session model with backward-compatible null default.
- `_index.json` includes `project_id` for fast client-side filtering.

**Tests:** 13 new. Total: ~237.
**Hermes CLI parity impact:** Low (CLI has no session organization)
**Claude parity impact:** Very High (projects are a core Claude concept)

### Candidates for later sprints
- Artifacts + code execution (HTML/SVG preview, inline Python execution)
- Voice input via Whisper
- Subagent delegation cards (enhanced tool card rendering)

---

## Sprint 16 -- Session Sidebar Visual Polish (COMPLETED)

**Theme:** Make the session list feel high-quality and delightful.

**Why now:** The session sidebar had two visible UX bugs: titles truncated
unnecessarily because action icons reserved space even when hidden, and
the project folder icon felt "sticky" and awkward. Emoji icons rendered
inconsistently across platforms. These were the most common visual complaints.

### Track A: Bugs (from BUGS.md)
- **Session title truncation.** Action icons (pin, move, archive, dup, trash)
  were always in the DOM with `flex-shrink:0`, reserving ~30px even when
  invisible. Fix: wrapped all actions in a `.session-actions` overlay
  container with `position:absolute`. Titles now use full available width.
  Actions appear on hover with a gradient fade from the right edge.
- **Folder button feels sticky.** Replaced `.has-project` persistent blue
  button with a colored left border matching the project color. The folder
  button now only appears in the hover overlay like all other actions.

### Track B: Features
- **SVG action icons.** Replaced all emoji HTML entities (★, 📂, 📦, ⊕, 🗑)
  with monochrome SVG line icons that inherit `currentColor`. Consistent
  rendering across macOS, Linux, and Windows. Icons: pin (star), folder,
  archive (box), duplicate (overlapping squares), trash (bin with lines).
- **Pin indicator.** Small gold filled-star icon rendered inline before the
  title only when the session is actually pinned. Unpinned sessions get
  full title width with zero space reservation.
- **Project border indicator.** Sessions assigned to a project show a
  colored left border matching the project color, replacing the old
  always-visible blue folder button.
- **Hover overlay polish.** Actions container uses a gradient background
  that fades from transparent to the sidebar color, creating a smooth
  emergence effect. Overlay hides automatically during inline rename.

### Deferred to Sprint 17
- Slash commands (basic set with `commands.js` module)
- Thinking/reasoning display for extended-thinking models
- Slash command autocomplete popup

**Tests:** 74 new (test_sprint16.py: safe HTML rendering, XSS security, sidebar polish). Total: 289.
**Hermes CLI parity impact:** Low
**Claude parity impact:** Medium (sidebar polish matches Claude's quality bar)

---

## Sprint 17 -- Workspace Polish + Slash Commands + Settings (COMPLETED)

**Theme:** Workspace polish, slash commands, and composer settings.

**Why now:** Three things converge: @nothingmn filed Issue #22 requesting a
tree/accordion workspace view (breadcrumb navigation is the foundation for
that), slash commands were deferred from Sprint 16, and Issue #26 (send key
personalization) fits naturally since we are already touching the keydown
handler for slash command autocomplete.

### Track A: Workspace Breadcrumb Navigation
- **Breadcrumb path bar.** When users click into subdirectories, a breadcrumb
  bar appears showing the path (e.g. `~ / src / components`) with clickable
  segments to navigate back. Hidden at root level for a clean UI.
- **Up button.** Arrow-up button in the panel header navigates to the parent
  directory. Hidden when already at workspace root.
- **Current directory tracking.** `S.currentDir` state property tracks the
  active directory. File operations (rename, delete, new file, new folder)
  stay in the current directory instead of jumping back to root.
- **New file/folder in subdirectories.** Creating files or folders now respects
  the current directory, creating them in the viewed subdirectory.

### Track B: Slash Commands Foundation
- **commands.js module.** New 7th JS module with command registry, parser,
  autocomplete dropdown, and built-in command handlers.
- **Built-in commands:** `/help` (list commands), `/clear` (clear conversation),
  `/model <name>` (switch model with fuzzy match), `/workspace <name>` (switch
  workspace), `/new` (start new session).
- **Autocomplete dropdown.** Typing `/` in the composer shows a filtered
  dropdown. Arrow keys navigate, Tab/Enter select, Escape closes. Positioned
  above the composer using the workspace dropdown CSS pattern.
- **Transparent pass-through.** Unrecognized `/` commands pass through to the
  agent normally (not intercepted).

### Track C: Send Key Setting (Issue #26)
- **`send_key` setting.** New setting in Settings panel: "Enter" (default) or
  "Ctrl+Enter". Persisted to `settings.json`. Loaded on boot.
- **Keydown handler rewrite.** Combined handler for autocomplete navigation
  and send key preference. When `ctrl+enter` is selected, plain Enter inserts
  a newline and Ctrl/Cmd+Enter sends.

### Deferred to Sprint 18
- Thinking/reasoning display for extended-thinking models
- Voice input via Whisper
- Workspace tree/accordion view (full implementation of Issue #22)

**Tests:** 6 new (test_sprint17.py). Total: 318.
**Hermes CLI parity impact:** Low (slash commands add convenience)
**Claude parity impact:** Medium (workspace nav, slash commands match Claude UX)

---

## Sprint 18 -- Thinking Display + Workspace Tree + Preview Fix (COMPLETED)

**Theme:** Show the model's reasoning, improve workspace navigation, fix UX bug.

**Why now:** Thinking/reasoning display was deferred twice (Sprint 16 → 17 → 18).
Workspace tree view was the #1 community request (Issue #22). File preview
staying open on directory navigation was a daily-driver annoyance.

### Track A: Bugs
- **File preview auto-close.** When viewing a file in the right panel and
  navigating directories (breadcrumbs, up button, folder clicks), the preview
  stayed visible with stale content. Fix: extracted `clearPreview()` as a named
  function in boot.js and call it from `loadDir()` in workspace.js.

### Track B: Features
- **Thinking/reasoning display.** Assistant messages with structured content
  arrays containing `type:'thinking'` or `type:'reasoning'` blocks now render
  as collapsible gold-themed cards above the response text. Collapsed by
  default, click header to expand. Works with Claude extended thinking and
  o3 reasoning tokens when preserved in the message array.
- **Workspace tree view (Issue #22).** Directories expand/collapse in-place
  with toggle arrows. Single-click toggles, double-click navigates (breadcrumb
  view). Subdirectory contents fetched lazily and cached in `S._dirCache`.
  Nesting depth shown via indentation. Empty directories show "(empty)".

**Tests:** 0 new (pure CSS/DOM changes). Total: 318.
**Hermes CLI parity impact:** Low
**Claude parity impact:** High (reasoning display matches Claude's UI)

---

## Sprint 19 -- Auth + Security Hardening (COMPLETED)

**Theme:** Make this safe to leave running beyond localhost.

**Why now:** Issue #23 requested authentication. Auth is the last production
hardening feature before the app is safe to expose to a network.

### Track A: Bugs
- **No request size limit.** POST bodies were unbounded (DoS risk). Added 20MB
  cap in `read_body()`.

### Track B: Features
- **Password authentication (Issue #23).** Off by default — zero friction for
  localhost. Enable via `HERMES_WEBUI_PASSWORD` env var or Settings panel.
  Password-only (no username — single-user app). Signed HMAC HTTP-only cookie
  with 24h TTL. Minimal dark-themed login page at `/login`. API calls without
  auth return 401; page loads redirect to `/login`. Settings panel gains
  "Access Password" field and "Sign Out" button.
- **Security headers.** All responses now include `X-Content-Type-Options: nosniff`,
  `X-Frame-Options: DENY`, `Referrer-Policy: same-origin`.

### Track C: Architecture
- New `api/auth.py` module: password hashing (SHA-256 + STATE_DIR salt), signed
  session cookies, auth middleware, public path allowlist.
- Auth check in `server.py` do_GET/do_POST before routing.
- `password_hash` added to `_SETTINGS_DEFAULTS` in config.py.
- `_set_password` special field in save_settings for secure password updates.

**Tests:** 10 new. Total: 328.
**Hermes CLI parity impact:** Low (CLI has no auth concerns)
**Claude parity impact:** High (Claude is authenticated)

---

## Sprint 20 -- Voice Input + Send Button Polish (COMPLETED)

**Theme:** Input refinements — voice and visual polish.

**Why now:** Voice input was the next feature on the roadmap. The send button
UX was a low-effort high-impact polish opportunity that pairs naturally.

### Track A: Bugs
- **Send button always visible.** The old pill-shaped "Send" button was always
  visible even with an empty textarea, wasting space. Now hidden by default,
  appears only when there is content to send.

### Track B: Features
- **Voice input (Web Speech API).** Microphone button in composer. Tap to
  record, tap again to stop. Live interim transcription in textarea. Auto-stops
  after ~2s of silence. Appends to existing text. Hidden when browser doesn't
  support Web Speech API. No API keys, no server changes.
- **Send button polish.** Icon-only 34px circle with upward arrow SVG. Pop-in
  spring animation on appear. Scale hover/active for tactile feedback. Hidden
  while agent is responding.

### Track C: Architecture
- Voice input IIFE in `boot.js` with SpeechRecognition lifecycle.
- `updateSendBtn()` in `ui.js` hooked into setBusy, renderTray, autoResize.

**Tests:** 52 new (voice) + 33 new (send button). Total: 415.
**Hermes CLI parity impact:** Medium (voice not in CLI, but adds capability)
**Claude parity impact:** High (Claude has native voice mode)

---

## Sprint 21 -- Mobile Responsive + Docker (COMPLETED)

**Theme:** Mobile experience + containerized deployment.

**Why now:** Issue #21 (mobile) was the most-requested UX gap. Issue #7 (Docker)
enables deployment beyond localhost. Both were achievable without new dependencies.

### Track A: Bugs (from review)
- **CSS cascade broke mobile slide-in.** `position:relative` after the media query
  overrode `position:fixed`. Wrapped in `@media(min-width:641px)`.
- **mobileSwitchPanel() always reopened sidebar.** Chat tab now closes it.
- **Dockerfile missing pip install.** Container failed on startup.
- **No .dockerignore.** `.git`, `tests/`, `.env*` leaked into images.
- **docker-compose tilde expansion.** `~` doesn't expand in Compose defaults.

### Track B: Features
- **Hamburger sidebar.** Slide-in overlay on mobile, tap outside to close.
- **Bottom navigation bar.** 5-tab iOS-style bar replaces sidebar tabs.
- **Files slide-over.** Right panel opens as slide-over from right edge.
- **Touch targets.** Minimum 44px on all interactive elements.
- **Docker support.** Dockerfile, docker-compose.yml, .dockerignore.

### Track C: Architecture
- Mobile nav functions in `boot.js`. Session click auto-closes sidebar.
- 69 new CSS lines scoped to `@media(max-width:640px)`.
- Desktop layout untouched — all mobile elements `display:none` by default.

**Tests:** 0 new (CSS/DOM changes). Total: 415.
**Hermes CLI parity impact:** Low
**Claude parity impact:** High (Claude has mobile layout)

---

## Sprint 22 -- Multi-Profile Support (COMPLETED, Issue #28)

**Theme:** Switch between Hermes agent profiles seamlessly from the web UI.

**Why now:** Issue #28 requested full profile management in the UI. The CLI has
had comprehensive profile support since v0.6.0 — isolated instances with their
own config, skills, memory, cron, and API keys. The web UI was locked to a
single default profile, blocking multi-persona workflows.

### Track A: Bugs
- **Hardcoded `~/.hermes` paths.** Memory read/write in routes.py and model
  discovery in config.py used hardcoded paths instead of the active profile's
  directory. Fixed to resolve through `get_active_hermes_home()`.
- **Module-level cached paths.** hermes-agent's `skills_tool.py` and `cron/jobs.py`
  snapshot `HERMES_HOME` at import time. Profile switch now monkey-patches these
  cached variables (`SKILLS_DIR`, `CRON_DIR`, `JOBS_FILE`, `OUTPUT_DIR`).

### Track B: Features
- **Profile picker (topbar).** Purple-accented chip with SVG user icon in the
  topbar. Click opens a dropdown listing all profiles with gateway status dots,
  model info, and skill count. Click to switch; "Manage profiles" link opens
  the management panel.
- **Profiles sidebar panel.** New nav tab with full management UI. Cards show
  each profile with model, provider, skill count, API key status, and gateway
  badge. "Use" button to switch, delete button for non-default profiles.
- **Profile creation.** "+ New profile" form with name validation (lowercase
  alphanumeric + hyphens), optional "clone config from active" checkbox. Wraps
  `hermes_cli.profiles.create_profile()`.
- **Profile deletion.** Confirm dialog, auto-switches to default if deleting
  the active profile. Blocked while agent is running.
- **Seamless switching.** No server restart required. Profile switch updates
  `HERMES_HOME` env var, patches module-level caches, reloads `.env` API keys,
  reloads `config.yaml`, and refreshes the model dropdown, skills, memory, and
  cron panels.
- **Per-session profile tracking.** New `profile` field on Session records which
  profile was active when the session was created. Backward-compatible (defaults
  to `null` for old sessions).

### Track C: Architecture
- New `api/profiles.py` module (~200 lines): profile state management wrapping
  `hermes_cli.profiles`. Thread-safe with `_profile_lock`. Lazy imports to
  avoid circular dependencies.
- `api/config.py`: Replaced module-level `cfg` dict with reloadable
  `get_config()`/`reload_config()`. Dynamic `_get_config_path()` resolves
  through active profile.
- `api/streaming.py`: `HERMES_HOME` added to env save/restore block around
  agent runs (alongside `TERMINAL_CWD`, `HERMES_EXEC_ASK`).
- Profile switch blocked while any agent stream is active (process-global
  `HERMES_HOME` cannot be changed mid-run).
- Zero modifications to hermes-agent code required.

**Tests:** 0 new (profile management requires hermes-agent integration). Total: 415.
**Hermes CLI parity impact:** Very High (profile support is a major CLI feature)
**Claude parity impact:** Low (Claude has no profile concept)

---

## Sprint 23 -- Agentic Transparency + Context Visibility (COMPLETED)

**Theme:** Surface what the agent is doing and how much context it's using.

**Why now:** Users had no visibility into tool call arguments, session token
usage, or context window fill. Sprint 22 left five coherence bugs in the
profile/workspace/model flow that also needed closing before the UI felt
reliable.

### Track A: Bugs
- **Model picker ignores profile on switch.** `populateModelDropdown()` skipped
  the profile's default model if `localStorage` had a saved preference. Fixed:
  `switchToProfile()` now clears `hermes-webui-model` from localStorage and
  applies the profile's default model from the switch response.
- **Workspace list is a global file.** `workspaces.json` was process-global.
  Fixed: workspace storage is now profile-local at `{profile_home}/webui_state/`.
  Default profile uses global STATE_DIR for backward compatibility.
- **`DEFAULT_WORKSPACE` is a startup singleton.** Frozen at boot. Fixed:
  `get_last_workspace()` and `_profile_default_workspace()` now resolve
  dynamically through the active profile's config.
- **Session list shows all profiles.** Fixed: `renderSessionListFromCache()`
  filters to `S.activeProfile` by default, with "Show N from other profiles"
  toggle (modeled on the archived toggle).
- **`switchToProfile()` doesn't refresh workspace list or sessions.** Fixed:
  now calls `loadWorkspaceList()`, `renderSessionList()`, resets profile filter.

### Track B: Features
- **Profile-local workspace storage.** Each named profile stores its own
  `workspaces.json` and `last_workspace.txt` under `{profile_home}/webui_state/`.
  Falls back to global STATE_DIR for the default profile (preserves test
  isolation and backward compat).
- **Profile switch returns defaults.** `POST /api/profile/switch` response now
  includes `default_model` and `default_workspace` so the frontend can apply
  both in one round-trip.
- **Session profile filter.** Session sidebar filters to active profile by
  default. "Show N from other profiles" toggle reveals sessions from all
  profiles. Resets on profile switch.

### Track C: Architecture
- `api/workspace.py`: Rewritten with `_profile_state_dir()`, `_workspaces_file()`,
  `_last_workspace_file()`, `_profile_default_workspace()`. All lazy imports to
  avoid circular deps.
- `api/profiles.py`: `switch_profile()` returns `default_model` and
  `default_workspace` from the new profile's config.yaml.
- `static/panels.js`: `switchToProfile()` clears localStorage model key,
  refreshes workspace list and session list, resets profile filter.
- `static/sessions.js`: `_showAllProfiles` state variable, profile filter in
  `renderSessionListFromCache()`, toggle UI.

**Tests:** 8 new (test_sprint23.py). Total: 423.
**Hermes CLI parity impact:** High (coherent profile behavior)
**Claude parity impact:** Low

---

## Sprint 24 -- Web Polish + Bug Fix Pass (PLANNED)

**Theme:** Stabilize, harden, and close the last meaningful web UI gaps before
shifting focus to distribution. Goal is a release that's genuinely ready for
wider user adoption -- no rough edges, no obvious missing pieces.

**Why now:** Sprint 23 completed the core agentic transparency features. The
remaining web roadmap items are diminishing-returns polish. Rather than
grinding through marginal features, this sprint cleans up what's there, fixes
bugs users will actually hit, and closes a few real gaps before recommending
the app to others.

### Track A: Bug Fixes
- **Cron edit form has no skill picker.** Sprint 23 added skill picker to the
  create form but not the edit form. cronEditSave() doesn't include skills in
  the update body, so existing skills survive an edit but can't be changed.
  Fix: add the same skill picker UI to the inline edit form and include
  `skills` in the update POST body.
- **S.lastUsage dead code.** messages.js sets `S.lastUsage` from `d.usage` at
  done-time, but nothing reads it. The usage badge reads cumulative session
  totals from `S.session.input_tokens` instead. Either wire `S.lastUsage` into
  a per-turn display or remove the dead assignment.
- **_cronSkillsCache never invalidated.** Skills picker shows stale data if
  skills are added/removed mid-session. Add a cache-bust when the skills panel
  is opened or a skill is saved/deleted.
- **Tool args not shown on session reload.** Tool call cards in history show
  name and result snippet but not the args (args only exist in the live SSE
  event). Sprint 23 added args to the session JSON -- verify they're actually
  rendering in the settled history cards.

### Track B: Features
- **Cron edit: skill picker parity.** As above -- make create and edit forms
  identical in capability.
- **Per-turn cost display.** The current usage badge shows cumulative session
  totals attached to the last message, which is misleading. Either: (a) show
  per-turn cost from `S.lastUsage` immediately after each response instead of
  cumulative, or (b) show cumulative in the session topbar/header instead of
  attached to a message bubble. Pick the cleaner UX.
- **Virtual scroll for long session/skill lists.** When session count or skill
  count gets large (100+), the sidebar becomes sluggish. Add a simple virtual
  scroll or windowed render -- only render visible items + a buffer above/below.
  CSS `contain: strict` + IntersectionObserver approach, no library needed.

### Track C: Code Quality
- Audit and remove any remaining dead code introduced by Sprint 23 (e.g. `S.lastUsage` assignment in messages.js that nothing reads).
- Verify tool call args render correctly in settled history cards on session reload.
- Update test count in all docs to match actual pytest output after sprint merges.

**Estimated tests:** ~10 new. Target total: ~435.
**Hermes CLI parity impact:** Low
**Claude parity impact:** Low
**User-facing value:** Medium -- removes rough edges that would bother new users

---

## Sprint 25 -- macOS Desktop Application (PLANNED)

**Theme:** Native Mac desktop app. Single download, runs entirely offline,
feels like a real application -- not a browser tab.

**Why this matters:** The web UI requires an SSH tunnel or a server setup to
use. A .app bundle that a user can double-click and immediately have a working
Hermes interface is genuinely differentiating. No other open-source Hermes
interface ships as a native Mac app. This is the highest-leverage remaining
investment for user adoption.

**Approach: Swift + WKWebView (not Electron)**

The right architecture is a thin native Swift shell (~300-500 lines) that:
1. Bundles the existing Python server and all api/ modules inside the .app
2. Spawns the server as a subprocess on a random local port at launch
3. Opens a WKWebView window pointed at that localhost port
4. Handles Mac app lifecycle natively (dock icon, cmd+Q, window management,
   app menu, about box)
5. Bridges a small set of native Mac capabilities that WKWebView can't do

**Why not Electron:** WKWebView is Safari's engine -- dramatically lighter than
Chromium. No 200MB node_modules. No separate update daemon. The .app is ~30MB
including the Python runtime, vs 150MB+ for Electron.

**Why not full native Swift UI:** Would require rewriting the entire frontend
from scratch. The web UI is already fast, dark-themed, and feature-complete.
The thin shell approach gets 95% of the benefit at 5% of the cost.

### Track A: Swift App Shell

**Files to create:**
```
desktop/
  HermesApp.swift          -- @main entry point, NSApp delegate
  AppDelegate.swift        -- lifecycle: start server on launch, stop on quit
  WindowController.swift   -- NSWindow + WKWebView setup, cmd shortcuts
  ServerManager.swift      -- spawn/monitor Python subprocess, pick free port
  MenuBuilder.swift        -- native app menu (File, Edit, View, Window, Help)
  Info.plist               -- bundle ID, display name, version, icon
  Assets.xcassets/         -- app icon (1024x1024 + all required sizes)
  HermesApp.xcodeproj/     -- Xcode project file
```

**ServerManager.swift responsibilities:**
- Find Python: check bundled runtime first, fall back to system python3
- Pick a free port (bind to :0, read assigned port, close, use it)
- Spawn: `python3 server.py --port {port}` as a child Process
- Monitor: if server crashes, show an error sheet and offer restart
- Shutdown: SIGTERM on app quit, wait up to 3s, then SIGKILL

**WKWebView configuration:**
- `allowsBackForwardNavigationGestures = false` (it's a single-page app)
- `WKUserContentController` for JS bridge (native notifications, file picker)
- Wait for server health check before loading (poll /health, show loading
  spinner in the native window while waiting, typically <1s)
- `userAgent` override so the server can detect desktop app context

**Native menu items (beyond defaults):**
- File > New Session (Cmd+N) -- calls JS `newSession()`
- File > New Window (Cmd+Shift+N) -- opens second window with its own WKWebView
- View > Toggle Sidebar (Cmd+Shift+S)
- Window > Zoom, Minimize (standard)
- Help > About Hermes, Check for Updates (links to GitHub releases page)

### Track B: Python Bundling

Two options, in order of preference:

**Option A: Require system Python (simpler, recommended for v1)**
- Check for `python3` at known paths: `/usr/bin/python3`, homebrew paths,
  pyenv paths
- If not found: show a one-time setup sheet with instructions
- Pros: tiny download (~5MB for the Swift app + web assets), no bundling complexity
- Cons: user needs Python installed (most developers do; target audience does too)

**Option B: Bundle python-standalone (self-contained, larger)**
- Use `python-build-standalone` (from Astral/uv project): pre-built Python
  3.11 binaries, ~30MB compressed, no Xcode toolchain needed to build
- Extract to `~/Library/Application Support/Hermes/python/` on first launch
- Install `requirements.txt` via bundled pip into a local venv
- Pros: zero dependencies, works on a clean Mac
- Cons: first launch takes ~10-20s for extraction + pip install; ~30MB download

**Recommendation:** Ship v1 with Option A. Add Option B as an optional
"standalone" download for non-developers.

### Track C: Distribution

**GitHub Releases (primary):**
- Build with `xcodebuild -scheme HermesApp -configuration Release -archivePath`
- `xcodebuild -exportArchive` to produce a .app bundle
- `hdiutil create` to produce a .dmg with drag-to-Applications installer UI
- Upload .dmg as a GitHub Release asset via `gh release create`
- CI: add `.github/workflows/mac-release.yml` -- trigger on `vX.Y.Z-mac` tag

**Code signing:**
- Without an Apple Developer account: distribute as unsigned, users must
  right-click > Open on first launch (standard for open-source Mac apps)
- With a free Apple Developer account: ad-hoc signing removes the Gatekeeper
  warning without paying $99/year (no notarization, but much better UX)
- With paid account ($99/year): full notarization, no warnings, direct download

**Recommended for v1:** ad-hoc signing (free, good enough for early adopters).
Document the right-click > Open workaround in the README for unsigned builds.

**Universal binary (Intel + Apple Silicon):**
```bash
xcodebuild archive -scheme HermesApp -destination "generic/platform=macOS"
```
Both architectures in one .app. No separate downloads needed.

### Track D: Native Integrations (v1 scope)

**System notifications for cron completion:**
- The web UI polls `/api/cron/alerts` and shows in-page banners
- The Mac app can additionally post `UNUserNotificationCenter` notifications
- JS bridge: `window.webkit.messageHandlers.notify.postMessage({title, body})`
- Swift handler: posts a native notification with the cron job name and output
  summary -- appears in Notification Center, works even when app is in background

**File picker for workspace add:**
- Currently: user types a path string into the workspace add form
- Mac app: intercept workspace-add form submission, open `NSOpenPanel` instead,
  return the selected path to the JS via `evaluateJavaScript`
- Much better UX -- standard Mac folder picker, no typing paths

**Dock badge for pending approvals:**
- When an agent approval is waiting, set `NSApp.dockTile.badgeLabel = "1"`
- Clear badge when approval is resolved
- JS bridge fires when approval card appears/disappears

**Menu bar mode (optional, v2):**
- A small status bar item (⚗️ icon in menu bar) that opens a compact popover
- Popover shows current session status, last message, quick-compose field
- Useful for running Hermes in the background without a full window

### Track E: Testing

Since the Swift app is thin glue, most testing remains in the existing pytest
suite (server still runs identically). New Swift-specific tests:
- `ServerManagerTests.swift`: verify port picking, process spawn, health wait
- UI tests via `XCUITest`: launch app, wait for WKWebView to load, verify
  title bar shows "Hermes", verify /health responds
- Smoke test in CI: `xcodebuild test -scheme HermesApp`

### Implementation Order

1. `ServerManager.swift` + basic `AppDelegate` -- get Python server spawning
   and health-check working from Swift
2. `WindowController.swift` -- WKWebView loading, loading spinner while
   server starts
3. App icon + Info.plist -- make it look like a real app
4. `MenuBuilder.swift` -- native menus + keyboard shortcuts
5. JS bridge for notifications -- most impactful native integration
6. DMG build script + GitHub Actions CI
7. (Optional) File picker bridge, dock badge

### What to NOT do in v1

- Windows or Linux wrapper (different toolchain; do Mac first, assess demand)
- Full Swift/SwiftUI rewrite of the frontend (months of work, wrong tradeoff)
- App Store submission (sandboxing breaks local server; not worth the effort)
- Auto-update mechanism (GitHub releases + manual download is fine for v1)
- Menu bar mode (cool but not v1 scope)

### Files to create in the repo

```
desktop/mac/
  HermesApp/
    HermesApp.swift
    AppDelegate.swift
    WindowController.swift
    ServerManager.swift
    MenuBuilder.swift
    Assets.xcassets/
    Info.plist
  HermesApp.xcodeproj/
  README.md              -- build instructions, requirements, signing notes
.github/workflows/
  mac-release.yml        -- build + sign + upload DMG on tag push
```

The server code (`server.py`, `api/`, `static/`, `requirements.txt`) is
referenced from the repo root -- no duplication. The .app bundle copies them
at build time.

**Estimated effort:** 2-3x a typical web sprint (new language, new toolchain,
bundling complexity). Realistic for a focused weekend or a dedicated agent run
with clear instructions.

**Hermes CLI parity impact:** N/A (different distribution channel)
**Claude parity impact:** Medium (Claude.app is a native Mac app)
**User-facing value:** Very high -- lowers barrier to entry dramatically,
genuinely differentiating for an open-source project

---

## Feature Parity Summary

### Hermes CLI Parity (as of Sprint 19)

| CLI Feature | Status |
|-------------|--------|
| Chat / agent loop | Done (v0.3) |
| Streaming responses | Done (v0.5) |
| Tool call visibility | Done (v0.11) |
| File ops (read/write/search/patch) | Done (v0.6) |
| Terminal commands | Done via workspace |
| Cron job management | Done (v0.9) |
| Skills management | Done (v0.9) |
| Memory read/write | Done (v0.9) |
| Session history | Done (v0.3) |
| Workspace switching | Done (v0.7) |
| Model selection | Done (v0.3) |
| Multi-provider model support | Done (Sprint 11) |
| Settings persistence | Done (Sprint 12) |
| Cron completion alerts | Done (Sprint 13) |
| Slash commands | Done (Sprint 17) |
| Thinking/reasoning display | Done (Sprint 18) |
| Auth / login | Done (Sprint 19) |
| Voice input | Done (Sprint 20) |
| Multi-profile support | Done (Sprint 22) |
| Subagent visibility | Deferred |
| Code execution (Jupyter) | Deferred |
| Toolset control | Deferred |
| Virtual scroll (perf) | Deferred |

### Claude Parity (as of Sprint 19)

| Claude Feature | Status |
|----------------|--------|
| Dark theme, 3-panel layout | Done (v0.1) |
| Streaming chat | Done (v0.5) |
| Model switching | Done (v0.3) |
| File attachments | Done (v0.6) |
| Syntax highlighting | Done (v0.10) |
| Tool use visibility | Done (v0.11) |
| Edit/regenerate messages | Done (v0.10) |
| Session management | Done (v0.6) |
| Mermaid diagrams | Done (Sprint 14) |
| Projects / folders | Done (Sprint 15) |
| Pinned/starred sessions | Done (Sprint 12) |
| Notifications | Done (Sprint 13) |
| Settings panel | Done (Sprint 12) |
| Reasoning display | Done (Sprint 18) |
| Auth / login | Done (Sprint 19) |
| Mobile layout (basic) | Done (v0.16.1) |
| Workspace tree view | Done (Sprint 18) |
| Slash commands | Done (Sprint 17) |
| Voice input | Done (Sprint 20) |
| TTS playback | Deferred |
| Artifacts (HTML/SVG preview) | Deferred |
| Code execution inline | Deferred |
| Mobile-optimized layout | Done (Sprint 21) |
| Sharing / public URLs | Not planned (requires server infra) |
| Claude-specific features | Not replicable (Projects AI, artifacts sync) |

### What is intentionally not planned

- **Sharing / public conversation URLs:** Requires a hosted backend with access
  control and CDN. Out of scope for a personal VPS deployment.
- **Claude-specific model features:** Claude-native Projects memory, extended
  artifacts sync, Anthropic's proprietary reasoning UI. These are Anthropic
  infrastructure, not reproducible.
- **Real-time collaboration:** Multiple users in the same session simultaneously.
  Single-user assumption throughout.
- **Plugin marketplace:** Hermes skills cover this use case already.

---

## Sprint 26 -- Pluggable UI Themes (COMPLETED)

**Theme:** Let users choose how the app looks -- light, dark, and custom color
schemes. One-click switching, persistent preference, zero flicker on load.

**Difficulty: Low-Medium.** The existing CSS is already 100% CSS-variable-driven
off a single `:root` block. Every color, background, and accent in the entire UI
is already a variable. Adding themes is mostly a matter of defining alternative
`:root` overrides and wiring a picker -- not a rewrite. The main engineering
work is flicker prevention on load and the settings UI.

**Estimated effort:** 1 sprint, ~2 days of implementation. 8-12 new tests.

---

### Why now

The UI ships only one dark theme. Contributors have asked for light mode. Power
users want to match their terminal colorscheme. This is low-risk, high-value
polish that makes the app feel more finished and more personal. It's also a
good precedent-setter: once the theme system exists, community members can
contribute new themes as a pure CSS addition with no Python changes needed.

---

### Design decisions

**Themes are CSS-variable overrides, not separate stylesheets.** Each theme is
a named `:root[data-theme="name"]` block. The base stylesheet stays untouched.
Switching themes sets `document.documentElement.dataset.theme = name` in JS.
No FOUC (flash of unstyled content), no stylesheet swap latency.

**Theme preference persists server-side in `settings.json`.** Same mechanism
as `send_key` and `show_token_usage`. The server includes `theme` in the
`GET /api/settings` response. Boot.js reads it and applies before first paint.

**Flicker prevention.** A tiny inline `<script>` in `<head>` (before the
stylesheet link) reads `localStorage.getItem('hermes-theme')` and sets
`document.documentElement.dataset.theme` synchronously. This prevents a
dark-flash on light-mode users during the round-trip to `/api/settings`.
The localStorage value is kept in sync whenever the user changes themes.

**No third-party dependencies.** Pure CSS + vanilla JS. No theme library.

---

### Track A: Core theme system

**1. CSS variable blocks in `static/style.css`**

The existing `:root` block becomes the `dark` (default) theme. Add named
theme blocks immediately after:

```css
/* ── Default (dark) theme ── already in :root ── */

:root[data-theme="light"] {
  --bg: #f5f5f7;
  --sidebar: #e8e8ed;
  --border: rgba(0,0,0,0.10);
  --border2: rgba(0,0,0,0.16);
  --text: #1c1c1e;
  --muted: #6e6e80;
  --accent: #c0392b;
  --blue: #0a6dc2;
  --gold: #a07a20;
  --code-bg: #f0f0f5;
}

:root[data-theme="solarized"] {
  --bg: #002b36;
  --sidebar: #073642;
  --border: rgba(255,255,255,0.08);
  --border2: rgba(255,255,255,0.13);
  --text: #839496;
  --muted: #657b83;
  --accent: #dc322f;
  --blue: #268bd2;
  --gold: #b58900;
  --code-bg: #073642;
}

:root[data-theme="monokai"] {
  --bg: #272822;
  --sidebar: #1e1f1c;
  --border: rgba(255,255,255,0.07);
  --border2: rgba(255,255,255,0.12);
  --text: #f8f8f2;
  --muted: #75715e;
  --accent: #f92672;
  --blue: #66d9e8;
  --gold: #e6db74;
  --code-bg: #1e1f1c;
}

:root[data-theme="nord"] {
  --bg: #2e3440;
  --sidebar: #272c36;
  --border: rgba(255,255,255,0.07);
  --border2: rgba(255,255,255,0.12);
  --text: #eceff4;
  --muted: #9099aa;
  --accent: #bf616a;
  --blue: #81a1c1;
  --gold: #ebcb8b;
  --code-bg: #272c36;
}
```

Additional theming notes:
- `syntax-highlight` colors (Prism.js) are theme-independent (they come from the
  CDN stylesheet) -- acceptable for v1.
- The logo gradient (`linear-gradient(145deg,#e8a030,var(--accent))`) uses
  `--accent` already so it adapts automatically.
- Scrollbar colors and `::selection` backgrounds need explicit overrides in the
  light theme to avoid dark scrollbars on a light background.

**2. Flicker-prevention inline script in `static/index.html`**

Immediately after `<head>` opens, before the stylesheet `<link>`:

```html
<script>
(function(){
  var t=localStorage.getItem('hermes-theme');
  if(t && t!=='dark') document.documentElement.dataset.theme=t;
})();
</script>
```

This runs synchronously before the stylesheet parses. Zero flicker.

**3. Theme loading in `static/boot.js`**

In the existing `api('/api/settings')` call, read and apply the theme:

```js
const s = await api('/api/settings');
window._sendKey = s.send_key || 'enter';
window._showTokenUsage = !!s.show_token_usage;
window._showCliSessions = !!s.show_cli_sessions;
// Theme: apply server preference, update localStorage for flicker prevention
const theme = s.theme || 'dark';
document.documentElement.dataset.theme = theme;
localStorage.setItem('hermes-theme', theme);
```

**4. Theme setting in `api/config.py`**

```python
_SETTINGS_DEFAULTS = {
    ...
    'theme': 'dark',  # active UI theme name
    ...
}
_SETTINGS_ALLOWED_KEYS = set(_SETTINGS_DEFAULTS.keys()) - {'password_hash'}
```

No enum constraint on `theme` -- allows user-defined theme names to work
without server changes.

---

### Track B: Theme picker UI

**Settings panel addition (`static/index.html` + `static/panels.js`)**

A `<select>` in the Settings panel, below the send-key picker:

```html
<div class="settings-field">
  <label for="settingsTheme">Theme</label>
  <select id="settingsTheme" ...>
    <option value="dark">Dark (default)</option>
    <option value="light">Light</option>
    <option value="solarized">Solarized Dark</option>
    <option value="monokai">Monokai</option>
    <option value="nord">Nord</option>
  </select>
</div>
```

In `loadSettingsPanel()`:
```js
const themeSel = $('settingsTheme');
if(themeSel) themeSel.value = settings.theme || 'dark';
```

In `saveSettings()`:
```js
body.theme = $('settingsTheme').value;
```

**Live preview on select change (no save required):**
```js
$('settingsTheme').addEventListener('change', e => {
  document.documentElement.dataset.theme = e.target.value;
  localStorage.setItem('hermes-theme', e.target.value);
});
```

This gives instant visual feedback as the user clicks through options.
The full settings save then persists it server-side.

**`/theme` slash command (`static/commands.js`)**

```js
async function cmdTheme(arg) {
  const themes = ['dark','light','solarized','monokai','nord'];
  if(!arg || !themes.includes(arg)) {
    showToast('Usage: /theme dark|light|solarized|monokai|nord');
    return;
  }
  document.documentElement.dataset.theme = arg;
  localStorage.setItem('hermes-theme', arg);
  try { await api('/api/settings', {method:'POST', body: JSON.stringify({theme: arg})}); } catch(e) {}
  showToast('Theme: ' + arg);
}
```

---

### Track C: Tests

New test cases in `tests/test_sprint26.py`:

1. `GET /api/settings` returns `theme: 'dark'` by default
2. `POST /api/settings` with `{theme: 'light'}` persists and round-trips
3. `POST /api/settings` with `{theme: 'nord'}` accepts any string (no enum gate)
4. Theme value survives server restart (reads from `settings.json`)
5. `/theme` command fires without error for each named theme
6. `loadSettingsPanel()` populates the select with the current theme value
7. Settings save includes theme in the POST body
8. `data-theme` attribute is set on `<html>` before first paint (inline script)

**Estimated new tests:** 8. Target total after sprint: ~443.

---

### What's out of scope

- **Custom color editors** (hex pickers for each variable): saves that for v2.
  The five shipped themes cover the main use cases. A custom theme can always
  be added by dropping a CSS block with no code changes.
- **Per-session themes**: single global preference is the right call for v1.
- **System `prefers-color-scheme` sync**: nice-to-have, low priority. The
  flicker-prevention script could be extended to read the media query if no
  explicit preference is set.
- **Prism.js theme switching**: the code-block syntax highlighting comes from
  a CDN stylesheet. Swapping it requires a `<link>` swap and SRI re-check.
  Defer to a future sprint; the default Prism Tomorrow theme works on all
  current dark themes and is acceptable on light.

---

**Estimated tests:** 8 new. Target total: ~443.
**Hermes CLI parity impact:** None
**Claude parity impact:** Medium (Claude.ai has light/dark/system sync)
**User-facing value:** High -- first thing many users ask for

---

*Last updated: April 10, 2026*
*Current version: v0.45.0 | 604 tests*
*Next sprint: Sprint 24 (Web Polish + Bug Fix Pass)*
*Horizon sprint: Sprint 25 (macOS Desktop Application)*
*Docs sweep policy: update markdown proactively during PR reviews and after significant releases*
