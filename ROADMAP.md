     1|# Hermes Co-Work Web UI: Full Parity Roadmap
     2|
     3|> Goal: Full 1:1 parity with the Hermes CLI experience via a clean dark web UI.
     4|> Everything you can do from the CLI terminal, you can do from this UI.
     5|>
     6|> Last updated: Post-Sprint 10 bug sweeps (March 31, 2026)
     7|> Tests: 190/190 passing
     8|> Source: <repo>/
     9|
    10|---
    11|
    12|## Sprint History (Completed)
    13|
    14|| Sprint | Theme | Highlights | Tests |
    15||--------|-------|-----------|-------|
    16|| Sprint 1 | Bug fixes + foundations | B1-B11 fixed, LOCK on SESSIONS, section headers, request logging | 19 |
    17|| Sprint 2 | Rich file preview | Image preview, rendered markdown, table support, smart icons | 27 |
    18|| Sprint 3 | Panel nav + viewers | Sidebar tabs, cron/skills/memory panels, B6/B10/B14, Phase D start | 48 |
    19|| Sprint 4 | Relocation + power features | Source to <repo>/, CSS extracted, session rename/search, file ops | 68 |
    20|| Sprint 5 | Phase A complete + workspace | JS extracted (server.py 1778->1042 lines), workspace management, copy message, file editor, session index | 86 |
    21|| Test hardening | Isolated test environment | Port 8788 test server, conftest autouse, cleanup_zero_message, 5 test files rewritten | 90 |
    22|| Sprint 6 | Polish + Phase E complete | HTML to static/, resizable panels, cron create, session JSON export, Escape from editor | 106 |
    23|| Sprint 7 | Wave 2 Core: CRUD + Search | Cron edit/delete, skill create/edit/delete, memory write, session content search, health improvements, git init | 125 |
    24|| Sprint 8 | Daily Driver Finish Line | Edit+regenerate user messages, regenerate last response, clear conversation, Prism.js syntax highlighting, reconnect banner fix, session list scroll fix | 139 |
    25|| Sprint 8 hotfix | Message queue + INFLIGHT fix | Queue messages while busy (toast + badge + auto-drain), INFLIGHT-first loadSession (message stays on switch-away/back) | 139 |
    26|| Sprint 9 | Codebase health + daily driver gaps | app.js deleted and replaced by 6 modules, tool call cards inline, attachment persistence on reload, todo list panel | 149 |
    27|| Sprint 10 | Server health + operational polish | server.py split into api/ modules, background task cancel, cron run history viewer, tool card UX polish | 167 |
    28|| Sprint 10 fixes | Import regressions + regression tests | uuid, AIAgent, has_pending, SSE cancel loop, Session.__init__ tool_calls; test_regressions.py | 177 |
    29|| Concurrency sweeps | Multi-session correctness | Approval cross-session (R10), activity bar per-session (R11), live cards on switch-back (R12), tool cards after done (R13), session model authoritative (R14), newSession cards (R15) | 190 |
    30|
    31|---
    32|
    33|## Current Architecture Status
    34|
    35|| Layer | Location | Status |
    36||-------|----------|--------|
    37|| Python server | <repo>/server.py (~1100 lines) | Pure Python, no inline HTML/CSS/JS |
    38|| HTML template | <repo>/static/index.html | Served from disk |
    39|| CSS | <repo>/static/style.css | Served from disk |
    40|| JavaScript | <repo>/static/app.js | Served from disk |
    41|| Runtime state | ~/.hermes/webui-mvp/sessions/ | Session JSON files |
    42|| Test server | Port 8788, state dir ~/.hermes/webui-mvp-test/ | Isolated, wiped per run |
    43|| Production server | Port 8787 | SSH tunnel from Mac |
    44|
    45|---
    46|
    47|## Feature Parity Checklist
    48|
    49|### Chat and Agent
    50|- [x] Send messages, get SSE-streaming responses
    51|- [x] Switch models per session (10 models, grouped by provider)
    52|- [x] Upload files to workspace (drag-drop, click, clipboard paste)
    53|- [x] File tray with remove button
    54|- [x] Tool progress shown in activity bar above composer
    55|- [x] Approval card for dangerous commands (Allow once/session/always, Deny)
    56|- [x] Approval polling + SSE-pushed approval events
    57|- [x] INFLIGHT guard: switch sessions mid-request without losing response
    58|- [x] Session restores from localStorage on page load
    59|- [x] Reconnect banner if page reloaded mid-stream
    60|- [x] Copy message to clipboard (hover icon on each bubble)
    61|- [x] Edit last user message and regenerate
    62|- [ ] Branch/fork conversation (Wave 3)
    63|- [ ] Token/cost estimate per message (Wave 3)
    64|
    65|### Tool Visibility
    66|- [x] Tool progress in activity bar (moved out of composer footer)
    67|- [x] Approval card with all 4 choices
    68|- [x] Tool call cards inline (collapsed, show name/args/result)
    69|
    70|### Workspace / Files
    71|- [x] Browse workspace directory tree with type icons
    72|- [x] Preview text/code files (read-only)
    73|- [x] Preview markdown files (rendered, tables supported)
    74|- [x] Preview image files (PNG, JPG, GIF, SVG, WEBP inline)
    75|- [x] Edit files inline (Edit button, Enter to save, Escape to cancel)
    76|- [x] Create new file (+ button in panel header)
    77|- [x] Delete file (hover trash, confirm dialog)
    78|- [x] File name truncation with tooltip for long names
    79|- [x] Right panel resizable (drag inner edge)
    80|- [x] Syntax highlighted code preview (Prism.js)
    81|- [ ] Rename file (Wave 3)
    82|- [ ] Create folder (Wave 3)
    83|
    84|### Sessions
    85|- [x] Create session (+ button or Cmd/Ctrl+K)
    86|- [x] Load session (click in sidebar)
    87|- [x] Delete session (hover trash, toast, correct fallback)
    88|- [x] Auto-title from first user message
    89|- [x] Rename session title (double-click in sidebar, Enter saves, Escape cancels)
    90|- [x] Filter/search sessions by title (live filter box)
    91|- [x] Date group headers (Today / Yesterday / Earlier)
    92|- [x] Download session as Markdown transcript
    93|- [x] Export session as JSON (full messages + metadata)
    94|- [x] Session inherits last-used workspace on creation
    95|- [x] Session content search (search message text across sessions)
    96|- [ ] Session tags / labels (Wave 5)
    97|- [ ] Archive sessions (Wave 5)
    98|- [x] Clear conversation (wipe messages, keep session) (Wave 3)
    99|- [ ] Import session from JSON (Wave 3)
   100|
   101|### Workspace Management
   102|- [x] Add workspace with path validation (must be existing directory)
   103|- [x] Remove workspace
   104|- [x] Rename workspace display name
   105|- [x] Quick-switch workspace from topbar dropdown
   106|- [x] Sidebar live workspace display (name + path, updates in real time)
   107|- [x] New sessions inherit last used workspace
   108|- [x] Workspace list persists to workspaces.json
   109|- [ ] Workspace reorder (drag) (Wave 2)
   110|
   111|### Scheduled Tasks (Cron)
   112|- [x] View all cron jobs (Tasks sidebar tab)
   113|- [x] View last run output per job (auto-loaded on expand)
   114|- [x] Expand job to see prompt, schedule, last output
   115|- [x] Run job manually (Run now button)
   116|- [x] Pause / Resume job
   117|- [x] Create cron job from UI (+ New job form with name, schedule, prompt, delivery)
   118|- [x] Edit existing cron job
   119|- [x] Delete cron job
   120|- [x] View full cron run history (expandable per job)
   121|- [ ] Skill picker in cron create form (Wave 3)
   122|
   123|### Skills
   124|- [x] List all skills grouped by category (Skills sidebar tab)
   125|- [x] Search/filter skills by name, description, category
   126|- [x] View full SKILL.md content in right preview panel
   127|- [x] Create skill
   128|- [x] Edit skill
   129|- [x] Delete skill
   130|- [ ] View skill linked files (Wave 3)
   131|
   132|### Memory
   133|- [x] View personal notes (MEMORY.md) rendered as markdown (Memory tab)
   134|- [x] View user profile (USER.md) rendered as markdown (Memory tab)
   135|- [x] Last-modified timestamp on each section
   136|- [x] Add/edit memory entry inline
   137|
   138|### Configuration
   139|- [ ] Settings panel (default model, workspace, toolsets) (Wave 4)
   140|- [ ] Enable/disable toolsets per session (Wave 4)
   141|
   142|### Notifications
   143|- [ ] Cron job completion alerts (Wave 4)
   144|- [ ] Background agent error alerts (Wave 4)
   145|
   146|### Advanced / Future
   147|- [ ] Voice input via Whisper (Wave 6)
   148|- [ ] TTS playback of responses (Wave 6)
   149|- [ ] Subagent delegation cards (Wave 6)
   150|- [x] Background task cancel (activity bar Cancel button)
   151|- [ ] Code execution cell (Wave 6)
   152|- [ ] Password authentication (Wave 7)
   153|- [ ] HTTPS / reverse proxy (Wave 7)
   154|- [ ] Mobile responsive layout (Wave 7)
   155|- [ ] Virtual scroll for large lists (Wave 7)
   156|
   157|---
   158|
   159|## Sprint 7: Wave 2 Core -- Cron/Skill/Memory CRUD + Session Content Search (COMPLETED)
   160|
   161|**Theme:** "Wave 2 Core -- Cron/Skill/Memory CRUD + Session Content Search"
   162|
   163|### Track A: Bug Fixes
   164|| Item | Description |
   165||------|-------------|
   166|| Activity bar sizing | Activity bar sometimes overlaps first message on short viewports |
   167|| Model dropdown sync | Model chip in topbar sometimes shows stale model after session switch |
   168|| Cron output truncation | Long cron output in the tasks panel overflows its container |
   169|
   170|### Track B: Features
   171|| Feature | What | Value |
   172||---------|------|-------|
   173|| Session content search | Search message text across all sessions, not just titles. GET /api/sessions/search already does title search; extend to message content with a configurable depth limit | High: the single most-requested nav feature after rename |
   174|| Cron edit + delete | Edit an existing cron job (name, schedule, prompt, delivery) inline in the tasks panel. Delete with confirm. POST /api/crons/update and /api/crons/delete | High: closes the cron CRUD gap (create was Sprint 6) |
   175|| Skill create + edit | A "New skill" form in the Skills panel. Name, category, SKILL.md content in a textarea editor. Save calls POST /api/skills/save (writes to ~/.hermes/skills/). Edit opens existing skill in the same editor | High: biggest remaining CLI gap after cron |
   176|
   177|### Track C: Architecture
   178|| Item | What |
   179||------|------|
   180|| Phase E: app.js module split (start) | Split app.js (1332 lines) into logical modules: sessions.js, chat.js, workspace.js, panels.js, ui.js. Serve via ES module imports in index.html. This is Phase E completion. |
   181|| Health endpoint improvement | Add active_streams, uptime_seconds to /health response (Phase G) |
   182|| Git init | git init <repo>, first commit, push to private GitHub repo |
   183|
   184|### Tests
   185|- ~20 new pytest tests (cron update/delete, skill save, session content search)
   186|- TESTING.md: Sections 29-31 (cron edit, skill edit, session search)
   187|- Estimated total after Sprint 7: ~126
   188|
   189|---
   190|
   191|## Wave 2: Full CRUD and Interaction Parity
   192|
   193|**Status:** In progress. Sprint 6 completed cron create and workspace management.
   194|Remaining Wave 2 items targeted for Sprints 7-8.
   195|
   196|### Sprint 2.0: Workspace Management (COMPLETE Sprint 5+6)
   197|All workspace features delivered: add/validate/remove/rename workspaces, topbar quick-switch,
   198|sidebar live display, new sessions inherit last workspace. See Sprint 5 completed section.
   199|
   200|### Sprint 2.1: Cron Job Management (Partial -- Sprint 7 for remaining)
   201|- [x] View all jobs (Sprint 3)
   202|- [x] Run / pause / resume (Sprint 3)
   203|- [x] Create job from UI (Sprint 6)
   204|- [x] Edit job
   205|- [x] Delete job
   206|- [x] Full cron run history
   207|
   208|### Sprint 2.2: Skill Management (Partial -- Sprint 7 for remaining)
   209|- [x] List all skills with categories (Sprint 3)
   210|- [x] View SKILL.md content (Sprint 3)
   211|- [x] Create skill
   212|- [x] Edit skill
   213|- [x] Delete skill
   214|
   215|### Sprint 2.3: Memory Write (Sprint 7)
   216|- [x] View notes + profile (Sprint 3)
   217|- [x] Edit notes inline
   218|
   219|### Sprint 2.4: Todo Management (Wave 2)
   220|- [x] View current todo list (sidebar Todo panel, parsed from session history)
   221|
   222|### Sprint 2.5: Session Content Search (Sprint 7)
   223|- [x] Session title search (Sprint 4)
   224|- [x] Message content search across sessions
   225|
   226|### Sprint 2.6: Session Rename (COMPLETE Sprint 4)
   227|Double-click any session title in the left sidebar to edit inline.
   228|Enter saves, Escape cancels. Topbar updates immediately.
   229|
   230|---
   231|
   232|## Wave 3: Power Features and Developer Experience
   233|
   234|### Sprint 3.1: Tool Call Visibility Inline
   235|Show tool calls as collapsible cards in the conversation.
   236|Collapsed: tool name badge + one-line preview. Expanded: full args + result.
   237|
   238|### Sprint 3.2: Multi-Model Expansion
   239|Add more models. Group by provider. Model info tooltip on hover.
   240|(Partially done: 10 models in dropdown from Sprint 1.)
   241|
   242|### Sprint 3.2b: Resizable Panel Widths (COMPLETE Sprint 6)
   243|Both sidebar and workspace panel are drag-resizable with localStorage persistence.
   244|
   245|### Sprint 3.3: Workspace File Actions
   246|- [ ] Rename file (inline, double-click) (Wave 3)
   247|- [ ] Create folder (Wave 3)
   248|- [x] Syntax highlighted code preview (Prism.js)
   249|
   250|### Sprint 3.4: Conversation Controls
   251|- [x] Copy message (Sprint 5)
   252|- [x] Edit last user message + regenerate
   253|- [x] Regenerate last assistant response
   254|- [x] Clear conversation (wipe messages, keep session)
   255|
   256|---
   257|
   258|## Wave 4: Settings, Configuration, Notifications
   259|
   260|### Sprint 4.1: Settings Panel
   261|Full settings overlay: default model, default workspace, enabled toolsets, config viewer.
   262|
   263|### Sprint 4.2: Notification Panel
   264|Bell icon with unread count. SSE endpoint for cron completions and errors. Toast pop-ups.
   265|
   266|### Sprint 4.3: Delivery Target Config
   267|Configure and test-ping delivery targets (Discord, Telegram, Slack, email) for cron jobs.
   268|
   269|---
   270|
   271|## Wave 5: Honcho Integration and Long-term Memory
   272|
   273|### Sprint 5.1: Honcho Memory Panel
   274|User representation panel, cross-session context, Honcho search, memory write.
   275|
   276|### Sprint 5.2: Session Continuity Features
   277|"What were we working on?" button, session tags, session archive.
   278|
   279|---
   280|
   281|## Wave 6: Realtime and Agentic Features
   282|
   283|### Sprint 6.1: Background Task Monitor
   284|Live list of running agent threads. Cancel button. Queue visibility.
   285|
   286|### Sprint 6.2: Subagent Delegation Cards
   287|When delegate_task fires, show subagent progress inline in chat.
   288|
   289|### Sprint 6.3: Code Execution Panel
   290|Jupyter-style inline code cell. Stateful kernel per session.
   291|
   292|### Sprint 6.4: Voice Mode
   293|Push-to-talk mic button. Whisper transcription. Optional TTS playback.
   294|
   295|---
   296|
   297|## Wave 7: Production Hardening and Mobile
   298|
   299|### Sprint 7.1: Authentication
   300|HERMES_WEBUI_PASSWORD env var gate. Signed cookie. Login page.
   301|
   302|### Sprint 7.2: HTTPS and Reverse Proxy
   303|Nginx + Let's Encrypt. CORS headers for external domain.
   304|
   305|### Sprint 7.3: Mobile Responsive Layout
   306|Collapsible sidebar hamburger. Touch-friendly controls. Swipe gestures.
   307|
   308|### Sprint 7.4: Performance and Scale
   309|Virtual scroll for session/message lists. Incremental message loading.
   310|