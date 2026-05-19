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
_MAX_RATE_LIMIT_RETRIES = 3
_MAX_SEARCH_PAGES = 20


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
        for attempt in range(_MAX_RATE_LIMIT_RETRIES + 1):
            try:
                with urllib.request.urlopen(req, timeout=_REQUEST_TIMEOUT) as resp:
                    raw = resp.read()
                    return json.loads(raw) if raw else {}
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    if attempt >= _MAX_RATE_LIMIT_RETRIES:
                        raise RuntimeError(
                            f"Jira rate limit persisted after {_MAX_RATE_LIMIT_RETRIES} retries"
                        ) from e
                    try:
                        retry_after = int(e.headers.get("Retry-After", "5"))
                    except (TypeError, ValueError):
                        retry_after = 5
                    logger.warning(
                        "Jira rate limited, retrying in %ds (%d/%d)",
                        retry_after,
                        attempt + 1,
                        _MAX_RATE_LIMIT_RETRIES,
                    )
                    time.sleep(min(max(retry_after, 0), 60))
                    continue
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
        raise RuntimeError("Jira request failed unexpectedly")

    def search_issues(self, jql: str, max_results: int = 50) -> list[dict]:
        issues: list[dict] = []
        next_page_token: str | None = None
        fields = [
            "summary",
            "status",
            "assignee",
            "priority",
            "updated",
            "created",
            "issuetype",
            "parent",
            "components",
            "labels",
            "issuelinks",
        ]
        for page in range(_MAX_SEARCH_PAGES):
            payload: dict[str, Any] = {
                "jql": jql,
                "maxResults": max_results,
                "fields": fields,
            }
            if next_page_token:
                payload["nextPageToken"] = next_page_token
            data = self._request("POST", "/search/jql", payload)
            issues.extend(data.get("issues", []))
            next_page_token = data.get("nextPageToken")
            if not next_page_token:
                break
        else:
            logger.warning("Jira search stopped after %d pages for JQL: %s", _MAX_SEARCH_PAGES, jql)
        return issues

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


def _sync_error_code(message: str) -> str:
    if message == "sync already running":
        return "sync_in_progress"
    if " is disabled" in message:
        return "source_disabled"
    if message.startswith("Source ") and " not found" in message:
        return "source_not_found"
    return "sync_failed"


def _safe_progress(value: Any) -> int:
    try:
        return max(0, min(100, int(value or 0)))
    except (TypeError, ValueError):
        return 0


def _slug(value: str) -> str:
    cleaned = "".join(ch.lower() if ch.isalnum() else "_" for ch in value.strip())
    return "_".join(part for part in cleaned.split("_") if part)[:48] or "jira"


def _issue_type_name(issue: dict) -> str:
    return str(issue.get("fields", {}).get("issuetype", {}).get("name") or "").strip()


def _issue_summary(issue: dict, fallback: str = "") -> str:
    return str(issue.get("fields", {}).get("summary") or fallback or issue.get("key") or "Jira").strip()


def _is_epic_type_name(name: str) -> bool:
    normalized = name.strip().lower().replace("é", "e")
    return normalized in {"epic", "epico"}


def _is_epic(issue: dict) -> bool:
    return _is_epic_type_name(_issue_type_name(issue))


def _jira_ref(base_url: str, source_id: str, key: str, *, grouping: str, name: str = "") -> dict:
    return {
        "type": "jira",
        "source_id": source_id,
        "key": key,
        "url": f"{base_url}/browse/{key}" if key else "",
        "status": "",
        "synced_at": None,
        "grouping": grouping,
        "name": name,
    }


def _find_project_by_external_ref(store: dict, source_id: str, key: str, grouping: str | None = None) -> dict | None:
    for project in store["projects"]:
        ref = project.get("external_ref") or {}
        if ref.get("type") == "jira" and ref.get("source_id") == source_id and ref.get("key") == key:
            if grouping is None or ref.get("grouping") == grouping:
                return project
    return None


def _issue_linked_keys(issue: dict | None) -> set[str]:
    keys: set[str] = set()
    if not isinstance(issue, dict):
        return keys
    links = issue.get("fields", {}).get("issuelinks")
    if not isinstance(links, list):
        return keys
    for link in links:
        if not isinstance(link, dict):
            continue
        linked = link.get("outwardIssue") or link.get("inwardIssue")
        if isinstance(linked, dict) and linked.get("key"):
            keys.add(str(linked["key"]).strip().lower())
    return keys


