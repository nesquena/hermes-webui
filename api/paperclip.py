"""Paperclip status and Org Chat helpers for the WebUI.

The status panel is observation-only. Org Chat is deliberately narrower than
Paperclip control: it can append comments to existing issues, but it does not
checkout, release, approve, assign, execute, create agents, or modify runtime
state.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

DEFAULT_BASE_URL = "http://127.0.0.1:3100/api"
TERMINAL_ISSUE_STATUSES = {"done", "cancelled", "canceled", "closed"}


def _base_url() -> str:
    return os.getenv("HERMES_WEBUI_PAPERCLIP_API", DEFAULT_BASE_URL).rstrip("/")


def _get_json(path: str, timeout: float = 3.0) -> Any:
    url = _base_url() + path
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        raw = response.read().decode("utf-8")
    if not raw.strip():
        return None
    return json.loads(raw)


def _pick_active_company(companies: list[dict[str, Any]]) -> dict[str, Any] | None:
    active = [c for c in companies if str(c.get("status") or "").lower() == "active"]
    if active:
        return active[0]
    return companies[0] if companies else None


def _post_json(path: str, payload: dict[str, Any], timeout: float = 4.0) -> Any:
    url = _base_url() + path
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={"Accept": "application/json", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as response:
        raw = response.read().decode("utf-8")
    if not raw.strip():
        return None
    return json.loads(raw)


def _active_company() -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    companies = _get_json("/companies", timeout=3.0) or []
    if not isinstance(companies, list):
        companies = []
    return _pick_active_company(companies), companies


def _safe_text(value: Any) -> str:
    return str(value or "").strip()


def _compact_issue(issue: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": issue.get("id"),
        "identifier": issue.get("identifier") or issue.get("issueNumber") or issue.get("id"),
        "title": issue.get("title") or "Untitled issue",
        "status": issue.get("status") or "unknown",
        "priority": issue.get("priority"),
        "execution_run_id": issue.get("executionRunId"),
        "checkout_run_id": issue.get("checkoutRunId"),
        "execution_locked_at": issue.get("executionLockedAt"),
    }


def _classify_agent(agent: dict[str, Any], active_issues: list[dict[str, Any]], pending_approval_count: int) -> tuple[str, str]:
    """Classify the agent runtime light, not the health of its assigned backlog.

    Paperclip's own roster shows an agent as idle even when it owns blocked
    issues. Mirroring that avoids implying the runtime is broken; blocked work is
    exposed separately through blocked_issue_count / active_issues.
    """
    raw_status = _safe_text(agent.get("status")).lower()
    if pending_approval_count > 0:
        return "blue", "awaiting_approval"
    if raw_status in {"error", "failed", "blocked", "offline", "paused"}:
        return "red", raw_status
    if any(
        issue.get("executionRunId")
        or issue.get("checkoutRunId")
        or issue.get("executionLockedAt")
        or _safe_text(issue.get("status")).lower() in {"in_progress", "working", "running"}
        for issue in active_issues
    ):
        return "yellow", "working"
    if raw_status in {"", "idle", "active", "available"}:
        return "green", "idle"
    return "gray", raw_status or "unknown"


def _build_org_chart(normalized_agents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Build a nested org chart from Paperclip reportsTo relationships."""
    nodes: dict[str, dict[str, Any]] = {}
    for agent in normalized_agents:
        agent_id = _safe_text(agent.get("id"))
        if not agent_id:
            continue
        node = {k: v for k, v in agent.items() if k != "active_issues"}
        node["active_issues"] = agent.get("active_issues", [])
        node["children"] = []
        nodes[agent_id] = node

    roots: list[dict[str, Any]] = []
    for node in nodes.values():
        parent_id = _safe_text(node.get("reports_to"))
        parent = nodes.get(parent_id)
        if parent and parent is not node:
            parent["children"].append(node)
        else:
            roots.append(node)

    def sort_key(node: dict[str, Any]) -> tuple[int, str]:
        role = _safe_text(node.get("role")).lower()
        name = _safe_text(node.get("name")).lower()
        rank = 0 if role in {"ceo", "founder"} or name == "ceo" else 1
        return (rank, name)

    def sort_tree(items: list[dict[str, Any]]) -> None:
        items.sort(key=sort_key)
        for item in items:
            sort_tree(item.get("children", []))

    sort_tree(roots)
    return roots


