     1|# Hermes Web UI
     2|
     3|A lightweight, dark-themed browser interface for Hermes.
     4|Full parity with the CLI experience -- everything you can do from a terminal,
     5|you can do from this UI. No build step, no framework, no bundler. Just Python
     6|and vanilla JS.
     7|
     8|Layout: three-panel Claude Co-Work style. Left sidebar for sessions and tools,
     9|center for chat, right for workspace file browsing.
    10|
    11|---
    12|
    13|## Quick start
    14|
    15|```bash
    16|git clone <this-repo> hermes-webui
    17|cd hermes-webui
    18|./start.sh
    19|```
    20|
    21|That is it. The script will:
    22|
    23|1. Locate your Hermes agent checkout automatically.
    24|2. Find (or create) a Python environment with the required dependencies.
    25|3. Start the server.
    26|4. Print the URL (and SSH tunnel command if you are on a remote machine).
    27|
    28|---
    29|
    30|## What start.sh discovers automatically
    31|
    32|| Thing | How it finds it |
    33||---|---|
    34|| Hermes agent dir | `HERMES_WEBUI_AGENT_DIR` env, then `~/.hermes/hermes-agent`, then sibling `../hermes-agent` |
    35|| Python executable | Agent venv first, then `.venv` in this repo, then system `python3` |
    36|| State directory | `HERMES_WEBUI_STATE_DIR` env, then `~/.hermes/webui-mvp` |
    37|| Default workspace | `HERMES_WEBUI_DEFAULT_WORKSPACE` env, then `~/workspace`, then state dir |
    38|| Port | `HERMES_WEBUI_PORT` env or first argument, default `8787` |
    39|
    40|If discovery finds everything, nothing else is required.
    41|
    42|---
    43|
    44|## Overrides (only needed if auto-detection misses)
    45|
    46|```bash
    47|export HERMES_WEBUI_AGENT_DIR=/path/to/hermes-agent
    48|export HERMES_WEBUI_PYTHON=/path/to/python
    49|export HERMES_WEBUI_PORT=9000
    50|./start.sh
    51|```
    52|
    53|Or inline:
    54|
    55|```bash
    56|HERMES_WEBUI_AGENT_DIR=/custom/path ./start.sh 9000
    57|```
    58|
    59|Full list of environment variables:
    60|
    61|| Variable | Default | Description |
    62||---|---|---|
    63|| `HERMES_WEBUI_AGENT_DIR` | auto-discovered | Path to the hermes-agent checkout |
    64|| `HERMES_WEBUI_PYTHON` | auto-discovered | Python executable |
    65|| `HERMES_WEBUI_HOST` | `127.0.0.1` | Bind address |
    66|| `HERMES_WEBUI_PORT` | `8787` | Port |
    67|| `HERMES_WEBUI_STATE_DIR` | `~/.hermes/webui-mvp` | Where sessions and state are stored |
    68|| `HERMES_WEBUI_DEFAULT_WORKSPACE` | `~/workspace` | Default workspace |
    69|| `HERMES_WEBUI_DEFAULT_MODEL` | `openai/gpt-5.4-mini` | Default model |
    70|| `HERMES_HOME` | `~/.hermes` | Base directory for Hermes state (affects all paths above) |
    71|| `HERMES_CONFIG_PATH` | `~/.hermes/config.yaml` | Path to Hermes config file |
    72|
    73|---
    74|
    75|## Accessing from a remote machine
    76|
    77|The server binds to `127.0.0.1` by default (loopback only). If you are running
    78|Hermes on a VPS or remote server, use an SSH tunnel from your local machine:
    79|
    80|```bash
    81|ssh -N -L <local-port>:127.0.0.1:<remote-port> <user>@<server-host>
    82|```
    83|
    84|Example:
    85|
    86|```bash
    87|ssh -N -L 8787:127.0.0.1:8787 user@your.server.com
    88|```
    89|
    90|Then open `http://localhost:8787` in your local browser.
    91|
    92|`start.sh` will print this command for you automatically when it detects you
    93|are running over SSH.
    94|
    95|---
    96|
    97|## Manual launch (without start.sh)
    98|
    99|If you prefer to launch the server directly:
   100|
   101|```bash
   102|cd /path/to/hermes-agent          # or wherever sys.path can find Hermes modules
   103|HERMES_WEBUI_PORT=8787 python /path/to/hermes-webui/server.py
   104|```
   105|
   106|Health check:
   107|
   108|```bash
   109|curl http://127.0.0.1:8787/health
   110|```
   111|
   112|---
   113|
   114|## Running tests
   115|
   116|Tests discover the repo and the Hermes agent dynamically -- no hardcoded paths.
   117|
   118|```bash
   119|cd hermes-webui
   120|python -m pytest tests/ -v
   121|```
   122|
   123|Or using the agent venv explicitly:
   124|
   125|```bash
   126|/path/to/hermes-agent/venv/bin/python -m pytest tests/ -v
   127|```
   128|
   129|Tests run against an isolated server on port 8788 with a separate state directory.
   130|Production data and real cron jobs are never touched.
   131|
   132|---
   133|
   134|## Features
   135|
   136|### Chat and agent
   137|- Streaming responses via SSE (tokens appear as they are generated)
   138|- 10+ models across OpenAI, Anthropic, and other providers; last-used model persists
   139|- Send a message while one is processing -- it queues automatically
   140|- Edit any past user message inline and regenerate from that point
   141|- Retry the last assistant response with one click
   142|- Cancel a running task from the activity bar
   143|- Tool call cards inline -- each shows the tool name, args, and result snippet
   144|- Approval card for dangerous shell commands (allow once / session / always / deny)
   145|- File attachments persist across page reloads
   146|
   147|### Sessions
   148|- Create, rename, delete, search by title and message content
   149|- Grouped by Today / Yesterday / Earlier in the sidebar
   150|- Download as Markdown transcript or full JSON export
   151|- Sessions persist across page reloads and SSH tunnel reconnects
   152|
   153|### Workspace file browser
   154|- Browse directory tree with type icons
   155|- Preview text, code, Markdown (rendered), and images inline
   156|- Edit files in the browser
   157|- Create and delete files
   158|- Right panel is drag-resizable
   159|
   160|### Panels
   161|- **Chat** -- session list, search, new conversation
   162|- **Tasks** -- view, create, edit, run, pause/resume, delete cron jobs
   163|- **Skills** -- list all skills by category, search, preview, create/edit
   164|- **Memory** -- view and edit MEMORY.md and USER.md inline
   165|- **Todos** -- live task list from the current session
   166|- **Spaces** -- add, rename, remove workspaces; quick-switch from topbar
   167|
   168|---
   169|
   170|## Architecture
   171|
   172|```
   173|server.py               HTTP routing shell
   174|api/
   175|  config.py             Discovery + globals (HOST, PORT, SESSIONS, etc.)
   176|  helpers.py            HTTP helpers: j(), bad(), require(), safe_resolve()
   177|  models.py             Session model + CRUD
   178|  workspace.py          File ops: list_dir, read_file_content, workspace helpers
   179|  upload.py             Multipart parser, file upload handler
   180|  streaming.py          SSE engine, run_agent integration, cancel support
   181|static/
   182|  index.html            HTML template
   183|  style.css             All CSS
   184|  ui.js                 DOM helpers, renderMd, tool cards
   185|  workspace.js          File tree, preview, file ops
   186|  sessions.js           Session CRUD, list rendering, search
   187|  messages.js           send(), SSE event handlers, approval, transcript
   188|  panels.js             Cron, skills, memory, workspace, todo, switchPanel
   189|  boot.js               Event wiring + boot IIFE
   190|tests/
   191|  conftest.py           Isolated test server (port 8788, separate HERMES_HOME)
   192|  test_sprint1-10.py    Feature tests per sprint
   193|  test_regressions.py   Permanent regression gate
   194|```
   195|
   196|State lives outside the repo at `~/.hermes/webui-mvp/` by default
   197|(sessions, workspaces, last_workspace). Override with `HERMES_WEBUI_STATE_DIR`.
   198|
   199|---
   200|
   201|## Docs
   202|
   203|- `ROADMAP.md` -- feature roadmap and sprint history
   204|- `ARCHITECTURE.md` -- system design, all API endpoints, implementation notes
   205|- `TESTING.md` -- manual browser test plan and automated coverage reference
   206|- `CHANGELOG.md` -- release notes
   207|- `PORTABILITY.md` -- full portability design spec
   208|
   209|## Repo
   210|
   211|```
   212|git@github.com:<your-username>/hermes-webui.git
   213|```
   214|