def _resolve_project_group(source_config: dict, key: str, name: str, issue: dict | None = None) -> dict | None:
    """Return configured business-level grouping for a Jira issue/epic."""
    groups = source_config.get("project_groups")
    if not isinstance(groups, list):
        return None
    key_norm = key.strip().lower()
    name_norm = name.strip().lower()
    linked_issue_keys = _issue_linked_keys(issue)
    for group in groups:
        if not isinstance(group, dict):
            continue
        keys = {str(item).strip().lower() for item in group.get("keys", []) if str(item).strip()}
        if key_norm and key_norm in keys:
            return group
        linked_keys = {str(item).strip().lower() for item in group.get("linked_keys", []) if str(item).strip()}
        if linked_keys and linked_issue_keys.intersection(linked_keys):
            return group
        contains = [str(item).strip().lower() for item in group.get("name_contains", []) if str(item).strip()]
        if any(token in name_norm for token in contains):
            return group
        prefixes = [str(item).strip().lower() for item in group.get("name_prefixes", []) if str(item).strip()]
        if any(name_norm.startswith(prefix) for prefix in prefixes):
            return group
    return None


def _project_by_id(store: dict, project_id: str) -> dict | None:
    return next((p for p in store["projects"] if p.get("project_id") == project_id), None)


def _merge_jira_ref(project: dict, ref: dict) -> None:
    project["default_source_id"] = project.get("default_source_id") or ref.get("source_id")
    project["external_ref"] = {**(project.get("external_ref") or {}), **ref}
    refs = project.setdefault("refs", {})
    jira_refs = refs.setdefault("jira", [])
    if ref.get("url") and ref["url"] not in jira_refs:
        jira_refs.append(ref["url"])


def _ensure_jira_project(
    store: dict,
    *,
    source_config: dict,
    key: str,
    name: str,
    grouping: str,
    domain: str,
    issue: dict | None = None,
) -> tuple[dict, bool]:
    import uuid

    source_id = source_config["source_id"]
    base_url = source_config["base_url"]
    now = time.time()
    group = _resolve_project_group(source_config, key, name, issue) if grouping in {"epic", "parent"} else None
    if group:
        group_name = str(group.get("name") or name or key).strip()
        group_key = str(group.get("key") or group_name or key).strip()
        grouping = "group"
        existing = None
        if group.get("project_id"):
            existing = _project_by_id(store, str(group.get("project_id")))
        existing = existing or _find_project_by_external_ref(store, source_id, group_key, grouping)
        ref = _jira_ref(base_url, source_id, group_key, grouping=grouping, name=group_name)
        ref["parent_key"] = key
        ref["parent_url"] = f"{base_url}/browse/{key}" if key else ""
        if existing:
            if group_name and existing.get("name") != group_name[:128]:
                existing["name"] = group_name[:128]
            _merge_jira_ref(existing, ref)
            existing["updated_at"] = now
            return existing, False
        key = group_key
        name = group_name

    existing = _find_project_by_external_ref(store, source_id, key, grouping)
    if not existing and grouping == "epic":
        existing = _find_project_by_external_ref(store, source_id, key, None)
    if existing:
        if name and existing.get("name") != name[:128]:
            existing["name"] = name[:128]
            existing["updated_at"] = now
        return existing, False

    project = {
        "project_id": f"prj_jira_{_slug(source_id)}_{_slug(key or name)}_{uuid.uuid4().hex[:6]}",
        "name": name[:128] or key,
        "description": f"Projeto descoberto automaticamente via Jira ({grouping}).",
        "domain": domain,
        "status": "ativo",
        "color": source_config.get("project_color") or "#00E5FF",
        "default_source_id": source_id,
        "external_ref": _jira_ref(base_url, source_id, key, grouping=grouping, name=name),
        "refs": {"github": [], "obsidian": [], "sessions": [], "jira": [f"{base_url}/browse/{key}"] if key else []},
        "created_at": now,
        "updated_at": now,
        "archived": False,
    }
    store["projects"].append(project)
    return project, True


