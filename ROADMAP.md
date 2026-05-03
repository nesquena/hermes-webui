# Hermes Web UI — Roadmap

> Web companion to the Hermes Agent CLI. Same workflows, browser-native.
>
> Last updated: v0.50.283 (May 03, 2026) — 4019 tests collected
> Test source: `pytest tests/ --collect-only -q`
> Per-version detail: see [CHANGELOG.md](./CHANGELOG.md)

---

## Status snapshot

| Surface | Status |
|---|---|
| **Hermes CLI parity** | ✅ Complete — every CLI workflow has a web equivalent |
| **Streaming + tool transparency** | ✅ Live tool cards, reasoning cards, approval prompts, cancel |
| **Multi-provider model support** | ✅ Any provider configured in `config.yaml` shows in the picker |
| **Sessions + projects + search** | ✅ CRUD, content search, projects, tags, archive, fork, import |
| **Mobile + Docker + auth** | ✅ Hamburger nav, slide-overs, password auth, GHCR images |
| **Auxiliary surfaces** | ✅ Workspace tree + edit, cron CRUD, skills CRUD, memory write, MCP server UI |
| **Visual polish** | ✅ 8 themes (incl. light/system/OLED/Sienna), Mermaid, KaTeX, syntax highlighting |
| **Native distribution** | ✅ macOS desktop app (universal arm64+x86_64 DMG, signed) — separate repo |

