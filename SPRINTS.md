# Hermes Cowork -- Forward Sprint Plan

> Current state: v1.2.2 | 190 tests | Daily driver ready
> This document plans the path from here to two targets:
>
> Target A: 1:1 feature parity with the Hermes CLI (everything you can do from the
>           terminal, you can do from the browser)
>
> Target B: 1:1 parity with Claude Cowork's reproducible features (the full Claude
>           browser UI experience, minus things only Anthropic can build)
>
> Sprints are ordered by impact. Each builds on the one before.
> Past sprint history lives in CHANGELOG.md.

---

## Where we are now (v1.2.1)

**CLI parity: ~80% complete.** Core agent loop, all tools visible, workspace
file ops, cron/skills/memory CRUD, session management, streaming, cancel --
all solid. Gaps are configuration, subagent visibility, and runtime controls.

**Claude Cowork parity: ~55% complete.** Chat, streaming, file browser,
session management, tool cards, syntax highlighting, model switching -- all
present. Gaps are project organization, artifacts, voice, sharing, mobile.

---

## Sprint 11 -- Streaming Smoothness + Tool Card Incremental Render

**Theme:** Make heavy agentic work feel fast and fluid.

**Why now:** The biggest remaining daily friction point. During a 20-step task,
every tool event triggers a full renderMessages() re-render of the entire
message list. On fast tasks you can see flicker. This is the last thing that
makes the UI feel noticeably worse than watching the CLI.

### Track A: Bugs
- Tool card DOM thrash: renderMessages() rebuilds all cards on each tool event.
  Switch to incremental append (append new card to existing group, no full rebuild).
- Scroll position lost on re-render during streaming (messages jump).

### Track B: Features
- **Incremental tool card streaming:** Instead of renderMessages() on each
  tool event, maintain a live card group element per turn and append/update
  cards in place. The assistant text row below the cards also updates
  incrementally (already does via assistantBody.innerHTML).
- **Tool card collapse-all / expand-all:** A small toggle in the topbar or
  per-message to collapse all tool cards in a response. Useful when a response
  has 10+ tool calls.
- **Smooth scroll:** Pin scroll to bottom during streaming unless user has
  manually scrolled up (read-back mode). Resume pinning when user scrolls
  back to bottom.

### Track C: Architecture
- `api/routes.py`: extract the 49 if/elif route handlers from server.py's
  Handler class into a dedicated routes module. server.py becomes a true
  ~50-line shell: imports, Handler stub that delegates to routes, main().
  Completes the server split started in Sprint 10.

**Tests:** ~12 new. Total: ~196.
**Hermes CLI parity impact:** Low (smoothness, not features)
**Cowork parity impact:** Low

---

## Sprint 12 -- Settings Panel + Toolset Control

**Theme:** Configuration you can actually reach from the UI.

**Why now:** Last remaining thing that forces a trip to the CLI or config files
for basic setup. The model dropdown works but defaults aren't persisted
server-side. Toolsets can't be toggled per session.

### Track A: Bugs
- Model dropdown doesn't sync when a session was created with a model not in
  the current dropdown list (edge case from model additions).
- Workspace validation on add doesn't check symlinks (shows as invalid when
  it's actually a valid symlink to a directory).

### Track B: Features
- **Settings panel:** A gear icon in the topbar opens a slide-in settings panel.
  Sections: Default Model (writes HERMES_WEBUI_DEFAULT_MODEL to a settings file),
  Default Workspace (writes HERMES_WEBUI_DEFAULT_WORKSPACE), UI preferences
  (font size, message density). Persisted server-side in `~/.hermes/webui-mvp/settings.json`.
- **Toolset control per session:** A "Tools" chip in the session topbar opens
  a popover listing all available toolsets (terminal, web, file, memory, etc.)
  with toggles. Selected toolsets stored on the session and passed to AIAgent.
  Matches the `--tools` flag behavior in the CLI.
- **Rename file / Create folder:** Two small file tree ops that close the last
  workspace management gap. Inline rename on double-click (same pattern as
  session rename). Create folder via + menu next to the existing + file button.

### Track C: Architecture
- Settings schema: `settings.json` with typed fields, validated on load, with
  sane defaults. Served via `GET /api/settings`, written via `POST /api/settings`.

**Tests:** ~15 new. Total: ~211.
**Hermes CLI parity impact:** High (toolset control is the last major CLI feature)
**Cowork parity impact:** Medium (settings exist in Cowork as a panel)

