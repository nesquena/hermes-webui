# Fase 2: Projetos (Resumo + Jira Sync) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add project summary view, Jira integration (read/write), and sidebar cleanup to the Neo Projects Command Center.

**Architecture:** Server-side Jira client (`api/jira.py`) using REST API v3 with Basic Auth. Summary endpoint aggregates local tasks + cached Jira issues. Frontend adds a third "Resumo" view tab in `kanban.js`. Credentials loaded from env vars (JIRA_EMAIL + JIRA_TOKEN) at boot.

**Tech Stack:** Python 3.12 (stdlib `http.client` or `urllib.request` for Jira calls — no new deps), vanilla JS frontend, pytest for tests.

---

## File Structure

| Action | File | Responsibility |
|--------|------|----------------|
| Create | `api/jira.py` | Jira REST client, sync logic, route handlers |
| Create | `tests/test_jira.py` | Unit tests for Jira client + sync |
| Create | `tests/test_project_summary.py` | Tests for summary endpoint |
| Modify | `api/projects.py` | Add `project_summary()` function |
| Modify | `api/routes.py` | Register Jira + summary routes |
| Modify | `static/kanban.js` | Add "Resumo" view, sync button, Jira UI |
| Modify | `static/index.html` | Remove "Tarefas" from sidebar (3 places) |
| Modify | `static/panels.js` | Remove `todos` from `NEO_SHELL_PANELS` |
| Modify | `static/i18n.js` | Add i18n keys for summary + jira |

---

## Task 1: Sidebar Cleanup — Remove "Tarefas"

**Files:**
- Modify: `static/index.html:99` (rail button), `:113` (sidebar-nav), `:130` (neo-dashboard-menu)
- Modify: `static/panels.js:24` (NEO_SHELL_PANELS)

- [ ] **Step 1: Remove "Tarefas" rail button**

In `static/index.html`, delete line 99 (the `data-panel="todos"` button in the rail nav):
```html
<!-- DELETE THIS LINE -->
<button class="rail-btn nav-tab" data-panel="todos" onclick="switchPanel('todos')" title="Current task list" data-i18n-title="tab_todos" aria-label="Todos">...</button>
```

- [ ] **Step 2: Remove "Tarefas" sidebar-nav button**

In `static/index.html`, delete line 113 (the `data-panel="todos"` button in sidebar-nav):
```html
<!-- DELETE THIS LINE -->
<button class="nav-tab" data-panel="todos" data-label="Todos" onclick="switchPanel('todos')" title="Current task list" data-i18n-title="tab_todos">...</button>
```

- [ ] **Step 3: Remove "Tarefas" from neo-dashboard-menu**

In `static/index.html`, delete line 130 (the `data-panel="todos"` item in neo-dashboard-menu):
```html
<!-- DELETE THIS LINE -->
<button class="neo-dashboard-menu-item" data-neo-menu-item data-panel="todos" onclick="switchPanel('todos')">...<span data-i18n="tab_tasks">Tarefas</span></button>
```

- [ ] **Step 4: Remove `todos` from NEO_SHELL_PANELS**

In `static/panels.js:24`, change:
```javascript
const NEO_SHELL_PANELS = new Set(['dashboard', 'chat', 'projects', 'todos', 'profiles', 'finance', 'agents', 'settings', 'skills', 'tasks']);
```
To:
```javascript
const NEO_SHELL_PANELS = new Set(['dashboard', 'chat', 'projects', 'profiles', 'finance', 'agents', 'settings', 'skills', 'tasks']);
```

- [ ] **Step 5: Verify panel still accessible via URL**

Run: `curl -s http://localhost:8080/static/index.html | grep -c 'data-panel="todos"'`
Expected: 0 (no nav entries), but the panel div itself still exists in the HTML.

- [ ] **Step 6: Commit**

```bash
git add static/index.html static/panels.js
git commit -m "feat(projects): remove Tarefas shortcut from sidebar nav

Panel remains accessible via URL/API for backward compat.
Fase 2 cleanup — EP-10."
```

---

## Task 2: Jira Client — Core Module

**Files:**
- Create: `api/jira.py`
- Create: `tests/test_jira.py`

- [ ] **Step 1: Write failing test for Jira client initialization**

Create `tests/test_jira.py`:
```python
import os
import pytest


def test_jira_client_init_from_env(monkeypatch):
    monkeypatch.setenv("JIRA_EMAIL", "test@example.com")
    monkeypatch.setenv("JIRA_TOKEN", "fake-token-123")
    from api.jira import JiraClient
    client = JiraClient(base_url="https://test.atlassian.net")
    assert client.base_url == "https://test.atlassian.net"
    assert client._auth_header is not None


def test_jira_client_init_missing_creds(monkeypatch):
    monkeypatch.delenv("JIRA_EMAIL", raising=False)
    monkeypatch.delenv("JIRA_TOKEN", raising=False)
    from api.jira import JiraClient
    with pytest.raises(ValueError, match="JIRA_EMAIL"):
        JiraClient(base_url="https://test.atlassian.net")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_jira.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'api.jira'`

- [ ] **Step 3: Implement JiraClient class**

Create `api/jira.py`:
```python
"""Jira REST API v3 client for Neo WebUI project sync."""

import base64
import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
from typing import Any

logger = logging.getLogger(__name__)

_REQUEST_TIMEOUT = 30


class JiraClient:
    """Thin wrapper around Jira Cloud REST API v3."""

    def __init__(self, base_url: str, email: str | None = None, token: str | None = None):
        self.base_url = base_url.rstrip("/")
        email = email or os.environ.get("JIRA_EMAIL")
        token = token or os.environ.get("JIRA_TOKEN")
        if not email:
            raise ValueError("JIRA_EMAIL env var or email param required")
        if not token:
            raise ValueError("JIRA_TOKEN env var or token param required")
        creds = base64.b64encode(f"{email}:{token}".encode()).decode()
        self._auth_header = f"Basic {creds}"

    def _request(self, method: str, path: str, body: dict | None = None) -> dict:
        url = f"{self.base_url}/rest/api/3{path}"
        data = json.dumps(body).encode() if body else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", self._auth_header)
        req.add_header("Content-Type", "application/json")
        req.add_header("Accept", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=_REQUEST_TIMEOUT) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            if e.code == 429:
                retry_after = int(e.headers.get("Retry-After", "5"))
                logger.warning("Jira rate limited, retry after %ds", retry_after)
                time.sleep(min(retry_after, 60))
                return self._request(method, path, body)
            body_text = ""
            try:
                body_text = e.read().decode()[:500]
            except Exception:
                pass
            logger.error("Jira API %s %s → %d: %s", method, path, e.code, body_text)
            raise
        except urllib.error.URLError as e:
            logger.error("Jira connection error: %s", e.reason)
            raise

    def search_issues(self, jql: str, max_results: int = 50) -> list[dict]:
        data = self._request("POST", "/search/jql", {
            "jql": jql,
            "maxResults": max_results,
            "fields": ["summary", "status", "assignee", "priority", "updated", "created"],
        })
        return data.get("issues", [])

    def get_project(self, project_key: str) -> dict:
        return self._request("GET", f"/project/{project_key}")

    def create_issue(self, project_key: str, summary: str, issue_type: str = "Task") -> dict:
        body = {
            "fields": {
                "project": {"key": project_key},
                "summary": summary,
                "issuetype": {"name": issue_type},
            }
        }
        return self._request("POST", "/issue", body)

    def transition_issue(self, issue_key: str, transition_id: str) -> dict:
        return self._request("POST", f"/issue/{issue_key}/transitions", {
            "transition": {"id": transition_id}
        })

    def get_transitions(self, issue_key: str) -> list[dict]:
        data = self._request("GET", f"/issue/{issue_key}/transitions")
        return data.get("transitions", [])
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /home/jrmelo/Projetos/neo-webui && .venv/bin/pytest tests/test_jira.py -v`
Expected: 2 passed

