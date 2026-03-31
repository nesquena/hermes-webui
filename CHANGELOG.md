     1|# Hermes Co-Work Web UI -- Changelog
     2|
     3|> Living document. Updated at the end of every sprint.
     4|> Source: <repo>/
     5|> Repository: https://github.com/nesquena/hermes-webui
     6|
     7|---
     8|
     9|## [v1.2.2] Concurrency + Correctness Sweeps
    10|*March 31, 2026 | 190 tests*
    11|
    12|Two systematic audits of all concurrent multi-session scenarios. Each finding
    13|became a regression test so it cannot silently return.
    14|
    15|### Sweep 1 (R10-R12)
    16|- **R10: Approval response to wrong session.** `respondApproval()` used
    17|  `S.session.session_id` -- whoever you were viewing. If session A triggered
    18|  a dangerous command requiring approval and you switched to B then clicked
    19|  Allow, the approval went to B's session_id. Agent on A stayed stuck. Fixed:
    20|  approval events tag `_approvalSessionId`; `respondApproval()` uses that.
    21|- **R11: Activity bar showed cross-session tool status.** Session A's tool
    22|  name appeared in session B's activity bar while you were viewing B. Fixed:
    23|  `setStatus()` in the tool SSE handler is now inside the `activeSid` guard.
    24|- **R12: Live tool cards vanished on switch-away and back.** Switching back to
    25|  an in-flight session showed empty live cards even though tools had fired.
    26|  Fixed: `loadSession()` INFLIGHT branch now restores cards from `S.toolCalls`.
    27|
    28|### Sweep 2 (R13-R15)
    29|- **R13: Settled tool cards never rendered after response completes.**
    30|  `renderMessages()` has a `!S.busy` guard on tool card rendering. It was
    31|  called with `S.busy=true` in the done handler -- tool cards were skipped
    32|  every time. Fixed: `S.busy=false` set inline before `renderMessages()`.
    33|- **R14: Wrong model sent for sessions with unlisted model.** `send()` used
    34|  `$('modelSelect').value` which could be stale if the session's model isn't
    35|  in the dropdown. Fixed: now uses `S.session.model || $('modelSelect').value`.
    36|- **R15: Stale live tool cards in new sessions.** `newSession()` didn't call
    37|  `clearLiveToolCards()`. Fixed.
    38|
    39|---
    40|
    41|## [v1.2.1] Sprint 10 Post-Release Fixes
    42|*March 31, 2026 | 177 tests*
    43|
    44|Critical regressions introduced during the server.py split, caught by users and fixed immediately.
    45|
    46|- **`uuid` not imported in server.py** -- `chat/start` returned 500 (NameError) on every new message
    47|- **`AIAgent` not imported in api/streaming.py** -- agent thread crashed immediately, SSE returned 404
    48|- **`has_pending` not imported in api/streaming.py** -- NameError during tool approval checks
    49|- **`Session.__init__` missing `tool_calls` param** -- 500 on any session with tool history
    50|- **SSE loop did not break on `cancel` event** -- connection hung after cancel
    51|- **Regression test file added** (`tests/test_regressions.py`): 10 tests, one per introduced bug. These form a permanent regression gate so each class of error can never silently return.
    52|
    53|---
    54|
    55|## [v1.2] Sprint 10 -- Server Health + Operational Polish
    56|*March 31, 2026 | 167 tests*
    57|
    58|### Post-sprint Bug Fixes
    59|- SSE loop now breaks on `cancel` event (was hanging after cancel)
    60|- `setBusy(false)` now always hides the Cancel button
    61|- `S.activeStreamId` properly initialized in the S global state object
    62|- Tool card "Show more" button uses data attributes instead of inline JSON.stringify (XSS/parse safety)
    63|- Version label updated to v1.2
    64|- `Session.__init__` accepts `**kwargs` for forward-compatibility with future JSON fields
    65|- Test cron jobs now isolated via `HERMES_HOME` env var in conftest (no more pollution of real jobs.json)
    66|- `last_workspace` reset after each test in conftest (prevents workspace state bleed between tests)
    67|- Tool cards now grouped per assistant turn instead of piled before last message
    68|- Tool card insertion uses `data-msg-idx` attribute correctly (was `msgIdx`, matching HTML5 dataset API)
    69|
    70|### Architecture
    71|- **server.py split into api/ modules.** 1,150 lines -> 673 lines in server.py.
    72|  Extracted modules: `api/config.py` (101), `api/helpers.py` (57), `api/models.py` (114),
    73|  `api/workspace.py` (77), `api/upload.py` (77), `api/streaming.py` (187).
    74|  server.py is now the thin routing shell only. All business logic is independently importable.
    75|
    76|### Features
    77|- **Background task cancel.** Red "Cancel" button appears in the activity bar while a task
    78|  is running. Calls `GET /api/chat/cancel?stream_id=X`. The agent thread receives a cancel
    79|  event, emits a 'cancel' SSE event, and the UI shows "*Task cancelled.*" in the conversation.
    80|  Note: a tool call already in progress (e.g. a long terminal command) completes before
    81|  the cancel takes effect -- same behavior as CLI Ctrl+C.
    82|- **Cron run history viewer.** Each job in the Tasks panel now has an "All runs" button.
    83|  Click to expand a list of up to 20 past runs with timestamps, each collapsible to show
    84|  the full output. Click again to hide.
    85|- **Tool card UX polish.** Three improvements:
    86|  1. Pulsing blue dot on cards for in-progress tools (distinct from completed cards)
    87|  2. Smart snippet truncation at sentence boundaries instead of hard byte cutoff
    88|  3. "Show more / Show less" toggle on tool results longer than 220 chars
    89|
    90|---
    91|
    92|## [v1.1] Sprint 9 -- Codebase Health + Daily Driver Gaps
    93|*March 31, 2026 | 149 tests*
    94|
    95|The sprint that closed the last gaps for heavy agentic use.
    96|
    97|### Architecture
    98|- **app.js replaced by 6 modules.** `app.js` is deleted. The browser now loads 6 focused files:
    99|  `ui.js` (530), `workspace.js` (132), `sessions.js` (189), `messages.js` (221),
   100|  `panels.js` (555), `boot.js` (142). The modules are a superset of the original app.js
   101|  (two functions -- `loadTodos`, `toolIcon` -- were added directly to the modules after the split).
   102|  No single file exceeds 555 lines.
   103|
   104|### Features
   105|- **Tool call cards inline.** Every tool Hermes uses now appears as a collapsible card
   106|  in the conversation between the user message and the response. Live during streaming,
   107|  restored from session history on reload. Shows tool name, preview, args, result snippet.
   108|- **Attachment metadata persists on reload.** File badges on user messages survive page
   109|  refresh. Server stores filenames on the user message in session JSON.
   110|- **Todo list panel.** New checkmark tab in the sidebar. Shows current task list parsed
   111|  from the most recent todo tool result in message history. Status icons: pending (○),
   112|  in-progress (◉), completed (✓), cancelled (✗). Auto-refreshes when panel is active.
   113|- **Model preference persists.** Last-used model saved to localStorage. Restored on page
   114|  load. New sessions inherit it automatically.
   115|
   116|### Bug Fixes
   117|- Tool card toggle arrow only shown when card has expandable content
   118|- Attachment tagging matches by message content to avoid wrong-turn tagging
   119|- SSE tool event was missing `args` field
   120|- `/api/session` GET was not returning `tool_calls` (history lost on reload)
   121|
   122|---
   123|
   124|## [v1.0] Sprint 8 -- Daily Driver Finish Line
   125|*March 31, 2026 | 139 tests*
   126|
   127|### Features
   128|- **Edit user message + regenerate.** Hover any user bubble, click the pencil icon.
   129|  Inline textarea, Enter submits, Escape cancels. Truncates session at that point and re-runs.
   130|- **Regenerate last response.** Retry icon on the last assistant bubble only.
   131|- **Clear conversation.** "Clear" button in topbar. Wipes messages, keeps session slot.
   132|- **Syntax highlighting.** Prism.js via CDN (deferred). Python, JS, bash, JSON, SQL and more.
   133|
   134|### Bug Fixes
   135|- Reconnect banner false positive on normal loads (90-second window)
   136|- Session list clipping on short screens
   137|- Favicon 404 console noise (server now returns 204)
   138|- Edit textarea auto-resize on open
   139|- Send button guard while inline edit is active
   140|- Escape closes dropdown, clears search, cancels active edit
   141|- Approval polling not restarted on INFLIGHT session switch-back
   142|- Version label updated to v1.0
   143|
   144|### Hotfix: Message Queue + INFLIGHT
   145|- **Message queue.** Sending while busy queues the message with toast + badge.
   146|  Drains automatically on completion. Cleared on session switch.
   147|- **Message stays visible on switch-away/back.** loadSession checks INFLIGHT before
   148|  server fetch, so sent message and thinking dots persist correctly.
   149|
   150|---
   151|
   152|## [v0.9] Sprint 7 -- Wave 2 Core: CRUD + Search
   153|*March 31, 2026 | 125 tests*
   154|
   155|### Features
   156|- **Cron edit + delete.** Inline edit form per job, save and delete with confirmation.
   157|- **Skill create, edit, delete.** "+ New skill" form in Skills panel. Writes to `~/.hermes/skills/`.
   158|- **Memory inline edit.** "Edit" button opens textarea for MEMORY.md. Saves via `/api/memory/write`.
   159|- **Session content search.** Filter box searches message text (up to 5 messages per session)
   160|  in addition to titles. Debounced API call, results appended below title matches.
   161|
   162|### Architecture
   163|- `/health` now returns `active_streams` and `uptime_seconds`
   164|- `git init` on `<repo>/`, pushed to GitHub
   165|
   166|### Bug Fixes
   167|- Activity bar overlap on short viewports
   168|- Model chip stale after session switch
   169|- Cron output overflow in tasks panel
   170|
   171|---
   172|
   173|## [v0.8] Sprint 6 -- Polish + Phase E Complete
   174|*March 31, 2026 | 106 tests*
   175|
   176|### Architecture
   177|- **Phase E complete.** HTML extracted to `static/index.html`. server.py now pure Python.
   178|  Line count progression: 1778 (Sprint 1) → 1042 (Sprint 5) → 903 (Sprint 6).
   179|- **Phase D complete.** All endpoints validated with proper 400/404 responses.
   180|
   181|### Features
   182|- **Resizable panels.** Sidebar and workspace panel drag-resizable. Widths persisted to localStorage.
   183|- **Create cron job from UI.** "+ New job" form in Tasks panel with name, schedule, prompt, delivery.
   184|- **Session JSON export.** Downloads full session as JSON via "JSON" button in sidebar footer.
   185|- **Escape from file editor.** Cancels inline file edit without saving.
   186|
   187|---
   188|
   189|## [v0.7] Sprint 5 -- Phase A Complete + Workspace Management
   190|*March 30, 2026 | 86 tests*
   191|
   192|### Architecture
   193|- **Phase A complete.** JS extracted to `static/app.js`. server.py: 1778 → 1042 lines.
   194|- **LRU session cache.** `collections.OrderedDict` with cap of 100, oldest evicted automatically.
   195|- **Session index.** `sessions/_index.json` for O(1) session list loads.
   196|- **Isolated test server.** Port 8788 with own state dir, conftest autouse cleanup.
   197|
   198|### Features
   199|- **Workspace management panel.** Add/remove/rename workspaces. Persisted to `workspaces.json`.
   200|- **Topbar workspace quick-switch.** Dropdown chip lists all workspaces, switches on click.
   201|- **New sessions inherit last workspace.** `last_workspace.txt` tracks last used.
   202|- **Copy message to clipboard.** Hover icon on each bubble with checkmark confirmation.
   203|- **Inline file editor.** Preview any file, click Edit to modify, Save writes to disk.
   204|
   205|---
   206|
   207|## [v0.6] Sprint 4 -- Relocation + Session Power Features
   208|*March 30, 2026 | 68 tests*
   209|
   210|### Architecture
   211|- **Source relocated** to `<repo>/` outside the hermes-agent git repo.
   212|  Safe from `git pull`, `git reset`, `git stash`. Symlink maintained at `hermes-agent/webui-mvp`.
   213|- **CSS extracted (Phase A start).** All CSS moved to `static/style.css`.
   214|- **Per-session agent lock (Phase B).** Prevents concurrent requests to same session from
   215|  corrupting environment variables.
   216|
   217|### Features
   218|- **Session rename.** Double-click any title in sidebar to edit inline. Enter saves, Escape cancels.
   219|- **Session search/filter.** Live client-side filter box above session list.
   220|- **File delete.** Hover trash icon on workspace files. Confirm dialog.
   221|- **File create.** "+" button in workspace panel header.
   222|
   223|---
   224|
   225|## [v0.5] Sprint 3 -- Panel Navigation + Feature Viewers
   226|*March 30, 2026 | 48 tests*
   227|
   228|### Features
   229|- **Sidebar panel navigation.** Four tabs: Chat, Tasks, Skills, Memory. Lazy-loads on first open.
   230|- **Tasks panel.** Lists scheduled cron jobs with status badges. Run now, Pause, Resume.
   231|  Shows last run output automatically.
   232|- **Skills panel.** All skills grouped by category. Search/filter. Click to preview SKILL.md.
   233|- **Memory panel.** Renders MEMORY.md and USER.md as formatted markdown with timestamps.
   234|
   235|### Bug Fixes
   236|- B6: New session inherits current workspace
   237|- B10: Tool events replace thinking dots (not stacked alongside)
   238|- B14: Cmd/Ctrl+K creates new chat from anywhere
   239|
   240|---
   241|
   242|## [v0.4] Sprint 2 -- Rich File Preview
   243|*March 30, 2026 | 27 tests*
   244|
   245|### Features
   246|- **Image preview.** PNG, JPG, GIF, SVG, WEBP displayed inline in workspace panel.
   247|- **Rendered markdown.** `.md` files render as formatted HTML in the preview panel.
   248|- **Table support.** Pipe-delimited markdown tables render as HTML tables.
   249|- **Smart file icons.** Type-appropriate icons by extension in the file tree.
   250|- **Preview path bar with type badge.** Colored badge shows file type.
   251|
   252|---
   253|
   254|## [v0.3] Sprint 1 -- Bug Fixes + Foundations
   255|*March 30, 2026 | 19 tests*
   256|
   257|The first sprint. Established the test suite, fixed critical bugs.
   258|
   259|### Bug Fixes
   260|- B1: Approval card now shows pattern keys
   261|- B2: File input accepts valid types only
   262|- B3: Model chip label correct for all 10 models (replaced substring check with dict)
   263|- B4/B5: Reconnect banner on mid-stream reload (localStorage inflight tracking)
   264|- B7: Session titles no longer overflow sidebar
   265|- B9: Empty assistant messages no longer render as blank bubbles
   266|- B11: `/api/session` GET returns 400 (not silent session creation) when ID missing
   267|
   268|### Architecture
   269|- Thread lock on SESSIONS dict
   270|- Structured JSON request logging
   271|- 10-model dropdown with 3 provider groups (OpenAI, Anthropic, Other)
   272|- First test suite: 19 HTTP integration tests
   273|
   274|---
   275|
   276|## [v0.2] UI Polish Pass
   277|*March 30, 2026*
   278|
   279|Visual audit via screenshot analysis. No new features -- design refinement only.
   280|
   281|- Nav tabs: icon-only with CSS tooltip (5 tabs, no overflow)
   282|- Session list: grouped by Today / Yesterday / Earlier
   283|- Active session: blue left border accent
   284|- Role labels: Title Case, softened color, circular icons
   285|- Code blocks: connected language header with separator
   286|- Send button: gradient + hover lift
   287|- Composer: blue glow ring on focus
   288|- Toast: frosted glass with float animation
   289|- Tool status moved from composer footer to activity bar above composer
   290|- Empty session flood fixed (filter + cleanup endpoint + test autouse)
   291|
   292|---
   293|
   294|## [v0.1] Initial Build
   295|*March 30, 2026*
   296|
   297|Single-file web UI for Hermes. stdlib HTTP server, no external dependencies.
   298|Three-panel layout: sessions sidebar, chat area, workspace panel.
   299|
   300|**Core capabilities:**
   301|- Send messages, receive SSE-streamed responses
   302|- Session create/load/delete, auto-title from first message
   303|- File upload with manual multipart parser
   304|- Workspace file tree with directory navigation
   305|- Tool approval card (4 choices: once, session, always, deny)
   306|- INFLIGHT session-switch guard
   307|- 10-model dropdown (OpenAI, Anthropic, Other)
   308|- SSH tunnel access on port 8787
   309|
   310|---
   311|
   312|*Last updated: Sprint 9, March 31, 2026 | Tests: 149/149*
   313|