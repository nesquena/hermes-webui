# Matrix Session Management Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Hide noisy Matrix sessions by default, preserve an explicit Matrix filter, organize imported Matrix rows through WebUI-owned metadata, and provide a persistent sidebar multiselect dock.

**Architecture:** Extend the existing session-list source/filter pipeline with a `show_matrix_sessions` setting and a Matrix-specific default-hide predicate. Add a narrow imported-Matrix organization mutation path that writes a minimal WebUI sidecar without touching Hermes Agent `state.db`. Move the existing batch-selection UI out of the scrollable list into a dedicated dock, preserving current endpoint behavior for writable sessions and adding imported-Matrix eligibility only where the backend contract permits it.

**Tech Stack:** Python stdlib HTTP handlers and JSON sidecars; vanilla JavaScript/CSS; pytest through `./scripts/test.sh`; existing Node syntax/runtime checks and Playwright browser smoke coverage.

## Global Constraints

- Do not mutate, migrate, or add columns to Hermes Agent `state.db`.
- Imported Matrix transcripts remain read-only; only WebUI organization metadata is writable.
- `show_matrix_sessions` defaults to `False` and is subordinate to `show_cli_sessions`.
- An explicit `source_filter=matrix` reveals Matrix rows regardless of the default-hide setting.
- Preserve existing CLI, Cron, Webhook, Kanban, subagent, profile, archive, and project authorization contracts.
- Use `./scripts/test.sh` for pytest; do not install test dependencies into a system interpreter.
- Keep the app dependency-free and compatible with the existing Python + vanilla JavaScript structure.
- Every behavior change gets a failing regression test before implementation code.

---

### Task 1: Add the Matrix sidebar visibility contract

**Files:**
- Modify: `api/config.py` settings defaults and allowed setting keys near the existing `show_cron_sessions`, `show_webhook_sessions`, and `show_kanban_sessions` entries.
- Modify: `api/models.py` `_hide_from_default_sidebar()` and background-session helpers.
- Modify: `api/routes.py` `_session_list_cache_key()`, `_build_session_list_cache_payload()`, `_dedupe_cli_sidebar_sessions_for_api()`, `/api/sessions`, and settings-save plumbing.
- Modify: `api/route_session_list_cache.py` cache-key helper arguments and tuple composition to include `show_matrix_sessions`.
- Create: `tests/test_matrix_session_visibility.py`.

**Interfaces:**
- Consumes: normalized external rows with `source`, `raw_source`, `source_tag`, `session_source`, and `source_label` fields.
- Produces: `show_matrix_sessions: bool` in persisted settings and the `/api/sessions` response settings block; cache keys that distinguish Matrix visibility; a source-filter override for `matrix`.

- [ ] **Step 1: Write failing backend tests for the source predicate and default filtering.**

  Add tests that call the production helpers with rows shaped like:

  ```python
  matrix = {
      "session_id": "matrix-room-1",
      "source": "matrix",
      "raw_source": "matrix",
      "session_source": "messaging",
      "message_count": 3,
      "project_id": None,
  }
  ```

  Assert that Matrix is hidden when `show_matrix_sessions=False`, visible when
  it is `True`, and visible when `source_filter="matrix"` even if the setting
  is false. Include a non-Matrix messaging row and assert it is unaffected.

- [ ] **Step 2: Run the new tests and verify the intended RED failure.**

  Run:

  ```bash
  ./scripts/test.sh tests/test_matrix_session_visibility.py -q
  ```

  Expected: collection succeeds and the new assertions fail because Matrix is
  not yet a recognized hidden source and the new setting is not threaded.

- [ ] **Step 3: Implement the minimal setting and backend wiring.**

  Add `show_matrix_sessions=False` beside the other source visibility settings.
  Thread it through every existing cache-key and builder call. Extend the
  default-hide helper with `show_matrix`, normalize the source filter once, and
  set `show_matrix_sessions=True` only for an explicit `matrix` filter. Include
  the setting in the response metadata and settings-save invalidation list.

  Keep Matrix separate from the existing “previous messaging sessions” switch:
  that switch controls reset/continuation deduplication, while this setting
  controls whether the current Matrix source is shown at all.

