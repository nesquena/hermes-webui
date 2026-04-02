# Hermes Web UI -- Changelog

> Living document. Updated at the end of every sprint.
> Repository: https://github.com/nesquena/hermes-webui

---

## [v0.16.2] Model List Updates + base_url Passthrough
*April 1, 2026 | 247 tests*

### Bug Fixes
- **MiniMax model list updated.** Replaced stale ABAB 6.5 models with current
  MiniMax-M2.7, M2.7-highspeed, M2.5, M2.5-highspeed, M2.1 lineup matching
  hermes-agent upstream. (Fixes #6)
- **Z.AI/GLM model list updated.** Replaced GLM-4 series with current GLM-5,
  GLM-5 Turbo, GLM-4.7, GLM-4.5, GLM-4.5 Flash lineup.
- **base_url passthrough to AIAgent.** `resolve_model_provider()` now reads
  `base_url` from config.yaml and passes it to AIAgent, so providers with
  custom endpoints (MiniMax, Z.AI, local LLMs) route to the correct API.

---

## [v0.16.1] Community Fixes -- Mobile + Auth + Provider Routing
*April 1, 2026 | 247 tests*

Community contributions from @deboste, reviewed and refined.

### Bug Fixes
- **Mobile responsive layout.** Comprehensive `@media(max-width:640px)` rules
  for topbar, messages, composer, tool cards, approval cards, and settings modal.
  Uses `100dvh` with `100vh` fallback to fix composer cutoff on mobile browsers.
  Textarea `font-size:16px` prevents iOS/Android auto-zoom on focus.
- **Reverse proxy basic auth support.** All `fetch()` and `EventSource` URLs now
  constructed via `new URL(path, location.origin)` to strip embedded credentials
  per Fetch spec. `credentials:'include'` on fetch, `withCredentials:true` on
  EventSource ensure auth headers are forwarded through reverse proxies.
- **Model provider routing.** New `resolve_model_provider()` helper in
  `api/config.py` strips provider prefix from dropdown model IDs (e.g.
  `anthropic/claude-sonnet-4.6` → `claude-sonnet-4.6`) and passes the correct
  `provider` to AIAgent. Handles cross-provider selection by matching against
  known direct-API providers.

---

## [v0.16] Sprint 14 -- Visual Polish + Workspace Ops + Session Organization
*March 30, 2026 | 233 tests*

### Features
- **Mermaid diagram rendering.** Code blocks tagged `mermaid` render as
  diagrams inline. Mermaid.js loaded lazily from CDN on first encounter.
  Dark theme with matching colors. Falls back to code block on parse error.
- **Message timestamps.** Subtle HH:MM time next to each role label. Full
  date/time on hover tooltip. User messages get `_ts` field when sent.
- **File rename.** Double-click any filename in workspace panel to rename
  inline. `POST /api/file/rename` endpoint with path traversal protection.
- **Folder create.** Folder icon button in workspace panel header. Prompt
  for name, `POST /api/file/create-dir` endpoint.
- **Session tags.** Add `#tag` to session titles. Tags shown as colored
  chips in sidebar. Click a tag to filter the session list.
- **Session archive.** Archive icon on each session. Archived sessions
  hidden by default; "Show N archived" toggle at top of list. Backend
  `POST /api/session/archive` with `archived` field on Session model.

### Bug Fixes
- **Date grouping fix.** Session list groups (Today/Yesterday/Earlier) now
  use `created_at` instead of `updated_at`, preventing sessions from jumping
  between groups when auto-titling touches `updated_at`.

---

## [v0.15] Sprint 13 -- Alerts + Session QoL + Polish
*March 30, 2026 | 221 tests*

### Features
- **Cron completion alerts.** New `GET /api/crons/recent` endpoint. UI polls every
  30s (pauses when tab is hidden). Toast notification per completion with status icon.
  Red badge count on Tasks nav tab, cleared when tab is opened.
- **Background agent error alerts.** When a streaming session errors out and the user
  is viewing a different session, a persistent red banner appears above the messages:
  "Session X has encountered an error." View button navigates, Dismiss clears.
- **Session duplicate.** Copy icon on each session in the sidebar (visible on hover).
  Creates a new session with the same workspace and model, titled "(copy)".
- **Browser tab title.** `document.title` updates to show the active session title
  (e.g. "My Task -- Hermes"). Resets to "Hermes" when no session is active.

### Bug Fixes
- Click guard added for duplicate button to prevent accidental session navigation.

---

## [v0.14] Sprint 12 -- Settings Panel + Reliability + Session QoL
*March 30, 2026 | 211 tests*

### Features
- **Settings panel.** Gear icon in topbar opens slide-in overlay. Persist default
  model and workspace server-side in `settings.json`. Server reads on startup.
- **SSE auto-reconnect.** When EventSource drops mid-stream, attempts one reconnect
  using the same stream_id after 1.5s. Shared `_wireSSE()` function eliminates
  handler duplication.
- **Pin sessions.** Star icon on each session. Pinned sessions float to top of sidebar
  under a gold "Pinned" header. Persisted in session JSON.
- **Import session from JSON.** Upload button in sidebar. Creates new session with
  fresh ID from exported JSON file.

### Bug Fixes
- `models.py` uses `_cfg.DEFAULT_MODEL` module reference so `save_settings()` changes
  take effect for `new_session()`.
- Full-scan fallback sort in `all_sessions()` now accounts for pinned sessions.
- `save_settings()` whitelists known keys only, rejecting arbitrary data.
- Escape key closes settings overlay.

---

## [v0.13] Sprint 11 -- Multi-Provider Models + Streaming Smoothness
*March 30, 2026 | 201 tests*

### Features
- **Multi-provider model support.** New `GET /api/models` endpoint discovers configured
  providers from `config.yaml`, `auth.json`, and API key environment variables. The model
  dropdown now populates dynamically from whatever providers the user has set up (Anthropic,
  OpenAI, Google, DeepSeek, Nous Portal, OpenRouter, etc.). Falls back to the hardcoded
  OpenRouter list when no providers are detected. Sessions with unlisted models auto-add
  them to the dropdown.
- **Smooth scroll pinning.** During streaming, auto-scroll only when the user is near the
  bottom of the message area. If the user scrolls up to read earlier content, new tokens
  no longer yank them back down. Pinning resumes when they scroll back to the bottom.

### Architecture
- **Routes extracted to api/routes.py.** All 49 GET/POST route handlers moved from server.py
  into `api/routes.py` (802 lines). server.py is now a 76-line thin shell: Handler class
  with structured logging, dispatch to `handle_get()`/`handle_post()`, and `main()`.
  Completes the server split started in Sprint 10.
- **Cleaned up duplicate dead-code routes** that existed in the old `do_GET` (skills/save,
  skills/delete, memory/write were duplicated in both GET and POST handlers).

### Bug Fixes
- Regression tests updated for new route module structure.

---

## [v0.12.2] Concurrency + Correctness Sweeps
*March 31, 2026 | 190 tests*

Two systematic audits of all concurrent multi-session scenarios. Each finding
became a regression test so it cannot silently return.

### Sweep 1 (R10-R12)
- **R10: Approval response to wrong session.** `respondApproval()` used
  `S.session.session_id` -- whoever you were viewing. If session A triggered
  a dangerous command requiring approval and you switched to B then clicked
  Allow, the approval went to B's session_id. Agent on A stayed stuck. Fixed:
  approval events tag `_approvalSessionId`; `respondApproval()` uses that.
- **R11: Activity bar showed cross-session tool status.** Session A's tool
  name appeared in session B's activity bar while you were viewing B. Fixed:
  `setStatus()` in the tool SSE handler is now inside the `activeSid` guard.
- **R12: Live tool cards vanished on switch-away and back.** Switching back to
  an in-flight session showed empty live cards even though tools had fired.
  Fixed: `loadSession()` INFLIGHT branch now restores cards from `S.toolCalls`.

### Sweep 2 (R13-R15)
- **R13: Settled tool cards never rendered after response completes.**
  `renderMessages()` has a `!S.busy` guard on tool card rendering. It was
  called with `S.busy=true` in the done handler -- tool cards were skipped
  every time. Fixed: `S.busy=false` set inline before `renderMessages()`.
- **R14: Wrong model sent for sessions with unlisted model.** `send()` used
  `$('modelSelect').value` which could be stale if the session's model isn't
  in the dropdown. Fixed: now uses `S.session.model || $('modelSelect').value`.
- **R15: Stale live tool cards in new sessions.** `newSession()` didn't call
  `clearLiveToolCards()`. Fixed.

---

## [v0.12.1] Sprint 10 Post-Release Fixes
*March 31, 2026 | 177 tests*

Critical regressions introduced during the server.py split, caught by users and fixed immediately.

- **`uuid` not imported in server.py** -- `chat/start` returned 500 (NameError) on every new message
- **`AIAgent` not imported in api/streaming.py** -- agent thread crashed immediately, SSE returned 404
- **`has_pending` not imported in api/streaming.py** -- NameError during tool approval checks
- **`Session.__init__` missing `tool_calls` param** -- 500 on any session with tool history
- **SSE loop did not break on `cancel` event** -- connection hung after cancel
- **Regression test file added** (`tests/test_regressions.py`): 10 tests, one per introduced bug. These form a permanent regression gate so each class of error can never silently return.

---

## [v0.12] Sprint 10 -- Server Health + Operational Polish
*March 31, 2026 | 167 tests*

### Post-sprint Bug Fixes
- SSE loop now breaks on `cancel` event (was hanging after cancel)
- `setBusy(false)` now always hides the Cancel button
- `S.activeStreamId` properly initialized in the S global state object
- Tool card "Show more" button uses data attributes instead of inline JSON.stringify (XSS/parse safety)
- Version label updated to v0.2
- `Session.__init__` accepts `**kwargs` for forward-compatibility with future JSON fields
- Test cron jobs now isolated via `HERMES_HOME` env var in conftest (no more pollution of real jobs.json)
- `last_workspace` reset after each test in conftest (prevents workspace state bleed between tests)
- Tool cards now grouped per assistant turn instead of piled before last message
- Tool card insertion uses `data-msg-idx` attribute correctly (was `msgIdx`, matching HTML5 dataset API)

### Architecture
- **server.py split into api/ modules.** 1,150 lines -> 673 lines in server.py.
  Extracted modules: `api/config.py` (101), `api/helpers.py` (57), `api/models.py` (114),
  `api/workspace.py` (77), `api/upload.py` (77), `api/streaming.py` (187).
  server.py is now the thin routing shell only. All business logic is independently importable.

### Features
- **Background task cancel.** Red "Cancel" button appears in the activity bar while a task
  is running. Calls `GET /api/chat/cancel?stream_id=X`. The agent thread receives a cancel
  event, emits a 'cancel' SSE event, and the UI shows "*Task cancelled.*" in the conversation.
  Note: a tool call already in progress (e.g. a long terminal command) completes before
  the cancel takes effect -- same behavior as CLI Ctrl+C.
- **Cron run history viewer.** Each job in the Tasks panel now has an "All runs" button.
  Click to expand a list of up to 20 past runs with timestamps, each collapsible to show
  the full output. Click again to hide.
- **Tool card UX polish.** Three improvements:
  1. Pulsing blue dot on cards for in-progress tools (distinct from completed cards)
  2. Smart snippet truncation at sentence boundaries instead of hard byte cutoff
  3. "Show more / Show less" toggle on tool results longer than 220 chars

---

## [v0.11] Sprint 9 -- Codebase Health + Daily Driver Gaps
*March 31, 2026 | 149 tests*

The sprint that closed the last gaps for heavy agentic use.

### Architecture
- **app.js replaced by 6 modules.** `app.js` is deleted. The browser now loads 6 focused files:
  `ui.js` (530), `workspace.js` (132), `sessions.js` (189), `messages.js` (221),
  `panels.js` (555), `boot.js` (142). The modules are a superset of the original app.js
  (two functions -- `loadTodos`, `toolIcon` -- were added directly to the modules after the split).
  No single file exceeds 555 lines.

### Features
- **Tool call cards inline.** Every tool Hermes uses now appears as a collapsible card
  in the conversation between the user message and the response. Live during streaming,
  restored from session history on reload. Shows tool name, preview, args, result snippet.
- **Attachment metadata persists on reload.** File badges on user messages survive page
  refresh. Server stores filenames on the user message in session JSON.
- **Todo list panel.** New checkmark tab in the sidebar. Shows current task list parsed
  from the most recent todo tool result in message history. Status icons: pending (○),
  in-progress (◉), completed (✓), cancelled (✗). Auto-refreshes when panel is active.
- **Model preference persists.** Last-used model saved to localStorage. Restored on page
  load. New sessions inherit it automatically.

### Bug Fixes
- Tool card toggle arrow only shown when card has expandable content
- Attachment tagging matches by message content to avoid wrong-turn tagging
- SSE tool event was missing `args` field
- `/api/session` GET was not returning `tool_calls` (history lost on reload)

---

## [v0.10] Sprint 8 -- Daily Driver Finish Line
*March 31, 2026 | 139 tests*

### Features
- **Edit user message + regenerate.** Hover any user bubble, click the pencil icon.
  Inline textarea, Enter submits, Escape cancels. Truncates session at that point and re-runs.
- **Regenerate last response.** Retry icon on the last assistant bubble only.
- **Clear conversation.** "Clear" button in topbar. Wipes messages, keeps session slot.
- **Syntax highlighting.** Prism.js via CDN (deferred). Python, JS, bash, JSON, SQL and more.

### Bug Fixes
- Reconnect banner false positive on normal loads (90-second window)
- Session list clipping on short screens
- Favicon 404 console noise (server now returns 204)
- Edit textarea auto-resize on open
- Send button guard while inline edit is active
- Escape closes dropdown, clears search, cancels active edit
- Approval polling not restarted on INFLIGHT session switch-back
- Version label updated to v0.10

### Hotfix: Message Queue + INFLIGHT
- **Message queue.** Sending while busy queues the message with toast + badge.
  Drains automatically on completion. Cleared on session switch.
- **Message stays visible on switch-away/back.** loadSession checks INFLIGHT before
  server fetch, so sent message and thinking dots persist correctly.

---

## [v0.9] Sprint 7 -- Wave 2 Core: CRUD + Search
*March 31, 2026 | 125 tests*

### Features
- **Cron edit + delete.** Inline edit form per job, save and delete with confirmation.
- **Skill create, edit, delete.** "+ New skill" form in Skills panel. Writes to `~/.hermes/skills/`.
- **Memory inline edit.** "Edit" button opens textarea for MEMORY.md. Saves via `/api/memory/write`.
- **Session content search.** Filter box searches message text (up to 5 messages per session)
  in addition to titles. Debounced API call, results appended below title matches.

### Architecture
- `/health` now returns `active_streams` and `uptime_seconds`
- `git init` on `<repo>/`, pushed to GitHub

### Bug Fixes
- Activity bar overlap on short viewports
- Model chip stale after session switch
- Cron output overflow in tasks panel

---

## [v0.8] Sprint 6 -- Polish + Phase E Complete
*March 31, 2026 | 106 tests*

### Architecture
- **Phase E complete.** HTML extracted to `static/index.html`. server.py now pure Python.
  Line count progression: 1778 (Sprint 1) → 1042 (Sprint 5) → 903 (Sprint 6).
- **Phase D complete.** All endpoints validated with proper 400/404 responses.

### Features
- **Resizable panels.** Sidebar and workspace panel drag-resizable. Widths persisted to localStorage.
- **Create cron job from UI.** "+ New job" form in Tasks panel with name, schedule, prompt, delivery.
- **Session JSON export.** Downloads full session as JSON via "JSON" button in sidebar footer.
- **Escape from file editor.** Cancels inline file edit without saving.

---

## [v0.7] Sprint 5 -- Phase A Complete + Workspace Management
*March 30, 2026 | 86 tests*

### Architecture
- **Phase A complete.** JS extracted to `static/app.js`. server.py: 1778 → 1042 lines.
- **LRU session cache.** `collections.OrderedDict` with cap of 100, oldest evicted automatically.
- **Session index.** `sessions/_index.json` for O(1) session list loads.
- **Isolated test server.** Port 8788 with own state dir, conftest autouse cleanup.

### Features
- **Workspace management panel.** Add/remove/rename workspaces. Persisted to `workspaces.json`.
- **Topbar workspace quick-switch.** Dropdown chip lists all workspaces, switches on click.
- **New sessions inherit last workspace.** `last_workspace.txt` tracks last used.
- **Copy message to clipboard.** Hover icon on each bubble with checkmark confirmation.
- **Inline file editor.** Preview any file, click Edit to modify, Save writes to disk.

---

## [v0.6] Sprint 4 -- Relocation + Session Power Features
*March 30, 2026 | 68 tests*

### Architecture
- **Source relocated** to `<repo>/` outside the hermes-agent git repo.
  Safe from `git pull`, `git reset`, `git stash`. Symlink maintained at `hermes-agent/webui-mvp`.
- **CSS extracted (Phase A start).** All CSS moved to `static/style.css`.
- **Per-session agent lock (Phase B).** Prevents concurrent requests to same session from
  corrupting environment variables.

### Features
- **Session rename.** Double-click any title in sidebar to edit inline. Enter saves, Escape cancels.
- **Session search/filter.** Live client-side filter box above session list.
- **File delete.** Hover trash icon on workspace files. Confirm dialog.
- **File create.** "+" button in workspace panel header.

---

## [v0.5] Sprint 3 -- Panel Navigation + Feature Viewers
*March 30, 2026 | 48 tests*

### Features
- **Sidebar panel navigation.** Four tabs: Chat, Tasks, Skills, Memory. Lazy-loads on first open.
- **Tasks panel.** Lists scheduled cron jobs with status badges. Run now, Pause, Resume.
  Shows last run output automatically.
- **Skills panel.** All skills grouped by category. Search/filter. Click to preview SKILL.md.
- **Memory panel.** Renders MEMORY.md and USER.md as formatted markdown with timestamps.

### Bug Fixes
- B6: New session inherits current workspace
- B10: Tool events replace thinking dots (not stacked alongside)
- B14: Cmd/Ctrl+K creates new chat from anywhere

---

## [v0.4] Sprint 2 -- Rich File Preview
*March 30, 2026 | 27 tests*

### Features
- **Image preview.** PNG, JPG, GIF, SVG, WEBP displayed inline in workspace panel.
- **Rendered markdown.** `.md` files render as formatted HTML in the preview panel.
- **Table support.** Pipe-delimited markdown tables render as HTML tables.
- **Smart file icons.** Type-appropriate icons by extension in the file tree.
- **Preview path bar with type badge.** Colored badge shows file type.

---

## [v0.3] Sprint 1 -- Bug Fixes + Foundations
*March 30, 2026 | 19 tests*

The first sprint. Established the test suite, fixed critical bugs.

### Bug Fixes
- B1: Approval card now shows pattern keys
- B2: File input accepts valid types only
- B3: Model chip label correct for all 10 models (replaced substring check with dict)
- B4/B5: Reconnect banner on mid-stream reload (localStorage inflight tracking)
- B7: Session titles no longer overflow sidebar
- B9: Empty assistant messages no longer render as blank bubbles
- B11: `/api/session` GET returns 400 (not silent session creation) when ID missing

### Architecture
- Thread lock on SESSIONS dict
- Structured JSON request logging
- 10-model dropdown with 3 provider groups (OpenAI, Anthropic, Other)
- First test suite: 19 HTTP integration tests

---

## [v0.2] UI Polish Pass
*March 30, 2026*

Visual audit via screenshot analysis. No new features -- design refinement only.

- Nav tabs: icon-only with CSS tooltip (5 tabs, no overflow)
- Session list: grouped by Today / Yesterday / Earlier
- Active session: blue left border accent
- Role labels: Title Case, softened color, circular icons
- Code blocks: connected language header with separator
- Send button: gradient + hover lift
- Composer: blue glow ring on focus
- Toast: frosted glass with float animation
- Tool status moved from composer footer to activity bar above composer
- Empty session flood fixed (filter + cleanup endpoint + test autouse)

---

## [v0.1] Initial Build
*March 30, 2026*

Single-file web UI for Hermes. stdlib HTTP server, no external dependencies.
Three-panel layout: sessions sidebar, chat area, workspace panel.

**Core capabilities:**
- Send messages, receive SSE-streamed responses
- Session create/load/delete, auto-title from first message
- File upload with manual multipart parser
- Workspace file tree with directory navigation
- Tool approval card (4 choices: once, session, always, deny)
- INFLIGHT session-switch guard
- 10-model dropdown (OpenAI, Anthropic, Other)
- SSH tunnel access on port 8787

---

*Last updated: v0.16.2, April 1, 2026 | Tests: 247*
