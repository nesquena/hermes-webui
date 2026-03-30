# Hermes Co-Work Web UI: Full Parity Roadmap

> Goal: Full 1:1 parity with the Hermes CLI experience via a clean dark web UI.
> Everything you can do from the CLI terminal, you can do from this UI.
>
> Last updated: Sprint 7 complete (March 31, 2026)
> Tests: 125/125 passing
> Source: /home/hermes/webui-mvp/

---

## Sprint History (Completed)

| Sprint | Theme | Highlights | Tests |
|--------|-------|-----------|-------|
| Sprint 1 | Bug fixes + foundations | B1-B11 fixed, LOCK on SESSIONS, section headers, request logging | 19 |
| Sprint 2 | Rich file preview | Image preview, rendered markdown, table support, smart icons | 27 |
| Sprint 3 | Panel nav + viewers | Sidebar tabs, cron/skills/memory panels, B6/B10/B14, Phase D start | 48 |
| Sprint 4 | Relocation + power features | Source to /home/hermes/webui-mvp/, CSS extracted, session rename/search, file ops | 68 |
| Sprint 5 | Phase A complete + workspace | JS extracted (server.py 1778->1042 lines), workspace management, copy message, file editor, session index | 86 |
| Test hardening | Isolated test environment | Port 8788 test server, conftest autouse, cleanup_zero_message, 5 test files rewritten | 90 |
| Sprint 6 | Polish + Phase E complete | HTML to static/, resizable panels, cron create, session JSON export, Escape from editor | 106 |
| Sprint 7 | Wave 2 Core: CRUD + Search | Cron edit/delete, skill create/edit/delete, memory write, session content search, health improvements, git init | 125 |

---

## Current Architecture Status

| Layer | Location | Status |
|-------|----------|--------|
| Python server | /home/hermes/webui-mvp/server.py (~990 lines) | Pure Python, no inline HTML/CSS/JS |
| HTML template | /home/hermes/webui-mvp/static/index.html | Served from disk |
| CSS | /home/hermes/webui-mvp/static/style.css | Served from disk |
| JavaScript | /home/hermes/webui-mvp/static/app.js | Served from disk |
| Runtime state | /home/hermes/.hermes/webui-mvp/sessions/ | Session JSON files |
| Test server | Port 8788, state dir ~/.hermes/webui-mvp-test/ | Isolated, wiped per run |
| Production server | Port 8787 | SSH tunnel from Mac |

---

## Feature Parity Checklist

### Chat and Agent
- [x] Send messages, get SSE-streaming responses
- [x] Switch models per session (10 models, grouped by provider)
- [x] Upload files to workspace (drag-drop, click, clipboard paste)
- [x] File tray with remove button
- [x] Tool progress shown in activity bar above composer
- [x] Approval card for dangerous commands (Allow once/session/always, Deny)
- [x] Approval polling + SSE-pushed approval events
- [x] INFLIGHT guard: switch sessions mid-request without losing response
- [x] Session restores from localStorage on page load
- [x] Reconnect banner if page reloaded mid-stream
- [x] Copy message to clipboard (hover icon on each bubble)
- [ ] Edit last user message and regenerate (Wave 3)
- [ ] Branch/fork conversation (Wave 3)
- [ ] Token/cost estimate per message (Wave 3)

### Tool Visibility
- [x] Tool progress in activity bar (moved out of composer footer)
- [x] Approval card with all 4 choices
- [ ] Tool call args/results inline (collapsed cards) (Wave 3)

### Workspace / Files
- [x] Browse workspace directory tree with type icons
- [x] Preview text/code files (read-only)
- [x] Preview markdown files (rendered, tables supported)
- [x] Preview image files (PNG, JPG, GIF, SVG, WEBP inline)
- [x] Edit files inline (Edit button, Enter to save, Escape to cancel)
- [x] Create new file (+ button in panel header)
- [x] Delete file (hover trash, confirm dialog)
- [x] File name truncation with tooltip for long names
- [x] Right panel resizable (drag inner edge)
- [ ] Syntax highlighted code preview (Wave 3)
- [ ] Rename file (Wave 3)
- [ ] Create folder (Wave 3)

