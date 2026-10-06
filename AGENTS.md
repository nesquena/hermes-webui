# Agent instructions for Hermes WebUI

Always-loaded entry point. Keep only what applies *before* you know which subsystem
you are in: the router, and universal safety. Anything with a trigger lives behind
that trigger. Personal machine setup, private network details, credentials, and
local-only workflow notes do not belong in this tracked file — use a git-ignored
local note instead.

## Route by task

- **Any change:** `docs/CONTRACTS.md` is the index of contracts, RFCs, and review
  expectations. Start there.
- **Install, reinstall, bootstrap, first-run onboarding, provider or local-model
  server setup, Docker or WSL onboarding, or support for a failed first run:**
  `docs/onboarding-agent-checklist.md` — read it *before* running commands or
  inspecting logs.
- **Runtime, streaming, recovery, replay, compression, or sidebar metadata:** read
  `CODING_STANDARDS.md` for the implementation invariants and
  `docs/rfcs/README.md` for the product semantics. Trigger list in `CODING_STANDARDS.md`.
- **UI or UX** (layout, interaction flow, themes, chat rendering, composer
  chrome): `docs/UIUX-GUIDE.md` and `DESIGN.md`.
- **Review shape, PR format, evidence, bug-class coverage:** `CONTRIBUTING.md`
  and `docs/GUIDELINES.md`.
- **Anything else** — product behavior, setup, usage, architecture, manual test
  plans: `README.md` carries the full docs index under `## Docs`.

## Universal safety

- This app reads and writes real agent state, sessions, workspaces, credentials,
  and cron data. Treat local validation as potentially destructive.
- Fail closed on authority, capability, identity, and containment checks. Unknown
  is not allowed.
- Do not delete or overwrite a real `~/.hermes` directory, and never print API
  keys, OAuth tokens, cookies, full `.env` or `auth.json` files, or password hashes.
- Run pytest through `./scripts/test.sh`, never bare `python3`, `python -m pytest`,
  or `pytest`.
- `CHANGELOG.md` is owned by release commits; put release-note wording in the PR body.

## What you may run

Within the requested scope you may run local tests with disposable fixtures, fix
failures your change caused, and rerun affected tests without asking each time.
That holds only with confirmed isolated state and no live credentials or services.
The runner manages Python dependencies — it is not a network sandbox. Not
authorized without explicit human approval: modifying real state, handling
credentials, restarting services, or exposing the app beyond localhost. If
verification is blocked, report the blocker instead of claiming completion.

Prefer isolated trial state:

```bash
HERMES_HOME=/tmp/hermes-webui-agent-home \
HERMES_WEBUI_STATE_DIR=/tmp/hermes-webui-agent-state \
HERMES_WEBUI_PORT=8789 \
python3 bootstrap.py
```