- [ ] **Step 5: Commit**

```bash
git add api/jira.py tests/test_jira.py
git commit -m "feat(jira): add JiraClient with Basic Auth and REST v3 methods

Supports search, create, transition. Rate-limit retry built in.
No new dependencies — uses stdlib urllib."
```

---

## Task 3: Jira Sync Logic

**Files:**
- Modify: `api/jira.py` (add sync functions)
- Modify: `api/projects.py` (add source helpers)
- Create: `tests/test_jira_sync.py`

- [ ] **Step 1: Write failing test for sync_source**

Create `tests/test_jira_sync.py`:
```python
import json
import os
import pytest
from unittest.mock import patch, MagicMock
from pathlib import Path


@pytest.fixture
def tmp_projects_file(tmp_path, monkeypatch):
    pf = tmp_path / "projects.json"
    store = {
        "schema_version": 2,
        "sources": [{
            "source_id": "jira_test",
            "type": "jira",
            "name": "Test Jira",
            "base_url": "https://test.atlassian.net",
            "project_key": "TST",
            "sync_enabled": True,
            "sync_mode": "read",
            "status_map": {"To Do": "backlog", "In Progress": "em_andamento", "Done": "concluido"},
            "last_sync_at": None,
            "sync_status": "idle",
            "sync_error": None,
        }],
        "projects": [{
            "project_id": "prj_test1",
            "name": "Test Project",
            "description": "",
            "domain": "projetos",
            "status": "ativo",
            "color": "#00E5FF",
            "default_source_id": "jira_test",
            "refs": {"github": [], "obsidian": [], "sessions": []},
            "created_at": 1700000000,
            "updated_at": 1700000000,
            "archived": False,
        }],
        "tasks": [],
        "activity": [],
    }
    pf.write_text(json.dumps(store))
    monkeypatch.setattr("api.config.PROJECTS_FILE", pf)
    monkeypatch.setenv("JIRA_EMAIL", "test@example.com")
    monkeypatch.setenv("JIRA_TOKEN", "fake-token")
    return pf


def _fake_issues():
    return [
        {
            "key": "TST-1",
            "fields": {
                "summary": "First issue",
                "status": {"name": "To Do"},
                "assignee": None,
                "priority": {"name": "Medium"},
                "updated": "2026-05-14T10:00:00.000+0000",
                "created": "2026-05-10T08:00:00.000+0000",
            },
            "self": "https://test.atlassian.net/rest/api/3/issue/10001",
        },
        {
            "key": "TST-2",
            "fields": {
                "summary": "Second issue",
                "status": {"name": "In Progress"},
                "assignee": {"displayName": "Junior"},
                "priority": {"name": "High"},
                "updated": "2026-05-14T12:00:00.000+0000",
                "created": "2026-05-11T09:00:00.000+0000",
            },
            "self": "https://test.atlassian.net/rest/api/3/issue/10002",
        },
    ]


@patch("api.jira.JiraClient.search_issues")
def test_sync_source_creates_tasks(mock_search, tmp_projects_file):
    mock_search.return_value = _fake_issues()
    from api.jira import sync_source
    result = sync_source("jira_test")
    assert result["synced"] == 2
    assert result["created"] == 2
    assert result["updated"] == 0

    store = json.loads(tmp_projects_file.read_text())
    assert len(store["tasks"]) == 2
    assert store["tasks"][0]["external_ref"]["key"] == "TST-1"
    assert store["tasks"][0]["status"] == "backlog"
    assert store["tasks"][1]["status"] == "em_andamento"


@patch("api.jira.JiraClient.search_issues")
def test_sync_source_updates_existing(mock_search, tmp_projects_file):
    mock_search.return_value = _fake_issues()
    from api.jira import sync_source
    sync_source("jira_test")

    _fake_issues()[0]["fields"]["status"]["name"] = "Done"
    mock_search.return_value = _fake_issues()
    result = sync_source("jira_test")
    assert result["updated"] >= 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_jira_sync.py -v`
Expected: FAIL — `ImportError: cannot import name 'sync_source' from 'api.jira'`

- [ ] **Step 3: Add source-aware fields to _normalize_source**

In `api/projects.py`, update `_normalize_source` to preserve Jira-specific fields:
```python
def _normalize_source(source: Any) -> dict | None:
    if not isinstance(source, dict):
        return None
    name = str(source.get("name") or "").strip()
    if not name:
        return None
    return {
        "source_id": str(source.get("source_id") or f"src_{uuid.uuid4().hex[:8]}"),
        "type": str(source.get("type") or "local").strip().lower(),
        "name": name[:128],
        "base_url": str(source.get("base_url") or "").strip(),
        "project_key": str(source.get("project_key") or "").strip(),
        "sync_enabled": bool(source.get("sync_enabled", False)),
        "sync_mode": str(source.get("sync_mode") or "read").strip(),
        "status_map": source.get("status_map") if isinstance(source.get("status_map"), dict) else {},
        "last_sync_at": source.get("last_sync_at"),
        "sync_status": str(source.get("sync_status") or "idle"),
        "sync_error": source.get("sync_error"),
    }
```

- [ ] **Step 4: Implement sync_source in api/jira.py**