def build_paperclip_status() -> dict[str, Any]:
    """Return a read-only Paperclip org/agent status snapshot.

    The response is deliberately compact and UI-oriented. Agent light colors are
    derived from active issue locks/status plus pending approvals instead of
    trusting a single Paperclip agent.status field.
    """
    snapshot_ts = time.time()
    try:
        health = _get_json("/health", timeout=2.5)
        companies = _get_json("/companies", timeout=3.0) or []
        if not isinstance(companies, list):
            companies = []
        company = _pick_active_company(companies)
        if not company:
            return {
                "ok": False,
                "read_only": True,
                "error": "No Paperclip company found",
                "health": health,
                "agents": [],
                "snapshot_at": snapshot_ts,
            }

        company_id = company.get("id")
        company_path_id = urllib.parse.quote(str(company_id), safe="")
        agents = _get_json(f"/companies/{company_path_id}/agents", timeout=4.0) or []
        issues = _get_json(f"/companies/{company_path_id}/issues?limit=100", timeout=4.0) or []
        approvals = _get_json(f"/companies/{company_path_id}/approvals", timeout=4.0) or []
        if not isinstance(agents, list):
            agents = []
        if not isinstance(issues, list):
            issues = []
        if not isinstance(approvals, list):
            approvals = []

        active_issues_by_agent: dict[str, list[dict[str, Any]]] = {}
        for issue in issues:
            if not isinstance(issue, dict):
                continue
            status = _safe_text(issue.get("status")).lower()
            if status in TERMINAL_ISSUE_STATUSES:
                continue
            assignee = _safe_text(issue.get("assigneeAgentId"))
            if not assignee:
                continue
            active_issues_by_agent.setdefault(assignee, []).append(issue)

        pending_approvals_by_agent: dict[str, int] = {}
        for approval in approvals:
            if not isinstance(approval, dict):
                continue
            if _safe_text(approval.get("status")).lower() != "pending":
                continue
            requester = _safe_text(approval.get("requestedByAgentId"))
            if requester:
                pending_approvals_by_agent[requester] = pending_approvals_by_agent.get(requester, 0) + 1

        normalized_agents = []
        for agent in agents:
            if not isinstance(agent, dict):
                continue
            agent_id = _safe_text(agent.get("id"))
            active_issues = active_issues_by_agent.get(agent_id, [])
            pending_count = pending_approvals_by_agent.get(agent_id, 0)
            light, display_status = _classify_agent(agent, active_issues, pending_count)
            blocked_issue_count = sum(1 for issue in active_issues if _safe_text(issue.get("status")).lower() == "blocked")
            normalized_agents.append({
                "id": agent_id,
                "name": agent.get("name") or agent.get("title") or agent_id or "Unknown agent",
                "title": agent.get("title") or "",
                "role": agent.get("role") or "",
                "reports_to": agent.get("reportsTo"),
                "adapter_type": agent.get("adapterType"),
                "raw_status": agent.get("status") or "unknown",
                "display_status": display_status,
                "light": light,
                "active_issue_count": len(active_issues),
                "blocked_issue_count": blocked_issue_count,
                "active_issues": [_compact_issue(issue) for issue in active_issues[:5]],
                "pending_approval_count": pending_count,
                "last_heartbeat_at": agent.get("lastHeartbeatAt"),
                "updated_at": agent.get("updatedAt"),
            })

        return {
            "ok": True,
            "read_only": True,
            "health": health,
            "company": {
                "id": company.get("id"),
                "name": company.get("name"),
                "status": company.get("status"),
                "issue_prefix": company.get("issuePrefix"),
            },
            "agents": normalized_agents,
            "org_chart": _build_org_chart(normalized_agents),
            "counts": {
                "agents": len(normalized_agents),
                "active_issues": sum(len(v) for v in active_issues_by_agent.values()),
                "pending_approvals": sum(pending_approvals_by_agent.values()),
            },
            "snapshot_at": snapshot_ts,
        }
    except urllib.error.URLError as exc:
        return {
            "ok": False,
            "read_only": True,
            "error": f"Paperclip API unreachable: {exc}",
            "agents": [],
            "snapshot_at": snapshot_ts,
        }
    except Exception as exc:
        return {
            "ok": False,
            "read_only": True,
            "error": f"Paperclip status failed: {exc}",
            "agents": [],
            "snapshot_at": snapshot_ts,
        }


