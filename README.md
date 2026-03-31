# Hermes Web UI

A lightweight, dark-themed browser interface for Hermes.
Full parity with the CLI experience -- everything you can do from a terminal,
you can do from this UI. No build step, no framework, no bundler. Just Python
and vanilla JS.

Layout: three-panel Claude Co-Work style. Left sidebar for sessions and tools,
center for chat, right for workspace file browsing.

---

## Quick start

```bash
git clone <this-repo> hermes-webui
cd hermes-webui
./start.sh
```

That is it. The script will:

1. Locate your Hermes agent checkout automatically.
2. Find (or create) a Python environment with the required dependencies.
3. Start the server.
4. Print the URL (and SSH tunnel command if you are on a remote machine).

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
| `HERMES_HOME` | `~/.hermes` | Base directory for Hermes state (affects all paths above) |
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
HERMES_WEBUI_PORT=8787 python /path/to/hermes-webui/server.py
```

Health check:

```bash
curl http://127.0.0.1:8787/health
```

---

## Running tests

Tests discover the repo and the Hermes agent dynamically -- no hardcoded paths.

```bash
cd hermes-webui
python -m pytest tests/ -v
```

Or using the agent venv explicitly:

```bash
/path/to/hermes-agent/venv/bin/python -m pytest tests/ -v
```

Tests run against an isolated server on port 8788 with a separate state directory.
Production data and real cron jobs are never touched.

---

## Features

### Chat and agent
- Streaming responses via SSE (tokens appear as they are generated)
- 10+ models across OpenAI, Anthropic, and other providers; last-used model persists
- Send a message while one is processing -- it queues automatically
- Edit any past user message inline and regenerate from that point
- Retry the last assistant response with one click
- Cancel a running task from the activity bar
- Tool call cards inline -- each shows the tool name, args, and result snippet
- Approval card for dangerous shell commands (allow once / session / always / deny)
- File attachments persist across page reloads

### Sessions
- Create, rename, delete, search by title and message content
- Grouped by Today / Yesterday / Earlier in the sidebar
- Download as Markdown transcript or full JSON export
- Sessions persist across page reloads and SSH tunnel reconnects

### Workspace file browser
- Browse directory tree with type icons
- Preview text, code, Markdown (rendered), and images inline
- Edit files in the browser
- Create and delete files
- Right panel is drag-resizable

### Panels
- **Chat** -- session list, search, new conversation
- **Tasks** -- view, create, edit, run, pause/resume, delete cron jobs
- **Skills** -- list all skills by category, search, preview, create/edit
- **Memory** -- view and edit MEMORY.md and USER.md inline
- **Todos** -- live task list from the current session
- **Spaces** -- add, rename, remove workspaces; quick-switch from topbar

---

## Architecture

```
server.py               HTTP routing shell
api/
  config.py             Discovery + globals (HOST, PORT, SESSIONS, etc.)
  helpers.py            HTTP helpers: j(), bad(), require(), safe_resolve()
  models.py             Session model + CRUD
  workspace.py          File ops: list_dir, read_file_content, workspace helpers
  upload.py             Multipart parser, file upload handler
  streaming.py          SSE engine, run_agent integration, cancel support
static/
  index.html            HTML template
  style.css             All CSS
  ui.js                 DOM helpers, renderMd, tool cards
  workspace.js          File tree, preview, file ops
  sessions.js           Session CRUD, list rendering, search
  messages.js           send(), SSE event handlers, approval, transcript
  panels.js             Cron, skills, memory, workspace, todo, switchPanel
  boot.js               Event wiring + boot IIFE
tests/
  conftest.py           Isolated test server (port 8788, separate HERMES_HOME)
  test_sprint1-10.py    Feature tests per sprint
  test_regressions.py   Permanent regression gate
```

State lives outside the repo at `~/.hermes/webui-mvp/` by default
(sessions, workspaces, last_workspace). Override with `HERMES_WEBUI_STATE_DIR`.

---

## Docs

- `ROADMAP.md` -- feature roadmap and sprint history
- `ARCHITECTURE.md` -- system design, all API endpoints, implementation notes
- `TESTING.md` -- manual browser test plan and automated coverage reference
- `CHANGELOG.md` -- release notes
- `PORTABILITY.md` -- full portability design spec

## Repo

```
git@github.com:nesquena/hermes-webui.git
```
