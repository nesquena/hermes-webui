# Multi-Source Sidebar Filter Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let users display the union of multiple dynamic session origins while showing at most two readable source chips plus a `+N` overflow control.

**Architecture:** Normalize repeated `sidebar_source` query values into a canonical server tuple used by filtering and cache identity, while preserving the existing omitted-parameter API behavior. On the client, own an ordered non-empty source array, migrate the prior single value, emit repeated parameters, and render immediate checkbox updates through the existing source menu.

**Tech Stack:** Python 3.11–3.13, vanilla JavaScript, CSS, pytest through `./scripts/test.sh`, Node-backed frontend behavior tests.

## Global Constraints

- Do not hardcode Matrix or any other adapter in filtering or toolbar logic.
- The browser always keeps at least one selected origin; removing the last restores WebUI.
- Omitted `sidebar_source` parameters retain the existing API behavior and return all permitted origins.
- Any malformed supplied source value fails closed; it must not widen results.
- Source changes apply immediately without closing the dropdown.
- Show at most two readable selected-source chips plus `+N`; never introduce horizontal toolbar scrolling.
- Do not add dependencies, frameworks, build tools, or long-lived processes.
- Run pytest only through `./scripts/test.sh`.

---

### Task 1: Repeated source parsing and union filtering

**Files:**
- Modify: `api/routes.py`
- Modify: `api/route_session_list_cache.py`
- Test: `tests/test_issue4766_sidebar_source_pushdown.py`
- Test: `tests/test_session_origin_taxonomy.py`

**Interfaces:**
- Consumes: `parse_qs(parsed.query).get("sidebar_source", [])`, `_sidebar_session_origin(session)`
- Produces: `_normalize_sidebar_sources(values: list[str]) -> tuple[str, ...] | None`, `sidebar_sources: tuple[str, ...] | None` accepted by `_build_session_list_cache_payload()` and `_session_list_cache_key()`

- [ ] **Step 1: Write failing route behavior tests**

  Add literal fixtures containing WebUI, CLI, Matrix, and Telegram rows. Assert repeated `sidebar_source=webui&sidebar_source=matrix` returns exactly WebUI + Matrix in authoritative order, duplicate Matrix values do not duplicate rows, omission retains all rows, and one malformed repeated value returns HTTP 400 without invoking a broadened builder.

- [ ] **Step 2: Run the route tests and verify RED**

  Run: `./scripts/test.sh tests/test_issue4766_sidebar_source_pushdown.py -q`

  Expected: FAIL because the route currently reads only the first parameter and silently converts malformed input to no filter.

- [ ] **Step 3: Implement strict repeated-value normalization**

  Add `_normalize_sidebar_sources(values)` in `api/routes.py`. Normalize case/spacing, preserve the first occurrence of each valid key, enforce the existing 48-character and `[a-z0-9_]+` constraints, return `None` only for an omitted list, and raise `ValueError` for any malformed supplied value. Map `ValueError` to the route's existing JSON 400 response pattern.

- [ ] **Step 4: Implement union membership filtering**

  Rename the payload-builder argument to `sidebar_sources`. Convert a non-`None` tuple to a set once and make `_filter_sidebar_source(rows)` retain rows whose `_sidebar_session_origin(row)` belongs to that set. Keep global `session_origin_counts` computed before the selected-union filter.

- [ ] **Step 5: Canonicalize cache identity**

  Change `_session_list_cache_key(..., sidebar_sources=None)` so the key stores `tuple(sorted(set(sidebar_sources)))` when supplied and `None` when omitted. Update every caller and test fixture to use the plural argument. This prevents selection order from creating duplicate server cache entries while distinguishing every source combination.

- [ ] **Step 6: Run focused server tests and verify GREEN**

  Run: `./scripts/test.sh tests/test_issue4766_sidebar_source_pushdown.py tests/test_session_origin_taxonomy.py tests/test_session_cache_ownership.py -q`

  Expected: PASS.

- [ ] **Step 7: Commit the server slice**

  Run: `git add api/routes.py api/route_session_list_cache.py tests/test_issue4766_sidebar_source_pushdown.py tests/test_session_origin_taxonomy.py && git commit -m "feat: filter sidebar by multiple sources"`