def _compact_agent(agent: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": agent.get("id"),
        "name": agent.get("name") or agent.get("title") or agent.get("id") or "Unknown agent",
        "title": agent.get("title") or "",
        "role": agent.get("role") or "",
        "reports_to": agent.get("reportsTo"),
        "status": agent.get("status") or "unknown",
        "display_status": agent.get("status") or "unknown",
        "light": "gray",
        "active_issue_count": 0,
        "blocked_issue_count": 0,
        "pending_approval_count": 0,
    }


def _compact_comment(comment: dict[str, Any], issue: dict[str, Any], agent_names: dict[str, str]) -> dict[str, Any]:
    author_agent_id = comment.get("authorAgentId")
    author_user_id = comment.get("authorUserId")
    if author_agent_id:
        author_name = agent_names.get(str(author_agent_id), str(author_agent_id))
        author_type = "agent"
    elif author_user_id:
        author_name = "Hermes/User"
        author_type = "user"
    else:
        author_name = "System"
        author_type = "system"
    return {
        "id": comment.get("id"),
        "issue_id": comment.get("issueId") or issue.get("id"),
        "issue_identifier": issue.get("identifier") or issue.get("issueNumber") or issue.get("id"),
        "issue_title": issue.get("title") or "Untitled issue",
        "author_agent_id": author_agent_id,
        "author_user_id": author_user_id,
        "author_name": author_name,
        "author_type": author_type,
        "body": comment.get("body") or "",
        "created_at": comment.get("createdAt"),
        "updated_at": comment.get("updatedAt"),
    }


