# Codex session bridge (read-only)

The WebUI can show conversations from a local OpenAI **Codex CLI** in the sidebar,
read-only, mirroring the existing Claude Code session bridge. This lets anyone
who works in Codex alongside Claude Code review, search, and reopen their CLI
conversations from the browser.

The bridge is **additive and defensive**: it opens nothing outside the Codex
home directory, and it disables cleanly when Codex is absent or the state store
is missing.

- [Discovery](#discovery)
- [Enabling / disabling](#enabling--disabling)
- [Environment](#environment)
- [Docker](#docker)
- [Long conversations and truncation](#long-conversations-and-truncation)
- [Security](#security)
- [Troubleshooting](#troubleshooting)

## Discovery

Codex threads are read from two places under the Codex home (default
`~/.codex`):

- **`state_5.sqlite`** — the `threads` table, which holds each thread's id,
  rollout path, title, model, workspace (`cwd`), and timestamps.
- **`sessions/YYYY/MM/DD/rollout-<ts>-<uuid>.jsonl`** — the underlying JSONL
  transcript for each thread.

Each thread appears in the sidebar as a row stamped with the `codex` source
badge and labelled **read-only** (you can view, search, and copy, but not
replay or reply from the WebUI). Clicking a row opens the transcript.

## Enabling / disabling

- **Settings → Show Codex sessions** — on by default. Turning it off hides
  Codex rows from the sidebar without touching anything on disk.
- Disabled entirely (even without the setting) when Codex is not installed or
  `state_5.sqlite` is missing — the bridge is a no-op in that case.

## Environment

| Variable | Purpose |
|----------|---------|
| `HERMES_WEBUI_CODEX_HOME` | Override the Codex home directory (default `~/.codex`). Useful when Codex keeps its state elsewhere. |

When neither an explicit home nor `HERMES_WEBUI_CODEX_HOME` is set _and_ the
process is running under `HERMES_WEBUI_TEST_STATE_DIR`, discovery is disabled so
tests never scan a developer's real `~/.codex`.

## Docker

The default Compose deployment adds a **read-only** bind mount of the Codex
home so the bridge works inside the container:

```yaml
volumes:
  - ${CODEX_HOME:-${HOME}/.codex}:${CODEX_HOME:-${HOME}/.codex}:ro
```

with an environment override so the container resolves the same path:

```yaml
environment:
  - HERMES_WEBUI_CODEX_HOME=${CODEX_HOME:-${HOME}/.codex}
```

The mount is read-only (`:ro`) — the WebUI never writes back into Codex state.
To point the bridge at a non-default location, set `CODEX_HOME` (or
`HERMES_WEBUI_CODEX_HOME`) to the absolute path you want mounted.

## Long conversations and truncation

Codex transcripts grow **newest-last** (each turn is appended). To keep a
recently-finished conversation's latest turns — the ones a reader actually
wants — the bridge renders only the **newest** 1000 messages per transcript.

A transcript is **never dropped for being large**. When a rollout exceeds the
tail-read threshold the parser reads only its tail (the newest records) instead
of the whole file, so a long-lived conversation stays visible in the sidebar
and the viewer however large it grows. The dropped prefix is always the oldest
turns.

Whether older turns were omitted is surfaced as a `truncated` boolean:

- on `GET /api/codex/session/<id>`, and
- on the **real viewer path** the WebUI actually uses to open a Codex session
  (`POST /api/session/import_cli` → `GET /api/session`) as a
  `cli_transcript_truncated` flag on the session payload.

The latter lets the transcript show an "earlier turns omitted" notice rather
than letting the reader believe the conversation starts where the window does.
There is no back-fill path for the omitted oldest turns — they are not stored
in the WebUI sidecar — so the notice is informational only.

The sidebar's per-row message count is derived from a small **bounded tail
read** (a few KiB per file), not a full parse, so a cold sidebar scan stays
bounded regardless of how large individual transcripts have grown. The count is
therefore a lower bound for very long sessions; opening the session still loads
the full newest-1000 window.

This intentionally mirrors the existing Claude Code bridge's
`CLAUDE_CODE_MAX_MESSAGES_PER_FILE` cap; the differing behaviour here is that
Codex keeps the **tail** (newest) rather than the head.

## Security

- The thread id must be a well-formed `codex_<uuid>` for the detail endpoint to
  run; anything else is rejected before it reaches SQLite.
- A thread's `rollout_path` from the DB is resolved and **rejected unless it
  lands under `<codex_home>/sessions`**, so a tampered database row cannot be
  used to read an arbitrary file.
- In Docker the Codex home is mounted read-only.

## Troubleshooting

- **No Codex rows in the sidebar** — confirm Codex is installed, `~/.codex/state_5.sqlite` exists, and **Settings → Show Codex sessions** is enabled. If running in Docker, verify the `CODEX_HOME` mount is present (`docker compose config` shows it) and `HERMES_WEBUI_CODEX_HOME` matches the mounted path.
- **Session shows an old or incomplete conversation** — the bridge renders the newest 1000 turns (and tail-reads very large files rather than dropping them); check whether `GET /api/codex/session/<id>` returns `"truncated": true`, or whether the open session shows an "earlier turns omitted" notice (both mean the conversation has more turns than the retained window). If it looks genuinely stale, touch the transcript and re-open the session — the parse cache invalidates on file mtime/size change.
- **"Session not found"** — the id is not a Codex thread the WebUI can see (wrong home, missing DB, or the rollout path is outside `<codex_home>/sessions`). Confirm `HERMES_WEBUI_CODEX_HOME` if you moved Codex state.
