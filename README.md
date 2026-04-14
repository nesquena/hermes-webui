# Hermes Web UI

[Hermes Agent](https://hermes-agent.nousresearch.com/) is a sophisticated autonomous agent that lives on your server, accessed via a terminal or messaging apps, that remembers what it learns and gets more capable the longer it runs.

Hermes WebUI is a lightweight, dark-themed web app interface in your browser for [Hermes Agent](https://hermes-agent.nousresearch.com/).
Full parity with the CLI experience - everything you can do from a terminal,
you can do from this UI. No build step, no framework, no bundler. Just Python
and vanilla JS.

Layout: three-panel. Left sidebar for sessions and navigation, center for chat,
right for workspace file browsing. Model, profile, and workspace controls live in
the **composer footer** — always visible while composing. A circular context ring
shows token usage at a glance. All settings and session tools are in the
**Hermes Control Center** (launcher at the sidebar bottom).

<img alt="Hermes Web UI — three-panel layout" width="1417" height="867" alt="image" src="https://github.com/user-attachments/assets/51adff98-53ee-4800-8508-78b6c34dd3dc" />

<table>
  <tr>
    <td width="50%" align="center">
      <img alt="Light mode with full profile support" src="https://github.com/user-attachments/assets/9b68142f-d974-4493-a8d1-fd73e622c7fd" />
      <br /><sub>Light mode with full profile support</sub>
    </td>
    <td width="50%" align="center">
      <img alt="Customize your settings, configure a password" src="https://github.com/user-attachments/assets/941f3156-21e3-41fd-bcc8-f975d5000cb8" />
      <br /><sub>Customize your settings, configure a password</sub>
    </td>
  </tr>
</table>

<table>
  <tr>
    <td width="50%" align="center">
      <img alt="Workspace file browser with inline preview" src="docs/images/ui-workspace.png" />
      <br /><sub>Workspace file browser with inline preview</sub>
    </td>
    <td width="50%" align="center">
      <img alt="Session projects, tags, and tool call cards" src="docs/images/ui-sessions.png" />
      <br /><sub>Session projects, tags, and tool call cards</sub>
    </td>
  </tr>
</table>

This gives you nearly **1:1 parity with Hermes CLI from a convenient web UI** which you can access securely through an SSH tunnel from your Hermes setup. Single command to start this up, and a single command to SSH tunnel for access on your computer. Every single part of the web UI uses your existing Hermes agent and existing models, without requiring any additional setup.

---

## Why Hermes

Most AI tools reset every session. They don't know who you are, what you worked on, or what
conventions your project follows. You re-explain yourself every time.

Hermes retains context across sessions, runs scheduled jobs while you're offline, and gets
smarter about your environment the longer it runs. It uses your existing Hermes agent setup,
your existing models, and requires no additional configuration to start.

What makes it different from other agentic tools:

- **Persistent memory** — user profile, agent notes, and a skills system that saves reusable
  procedures; Hermes learns your environment and does not have to relearn it
- **Self-hosted scheduling** — cron jobs that fire while you're offline and deliver results to
  Telegram, Discord, Slack, Signal, email, and more
- **10+ messaging platforms** — the same agent available in the terminal is reachable from your phone
- **Self-improving skills** — Hermes writes and saves its own skills automatically from experience;
  no marketplace to browse, no plugins to install
- **Provider-agnostic** — OpenAI, Anthropic, Google, DeepSeek, OpenRouter, and more
- **Orchestrates other agents** — can spawn Claude Code or Codex for heavy coding tasks and bring
  the results back into its own memory
- **Self-hosted** — your conversations, your memory, your hardware

**vs. the field** *(landscape is actively shifting — see [HERMES.md](HERMES.md) for the full breakdown)*:

| | OpenClaw | Claude Code | Codex CLI | OpenCode | Hermes |
|---|---|---|---|---|---|
| Persistent memory (auto) | Yes | Partial† | Partial | Partial | Yes |
| Scheduled jobs (self-hosted) | Yes | No‡ | No | No | Yes |
| Messaging app access | Yes (15+ platforms) | Partial (Telegram/Discord preview) | No | No | Yes (10+) |
| Web UI (self-hosted) | Dashboard only | No | No | Yes | Yes |
| Self-improving skills | Partial | No | No | No | Yes |
| Python / ML ecosystem | No (Node.js) | No | No | No | Yes |
| Provider-agnostic | Yes | No (Claude only) | Yes | Yes | Yes |
| Open source | Yes (MIT) | No | Yes | Yes | Yes |

† Claude Code has CLAUDE.md / MEMORY.md project context and rolling auto-memory, but not full automatic cross-session recall  
‡ Claude Code has cloud-managed scheduling (Anthropic infrastructure) and session-scoped `/loop`; no self-hosted cron

**The closest competitor is OpenClaw** — both are always-on, self-hosted, open-source agents
with memory, cron, and messaging. The key differences: Hermes writes and saves its own skills
automatically as a core behavior (OpenClaw's skill system centers on a community marketplace);
Hermes is more stable across updates (OpenClaw has documented release regressions and ClawHub
has had security incidents involving malicious skills); and Hermes runs natively in the Python
ecosystem. See [HERMES.md](HERMES.md) for the full side-by-side.

---

## Quick start

Run the repo bootstrap:

```bash
git clone https://github.com/nesquena/hermes-webui.git hermes-webui
cd hermes-webui
python3 bootstrap.py
```

Or keep using the shell launcher:

```bash
./start.sh
```

The bootstrap will:

1. Detect Hermes Agent and, if missing, attempt the official installer (`curl -fsSL https://raw.githubusercontent.com/NousResearch/hermes-agent/main/scripts/install.sh | bash`).
2. Find or create a Python environment with the WebUI dependencies.
3. Start the web server and wait for `/health`.
4. Open the browser unless you pass `--no-browser`.
5. Drop you into a first-run onboarding wizard inside the WebUI.

> Native Windows is not supported for this bootstrap yet. Use Linux, macOS, or WSL2.

If provider setup is still incomplete after install, the onboarding wizard will point you to finish it with `hermes model` instead of trying to replicate the full CLI setup in-browser.

---

## Docker

**Pre-built images** (amd64 + arm64) are published to GHCR on every release:

Make sure the `HERMES_WEBUI_STATE_DIR` (by default `~/.hermes/webui-mvp`, as detailed in the `.env.example` file) folder exist with the UID/GID of the owner of the `.hermes` folder. 
The container will also mount your configured "workspace" (also from the example .env.example) as `/workspace`. adapt the location as needed.


```bash
docker pull ghcr.io/nesquena/hermes-webui:latest
docker run -d \
-e WANTED_UID=`id -u` -e WANTED_GID=`id -g` \
-v ~/.hermes:/home/hermeswebui/.hermes -e HERMES_WEBUI_STATE_DIR=/home/hermeswebui/.hermes/webui-mvp \
-v ~/workspace:/workspace \
-p 8787:8787 ghcr.io/nesquena/hermes-webui:latest
```

Or run with Docker Compose (recommended):

```bash
# Check the docker-compose.yml and make sure to adapt as needed, at minimum WANTED_UID/WANTED_GID
docker compose up -d
```

Or build locally:

```bash
docker build -t hermes-webui .
docker run -d \
-e WANTED_UID=`id -u` -e WANTED_GID=`id -g` \
-v ~/.hermes:/home/hermeswebui/.hermes -e HERMES_WEBUI_STATE_DIR=/home/hermeswebui/.hermes/webui-mvp \
-v ~/workspace:/workspace \
-p 8787:8787 hermes-webui
```

Open http://localhost:8787 in your browser.

To enable password protection:

```bash
docker run -d \
-e WANTED_UID=`id -u` -e WANTED_GID=`id -g` \
-v ~/.hermes:/home/hermeswebui/.hermes -e HERMES_WEBUI_STATE_DIR=/home/hermeswebui/.hermes/webui-mvp \
-v ~/workspace:/workspace \
-p 8787:8787 -e HERMES_WEBUI_PASSWORD=your-secret ghcr.io/nesquena/hermes-webui:latest
```

> **Note:** By default, Docker Compose binds to `127.0.0.1` (localhost only).
> To expose on a network, change the port to `"8787:8787"` in `docker-compose.yml`
> and set `HERMES_WEBUI_PASSWORD` to enable authentication.

### Two-container setup (Agent + WebUI)

If you run the Hermes Agent in its own Docker container and want the WebUI
in a separate container:

```bash
docker compose -f docker-compose.two-container.yml up -d
```

This starts both containers with shared volumes:

- **`hermes-home`** — shared `~/.hermes` for config, sessions, skills, memory
- **`hermes-agent-src`** — the agent's source code, mounted into the WebUI
  container so it can install the agent's Python dependencies at startup

The WebUI's init script automatically installs hermes-agent and all its
dependencies (openai, anthropic, etc.) into its own Python environment on
first boot. Subsequent restarts reuse the installed packages.

> **How it works:** The WebUI imports hermes-agent's Python modules directly
> (not via HTTP). The shared volume makes the agent source available, and
> the init script runs `uv pip install` to set up the dependencies. Both
> containers share the same `~/.hermes` directory for config and state.

See `docker-compose.two-container.yml` for the full configuration.

---

## What start.sh discovers automatically

| Thing | How it finds it |
|---|---|
| Hermes agent dir | `HERMES_WEBUI_AGENT_DIR` env, then `~/.hermes/hermes-agent`, then sibling `../hermes-agent` |
| Python executable | Agent venv first, then `.venv` in this repo, then system `python3` |
| State directory | `HERMES_WEBUI_STATE_DIR` env, then `~/.hermes/webui-mvp` |
| Default workspace | `HERMES_WEBUI_DEFAULT_WORKSPACE` env, then `~/workspace`, then state dir |
| Port | `HERMES_WEBUI_PORT` env or first argument, default `8787` |

If discovery finds everything, nothing else is required.

---

## Overrides (only needed if auto-detection misses)

```bash
export HERMES_WEBUI_AGENT_DIR=/path/to/hermes-agent
export HERMES_WEBUI_PYTHON=/path/to/python
export HERMES_WEBUI_PORT=9000
./start.sh
```

Or inline:

```bash
HERMES_WEBUI_AGENT_DIR=/custom/path ./start.sh 9000
```

Full list of environment variables:

| Variable | Default | Description |
|---|---|---|
| `HERMES_WEBUI_AGENT_DIR` | auto-discovered | Path to the hermes-agent checkout |
| `HERMES_WEBUI_PYTHON` | auto-discovered | Python executable |
| `HERMES_WEBUI_HOST` | `127.0.0.1` | Bind address |
| `HERMES_WEBUI_PORT` | `8787` | Port |
| `HERMES_WEBUI_STATE_DIR` | `~/.hermes/webui-mvp` | Where sessions and state are stored |
| `HERMES_WEBUI_DEFAULT_WORKSPACE` | `~/workspace` | Default workspace |
| `HERMES_WEBUI_DEFAULT_MODEL` | `openai/gpt-5.4-mini` | Default model |
| `HERMES_WEBUI_PASSWORD` | *(unset)* | Set to enable password authentication |
| `HERMES_HOME` | `~/.hermes` | Base directory for Hermes state (affects all paths) |
| `HERMES_CONFIG_PATH` | `~/.hermes/config.yaml` | Path to Hermes config file |

---

## Accessing from a remote machine

The server binds to `127.0.0.1` by default (loopback only). If you are running
Hermes on a VPS or remote server, use an SSH tunnel from your local machine:

```bash
ssh -N -L <local-port>:127.0.0.1:<remote-port> <user>@<server-host>
```

Example:

```bash
ssh -N -L 8787:127.0.0.1:8787 user@your.server.com
```

Then open `http://localhost:8787` in your local browser.

`start.sh` will print this command for you automatically when it detects you
are running over SSH.

---

## Accessing on your phone with Tailscale

[Tailscale](https://tailscale.com) is a zero-config mesh VPN built on
WireGuard. Install it on your server and your phone, and they join the same
private network -- no port forwarding, no SSH tunnels, no public exposure.

The Hermes Web UI is fully responsive with a mobile-optimized layout
(hamburger sidebar, bottom navigation bar, touch-friendly controls), so it
works well as a daily-driver agent interface from your phone.

**Setup:**

1. Install [Tailscale](https://tailscale.com/download) on your server and
   your iPhone/Android.
2. Start the WebUI listening on all interfaces with password auth enabled:

```bash
HERMES_WEBUI_HOST=0.0.0.0 HERMES_WEBUI_PASSWORD=your-secret ./start.sh
```

3. Open `http://<server-tailscale-ip>:8787` in your phone's browser
   (find your server's Tailscale IP in the Tailscale app or with
   `tailscale ip -4` on the server).

That's it. Traffic is encrypted end-to-end by WireGuard, and password auth
protects the UI at the application level. You can add it to your home screen
for an app-like experience.

> **Tip:** If using Docker, set `HERMES_WEBUI_HOST=0.0.0.0` in your
> `docker-compose.yml` environment (already the default) and set
> `HERMES_WEBUI_PASSWORD`.

---

## Manual launch (without start.sh)

If you prefer to launch the server directly:

```bash
cd /path/to/hermes-agent          # or wherever sys.path can find Hermes modules
HERMES_WEBUI_PORT=8787 venv/bin/python /path/to/hermes-webui/server.py
```

Note: use the agent venv Python (or any Python environment that has the Hermes agent dependencies installed). System Python will be missing `openai`, `httpx`, and other required packages.

Health check:

```bash
curl http://127.0.0.1:8787/health
```

---

## Running tests

Tests discover the repo and the Hermes agent dynamically -- no hardcoded paths.

```bash
cd hermes-webui
pytest tests/ -v --timeout=60
```

Or using the agent venv explicitly:

```bash
/path/to/hermes-agent/venv/bin/python -m pytest tests/ -v
```

Tests run against an isolated server on port 8788 with a separate state directory.
Production data and real cron jobs are never touched. Current count: **961 tests**
across 53 test files.

---

## Features

### Chat and agent
- Streaming responses via SSE (tokens appear as they are generated)
- Multi-provider model support -- any Hermes API provider (OpenAI, Anthropic, Google, DeepSeek, Nous Portal, OpenRouter, MiniMax, Z.AI); dynamic model dropdown populated from configured keys
- Send a message while one is processing -- it queues automatically
- Edit any past user message inline and regenerate from that point
- Retry the last assistant response with one click
- Cancel a running task directly from the composer footer (Stop button next to Send)
- Tool call cards inline -- each shows the tool name, args, and result snippet; expand/collapse all toggle for multi-tool turns
- Subagent delegation cards -- child agent activity shown with distinct icon and indented border
- Mermaid diagram rendering inline (flowcharts, sequence diagrams, gantt charts)
- Thinking/reasoning display -- collapsible gold-themed cards for Claude extended thinking and o3 reasoning blocks
- Approval card for dangerous shell commands (allow once / session / always / deny)
- SSE auto-reconnect on network blips (SSH tunnel resilience)
- File attachments persist across page reloads
- Message timestamps (HH:MM next to each message, full date on hover)
- Code block copy button with "Copied!" feedback
- Syntax highlighting via Prism.js (Python, JS, bash, JSON, SQL, and more)
- Safe HTML rendering in AI responses (bold, italic, code converted to markdown)
- rAF-throttled token streaming for smoother rendering during long responses
- Context usage indicator in composer footer -- token count, cost, and fill bar (model-aware)

### Sessions
- Create, rename, duplicate, delete, search by title and message content
- Session actions via `⋯` dropdown per session — pin, move to project, archive, duplicate, delete
- Pin/star sessions to the top of the sidebar (gold indicator)
- Archive sessions (hide without deleting, toggle to show)
- Session projects -- named groups with colors for organizing sessions
- Session tags -- add #tag to titles for colored chips and click-to-filter
- Grouped by Today / Yesterday / Earlier in the sidebar (collapsible date groups)
- Download as Markdown transcript, full JSON export, or import from JSON
- Sessions persist across page reloads and SSH tunnel reconnects
- Browser tab title reflects the active session name
- CLI session bridge -- CLI sessions from hermes-agent's SQLite store appear in the sidebar with a gold "cli" badge; click to import with full history and reply normally
- Token/cost display -- input tokens, output tokens, estimated cost shown per conversation (toggle in Settings or `/usage` command)

### Workspace file browser
- Directory tree with expand/collapse (single-click toggles, double-click navigates)
- Breadcrumb navigation with clickable path segments
- Preview text, code, Markdown (rendered), and images inline
- Edit, create, delete, and rename files; create folders
- Binary file download (auto-detected from server)
- File preview auto-closes on directory navigation (with unsaved-edit guard)
- Git detection -- branch name and dirty file count badge in workspace header
- Right panel is drag-resizable
- Syntax highlighted code preview (Prism.js)

### Voice input
- Microphone button in the composer (Web Speech API)
- Tap to record, tap again or send to stop
- Live interim transcription appears in the textarea
- Auto-stops after ~2s of silence
- Appends to existing textarea content (doesn't replace)
- Hidden when browser doesn't support Web Speech API (Chrome, Edge, Safari)

### Profiles
- Profile chip in the **composer footer** -- dropdown showing all profiles with gateway status and model info
- Gateway status dots (green = running), model info, skill count per profile
- Profiles management panel -- create, switch, and delete profiles from the sidebar
- Clone config from active profile on create
- Optional custom endpoint fields on create -- Base URL and API key written into the profile's `config.yaml` at creation time, so Ollama, LMStudio, and other local endpoints can be configured without editing files manually
- Seamless switching -- no server restart; reloads config, skills, memory, cron, models
- Per-session profile tracking (records which profile was active at creation)

### Authentication and security
- Optional password auth -- off by default, zero friction for localhost
- Enable via `HERMES_WEBUI_PASSWORD` env var or Settings panel
- Signed HMAC HTTP-only cookie with 24h TTL
- Minimal dark-themed login page at `/login`
- Security headers on all responses (X-Content-Type-Options, X-Frame-Options, Referrer-Policy)
- 20MB POST body size limit
- CDN resources pinned with SRI integrity hashes

### Themes
- 7 built-in themes: Dark (default), Light, Slate, Solarized Dark, Monokai, Nord, OLED
- Switch via Settings panel dropdown (instant live preview) or `/theme` command
- Persists across reloads (server-side in settings.json + localStorage for flicker-free loading)
- Custom themes: define a `:root[data-theme="name"]` CSS block and it works — see [THEMES.md](THEMES.md)

### Settings and configuration
- **Hermes Control Center** (sidebar launcher button) -- Conversation tab (export/import/clear), Preferences tab (model, send key, theme, language, all toggles), System tab (version, password)
- Send key: Enter (default) or Ctrl/Cmd+Enter
- Show/hide CLI sessions toggle (enabled by default)
- Token usage display toggle (off by default, also via `/usage` command)
- Control Center always opens on the Conversation tab; resets on close
- Unsaved changes guard -- discard/save prompt when closing with unpersisted changes
- Cron completion alerts -- toast notifications and unread badge on Tasks tab
- Background agent error alerts -- banner when a non-active session encounters an error

### Slash commands
- Type `/` in the composer for autocomplete dropdown
- Built-in: `/help`, `/clear`, `/model <name>`, `/workspace <name>`, `/new`, `/usage`, `/theme`, `/compact`
- Arrow keys navigate, Tab/Enter select, Escape closes
- Unrecognized commands pass through to the agent

### Panels
- **Chat** -- session list, search, pin, archive, projects, new conversation
- **Tasks** -- view, create, edit, run, pause/resume, delete cron jobs; run history; completion alerts
- **Skills** -- list all skills by category, search, preview, create/edit/delete; linked files viewer
- **Memory** -- view and edit MEMORY.md and USER.md inline
- **Profiles** -- create, switch, delete agent profiles; clone config
- **Todos** -- live task list from the current session
- **Spaces** -- add, rename, remove workspaces; quick-switch from topbar

### Mobile responsive
- Hamburger sidebar -- slide-in overlay on mobile (<640px)
- Bottom navigation bar -- 5-tab iOS-style fixed bar
- Files slide-over panel from right edge
- Touch targets minimum 44px on all interactive elements
- Composer positioned above bottom nav
- Desktop layout completely unchanged

---

## Architecture

```
server.py               HTTP routing shell + auth middleware (~154 lines)
api/
  auth.py               Optional password authentication, signed cookies (~201 lines)
  config.py             Discovery, globals, model detection, reloadable config (~1110 lines)
  helpers.py            HTTP helpers, security headers (~175 lines)
  models.py             Session model + CRUD + CLI bridge (~377 lines)
  onboarding.py         First-run onboarding wizard, OAuth provider support (~507 lines)
  profiles.py           Profile state management, hermes_cli wrapper (~411 lines)
  routes.py             All GET + POST route handlers (~2250 lines)
  state_sync.py         /insights sync — message_count to state.db (~113 lines)
  streaming.py          SSE engine, run_agent, cancel support (~660 lines)
  updates.py            Self-update check and release notes (~257 lines)
  upload.py             Multipart parser, file upload handler (~82 lines)
  workspace.py          File ops, workspace helpers, git detection (~288 lines)
static/
  index.html            HTML template (~600 lines)
  style.css             All CSS incl. mobile responsive, themes (~1050 lines)
  ui.js                 DOM helpers, renderMd, tool cards, context indicator (~1740 lines)
  workspace.js          File preview, file ops, git badge (~286 lines)
  sessions.js           Session CRUD, collapsible groups, search, reload recovery (~800 lines)
  messages.js           send(), SSE handlers, live streaming, session recovery (~655 lines)
  panels.js             Cron, skills, memory, profiles, settings (~1438 lines)
  commands.js           Slash command autocomplete (~267 lines)
  boot.js               Mobile nav, voice input, boot IIFE (~524 lines)
tests/
  conftest.py           Isolated test server (port 8788)
  61 test files          961 test functions
Dockerfile              python:3.12-slim container image
docker-compose.yml      Compose with named volume and optional auth
.github/workflows/      CI: multi-arch Docker build + GitHub Release on tag
```

State lives outside the repo at `~/.hermes/webui-mvp/` by default
(sessions, workspaces, settings, projects, last_workspace). Override with `HERMES_WEBUI_STATE_DIR`.

---

## Docs

- `HERMES.md` -- why Hermes, mental model, and detailed comparison to Claude Code / Codex / OpenCode / Cursor
- `ROADMAP.md` -- feature roadmap and sprint history
- `ARCHITECTURE.md` -- system design, all API endpoints, implementation notes
- `TESTING.md` -- manual browser test plan and automated coverage reference
- `CHANGELOG.md` -- release notes per sprint
- `SPRINTS.md` -- forward sprint plan with CLI + Claude parity targets
- `THEMES.md` -- theme system documentation, custom theme guide

## Contributors

Hermes WebUI is built with help from the open-source community. Every PR — whether merged directly or incorporated via rebase — shapes the project, and we're grateful to everyone who has taken the time to contribute.

### Major contributions

**[@aronprins](https://github.com/aronprins)** — v0.50.0 UI overhaul (PR #242)
The biggest single contribution to the project: a complete UI redesign that moved model/profile/workspace controls into the composer footer, replaced the gear-icon settings panel with the Hermes Control Center (tabbed modal), removed the activity bar in favor of inline composer status, redesigned the session list with a `⋯` action dropdown, and added the workspace panel state machine. 26 commits, thoroughly designed and iterated through multiple review rounds.

**[@iRonin](https://github.com/iRonin)** — Security hardening sprint (PRs #196–#204)
Six consecutive security and reliability PRs: session memory leak fix (expired token pruning), Content-Security-Policy + Permissions-Policy headers, 30-second slow-client connection timeout, optional HTTPS/TLS support via environment variables, upstream branch tracking fix for self-update, and CLI session support in the file browser API. This is the kind of focused, high-quality security work that makes a self-hosted tool trustworthy.

**[@DavidSchuchert](https://github.com/DavidSchuchert)** — German translation (PR #190)
Complete German locale (`de`) covering all UI strings, settings labels, commands, and system messages — and in doing so, stress-tested the i18n system and exposed several elements that weren't yet translatable, which got fixed as part of the same PR.

**[@Jordan-SkyLF](https://github.com/Jordan-SkyLF)** — Live streaming, session recovery, workspace fallback (PRs #366, #367)
Three interlocking improvements: workspace fallback resolution so the server recovers gracefully when the configured workspace is deleted or unavailable; live reasoning cards that upgrade the generic thinking spinner to a real-time reasoning display as the model thinks; and durable session state recovery via `localStorage` so in-flight tool cards, partial assistant output, and the live SSE stream all survive a full page reload or session switch.

### Feature contributions

**[@gabogabucho](https://github.com/gabogabucho)** — Spanish locale + onboarding wizard (PRs #275, #285)
Full Spanish (`es`) locale covering all 175 UI strings, plus the one-shot bootstrap onboarding wizard that guides new users through provider setup on first launch — the feature most responsible for new users actually getting started.

**[@bergeouss](https://github.com/bergeouss)** — Real-time gateway session sync (PR #274)
Bridged the gateway session database (Telegram, Discord, Slack, etc.) into the WebUI sidebar with live SSE polling. Gateway sessions now appear alongside WebUI sessions in real time, without any changes to hermes-agent.

**[@ccqqlo](https://github.com/ccqqlo)** — Terminal approval UX + custom model discovery + mobile close button (PRs #224, #225, #238, #333)
A run of focused quality-of-life improvements: terminal tool approval prompts that stay visible long enough to actually be read, restored custom model API key discovery, and the redundant mobile close button fix that had been confusing users on narrow screens.

**[@kevin-ho](https://github.com/kevin-ho)** — OLED theme (PR #168)
Added the 7th built-in theme: pure black backgrounds with warm accents tuned to reduce burn-in risk. Small diff, big impact for anyone on an OLED display.

**[@Bobby9228](https://github.com/Bobby9228)** — Mobile Profiles button + Android Chrome fixes (PRs #253, #263, #265)
Added the Profiles tab to the mobile bottom navigation bar, making profile switching reachable on phones, plus a set of Android Chrome-specific fixes for the profile dropdown.

**[@franksong2702](https://github.com/franksong2702)** — Session title guard + breadcrumb nav (PRs #301, #302)
Two clean bug fixes / features: the session title guard that stops `title_from()` from overwriting user-renamed sessions after every turn, and clickable breadcrumb navigation in the workspace file preview panel.

**[@betamod](https://github.com/betamod)** — Security hardening (PR #171)
A comprehensive security audit PR covering CSRF protection, SSRF guards, XSS escaping improvements, and the env race condition between concurrent agent sessions — foundational security work that shipped in v0.39.0.

**[@TaraTheStar](https://github.com/TaraTheStar)** — Bot name + thinking blocks + login refactor (PRs #132, #176, #181)
Made the assistant display name configurable throughout the UI, added thinking/reasoning block display in chat, and refactored the login page to use template variables instead of inline string replacement.

**[@thadreber-web](https://github.com/thadreber-web)** — CLI session bridge (PR #56)
The original CLI session bridge: reads CLI sessions from the agent's SQLite state store and surfaces them in the WebUI sidebar. This was the first bridge between the CLI and WebUI session worlds.

**[@deboste](https://github.com/deboste)** — Reverse proxy auth + mobile responsive layout + model routing (PRs #3, #4, #5)
Three of the very first community PRs: fixed EventSource/fetch to use the URL origin for reverse proxy setups, corrected model provider routing from config, and added mobile responsive layout with dvh viewport fix. Early foundation work.

### Bug fix and security contributions

**[@Hinotoi-agent](https://github.com/Hinotoi-agent)** — Profile .env secret isolation (PR #351)
Fixed API key leakage between profiles on switch — switching from a profile with `OPENAI_API_KEY` to one without it left the key in the process environment for the duration of the session, effectively leaking credentials. A subtle and important security fix.

**[@lawrencel1ng](https://github.com/lawrencel1ng)** — Bandit security fixes B310/B324/B110 + QuietHTTPServer (PR #354)
Systematic bandit security scan fixes: URL scheme validation before `urlopen`, MD5 `usedforsecurity=False`, and 40+ bare `except: pass` blocks replaced with proper logging — plus `QuietHTTPServer` to stop client-disconnect log spam from SSE streams.

**[@lx3133584](https://github.com/lx3133584)** — CSRF fix for reverse proxy on non-standard ports (PR #360)
Fixed CSRF rejection for deployments behind Nginx Proxy Manager or similar on non-standard ports — a real-world blocker for anyone hosting on a port other than 80/443.

**[@DelightRun](https://github.com/DelightRun)** — session_search fix for WebUI sessions (PR #356)
The `session_search` tool silently returned "Session database not available" in every WebUI session. Tracked down the missing `SessionDB` injection in the streaming path and fixed it.

**[@shaoxianbilly](https://github.com/shaoxianbilly)** — Unicode filename downloads (PR #378)
Fixed `UnicodeEncodeError` crashes when downloading workspace files with Chinese, Japanese, or other non-ASCII names. Implemented proper `Content-Disposition` header with RFC 5987 `filename*=UTF-8''...` encoding.

**[@huangzt](https://github.com/huangzt)** — Cancel interrupts agent (PR #244)
Made the Cancel button actually interrupt the running agent and clean up UI state, rather than just hiding the button while the agent kept running.

**[@tgaalman](https://github.com/tgaalman)** — Thinking card fix (PR #169)
Fixed top-level reasoning fields being missed in the thinking card display — an edge case in how Claude's extended thinking blocks surface in the API response.

**[@smurmann](https://github.com/smurmann)** — Custom provider routing fix (PR #189)
Fixed model routing for slash-prefixed custom provider models, which were being misrouted in the model selector. A precise fix for a real edge case in multi-provider setups.

**[@jeffscottward](https://github.com/jeffscottward)** — Claude Haiku model ID fix (PR #145)
Caught and corrected the Claude Haiku model ID (`3-5` → `4-5`) immediately after the Anthropic release — the kind of quick community catch that keeps the model dropdown accurate.

**[@kcclaw001](https://github.com/kcclaw001)** — Credential redaction in API responses (PR #243)
Added credential redaction to all API response paths so API keys, tokens, and other secrets in session data or error messages are masked before reaching the browser.

**[@mbac](https://github.com/mbac)** — Phantom "Custom" provider group fix (PR #191)
Removed the phantom "Custom" optgroup that appeared in the model dropdown even when no custom provider was configured — a small but consistently confusing UI noise issue.

**[@andrewy-wizard](https://github.com/andrewy-wizard)** — Chinese localization (PR #177)
Added Simplified Chinese (`zh`) locale to the WebUI. One of the first non-English locales and the most-used non-English locale in the codebase.

**[@mmartial](https://github.com/mmartial)** — Docker UID/GID matching (PR #237)
Added Docker support for running as an arbitrary UID/GID matching the host user, eliminating permission issues with bind-mounted volumes — essential for Docker deployments where the host user isn't UID 1000.

**[@vCillusion](https://github.com/vCillusion)** — pip package resolution fix (PR #76)
Fixed agent dependency resolution to prefer packages from the venv's site-packages over the agent directory itself, preventing shadowing bugs when developing locally.

**[@carlytwozero](https://github.com/carlytwozero)** — API key pass-through for non-Anthropic providers (PR #78)
Fixed `api_key` not being passed to `AIAgent` for non-Anthropic `/anthropic` providers — a quiet regression that silently broke any non-default provider.

**[@mangodxd](https://github.com/mangodxd)** — Type hints cleanup (PR #115)
Added missing type hints across 10 files and corrected 9 inaccurate existing ones — the kind of maintenance work that makes the codebase easier to reason about.

**[@Argonaut790](https://github.com/Argonaut790)** — HTML entity decode + Traditional Chinese locale (PR #239)
Fixed double-escaping of HTML entities in `renderMd()` — LLM output containing `&lt;code&gt;` was being escaped a second time, rendering as literal text instead of the intended markdown. The same PR also completed the Simplified Chinese translation (40+ missing keys) and added a full Traditional Chinese (`zh-Hant`) locale.

**[@indigokarasu](https://github.com/indigokarasu)** — Visual redesign proposal: icon rail + design token system + 7 themes (PR #213)
A CSS-only redesign of the full UI — proper design tokens (`--bg-primary`, `--text-info`, spacing scale), an icon rail sidebar replacing the emoji tab strip, consistent form cards, breadcrumb nav, and 7 built-in themes as custom properties. The PR didn't merge as-is but directly shaped the design language and theme architecture that shipped in v0.50.0.

**[@zenc-cp](https://github.com/zenc-cp)** — Anti-hallucination guard for ReAct loop (PR #133)
Added a streaming token buffer and post-run message scrub to `streaming.py` to detect and strip fake tool execution JSON that weaker models write inline instead of calling tools properly. A three-layer approach: ephemeral anti-hallucination prompt, live token filtering, and session history cleanup. The pattern influenced later streaming.py improvements.

---

Want to contribute? See [ARCHITECTURE.md](ARCHITECTURE.md) for the codebase layout and [TESTING.md](TESTING.md) for how to run the test suite. The best contributions are focused, well-tested, and solve a real problem — exactly what every person on this list did.

## Repo

```
git@github.com:nesquena/hermes-webui.git
```