def get_org_chat_context(limit: int = 25, comments_per_issue: int = 8) -> dict[str, Any]:
    """Return compact Org Chat context: roster, recent issues, recent comments.

    This is read-oriented. It aggregates existing issue comments only; it does
    not wake agents or create new work.
    """
    snapshot_ts = time.time()
    try:
        health = _get_json("/health", timeout=2.5)
        company, _ = _active_company()
        if not company:
            return {"ok": False, "read_only": True, "error": "No Paperclip company found", "agents": [], "issues": [], "messages": [], "snapshot_at": snapshot_ts}
        company_id = company.get("id")
        company_path_id = urllib.parse.quote(str(company_id), safe="")
        safe_limit = max(1, min(int(limit or 25), 100))
        safe_comments = max(1, min(int(comments_per_issue or 8), 25))
        agents = _get_json(f"/companies/{company_path_id}/agents", timeout=4.0) or []
        issues = _get_json(f"/companies/{company_path_id}/issues?limit={safe_limit}", timeout=4.0) or []
        if not isinstance(agents, list):
            agents = []
        if not isinstance(issues, list):
            issues = []
        agent_names = {str(a.get("id")): (a.get("name") or a.get("title") or str(a.get("id"))) for a in agents if isinstance(a, dict)}
        compact_issues = [_compact_issue(i) | {"assignee_agent_id": i.get("assigneeAgentId"), "updated_at": i.get("updatedAt")} for i in issues if isinstance(i, dict)]
        messages: list[dict[str, Any]] = []
        for issue in issues[: min(len(issues), 8)]:
            if not isinstance(issue, dict) or not issue.get("id"):
                continue
            try:
                comments = _get_json(f"/issues/{urllib.parse.quote(str(issue.get('id')), safe='')}/comments", timeout=4.0) or []
            except Exception:
                comments = []
            if not isinstance(comments, list):
                continue
            comments = [c for c in comments if isinstance(c, dict)]
            comments = sorted(comments, key=lambda c: c.get("createdAt") or c.get("updatedAt") or "")[-safe_comments:]
            messages.extend(_compact_comment(c, issue, agent_names) for c in comments)
        messages.sort(key=lambda m: m.get("created_at") or m.get("updated_at") or "")
        compact_agents = [_compact_agent(a) for a in agents if isinstance(a, dict)]
        return {
            "ok": True,
            "read_only": True,
            "health": health,
            "company": {"id": company.get("id"), "name": company.get("name"), "status": company.get("status"), "issue_prefix": company.get("issuePrefix")},
            "agents": compact_agents,
            "org_chart": _build_org_chart(compact_agents),
            "issues": compact_issues,
            "messages": messages[-80:],
            "snapshot_at": snapshot_ts,
        }
    except urllib.error.URLError as exc:
        return {"ok": False, "read_only": True, "error": f"Paperclip API unreachable: {exc}", "agents": [], "issues": [], "messages": [], "snapshot_at": snapshot_ts}
    except Exception as exc:
        return {"ok": False, "read_only": True, "error": f"Paperclip Org Chat context failed: {exc}", "agents": [], "issues": [], "messages": [], "snapshot_at": snapshot_ts}


def send_org_chat_message(issue_id: str, message: str, target_label: str | None = None) -> dict[str, Any]:
    """Append an Org Chat comment to an existing Paperclip issue.

    MVP safety boundary: existing issue comment only. No checkout/release,
    approval, assignment, agent creation, or free-floating task creation.
    """
    issue_id = _safe_text(issue_id)
    message = _safe_text(message)
    target_label = _safe_text(target_label)
    if not issue_id:
        return {"ok": False, "read_only": False, "mutation": "comment_only", "error": "issue_id is required"}
    if not message:
        return {"ok": False, "read_only": False, "mutation": "comment_only", "error": "message is required"}
    if len(message) > 12000:
        return {"ok": False, "read_only": False, "mutation": "comment_only", "error": "message is too long; keep Org Chat comments under 12,000 characters"}
    try:
        issue = _get_json(f"/issues/{urllib.parse.quote(issue_id, safe='')}", timeout=3.0)
        if not isinstance(issue, dict) or not issue.get("id"):
            return {"ok": False, "read_only": False, "mutation": "comment_only", "error": "Issue not found"}
        prefix = f"[Org Chat → {target_label}]\n\n" if target_label else "[Org Chat]\n\n"
        body = prefix + message
        comment = _post_json(f"/issues/{urllib.parse.quote(issue_id, safe='')}/comments", {"body": body}, timeout=4.0)
        comments = _get_json(f"/issues/{urllib.parse.quote(issue_id, safe='')}/comments", timeout=4.0) or []
        verified = False
        if isinstance(comments, list):
            verified = any(isinstance(c, dict) and c.get("id") == (comment or {}).get("id") and c.get("body") == body for c in comments)
        return {
            "ok": True,
            "read_only": False,
            "mutation": "comment_only",
            "issue": _compact_issue(issue),
            "comment": comment,
            "verified": verified,
        }
    except urllib.error.HTTPError as exc:
        return {"ok": False, "read_only": False, "mutation": "comment_only", "error": f"Paperclip comment failed: HTTP {exc.code}"}
    except urllib.error.URLError as exc:
        return {"ok": False, "read_only": False, "mutation": "comment_only", "error": f"Paperclip API unreachable: {exc}"}
    except Exception as exc:
        return {"ok": False, "read_only": False, "mutation": "comment_only", "error": f"Paperclip Org Chat send failed: {exc}"}
