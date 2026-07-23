# Hermes Web UI — Roadmap

> Web companion to the Hermes Agent CLI. Same workflows, browser-native.
>
> This roadmap tracks the whole ecosystem: the web app, the native clients that
> wrap it (macOS, Windows/Linux, Android, iOS), the extension system, and the
> autonomous maintenance framework that runs the project.
>
> Version, test count, and release history are **derived live**, not stamped here
> (they drift every release). For the current numbers:
> - Version + history: `git tag --sort=-v:refname | head` + [CHANGELOG.md](./CHANGELOG.md)
> - Test count: `pytest tests/ --collect-only -q`

---

## Status snapshot

| Surface | Status |
|---|---|
| **Hermes CLI parity** | ✅ Complete — every CLI workflow has a web equivalent |
| **Streaming + tool transparency** | ✅ Live tool cards, reasoning cards, approval prompts, clarify, cancel, steer |
| **Multi-provider model support** | ✅ Any provider configured in `config.yaml` shows in the picker + live custom-endpoint discovery |
| **Sessions + projects + search** | ✅ CRUD, content search, projects, tags, archive, fork, import, batch ops, CLI bridge |
| **Mobile + Docker + auth** | ✅ Hamburger nav, slide-overs, PWA install, password auth, GHCR images |
| **Auxiliary surfaces** | ✅ Workspace tree + edit + terminal, cron CRUD, skills CRUD, memory write, MCP server UI |
| **Visual polish** | ✅ Light / dark / system × 21 skins, Mermaid, KaTeX, syntax highlighting, Transparent Stream |
| **Internationalization** | ✅ 15 locales with key-parity guards + CJK/RTL/IME handling |
| **Gateway integration** | ✅ Live external sessions (Telegram, Discord, Slack, WeChat/Weixin, Signal, SMS) + cross-channel handoff |
| **Extension system** | ✅ Opt-in loader, capability surface (themes / TTS / nav / sidecars), one-click gallery, vetted library repo |
| **Native clients** | ✅ macOS (Swift), Windows + Linux + macOS (Rust/Tauri), Android, iOS — each in its own repo |
| **Autonomous maintenance** | ✅ Self-running triage / review / release pipeline; generalized publicly as [StewardOS](https://github.com/nesquena/steward-os) |

Remaining gaps and forward work live in [Forward Work](#forward-work) below. The
short version: the original CLI-parity + Claude-parity vision is done, and the
work has shifted from "reach parity" to "harden reliability + widen distribution."

---

## Architecture

| Layer | Files | Status |
|---|---|---|
| Python server | `server.py` + `api/` modules | Thin HTTP shell + auth middleware over the `api/` business logic (config, sessions, streaming, profiles, routes, onboarding, workspace, updates, upload, extensions) |
| HTML template | `static/index.html` | Served from disk |
| CSS | `static/style.css` | Themes + skins, mobile responsive, KaTeX, table styles |
| JavaScript | `static/{ui,sessions,messages,workspace,panels,boot,commands,icons,i18n,login,onboarding,extension_settings}.js` | Vanilla-JS modules served as static files — no bundler |
| Service worker | `static/sw.js` | Offline shell cache, version-pinned assets |
| Docker | `Dockerfile`, `docker-compose.yml` | `python:3.12-slim`, multi-arch (amd64+arm64), HEALTHCHECK |
| CI/CD | `.github/workflows/` | ruff lint + sharded pytest + Playwright browser + Docker smoke on every PR; auto-release + GHCR publish on tag push |
| Test isolation | `tests/_pytest_port.py` | Per-worktree port + state-dir derivation, no collisions |

> Per-file line counts drift every release; see `ARCHITECTURE.md` for the current module map and `git ls-files` for exact sizes.

---

## Feature parity checklist

### Chat and streaming
- [x] Send messages, get SSE-streaming responses
- [x] Composer-scoped model picker (per-conversation model selection)
- [x] Rich provider-grouped, searchable model picker everywhere (composer + Preferences default-model)
- [x] Multi-provider API support — OpenAI, Anthropic, Google, OpenRouter, xAI, GLM, DeepSeek, Mistral, MiniMax, Kimi, OpenCode, Nous Portal, custom OpenAI-compatible endpoints
- [x] Live custom-endpoint model discovery (Ollama, LM Studio, vLLM via `/v1/models`)
- [x] Add a self-hosted provider (Ollama / LM Studio) from Settings — no `config.yaml` edit
- [x] Free-form OpenRouter model name (autocomplete + custom input)
- [x] Tool progress shown inline via live tool cards
- [x] Approval card for dangerous commands (Allow once / session / always, Deny)
- [x] Approval polling + SSE-pushed approval events
- [x] Clarify dialog — agent can ask blocking clarifying questions
- [x] Subagent delegation cards in tool view
- [x] INFLIGHT guard: switch sessions mid-request without losing response
- [x] Session restores from localStorage on page load
- [x] Reconnect banner if page reloaded mid-stream
- [x] SSE auto-reconnect with extended backoff ladder + full-session poll fallback
- [x] Token / cost estimate per message and per session
- [x] Context usage indicator (compact ring badge in composer footer)
- [x] Auto-compaction handling + `/compact` command
- [x] One-click recovery from "context compression exhausted" (focused continuation)
- [x] rAF-throttled token rendering (smooth, no DOM thrash)
- [x] Cancel / stop button in composer footer
- [x] Reasoning effort selector (low / medium / high / xhigh) + `/reasoning`
- [x] Pure-text streaming with crash-recovery — partial messages restored from localStorage on reload
- [x] Default message mode on a busy turn: Queue / Interrupt / Steer (new installs default to Steer)
- [x] Steer a live turn with attachments (files uploaded + scoped to the owning session)
- [x] Transparent Stream mode — chronological worklog with per-word fade-in (respects reduced-motion)

### Conversation controls
- [x] Copy message to clipboard (hover icon on each bubble)
- [x] Edit last user message and regenerate
- [x] Regenerate last response
- [x] Clear conversation (truncation-watermarked so cleared history cannot resurrect)
- [x] Branch / fork conversation from any message point (turn-boundary aligned on compacted sessions)
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
- [x] CLI session bridge — read CLI/agent sessions from state.db, surface running ones live, import as WebUI sessions
- [x] Fork a read-only cron (scheduled-job) session into an editable chat
- [x] Stable sidebar grouping (fork / compaction / subagent clusters don't reshuffle on refresh)
- [x] Cross-device recency re-sort (a reactivated conversation bumps to top even in a background tab)

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
- [x] Copy absolute + relative file path
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
- [x] No-cron-syntax schedule builder (frequency presets + time / day pickers, inline expression preview)
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
- [x] Seamless switching (no server restart, refreshes models / skills / memory / cron / workspace + forces config reload)
- [x] Profile-local workspace storage
- [x] First-run onboarding wizard with provider config (OpenRouter / Anthropic / OpenAI / Custom)
- [x] In-app OAuth for Codex and Claude
- [x] Concurrent per-profile isolation (context-local home override so parallel workers can't clobber each other)

### Configuration
- [x] Settings panel (default model, default workspace, send key, theme, skin, voice, font size)
- [x] Searchable settings
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
- [x] Built-ins: `/help`, `/clear`, `/model`, `/workspace`, `/new`, `/usage`, `/theme`, `/compact`, `/queue`, `/interrupt`, `/steer`, `/goal`, `/btw`, `/reasoning`, `/skills`, `/toolsets`
- [x] Transparent pass-through for unrecognized commands

### Security
- [x] Password auth with signed HMAC HTTP-only cookies (24h TTL)
- [x] Security headers (X-Content-Type-Options, X-Frame-Options, Referrer-Policy)
- [x] CSRF protection (scheme-aware, port-normalized for reverse proxies)
- [x] CORS preflight echoes allowlisted origin only — never wildcard
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
- [x] Open-redirect guard on `?next=` login param (bounded decode, collapses login-loop chains)
- [x] Auto-install gate (opt-in via `HERMES_WEBUI_AUTO_INSTALL=1`)

### Visual / UX
- [x] 3 base modes — Light, Dark, System (auto-sync)
- [x] 21 skins layered on the base mode — default, ares, catppuccin, charizard, codex, geist-contrast, github, graphite, hepburn, mono, neon, neon-paint, neon-soft, nous, poseidon, sienna, sisyphus, slate, terracotta, verdigris, zeus
- [x] 2-axis appearance model (base mode + skin) for community theme contributions
- [x] Mermaid diagram rendering (with fit / fullscreen toolbar)
- [x] KaTeX math rendering with fence-before-math fix
- [x] Syntax highlighting (Prism.js, language-aware, YAML newline preservation)
- [x] Markdown image syntax `![alt](url)` and inline MEDIA: tokens render as `<img>`
- [x] Plain URL auto-linking
- [x] Inline markdown in table cells (bold, italic, code, links)
- [x] Code block copy button
- [x] Tool card expand / collapse toggle
- [x] Collapsible thinking / reasoning cards (Claude extended thinking, o3 reasoning tokens)
- [x] Message timestamps (subtle, full date on hover)
- [x] Chat header (model, icon, TPS badge, timestamp) scales with text-size preference
- [x] Empty composer hides send button (icon-circle with pop-in animation)
- [x] Pluggable Lucide SVG icons (no emoji rendering inconsistencies)
- [x] Composer-centric controls (v0.50.0 UI overhaul)
- [x] Hermes Control Center modal (centralized actions)
- [x] Workspace panel state machine (defaults closed, opens for browsing / preview)
- [x] Three-panel desktop layout keeps a readable conversation floor when resizing
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
- [x] Streaming-scroll hardening — off-screen height retention, native scroll-anchoring, no jump-to-top on iOS/Android
- [x] Mobile drawer surfaces dashboard link + extension nav-actions

### Internationalization
- [x] 15 locales — English, Italian, Japanese, Russian, Spanish, German, Chinese (zh + zh-Hant), Portuguese, Korean, French, Czech, Turkish, Polish, Vietnamese
- [x] Near-full translation coverage across non-English locales — a small English-fallback tail remains on recently added keys (measured ~2% for Czech up to ~15% for Turkish), backfilled by periodic translation passes
- [x] Key-parity test ensures every locale has every key
- [x] Right-to-left and CJK input (IME composition fixes)

### Gateway integration
- [x] Real-time gateway sessions in sidebar (Telegram, Discord, Slack, WeChat/Weixin, Signal, SMS) via SSE + DB polling
- [x] Cross-channel handoff dock — composer-docked flyout summarizing the live external session
- [x] Transcript-summary card at 10+ rounds
- [x] Sidebar dedup keying on per-conversation identity (distinct chats from same platform stay separate)
- [x] Gateway session sync skips dup / delete options for external sessions
- [x] LLM Gateway routing metadata display — assistant turns and session metadata show the served model/provider, failover path, and model-switch warnings (#732)
- [x] Gateway status card in Settings (#1457)
- [x] Gateway approval-runs API opt-in (documented)

### MCP integration
- [x] MCP server management UI (System Settings → MCP Servers)
- [x] Add / edit / delete MCP server entries

### Extension system
- [x] Opt-in extension loader — serve local static assets, inject same-origin CSS/JS into the app shell
- [x] One-click install from an in-app gallery (Settings → Extensions) into a WebUI-managed state-dir directory (no restart, no env vars)
- [x] Manifest contract (`extensions.json`) for bundled / multi-extension installs
- [x] Capability surface — register custom themes (skins), register custom TTS engines, add nav-actions, declare loopback sidecars, embed an external web app in an iframe tab
- [x] Per-extension settings schema + owned storage
- [x] Consented extension sidecar proxy path (no arbitrary backend route registration)
- [x] Status + diagnostics endpoint
- [x] Trust model documented — extensions run with full session authority; install only vetted/self-authored
- [x] Vetted, versioned library repo with CI safety gates — [hermes-webui/hermes-webui-extensions](https://github.com/hermes-webui/hermes-webui-extensions) ("in the registry == vetted")
- [x] Core repo bundles zero extensions — clean client-side extensions are migrated to the library repo, not merged into core

### Distribution
- [x] Docker support (multi-arch amd64 + arm64, HEALTHCHECK, UID/GID auto-detect)
- [x] Two-container Docker compose (webui + agent)
- [x] GHCR auto-publish on tag push
- [x] Subpath mount support (reverse proxy at `/hermes/`)
- [x] PWA installable from any browser
- [x] Native macOS app — universal Intel + Apple Silicon, signed + notarized DMG, Sparkle 2 auto-update
- [x] Native Windows + Linux + macOS desktop app — Rust/Tauri, per-platform installers
- [x] Native Android app — Play-ready APK/AAB releases
- [x] Native iOS app — iPhone client (connects over Tailscale)

See [Native clients](#native-clients) for the per-repo detail.

---

## Native clients

The web app is the engine; a small fleet of native shells wrap it so Hermes runs
as a real app on every platform. Each lives in its **own repo** with its own
versioning and release cadence — none is a fork of the WebUI source.

| Client | Platforms | Tech | Repo |
|---|---|---|---|
| **Hermes for Mac** | macOS (Intel + Apple Silicon) | Swift + WKWebView, SSH tunnel, Sparkle 2 auto-update, signed + notarized DMG | [hermes-webui/hermes-swift-mac](https://github.com/hermes-webui/hermes-swift-mac) |
| **Hermes Desktop** | Windows + Linux + macOS | Rust / Tauri (WebView2 / WebKitGTK), per-platform installers | [hermes-webui/hermes-desktop-rust](https://github.com/hermes-webui/hermes-desktop-rust) |
| **Hermes for Android** | Android | Native Android, Play-ready releases | [hermes-webui/hermes-android](https://github.com/hermes-webui/hermes-android) |
| **Hermes for iOS** | iOS (iPhone) | Native Swift, connects over Tailscale (QR / hostname pairing) | [hermes-webui/hermes-swift-ios](https://github.com/hermes-webui/hermes-swift-ios) |

One server, every client: run the WebUI on one machine and reach it from web,
desktop, and phone. Live versions and release notes are on each repo's Releases
page — this table intentionally carries no version numbers (they drift per repo).

---

## Autonomous project maintenance

Hermes WebUI is maintained by an autonomous agent system: inbound issues and PRs
are triaged, deep-reviewed (multi-model gate + full test suite + browser QA),
released, and closed with attribution — largely without manual steering. The
project-agnostic distillation of that system is published as an open framework so
any maintainer can adopt it:

- **StewardOS** — [nesquena/steward-os](https://github.com/nesquena/steward-os) ·
  docs at [nesquena.github.io/steward-os](https://nesquena.github.io/steward-os/).
  Roles, autonomy bands, the security spine, issue/PR/quality-gate lifecycles,
  and reusable skills — harness-agnostic (no specific agent runtime required).

StewardOS is the *generalization*; the WebUI's own operating procedures are the
*source*. This is the reason contributor PRs get same-day deep review and the
release cadence runs multiple ships per day.

---

## Forward work

Most of the original forward-work list has shipped. What remains is either an
open feature request under active consideration or an explicitly-deferred concept.
Issue state drifts — re-derive with `gh issue list --repo nesquena/hermes-webui`.

### Open candidates (feature requests under active consideration)

| Theme | Tracking | Why |
|---|---|---|
| Lightweight in-app Canvas editing | #1255 | Text canvas for prompt drafting / shared notes |
| Provider / Model source-of-truth alignment | #1240 | Reconcile WebUI vs CLI vs Gateway provider resolution |
| Built-in SearXNG web search | #1037 | Lightweight search tool with on / off toggle |
| Sunset legacy `LMSTUDIO_API_KEY` env var | #1502 | Alias stays for one minor cycle, then removed |
| Native MCP server expose | #733 | Hermes WebUI as an MCP server for direct agent integration |
| Teams / agents management panel | #719 | Editable names, roles, assignments |
| WebUI profile ↔ Hermes runtime model alignment | #749 | Design parity between WebUI profiles and the runtime model |
| Add agent / replace model modals | #698 | Dedicated modals for agent + model management |

### Backlog (deferred, listed for visibility)

- **Insights / monitoring suite** — data tabs / live integration (#722), monitor dashboard concepts (#721)
- **Code execution inline cells** — Jupyter-style cell rendering inside chat
- **Sharing / public conversation URLs** — requires hosted backend with access control (out of scope for self-host)

### Intentionally not planned
- Full SwiftUI/native rewrite of the frontend — the WebView shells already get ~95% of native benefit at a fraction of the maintenance cost
- App Store distribution for the desktop apps — sandboxing breaks the local-server model
- Real-time multi-user collaboration — single-user assumption throughout
- Multi-tenant / white-label hosting — the project is single-user / multi-profile, not multi-tenant
- Anthropic / Claude proprietary features — Projects AI memory, Claude artifacts sync (not reproducible)

> Note: a plugin/extension marketplace was once "not planned" — that gap is now
> filled by the [extension system](#extension-system) + the vetted library repo,
> which cover the customization surface without a heavyweight dependency system.

---

## Sprint history

Per-version detail lives in [CHANGELOG.md](./CHANGELOG.md). The table below is a high-level chronology of major sprint themes; individual PR / fix detail lives in CHANGELOG to keep this file readable.

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
| Sprint 24 | Web polish | rAF streaming, git detection, collapsible date groups, context ring (#80–#83) |
| Sprint 25 | macOS desktop application | Native Swift + WKWebView shell, universal DMG, Sparkle 2 auto-update |
| Sprint 26 | Pluggable themes | Light / Slate / Solarized / Monokai / Nord, settings unsaved-changes guard, `/theme` |
| Sprint 27 | Theme polish | 30+ hardcoded colors → CSS variables, light theme final polish |
| Sprint 28 | Security hardening | Env race fix, random signing key, upload traversal, PBKDF2 |
| Sprints 29–32 | Model routing + custom endpoints + reasoning | Model routing by provider prefix, custom endpoint URL fix, OLED theme, top-level reasoning, message_count sync |
| Sprint 33 | Approval card + Lucide icons | Approval prompt surfaced, emoji → SVG, login CSP fix, update diagnostics |
| Sprint 34 | v0.50.0 UI overhaul | Composer-centric controls, Control Center modal, workspace state machine, collapsible date groups, rAF throttle, context ring |
| Sprints 35–37 | Onboarding + i18n | First-run wizard, provider config, Spanish locale, Docker two-container, mobile Profiles button |
| Sprints 38–40 | Session + UI polish | Five-bug clean-up, sidebar timestamp, test port isolation |
| Sprints 41–42 | Renderer hardening + KaTeX + handoff | Context ring live usage, renderMd link / image / code stash chain, MEDIA: image rendering, gateway handoff foundation |
| Sprints 43+ | Continuous contributor sprints | Custom providers, more locales, IME fixes, model-switch toast, approval queue multi-slot, profile polish, font-size CSS, contributor wave |
| Ongoing | Ecosystem expansion + reliability | Extension system + library, Rust/Android/iOS native clients, StewardOS, gateway platform expansion (Signal/SMS/WeChat), Transparent Stream, mobile scroll hardening, session-load perf, test-isolation flake elimination |

---

## Versioning conventions

- **Patch** (`v0.51.X`) — small batches, contributor PR releases, hotfixes
- **Minor** (`v0.X.0`) — sprint completion, new feature surface, architecture milestone
- **Major** (`v1.0.0`) — declared when the feature surface stabilizes and reliability across all clients reaches steady state

Per-version detail and contributor attribution live in [CHANGELOG.md](./CHANGELOG.md).