Append to `api/jira.py`:
```python
# ── Sync engine ────────────────────────────────────────────────────────────

_sync_locks: dict[str, threading.Lock] = {}
_sync_locks_guard = threading.Lock()


def _get_sync_lock(source_id: str) -> threading.Lock:
    with _sync_locks_guard:
        if source_id not in _sync_locks:
            _sync_locks[source_id] = threading.Lock()
        return _sync_locks[source_id]


def sync_source(source_id: str) -> dict:
    """Sync issues from a Jira source into local tasks. Returns stats."""
    from api.projects import load_project_store, save_project_store

    lock = _get_sync_lock(source_id)
    if not lock.acquire(blocking=False):
        return {"error": "sync already running", "synced": 0, "created": 0, "updated": 0}

    try:
        store = load_project_store()
        source = next((s for s in store["sources"] if s["source_id"] == source_id), None)
        if not source:
            raise ValueError(f"Source {source_id} not found")
        if source["type"] != "jira":
            raise ValueError(f"Source {source_id} is not a Jira source")

        project = next(
            (p for p in store["projects"] if p.get("default_source_id") == source_id),
            None,
        )
        if not project:
            raise ValueError(f"No project linked to source {source_id}")

        client = JiraClient(base_url=source["base_url"])
        jql = f'project = "{source["project_key"]}" ORDER BY updated DESC'
        issues = client.search_issues(jql, max_results=50)

        status_map = source.get("status_map") or {}
        created = 0
        updated = 0

        for issue in issues:
            key = issue["key"]
            fields = issue.get("fields", {})
            jira_status = fields.get("status", {}).get("name", "")
            local_status = status_map.get(jira_status, "backlog")
            summary = fields.get("summary", "")
            issue_url = f"{source['base_url']}/browse/{key}"

            existing = next(
                (t for t in store["tasks"]
                 if t.get("external_ref") and t["external_ref"].get("key") == key
                 and t["external_ref"].get("source_id") == source_id),
                None,
            )

            now = time.time()
            if existing:
                existing["title"] = summary[:180]
                existing["status"] = local_status
                existing["external_ref"]["status"] = jira_status
                existing["external_ref"]["synced_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))
                existing["updated_at"] = now
                updated += 1
            else:
                import uuid
                task = {
                    "task_id": f"tsk_{uuid.uuid4().hex[:12]}",
                    "project_id": project["project_id"],
                    "title": summary[:180],
                    "description": "",
                    "status": local_status,
                    "priority": "media",
                    "category": "Backend",
                    "owner": (fields.get("assignee") or {}).get("displayName", "")[:64] or "jr",
                    "progress": 100 if local_status == "concluido" else 0,
                    "due_date": "",
                    "external_ref": {
                        "type": "jira",
                        "source_id": source_id,
                        "key": key,
                        "url": issue_url,
                        "status": jira_status,
                        "synced_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
                    },
                    "refs": {"github": [], "obsidian": [], "sessions": []},
                    "created_at": now,
                    "updated_at": now,
                    "archived": False,
                }
                store["tasks"].append(task)
                created += 1

        source["last_sync_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        source["sync_status"] = "ok"
        source["sync_error"] = None
        save_project_store(store)

        return {"synced": len(issues), "created": created, "updated": updated}

    except Exception as e:
        logger.exception("Jira sync failed for source %s", source_id)
        try:
            store = load_project_store()
            src = next((s for s in store["sources"] if s["source_id"] == source_id), None)
            if src:
                src["sync_status"] = "error"
                src["sync_error"] = str(e)[:500]
                save_project_store(store)
        except Exception:
            pass
        return {"error": str(e), "synced": 0, "created": 0, "updated": 0}
    finally:
        lock.release()
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_jira_sync.py -v`
Expected: 2 passed

- [ ] **Step 6: Commit**

```bash
git add api/jira.py api/projects.py tests/test_jira_sync.py
git commit -m "feat(jira): implement sync_source — imports issues into local tasks

Maps Jira status via source.status_map, creates/updates tasks with
external_ref. Mutex prevents concurrent sync per source."
```

---

## Task 4: Jira API Routes

**Files:**
- Modify: `api/jira.py` (add route handler functions)
- Modify: `api/routes.py` (register routes in handle_get and handle_post)

- [ ] **Step 1: Write failing test for Jira routes**

Add to `tests/test_jira.py`:
```python
@patch("api.jira.sync_source")
def test_sync_route_triggers_sync(mock_sync, monkeypatch):
    monkeypatch.setenv("JIRA_EMAIL", "test@example.com")
    monkeypatch.setenv("JIRA_TOKEN", "fake-token")
    mock_sync.return_value = {"synced": 5, "created": 3, "updated": 2}
    from api.jira import handle_jira_sync
    result = handle_jira_sync("jira_test")
    assert result["synced"] == 5
    mock_sync.assert_called_once_with("jira_test")


def test_jira_status_returns_sources(monkeypatch, tmp_projects_file):
    from api.jira import get_sources_status
    result = get_sources_status()
    assert len(result) == 1
    assert result[0]["source_id"] == "jira_test"
    assert result[0]["sync_status"] == "idle"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_jira.py::test_sync_route_triggers_sync -v`
Expected: FAIL — `ImportError: cannot import name 'handle_jira_sync'`

- [ ] **Step 3: Add route handler functions to api/jira.py**

Append to `api/jira.py`:
```python
# ── Route handlers ─────────────────────────────────────────────────────────

def handle_jira_sync(source_id: str) -> dict:
    """Trigger manual sync for a source. Returns sync stats."""
    return sync_source(source_id)


def get_sources_status() -> list[dict]:
    """Return sync status for all Jira sources (no credentials exposed)."""
    from api.projects import load_project_store
    store = load_project_store()
    return [
        {
            "source_id": s["source_id"],
            "name": s.get("name", ""),
            "base_url": s.get("base_url", ""),
            "project_key": s.get("project_key", ""),
            "sync_enabled": s.get("sync_enabled", False),
            "sync_mode": s.get("sync_mode", "read"),
            "last_sync_at": s.get("last_sync_at"),
            "sync_status": s.get("sync_status", "idle"),
            "sync_error": s.get("sync_error"),
        }
        for s in store["sources"]
        if s.get("type") == "jira"
    ]


def get_issues_for_source(source_id: str, max_results: int = 20) -> list[dict]:
    """Proxy: fetch recent issues from Jira for display (not persisted)."""
    from api.projects import load_project_store
    store = load_project_store()
    source = next((s for s in store["sources"] if s["source_id"] == source_id), None)
    if not source or source["type"] != "jira":
        raise ValueError(f"Source {source_id} not found or not Jira")
    client = JiraClient(base_url=source["base_url"])
    jql = f'project = "{source["project_key"]}" ORDER BY updated DESC'
    issues = client.search_issues(jql, max_results=max_results)
    return [
        {
            "key": i["key"],
            "summary": i.get("fields", {}).get("summary", ""),
            "status": i.get("fields", {}).get("status", {}).get("name", ""),
            "assignee": (i.get("fields", {}).get("assignee") or {}).get("displayName", ""),
            "priority": i.get("fields", {}).get("priority", {}).get("name", ""),
            "url": f"{source['base_url']}/browse/{i['key']}",
        }
        for i in issues
    ]


def handle_create_issue(source_id: str, summary: str) -> dict:
    """Create an issue in Jira and return key + URL."""
    from api.projects import load_project_store
    store = load_project_store()
    source = next((s for s in store["sources"] if s["source_id"] == source_id), None)
    if not source or source["type"] != "jira":
        raise ValueError(f"Source {source_id} not found or not Jira")
    client = JiraClient(base_url=source["base_url"])
    result = client.create_issue(source["project_key"], summary)
    key = result.get("key", "")
    return {
        "key": key,
        "url": f"{source['base_url']}/browse/{key}",
        "self": result.get("self", ""),
    }
```

