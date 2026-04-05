# Hermes Web UI — Agent Instructions

This file is loaded automatically when an agent opens this workspace. It should give
you everything you need to orient quickly and know where to look for more.

---

## What this repo is

A Claude-style browser UI for the Hermes agent. Three panels: session sidebar, chat
area, workspace file browser. Zero build step — Python HTTP server, seven vanilla JS
modules, no framework. The public repo is at github.com/nesquena/hermes-webui.

---

## Current state (update this block after every release)

    Public version:  v0.35
    Tests:           433 passing, 0 failures
    Private version: v0.35 · private (synced)
    Last updated:    April 5, 2026

---

## Repo layout

    server.py              Thin HTTP handler + auth middleware. Delegates to api/routes.py.
    start.sh               Discovery: finds agent dir, Python venv, starts server.
    Dockerfile / compose   Container support.

    api/
      auth.py              Password auth, signed session cookies, PBKDF2 hashing.
      config.py            Discovery, globals, settings load/save, model resolution.
      helpers.py           j(), bad(), require(), safe_resolve_ws(), security headers.
      models.py            Session model + CRUD.
      profiles.py          Profile state, hermes_cli wrapper.
      routes.py            All GET/POST route handlers (~1200 lines).
      state_sync.py        Opt-in state.db bridge for /insights sync.
      streaming.py         SSE engine, _run_agent_streaming, _ENV_LOCK, cancel.
      upload.py            Multipart file upload with path traversal guard.
      workspace.py         list_dir, read_file_content, workspace helpers.

    static/
      index.html           HTML shell (~364 lines). Theme picker, settings panel.
      style.css            All CSS + theme variable blocks (~670 lines).
      ui.js                DOM helpers, renderMd(), tool cards, file tree (~977 lines).
      workspace.js         File preview, loadDir, clearPreview (~185 lines).
      sessions.js          Session CRUD, list rendering, search, overlay actions (~533 lines).
      messages.js          send(), SSE handlers, approval card, transcript (~297 lines).
      panels.js            Cron, skills, memory, profiles, todo, settings (~974 lines).
                           Contains: toggleSettings(), _closeSettingsPanel(), saveSettings(),
                           _settingsDirty guard, loadSettingsPanel().
      commands.js          /slash command registry, parser, autocomplete dropdown (~156 lines).
      boot.js              Event wiring, Escape/Keydown handlers, voice input, boot (~338 lines).

    tests/
      conftest.py          Isolated test server on port 8788, separate HERMES_HOME.
      test_sprint{1-26}.py One file per sprint, ~433 tests total.
      test_regressions.py  Permanent regression gate.

---

## Servers

    Public:  port 8786  state: ~/.hermes/webui-public
    Private: port 8787  state: ~/.hermes/webui-mvp
    Tests:   port 8788  (conftest.py — never start manually, never tunnel)

Start public:
    cd /home/hermes/.hermes/hermes-agent
    HERMES_WEBUI_PORT=8786 HERMES_WEBUI_STATE_DIR=~/.hermes/webui-public \
      venv/bin/python /home/hermes/hermes-webui-public/server.py \
      > /tmp/hermes-webui-8786.log 2>&1 &

Start private:
    cd /home/hermes/.hermes/hermes-agent
    HERMES_WEBUI_PORT=8787 HERMES_WEBUI_STATE_DIR=~/.hermes/webui-mvp \
      venv/bin/python /home/hermes/webui-mvp/server.py \
      > /tmp/hermes-webui-8787.log 2>&1 &

Health check:  curl http://127.0.0.1:8786/health
SSH tunnel:    ssh -N -L 8786:127.0.0.1:8786 -L 8787:127.0.0.1:8787 hermes@<server>

CRITICAL: always pass HERMES_WEBUI_STATE_DIR explicitly. Both servers default to the
same directory if omitted, which causes them to share state.

---

## Run tests

    cd /home/hermes/hermes-webui-public  # or webui-mvp for private
    kill $(lsof -ti:8788) 2>/dev/null; sleep 1
    /home/hermes/.hermes/hermes-agent/venv/bin/python -m pytest tests/ -q --tb=short

Kill port 8788 first -- conftest starts its own server. A leftover process causes false
failures that look like real test failures but aren't.

---

## Git + GitHub

    Public repo:  ~/hermes-webui-public  ->  git@github.com:nesquena/hermes-webui.git
    Private repo: ~/webui-mvp            ->  git@github.com:nesquena-hermes/hermes-webui-private.git
    gh CLI:       ~/.local/bin/gh  (export PATH=$HOME/.local/bin:$PATH before use)
    Auth:         nesquena-hermes account

HARD RULE: never push directly to public master. All changes go through a named
branch + PR, even one-liners. After any sync involving a public remote:
    git remote remove public

---

## PR review flow (summary)

Full workflow is in the `webui-public-pr-review` skill. The nine steps:

1. gh pr view N --json ... (read body and file list)
2. git diff origin/master..origin/<branch> (read every line)
3. git checkout -b pr-N-review origin/<branch> + pytest + security scan (parallel)
4. Fix any issues on the branch, push
5. Post review comment via --body-file, then gh pr merge --squash --delete-branch
6. Pull master + final pytest
7. Docs PR: bump CHANGELOG.md + static/index.html -> merge -> git tag -> push -> CI
8. Delete local branches + restart 8786 from master
9. Update memory entry with new version/commit/test count

