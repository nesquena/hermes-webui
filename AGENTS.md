# Web UI MVP Instructions

Canonical source: <repo>/
Symlink (for imports): <agent-dir>/webui-mvp -> <repo>
Runtime state: ~/.hermes/webui-mvp/sessions/

Purpose:
- Claude-style web UI for Hermes. Chat, workspace file browser, cron/skills/memory viewers.

Start server:
  cd <agent-dir>
  nohup venv/bin/python <repo>/server.py > /tmp/webui-mvp.log 2>&1 &
  # OR: <repo>/start.sh

Run tests:
  cd <agent-dir>
  venv/bin/python -m pytest <repo>/tests/ -v

Health check: curl http://127.0.0.1:8787/health
Logs: tail -f /tmp/webui-mvp.log
SSH tunnel from Mac: ssh -N -L 8787:127.0.0.1:8787 <user>@<your-server>

Living documents (always update after a sprint):
  <repo>/ROADMAP.md
  <repo>/ARCHITECTURE.md
  <repo>/TESTING.md

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
~/workspace when this tag is present.

Apply it as the default working directory for ALL file operations:

  - write_file: resolve relative paths against this workspace
  - read_file / search_files: resolve paths relative to this workspace
  - terminal workdir: set to this path unless the user explicitly says otherwise
  - patch: resolve file paths relative to this workspace

If no [Workspace: ...] tag is present (e.g., CLI sessions), fall back to
~/workspace as the default.

# UI / Design System

For any UI, component, or styling work (HTML/CSS/JS in `static/`, templates,
new pages, visual tweaks), follow the design system in `@DESIGN.md` (Genesis
by chef, sourced from designmd.ai). Use its color tokens, typography scale,
spacing, and component conventions instead of inventing new ones. Update
`DESIGN.md` itself if a new pattern becomes canonical.