### Task 2: Ordered client state, persistence migration, and request identity

**Files:**
- Modify: `static/sessions.js`
- Test: `tests/test_issue4766_sidebar_source_pushdown.py`
- Test: `tests/test_session_origin_taxonomy.py`
- Test: `tests/test_sidebar_session_partition.py`

**Interfaces:**
- Consumes: server repeated-parameter contract from Task 1
- Produces: `_sessionSourceFilters: string[]`, `_normalizeSessionSourceFilters(values) -> string[]`, `_setSessionSourceFilters(values)`, `_toggleSessionSourceFilter(origin, selected)`, `_requestedSessionSidebarSources() -> string[]`

- [ ] **Step 1: Write failing client-state behavior tests**

  Use the existing Node extraction harness to assert normalization keeps first-seen order, removes duplicates, rejects malformed entries, and restores `['webui']` for an empty list. Assert migration reads `hermes-session-source-filter=matrix`, writes the new ordered storage key, and preserves Matrix. Assert add/remove operations append new origins, preserve retained order, and restore WebUI after removing the last origin.

- [ ] **Step 2: Write failing request-identity tests**

  Assert `_sessionListQueryString()` emits `sidebar_source=webui&sidebar_source=matrix` through `URLSearchParams.append`, and source snapshots/stale guards compare the complete normalized array rather than only the first origin.

- [ ] **Step 3: Run client-state tests and verify RED**

  Run: `./scripts/test.sh tests/test_issue4766_sidebar_source_pushdown.py tests/test_session_origin_taxonomy.py tests/test_sidebar_session_partition.py -q`

  Expected: FAIL because the client owns one `_sessionSourceFilter` string and emits one parameter.

- [ ] **Step 4: Implement ordered non-empty source state**

  Replace the single string with `_sessionSourceFilters=['webui']`. Centralize normalization and mutation in the functions named above. `_setSessionSourceFilters` must no-op when arrays are equal; otherwise it clears `_activeProject`, `_selectedSessions`, and `_sessionSelectMode` once, persists the ordered array, renders compatible cache state, and starts one list request.

- [ ] **Step 5: Implement persistence migration**

  Read the new versioned JSON-array key first. When absent, read and validate the old single-value key, initialize the array with that value, and write the new key. Invalid or empty persisted state becomes `['webui']`. Do not delete the old key in this PR so rollback retains the previous preference.

- [ ] **Step 6: Update every single-source consumer**

  Replace equality branches with explicit helpers: `includes('cli')` for CLI inclusion, a one-origin helper only where copy requires a singular label, and membership checks for partition/render logic. Query building appends every selected origin. Snapshot, pending-payload, cache, and stale-response identity includes a canonical joined selection key.

- [ ] **Step 7: Run focused client tests and verify GREEN**

  Run: `./scripts/test.sh tests/test_issue4766_sidebar_source_pushdown.py tests/test_session_origin_taxonomy.py tests/test_sidebar_session_partition.py tests/test_issue4759_parallel_sidebar_boot_fetch.py -q`

  Expected: PASS.

- [ ] **Step 8: Commit the client-state slice**

  Run: `git add static/sessions.js tests/test_issue4766_sidebar_source_pushdown.py tests/test_session_origin_taxonomy.py tests/test_sidebar_session_partition.py && git commit -m "feat: persist multiple sidebar sources"`

### Task 3: Immediate multi-select menu and compact toolbar

**Files:**
- Modify: `static/sessions.js`
- Modify: `static/style.css`
- Test: `tests/test_session_origin_taxonomy.py`
- Test: `tests/test_mobile_layout.py`

**Interfaces:**
- Consumes: `_sessionSourceFilters`, `_toggleSessionSourceFilter(origin, selected)`, `_sessionSourceFilterModel(...)`
- Produces: two `.session-source-chip` elements maximum, optional `.session-source-overflow`, and checkbox-based `.session-source-menu-item` rows

- [ ] **Step 1: Write failing toolbar-model tests**

  With selected origins `['matrix','telegram','slack','discord']`, assert the model exposes visible chips Matrix and Telegram plus `overflowCount: 2`. Assert one and two selections produce no overflow, dynamic future origin labels remain complete, and counts remain global.