- [ ] **Step 4: Run the focused tests and neighboring source-filter tests.**

  Run:

  ```bash
  ./scripts/test.sh tests/test_matrix_session_visibility.py tests/test_webhook_project_sessions.py tests/test_1079_cron_session_project.py tests/test_issue4766_sidebar_source_pushdown.py -q
  ```

  Expected: all tests pass, including cache-key separation and explicit-source
  reveal behavior.

- [ ] **Step 5: Commit the backend visibility slice.**

  ```bash
  git add api/config.py api/models.py api/routes.py api/route_session_list_cache.py tests/test_matrix_session_visibility.py
  git commit -m "feat: hide Matrix sessions behind a sidebar filter"
  ```

### Task 2: Add Matrix preference UI and source-chip presentation

**Files:**
- Modify: `static/index.html` Preferences markup near the existing Cron/Webhook/Kanban session toggles.
- Modify: `static/panels.js` preference load, autosave, explicit save, and dependency gating.
- Modify: `static/boot.js` settings mirror if source visibility is mirrored there.
- Modify: `static/sessions.js` source classification/label and source-filter rendering.
- Modify: `static/i18n.js` English and every supported locale catalog.
- Modify: `tests/test_matrix_session_visibility.py` or create `tests/test_matrix_session_ui.py` for static contracts.
- Modify: `TESTING.md` session-sidebar manual test section.

**Interfaces:**
- Consumes: `/api/sessions.settings.show_matrix_sessions` and existing source-filter state.
- Produces: a persisted Settings checkbox, localized Matrix copy, and a Matrix chip/filter that remains discoverable when rows exist.

- [ ] **Step 1: Write failing static/UI contract tests.**

  Assert that the HTML exposes a Matrix visibility checkbox, `panels.js`
  includes the setting in both autosave and explicit-save payloads, the setting
  is disabled when Show non-WebUI sessions is disabled, and `sessions.js`
  recognizes `matrix` as a source label/filter. Assert that the required keys
  occur in all locale blocks using the repository’s existing locale-parity test
  style.

- [ ] **Step 2: Run the tests and verify RED.**

  ```bash
  ./scripts/test.sh tests/test_matrix_session_ui.py -q
  ```

  Expected: failures for the absent checkbox, save payload, and locale/source
  strings.

- [ ] **Step 3: Implement the preference and chip behavior.**

  Follow the existing Cron/Webhook/Kanban checkbox pattern. Use one English
  source label (`Matrix`) and localized descriptions, preserving the existing
  source-filter query contract instead of creating a second Matrix-only API.
  Make the checkbox subordinate to Show non-WebUI sessions in both load and
  change handlers.

- [ ] **Step 4: Run focused UI/static checks.**

  ```bash
  ./scripts/test.sh tests/test_matrix_session_ui.py tests/test_locale_completeness.py -q
  node --check static/sessions.js
  node --check static/panels.js
  ```

  Expected: all focused tests pass and both JavaScript files parse cleanly.

- [ ] **Step 5: Commit the preference slice.**

  ```bash
  git add static/index.html static/panels.js static/boot.js static/sessions.js static/i18n.js tests/test_matrix_session_ui.py TESTING.md
  git commit -m "feat: add Matrix sidebar visibility preference"
  ```

### Task 3: Implement WebUI-owned organization for imported Matrix sessions

**Files:**
- Modify: `api/routes.py` imported-session resolution and `/api/session/move` plus `/api/session/archive` mutation branches.
- Modify: `api/models.py` sidecar projection/overlay helpers if `project_id` or `archived` is not currently overlaid for imported Matrix rows.
- Modify: `static/sessions.js` read-only action-menu and archive/move eligibility helpers.
- Create: `tests/test_matrix_session_organization.py`.

**Interfaces:**
- Consumes: exact external Matrix row identity/profile from the state-db projection; existing `load_projects()` and per-session lock helpers.
- Produces: a narrow internal helper with behavior equivalent to `materialize_matrix_organization_metadata(sid, cli_meta)`, returning a WebUI `Session` sidecar with no transcript messages and `read_only=True`; `/api/session/move` and `/api/session/archive` accept only the Matrix source for this path.

