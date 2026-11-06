# Agent instructions for Hermes WebUI

Shared entry point for AI assistants in this repo. Project-specific, safe to publish: no
personal machine setup, network details, credentials, tokens, or local-only notes. Personal
workflow details: git-ignored local note, not this tracked file.
Pointer architecture: invariants only; mechanics live in the area docs each section points to.

## Documentation by task
`CONTRIBUTING.md` (Before You Start) routes most work: `docs/GUIDELINES.md`,
`docs/CONTRACTS.md`, `docs/UIUX-GUIDE.md`+`DESIGN.md`, `docs/rfcs/README.md`,
`ARCHITECTURE.md`, `TESTING.md`, `docs/onboarding.md`, `docs/runtime-contracts.md`
(Steer/Stop + recovery, before runtime work). Full index: `README.md`.

## Hard working rules
- Read `docs/CONTRACTS.md` + the touched subsystem's contract/RFC before editing.
- Pytest via `./scripts/test.sh` only (never bare `pytest`): repo `.venv`, Python 3.11-3.13;
  `HERMES_WEBUI_TEST_PYTHON` picks the venv base interpreter (never system/Homebrew);
  interpreter errors: see the script.
- No new deps/build tools/frameworks/long-lived processes without justification + rollback
  story (`docs/CONTRACTS.md`).
- Runtime/streaming/recovery/replay/compression/sidebar-metadata changes: name the state
  layer mutated, prove the invariant (`docs/runtime-contracts.md`).
- UI/UX changes: before/after evidence, desktop/narrow/mobile states (`CONTRIBUTING.md`).
- Behavior changes: tests where practical + manual verification (`TESTING.md`).
- Docker `docker_init.bash` edits: exclusion-mirroring rule in `docs/docker.md`.

## Safety gates
- Install/reinstall/bootstrap/first-run/provider/model-server/Docker/WSL or failed-first-run
  → `docs/onboarding-agent-checklist.md` first; isolated `HERMES_HOME` +
  `HERMES_WEBUI_STATE_DIR` for trials (recipe in the checklist) unless asked for real state.
- Never delete/overwrite a real `~/.hermes` without explicit approval; never print API keys,
  OAuth tokens, cookies, full `.env`/`auth.json`, or password hashes; collect non-secret
  evidence before recommending a fix.
- This app reads/writes real agent state, sessions, workspaces, credentials, cron data —
  treat local validation as potentially destructive until state dirs are confirmed.
- Authority/capability/identity/containment checks fail closed (unknown ≠ allowed); input
  validation + cache scoping per `docs/GUIDELINES.md` rules 3 & 5.
- Never authorizes real state changes, credentials, service restarts, or non-localhost
  exposure — explicit human approval; onboarding safety rules still apply.

## Scope and completion
Local-validation scope without per-step approval: `docs/agent-workflow.md`.
Ready for review = `docs/GUIDELINES.md` complete-and-verified evidence (state changes name
the authoritative value + owner, clean up on every exit — fallbacks extend, don't duplicate)
+ `CONTRIBUTING.md` §3 (PR body: actual commands/outcomes/assumptions;
`Contract Routing`/`Contract Change`; docs current; what wasn't verified — mocks ≠ proof of
external behavior).
Blocked → report the blocker, don't claim completion.