Remaining gaps and forward work live in [Forward Work](#forward-work) below.

---

## Architecture

| Layer | Files | Status |
|---|---|---|
| Python server | `server.py` (~165 lines) + `api/` modules (~20k lines) | Thin shell + auth middleware + business logic |
| HTML template | `static/index.html` (~600 lines) | Served from disk |
| CSS | `static/style.css` (~3k lines) | Themes, mobile responsive, KaTeX, table styles |
| JavaScript | `static/{ui,sessions,messages,workspace,panels,boot,commands,icons,i18n,login,onboarding}.js` (~26k lines) | 11 modules served as static files |
| Service worker | `static/sw.js` | Offline shell cache, version-pinned assets |
| Docker | `Dockerfile`, `docker-compose.yml` | `python:3.12-slim`, multi-arch (amd64+arm64), HEALTHCHECK |
| CI/CD | `.github/workflows/release.yml` | Auto-release + GHCR publish on tag push |
| Test isolation | `tests/_pytest_port.py` | Per-worktree port + state-dir derivation, no collisions |

---

## Feature parity checklist

### Chat and streaming
- [x] Send messages, get SSE-streaming responses
- [x] Composer-scoped model picker (per-conversation model selection)
- [x] Multi-provider API support — OpenAI, Anthropic, Google, OpenRouter, xAI, GLM, DeepSeek, Mistral, MiniMax, Kimi, OpenCode, Nous Portal, custom OpenAI-compatible endpoints
- [x] Live custom-endpoint model discovery (Ollama, LM Studio, vLLM via `/v1/models`)
- [x] Free-form OpenRouter model name (autocomplete + custom input)
- [x] Tool progress shown inline via live tool cards
- [x] Approval card for dangerous commands (Allow once / session / always, Deny)
- [x] Approval polling + SSE-pushed approval events
- [x] Clarify dialog — agent can ask blocking clarifying questions
- [x] Subagent delegation cards in tool view
- [x] INFLIGHT guard: switch sessions mid-request without losing response
- [x] Session restores from localStorage on page load
- [x] Reconnect banner if page reloaded mid-stream
- [x] SSE auto-reconnect with stream replay
- [x] Token / cost estimate per message and per session
- [x] Context usage indicator (compact ring badge in composer footer)
- [x] Auto-compaction handling + `/compact` command
- [x] rAF-throttled token rendering (smooth, no DOM thrash)
- [x] Cancel / stop button in composer footer
- [x] Reasoning effort selector (low / medium / high / xhigh) + `/reasoning`
- [x] Pure-text streaming with crash-recovery — partial messages restored from localStorage on reload

### Conversation controls
- [x] Copy message to clipboard (hover icon on each bubble)
- [x] Edit last user message and regenerate
- [x] Regenerate last response
- [x] Clear conversation (wipe messages, keep session)
- [x] Branch / fork conversation from any message point (#465)
- [x] Pure-text + tool-call streams both recover

### Sessions
- [x] Create session (+ button or Cmd/Ctrl+K)
- [x] Load session (click in sidebar)
- [x] Delete session (hover trash, toast undo, fallback)
- [x] Auto-title from first user message + adaptive title refresh (configurable cadence)
- [x] LLM-generated titles via auxiliary route (configurable model)
- [x] Rename session inline (double-click, Enter saves, Escape cancels)
- [x] Title search (live filter)
- [x] Content search (full-text across all sessions)
- [x] Date group headers (Today / Yesterday / Earlier) with collapsible groups
- [x] Pin / star sessions to top
- [x] Duplicate session
- [x] Import / Export session as JSON (full messages + metadata)
- [x] Download as Markdown transcript
- [x] Tags (`#tag` extraction + filter chips)
- [x] Archive sessions (hidden by default, "Show N archived" toggle)
- [x] Projects / folders (chip filter bar, "Unassigned" filter)
- [x] Per-session profile tracking
- [x] Per-session toolset override (`/toolsets`)
- [x] Batch select mode (multi-select, bulk delete / move / archive)
- [x] CLI session bridge — read CLI sessions from state.db, import as WebUI sessions

### Workspace and files
- [x] Add workspace with path validation (existing directory, follows symlinks)
- [x] Remove / rename workspace
- [x] Quick-switch from topbar dropdown
- [x] Sidebar live workspace display (name + path)
- [x] New sessions inherit last-used workspace
- [x] Browse workspace directory tree with type icons
- [x] Tree view with expand / collapse + lazy load (#22)
- [x] Breadcrumb navigation in subdirectories
- [x] Preview text / code (read-only)
- [x] Preview markdown (rendered + tables + Mermaid + KaTeX)
- [x] Preview images (PNG, JPG, GIF, SVG, WEBP, AVIF inline)
- [x] Preview PDF / SVG / audio / video / Excalidraw / CSV / JSON / YAML
- [x] Edit files inline (Edit button, Enter saves, Escape cancels)
- [x] Create / rename / delete files and folders (in current directory)
- [x] Drag-drop / click / clipboard paste upload
- [x] Archive upload (zip / tar) with extraction
- [x] Syntax highlighted code preview (Prism.js, language-aware)
- [x] File preview auto-close on directory navigation
- [x] Right panel resizable (drag inner edge)
- [x] Embedded workspace terminal (`/api/terminal/{start,input,output}`)
- [x] Git branch + dirty status badge in workspace header

### Cron jobs
- [x] List all cron jobs (Tasks sidebar tab)
- [x] View job details (prompt, schedule, last run, output)
- [x] Run / pause / resume / delete
- [x] Create job from UI (name, schedule, prompt, delivery target)
- [x] Edit job inline (full create-form parity, including skills)
- [x] Skill picker in create + edit forms
- [x] Cron run history viewer (expandable per job)
- [x] Cron completion alerts (toast + badge)
- [x] Run-status tracking with live watch mode

### Skills
- [x] List all skills grouped by category
- [x] Search / filter by name, description, category
- [x] View full SKILL.md content
- [x] View skill linked files
- [x] Create / edit / delete skill
- [x] `/skills` slash command

### Memory
- [x] View personal notes (MEMORY.md) rendered as markdown
- [x] View user profile (USER.md) rendered as markdown
- [x] Last-modified timestamp per section
- [x] Add / edit memory entries inline

### Profiles
- [x] Multi-profile support — create, switch, delete (#28)
- [x] Topbar profile picker with gateway-status dots
- [x] Profile management panel (full CRUD)
- [x] Seamless switching (no server restart, refreshes models / skills / memory / cron / workspace)
- [x] Profile-local workspace storage
- [x] First-run onboarding wizard with provider config (OpenRouter / Anthropic / OpenAI / Custom)
- [x] In-app OAuth for Codex and Claude

### Configuration
- [x] Settings panel (default model, default workspace, send key, theme, voice, font size)
- [x] Send key preference (Enter or Ctrl+Enter)
- [x] Password authentication (off by default)
- [x] Per-session toolset override
- [x] Personality config via `config.yaml`
- [x] Reasoning effort persistence

### Notifications
- [x] Cron job completion alerts
- [x] Background agent error banner
- [x] Approval pending badge
- [x] Provider / model mismatch toast warning

### Slash commands
- [x] Command registry + autocomplete dropdown
- [x] Built-ins: `/help`, `/clear`, `/model`, `/workspace`, `/new`, `/usage`, `/theme`, `/compact`, `/queue`, `/interrupt`, `/steer`, `/btw`, `/reasoning`, `/skills`, `/toolsets`
- [x] Transparent pass-through for unrecognized commands

### Security
- [x] Password auth with signed HMAC HTTP-only cookies (24h TTL)
- [x] Security headers (X-Content-Type-Options, X-Frame-Options, Referrer-Policy)
- [x] CSRF protection (scheme-aware, port-normalized for reverse proxies)
- [x] PBKDF2 password hashing
- [x] Rate limiting on auth endpoints
- [x] Session ID validation
- [x] SSRF guard on `/api/models/live`, `cfg_base_url`, `custom_providers[]`
- [x] ENV_LOCK around env mutations
- [x] XSS sanitization on all rendered HTML
- [x] HMAC-signed signing keys (random per install)
- [x] Skills path-traversal guard
- [x] Secure cookie flags (HttpOnly, SameSite, Secure when HTTPS)
- [x] Error message sanitization (no stack traces in responses)
- [x] POST body size limit (20MB)
- [x] Upload path-traversal guard
- [x] Credential redaction in API responses
- [x] Profile `.env` secret isolation on switch
- [x] Auto-install gate (opt-in via `HERMES_WEBUI_AUTO_INSTALL=1`)

### Visual / UX
- [x] 8 themes — Dark, Light, System (auto-sync), Slate, Solarized, Monokai, Nord, OLED, Sienna
- [x] 2-axis appearance model (theme + skin) for community theme contributions
- [x] Mermaid diagram rendering
- [x] KaTeX math rendering with fence-before-math fix
- [x] Syntax highlighting (Prism.js, language-aware, YAML newline preservation)
- [x] Markdown image syntax `![alt](url)` and inline MEDIA: tokens render as `<img>`
- [x] Plain URL auto-linking
- [x] Inline markdown in table cells (bold, italic, code, links)
- [x] Code block copy button
- [x] Tool card expand / collapse toggle
- [x] Collapsible thinking / reasoning cards (Claude extended thinking, o3 reasoning tokens)
- [x] Message timestamps (subtle, full date on hover)
- [x] Empty composer hides send button (icon-circle with pop-in animation)
- [x] Pluggable Lucide SVG icons (no emoji rendering inconsistencies)
- [x] Composer-centric controls (v0.50.0 UI overhaul)
- [x] Hermes Control Center modal (centralized actions)
- [x] Workspace panel state machine (defaults closed, opens for browsing / preview)
- [x] PWA manifest + service worker (offline shell)
- [x] Favicon (SVG + PNG + ICO)
- [x] Branded onboarding wizard

### Voice
- [x] Voice input via Web Speech API (push-to-talk dictation)
- [x] Hands-free voice mode (turn-based conversation, opt-in via Settings → Preferences)
- [x] TTS playback of responses (configurable voice, rate, pitch)

### Mobile
- [x] Hamburger sidebar (slide-in overlay)
- [x] Bottom navigation bar (5-tab iOS-style)
- [x] Files slide-over (right panel as slide-over)
- [x] 44px minimum touch targets
- [x] Container queries on composer
- [x] Android Chrome compatibility fixes
- [x] PWA installation (manifest + icons + Android support)

### Internationalization
- [x] 9 locales — English, Japanese, Russian, Spanish, German, Chinese (zh + zh-Hant), Portuguese, Korean, French
- [x] Key-parity test ensures every locale has every key
- [x] Right-to-left and CJK input (IME composition fixes)

### Gateway integration
- [x] Real-time gateway sessions in sidebar (Telegram, Discord, Slack, Weixin) via SSE + DB polling
- [x] Cross-channel handoff dock — composer-docked flyout summarizing the live external session
- [x] Transcript-summary card at 10+ rounds
- [x] Sidebar dedup keying on per-conversation identity (distinct chats from same platform stay separate)
- [x] Gateway session sync skips dup / delete options for external sessions

### MCP integration
- [x] MCP server management UI (System Settings → MCP Servers)
- [x] Add / edit / delete MCP server entries

### Distribution
- [x] Docker support (multi-arch amd64 + arm64, HEALTHCHECK, UID/GID auto-detect)
- [x] Two-container Docker compose (webui + agent)
- [x] GHCR auto-publish on tag push
- [x] Subpath mount support (reverse proxy at `/hermes/`)
- [x] PWA installable from any browser
- [x] Native macOS app — universal Intel + Apple Silicon, signed + notarized DMG, Sparkle 2 auto-update — see `hermes-webui/hermes-swift-mac` repo

---

## Forward work

### Confirmed candidates (open feature requests with sprint-candidate or active interest)

| Theme | Tracking | Why |
|---|---|---|
| Persistent-host stability | #1458 | Bootstrap fork pattern crashes under launchd / systemd — partial fix shipped (foreground mode); state.db FD leak and HTTP-unhealthy wedge remain |
| Free-tier OpenRouter variants visible | #1426 | `:free` tool-support filter currently hides them from the picker |
| macOS scroll override regression | #1360 | Auto-scroll sometimes overrides user scroll on the desktop app |
| GLM dual-use (main + auxiliary) | #1291 | Currently mutually exclusive; same provider can't serve both surfaces |
| Auto-assign session to filtered project | #1468 | When user is filtering by project X, new session should default to project X |
| Update banner "What's new?" link | #1512 | Surface release highlights from the update banner |
| Sunset legacy `LMSTUDIO_API_KEY` env var | #1502 | Tracking issue — alias stays for one minor cycle, then removed |
| Hermes Agent dashboard cross-link | #1459 | Detect a running Hermes Agent and surface link in nav |
| Gateway status card in Settings | #1457 | Current gateway-status dots only on profile picker |
| Insights — daily token chart + per-model breakdown | #1456 | Existing usage badge is per-message; need rollup view |
| Logs tab — view agent / errors / gateway logs | #1455 | Currently requires terminal access to log files |
| Model picker collision handling | #1425 | Same-name models from different providers aren't disambiguated in dropdown |
| "Reveal in Finder" right-click on workspace | #1424 | macOS desktop app convenience |
| Configurable session persistence timing | #1406 | Currently every checkpoint, want operator control |
| Silent credential self-heal on 401 | #1401 | Gateway auth.json drift should resolve without user re-auth |
| LLM Wiki status panel | #1257 | On / off toggle for Wiki integration |
| Lightweight in-app Canvas editing | #1255 | Text canvas for prompt drafting / shared notes |
| Provider / Model source-of-truth alignment | #1240 | Reconcile WebUI vs CLI vs Gateway provider resolution |
| Built-in SearXNG web search | #1037 | Lightweight search tool with on / off toggle |
| Subagent session relationship view | #1004 | Show subagent hierarchy in sidebar with expand / collapse |

### Backlog (deferred, listed for visibility)

- **Insights / monitoring suite** — agent heartbeat + alerts (#716), quota / rate-limit display (#706), data tabs (#722), monitor dashboard concepts (#766, #721)
- **Native MCP server expose** — Hermes WebUI as an MCP server for direct agent integration (#733)
- **Provider failover status** — show active provider per model + LLM Gateway failover state (#732)
- **Teams / agents management panel** — editable names, roles, assignments (#719)
- **Web UI profile model alignment with Hermes runtime** — design parity (#749)
- **DOM windowing / message virtualization** — for sessions with hundreds of messages (#734)
- **Searchable global tool list** (#697)
- **Add agent / replace model modals** (#698)
- **Code execution inline cells** — Jupyter-style cell rendering inside chat
- **Sharing / public conversation URLs** — requires hosted backend with access control (out of scope for self-host)

### Intentionally not planned
- Full SwiftUI rewrite of the frontend — the WKWebView shell already gets 95% of native benefit
- App Store distribution — sandboxing breaks the local server model
- Real-time multi-user collaboration — single-user assumption throughout
- Plugin marketplace — Hermes skills cover this surface
- Anthropic / Claude proprietary features — Projects AI memory, Claude artifacts sync (not reproducible)

---

## Sprint history

Per-version detail lives in [CHANGELOG.md](./CHANGELOG.md). The table below is a high-level chronology of major sprint themes; individual PR / fix detail moved to CHANGELOG to keep this file readable.

| Range | Theme | Highlights |
|---|---|---|
| Sprints 1–6 | Foundations + workspace | server / static split, JS module split, workspace CRUD, file editor, message queue + INFLIGHT, isolated test environment |
| Sprint 7 | Wave 2 core | Cron / skill / memory CRUD, session content search, health endpoint, git init |
| Sprint 8 | Daily-driver finish line | Edit + regenerate, regenerate last response, clear conversation, Prism.js, queue + INFLIGHT polish |
| Sprints 9–10 | Codebase health + operational polish | `app.js` → 6 modules, server.py → `api/` modules, tool card UX, background task cancel, regression tests |
| Sprint 11 | Multi-provider models + streaming | Dynamic model dropdown, smooth scroll pinning, routes extracted to `api/routes.py` |
| Sprint 12 | Settings + reliability + session QoL | Settings panel, SSE auto-reconnect, pin sessions, JSON import |
| Sprint 13 | Alerts + polish | Cron alerts, background error banner, session duplicate, browser tab title |
| Sprint 14 | Visual polish + workspace ops | Mermaid, message timestamps, file rename, folder create, session tags, archive |
| Sprint 15 | Session projects + code copy | Projects / folders, code copy button, tool card expand / collapse |
| Sprint 16 | Sidebar visual polish | SVG icons, action dropdown, pin indicator, project border, safe HTML rendering |
| Sprint 17 | Workspace polish + slash commands | Breadcrumb nav, slash command autocomplete, send key setting (#26) |
| Sprint 18 | Thinking display + workspace tree | File preview auto-close, thinking / reasoning cards, expandable directory tree (#22) |
| Sprint 19 | Auth + security hardening | Password auth, login page, security headers, body limit (#23) |
| Sprint 20 | Voice input + send button | Web Speech API voice, send button polish |
| Sprint 21 | Mobile responsive + Docker | Hamburger sidebar, mobile nav, slide-over files, Docker support (#21, #7) |
| Sprint 22 | Multi-profile support | Profile picker, management panel, seamless switching, per-session tracking (#28) |
| Sprint 23 | Agentic transparency | Token / cost display, subagent cards, skill picker in cron, profile-local storage |
| Sprint 24 | Web polish | rAF streaming, git detection, collapsible date groups, context ring (#80, #81, #82, #83) |
| Sprint 25 | macOS desktop application | Native Swift + WKWebView shell, universal DMG, Sparkle 2 auto-update — separate repo |
| Sprint 26 | Pluggable themes | Light / Slate / Solarized / Monokai / Nord, settings unsaved-changes guard, `/theme` |
| Sprint 27 | Theme polish | 30+ hardcoded colors → CSS variables, light theme final polish |
| Sprint 28 | Security hardening | Env race fix, random signing key, upload traversal, PBKDF2 |
| Sprints 29–32 | Model routing + custom endpoints + reasoning | Model routing by provider prefix, custom endpoint URL fix, OLED theme, top-level reasoning, message_count sync |
| Sprint 33 | Approval card + Lucide icons | Approval prompt surfaced, emoji → SVG, login CSP fix, update diagnostics |
| Sprint 34 | v0.50.0 UI overhaul | Composer-centric controls, Control Center modal, workspace state machine, collapsible date groups, rAF throttle, context ring |
| Sprints 35–37 | Onboarding + i18n + Spanish | First-run wizard, OpenRouter / Anthropic / OpenAI / Custom config, Spanish locale, Docker two-container, mobile Profiles button |
| Sprints 38–40 | Session + UI polish + Sprint 40 | Five-bug clean-up + sidebar timestamp + test port isolation |
| Sprints 41–42 | Renderer hardening + KaTeX + handoff | Context ring live usage, renderMd link / image / code stash chain, MEDIA: image rendering, gateway handoff foundation |
| Sprints 43+ | Continuous contributor sprints | Custom providers, Russian locale, IME fixes, model-switch toast, approval queue multi-slot, profile polish, font-size CSS, contributor wave |

---

## Versioning conventions

- **Patch** (`v0.50.X`) — small batches, contributor PR releases, hotfixes
- **Minor** (`v0.X.0`) — sprint completion, new feature surface, architecture milestone
- **Major** (`v1.0.0`) — declared when CLI parity + Claude parity reach steady state and the feature surface stabilizes

Per-version detail and contributor attribution live in [CHANGELOG.md](./CHANGELOG.md).