Version rule: feature sprint or security fix = minor bump (v0.34 -> v0.35).
Bug fix or polish = patch (v0.35 -> v0.35.1). Docs-only = no bump, no tag.

---

## Theme system (as of v0.35)

The CSS is entirely CSS-variable-driven. Six built-in themes: Dark (default), Slate,
Light, Solarized Dark, Monokai, Nord. Each theme block defines three variable groups:

  Group 1 — Core palette:  --bg, --sidebar, --border, --border2, --text, --muted,
                            --accent, --blue, --gold, --code-bg
  Group 2 — Surfaces:      --surface, --topbar-bg, --main-bg, --input-bg, --hover-bg,
                            --focus-ring, --focus-glow
  Group 3 — Typography:    --strong, --em, --code-text, --code-inline-bg, --pre-text

Light themes also need ~46 scoped selector overrides replacing rgba(255,255,255,.XX)
with rgba(0,0,0,.XX) equivalents. See the `:root[data-theme="light"]` block in style.css.

Switching themes: Settings gear > Theme dropdown (live preview), or /theme <name>.
Theme persists server-side in settings.json and client-side in localStorage (flicker-free).

See THEMES.md for the full custom theme guide and variable reference.

---

## Security posture (as of v0.35)

All four audit findings from PR #108 are fixed:
- ENV race: global _ENV_LOCK in streaming.py serializes os.environ writes across sessions
- Signing key: random 32-byte key in STATE_DIR/.signing_key (chmod 600), not STATE_DIR hash
- Upload traversal: dot-only filenames rejected; safe_resolve_ws() sandbox enforced
- Password hash: PBKDF2-SHA256, 600k iterations, signing key as salt (stdlib only)

Standard security checks to run on every PR diff (from webui-public-pr-review skill):
- eval/exec/atob/fromCharCode patterns
- path traversal sandbox (relative_to check in _serve_static)
- SRI hashes intact (3 in index.html)
- logger crash pattern in api/config.py
- requirements.txt: should only have pyyaml>=6.0

---

## Private repo differences (webui-mvp)

The private repo shares all code with public except:

  api/config.py   STATE_DIR defaults to ~/.hermes/webui-mvp (not webui)
                  sys.path.insert(0,...) instead of append
                  verify_hermes_imports() returns 2-tuple (ok, missing)
                  _SETTINGS_DEFAULTS includes theme, show_token_usage, show_cli_sessions,
                  sync_to_insights (must be kept in sync manually on each public sync)
  server.py       Uses 2-tuple verify_hermes_imports() return
  start.sh        STATE_DIR default is webui-mvp
  static/index.html  Sidebar label says "v0.35 · private"

When syncing public -> private:
  git remote add public git@github.com:nesquena/hermes-webui.git
  git fetch public
  git checkout -b sync-public-vX.Y master
  git checkout public/master -- <files>   # do NOT overwrite: api/config.py, server.py, start.sh, README.md, static/index.html
  # Manually patch api/config.py: restore private defaults, add any new _SETTINGS_DEFAULTS keys
  # Manually patch static/index.html: set "vX.Y · private"
  pytest tests/  # must pass 433
  git add -A && git commit -m "sync: public vX.Y -> private"
  git checkout master && git merge sync-public-vX.Y --no-ff
  git push origin master && git remote remove public

---

## Markdown documents index

Read these on-demand, not all at once. All are in the repo root.

  CHANGELOG.md    Release notes per version. Check top entry for current version/test count.
  ROADMAP.md      Feature checklist and sprint history table. Last updated: v0.35.
  SPRINTS.md      Forward sprint plan. Sprint 26 COMPLETED. Next: Sprint 24. Horizon: Sprint 25.
  TESTING.md      Manual browser test plan + automated test coverage. Currently Sprint 26 / v0.35.
  THEMES.md       Custom theme guide, full CSS variable contract, built-in theme descriptions.
  ARCHITECTURE.md Full developer guide: file inventory, design decisions, API patterns.
  BUGS.md         Bug backlog. Currently empty (no open bugs).
  README.md       Public-facing: setup, install, Docker, SSH tunnel, mobile access.
  HERMES.md       Why Hermes doc: comparison with OpenClaw, Claude Code, Codex, etc.

---

## Sprint planning

New sprints use a Tracks A/B/C structure in SPRINTS.md:
  Track A: Core implementation (CSS/JS/Python with code examples)
  Track B: UI/UX wiring (settings panel, slash commands, picker)
  Track C: Tests (specific test cases for test_sprintN.py)

Each entry includes: difficulty, estimated effort, estimated new tests, target total,
Hermes CLI parity impact, Claude parity impact, user-facing value.

After a sprint ships: mark header (COMPLETED), bump SPRINTS.md footer version + horizon,
update ROADMAP.md feature checklist [x] and sprint history table row.

---

## Workspace convention

When invoked from the web UI, each user message is prefixed with:

  [Workspace: /absolute/path/to/workspace]

This is the authoritative working directory. Use it for all file operations.
Overrides any prior workspace in system prompt or memory.
If absent (CLI session), fall back to ~/workspace.