- [ ] **Step 4: Register routes in api/routes.py**

In `api/routes.py`, add import near line 497 (where `neo_projects` is imported):
```python
from api import jira as neo_jira
```

In `handle_get` (after the `/api/projects` block around line 1243), add:
```python
    if parsed.path == "/api/jira/status":
        try:
            return j(handler, {"sources": neo_jira.get_sources_status()})
        except Exception as e:
            logger.exception("Jira status failed")
            return bad(handler, str(e))

    if parsed.path.startswith("/api/jira/issues/"):
        source_id = parsed.path[len("/api/jira/issues/"):]
        if not source_id or "/" in source_id:
            return bad(handler, "Invalid source_id", 404)
        try:
            issues = neo_jira.get_issues_for_source(source_id)
            return j(handler, {"issues": issues})
        except ValueError as e:
            return bad(handler, str(e), 404)
        except Exception as e:
            logger.exception("Jira issues fetch failed")
            return bad(handler, str(e))
```

In `handle_post` (after the project-tasks block around line 2214), add:
```python
    if parsed.path.startswith("/api/jira/sync/"):
        source_id = parsed.path[len("/api/jira/sync/"):]
        if not source_id or "/" in source_id:
            return bad(handler, "Invalid source_id", 404)
        try:
            result = neo_jira.handle_jira_sync(source_id)
            return j(handler, {"ok": True, **result})
        except ValueError as e:
            return bad(handler, str(e), 404)
        except Exception as e:
            logger.exception("Jira sync failed")
            return bad(handler, str(e))

    if parsed.path == "/api/jira/create-issue":
        try:
            require(body, "source_id", "summary")
        except ValueError as e:
            return bad(handler, str(e))
        try:
            result = neo_jira.handle_create_issue(body["source_id"], body["summary"])
            return j(handler, {"ok": True, **result})
        except ValueError as e:
            return bad(handler, str(e), 404)
        except Exception as e:
            logger.exception("Jira create issue failed")
            return bad(handler, str(e))
```

- [ ] **Step 5: Run tests**

Run: `.venv/bin/pytest tests/test_jira.py tests/test_jira_sync.py -v`
Expected: All pass

- [ ] **Step 6: Commit**

```bash
git add api/jira.py api/routes.py tests/test_jira.py
git commit -m "feat(jira): register API routes for sync, issues, create-issue

POST /api/jira/sync/{source_id}
GET /api/jira/issues/{source_id}
GET /api/jira/status
POST /api/jira/create-issue"
```

---

## Task 5: Project Summary Endpoint

**Files:**
- Modify: `api/projects.py` (add `project_summary()`)
- Modify: `api/routes.py` (register GET route)
- Create: `tests/test_project_summary.py`

- [ ] **Step 1: Write failing test for project_summary**

Create `tests/test_project_summary.py`:
```python
import json
import pytest
from pathlib import Path


@pytest.fixture
def tmp_projects_file(tmp_path, monkeypatch):
    pf = tmp_path / "projects.json"
    store = {
        "schema_version": 2,
        "sources": [{
            "source_id": "jira_test",
            "type": "jira",
            "name": "Test Jira",
            "base_url": "https://test.atlassian.net",
            "project_key": "TST",
            "sync_enabled": True,
            "sync_mode": "read",
            "status_map": {},
            "last_sync_at": "2026-05-14T21:00:00Z",
            "sync_status": "ok",
            "sync_error": None,
        }],
        "projects": [{
            "project_id": "prj_abc",
            "name": "Alpha",
            "description": "Test project",
            "domain": "projetos",
            "status": "ativo",
            "color": "#FF5722",
            "default_source_id": "jira_test",
            "refs": {"github": ["https://github.com/test/alpha"], "obsidian": [], "sessions": []},
            "created_at": 1700000000,
            "updated_at": 1700000000,
            "archived": False,
        }],
        "tasks": [
            {"task_id": "tsk_1", "project_id": "prj_abc", "title": "Task A", "description": "", "status": "backlog", "priority": "media", "category": "Backend", "owner": "jr", "progress": 0, "due_date": "", "external_ref": None, "refs": {"github": [], "obsidian": [], "sessions": []}, "created_at": 1700000100, "updated_at": 1700000200, "archived": False},
            {"task_id": "tsk_2", "project_id": "prj_abc", "title": "Task B", "description": "", "status": "em_andamento", "priority": "alta", "category": "Frontend", "owner": "jr", "progress": 50, "due_date": "", "external_ref": None, "refs": {"github": [], "obsidian": [], "sessions": []}, "created_at": 1700000300, "updated_at": 1700000400, "archived": False},
            {"task_id": "tsk_3", "project_id": "prj_abc", "title": "Task C", "description": "", "status": "concluido", "priority": "baixa", "category": "Docs", "owner": "jr", "progress": 100, "due_date": "", "external_ref": None, "refs": {"github": [], "obsidian": [], "sessions": []}, "created_at": 1700000500, "updated_at": 1700000600, "archived": False},
        ],
        "activity": [],
    }
    pf.write_text(json.dumps(store))
    monkeypatch.setattr("api.config.PROJECTS_FILE", pf)
    return pf


def test_project_summary_stats(tmp_projects_file):
    from api.projects import project_summary
    result = project_summary("prj_abc")
    assert result["project"]["name"] == "Alpha"
    assert result["stats"]["total"] == 3
    assert result["stats"]["by_status"]["backlog"] == 1
    assert result["stats"]["by_status"]["em_andamento"] == 1
    assert result["stats"]["by_status"]["concluido"] == 1
    assert result["stats"]["completion_pct"] == pytest.approx(33.3, abs=0.1)


def test_project_summary_recent_tasks(tmp_projects_file):
    from api.projects import project_summary
    result = project_summary("prj_abc")
    assert len(result["recent_tasks"]) == 3
    assert result["recent_tasks"][0]["task_id"] == "tsk_3"


def test_project_summary_not_found(tmp_projects_file):
    from api.projects import project_summary
    with pytest.raises(KeyError):
        project_summary("prj_nonexistent")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_project_summary.py -v`
Expected: FAIL — `ImportError: cannot import name 'project_summary'`

- [ ] **Step 3: Implement project_summary in api/projects.py**

