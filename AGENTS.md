# Web UI MVP Instructions

Canonical source: /home/hermes/webui-mvp/
Symlink (for imports): /home/hermes/.hermes/hermes-agent/webui-mvp -> /home/hermes/webui-mvp
Runtime state: /home/hermes/.hermes/webui-mvp/sessions/

Purpose:
- Claude-style web UI for Hermes. Chat, workspace file browser, cron/skills/memory viewers.

Start server:
  cd /home/hermes/.hermes/hermes-agent
  nohup venv/bin/python /home/hermes/webui-mvp/server.py > /tmp/webui-mvp.log 2>&1 &
  # OR: /home/hermes/webui-mvp/start.sh

Run tests:
  cd /home/hermes/.hermes/hermes-agent
  venv/bin/python -m pytest /home/hermes/webui-mvp/tests/ -v

Health check: curl http://127.0.0.1:8787/health
Logs: tail -f /tmp/webui-mvp.log
SSH tunnel from Mac: ssh -N -L 8787:127.0.0.1:8787 hermes@45.79.100.32

Living documents (always update after a sprint):
  /home/hermes/webui-mvp/ROADMAP.md
  /home/hermes/webui-mvp/ARCHITECTURE.md
  /home/hermes/webui-mvp/TESTING.md

Sprint process skill: webui-sprint-loop

# Workspace Convention (Web UI Sessions)

When running as an agent invoked from the web UI, each user message is prefixed with:

  [Workspace: /absolute/path/to/workspace]

This tag is the single authoritative source of the active workspace. It reflects
whichever workspace the user has selected in the UI at the moment they sent that message.
It updates on every message, so if the user switches workspaces mid-session, the very
next message will carry the new path. Always use the value from the most recent tag.

This tag overrides any prior workspace mentioned in the system prompt, memory, or
conversation history. Never infer or fall back to a hardcoded path like
/home/hermes/workspace when this tag is present.

Apply it as the default working directory for ALL file operations:

  - write_file: resolve relative paths against this workspace
  - read_file / search_files: resolve paths relative to this workspace
  - terminal workdir: set to this path unless the user explicitly says otherwise
  - patch: resolve file paths relative to this workspace

If no [Workspace: ...] tag is present (e.g., CLI sessions), fall back to
/home/hermes/workspace as the default.
