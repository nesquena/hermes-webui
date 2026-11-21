# Per-turn session profile binding (`HERMES_SESSION_PROFILE`)

This document records how a streaming turn decides which Hermes profile it
runs as, and how that identity is shared across concurrent turns in one WebUI
process. It describes shipped behavior and changes no runtime behavior. It was
added because the #8452 review flagged the contract as undocumented: the
resolution, the shared process-env mirror and the legacy fallbacks live across
`api/streaming.py` and `api/profiles.py` with only code comments behind them.

Start here before changing profile resolution for a turn, the Docker container
key derivation, or the process-env mirror.

## Why the identity is resolved per turn

One WebUI process serves several browser tabs, possibly on different profiles.
The streaming worker runs on its own `threading.Thread`, so it does **not**
inherit the HTTP handler thread's request-local profile (`_tls.profile`). Any
lookup that falls back to process-wide state can therefore resolve a
**different tab's** profile. The turn's identity is captured once at setup,
before the agent starts, and carried by the turn.

## Which value wins

For each turn, in order:

1. **`session.profile`** — stamped at `new_session()` time from the client's
   active profile. This is the authoritative owner of the turn: the same value
   feeds `get_hermes_home_for_profile()` for the turn's `HERMES_HOME`.
2. **The profile home's `.env` `HERMES_SESSION_PROFILE`** — used only when the
   session resolves no profile, so a profile's own dotenv identity still
   reaches the agent (the pre-#8452 behavior).
3. **Ambient / unresolved** — the turn binds the captured ambient value while
   a publisher is live, or the current process environment value otherwise.
   If that value is absent, the ContextVar resolves to `""`. Another live
   turn's published profile does not replace the captured ambient value.

A saved chat with **no** `profile` belongs to the **root** profile: its home is
the root Hermes home, and its turn identity is the root one. It must never
inherit the process-wide active profile — that would run a root chat in another
tab's named profile and Docker container while its config still comes from the
root home.

## What the turn publishes

- **ContextVar `gateway.session_context._SESSION_PROFILE`** — bound by
  `_set_turn_session_identity()`. In-process consumers
  (`tools.terminal_tool._current_session_profile`, `get_session_env`) read this
  first.
- **Thread-local agent env** — `_build_agent_thread_env()` sets
  `HERMES_SESSION_PROFILE` (only when the turn resolved one) plus
  `HERMES_SESSION_KEY` / `HERMES_SESSION_ID` / `HERMES_HOME` for that turn.
- **Process-env mirror** — `_publish_turn_session_profile()` writes
  `HERMES_SESSION_PROFILE` into `os.environ` for subprocess children (Docker
  sandbox spawns, background shells), which inherit the process environment at
  fork/exec and cannot inherit ContextVars.

## Overlapping workers: shared ownership, not save/restore

The process-env mirror is a **publisher stack**, not a save/restore pair.
Every turn — including one that resolved no profile — registers a publisher
token and appends `(token, profile_or_None)` to `_PROFILE_MIRROR_PUBLISHERS`.
The first live publisher captures the ambient value once.

On teardown, `_release_turn_session_profile(token)` removes only its own slot
and then resolves the key from the **most recent remaining publisher that
resolved a profile**; profile-less owners never hide a live named turn's value.
If no named publisher remains, the captured ambient is restored exactly as it
was (absent stays absent).

`_reassert_turn_session_profile(token)` repairs the env after a writer that
bypasses the mirror (MCP discovery's dotenv load). It is a no-op for a
released/unknown token and uses the **same** resolver as the release tail, so a
delayed repair can never resurrect an older owner's profile over a newer
sibling's.

A consequence worth stating: mid-turn, the process env reflects the newest live
named publisher, while each turn's ContextVar carries its own identity. That is
why in-process consumers must read the ContextVar first.

## Older Agents

`_set_turn_session_identity()` binds the ContextVar through optional imports.
An Agent build that predates it simply skips that binding and keeps the
process-env fallback (`HERMES_SESSION_PROFILE` from the turn's thread env, then
the shared mirror). The WebUI never fabricates a ContextVar the installed
Agent does not provide.

## Tests

- `tests/test_issue8452_docker_session_profile.py` — profile resolution,
  Docker container key derivation, the publisher stack, overlapping workers
  (named vs unresolved), and the mirror-bypass repair.
- `tests/test_issue1968_mcp_profile_discovery.py` — MCP discovery's dotenv
  publication and the re-assert path.
- `tests/test_issue8490_legacy_root_chat.py` — a legacy chat with no `profile`
  keeps the root identity even while another tab publishes a named profile.