Add after the `snapshot()` function (around line 198):
```python
def project_summary(project_id: str) -> dict:
    """Return summary data for a single project: info, stats, recent tasks."""
    store = load_project_store()
    project = next((p for p in store["projects"] if p["project_id"] == project_id), None)
    if not project:
        raise KeyError("Project not found")

    tasks = [t for t in store["tasks"] if t["project_id"] == project_id and not t.get("archived")]
    by_status = {"backlog": 0, "em_andamento": 0, "em_revisao": 0, "concluido": 0}
    for t in tasks:
        by_status[t["status"]] = by_status.get(t["status"], 0) + 1
    total = sum(by_status.values())
    completion_pct = round((by_status["concluido"] / total) * 100, 1) if total > 0 else 0.0

    recent_tasks = sorted(tasks, key=lambda t: t.get("updated_at", 0), reverse=True)[:10]
    last_activity = recent_tasks[0]["updated_at"] if recent_tasks else None

    source = next((s for s in store["sources"] if s["source_id"] == project.get("default_source_id")), None)

    return {
        "project": project,
        "stats": {
            "total": total,
            "by_status": by_status,
            "completion_pct": completion_pct,
            "last_activity": last_activity,
        },
        "recent_tasks": [
            {"task_id": t["task_id"], "title": t["title"], "status": t["status"],
             "priority": t["priority"], "external_ref": t.get("external_ref"), "updated_at": t["updated_at"]}
            for t in recent_tasks
        ],
        "source": {
            "source_id": source["source_id"],
            "name": source["name"],
            "sync_status": source.get("sync_status", "idle"),
            "last_sync_at": source.get("last_sync_at"),
        } if source else None,
    }
```

- [ ] **Step 4: Register route in api/routes.py**

In `handle_get`, after the `/api/jira/issues/` block, add:
```python
    if parsed.path.startswith("/api/projects/") and parsed.path.endswith("/summary"):
        project_id = parsed.path[len("/api/projects/"):-len("/summary")]
        if not project_id or "/" in project_id:
            return bad(handler, "Invalid project_id", 404)
        try:
            return j(handler, neo_projects.project_summary(project_id))
        except KeyError:
            return bad(handler, "Project not found", 404)
        except Exception as e:
            logger.exception("Project summary failed")
            return bad(handler, str(e))
```

- [ ] **Step 5: Run tests**

Run: `.venv/bin/pytest tests/test_project_summary.py -v`
Expected: 3 passed

- [ ] **Step 6: Commit**

```bash
git add api/projects.py api/routes.py tests/test_project_summary.py
git commit -m "feat(projects): add project_summary endpoint

GET /api/projects/{id}/summary returns stats, recent tasks, source info.
Completion % calculated from active tasks only."
```

---

## Task 6: i18n Keys for Summary + Jira

**Files:**
- Modify: `static/i18n.js` (add keys to both `en` and `pt-BR` locales)

- [ ] **Step 1: Add English keys**

In `static/i18n.js`, inside the `en` locale object, add after the existing `projects_*` keys:
```javascript
    // Projects — Summary view
    projects_view_summary: 'Summary',
    projects_summary_info: 'Project Info',
    projects_summary_kpis: 'KPIs',
    projects_summary_links: 'External Links',
    projects_summary_recent: 'Recent Activity',
    projects_summary_jira_issues: 'Jira Issues',
    projects_summary_completion: 'completed',
    projects_summary_no_project: 'Select a project to see its summary',
    projects_summary_no_tasks: 'No tasks yet',
    projects_summary_domain: 'Domain',
    projects_summary_status: 'Status',
    projects_summary_created: 'Created',
    projects_summary_source: 'Source',
    // Jira integration
    jira_sync: 'Sync',
    jira_syncing: 'Syncing…',
    jira_sync_success: 'Sync complete',
    jira_sync_error: 'Sync failed',
    jira_last_sync: 'Last sync',
    jira_create_issue: 'Create in Jira',
    jira_create_success: 'Issue created',
    jira_chip: 'JIRA',
    jira_never_synced: 'Never synced',
```

- [ ] **Step 2: Add Portuguese keys**

In `static/i18n.js`, inside the `pt-BR` (or `pt`) locale object, add:
```javascript
    // Projects — Summary view
    projects_view_summary: 'Resumo',
    projects_summary_info: 'Info do Projeto',
    projects_summary_kpis: 'KPIs',
    projects_summary_links: 'Links Externos',
    projects_summary_recent: 'Atividade Recente',
    projects_summary_jira_issues: 'Issues Jira',
    projects_summary_completion: 'concluído',
    projects_summary_no_project: 'Selecione um projeto para ver o resumo',
    projects_summary_no_tasks: 'Nenhuma tarefa ainda',
    projects_summary_domain: 'Domínio',
    projects_summary_status: 'Status',
    projects_summary_created: 'Criado em',
    projects_summary_source: 'Fonte',
    // Jira integration
    jira_sync: 'Sincronizar',
    jira_syncing: 'Sincronizando…',
    jira_sync_success: 'Sincronização concluída',
    jira_sync_error: 'Falha na sincronização',
    jira_last_sync: 'Última sincronização',
    jira_create_issue: 'Criar no Jira',
    jira_create_success: 'Issue criada',
    jira_chip: 'JIRA',
    jira_never_synced: 'Nunca sincronizado',
```

- [ ] **Step 3: Verify no syntax errors**

Run: `node -c /home/jrmelo/Projetos/neo-webui/static/i18n.js`
Expected: exits 0 (no syntax error)

- [ ] **Step 4: Commit**

```bash
git add static/i18n.js
git commit -m "feat(i18n): add summary + jira keys for en and pt-BR"
```

---

## Task 7: Frontend — Summary View (Resumo Tab)

**Files:**
- Modify: `static/kanban.js` (add summary view rendering + tab)
- Modify: `static/index.html` (add view toggle button for "Resumo")

- [ ] **Step 1: Add "Resumo" view toggle button in index.html**

Find the projects view toggle buttons in `static/index.html` (the `.projects-view-btn` buttons). Add a third button after the existing Kanban and Lista buttons:
```html
<button class="projects-view-btn" data-view="summary" aria-selected="false" data-i18n="projects_view_summary">Resumo</button>
```

- [ ] **Step 2: Add summary container in index.html**

After the `#projectsList` container div, add:
```html
<div id="projectsSummary" class="projects-summary" hidden></div>
```

- [ ] **Step 3: Update _setView in kanban.js to handle 'summary'**

Replace the `_setView` function (line ~914):
```javascript
  function _setView(view) {
    const valid = ['kanban', 'list', 'summary'];
    state.view = valid.includes(view) ? view : 'kanban';
    document.querySelectorAll('.projects-view-btn').forEach(btn => {
      const active = btn.dataset.view === state.view;
      btn.classList.toggle('active', active);
      btn.setAttribute('aria-selected', active ? 'true' : 'false');
    });
    renderAll();
  }
```

- [ ] **Step 4: Update renderEmptyState to handle summary container**

Replace `renderEmptyState` (line ~501):
```javascript
  function renderEmptyState() {
    const empty = $id('projectsEmptyState');
    const kanban = $id('projectsKanban');
    const list = $id('projectsList');
    const summary = $id('projectsSummary');
    const noProjects = state.projects.filter(p => !p.archived).length === 0;
    if (empty) empty.hidden = !noProjects;
    if (kanban) kanban.hidden = state.view !== 'kanban';
    if (list) list.hidden = state.view !== 'list';
    if (summary) summary.hidden = state.view !== 'summary';
  }
```

