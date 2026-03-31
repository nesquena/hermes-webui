# Hermes Cowork Web UI

A lightweight, dark-themed browser interface for Hermes. Full parity with the CLI
experience -- everything you can do from a terminal, you can do from this UI.
No build step, no framework, no bundler. Just Python and vanilla JS.

Layout: three-panel Claude Cowork style. Left sidebar for sessions and tools, center
for chat, right for workspace file browsing.

---

## Features

### Chat and agent
- Streaming responses via SSE (tokens appear as they're generated)
- 10 models across OpenAI, Anthropic, and other providers; last-used model persists
- Send a message while one is processing -- it queues automatically
- Edit any past user message inline and regenerate from that point
- Retry the last assistant response with one click
- Cancel a running task from the activity bar
- Clear a conversation without deleting the session
- Tool call cards inline in the conversation -- each shows the tool name, args, and result snippet, grouped by the assistant turn that called them, with expand/collapse
- Approval card for dangerous shell commands (allow once / session / always / deny)
- File attachments persist across page reloads

### Sessions
- Create, rename (double-click), delete, search by title and message content
- Grouped by Today / Yesterday / Earlier in the sidebar
- Download as Markdown transcript or full JSON export
- Sessions persist across page reloads and SSH tunnel reconnects
- Reconnect banner if page reloaded mid-stream

### Workspace file browser
- Browse directory tree with type icons
- Preview text, code, Markdown (rendered), and images inline
- Edit files in the browser (Edit / Save / Escape to cancel)
- Create and delete files
- Right panel is drag-resizable

### Panels (sidebar tabs)
- **Chat** -- session list, search, new conversation
- **Tasks** -- view, create, edit, run, pause/resume, delete cron jobs; full run history per job expandable
- **Skills** -- list all skills by category, search, preview SKILL.md, create/edit skills from the UI
- **Memory** -- view and edit MEMORY.md and USER.md inline
- **Todos** -- live task list from the current session, parsed from agent history
- **Spaces** -- add, rename, remove workspaces; quick-switch from topbar dropdown

### Syntax highlighting
- Prism.js (deferred CDN load) -- Python, JS, bash, JSON, SQL and more via autoloader

---

## Architecture

```
server.py               HTTP routing shell (~680 lines)
api/
  config.py             Globals: HOST, PORT, SESSIONS, STREAMS, etc.
  helpers.py            HTTP helpers: j(), bad(), require(), safe_resolve()
  models.py             Session model + CRUD (get_session, new_session, all_sessions)
  workspace.py          File ops: list_dir, read_file_content, workspace helpers
  upload.py             Multipart parser, file upload handler
  streaming.py          SSE engine, _run_agent_streaming, cancel support
static/
  index.html            HTML template (served from disk)
  style.css             All CSS
  ui.js                 DOM helpers, renderMd, renderMessages, tool cards, edit/regenerate
  workspace.js          File tree, preview, file ops
  sessions.js           Session CRUD, list rendering, search
  messages.js           send(), SSE event handlers, approval, transcript
  panels.js             Cron, skills, memory, workspace, todo, switchPanel
  boot.js               Event wiring + boot IIFE
tests/
  conftest.py           Isolated test server (port 8788, separate HERMES_HOME)
  test_sprint1.py       ... test_sprint10.py -- feature tests per sprint
  test_regressions.py   Permanent regression gate -- one test per introduced bug
```

State lives outside the repo at `~/.hermes/webui-mvp/` (sessions, workspaces, last_workspace).

---

## Run

```bash
# Start (from Hermes environment)
./start.sh

# Or manually
cd /home/hermes/.hermes/hermes-agent
venv/bin/python /home/hermes/webui-mvp/server.py
```

Health check: `curl http://127.0.0.1:8787/health`

## SSH tunnel (Mac -> VPS)

```bash
ssh -N -L 8787:127.0.0.1:8787 hermes@<SERVER_IP>
```

Then open `http://localhost:8787` in your browser.

## Tests

```bash
cd /home/hermes/.hermes/hermes-agent
venv/bin/python -m pytest /home/hermes/webui-mvp/tests/ -v
```

Tests run against an isolated server on port 8788 with a separate state directory.
Production data and real cron jobs are never touched.

---

## Docs

- `ROADMAP.md` -- feature roadmap, sprint history, what is and isn't built
- `ARCHITECTURE.md` -- system design, all API endpoints, implementation notes, known pitfalls
- `TESTING.md` -- manual browser test plan, automated coverage reference
- `CHANGELOG.md` -- release notes from v0.1 through current

## Repo

```
git@github.com:nesquena/hermes-webui.git
```