### Sessions
- [x] Create session (+ button or Cmd/Ctrl+K)
- [x] Load session (click in sidebar)
- [x] Delete session (hover trash, toast, correct fallback)
- [x] Auto-title from first user message
- [x] Rename session title (double-click in sidebar, Enter saves, Escape cancels)
- [x] Filter/search sessions by title (live filter box)
- [x] Date group headers (Today / Yesterday / Earlier)
- [x] Download session as Markdown transcript
- [x] Export session as JSON (full messages + metadata)
- [x] Session inherits last-used workspace on creation
- [x] Session content search (search message text across sessions)
- [ ] Session tags / labels (Wave 5)
- [ ] Archive sessions (Wave 5)
- [ ] Clear conversation (wipe messages, keep session) (Wave 3)
- [ ] Import session from JSON (Wave 3)

### Workspace Management
- [x] Add workspace with path validation (must be existing directory)
- [x] Remove workspace
- [x] Rename workspace display name
- [x] Quick-switch workspace from topbar dropdown
- [x] Sidebar live workspace display (name + path, updates in real time)
- [x] New sessions inherit last used workspace
- [x] Workspace list persists to workspaces.json
- [ ] Workspace reorder (drag) (Wave 2)

### Scheduled Tasks (Cron)
- [x] View all cron jobs (Tasks sidebar tab)
- [x] View last run output per job (auto-loaded on expand)
- [x] Expand job to see prompt, schedule, last output
- [x] Run job manually (Run now button)
- [x] Pause / Resume job
- [x] Create cron job from UI (+ New job form with name, schedule, prompt, delivery)
- [x] Edit existing cron job
- [x] Delete cron job
- [ ] View full run history (Wave 3)
- [ ] Skill picker in cron create form (Wave 3)

### Skills
- [x] List all skills grouped by category (Skills sidebar tab)
- [x] Search/filter skills by name, description, category
- [x] View full SKILL.md content in right preview panel
- [x] Create skill
- [x] Edit skill
- [x] Delete skill
- [ ] View skill linked files (Wave 3)

### Memory
- [x] View personal notes (MEMORY.md) rendered as markdown (Memory tab)
- [x] View user profile (USER.md) rendered as markdown (Memory tab)
- [x] Last-modified timestamp on each section
- [x] Add/edit memory entry inline

### Configuration
- [ ] Settings panel (default model, workspace, toolsets) (Wave 4)
- [ ] Enable/disable toolsets per session (Wave 4)

### Notifications
- [ ] Cron job completion alerts (Wave 4)
- [ ] Background agent error alerts (Wave 4)

### Advanced / Future
- [ ] Voice input via Whisper (Wave 6)
- [ ] TTS playback of responses (Wave 6)
- [ ] Subagent delegation cards (Wave 6)
- [ ] Background task monitor / cancel (Wave 6)
- [ ] Code execution cell (Wave 6)
- [ ] Password authentication (Wave 7)
- [ ] HTTPS / reverse proxy (Wave 7)
- [ ] Mobile responsive layout (Wave 7)
- [ ] Virtual scroll for large lists (Wave 7)

---

## Sprint 7: Wave 2 Core -- Cron/Skill/Memory CRUD + Session Content Search (COMPLETED)

**Theme:** "Wave 2 Core -- Cron/Skill/Memory CRUD + Session Content Search"

### Track A: Bug Fixes
| Item | Description |
|------|-------------|
| Activity bar sizing | Activity bar sometimes overlaps first message on short viewports |
| Model dropdown sync | Model chip in topbar sometimes shows stale model after session switch |
| Cron output truncation | Long cron output in the tasks panel overflows its container |

### Track B: Features
| Feature | What | Value |
|---------|------|-------|
| Session content search | Search message text across all sessions, not just titles. GET /api/sessions/search already does title search; extend to message content with a configurable depth limit | High: the single most-requested nav feature after rename |
| Cron edit + delete | Edit an existing cron job (name, schedule, prompt, delivery) inline in the tasks panel. Delete with confirm. POST /api/crons/update and /api/crons/delete | High: closes the cron CRUD gap (create was Sprint 6) |
| Skill create + edit | A "New skill" form in the Skills panel. Name, category, SKILL.md content in a textarea editor. Save calls POST /api/skills/save (writes to ~/.hermes/skills/). Edit opens existing skill in the same editor | High: biggest remaining CLI gap after cron |

### Track C: Architecture
| Item | What |
|------|------|
| Phase E: app.js module split (start) | Split app.js (1332 lines) into logical modules: sessions.js, chat.js, workspace.js, panels.js, ui.js. Serve via ES module imports in index.html. This is Phase E completion. |
| Health endpoint improvement | Add active_streams, uptime_seconds to /health response (Phase G) |
| Git init | git init /home/hermes/webui-mvp, first commit, push to private GitHub repo |