- [ ] **Step 5: Update renderAll to call renderSummary**

Replace `renderAll` (line ~513):
```javascript
  function renderAll() {
    renderEmptyState();
    renderStatusPills();
    if (state.view === 'kanban') renderKanban();
    else if (state.view === 'list') renderList();
    else if (state.view === 'summary') renderSummary();
  }
```

- [ ] **Step 6: Implement renderSummary function**

Add before the `renderAll` function:
```javascript
  // ── Summary view ────────────────────────────────────────────────────────
  async function renderSummary() {
    const container = $id('projectsSummary');
    if (!container) return;

    const selectedProjects = [...state.filters.project_id];

    if (selectedProjects.length === 1) {
      await _renderProjectDetail(container, selectedProjects[0]);
    } else {
      _renderProjectsGrid(container);
    }
  }

  function _renderProjectsGrid(container) {
    const active = state.projects.filter(p => !p.archived);
    if (!active.length) {
      container.innerHTML = `<p class="projects-summary-empty">${_esc(_t('projects_summary_no_tasks', 'Nenhuma tarefa ainda'))}</p>`;
      return;
    }
    const cards = active.map(p => {
      const tasks = state.tasks.filter(t => t.project_id === p.project_id && !t.archived);
      const done = tasks.filter(t => t.status === 'concluido').length;
      const pct = tasks.length ? Math.round((done / tasks.length) * 100) : 0;
      return `<article class="project-summary-card" data-project-id="${_esc(p.project_id)}" tabindex="0" role="button">
        <div class="project-summary-card-color" style="background:${_esc(p.color)}"></div>
        <h3>${_esc(p.name)}</h3>
        <p class="project-summary-card-desc">${_esc(p.description || p.domain)}</p>
        <div class="project-summary-card-bar">
          <div class="project-summary-card-bar-fill" style="width:${pct}%"></div>
        </div>
        <span class="project-summary-card-pct">${pct}% ${_esc(_t('projects_summary_completion', 'concluído'))}</span>
        <span class="project-summary-card-count">${tasks.length} tasks</span>
      </article>`;
    }).join('');
    container.innerHTML = `<div class="projects-summary-grid">${cards}</div>`;

    container.querySelectorAll('.project-summary-card').forEach(card => {
      card.addEventListener('click', () => {
        const pid = card.dataset.projectId;
        state.filters.project_id.clear();
        state.filters.project_id.add(pid);
        _updateFiltersBadge();
        renderAll();
      });
      card.addEventListener('keydown', ev => {
        if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); card.click(); }
      });
    });
  }

  async function _renderProjectDetail(container, projectId) {
    container.innerHTML = '<div class="projects-summary-loading">…</div>';
    try {
      const data = await _api('GET', `/api/projects/${encodeURIComponent(projectId)}/summary`);
      const p = data.project;
      const s = data.stats;
      const pct = s.completion_pct || 0;
      const createdDate = new Date(p.created_at * 1000).toLocaleDateString();

      const linksHtml = _renderSummaryLinks(p);
      const recentHtml = _renderRecentTasks(data.recent_tasks || []);
      const sourceHtml = data.source ? _renderSourceStatus(data.source) : '';

      container.innerHTML = `
        <div class="projects-summary-detail">
          <div class="projects-summary-row">
            <section class="projects-summary-info">
              <h3>${_esc(_t('projects_summary_info', 'Info do Projeto'))}</h3>
              <dl>
                <dt>${_esc(_t('projects_create_name', 'Nome'))}</dt><dd>${_esc(p.name)}</dd>
                <dt>${_esc(_t('projects_summary_domain', 'Domínio'))}</dt><dd>${_esc(p.domain)}</dd>
                <dt>${_esc(_t('projects_summary_status', 'Status'))}</dt><dd>${_esc(p.status)}</dd>
                <dt>${_esc(_t('projects_summary_created', 'Criado em'))}</dt><dd>${_esc(createdDate)}</dd>
                ${data.source ? `<dt>${_esc(_t('projects_summary_source', 'Fonte'))}</dt><dd>${_esc(data.source.name)}</dd>` : ''}
              </dl>
            </section>
            <section class="projects-summary-kpis">
              <h3>${_esc(_t('projects_summary_kpis', 'KPIs'))}</h3>
              <div class="projects-summary-bar">
                <div class="projects-summary-bar-fill" style="width:${pct}%"></div>
              </div>
              <span class="projects-summary-pct">${pct.toFixed(1)}% ${_esc(_t('projects_summary_completion', 'concluído'))}</span>
              <ul class="projects-summary-counts">
                <li>Backlog: ${s.by_status.backlog || 0}</li>
                <li>${_esc(_t('projects_col_in_progress', 'Em andamento'))}: ${s.by_status.em_andamento || 0}</li>
                <li>${_esc(_t('projects_col_in_review', 'Em revisão'))}: ${s.by_status.em_revisao || 0}</li>
                <li>${_esc(_t('projects_col_completed', 'Concluído'))}: ${s.by_status.concluido || 0}</li>
              </ul>
            </section>
          </div>
          ${linksHtml}
          ${recentHtml}
          ${sourceHtml}
        </div>`;
    } catch (err) {
      container.innerHTML = `<p class="projects-summary-error">${_esc(err.message)}</p>`;
    }
  }

  function _renderSummaryLinks(project) {
    const refs = project.refs || {};
    const links = [];
    (refs.github || []).forEach(url => links.push(`<a href="${_esc(url)}" target="_blank" rel="noopener">🔗 GitHub</a>`));
    (refs.obsidian || []).forEach(path => links.push(`<span>📓 ${_esc(path)}</span>`));
    if (!links.length) return '';
    return `<section class="projects-summary-links">
      <h3>${_esc(_t('projects_summary_links', 'Links Externos'))}</h3>
      <div class="projects-summary-links-list">${links.join('')}</div>
    </section>`;
  }

  function _renderRecentTasks(tasks) {
    if (!tasks.length) return '';
    const rows = tasks.map(t => {
      const extChip = t.external_ref && t.external_ref.key
        ? `<span class="kanban-card-ref">${_esc(t.external_ref.key)}</span>` : '';
      return `<li>${extChip} ${_esc(t.title)} <span class="projects-summary-task-status">[${_esc(_statusLabel(t.status))}]</span></li>`;
    }).join('');
    return `<section class="projects-summary-recent">
      <h3>${_esc(_t('projects_summary_recent', 'Atividade Recente'))}</h3>
      <ul>${rows}</ul>
    </section>`;
  }

  function _renderSourceStatus(source) {
    const lastSync = source.last_sync_at
      ? new Date(source.last_sync_at).toLocaleString()
      : _t('jira_never_synced', 'Nunca sincronizado');
    return `<section class="projects-summary-source">
      <h3>${_esc(_t('projects_summary_jira_issues', 'Issues Jira'))}</h3>
      <div class="projects-summary-source-header">
        <span>${_esc(_t('jira_last_sync', 'Última sincronização'))}: ${_esc(lastSync)}</span>
        <button class="btn-jira-sync" data-source-id="${_esc(source.source_id)}">${_esc(_t('jira_sync', 'Sincronizar'))}</button>
      </div>
    </section>`;
  }
```