def _linked_or_inbox_project(store: dict, source_config: dict) -> dict:
    source_id = source_config["source_id"]
    default_project_id = str(source_config.get("default_project_id") or "").strip()
    if default_project_id:
        project = _project_by_id(store, default_project_id)
        if project:
            return project
    linked = next((p for p in store["projects"] if p.get("default_source_id") == source_id), None)
    if linked:
        return linked
    project, _ = _ensure_jira_project(
        store,
        source_config=source_config,
        key=f"{source_id}:inbox",
        name=f"{source_config.get('name') or source_id} - Inbox",
        grouping="inbox",
        domain=source_config.get("domain") or "projetos",
    )
    return project


def _project_for_issue(
    store: dict,
    source_config: dict,
    issue: dict,
    issue_lookup: dict[str, dict],
    *,
    depth: int = 0,
) -> tuple[dict, bool]:
    fields = issue.get("fields", {})
    domain = source_config.get("domain") or "projetos"

    parent = fields.get("parent") if isinstance(fields.get("parent"), dict) else None
    if parent and parent.get("key"):
        parent_key = str(parent.get("key"))
        parent_fields = parent.get("fields") if isinstance(parent.get("fields"), dict) else {}
        parent_type = str(parent_fields.get("issuetype", {}).get("name") or "") if isinstance(parent_fields.get("issuetype"), dict) else ""

        # Subtasks often point to a Task, and that Task points to the Epic.
        # If the parent issue is in the current page, climb once/twice to the
        # highest business grouping instead of creating projects for every task.
        parent_issue = issue_lookup.get(parent_key)
        if parent_issue and not _is_epic_type_name(parent_type) and depth < 4:
            grandparent = parent_issue.get("fields", {}).get("parent")
            if isinstance(grandparent, dict) and grandparent.get("key"):
                return _project_for_issue(store, source_config, parent_issue, issue_lookup, depth=depth + 1)

        parent_summary = str(parent_fields.get("summary") or "").strip()
        if not parent_summary and parent_issue:
            parent_summary = _issue_summary(parent_issue, parent_key)
        return _ensure_jira_project(
            store,
            source_config=source_config,
            key=parent_key,
            name=parent_summary or parent_key,
            grouping="epic" if _is_epic_type_name(parent_type) or (parent_issue and _is_epic(parent_issue)) else "parent",
            domain=domain,
            issue=parent_issue,
        )

    direct_group = _resolve_project_group(source_config, str(issue.get("key") or ""), _issue_summary(issue), issue)
    if direct_group:
        return _ensure_jira_project(
            store,
            source_config=source_config,
            key=str(issue.get("key") or ""),
            name=_issue_summary(issue),
            grouping="parent",
            domain=domain,
            issue=issue,
        )

    components = fields.get("components") if isinstance(fields.get("components"), list) else []
    first_component = next((c for c in components if isinstance(c, dict) and str(c.get("name") or "").strip()), None)
    if first_component:
        component_name = str(first_component.get("name")).strip()
        return _ensure_jira_project(
            store,
            source_config=source_config,
            key=f"component:{component_name}",
            name=component_name,
            grouping="component",
            domain=domain,
        )

    # Labels are useful metadata, but too noisy as project boundaries (ADM,
    # 2026, API, etc.). Business grouping stays on Epic/Parent first,
    # Component second, and otherwise falls back to the linked source project.
    return _linked_or_inbox_project(store, source_config), False


