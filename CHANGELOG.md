# Hermes Co-Work Web UI -- Changelog

> Living document. Updated at the end of every sprint.
> Source: /home/hermes/webui-mvp/
> Repository: https://github.com/nesquena/hermes-webui

---

## [v1.1] Sprint 9 -- Codebase Health + Daily Driver Gaps
*March 31, 2026 | 149 tests*

The sprint that closed the last gaps for heavy agentic use.

### Architecture
- **app.js split into 6 modules.** The 1,669-line monolith is gone. Files:
  `ui.js` (530), `workspace.js` (132), `sessions.js` (189), `messages.js` (221),
  `panels.js` (555), `boot.js` (142). Zero behavior change. Character-verified against original.

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

## [v1.0] Sprint 8 -- Daily Driver Finish Line
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
- Version label updated to v1.0

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
- `git init` on `/home/hermes/webui-mvp/`, pushed to GitHub

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
- **Source relocated** to `/home/hermes/webui-mvp/` outside the hermes-agent git repo.
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

*Last updated: Sprint 9, March 31, 2026 | Tests: 149/149*