- [ ] **Step 7: Bind sync button click in _bindOnce**

In the `_bindOnce` function, add after the existing event bindings:
```javascript
    // Jira sync button (delegated — button is rendered dynamically)
    document.addEventListener('click', async ev => {
      const btn = ev.target.closest('.btn-jira-sync');
      if (!btn) return;
      const sourceId = btn.dataset.sourceId;
      if (!sourceId) return;
      btn.disabled = true;
      btn.textContent = _t('jira_syncing', 'Sincronizando…');
      try {
        await _api('POST', `/api/jira/sync/${encodeURIComponent(sourceId)}`);
        _toast(_t('jira_sync_success', 'Sincronização concluída'), 'success');
        await fetchSnapshot();
        renderAll();
      } catch (err) {
        _toast(_t('jira_sync_error', 'Falha na sincronização') + ': ' + err.message, 'error');
      } finally {
        btn.disabled = false;
        btn.textContent = _t('jira_sync', 'Sincronizar');
      }
    });
```

- [ ] **Step 8: Verify in browser**

Start dev server, navigate to Projects, click "Resumo" tab.
- Without project filter: should show grid of project cards
- Click a card: should show detailed summary with KPIs

- [ ] **Step 9: Commit**

```bash
git add static/kanban.js static/index.html
git commit -m "feat(projects): add Summary view with project detail + grid

Third tab in Projects Command Center. Shows KPIs, links, recent
activity, and Jira source status with sync button."
```

---

## Task 8: Frontend — CSS for Summary + Jira

**Files:**
- Modify: `static/index.html` (inline `<style>` or separate CSS — follow existing pattern)

- [ ] **Step 1: Identify where CSS lives**

Check if styles are in `static/index.html` inline or a separate file. The project uses inline `<style>` blocks in index.html based on existing patterns.

- [ ] **Step 2: Add summary + jira CSS**

Add these styles to the existing `<style>` section:
```css
/* ── Projects Summary view ─────────────────────────────────── */
.projects-summary { padding: 1rem; }
.projects-summary-loading { text-align: center; padding: 2rem; opacity: .6; }
.projects-summary-error { color: var(--error); padding: 1rem; }
.projects-summary-empty { text-align: center; padding: 2rem; opacity: .6; }

.projects-summary-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
  gap: 1rem;
}
.project-summary-card {
  background: var(--surface-1, #1e1e2e);
  border-radius: 12px;
  padding: 1rem;
  cursor: pointer;
  transition: transform .15s, box-shadow .15s;
  position: relative;
  overflow: hidden;
}
.project-summary-card:hover,
.project-summary-card:focus-visible {
  transform: translateY(-2px);
  box-shadow: 0 4px 12px rgba(0,0,0,.3);
  outline: 2px solid var(--accent, #00E5FF);
}
.project-summary-card-color {
  position: absolute; top: 0; left: 0; right: 0; height: 4px;
}
.project-summary-card h3 { margin: .5rem 0 .25rem; font-size: .95rem; }
.project-summary-card-desc { font-size: .8rem; opacity: .7; margin-bottom: .75rem; }
.project-summary-card-bar {
  height: 6px; border-radius: 3px; background: var(--surface-2, #2a2a3e); overflow: hidden;
}
.project-summary-card-bar-fill {
  height: 100%; background: var(--accent, #00E5FF); border-radius: 3px;
  transition: width .3s;
}
.project-summary-card-pct { font-size: .75rem; opacity: .8; }
.project-summary-card-count { font-size: .75rem; opacity: .6; float: right; }

/* Detail view */
.projects-summary-detail { max-width: 900px; }
.projects-summary-row {
  display: grid; grid-template-columns: 1fr 1fr; gap: 1.5rem; margin-bottom: 1.5rem;
}
@media (max-width: 600px) {
  .projects-summary-row { grid-template-columns: 1fr; }
}
.projects-summary-info,
.projects-summary-kpis,
.projects-summary-links,
.projects-summary-recent,
.projects-summary-source {
  background: var(--surface-1, #1e1e2e);
  border-radius: 12px;
  padding: 1rem;
}
.projects-summary-info h3,
.projects-summary-kpis h3,
.projects-summary-links h3,
.projects-summary-recent h3,
.projects-summary-source h3 {
  font-size: .85rem; text-transform: uppercase; letter-spacing: .05em;
  opacity: .7; margin-bottom: .75rem;
}
.projects-summary-info dl { display: grid; grid-template-columns: auto 1fr; gap: .25rem .75rem; font-size: .85rem; }
.projects-summary-info dt { opacity: .6; }
.projects-summary-bar {
  height: 8px; border-radius: 4px; background: var(--surface-2, #2a2a3e); overflow: hidden; margin-bottom: .5rem;
}
.projects-summary-bar-fill {
  height: 100%; background: var(--accent, #00E5FF); border-radius: 4px; transition: width .3s;
}
.projects-summary-pct { font-size: .85rem; font-weight: 600; }
.projects-summary-counts { list-style: none; padding: 0; margin: .75rem 0 0; font-size: .8rem; }
.projects-summary-counts li { padding: .15rem 0; }
.projects-summary-links-list { display: flex; flex-wrap: wrap; gap: .5rem; }
.projects-summary-links-list a,
.projects-summary-links-list span { font-size: .8rem; }
.projects-summary-recent ul { list-style: none; padding: 0; margin: 0; font-size: .8rem; }
.projects-summary-recent li { padding: .3rem 0; border-bottom: 1px solid var(--surface-2, #2a2a3e); }
.projects-summary-task-status { opacity: .6; font-size: .75rem; }

/* Jira sync */
.projects-summary-source-header {
  display: flex; align-items: center; justify-content: space-between; gap: .5rem;
  font-size: .8rem;
}
.btn-jira-sync {
  background: var(--accent, #00E5FF); color: #000; border: none; border-radius: 6px;
  padding: .35rem .75rem; font-size: .75rem; font-weight: 600; cursor: pointer;
  transition: opacity .15s;
}
.btn-jira-sync:hover { opacity: .85; }
.btn-jira-sync:disabled { opacity: .5; cursor: not-allowed; }
```

- [ ] **Step 3: Verify no visual regressions**

Open browser, check Kanban and Lista views still render correctly. Check Summary view renders with proper spacing.

- [ ] **Step 4: Commit**

```bash
git add static/index.html
git commit -m "style(projects): add CSS for summary view + jira sync button"
```

