     1|# Web UI MVP Instructions
     2|
     3|Canonical source: <repo>/
     4|Symlink (for imports): <agent-dir>/webui-mvp -> <repo>
     5|Runtime state: ~/.hermes/webui-mvp/sessions/
     6|
     7|Purpose:
     8|- Claude-style web UI for Hermes. Chat, workspace file browser, cron/skills/memory viewers.
     9|
    10|Start server:
    11|  cd <agent-dir>
    12|  nohup venv/bin/python <repo>/server.py > /tmp/webui-mvp.log 2>&1 &
    13|  # OR: <repo>/start.sh
    14|
    15|Run tests:
    16|  cd <agent-dir>
    17|  venv/bin/python -m pytest <repo>/tests/ -v
    18|
    19|Health check: curl http://127.0.0.1:8787/health
    20|Logs: tail -f /tmp/webui-mvp.log
    21|SSH tunnel from Mac: ssh -N -L 8787:127.0.0.1:8787 <user>@<your-server>
    22|
    23|Living documents (always update after a sprint):
    24|  <repo>/ROADMAP.md
    25|  <repo>/ARCHITECTURE.md
    26|  <repo>/TESTING.md
    27|
    28|Sprint process skill: webui-sprint-loop
    29|
    30|# Workspace Convention (Web UI Sessions)
    31|
    32|When running as an agent invoked from the web UI, each user message is prefixed with:
    33|
    34|  [Workspace: /absolute/path/to/workspace]
    35|
    36|This tag is the single authoritative source of the active workspace. It reflects
    37|whichever workspace the user has selected in the UI at the moment they sent that message.
    38|It updates on every message, so if the user switches workspaces mid-session, the very
    39|next message will carry the new path. Always use the value from the most recent tag.
    40|
    41|This tag overrides any prior workspace mentioned in the system prompt, memory, or
    42|conversation history. Never infer or fall back to a hardcoded path like
    43|~/workspace when this tag is present.
    44|
    45|Apply it as the default working directory for ALL file operations:
    46|
    47|  - write_file: resolve relative paths against this workspace
    48|  - read_file / search_files: resolve paths relative to this workspace
    49|  - terminal workdir: set to this path unless the user explicitly says otherwise
    50|  - patch: resolve file paths relative to this workspace
    51|
    52|If no [Workspace: ...] tag is present (e.g., CLI sessions), fall back to
    53|~/workspace as the default.
    54|