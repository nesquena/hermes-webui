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
    import uuid
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