---

## Sprint 13 -- Notification System + Background Visibility

**Theme:** Know what Hermes is doing even when you're not watching.

**Why now:** Cron jobs run silently. Background errors surface nowhere. You have
no way to know a long-running task finished (or failed) while you were on another
tab. This is a meaningful daily driver gap for anyone using cron heavily.

### Track A: Bugs
- Cron "Run now" button shows no feedback if the job errors immediately.
- Sessions with very long message histories (100+ messages) cause noticeable
  render lag on load (no virtual scroll yet).

### Track B: Features
- **Cron completion alerts:** When a cron job finishes (success or error), push
  a toast notification to the UI. Use a polling endpoint (`GET /api/crons/status`)
  that the UI checks every 30s while the window is focused. Badge count on the
  Tasks tab icon when there are unread completions.
- **Background agent error alerts:** When a streaming session errors out (network
  drop, model error, tool failure), and the user is not currently viewing that
  session, show a persistent banner: "Session X encountered an error." Clicking
  it navigates to that session.
- **Virtual scroll for session list:** Session list currently renders all sessions
  in the DOM. Above ~100 sessions, the sidebar gets slow. Implement simple virtual
  scroll: render only ~20 visible rows, reuse DOM nodes on scroll.

### Track C: Architecture
- SSE reconnect: if the SSE connection drops mid-stream, auto-reconnect once
  (with the same stream_id). Currently a network blip ends the response silently.

**Tests:** ~14 new. Total: ~225.
**Hermes CLI parity impact:** High (cron visibility, error surfacing)
**Cowork parity impact:** Medium (Cowork has notification panel)

---

## Sprint 14 -- Project Organization + Session Management

**Theme:** Organize work the way you think, not just chronologically.

**Why now:** After 100+ sessions the sidebar is a flat chronological list.
Finding sessions from 2 weeks ago, or keeping a "MyProject" workspace separate
from personal work, requires the search box. This is the biggest remaining
daily organizational gap vs. Claude Cowork's project folders.

### Track A: Bugs
- Session search content scan (depth=5) is slow on large session histories.
  Add server-side caching of search index.
- Date group headers ("Today / Yesterday / Earlier") use updated_at which can
  be misleading for sessions touched by automated title-setting. Use created_at
  for initial grouping, updated_at for sort order.

### Track B: Features
- **Session folders / projects:** A "Projects" section above the session list.
  Each project is a named group. Sessions can be dragged into projects or
  assigned via right-click. Stored in `projects.json`. Projects collapse/expand.
  This is the single biggest Cowork parity feature missing.
- **Pin sessions:** Star icon on any session to pin it to the top of the list
  above date groups. Persisted on the session JSON as `pinned: true`.
- **Session tags:** Inline `#tag` syntax in session titles gets extracted and
  shown as colored chips. Clicking a tag filters the list. No backend change
  needed -- parsed client-side from title text.
- **Archive sessions:** A "More" overflow menu on each session (right-click or
  long-press) with: Archive (hides from main list, accessible via filter),
  Duplicate (new session with same workspace/model), Export JSON.
- **Import session from JSON:** Drag a `.json` export file into the sidebar to
  restore it as a new session. Mirrors the existing JSON export.

### Track C: Architecture
- Session index v2: extend `_index.json` to include `tags`, `pinned`, and
  `project_id` fields. Rebuild on session save. Enables fast client-side
  filtering without disk reads.

**Tests:** ~16 new. Total: ~241.
**Hermes CLI parity impact:** Low (CLI has no session organization)
**Cowork parity impact:** Very High (projects are a core Cowork concept)

---

## Sprint 15 -- Artifacts + Code Execution

**Theme:** See outputs, not just text.

**Why now:** Claude Cowork's most distinctive feature is the artifact panel --
code runs inline, HTML renders in a sandboxed iframe, SVGs show as images.
This is the largest single capability gap between what we have and what Cowork
feels like. It also directly enables the Hermes "code execution cell" feature
(Jupyter-style in-browser execution).

### Track A: Bugs
- Prism.js autoloader makes one CDN request per language encountered. On a
  code-heavy session this causes noticeable latency. Bundle the top 10 languages
  (Python, JS, bash, JSON, SQL, YAML, TypeScript, CSS, HTML, Rust) locally.
