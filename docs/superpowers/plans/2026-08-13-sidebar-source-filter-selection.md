# Sidebar Source Filter and Selection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace overflowing source tabs with a readable dynamic source menu and make date-group batch selection progressively disclosed.

**Architecture:** Preserve `_sessionSourceFilter`, `_sessionSelectMode`, and `_selectedSessions` as authoritative state. Change only the sidebar render and interaction layer in `static/sessions.js` and its styling in `static/style.css`, using the existing persistent batch dock and dynamic origin metadata.

**Tech Stack:** Vanilla JavaScript, CSS, Python pytest harness, Node-based frontend behavior tests.

## Global Constraints

- Do not hardcode Matrix or any other adapter as a special high-level filter.
- Do not add dependencies, frameworks, build tools, or long-lived processes.
- Source filtering remains single-origin and preserves server pushdown and local storage.
- Hover-only affordances must have keyboard and touch equivalents.
- Local pytest runs use `./scripts/test.sh`.

---

### Task 1: Dynamic source menu

**Files:**
- Modify: `static/sessions.js`
- Modify: `static/style.css`
- Test: `tests/test_session_origin_taxonomy.py`

**Interfaces:**
- Consumes: `_sessionOriginKeys()`, `_sessionSourceLabel(origin, count)`, `_sessionSourceTabCount(origin, webuiCount, cliCount)`, `_setSessionSourceFilter(origin)`
- Produces: `_renderSessionSourceFilterControl(...)` and a `.session-source-filter` control with an anchored `.session-source-menu`

- [ ] **Step 1: Write a failing behavior test**

  Add a Node-backed test fixture that renders at least WebUI, CLI, Matrix, and Telegram origins and asserts one summary control, one Sources trigger, full menu labels/counts, active state, Escape closure, and selection through `_setSessionSourceFilter`.

- [ ] **Step 2: Run the focused test and verify RED**

  Run: `./scripts/test.sh tests/test_session_origin_taxonomy.py -q`

  Expected: FAIL because the source menu renderer and menu hooks do not exist.

- [ ] **Step 3: Implement the minimal source control**

  Extract source-control rendering from `renderSessionListFromCache()`. Build the active summary and Sources button from `_sessionOriginKeys()` and existing label/count helpers. Build an anchored menu with native buttons, active-state semantics, Escape/outside closure, and no adapter-specific branches.

- [ ] **Step 4: Add restrained responsive styling**

  Replace `.session-source-tabs` rules with source-summary, trigger, menu, item, count, focus, and narrow-width rules. Keep project filters visually separate and prevent label truncation inside the menu.

- [ ] **Step 5: Run focused and neighboring source tests**

  Run: `./scripts/test.sh tests/test_session_origin_taxonomy.py tests/test_issue2351_cli_session_source_filter.py tests/test_issue4766_sidebar_source_pushdown.py -q`

  Expected: PASS.

### Task 2: Progressive batch-selection disclosure

**Files:**
- Modify: `static/sessions.js`
- Modify: `static/style.css`
- Test: `tests/test_session_batch_select.py`

**Interfaces:**
- Consumes: `_enableSessionSelectMode()`, `exitSessionSelectMode()`, `_selectedSessions`, `_renderSessionBatchDock()`
- Produces: `.session-group-select-trigger` date-heading button and selection-mode visibility rules for `.session-select-cb-wrapper`

- [ ] **Step 1: Write failing selection behavior tests**

  Assert date groups render a button after the heading label, activation enters mode with an empty selected set, the persistent dock renders at zero selection, and no date control reports checked state merely because selection mode is active.

- [ ] **Step 2: Run the focused test and verify RED**

  Run: `./scripts/test.sh tests/test_session_batch_select.py -q`

  Expected: FAIL because the current entry point is a checked checkbox and all row checkboxes are exposed in selection mode.

- [ ] **Step 3: Implement the date-heading trigger**

  Replace `.session-group-select-toggle` input creation with a native button labeled Select. Stop pointer/click propagation and call `_enableSessionSelectMode()` without selecting rows. Preserve header collapse behavior outside the button.

- [ ] **Step 4: Implement quiet checkbox disclosure**

  Keep row checkbox elements in selection mode, but hide unchecked wrappers until their row is hovered or focus is within it. Keep selected wrappers visible. On narrow/touch layouts, show wrappers throughout selection mode.

- [ ] **Step 5: Run focused and neighboring batch tests**

  Run: `./scripts/test.sh tests/test_session_batch_select.py tests/test_session_action_menu_focus.py tests/test_mobile_layout.py -q`

  Expected: PASS.

### Task 3: Integrated verification and deployment

**Files:**
- Modify only if required by verified defects: `static/sessions.js`, `static/style.css`, affected tests

**Interfaces:**
- Consumes: completed source-filter and selection behavior
- Produces: deployed local build with desktop and narrow verification evidence

- [ ] **Step 1: Run the affected regression suite**

  Run: `./scripts/test.sh tests/test_session_origin_taxonomy.py tests/test_issue2351_cli_session_source_filter.py tests/test_issue4766_sidebar_source_pushdown.py tests/test_session_batch_select.py tests/test_matrix_session_organization.py tests/test_mobile_layout.py -q`

  Expected: PASS.

- [ ] **Step 2: Inspect the diff for scope and static regressions**

  Run: `git diff --check && git diff --stat && git status --short`

  Expected: no whitespace errors and only source-filter/selection files plus their tests/docs.

- [ ] **Step 3: Deploy to the existing local Hermes host**

  Reuse the previously verified deployment path and preserve unrelated remote edits. Restart only the Hermes WebUI service.

- [ ] **Step 4: Perform automated browser verification**

  Verify full source labels and counts, source switching, menu dismissal, hover/focus selection entry, zero-selection dock, row selection, Select all/Deselect all, Exit, desktop width, and narrow width.

- [ ] **Step 5: Perform manual verification**

  Confirm the same behaviors in the running local build and capture before/after desktop and narrow evidence for the PR.

- [ ] **Step 6: Commit and push**

  Commit only the focused diff, push `feat/matrix-session-management` to the user's fork, and update the existing PR body with RED/GREEN evidence, verification commands, screenshots, sibling origins covered, and any unverified gaps.