- [ ] **Step 2: Write failing menu-interaction tests**

  Exercise the renderer with a minimal real DOM harness or available browser fixture. Assert menu rows are native checkboxes, checking Slack calls `_toggleSessionSourceFilter('slack', true)` without hiding the menu, unchecking Matrix calls the false branch, chip removal updates immediately, and `+N` opens the same menu.

- [ ] **Step 3: Run UI tests and verify RED**

  Run: `./scripts/test.sh tests/test_session_origin_taxonomy.py tests/test_mobile_layout.py -q`

  Expected: FAIL because the current model exposes one active origin and menu items behave like radio buttons.

- [ ] **Step 4: Implement the compact toolbar**

  Render the first two ordered selections as full-label chips with named remove buttons. Render `+N` only when more than two are selected and make it invoke the same menu-opening function as `Sources`. Keep the trigger count representing all available origins, not selected origins.

- [ ] **Step 5: Implement immediate checkbox updates**

  Render one native checkbox per dynamic origin. Checkbox change calls `_toggleSessionSourceFilter` and leaves the menu open. Reconcile the checked states, toolbar chips, and overflow text in place before the asynchronous server response, preserving focus on the changed row.

- [ ] **Step 6: Implement restrained responsive styling**

  Allow two chips to share available width with ellipsis only in the toolbar; preserve full wrapping labels in the menu. Keep `+N` and Sources reachable, use no horizontal scrolling, and verify the menu stays within the sidebar at desktop and narrow widths.

- [ ] **Step 7: Run UI tests and verify GREEN**

  Run: `./scripts/test.sh tests/test_session_origin_taxonomy.py tests/test_mobile_layout.py tests/test_session_batch_select.py -q`

  Expected: PASS.

- [ ] **Step 8: Commit the UI slice**

  Run: `git add static/sessions.js static/style.css tests/test_session_origin_taxonomy.py tests/test_mobile_layout.py && git commit -m "feat: add multi-source sidebar toolbar"`

### Task 4: Integrated regression, deployment, and PR evidence

**Files:**
- Modify only for verified defects: files from Tasks 1–3 and their tests
- Update: existing pull request body for PR 6985

**Interfaces:**
- Consumes: completed server, state, and toolbar slices
- Produces: deployed local build and reviewable verification evidence

- [ ] **Step 1: Run the affected and neighboring regression suite**

  Run: `./scripts/test.sh tests/test_issue4766_sidebar_source_pushdown.py tests/test_session_origin_taxonomy.py tests/test_session_cache_ownership.py tests/test_sidebar_session_partition.py tests/test_issue4759_parallel_sidebar_boot_fetch.py tests/test_session_batch_select.py tests/test_matrix_session_organization.py tests/test_mobile_layout.py tests/test_sessions_search_profile_scope.py -q`

  Expected: PASS, with only documented environment skips.

- [ ] **Step 2: Run static and diff verification**

  Run: `node --check static/sessions.js && git diff --check && git status --short`

  Expected: valid JavaScript, no whitespace errors, and only planned files changed.

- [ ] **Step 3: Deploy without disturbing remote-only edits**

  Copy only the changed runtime files into `/opt/hermes-webui`, restart `hermes-webui.service`, wait for the listener log, verify service activity, and compare deployed/local SHA-256 hashes. Do not reset or overwrite unrelated remote modifications.

- [ ] **Step 4: Perform automated live verification**

  Verify repeated source requests return union results, malformed repeated values fail closed, toolbar state persists across refresh, checkbox updates issue one request, and batch selection still operates over the visible union.

- [ ] **Step 5: Perform signed-in manual verification**

  At desktop and narrow widths, select one, two, and four origins; confirm two chips + `+2`, stable ordering, immediate updates without menu closure, last-remove WebUI fallback, full menu labels/counts, no horizontal scroll, and unchanged date-heading multi-select behavior. Capture before/after evidence.

- [ ] **Step 6: Push and update the existing PR**

  Push `feat/matrix-session-management` to the user's fork. Update PR 6985 with repeated-parameter/API compatibility notes, RED/GREEN evidence, regression counts, desktop/narrow images, deployment hashes, and any manual-verification limitation stated explicitly.
