# Hermes Web UI — AGENTS.md

Instructions for agents working on the Hermes Web UI MVP.

The pre-simplification file is preserved at `agent-runbooks/agents-full-pre-simplification.md`.

## Purpose

Claude-style web UI for Hermes: chat, workspace file browser, cron, skills, and memory viewers.

## Paths

| Item | Path |
|---|---|
| Canonical source | this repo (`/home/adam/hermes-webui`) |
| Import symlink | `<agent-dir>/webui-mvp -> <repo>` |
| Runtime sessions | `~/.hermes/webui-mvp/sessions/` |

## Start / Test / Debug

Start server:

```bash
cd <agent-dir>
<repo>/start.sh
# or:
nohup venv/bin/python <repo>/server.py > /tmp/webui-mvp.log 2>&1 &
```

Run tests and health checks:

```bash
cd <agent-dir>
venv/bin/python -m pytest <repo>/tests/ -v
curl http://127.0.0.1:8787/health
tail -f /tmp/webui-mvp.log
```

SSH tunnel from Mac:

```bash
ssh -N -L 8787:127.0.0.1:8787 <user>@<your-server>
```

## Sprint Rule

After a sprint, update:

- `<repo>/ROADMAP.md`
- `<repo>/ARCHITECTURE.md`
- `<repo>/TESTING.md`

Sprint process skill: `webui-sprint-loop`.

## Workspace Convention

Web UI sessions prefix user messages with:

```text
[Workspace: /absolute/path/to/workspace]
```

Rules:

1. The most recent `[Workspace: ...]` tag is authoritative.
2. It overrides system prompt, memory, and conversation history.
3. Use it as the default working directory for all file operations:
   - `write_file`: resolve relative paths against this workspace.
   - `read_file` / `search_files`: resolve paths relative to this workspace.
   - `terminal`: set `workdir` to this path unless the user explicitly says otherwise.
   - `patch`: resolve file paths relative to this workspace.
4. If no workspace tag is present, fall back to `~/workspace`.

## Before Editing This File

Keep this file short. Put detailed architecture, sprint history, and long procedures in project docs.
