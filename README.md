# Hermes Web UI

[Hermes Agent](https://hermes-agent.nousresearch.com/) is a sophisticated autonomous agent that lives on your server, accessed via a terminal or messaging apps, remembers what it learns, and gets more capable the longer it runs.

Hermes WebUI is a lightweight, dark-themed web app interface in your browser for [Hermes Agent](https://hermes-agent.nousresearch.com/).
Full parity with the CLI experience - everything you can do from a terminal,
you can do from this UI. No build step, no framework, no bundler. Just Python
and vanilla JS.

Layout: three-panel Claude-style. Left sidebar for sessions and tools,
center for chat, right for workspace file browsing.

<img width="1392" height="854" alt="Hermes Web UI - full three-panel layout" src="https://github.com/user-attachments/assets/79cd3c0d-3167-42ed-9434-447a742c25c3" />

<table>
  <tr>
    <td align="center" width="50%">
      <img width="680" alt="Hermes Web UI - workspace file browser and chat" src="docs/images/ui-workspace.png" />
      <br /><sub>Workspace file browser with inline preview</sub>
    </td>
    <td align="center" width="50%">
      <img width="680" alt="Hermes Web UI - session projects, tags, and memory" src="docs/images/ui-sessions.png" />
      <br /><sub>Session projects, tags, and tool call cards</sub>
    </td>
  </tr>
</table>

This gives you nearly **1:1 parity with Hermes CLI from a convenient web UI** which you can access securely through an SSH tunnel from your Hermes setup. Single command to start this up, and a single command to SSH tunnel for access on your computer. Every single part of the web UI leverages your existing Hermes agent, existing models, without requiring any setup.

---

## Why Hermes

Most AI tools are **assistants**: you open a window, ask a question, get an answer, close it. The next day, they have forgotten you. You re-explain your stack, your conventions, your preferences -- every single time.

Hermes is an **agent**: it runs on your server, remembers everything, and gets more capable the longer it runs. There is no other open-source, self-hosted tool in this space that combines persistent memory, autonomous scheduling, and multi-modal access (terminal + web + messaging) in one package.

**What makes Hermes different:**

- **Memory that compounds** -- user profile, agent notes, and a skills system that saves reusable procedures; Hermes learns your environment and never has to relearn it
- **Autonomous scheduling** -- cron jobs that fire while you are offline, deliver results to Telegram/Discord/Slack/Signal/email, and run any agent task on any schedule
- **10+ messaging platforms** -- the same agent you use in the terminal is reachable from your phone, no new setup required
- **Provider-agnostic** -- OpenAI, Anthropic, Google, DeepSeek, OpenRouter, and more; switch models without rebuilding your workflow
- **Runs other agents** -- Hermes can spawn Claude Code or Codex as sub-agents for heavy coding tasks and fold the results back into its own memory
- **Fully self-hosted** -- your conversations, your memory, your hardware; nothing leaves your server

**Hermes vs. the field (quick summary):**

| | Claude Code | Codex CLI | OpenCode | Claude.ai | **Hermes** |
|---|---|---|---|---|---|
| Persistent memory | No | No | No | Shallow | **Yes** |
| Scheduled jobs (cron) | No | No | No | No | **Yes** |
| Messaging app access | No | No | No | No | **Yes** |
| Web UI | No | No | No | Yes | **Yes** |
| Provider-agnostic | No | No | Yes | No | **Yes** |
| Self-hosted | Yes | Yes | Yes | No | **Yes** |
| Skills system | No | No | No | No | **Yes** |
| Open source | No | Yes | Yes | No | **Yes** |

For a full breakdown with mental model, framework, and detailed comparison tables, see [docs/hermes.md](docs/hermes.md).

---

## Quick start

First, you need to install and configure [Hermes Agent](https://hermes-agent.nousresearch.com/). Once installed:

```bash
git clone https://github.com/nesquena/hermes-webui.git hermes-webui
cd hermes-webui
./start.sh
```

That is it. The script will:

1. Locate your Hermes agent checkout automatically.
2. Find (or create) a Python environment with the required dependencies.
3. Start the server.
4. Print the URL (and SSH tunnel command if you are on a remote machine).

---

## Docker

**Pre-built images** (amd64 + arm64) are published to GHCR on every release:

```bash
docker pull ghcr.io/nesquena/hermes-webui:latest
docker run -d -p 8787:8787 -v ~/.hermes:/root/.hermes ghcr.io/nesquena/hermes-webui:latest
```

Or run with Docker Compose (recommended):

```bash
docker compose up -d
```

Or build locally:

```bash
docker build -t hermes-webui .
docker run -d -p 8787:8787 -v ~/.hermes:/root/.hermes hermes-webui
```

Open http://localhost:8787 in your browser.

To enable password protection:

```bash
docker run -d -p 8787:8787 -e HERMES_WEBUI_PASSWORD=your-secret -v ~/.hermes:/root/.hermes ghcr.io/nesquena/hermes-webui:latest
```

Session data persists in a named volume (`hermes-data`) across restarts.

> **Note:** By default, Docker Compose binds to `127.0.0.1` (localhost only).
> To expose on a network, change the port to `"8787:8787"` in `docker-compose.yml`
> and set `HERMES_WEBUI_PASSWORD` to enable authentication.

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
Production data and real cron jobs are never touched. Current count: **424 tests**
across 22 test files.

---

## Features

### Chat and agent
- Streaming responses via SSE (tokens appear as they are generated)
- Multi-provider model support -- any Hermes API provider (OpenAI, Anthropic, Google, DeepSeek, Nous Portal, OpenRouter, MiniMax, Z.AI); dynamic model dropdown populated from configured keys
- Send a message while one is processing -- it queues automatically
- Edit any past user message inline and regenerate from that point
- Retry the last assistant response with one click
- Cancel a running task from the activity bar
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

### Sessions
- Create, rename, duplicate, delete, search by title and message content
- Pin/star sessions to the top of the sidebar (gold indicator)
- Archive sessions (hide without deleting, toggle to show)
- Session projects -- named groups with colors for organizing sessions
- Session tags -- add #tag to titles for colored chips and click-to-filter
- Grouped by Today / Yesterday / Earlier in the sidebar
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
- Profile picker in the topbar -- purple chip with dropdown showing all profiles
- Gateway status dots (green = running), model info, skill count per profile
- Profiles management panel -- create, switch, and delete profiles from the sidebar
- Clone config from active profile on create
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

### Settings and configuration
- Settings panel (gear icon) -- default model, default workspace, send key preference
- Send key: Enter (default) or Ctrl/Cmd+Enter
- Show/hide CLI sessions toggle (enabled by default)
- Token usage display toggle (off by default, also via `/usage` command)
- Cron completion alerts -- toast notifications and unread badge on Tasks tab
- Background agent error alerts -- banner when a non-active session encounters an error

### Slash commands
- Type `/` in the composer for autocomplete dropdown
- Built-in: `/help`, `/clear`, `/model <name>`, `/workspace <name>`, `/new`, `/usage`
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
server.py               HTTP routing shell + auth middleware (~81 lines)
api/
  auth.py               Optional password authentication, signed cookies (~149 lines)
  config.py             Discovery, globals, model detection, reloadable config (~702 lines)
  helpers.py            HTTP helpers, security headers (~71 lines)
  models.py             Session model + CRUD (~146 lines)
  profiles.py           Profile state management, hermes_cli wrapper (~366 lines)
  routes.py             All GET + POST route handlers (~1180 lines)
  streaming.py          SSE engine, run_agent, cancel support (~272 lines)
  upload.py             Multipart parser, file upload handler (~78 lines)
  workspace.py          File ops, workspace helpers (~245 lines)
static/
  index.html            HTML template (~364 lines)
  style.css             All CSS incl. mobile responsive (~670 lines)
  ui.js                 DOM helpers, renderMd, tool cards, file tree (~1002 lines)
  workspace.js          File preview, file ops (~191 lines)
  sessions.js           Session CRUD, list rendering, search (~556 lines)
  messages.js           send(), SSE handlers, approval, transcript (~337 lines)
  panels.js             Cron, skills, memory, profiles, settings (~1030 lines)
  commands.js           Slash command autocomplete (~156 lines)
  boot.js               Mobile nav, voice input, boot IIFE (~338 lines)
tests/
  conftest.py           Isolated test server (port 8788)
  test_sprint{1-23}.py  22 test files, 426 test functions
  test_regressions.py   Permanent regression gate (23 tests)
Dockerfile              python:3.12-slim container image
docker-compose.yml      Compose with named volume and optional auth
.github/workflows/      CI: multi-arch Docker build + GitHub Release on tag
```

State lives outside the repo at `~/.hermes/webui-mvp/` by default
(sessions, workspaces, settings, projects, last_workspace). Override with `HERMES_WEBUI_STATE_DIR`.

---

## Docs

- `docs/hermes.md` -- why Hermes, mental model, and detailed comparison to Claude Code / Codex / OpenCode
- `ROADMAP.md` -- feature roadmap and sprint history
- `ARCHITECTURE.md` -- system design, all API endpoints, implementation notes
- `TESTING.md` -- manual browser test plan and automated coverage reference
- `CHANGELOG.md` -- release notes per sprint
- `SPRINTS.md` -- forward sprint plan with CLI + Claude parity targets

## Repo

```
git@github.com:nesquena/hermes-webui.git
```
