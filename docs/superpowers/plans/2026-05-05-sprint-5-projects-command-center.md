# Sprint 5 Projects Command Center Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the Neo Projects Command Center as a local-first Projects page with Kanban, List, filters, local persistence, and external reference fields ready for future Jira sync.

**Architecture:** Add a focused `api/projects.py` module that owns the schema, migration, validation, persistence, counts, and task/project CRUD. Keep `api/routes.py` as a thin route adapter and add minimal `PATCH` support in `server.py`. Add `static/kanban.js` as the Projects page controller, replacing the current placeholder in `#mainProjects` while preserving the existing Neo shell.

**Tech Stack:** Python stdlib HTTP server, JSON file persistence in `~/.hermes/webui/projects.json`, vanilla JS, static CSS, pytest, Node syntax checks.

---

### Task 1: Backend Local-First Projects API

**Files:**
- Create: `api/projects.py`
- Modify: `api/routes.py`
- Modify: `server.py`
- Test: `tests/test_neo_projects_api.py`

- [ ] Add tests for schema migration, project CRUD, task CRUD, counts, and `external_ref` preservation.
- [ ] Implement `api/projects.py` with schema v2 helpers and atomic-ish JSON writes.
- [ ] Register `GET /api/projects`, `POST /api/projects`, `PATCH /api/projects/{id}`, `POST /api/project-tasks`, `PATCH /api/project-tasks/{id}`, and `POST /api/project-tasks/{id}/archive`.
- [ ] Keep legacy `/api/projects/create`, `/rename`, `/delete` working by delegating to the new module.
- [ ] Run `pytest tests/test_neo_projects_api.py -q`.

### Task 2: Projects Page DOM and Controller

**Files:**
- Create: `static/kanban.js`
- Modify: `static/index.html`
- Modify: `static/panels.js`
- Test: `tests/test_neo_projects_kanban.py`

- [ ] Replace the `#mainProjects` placeholder with semantic containers for header actions, filters, status pills, Kanban, List, empty state, and modals.
- [ ] Load `static/kanban.js` before `panels.js`.
- [ ] Make `switchPanel('projects')` call `loadProjectsCommandCenter()` if present.
- [ ] Implement `static/kanban.js` with fetch, render, filter state, view switching, modal forms, and HTML5 drag-and-drop.
- [ ] Run `node --check static/kanban.js` and `pytest tests/test_neo_projects_kanban.py -q`.

### Task 3: Neo Visual Styling and i18n

**Files:**
- Modify: `static/style.css`
- Modify: `static/i18n.js`
- Test: `tests/test_neo_projects_kanban.py`

- [ ] Add `.projects-*` CSS using existing Neo tokens.
- [ ] Keep UI dense, scan-friendly, and aligned to the provided Kanban/List references.
- [ ] Add `en` and `pt-BR` i18n keys for Projects labels, filters, modals, empty states, and errors.
- [ ] Run `node --check static/i18n.js` and project UI tests.

### Task 4: Documentation and Verification

**Files:**
- Modify: `docs/neo/TASKS.md`
- Add: `docs/neo/evidencias/HU-04.*/README.md` as implementation evidence.

- [ ] Mark implemented Sprint 5 HUs according to actual delivered scope.
- [ ] Record verification commands and outputs.
- [ ] Run focused verification: backend tests, frontend static tests, `node --check`, and Neo focused suite.

## Self-Review

- Spec coverage: local-first persistence, `external_ref`, Kanban, List, filters, docs, and future Jira sync are covered.
- Scope: Jira API calls, OAuth, GitHub sync, Obsidian writes, and timeline view remain out of scope.
- Interface consistency: Project routes use `/api/projects`; task routes use `/api/project-tasks`; task status updates use `PATCH`.