- [ ] **Step 1: Write failing tests for the organization boundary.**

  Add production-composed tests that:

  - create an isolated state directory and an external Matrix row;
  - move it to a same-profile project and assert HTTP success;
  - reload the sidebar projection and assert the project is present;
  - archive it and assert it disappears from the default projection while the
    sidecar records `archived=True`;
  - inspect the agent `state.db` row before and after and assert it is byte- or
    row-equivalent;
  - send the same move request for a non-Matrix read-only row and assert 403;
  - send an unknown Matrix ID and assert 404 without creating a sidecar;
  - reject a cross-profile target project with 404.

- [ ] **Step 2: Run the organization tests and verify RED.**

  ```bash
  ./scripts/test.sh tests/test_matrix_session_organization.py -q
  ```

  Expected: imported Matrix move/archive currently return the read-only error,
  and no WebUI sidecar organization metadata exists.

- [ ] **Step 3: Implement the narrow Matrix sidecar path.**

  Resolve the authoritative Matrix row and profile before creating a sidecar.
  Create/update only the metadata fields needed for sidebar organization:
  session ID, title/model timestamps, profile, source fields, read-only flag,
  `project_id`, and `archived`. Keep `messages=[]` and do not call any agent
  state-db write helper. Use the existing per-session lock and target-project
  authorization. Preserve the current PermissionError path for every other
  read-only source.

  Update sidebar projection overlay logic so an existing sidecar wins for
  `project_id` and `archived` without replacing external message counts or
  transcript ownership.

- [ ] **Step 4: Update individual UI actions to expose only permitted Matrix operations.**

  Keep rename, delete, duplicate, and chat-send protections unchanged. For an
  imported Matrix row, expose Archive/Restore and Move to project; route them to
  the new backend path and show the existing error style for failures. Ensure
  the UI never presents a misleading writable composer action.

- [ ] **Step 5: Run focused organization and neighboring lifecycle tests.**

  ```bash
  ./scripts/test.sh tests/test_matrix_session_organization.py tests/test_issue3746_session_move_delete_timeout.py tests/test_issue2472_fork_from_here_messaging.py tests/test_session_events.py -q
  node --check static/sessions.js
  ```

  Expected: Matrix metadata moves/archive locally, the external row is
  unchanged, and existing non-Matrix protections remain green.

- [ ] **Step 6: Commit the organization slice.**

  ```bash
  git add api/routes.py api/models.py static/sessions.js tests/test_matrix_session_organization.py
  git commit -m "feat: organize imported Matrix sessions locally"
  ```

### Task 4: Dock conversation multiselect and support Matrix organization

**Files:**
- Modify: `static/index.html` Chat sidebar structure to place a dock outside `#sessionList`.
- Modify: `static/sessions.js` selection state, selection pruning, dock rendering, batch actions, and Matrix eligibility.
- Modify: `static/style.css` dock layout, responsive styles, focus/disabled states, and safe-area spacing.
- Modify: `tests/test_session_batch_select.py` or add `tests/test_issue6928_6929_session_select_dock.py`.
- Modify: `TESTING.md` batch-selection manual procedure.

**Interfaces:**
- Consumes: `_isReadOnlySession()`, the new Matrix organization backend contract, current `_selectedSessions` state, and existing archive/delete/move confirmation flows.
- Produces: `#sessionBatchDock` with idle `#sessionSelectToggle` or active `#batchActionBar`; selection pruning that retains writable sessions and imported Matrix rows only.

- [ ] **Step 1: Write failing dock and eligibility tests.**

  Assert that the sidebar HTML contains a dock sibling after `#sessionList`,
  that `sessions.js` renders the dock rather than appending Select to the
  scrolling list, and that the active toolbar remains visible with zero
  selected rows. Add behavior coverage for selecting a writable row and an
  imported Matrix row, pruning a generic read-only row, and posting batch Move
  requests to the organization endpoint.

- [ ] **Step 2: Run the tests and verify RED.**

  ```bash
  ./scripts/test.sh tests/test_session_batch_select.py tests/test_issue6928_6929_session_select_dock.py -q
  ```

  Expected: the current implementation fails the sibling-dock placement and
  persistent-toolbar assertions; read-only Matrix rows are currently excluded.

- [ ] **Step 3: Move the existing selection UI into the persistent dock.**

  Adapt the behavior from open PR #6930 without copying unrelated changes:
  preserve valid selections across rerenders, clear selections on scope/profile
  changes, keep keyboard focus stable, use semantic buttons, and make the
  viewport-bounded project picker a child of the dock. Keep Archive/Delete
  confirmations and current per-session request handling.

