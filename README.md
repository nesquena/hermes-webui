# Hermes Co-Work Web UI

A lightweight, dark-themed web UI for working with Hermes in the browser. The goal is CLI parity with a clean Claude Co-Work style layout: sidebar sessions, a central chat area, and a workspace file browser.

## What this repo contains

- `server.py`: the main Python HTTP server and API handlers
- `static/`: browser assets for the UI (`index.html`, `style.css`, `app.js`)
- `tests/`: browser and API tests
- `ARCHITECTURE.md`: current system design and implementation notes
- `ROADMAP.md`: feature roadmap and sprint history
- `TESTING.md`: manual browser test plan
- `archive/`: organized backups, screenshots, and superseded drafts

## Run locally

From the Hermes environment, start the web UI with:

```bash
./start.sh
```

The app listens on the local Hermes port configured in the server, typically `127.0.0.1:8787`.

## Development notes

- This repo is intentionally simple and file-based, with no frontend build step.
- Keep `ARCHITECTURE.md`, `ROADMAP.md`, and `TESTING.md` updated when behavior changes.
- Prefer organizing old backups and screenshots under `archive/` instead of leaving them in the repo root.

## Git

The repository remote is configured for SSH:

```bash
git@github.com:nesquena/hermes-webui.git
```