### Tests
- ~20 new pytest tests (cron update/delete, skill save, session content search)
- TESTING.md: Sections 29-31 (cron edit, skill edit, session search)
- Estimated total after Sprint 7: ~126

---

## Wave 2: Full CRUD and Interaction Parity

**Status:** In progress. Sprint 6 completed cron create and workspace management.
Remaining Wave 2 items targeted for Sprints 7-8.

### Sprint 2.0: Workspace Management (COMPLETE Sprint 5+6)
All workspace features delivered: add/validate/remove/rename workspaces, topbar quick-switch,
sidebar live display, new sessions inherit last workspace. See Sprint 5 completed section.

### Sprint 2.1: Cron Job Management (Partial -- Sprint 7 for remaining)
- [x] View all jobs (Sprint 3)
- [x] Run / pause / resume (Sprint 3)
- [x] Create job from UI (Sprint 6)
- [x] Edit job
- [x] Delete job
- [ ] Full run history (Sprint 7)

### Sprint 2.2: Skill Management (Partial -- Sprint 7 for remaining)
- [x] List all skills with categories (Sprint 3)
- [x] View SKILL.md content (Sprint 3)
- [x] Create skill
- [x] Edit skill
- [x] Delete skill

### Sprint 2.3: Memory Write (Sprint 7)
- [x] View notes + profile (Sprint 3)
- [x] Edit notes inline

### Sprint 2.4: Todo Management (Wave 2)
- [ ] View current todo list
- [ ] Update item status
- [ ] Add / remove items

### Sprint 2.5: Session Content Search (Sprint 7)
- [x] Session title search (Sprint 4)
- [x] Message content search across sessions

### Sprint 2.6: Session Rename (COMPLETE Sprint 4)
Double-click any session title in the left sidebar to edit inline.
Enter saves, Escape cancels. Topbar updates immediately.

---

## Wave 3: Power Features and Developer Experience

### Sprint 3.1: Tool Call Visibility Inline
Show tool calls as collapsible cards in the conversation.
Collapsed: tool name badge + one-line preview. Expanded: full args + result.

### Sprint 3.2: Multi-Model Expansion
Add more models. Group by provider. Model info tooltip on hover.
(Partially done: 10 models in dropdown from Sprint 1.)

### Sprint 3.2b: Resizable Panel Widths (COMPLETE Sprint 6)
Both sidebar and workspace panel are drag-resizable with localStorage persistence.

### Sprint 3.3: Workspace File Actions
- [ ] Rename file (inline, double-click)
- [ ] Create folder
- [ ] Syntax highlighted code preview (Prism.js CDN)

### Sprint 3.4: Conversation Controls
- [x] Copy message (Sprint 5)
- [ ] Edit last user message + regenerate
- [ ] Regenerate last assistant response
- [ ] Clear conversation (wipe messages, keep session)

---

## Wave 4: Settings, Configuration, Notifications

### Sprint 4.1: Settings Panel
Full settings overlay: default model, default workspace, enabled toolsets, config viewer.

### Sprint 4.2: Notification Panel
Bell icon with unread count. SSE endpoint for cron completions and errors. Toast pop-ups.

### Sprint 4.3: Delivery Target Config
Configure and test-ping delivery targets (Discord, Telegram, Slack, email) for cron jobs.

---

## Wave 5: Honcho Integration and Long-term Memory

### Sprint 5.1: Honcho Memory Panel
User representation panel, cross-session context, Honcho search, memory write.

### Sprint 5.2: Session Continuity Features
"What were we working on?" button, session tags, session archive.

---

## Wave 6: Realtime and Agentic Features

### Sprint 6.1: Background Task Monitor
Live list of running agent threads. Cancel button. Queue visibility.

### Sprint 6.2: Subagent Delegation Cards
When delegate_task fires, show subagent progress inline in chat.

### Sprint 6.3: Code Execution Panel
Jupyter-style inline code cell. Stateful kernel per session.

### Sprint 6.4: Voice Mode
Push-to-talk mic button. Whisper transcription. Optional TTS playback.

---

## Wave 7: Production Hardening and Mobile

### Sprint 7.1: Authentication
HERMES_WEBUI_PASSWORD env var gate. Signed cookie. Login page.

### Sprint 7.2: HTTPS and Reverse Proxy
Nginx + Let's Encrypt. CORS headers for external domain.

### Sprint 7.3: Mobile Responsive Layout
Collapsible sidebar hamburger. Touch-friendly controls. Swipe gestures.

### Sprint 7.4: Performance and Scale
Virtual scroll for session/message lists. Incremental message loading.
