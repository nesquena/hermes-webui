     1|# Hermes Cowork -- Forward Sprint Plan
     2|
     3|> Current state: v1.2.2 | 190 tests | Daily driver ready
     4|> This document plans the path from here to two targets:
     5|>
     6|> Target A: 1:1 feature parity with the Hermes CLI (everything you can do from the
     7|>           terminal, you can do from the browser)
     8|>
     9|> Target B: 1:1 parity with Claude Cowork's reproducible features (the full Claude
    10|>           browser UI experience, minus things only Anthropic can build)
    11|>
    12|> Sprints are ordered by impact. Each builds on the one before.
    13|> Past sprint history lives in CHANGELOG.md.
    14|
    15|---
    16|
    17|## Where we are now (v1.2.1)
    18|
    19|**CLI parity: ~80% complete.** Core agent loop, all tools visible, workspace
    20|file ops, cron/skills/memory CRUD, session management, streaming, cancel --
    21|all solid. Gaps are configuration, subagent visibility, and runtime controls.
    22|
    23|**Claude Cowork parity: ~55% complete.** Chat, streaming, file browser,
    24|session management, tool cards, syntax highlighting, model switching -- all
    25|present. Gaps are project organization, artifacts, voice, sharing, mobile.
    26|
    27|---
    28|
    29|## Sprint 11 -- Streaming Smoothness + Tool Card Incremental Render
    30|
    31|**Theme:** Make heavy agentic work feel fast and fluid.
    32|
    33|**Why now:** The biggest remaining daily friction point. During a 20-step task,
    34|every tool event triggers a full renderMessages() re-render of the entire
    35|message list. On fast tasks you can see flicker. This is the last thing that
    36|makes the UI feel noticeably worse than watching the CLI.
    37|
    38|### Track A: Bugs
    39|- Tool card DOM thrash: renderMessages() rebuilds all cards on each tool event.
    40|  Switch to incremental append (append new card to existing group, no full rebuild).
    41|- Scroll position lost on re-render during streaming (messages jump).
    42|
    43|### Track B: Features
    44|- **Incremental tool card streaming:** Instead of renderMessages() on each
    45|  tool event, maintain a live card group element per turn and append/update
    46|  cards in place. The assistant text row below the cards also updates
    47|  incrementally (already does via assistantBody.innerHTML).
    48|- **Tool card collapse-all / expand-all:** A small toggle in the topbar or
    49|  per-message to collapse all tool cards in a response. Useful when a response
    50|  has 10+ tool calls.
    51|- **Smooth scroll:** Pin scroll to bottom during streaming unless user has
    52|  manually scrolled up (read-back mode). Resume pinning when user scrolls
    53|  back to bottom.
    54|
    55|### Track C: Architecture
    56|- `api/routes.py`: extract the 49 if/elif route handlers from server.py's
    57|  Handler class into a dedicated routes module. server.py becomes a true
    58|  ~50-line shell: imports, Handler stub that delegates to routes, main().
    59|  Completes the server split started in Sprint 10.
    60|
    61|**Tests:** ~12 new. Total: ~196.
    62|**Hermes CLI parity impact:** Low (smoothness, not features)
    63|**Cowork parity impact:** Low
    64|
    65|---
    66|
    67|## Sprint 12 -- Settings Panel + Toolset Control
    68|
    69|**Theme:** Configuration you can actually reach from the UI.
    70|
    71|**Why now:** Last remaining thing that forces a trip to the CLI or config files
    72|for basic setup. The model dropdown works but defaults aren't persisted
    73|server-side. Toolsets can't be toggled per session.
    74|
    75|### Track A: Bugs
    76|- Model dropdown doesn't sync when a session was created with a model not in
    77|  the current dropdown list (edge case from model additions).
    78|- Workspace validation on add doesn't check symlinks (shows as invalid when
    79|  it's actually a valid symlink to a directory).
    80|
    81|### Track B: Features
    82|- **Settings panel:** A gear icon in the topbar opens a slide-in settings panel.
    83|  Sections: Default Model (writes HERMES_WEBUI_DEFAULT_MODEL to a settings file),
    84|  Default Workspace (writes HERMES_WEBUI_DEFAULT_WORKSPACE), UI preferences
    85|  (font size, message density). Persisted server-side in `~/.hermes/webui-mvp/settings.json`.
    86|- **Toolset control per session:** A "Tools" chip in the session topbar opens
    87|  a popover listing all available toolsets (terminal, web, file, memory, etc.)
    88|  with toggles. Selected toolsets stored on the session and passed to AIAgent.
    89|  Matches the `--tools` flag behavior in the CLI.
    90|- **Rename file / Create folder:** Two small file tree ops that close the last
    91|  workspace management gap. Inline rename on double-click (same pattern as
    92|  session rename). Create folder via + menu next to the existing + file button.
    93|
    94|### Track C: Architecture
    95|- Settings schema: `settings.json` with typed fields, validated on load, with
    96|  sane defaults. Served via `GET /api/settings`, written via `POST /api/settings`.
    97|
    98|**Tests:** ~15 new. Total: ~211.
    99|**Hermes CLI parity impact:** High (toolset control is the last major CLI feature)
   100|**Cowork parity impact:** Medium (settings exist in Cowork as a panel)
   101|
   102|---
   103|
   104|## Sprint 13 -- Notification System + Background Visibility
   105|
   106|**Theme:** Know what Hermes is doing even when you're not watching.
   107|
   108|**Why now:** Cron jobs run silently. Background errors surface nowhere. You have
   109|no way to know a long-running task finished (or failed) while you were on another
   110|tab. This is a meaningful daily driver gap for anyone using cron heavily.
   111|
   112|### Track A: Bugs
   113|- Cron "Run now" button shows no feedback if the job errors immediately.
   114|- Sessions with very long message histories (100+ messages) cause noticeable
   115|  render lag on load (no virtual scroll yet).
   116|
   117|### Track B: Features
   118|- **Cron completion alerts:** When a cron job finishes (success or error), push
   119|  a toast notification to the UI. Use a polling endpoint (`GET /api/crons/status`)
   120|  that the UI checks every 30s while the window is focused. Badge count on the
   121|  Tasks tab icon when there are unread completions.
   122|- **Background agent error alerts:** When a streaming session errors out (network
   123|  drop, model error, tool failure), and the user is not currently viewing that
   124|  session, show a persistent banner: "Session X encountered an error." Clicking
   125|  it navigates to that session.
   126|- **Virtual scroll for session list:** Session list currently renders all sessions
   127|  in the DOM. Above ~100 sessions, the sidebar gets slow. Implement simple virtual
   128|  scroll: render only ~20 visible rows, reuse DOM nodes on scroll.
   129|
   130|### Track C: Architecture
   131|- SSE reconnect: if the SSE connection drops mid-stream, auto-reconnect once
   132|  (with the same stream_id). Currently a network blip ends the response silently.
   133|
   134|**Tests:** ~14 new. Total: ~225.
   135|**Hermes CLI parity impact:** High (cron visibility, error surfacing)
   136|**Cowork parity impact:** Medium (Cowork has notification panel)
   137|
   138|---
   139|
   140|## Sprint 14 -- Project Organization + Session Management
   141|
   142|**Theme:** Organize work the way you think, not just chronologically.
   143|
   144|**Why now:** After 100+ sessions the sidebar is a flat chronological list.
   145|Finding sessions from 2 weeks ago, or keeping a "MyProject" workspace separate
   146|from personal work, requires the search box. This is the biggest remaining
   147|daily organizational gap vs. Claude Cowork's project folders.
   148|
   149|### Track A: Bugs
   150|- Session search content scan (depth=5) is slow on large session histories.
   151|  Add server-side caching of search index.
   152|- Date group headers ("Today / Yesterday / Earlier") use updated_at which can
   153|  be misleading for sessions touched by automated title-setting. Use created_at
   154|  for initial grouping, updated_at for sort order.
   155|
   156|### Track B: Features
   157|- **Session folders / projects:** A "Projects" section above the session list.
   158|  Each project is a named group. Sessions can be dragged into projects or
   159|  assigned via right-click. Stored in `projects.json`. Projects collapse/expand.
   160|  This is the single biggest Cowork parity feature missing.
   161|- **Pin sessions:** Star icon on any session to pin it to the top of the list
   162|  above date groups. Persisted on the session JSON as `pinned: true`.
   163|- **Session tags:** Inline `#tag` syntax in session titles gets extracted and
   164|  shown as colored chips. Clicking a tag filters the list. No backend change
   165|  needed -- parsed client-side from title text.
   166|- **Archive sessions:** A "More" overflow menu on each session (right-click or
   167|  long-press) with: Archive (hides from main list, accessible via filter),
   168|  Duplicate (new session with same workspace/model), Export JSON.
   169|- **Import session from JSON:** Drag a `.json` export file into the sidebar to
   170|  restore it as a new session. Mirrors the existing JSON export.
   171|
   172|### Track C: Architecture
   173|- Session index v2: extend `_index.json` to include `tags`, `pinned`, and
   174|  `project_id` fields. Rebuild on session save. Enables fast client-side
   175|  filtering without disk reads.
   176|
   177|**Tests:** ~16 new. Total: ~241.
   178|**Hermes CLI parity impact:** Low (CLI has no session organization)
   179|**Cowork parity impact:** Very High (projects are a core Cowork concept)
   180|
   181|---
   182|
   183|## Sprint 15 -- Artifacts + Code Execution
   184|
   185|**Theme:** See outputs, not just text.
   186|
   187|**Why now:** Claude Cowork's most distinctive feature is the artifact panel --
   188|code runs inline, HTML renders in a sandboxed iframe, SVGs show as images.
   189|This is the largest single capability gap between what we have and what Cowork
   190|feels like. It also directly enables the Hermes "code execution cell" feature
   191|(Jupyter-style in-browser execution).
   192|
   193|### Track A: Bugs
   194|- Prism.js autoloader makes one CDN request per language encountered. On a
   195|  code-heavy session this causes noticeable latency. Bundle the top 10 languages
   196|  (Python, JS, bash, JSON, SQL, YAML, TypeScript, CSS, HTML, Rust) locally.
   197|- Code blocks in long responses sometimes re-highlight on every renderMessages()
   198|  call. Debounce highlightCode() with requestAnimationFrame.
   199|
   200|### Track B: Features
   201|- **Artifact panel:** When Hermes produces a code block tagged as `html`, `svg`,
   202|  or `react`, a "Preview" button appears on that code block. Clicking it opens
   203|  a sandboxed `<iframe>` in the right panel showing the rendered output. The
   204|  preview updates live if Hermes edits the artifact in a follow-up.
   205|- **Code execution cell:** A "Run" button on Python code blocks. Sends the code
   206|  to a new server endpoint (`POST /api/execute`) which runs it in a subprocess
   207|  with a 30-second timeout and streams stdout/stderr back as SSE. Output appears
   208|  below the code block inline. This is the Jupyter cell experience without
   209|  needing a kernel.
   210|- **Mermaid diagram rendering:** Mermaid.js CDN (deferred). Code blocks tagged
   211|  as `mermaid` render as flow/sequence/gantt diagrams inline.
   212|
   213|### Track C: Architecture
   214|- Sandbox safety: `/api/execute` runs in a restricted subprocess (no network,
   215|  limited filesystem via a temp directory). Returns exit code, stdout, stderr,
   216|  and execution time.
   217|- Artifact state: artifacts are tracked in `S.artifacts = {}` (code block hash
   218|  -> rendered content). Persisted in session JSON as `artifacts` array.
   219|
   220|**Tests:** ~18 new. Total: ~259.
   221|**Hermes CLI parity impact:** High (code execution closes the Jupyter gap)
   222|**Cowork parity impact:** Very High (artifacts are Cowork's signature feature)
   223|
   224|---
   225|
   226|## Sprint 16 -- Voice + Multimodal Input
   227|
   228|**Theme:** Input beyond the keyboard.
   229|
   230|**Why now:** Voice is a meaningful quality-of-life feature for longer sessions
   231|and is achievable with Whisper. Image input closes the last modality gap with
   232|Claude Cowork (Cowork accepts image paste natively -- we do too, but only as
   233|file uploads, not clipboard screenshots into the conversation directly).
   234|
   235|### Track A: Bugs
   236|- Image paste currently requires a click-to-attach flow. Direct paste into the
   237|  message textarea should embed the image inline (as a preview chip) and queue
   238|  it for upload on Send. (Partially works -- clean up edge cases.)
   239|- Large image uploads (>5MB) time out the upload step silently.
   240|
   241|### Track B: Features
   242|- **Voice input (Whisper):** A microphone icon in the composer. Hold to record,
   243|  release to transcribe via `POST /api/transcribe` (calls local Whisper or
   244|  OpenAI Whisper API). Transcribed text appears in the message input, editable
   245|  before send. Supports the full "voice -> text -> Hermes response" loop.
   246|- **TTS playback:** A speaker icon on assistant messages. Calls a TTS endpoint
   247|  (ElevenLabs or OpenAI TTS) and plays the audio. Toggle per-message. Optional
   248|  auto-play mode in settings.
   249|- **Vision input improvements:** Paste a screenshot directly from clipboard into
   250|  the conversation (not just the tray). Shows as an inline preview chip with
   251|  the image thumbnail. On Send, uploads and includes in the message.
   252|
   253|### Track C: Architecture
   254|- Audio pipeline: `POST /api/transcribe` streams audio bytes, returns transcript.
   255|  `GET /api/tts?text=...` returns audio/mpeg. Both use lazy import of Whisper
   256|  and TTS libraries to keep cold start fast.
   257|
   258|**Tests:** ~12 new. Total: ~271.
   259|**Hermes CLI parity impact:** Medium (voice not in CLI, but adds capability)
   260|**Cowork parity impact:** High (Cowork has native voice mode)
   261|
   262|---
   263|
   264|## Sprint 17 -- Subagent Visibility + Agentic Transparency
   265|
   266|**Theme:** Watch Hermes think, not just respond.
   267|
   268|**Why now:** When Hermes delegates to subagents (delegate_task, spawns parallel
   269|workstreams), the UI shows nothing. On long multi-agent tasks you have no idea
   270|what's happening. This is the last major "CLI feels better" gap for power users.
   271|
   272|### Track A: Bugs
   273|- Tool cards for delegate_task show no information about what the subagent was
   274|  asked to do or what it returned.
   275|- The activity bar text truncates at 55 chars -- tool previews for long terminal
   276|  commands show nothing useful.
   277|
   278|### Track B: Features
   279|- **Subagent delegation cards:** When `delegate_task` fires, show an expandable
   280|  card with the subagent's goal, status (pending/running/done), and result
   281|  summary. Multiple subagents from one call appear as a card group. Uses the
   282|  existing tool card infrastructure.
   283|- **Background task monitor:** A "Tasks" indicator in the topbar (separate from
   284|  the cron Tasks panel). Shows count of active agent threads. Click opens a
   285|  popover listing all in-flight streams with session names and elapsed times.
   286|  Cancel any individual thread. This is the full job queue visibility the CLI
   287|  implicitly has via `ps aux`.
   288|- **Thinking/reasoning display:** When the model emits reasoning tokens (o3,
   289|  Claude extended thinking), show them in a collapsible "Reasoning" card above
   290|  the response. Collapsed by default. This matches Cowork's reasoning display.
   291|
   292|### Track C: Architecture
   293|- Task registry: extend STREAMS to include session name, start time, and task
   294|  description. New `GET /api/tasks/active` endpoint returns all running streams
   295|  with metadata.
   296|
   297|**Tests:** ~14 new. Total: ~285.
   298|**Hermes CLI parity impact:** Very High (subagent and task visibility is the
   299|  last major CLI gap)
   300|**Cowork parity impact:** High (Cowork shows reasoning, tool use visibly)
   301|
   302|---
   303|
   304|## Sprint 18 -- Auth, HTTPS, and Production Hardening
   305|
   306|**Theme:** Make this safe to leave running.
   307|
   308|**Why now:** Everything else is done. This is the sprint you run when you want
   309|to expose the UI beyond localhost -- to a team, a mobile device, or a public
   310|address.
   311|
   312|### Track A: Bugs
   313|- Server has no request size limit on non-upload endpoints (potential DoS).
   314|- Session JSON files have no size cap (a runaway agent could write GBs).
   315|
   316|### Track B: Features
   317|- **Password authentication:** A login page with a configurable password
   318|  (HERMES_WEBUI_PASSWORD env var). Signed cookie session (24h expiry).
   319|  Single-user model -- no accounts, no registration.
   320|- **HTTPS / reverse proxy guide:** A one-page `DEPLOY.md` with instructions
   321|  for running behind nginx + Let's Encrypt on a VPS. Configuration snippets
   322|  for systemd service, nginx config, certbot.
   323|- **Mobile responsive layout:** Collapsible sidebar (hamburger). Touch-friendly
   324|  session list (swipe to delete, tap to navigate). Composer expands on focus.
   325|  Right panel hidden by default on mobile, accessible via a Files tab.
   326|- **Rate limiting:** Simple per-IP token bucket on the chat/start endpoint
   327|  (configurable, default 10 req/min) to prevent accidental hammering.
   328|
   329|### Track C: Architecture
   330|- Helmet headers: X-Content-Type-Options, X-Frame-Options, HSTS (when served
   331|  over HTTPS). Simple middleware in the Handler.
   332|
   333|**Tests:** ~12 new. Total: ~297.
   334|**Hermes CLI parity impact:** Low (CLI has no auth/HTTPS concerns)
   335|**Cowork parity impact:** Very High (Cowork is authenticated, HTTPS only)
   336|
   337|---
   338|
   339|## Feature Parity Summary
   340|
   341|### After Sprint 17 (Hermes CLI parity: complete)
   342|
   343|| CLI Feature | Status |
   344||-------------|--------|
   345|| Chat / agent loop | Done (v0.3) |
   346|| Streaming responses | Done (v0.5) |
   347|| Tool call visibility | Done (v1.1) |
   348|| File ops (read/write/search/patch) | Done (v0.6) |
   349|| Terminal commands | Done via workspace |
   350|| Cron job management | Done (v0.9) |
   351|| Skills management | Done (v0.9) |
   352|| Memory read/write | Done (v0.9) |
   353|| Session history | Done (v0.3) |
   354|| Workspace switching | Done (v0.7) |
   355|| Model selection | Done (v0.3) |
   356|| Toolset control | Sprint 12 |
   357|| Settings persistence | Sprint 12 |
   358|| Subagent visibility | Sprint 17 |
   359|| Background task monitor | Sprint 17 |
   360|| Code execution (Jupyter) | Sprint 15 |
   361|| Cron completion alerts | Sprint 13 |
   362|| Virtual scroll (perf) | Sprint 13 |
   363|
   364|### After Sprint 18 (Claude Cowork parity: ~90% complete)
   365|
   366|| Cowork Feature | Status |
   367||----------------|--------|
   368|| Dark theme, 3-panel layout | Done (v0.1) |
   369|| Streaming chat | Done (v0.5) |
   370|| Model switching | Done (v0.3) |
   371|| File attachments | Done (v0.6) |
   372|| Syntax highlighting | Done (v1.0) |
   373|| Tool use visibility | Done (v1.1) |
   374|| Edit/regenerate messages | Done (v1.0) |
   375|| Session management | Done (v0.6) |
   376|| Artifacts (HTML/SVG preview) | Sprint 15 |
   377|| Code execution inline | Sprint 15 |
   378|| Mermaid diagrams | Sprint 15 |
   379|| Projects / folders | Sprint 14 |
   380|| Pinned/starred sessions | Sprint 14 |
   381|| Reasoning display | Sprint 17 |
   382|| Voice input | Sprint 16 |
   383|| TTS playback | Sprint 16 |
   384|| Notifications | Sprint 13 |
   385|| Settings panel | Sprint 12 |
   386|| Auth / login | Sprint 18 |
   387|| HTTPS | Sprint 18 |
   388|| Mobile layout | Sprint 18 |
   389|| Sharing / public URLs | Not planned (requires server infra) |
   390|| Claude-specific features | Not replicable (Projects AI, artifacts sync) |
   391|
   392|### What is intentionally not planned
   393|
   394|- **Sharing / public conversation URLs:** Requires a hosted backend with access
   395|  control and CDN. Out of scope for a personal VPS deployment.
   396|- **Claude-specific model features:** Claude-native Projects memory, extended
   397|  artifacts sync, Anthropic's proprietary reasoning UI. These are Anthropic
   398|  infrastructure, not reproducible.
   399|- **Real-time collaboration:** Multiple users in the same session simultaneously.
   400|  Single-user assumption throughout.
   401|- **Plugin marketplace:** Hermes skills cover this use case already.
   402|
   403|---
   404|
   405|*Last updated: March 31, 2026*
   406|*Current version: v1.2.2 | 190 tests*
   407|*Next sprint: Sprint 11 (streaming smoothness + api/routes.py split)*
   408|