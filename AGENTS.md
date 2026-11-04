# Agent instructions for Hermes WebUI

Shared entry point for AI assistants in this repo. Project-specific, safe to publish:
no personal machine setup, network details, credentials, tokens, or local-only notes.

## Documentation by task
Match the change to its doc: `CONTRIBUTING.md` (scope/PR
format/review), `docs/CONTRACTS.md` (subsystem contracts — read before editing),
`docs/GUIDELINES.md` (bug-classes/state ownership/evidence), `ARCHITECTURE.md` (design
constraints), `TESTING.md` (verification commands), `docs/onboarding.md` (first-run),
`docs/rfcs/README.md` (RFCs/state-durability), `docs/UIUX-GUIDE.md`+`DESIGN.md` (UI),
`docs/runtime-contracts.md` (Steer/Stop + recovery, before runtime work). Full index:
`README.md`.

## Onboarding and reinstall support
Install/reinstall/bootstrap/first-run/provider/model-server/Docker/WSL or failed-first-run
work → `docs/onboarding-agent-checklist.md` first. Safety: isolated `HERMES_HOME` +
`HERMES_WEBUI_STATE_DIR` for trials unless asked for real state; never delete/overwrite a real `~/.hermes` without explicit approval; never
print API keys, OAuth tokens, cookies/full `.env`/`auth.json`/password hashes;
collect non-secret evidence before recommending a fix.

## Contribution style
- Read `docs/CONTRACTS.md` + the touched subsystem's contract/RFC before editing.
- Pytest via `./scripts/test.sh` only (never bare `pytest`): repo `.venv`, Python 3.11-3.13,
  dev test deps via the script; `HERMES_WEBUI_TEST_PYTHON` picks the venv base
  interpreter (never system/Homebrew); unsupported-interpreter error →
  rerun through the script.
- Prefer existing Python + vanilla JS; no new deps/build tools/frameworks/long-lived
  processes without justification + rollback story.
- UI/UX changes: before/after evidence + desktop/narrow/mobile states.
- Behavior changes: add/update automated tests where practical; list manual verification.
- Runtime/streaming/recovery/replay/compression/sidebar-metadata changes: name the state
  layer mutated, prove the invariant; full contracts: `docs/runtime-contracts.md`.
- Docker `docker_init.bash`: mirror directory exclusions in both `rsync` and `cp -a`
  paths (`/opt/hermes` may hold restricted-permission subdirs, e.g. `.playwright/`).

## Completion and verification
Ready for review when: scope implemented (call sites + lifecycle covered, unrelated
cleanup absent); behavior changes have regression evidence (bug-fix test fails before /
passes after; affected + neighboring tests); state changes name the authoritative value
+ owner, clean up on every exit (fallbacks extend, don't duplicate); UI before/after at
desktop + narrow + relevant mobile; docs current; PR body per `CONTRIBUTING.md` with
required `Contract Routing`/`Contract Change`, actual commands/outcomes/assumptions,
what wasn't verified (mocks ≠ proof of external behavior).

Within scope: local tests with disposable fixtures, fix change-caused failures, rerun
affected tests — no per-step approval, only with confirmed isolated state, no live
credentials/services (not a network sandbox). Never authorizes real state changes,
credentials, service restarts, or non-localhost exposure — explicit human approval;
onboarding safety rules still apply. Blocked → report the blocker, don't claim completion.

## Local state and secrets
Reads/writes real agent state, sessions, workspaces, credentials, cron data — treat
local validation as potentially destructive until state dirs are confirmed.
Authority/capability/identity/containment checks fail closed when safety can't be
confirmed (unknown is not allowed); validate adversarial inputs at point of use; account
for check-then-use races; scope caches by complete identity so profiles/sessions can't
leak into each other.

Isolated trial state:
```bash
HERMES_HOME=/tmp/hermes-webui-agent-home \
HERMES_WEBUI_STATE_DIR=/tmp/hermes-webui-agent-state \
HERMES_WEBUI_PORT=8789 \
python3 bootstrap.py
```
Personal workflow details: git-ignored local note, not this tracked file.