- Code blocks in long responses sometimes re-highlight on every renderMessages()
  call. Debounce highlightCode() with requestAnimationFrame.

### Track B: Features
- **Artifact panel:** When Hermes produces a code block tagged as `html`, `svg`,
  or `react`, a "Preview" button appears on that code block. Clicking it opens
  a sandboxed `<iframe>` in the right panel showing the rendered output. The
  preview updates live if Hermes edits the artifact in a follow-up.
- **Code execution cell:** A "Run" button on Python code blocks. Sends the code
  to a new server endpoint (`POST /api/execute`) which runs it in a subprocess
  with a 30-second timeout and streams stdout/stderr back as SSE. Output appears
  below the code block inline. This is the Jupyter cell experience without
  needing a kernel.
- **Mermaid diagram rendering:** Mermaid.js CDN (deferred). Code blocks tagged
  as `mermaid` render as flow/sequence/gantt diagrams inline.

### Track C: Architecture
- Sandbox safety: `/api/execute` runs in a restricted subprocess (no network,
  limited filesystem via a temp directory). Returns exit code, stdout, stderr,
  and execution time.
- Artifact state: artifacts are tracked in `S.artifacts = {}` (code block hash
  -> rendered content). Persisted in session JSON as `artifacts` array.

**Tests:** ~18 new. Total: ~259.
**Hermes CLI parity impact:** High (code execution closes the Jupyter gap)
**Cowork parity impact:** Very High (artifacts are Cowork's signature feature)

---

## Sprint 16 -- Voice + Multimodal Input

**Theme:** Input beyond the keyboard.

**Why now:** Voice is a meaningful quality-of-life feature for longer sessions
and is achievable with Whisper. Image input closes the last modality gap with
Claude Cowork (Cowork accepts image paste natively -- we do too, but only as
file uploads, not clipboard screenshots into the conversation directly).

### Track A: Bugs
- Image paste currently requires a click-to-attach flow. Direct paste into the
  message textarea should embed the image inline (as a preview chip) and queue
  it for upload on Send. (Partially works -- clean up edge cases.)
- Large image uploads (>5MB) time out the upload step silently.

### Track B: Features
- **Voice input (Whisper):** A microphone icon in the composer. Hold to record,
  release to transcribe via `POST /api/transcribe` (calls local Whisper or
  OpenAI Whisper API). Transcribed text appears in the message input, editable
  before send. Supports the full "voice -> text -> Hermes response" loop.
- **TTS playback:** A speaker icon on assistant messages. Calls a TTS endpoint
  (ElevenLabs or OpenAI TTS) and plays the audio. Toggle per-message. Optional
  auto-play mode in settings.
- **Vision input improvements:** Paste a screenshot directly from clipboard into
  the conversation (not just the tray). Shows as an inline preview chip with
  the image thumbnail. On Send, uploads and includes in the message.

### Track C: Architecture
- Audio pipeline: `POST /api/transcribe` streams audio bytes, returns transcript.
  `GET /api/tts?text=...` returns audio/mpeg. Both use lazy import of Whisper
  and TTS libraries to keep cold start fast.

**Tests:** ~12 new. Total: ~271.
**Hermes CLI parity impact:** Medium (voice not in CLI, but adds capability)
**Cowork parity impact:** High (Cowork has native voice mode)

---

## Sprint 17 -- Subagent Visibility + Agentic Transparency

**Theme:** Watch Hermes think, not just respond.

**Why now:** When Hermes delegates to subagents (delegate_task, spawns parallel
workstreams), the UI shows nothing. On long multi-agent tasks you have no idea
what's happening. This is the last major "CLI feels better" gap for power users.

### Track A: Bugs
- Tool cards for delegate_task show no information about what the subagent was
  asked to do or what it returned.
- The activity bar text truncates at 55 chars -- tool previews for long terminal
  commands show nothing useful.

### Track B: Features
- **Subagent delegation cards:** When `delegate_task` fires, show an expandable
  card with the subagent's goal, status (pending/running/done), and result
  summary. Multiple subagents from one call appear as a card group. Uses the
  existing tool card infrastructure.
- **Background task monitor:** A "Tasks" indicator in the topbar (separate from
  the cron Tasks panel). Shows count of active agent threads. Click opens a
  popover listing all in-flight streams with session names and elapsed times.
  Cancel any individual thread. This is the full job queue visibility the CLI
  implicitly has via `ps aux`.
- **Thinking/reasoning display:** When the model emits reasoning tokens (o3,
  Claude extended thinking), show them in a collapsible "Reasoning" card above
  the response. Collapsed by default. This matches Cowork's reasoning display.

### Track C: Architecture
- Task registry: extend STREAMS to include session name, start time, and task
  description. New `GET /api/tasks/active` endpoint returns all running streams
  with metadata.

**Tests:** ~14 new. Total: ~285.
**Hermes CLI parity impact:** Very High (subagent and task visibility is the
  last major CLI gap)
**Cowork parity impact:** High (Cowork shows reasoning, tool use visibly)

---

## Sprint 18 -- Auth, HTTPS, and Production Hardening

**Theme:** Make this safe to leave running.

**Why now:** Everything else is done. This is the sprint you run when you want
to expose the UI beyond localhost -- to a team, a mobile device, or a public
address.

### Track A: Bugs
- Server has no request size limit on non-upload endpoints (potential DoS).
- Session JSON files have no size cap (a runaway agent could write GBs).

### Track B: Features
- **Password authentication:** A login page with a configurable password
  (HERMES_WEBUI_PASSWORD env var). Signed cookie session (24h expiry).
  Single-user model -- no accounts, no registration.
- **HTTPS / reverse proxy guide:** A one-page `DEPLOY.md` with instructions
  for running behind nginx + Let's Encrypt on a VPS. Configuration snippets
  for systemd service, nginx config, certbot.
- **Mobile responsive layout:** Collapsible sidebar (hamburger). Touch-friendly
  session list (swipe to delete, tap to navigate). Composer expands on focus.
  Right panel hidden by default on mobile, accessible via a Files tab.
- **Rate limiting:** Simple per-IP token bucket on the chat/start endpoint
  (configurable, default 10 req/min) to prevent accidental hammering.

### Track C: Architecture
- Helmet headers: X-Content-Type-Options, X-Frame-Options, HSTS (when served
  over HTTPS). Simple middleware in the Handler.

**Tests:** ~12 new. Total: ~297.
**Hermes CLI parity impact:** Low (CLI has no auth/HTTPS concerns)
**Cowork parity impact:** Very High (Cowork is authenticated, HTTPS only)

---

## Feature Parity Summary

### After Sprint 17 (Hermes CLI parity: complete)

| CLI Feature | Status |
|-------------|--------|
| Chat / agent loop | Done (v0.3) |
| Streaming responses | Done (v0.5) |
| Tool call visibility | Done (v1.1) |
| File ops (read/write/search/patch) | Done (v0.6) |
| Terminal commands | Done via workspace |
| Cron job management | Done (v0.9) |
| Skills management | Done (v0.9) |
| Memory read/write | Done (v0.9) |
| Session history | Done (v0.3) |
| Workspace switching | Done (v0.7) |
| Model selection | Done (v0.3) |
| Toolset control | Sprint 12 |
| Settings persistence | Sprint 12 |
| Subagent visibility | Sprint 17 |
| Background task monitor | Sprint 17 |
| Code execution (Jupyter) | Sprint 15 |
| Cron completion alerts | Sprint 13 |
| Virtual scroll (perf) | Sprint 13 |

### After Sprint 18 (Claude Cowork parity: ~90% complete)

| Cowork Feature | Status |
|----------------|--------|
| Dark theme, 3-panel layout | Done (v0.1) |
| Streaming chat | Done (v0.5) |
| Model switching | Done (v0.3) |
| File attachments | Done (v0.6) |
| Syntax highlighting | Done (v1.0) |
| Tool use visibility | Done (v1.1) |
| Edit/regenerate messages | Done (v1.0) |
| Session management | Done (v0.6) |
| Artifacts (HTML/SVG preview) | Sprint 15 |
| Code execution inline | Sprint 15 |
| Mermaid diagrams | Sprint 15 |
| Projects / folders | Sprint 14 |
| Pinned/starred sessions | Sprint 14 |
| Reasoning display | Sprint 17 |
| Voice input | Sprint 16 |
| TTS playback | Sprint 16 |
| Notifications | Sprint 13 |
| Settings panel | Sprint 12 |
| Auth / login | Sprint 18 |
| HTTPS | Sprint 18 |
| Mobile layout | Sprint 18 |
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

*Last updated: March 31, 2026*
*Current version: v1.2.2 | 190 tests*
*Next sprint: Sprint 11 (streaming smoothness + api/routes.py split)*
