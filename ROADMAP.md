# Hermes Web UI: Full Parity Roadmap

> Goal: Full 1:1 parity with the Hermes CLI experience via a clean dark web UI.
> Everything you can do from the CLI terminal, you can do from this UI.
>
> Last updated: v0.50.21 (April 13, 2026) — 961 tests, 961 passing
> Full production-ready: onboarding wizard, multi-profile support, KaTeX math rendering,
> live reasoning cards with localStorage reload recovery, CSRF reverse proxy fixes, Docker improvements.
> Tests: 961 total (961 passing, 0 failures)
> Source: <repo>/

---

## Sprint History (Completed)

| Sprint | Theme | Highlights | Tests |
|--------|-------|-----------|-------|
| Sprint 1 | Bug fixes + foundations | B1-B11 fixed, LOCK on SESSIONS, section headers, request logging | 19 |
| Sprint 2 | Rich file preview | Image preview, rendered markdown, table support, smart icons | 27 |
| Sprint 3 | Panel nav + viewers | Sidebar tabs, cron/skills/memory panels, B6/B10/B14, Phase D start | 48 |
| Sprint 4 | Relocation + power features | Source to <repo>/, CSS extracted, session rename/search, file ops | 68 |
| Sprint 5 | Phase A complete + workspace | JS extracted (server.py 1778->1042 lines), workspace management, copy message, file editor, session index | 86 |
| Test hardening | Isolated test environment | Port 8788 test server, conftest autouse, cleanup_zero_message, 5 test files rewritten | 90 |
| Sprint 6 | Polish + Phase E complete | HTML to static/, resizable panels, cron create, session JSON export, Escape from editor | 106 |
| Sprint 7 | Wave 2 Core: CRUD + Search | Cron edit/delete, skill create/edit/delete, memory write, session content search, health improvements, git init | 125 |
| Sprint 8 | Daily Driver Finish Line | Edit+regenerate user messages, regenerate last response, clear conversation, Prism.js syntax highlighting, reconnect banner fix, session list scroll fix | 139 |
| Sprint 8 hotfix | Message queue + INFLIGHT fix | Queue messages while busy (toast + badge + auto-drain), INFLIGHT-first loadSession (message stays on switch-away/back) | 139 |
| Sprint 9 | Codebase health + daily driver gaps | app.js deleted and replaced by 6 modules, tool call cards inline, attachment persistence on reload, todo list panel | 149 |
| Sprint 10 | Server health + operational polish | server.py split into api/ modules, background task cancel, cron run history viewer, tool card UX polish | 167 |
| Sprint 10 fixes | Import regressions + regression tests | uuid, AIAgent, has_pending, SSE cancel loop, Session.__init__ tool_calls; test_regressions.py | 177 |
| Concurrency sweeps | Multi-session correctness | Approval cross-session (R10), activity bar per-session (R11), live cards on switch-back (R12), tool cards after done (R13), session model authoritative (R14), newSession cards (R15) | 190 |
| Sprint 11 | Multi-provider models + streaming | Dynamic model dropdown (any Hermes provider), smooth scroll pinning, routes extracted to api/routes.py (server.py 704→76 lines) | 201 |
| Sprint 12 | Settings + reliability + session QoL | Settings panel (gear icon, settings.json), SSE auto-reconnect, pin sessions, import session from JSON | 211 |
| Sprint 13 | Alerts + polish | Cron completion alerts (polling + badge), background error banner, session duplicate, browser tab title | 221 |
| Sprint 14 | Visual polish + workspace ops | Mermaid diagrams, message timestamps, file rename, folder create, session tags, session archive | 233 |
| Sprint 15 | Session projects + code copy | Session projects/folders, code block copy button, tool card expand/collapse toggle | 237 |
| Sprint 16 | Session sidebar visual polish | SVG action icons, session action dropdown, pin indicator, project border, safe HTML rendering | 289 |
| Sprint 17 | Workspace polish + slash commands + settings | Breadcrumb navigation, slash command autocomplete, send key setting (#26) | 318 |
| Sprint 18 | Thinking display + workspace tree | File preview auto-close, thinking/reasoning cards, expandable directory tree (#22) | 318 |
| Sprint 19 | Auth + security hardening | Password auth (off by default), login page, security headers, 20MB body limit (#23) | 328 |
| Sprint 20 | Voice input + send button | Voice input (Web Speech API), send button icon-circle with pop-in animation | 415 |
| Sprint 21 | Mobile responsive + Docker | Hamburger sidebar, bottom nav, files slide-over, Docker support (#21, #7) | 415 |
| Sprint 22 | Multi-profile support | Profile picker, management panel, seamless switching, per-session tracking (#28) | 415 |
| Sprint 23 | Agentic transparency | Token/cost display, subagent cards, skill picker in cron, skill linked files, workspace tree persistence, timestamp fixes | 424 |
| v0.44.0 patch | Fix batch: approval card, login CSP, update diagnostics, Lucide icons | PRs #221 #225 #226 #227 #228 | 579 |
| v0.45.0 | Custom endpoint in new profile form | Base URL + API key fields; server-side URL validation; config.yaml merge; 9 new tests (PR #233, fixes #170) | 604 |
| v0.46.0 | Security, Docker UID/GID, model discovery, i18n, cancel fix | Credential redaction in API responses (PR #243); Docker UID/GID matching (PR #237); custom model API key discovery (PR #238); HTML entity decode + zh/zh-Hant i18n (PR #239); cancel interrupts agent (PR #244); +20 tests | 624 |
| v0.47.0 | Dialogs, session menu, skills command, mobile fixes, mobile QA | Shared app dialogs (#251); session ⋯ menu (#252); mobile QA suite (#254); custom provider slash routing fix (#255); Android Chrome mobile fixes (#256); /skills command (#257); +21 tests | 645 |
| v0.47.1 | Spanish locale | Full Spanish (es) locale, 175 keys, key-parity tests (#275 @gabogabucho); +3 tests | 648 |
| v0.48.0 | Gateway session sync | Real-time Telegram/Discord/Slack sessions in sidebar via SSE + DB polling (#274 @bergeouss); +10 tests | 658 |
| v0.48.1 | Table inline formatting | `inlineMd()` in table cells — **bold**, *italic*, `code`, links render correctly (PR #278); 0 new tests | 658 |
| v0.48.2 | Provider mismatch warning | Toast warning + auth_mismatch error type for provider/model mismatches (#283, fixes #266); +21 tests | 679 |
| v0.49.1 | Docker docs + mobile Profiles button | Two-container Docker compose (#291/#288); Profiles button in mobile bottom nav with mobileSwitchPanel, data-panel, correct SVG size and position (#297/#265 @gabogabucho); +3 tests | 700 |
| v0.49.0 | First-run onboarding wizard + self-update hardening | One-shot bootstrap + guided setup wizard; provider config persisted to config.yaml + .env; OpenRouter/Anthropic/OpenAI/Custom; wizard hidden after completion (#285); self-update stderr/split-ref/conflict fixes (#287); skip flaky redaction test (#289); +18 tests | 697 |
| v0.32 | Auto-compaction handling | Compression detection, /compact command, real context window indicator | 424 |
| v0.33 | /insights sync | Opt-in state.db sync so `hermes /insights` includes WebUI sessions | 424 |
| v0.34 | Sprint 26 — Pluggable themes | Dark, Light, Slate, Solarized, Monokai, Nord; settings unsaved-changes guard; /theme command | 433 |
| v0.34.1 | Theme variable polish | 30+ hardcoded dark-navy colors replaced with theme-aware CSS variables | 433 |
| v0.34.2 | Theme text colors | 5 new per-theme typography variables (--strong, --em, --code-text, --code-inline-bg, --pre-text) | 433 |
| v0.34.3 | Light theme final polish | 46 light-scoped selector overrides for sidebar, roles, chips, interactive elements | 433 |
| v0.35 | Security hardening | Env race fix, random signing key, upload path traversal, PBKDF2 password hash | 433 |
| v0.36–v0.37 | Model routing, personality config, tool card reload, duplicate model fixes | Model routing by provider prefix, personality via config.yaml, tool cards reload on page refresh | 466 |
| v0.38.0–v0.38.6 | Model selector, custom endpoints, OLED theme, reasoning display, insights sync | Custom endpoint URL fix, OLED theme, top-level reasoning field fix, message_count sync to state.db | 466 |
| v0.39.0 | Security hardening (Sprint 29) | CSRF, PBKDF2, rate limiting, session ID validation, SSRF, ENV_LOCK, XSS, HMAC, skills traversal, secure cookie, error sanitization, startup warning | 499 |
| v0.40–v0.44.2 | Approval card + Lucide icons + sprint auth | Approval prompt surfaced in UI, emoji icons → Lucide SVG, login CSP inline fix, update diagnostics | 579 |
| v0.45–v0.46 | Custom endpoints + security + i18n + cancel | Custom endpoint Base URL + API key on profile create, credential redaction (PR #243), Docker UID/GID (PR #237), HTML entity decode + zh/zh-Hant i18n, cancel interrupts agent | 624 |
| v0.47–v0.47.1 | Dialogs + session menu + skills + mobile QA + Spanish | Shared app dialogs, session ⋯ menu, /skills command, mobile QA suite, Android Chrome fixes, Spanish locale (@gabogabucho) | 648 |
| v0.48–v0.48.2 | Gateway session sync + table formatting + provider warnings | Real-time Telegram/Discord/Slack sessions in sidebar (@bergeouss), inlineMd() in table cells, provider/model mismatch toast | 679 |
| v0.49–v0.49.1 | Onboarding wizard + Docker two-container | One-shot bootstrap + guided setup wizard, OpenRouter/Anthropic/OpenAI/Custom provider config, two-container Docker compose, mobile Profiles button | 700 |
| v0.50.0 | v0.50.0 UI overhaul (Sprint 34) | Composer-centric controls, Hermes Control Center modal, workspace panel state machine, collapsible date groups, rAF streaming throttle, context ring indicator (@aronprins) | 742 |
| v0.50.5–v0.50.10 | Think-tag edge cases + onboarding hardening + mobile fixes | MiniMax M2.5 leading-whitespace think-tag fix, skip-onboarding env var, OAuth provider path, Docker bridge networks fix, model dropdown dedup, title auto-generation fix, mobile close button | 802 |
| v0.50.11–v0.50.12 | Chat table styles + URL autolink + profile env isolation | .msg-body table borders, plain URL auto-linking, profile .env secret isolation on switch (prevents API key leakage across profiles, @Hinotoi-agent) | 815 |
| v0.50.13–v0.50.15 | session_search + security sweep + KaTeX math | SessionDB injection for session_search in WebUI (@DelightRun), bandit B310/B324/B110 + QuietHTTPServer (@lawrencel1ng), KaTeX math rendering with fence-before-math fix | 871 |
| v0.50.16–v0.50.17 | CSRF reverse proxy + Docker uv pre-install | Scheme-aware CSRF port normalization for non-standard ports (@lx3133584), Docker uv pre-installed at build time as root (fixes air-gapped startup, @mmartial-pattern) | 900 |
| v0.50.18–v0.50.19 | Workspace fallback + Unicode filenames | Cascading workspace path recovery (@Jordan-SkyLF), Unicode Content-Disposition headers with RFC 5987 filename* (@shaoxianbilly), silent auth error surfacing, stale model cleanup | 924 |
| v0.50.20–v0.50.21 | Silent errors + live model fetching + durable streaming recovery | apperror on empty agent response, /api/models/live endpoint with SSRF guard, live reasoning cards, tool_complete SSE events, SESSION_QUEUES, localStorage reload recovery (@Jordan-SkyLF) | 961 |

---

## Current Architecture Status

| Layer | Location | Status |
|-------|----------|--------|
| Python server | <repo>/server.py (~165 lines) + api/ modules (~5000 lines) | Thin shell + QuietHTTPServer + auth middleware + business logic in api/ |
| HTML template | <repo>/static/index.html (~600 lines) | Served from disk |
| CSS | <repo>/static/style.css (~1050 lines) | Served from disk, incl. mobile responsive, KaTeX, table styles |
| JavaScript | <repo>/static/{ui,workspace,sessions,messages,panels,boot,commands,icons,i18n,login}.js | 10 modules, ~7100 lines total |
| Docker | Dockerfile, docker-compose.yml, .dockerignore | python:3.12-slim, multi-arch (amd64+arm64) |
| CI/CD | .github/workflows/release.yml | Auto-release + GHCR publish on tag push |
| Runtime state | ~/.hermes/webui-mvp/sessions/ | Session JSON files |
| Test server | Port 8788 (conftest.py), port 8789 (browser sanity) | Isolated, wiped per run |
| Production server | Port 8787 | SSH tunnel from Mac |

---

## Feature Parity Checklist

### Chat and Agent
- [x] Send messages, get SSE-streaming responses
- [x] Switch models per session (10 models, grouped by provider)
- [x] Composer-scoped model picker in footer (moved from sidebar to align with per-conversation model selection)
- [x] Multi-provider API support: use any Hermes agent API provider (OpenAI, Anthropic, Google, etc.) directly, not just OpenRouter (Sprint 11)
- [x] Custom endpoint model discovery: auto-detect models from Ollama, LM Studio, and other local LLM servers via base_url (PR #18)
- [x] Upload files to workspace (drag-drop, click, clipboard paste)
- [x] File tray with remove button
- [x] Tool progress shown inline in the conversation via live tool cards
- [x] Approval card for dangerous commands (Allow once/session/always, Deny)
- [x] Approval polling + SSE-pushed approval events
- [x] INFLIGHT guard: switch sessions mid-request without losing response
- [x] Session restores from localStorage on page load
- [x] Reconnect banner if page reloaded mid-stream
- [x] Copy message to clipboard (hover icon on each bubble)
- [x] Edit last user message and regenerate
- [ ] Branch/fork conversation (Wave 3)
- [x] Token/cost estimate per message (Sprint 23)

### Tool Visibility
- [x] Tool progress in live tool cards (kept out of the composer/footer chrome)
- [x] Approval card with all 4 choices
- [x] Tool call cards inline (collapsed, show name/args/result)

### Workspace / Files
- [x] Workspace panel defaults closed and opens only for active browsing or preview
- [x] Browse workspace directory tree with type icons
- [x] Preview text/code files (read-only)
- [x] Preview markdown files (rendered, tables supported)
- [x] Preview image files (PNG, JPG, GIF, SVG, WEBP inline)
- [x] Edit files inline (Edit button, Enter to save, Escape to cancel)
- [x] Create new file (+ button in panel header)
- [x] Delete file (hover trash, confirmation modal)
- [x] File name truncation with tooltip for long names
- [x] Right panel resizable (drag inner edge)
- [x] Syntax highlighted code preview (Prism.js)
- [x] Rename file (Sprint 14)
- [x] Create folder (Sprint 14)
- [x] Shared app modal for confirm/input flows (Sprint 33)

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
- [x] Session tags / labels (Sprint 14)
- [x] Archive sessions (Sprint 14)
- [x] Clear conversation (wipe messages, keep session) (Wave 3)
- [x] Import session from JSON (Sprint 12)
- [x] Pin/star sessions to top of list (Sprint 12)
- [x] Duplicate session (Sprint 13)
- [x] Session projects / folders (Sprint 15)

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
- [x] View full cron run history (expandable per job)
- [x] Skill picker in cron create form (Sprint 23)

### Skills
- [x] List all skills grouped by category (Skills sidebar tab)
- [x] Search/filter skills by name, description, category
- [x] View full SKILL.md content in right preview panel
- [x] Create skill
- [x] Edit skill
- [x] Delete skill
- [x] View skill linked files (Sprint 23)

### Memory
- [x] View personal notes (MEMORY.md) rendered as markdown (Memory tab)
- [x] View user profile (USER.md) rendered as markdown (Memory tab)
- [x] Last-modified timestamp on each section
- [x] Add/edit memory entry inline

### Configuration
- [x] Settings panel (default model, default workspace) (Sprint 12)
- [x] Send key preference (Enter or Ctrl+Enter) (Sprint 17)
- [x] Password authentication (Sprint 19)
- [ ] Enable/disable toolsets per session (deferred)

### Notifications
- [x] Cron job completion alerts (Sprint 13)
- [x] Background agent error alerts (Sprint 13)

### Workspace
- [x] Breadcrumb navigation in subdirectories (Sprint 17)
- [x] Workspace tree view with expand/collapse (Sprint 18, Issue #22)
- [x] File preview auto-close on directory navigation (Sprint 18)

### Slash Commands
- [x] Command registry + autocomplete dropdown (Sprint 17)
- [x] Built-in: /help, /clear, /model, /workspace, /new (Sprint 17)

### Security
- [x] Password auth with signed cookies (Sprint 19, Issue #23)
- [x] Security headers (X-Content-Type-Options, X-Frame-Options) (Sprint 19)
- [x] POST body size limit (20MB) (Sprint 19)

### Thinking / Reasoning
- [x] Collapsible thinking cards for extended-thinking models (Sprint 18)

### Voice
- [x] Voice input via Web Speech API (Sprint 20)

### Mobile
- [x] Mobile responsive layout — hamburger sidebar, bottom nav, files slide-over (Sprint 21)

### Profiles
- [x] Multi-profile support — create, switch, delete profiles (Sprint 22, Issue #28)

### Advanced / Future
- [ ] Subagent session tree -- show subagent hierarchy in sidebar with expand/collapse (PR #75)
- [ ] Specialized tool card renderers -- diff viewer, terminal output, todo checklist views (PR #75)
- [x] Streaming performance -- rAF-throttled token rendering (Sprint 24, PR #81)
- [x] Workspace git detection -- branch name and dirty status badge (Sprint 24, PR #82)
- [x] Collapsible date groups -- click group headers to collapse (Sprint 24, PR #80)
- [x] Context usage indicator -- compact circular badge in composer footer (Sprint 24, PR #83; refreshed April 10, 2026)
- [ ] LLM-generated session titles -- auto-title via small model instead of first-message substring (PR #75)
- [ ] Workspace git detection -- show branch name, dirty status in workspace header (PR #75)
- [ ] Clarify dialog -- agent can ask clarifying questions that block until user responds (PR #75)
- [ ] Gateway approval polling -- support blocking approvals from messaging gateway (PR #75)
- [ ] Unified session storage -- SessionDB shared between webui and CLI (PR #75)
- [ ] TTS playback of responses (deferred)
- [x] Background task cancel (composer footer stop button)
- [ ] Code execution cell (deferred)
- [ ] Desktop application (Sprint 25, PLANNED)
- [x] Pluggable UI themes -- Dark, Light, Slate, Solarized, Monokai, Nord (Sprint 26, v0.34)
- [ ] Extended slash command / skill integration (deferred)
- [ ] Virtual scroll for large lists (deferred)

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
| Git init | git init <repo>, first commit, push to private GitHub repo |

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
- [x] Full cron run history

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
- [x] View current todo list (sidebar Todo panel, parsed from session history)

### Sprint 2.5: Session Content Search (Sprint 7)
- [x] Session title search (Sprint 4)
- [x] Message content search across sessions

### Sprint 2.6: Session Rename (COMPLETE Sprint 4)
Double-click any session title in the left sidebar to edit inline.
Enter saves, Escape cancels. Topbar updates immediately.

---

## Completed Waves (Summary)

| Wave | Theme | Key Deliverables |
|------|-------|-----------------|
| Wave 2 | Full CRUD + Interaction | Cron/skill/memory CRUD, session search, workspace management, session rename |
| Wave 3 | Power Features | Tool call cards, multi-model dropdown, resizable panels, file actions, conversation controls |
| Wave 4 | Settings + Notifications | Settings panel, cron alerts, background error banner |
| Wave 5 | Session Continuity | Session tags, archive, projects/folders |
| Wave 6 | Agentic Features | Background task cancel, voice input (Web Speech API) |
| Wave 7 | Production Hardening | Password auth, security headers, mobile responsive, Docker + GHCR CI |

---

## User Requested Features

Community-requested enhancements tracked from GitHub issues. All shipped.

| Feature | Issue | Shipped | Sprint |
|---------|-------|---------|--------|
| Workspace tree view | #22 | Done | Sprint 18 |
| Docker container + GHCR images | #7 | Done | Sprint 21 + v0.28.1 CI |
| Authentication | #23 | Done | Sprint 19 |
| Send key / personalization | #26 | Done | Sprint 17 |
| Multi-profile support | #28 | Done | Sprint 22 |
| Mobile responsive UI | #21 | Done | Sprint 21 |
| Profile creation in Docker | #44 | Done | v0.27 |