def sync_source(source_id: str) -> dict:
    """Sync issues from a Jira source into local tasks. Returns stats."""
    import uuid
    from api.projects import load_project_store, project_store_lock, save_project_store

    lock = _get_sync_lock(source_id)
    if not lock.acquire(blocking=False):
        return {
            "error": "sync already running",
            "error_code": "sync_in_progress",
            "synced": 0,
            "created": 0,
            "updated": 0,
        }

    try:
        with project_store_lock():
            store = load_project_store()
            source = next((s for s in store["sources"] if s["source_id"] == source_id), None)
            if not source:
                raise ValueError(f"Source {source_id} not found")
            if source["type"] != "jira":
                raise ValueError(f"Source {source_id} is not a Jira source")
            if not source.get("sync_enabled", False):
                raise ValueError(f"Jira source {source_id} is disabled")

            source_config = dict(source)
            source_config["source_id"] = source_id
            source_config.setdefault("domain", "projetos")

        client = JiraClient(base_url=source_config["base_url"])
        jql = f'project = "{source_config["project_key"]}" ORDER BY updated DESC'
        issues = client.search_issues(jql, max_results=50)

        status_map = source_config.get("status_map") or {}
        created = 0
        updated = 0
        projects_created = 0
        projects_updated = 0
        issue_lookup = {issue["key"]: issue for issue in issues if issue.get("key")}

        with project_store_lock():
            store = load_project_store()
            source = next((s for s in store["sources"] if s["source_id"] == source_id), None)
            if not source:
                raise ValueError(f"Source {source_id} not found")
            if not source.get("sync_enabled", False):
                raise ValueError(f"Jira source {source_id} is disabled")

            for issue in issues:
                key = issue["key"]
                fields = issue.get("fields", {})
                jira_status = fields.get("status", {}).get("name", "")
                local_status = status_map.get(jira_status, "backlog")
                summary = fields.get("summary", "")
                issue_url = f"{source_config['base_url']}/browse/{key}"
                now = time.time()

                if _is_epic(issue):
                    project, was_created = _ensure_jira_project(
                        store,
                        source_config=source_config,
                        key=key,
                        name=_issue_summary(issue, key),
                        grouping="epic",
                        domain=source_config.get("domain") or "projetos",
                        issue=issue,
                    )
                    project["external_ref"]["status"] = jira_status
                    project["external_ref"]["synced_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))
                    if was_created:
                        projects_created += 1
                    else:
                        projects_updated += 1
                    continue

                project, was_created = _project_for_issue(store, source_config, issue, issue_lookup)
                if was_created:
                    projects_created += 1
                project_id = project["project_id"]

                existing = next(
                    (t for t in store["tasks"]
                     if t.get("external_ref") and t["external_ref"].get("key") == key
                     and t["external_ref"].get("source_id") == source_id),
                    None,
                )

                parent = fields.get("parent") if isinstance(fields.get("parent"), dict) else None
                parent_key = str(parent.get("key") or "") if parent else ""
                external_ref = {
                    "type": "jira",
                    "source_id": source_id,
                    "key": key,
                    "url": issue_url,
                    "status": jira_status,
                    "synced_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
                    "parent_key": parent_key,
                    "parent_url": f"{source_config['base_url']}/browse/{parent_key}" if parent_key else "",
                }

                if existing:
                    existing["project_id"] = project_id
                    existing["title"] = summary[:180]
                    existing["status"] = local_status
                    if local_status == "concluido":
                        existing["progress"] = 100
                    elif _safe_progress(existing.get("progress")) >= 100:
                        existing["progress"] = 0
                    existing["external_ref"] = {**(existing.get("external_ref") or {}), **external_ref}
                    existing["updated_at"] = now
                    updated += 1
                else:
                    task = {
                        "task_id": f"tsk_{uuid.uuid4().hex[:12]}",
                        "project_id": project_id,
                        "title": summary[:180],
                        "description": "",
                        "status": local_status,
                        "priority": "media",
                        "category": _issue_type_name(issue) or "Backend",
                        "owner": (fields.get("assignee") or {}).get("displayName", "")[:64] or "jr",
                        "progress": 100 if local_status == "concluido" else 0,
                        "due_date": "",
                        "external_ref": external_ref,
                        "refs": {"github": [], "obsidian": [], "sessions": [], "jira": [issue_url]},
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

        return {
            "synced": len(issues),
            "created": created,
            "updated": updated,
            "projects_created": projects_created,
            "projects_updated": projects_updated,
        }

    except Exception as e:
        logger.exception("Jira sync failed for source %s", source_id)
        try:
            with project_store_lock():
                store = load_project_store()
                src = next((s for s in store["sources"] if s["source_id"] == source_id), None)
                if src:
                    src["sync_status"] = "error"
                    src["sync_error"] = str(e)[:500]
                    save_project_store(store)
        except Exception:
            pass
        message = str(e)
        return {"error": message, "error_code": _sync_error_code(message), "synced": 0, "created": 0, "updated": 0}
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
            "domain": s.get("domain", "projetos"),
            "default_project_id": s.get("default_project_id"),
            "project_groups": s.get("project_groups", []),
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