---

## Task 9: Frontend — Jira Chip on Task Cards

**Files:**
- Modify: `static/kanban.js` (already renders `extLabel` — verify it works with synced tasks)

- [ ] **Step 1: Verify existing external_ref rendering**

The `_renderTaskCard` function (line ~236) already renders:
```javascript
const extLabel = ext && (ext.key || ext.type)
  ? `<span class="kanban-card-ref" data-ref-type="${_esc(ext.type || 'local')}">${_esc((ext.type || 'local').toUpperCase())}${ext.key ? ' · ' + _esc(ext.key) : ''}</span>`
  : '';
```

This already handles Jira tasks — when `external_ref.type === "jira"` and `external_ref.key === "KAN-123"`, it renders `JIRA · KAN-123`. No code change needed.

- [ ] **Step 2: Add clickable link to Jira issue**

Update the `extLabel` rendering to wrap in a link when URL exists:
```javascript
    const ext = task.external_ref;
    let extLabel = '';
    if (ext && (ext.key || ext.type)) {
      const chip = `<span class="kanban-card-ref" data-ref-type="${_esc(ext.type || 'local')}">${_esc((ext.type || 'local').toUpperCase())}${ext.key ? ' · ' + _esc(ext.key) : ''}</span>`;
      extLabel = ext.url
        ? `<a href="${_esc(ext.url)}" target="_blank" rel="noopener" class="kanban-card-ref-link">${chip}</a>`
        : chip;
    }
```

- [ ] **Step 3: Add CSS for ref link**

```css
.kanban-card-ref-link { text-decoration: none; }
.kanban-card-ref-link:hover .kanban-card-ref { opacity: .8; text-decoration: underline; }
```

- [ ] **Step 4: Verify in browser**

Sync a Jira source, check that task cards show "JIRA · KAN-123" chip that links to Jira.

- [ ] **Step 5: Commit**

```bash
git add static/kanban.js static/index.html
git commit -m "feat(projects): make Jira chip on task cards clickable

Links to the issue URL on Atlassian. Existing local tasks unaffected."
```

---

## Task 10: Integration Test — Full Sync Flow

**Files:**
- Create: `tests/test_jira_integration.py`

- [ ] **Step 1: Write integration test**

Create `tests/test_jira_integration.py`:
```python
"""Integration test: full Jira sync → summary → verify data flow."""
import json
import pytest
from unittest.mock import patch
from pathlib import Path


@pytest.fixture
def full_store(tmp_path, monkeypatch):
    pf = tmp_path / "projects.json"
    store = {
        "schema_version": 2,
        "sources": [{
            "source_id": "jira_cotin",
            "type": "jira",
            "name": "Jira COTIN/DELOG",
            "base_url": "https://cotin5.atlassian.net",
            "project_key": "KAN",
            "sync_enabled": True,
            "sync_mode": "read",
            "status_map": {
                "A fazer": "backlog",
                "Em andamento": "em_andamento",
                "SUSPENSA": "em_revisao",
                "Concluído": "concluido",
            },
            "last_sync_at": None,
            "sync_status": "idle",
            "sync_error": None,
        }],
        "projects": [{
            "project_id": "prj_cotin",
            "name": "COTIN/DELOG",
            "description": "MGI project",
            "domain": "governo",
            "status": "ativo",
            "color": "#4CAF50",
            "default_source_id": "jira_cotin",
            "refs": {"github": [], "obsidian": [], "sessions": []},
            "created_at": 1700000000,
            "updated_at": 1700000000,
            "archived": False,
        }],
        "tasks": [],
        "activity": [],
    }
    pf.write_text(json.dumps(store))
    monkeypatch.setattr("api.config.PROJECTS_FILE", pf)
    monkeypatch.setenv("JIRA_EMAIL", "test@example.com")
    monkeypatch.setenv("JIRA_TOKEN", "fake-token")
    return pf


@patch("api.jira.JiraClient.search_issues")
def test_full_sync_then_summary(mock_search, full_store):
    mock_search.return_value = [
        {"key": "KAN-315", "fields": {"summary": "Deploy staging", "status": {"name": "A fazer"}, "assignee": None, "priority": {"name": "Medium"}, "updated": "2026-05-14T10:00:00.000+0000", "created": "2026-05-10T08:00:00.000+0000"}, "self": "https://cotin5.atlassian.net/rest/api/3/issue/1"},
        {"key": "KAN-316", "fields": {"summary": "Fix auth flow", "status": {"name": "Concluído"}, "assignee": {"displayName": "Junior"}, "priority": {"name": "High"}, "updated": "2026-05-14T12:00:00.000+0000", "created": "2026-05-11T09:00:00.000+0000"}, "self": "https://cotin5.atlassian.net/rest/api/3/issue/2"},
        {"key": "KAN-317", "fields": {"summary": "Preparar iframes", "status": {"name": "Em andamento"}, "assignee": {"displayName": "Junior"}, "priority": {"name": "High"}, "updated": "2026-05-14T14:00:00.000+0000", "created": "2026-05-12T10:00:00.000+0000"}, "self": "https://cotin5.atlassian.net/rest/api/3/issue/3"},
    ]

    from api.jira import sync_source
    result = sync_source("jira_cotin")
    assert result["synced"] == 3
    assert result["created"] == 3

    from api.projects import project_summary
    summary = project_summary("prj_cotin")
    assert summary["stats"]["total"] == 3
    assert summary["stats"]["by_status"]["backlog"] == 1
    assert summary["stats"]["by_status"]["em_andamento"] == 1
    assert summary["stats"]["by_status"]["concluido"] == 1
    assert summary["stats"]["completion_pct"] == pytest.approx(33.3, abs=0.1)
    assert summary["source"]["sync_status"] == "ok"
    assert len(summary["recent_tasks"]) == 3
```

- [ ] **Step 2: Run test**

Run: `.venv/bin/pytest tests/test_jira_integration.py -v`
Expected: 1 passed

- [ ] **Step 3: Run full test suite**

Run: `.venv/bin/pytest tests/ -v --timeout=30`
Expected: All tests pass (no regressions)

- [ ] **Step 4: Commit**

```bash
git add tests/test_jira_integration.py
git commit -m "test(jira): add integration test for sync → summary flow

Verifies status mapping, task creation, and summary stats after sync."
```

---

## Execution Order Summary

| Task | Depends on | Deliverable |
|------|-----------|-------------|
| 1 | — | Sidebar cleanup |
| 2 | — | JiraClient class |
| 3 | 2 | sync_source logic |
| 4 | 3 | API routes registered |
| 5 | — | project_summary endpoint |
| 6 | — | i18n keys |
| 7 | 5, 6 | Summary view UI |
| 8 | 7 | CSS styling |
| 9 | 4 | Clickable Jira chips |
| 10 | 3, 5 | Integration test |

**Parallelizable:** Tasks 1, 2, 5, 6 can run in parallel (no deps).