- [ ] **Step 4: Add Matrix-specific selection eligibility.**

  Replace the blanket read-only exclusion with a single predicate that returns
  true for writable sessions and imported Matrix rows, false for every other
  read-only row. Ensure Select All uses the same predicate as individual
  checkboxes and stale-selection pruning.

- [ ] **Step 5: Implement responsive dock styling.**

  Make the sidebar a column layout with the session list as the only scrolling
  region and the dock as a non-scrolling flex item. Use existing color/radius
  variables, compact controls, clear disabled states, and mobile safe-area
  padding. Verify no last-row occlusion or horizontal clipping at desktop,
  laptop, narrow, and mobile widths.

- [ ] **Step 6: Run focused UI tests and syntax checks.**

  ```bash
  ./scripts/test.sh tests/test_session_batch_select.py tests/test_issue6928_6929_session_select_dock.py tests/test_issue4662_profile_switch_skeleton_behaviour.py tests/test_issue4766_sidebar_source_pushdown.py -q
  node --check static/sessions.js
  npm run lint:runtime
  ```

  Expected: focused tests and runtime lint pass. If ESLint is unavailable,
  record the repository-prescribed skip rather than installing an unrelated
  global tool.

- [ ] **Step 7: Commit the multiselect slice.**

  ```bash
  git add static/index.html static/sessions.js static/style.css tests/test_session_batch_select.py tests/test_issue6928_6929_session_select_dock.py TESTING.md
  git commit -m "feat: dock conversation multiselect controls"
  ```

### Task 5: Full verification, visual evidence, and fork-backed PR handoff

**Files:**
- Modify: `TESTING.md` with final manual verification notes and the captured desktop/narrow evidence references.
- Create: local screenshot/evidence files only if the repository’s existing browser workflow stores them in an ignored path.

**Interfaces:**
- Consumes: all four feature slices and their regression tests.
- Produces: verified branch, concise PR description, and a fork-backed PR targeting `nesquena/hermes-webui:master`.

- [ ] **Step 1: Run the complete affected test set.**

  ```bash
  ./scripts/test.sh \
    tests/test_matrix_session_visibility.py \
    tests/test_matrix_session_ui.py \
    tests/test_matrix_session_organization.py \
    tests/test_session_batch_select.py \
    tests/test_issue6928_6929_session_select_dock.py \
    tests/test_webhook_project_sessions.py \
    tests/test_1079_cron_session_project.py \
    tests/test_issue3746_session_move_delete_timeout.py \
    tests/test_issue2472_fork_from_here_messaging.py \
    tests/test_session_events.py -q
  ```

- [ ] **Step 2: Run repository hygiene and browser checks after the last code change.**

  ```bash
  node --check static/sessions.js
  node --check static/panels.js
  python3 -m compileall -q api server.py bootstrap.py tests scripts
  git diff --check origin/master...HEAD
  python tests/browser_smoke.py
  ```

  Capture the sidebar at a wide desktop viewport and a narrow/mobile viewport
  with enough seeded sessions to prove the dock remains visible and Matrix rows
  are hidden in All but visible through the Matrix filter. Record any browser
  or Playwright checks that cannot run locally.

- [ ] **Step 3: Review the final diff against the approved scope.**

  Confirm that only Matrix visibility/organization, multiselect docking,
  associated tests/docs, and the design/plan artifacts changed. Confirm no
  agent `state.db` write path is called by Matrix move/archive.

- [ ] **Step 4: Prepare the fork-backed GitHub branch and draft PR.**

  Resolve GitHub authentication and fork ownership with the GitHub connector.
  Push `feat/matrix-session-management` to the user’s fork, then open a draft
  PR targeting `nesquena/hermes-webui:master`. The PR body must include:
  affected sibling surfaces, RED/GREEN regression evidence, verification
  commands, desktop/narrow visual evidence, the state-layer ownership proof,
  and explicit unverifiable items.

- [ ] **Step 5: Do not claim completion until fresh verification output exists.**

  Re-run the final verification commands after the last edit and before the PR
  or completion message. Report exact pass counts and any skipped checks.
