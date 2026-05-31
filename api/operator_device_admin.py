"""Read-only Device Admin Foundations operator payloads.

Slice 8 models host/path allowlists, dry-run action previews, receipt-log
availability, and per-action approval requirements. It never executes device
operations, reads credentials, writes receipts, scans arbitrary paths, or uses
paths from the allowlist for filesystem access.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PAYLOAD_VERSION = 1
MODE = "device-admin-foundations-read-only"
PREVIEW_MODE = "device-admin-dry-run-preview-read-only"
MAX_LIST_ITEMS = 50
MAX_QUERY_LIMIT = 100
MAX_PREVIEW_BYTES = 24000
MAX_RECEIPTS = 20
MAX_SOURCE_AGE_SECONDS = 7 * 24 * 60 * 60

WORKSPACE_ROOT = Path("/mnt/c/Users/malac/.openclaw/workspace/main")
ALLOWLIST_PATH = WORKSPACE_ROOT / "state" / "operator_device_admin_allowlist.json"
RECEIPT_LOG_PATH = WORKSPACE_ROOT / "state" / "operator_device_admin_receipts.jsonl"

_ALLOWLIST_DISPLAY_PATH = "state/operator_device_admin_allowlist.json"
_RECEIPTS_DISPLAY_PATH = "state/operator_device_admin_receipts.jsonl"
_NO_EXECUTION_TEXT = "No device action was executed. Approval and execution are disabled in Slice 8."
_SECRET_REDACTION = "[redacted-secret]"
_PATH_REDACTION = "[redacted-path]"
_ALLOWED_HOST_KINDS = {"local", "windows", "linux", "mac", "nas", "unknown"}
_ALLOWED_SOURCE_STATES = {"allowed", "disabled", "unknown"}
_ALLOWED_ACTIONS = {"copy", "move", "delete", "rename", "mkdir", "inventory", "unknown"}
_ALLOWED_DRY_RUN_STATES = {"blocked", "draft", "unknown"}
_ALLOWED_RISKS = {"low", "medium", "high", "unknown"}
_VALID_RECEIPT_STATUSES = {"dry_run", "blocked", "approved_model_only", "unknown"}
_REQUIRED_APPROVAL_FIELDS = [
    "action_id",
    "host_id",
    "source_path_id",
    "destination_path_id",
    "reason",
    "approved_by",
    "approved_at",
    "expires_at",
]

_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"\b[A-Z][A-Z0-9_]*(?:API[_-]?KEY|TOKEN|SECRET|PASSWORD|PRIVATE[_-]?KEY|ACCESS[_-]?KEY(?:[_-]?ID)?|SECRET[_-]?ACCESS[_-]?KEY|SESSION[_-]?COOKIE|COOKIE|PASS)\s*[:=]\s*\"?[^\s,;\"']+\"?",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:password|passwd|pwd|pass|api[_-]?key|token|access[_-]?token|refresh[_-]?token|private[_-]?key|access[_-]?key(?:[_-]?id)?|secret[_-]?access[_-]?key|session[_-]?cookie|cookie|secret)\s*[:=]\s*\"?[^\s,;\"']+\"?",
        re.IGNORECASE,
    ),
    re.compile(r"\b(?:password|passwd|pwd|pass|api[_-]?key|token|access[_-]?token|refresh[_-]?token|private[_-]?key|access[_-]?key(?:[_-]?id)?|session[_-]?cookie|cookie|secret)\s+[A-Za-z0-9._~+/=-]{8,}\b", re.IGNORECASE),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{8,}", re.IGNORECASE),
    re.compile(r"\b(?:sk|xox[baprs]?)-[A-Za-z0-9._/=-]{8,}\b", re.IGNORECASE),
    re.compile(r"\b(?:ghp|github_pat)_[A-Za-z0-9_.]{8,}(?![A-Za-z0-9_.])", re.IGNORECASE),
)
_PATH_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\b[A-Za-z]:[\\/][^\s\"'<>]+"),
    re.compile(r"\\\\[^\s\"'<>]+\\[^\s\"'<>]+"),
    re.compile(r"(?<![A-Za-z0-9_:])/(?:[^\s\"'<>]+)"),
)
_SECRET_NAME_PATTERN = re.compile(
    r"\b(?:[A-Z0-9]+[_-])*(?:password|passwd|pwd|pass|api[_-]?key|token|access[_-]?token|refresh[_-]?token|private[_-]?key|access[_-]?key(?:[_-]?id)?|secret[_-]?access[_-]?key|secret|session[_-]?cookie|auth[_-]?cookie|cookie)\b",
    re.IGNORECASE,
)
_OPAQUE_ACTION_ID_PATTERN = re.compile(r"^dda_[0-9a-f]{16}$")


def build_operator_device_admin_payload(
    query_text: Any = "",
    host: Any = "all",
    action: Any = "all",
    limit: Any = MAX_LIST_ITEMS,
    now: float | None = None,
) -> dict[str, Any]:
    """Build the Slice 8 read-only Device Admin Foundations catalog payload."""

    generated_at = float(time.time() if now is None else now)
    raw_query_text = _clean_text(query_text)
    normalized_host = _clean_filter(host)
    normalized_action = _clean_filter(action).lower()
    normalized_limit = _coerce_limit(limit)
    query = {
        "text": _redact_text(raw_query_text),
        "host": _redact_text(normalized_host),
        "action": _redact_text(normalized_action),
        "limit": normalized_limit,
    }

    allowlist = _load_allowlist_catalog(now=generated_at)
    receipt_source, receipts, receipt_issues = _read_receipts(now=generated_at)
    sources = [allowlist["source"], receipt_source]
    issues = [*allowlist["issues"], *receipt_issues]

    hosts = list(allowlist["hosts"])
    paths = list(allowlist["paths"])
    dry_runs = list(allowlist["dry_runs"])
    hosts, paths, dry_runs = _filter_catalog(
        hosts,
        paths,
        dry_runs,
        query_text=raw_query_text,
        host=normalized_host,
        action=normalized_action,
        limit=normalized_limit,
    )

    status = _catalog_status(allowlist["source"], receipt_source, dry_runs, issues)
    summary = _catalog_summary(status, hosts, paths, dry_runs, issues)

    return {
        "version": PAYLOAD_VERSION,
        "mode": MODE,
        "generated_at": generated_at,
        "status": status,
        "execution_state": "blocked",
        "summary": summary,
        "query": query,
        "sources": sources,
        "approval_model": _approval_model(),
        "hosts": hosts,
        "paths": paths,
        "dry_runs": dry_runs,
        "receipts": receipts,
        "issues": issues,
        "would_execute": False,
    }


def build_operator_device_admin_preview_payload(action_id: Any = "", now: float | None = None) -> dict[str, Any]:
    """Build a blocked dry-run preview for a source-backed opaque action id."""

    generated_at = float(time.time() if now is None else now)
    raw_action_id = "" if action_id is None else str(action_id)
    normalized_action_id = _clean_text(action_id)
    if _is_suspicious_action_id(raw_action_id):
        return _preview_payload(
            generated_at=generated_at,
            status="unknown",
            action=_unknown_preview_action(""),
            preview=_empty_preview(),
            issues=["rejected malformed device-admin dry-run id"],
        )
    if not normalized_action_id:
        return _preview_payload(
            generated_at=generated_at,
            status="unknown",
            action=_unknown_preview_action(""),
            preview=_empty_preview(),
            issues=["missing or unknown device-admin dry-run id"],
        )

    allowlist = _load_allowlist_catalog(now=generated_at)
    target = next((item for item in allowlist["dry_runs"] if item.get("id") == normalized_action_id), None)
    if target is None:
        return _preview_payload(
            generated_at=generated_at,
            status="unknown",
            action=_unknown_preview_action(normalized_action_id),
            preview=_empty_preview(),
            issues=["missing or unknown device-admin dry-run id"],
        )

    action = _preview_action(target)
    issues = list(target.get("issues") or [])
    source_state = _clean_text(allowlist["source"].get("state")).lower()
    status = source_state if source_state in {"live", "stale"} and not issues else "unknown"
    return _preview_payload(
        generated_at=generated_at,
        status=status,
        action=action,
        preview=_dry_run_preview(target),
        issues=issues,
    )


def _is_suspicious_action_id(raw_action_id: str) -> bool:
    raw = "" if raw_action_id is None else str(raw_action_id)
    stripped = raw.strip()
    if not stripped:
        return False
    if "\x00" in raw:
        return True
    return _OPAQUE_ACTION_ID_PATTERN.fullmatch(stripped) is None


def _preview_payload(*, generated_at: float, status: str, action: dict[str, Any], preview: dict[str, Any], issues: list[str]) -> dict[str, Any]:
    return {
        "version": PAYLOAD_VERSION,
        "mode": PREVIEW_MODE,
        "generated_at": generated_at,
        "status": status,
        "execution_state": "blocked",
        "action": action,
        "preview": preview,
        "issues": issues,
        "would_execute": False,
    }


def _unknown_preview_action(action_id: str) -> dict[str, Any]:
    if not action_id:
        return {"id": "", "action": "unknown", "summary": ""}
    return {"id": _redact_text(action_id), "action": "unknown", "summary": ""}


def _preview_action(dry_run: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": _redact_text(dry_run.get("id")),
        "source_action_id": _redact_text(dry_run.get("source_action_id")),
        "action": _known_value(dry_run.get("action"), _ALLOWED_ACTIONS),
        "summary": _redact_text(dry_run.get("summary")),
        "reason": _redact_text(dry_run.get("reason")),
        "risk": _known_value(dry_run.get("risk"), _ALLOWED_RISKS),
        "approval_required": True,
        "state": _known_value(dry_run.get("state"), _ALLOWED_DRY_RUN_STATES, default="blocked"),
        "would_execute": False,
    }


def _dry_run_preview(dry_run: Mapping[str, Any]) -> dict[str, Any]:
    lines = [
        _NO_EXECUTION_TEXT,
        f"Action: {_redact_text(dry_run.get('action')) or 'unknown'}",
    ]
    summary = _redact_text(dry_run.get("summary"))
    reason = _redact_text(dry_run.get("reason"))
    risk = _redact_text(dry_run.get("risk"))
    if summary:
        lines.append(f"Summary: {summary}")
    if reason:
        lines.append(f"Reason: {reason}")
    if risk:
        lines.append(f"Risk: {risk}")
    lines.append("Approval and execution are disabled in Slice 8.")
    text = "\n".join(lines)
    encoded = text.encode("utf-8", errors="replace")
    truncated = len(encoded) > MAX_PREVIEW_BYTES
    if truncated:
        encoded = encoded[:MAX_PREVIEW_BYTES]
        text = encoded.decode("utf-8", errors="replace")
    return {
        "format": "dry-run-summary",
        "text": text,
        "truncated": truncated,
        "bytes_read": len(encoded),
        "max_bytes": MAX_PREVIEW_BYTES,
    }


def _approval_model() -> dict[str, Any]:
    return {
        "required": True,
        "per_action": True,
        "execution_enabled": False,
        "required_fields": list(_REQUIRED_APPROVAL_FIELDS),
    }


def _load_allowlist_catalog(*, now: float | None = None) -> dict[str, Any]:
    source = {
        "id": "allowlist",
        "display_path": _ALLOWLIST_DISPLAY_PATH,
        "state": "unknown",
        "issue": "missing allowlist",
        "count": 0,
    }
    path = Path(ALLOWLIST_PATH)
    if not _path_exists(path):
        return {"source": source, "hosts": [], "paths": [], "dry_runs": [], "issues": ["device admin allowlist is missing"]}

    try:
        raw_text = path.read_text(encoding="utf-8-sig", errors="replace")
        data = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        source["issue"] = "malformed allowlist JSON"
        return {
            "source": source,
            "hosts": [],
            "paths": [],
            "dry_runs": [],
            "issues": [f"device admin allowlist is malformed JSON: {exc.msg}"],
        }
    except Exception as exc:  # pragma: no cover - permissions/platform edge
        source["issue"] = "unreadable allowlist"
        return {
            "source": source,
            "hosts": [],
            "paths": [],
            "dry_runs": [],
            "issues": [f"device admin allowlist is unreadable: {_short_error(exc)}"],
        }

    if not isinstance(data, Mapping):
        source["issue"] = "malformed allowlist schema"
        return {"source": source, "hosts": [], "paths": [], "dry_runs": [], "issues": ["device admin allowlist is malformed: expected object"]}

    hosts, paths, host_by_id, path_by_id, parse_issues = _parse_hosts(data.get("hosts"))
    dry_runs, action_issues = _parse_actions(data.get("proposed_actions"), host_by_id=host_by_id, path_by_id=path_by_id)
    issues = [*parse_issues, *action_issues]
    stale_issue = _stale_source_issue(data.get("generated_at"), now=now, label="allowlist")
    if stale_issue:
        issues.append(stale_issue)
    source["count"] = len(hosts) + len(dry_runs)
    if parse_issues or action_issues:
        source["state"] = "unknown"
        source["issue"] = "one or more allowlist records have issues"
    elif stale_issue:
        source["state"] = "stale"
        source["issue"] = stale_issue
    else:
        source["state"] = "live"
        source["issue"] = ""
    return {"source": source, "hosts": hosts, "paths": paths, "dry_runs": dry_runs, "issues": issues}


def _parse_hosts(raw_hosts: Any) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, dict[str, Any]], dict[str, dict[str, Any]], list[str]]:
    hosts: list[dict[str, Any]] = []
    paths: list[dict[str, Any]] = []
    host_by_id: dict[str, dict[str, Any]] = {}
    path_by_id: dict[str, dict[str, Any]] = {}
    issues: list[str] = []
    if raw_hosts is None:
        raw_hosts = []
    if not isinstance(raw_hosts, list):
        return hosts, paths, host_by_id, path_by_id, ["allowlist hosts field is malformed"]

    for index, raw_host in enumerate(raw_hosts):
        if not isinstance(raw_host, Mapping):
            issues.append(f"host entry {index} is malformed")
            continue
        raw_host_id = _clean_text(raw_host.get("id"))
        host_issues: list[str] = []
        if not raw_host_id:
            host_issues.append("missing host id")
            issues.append(f"host entry {index}: missing host id")
        host_id = _redact_text(raw_host_id) if raw_host_id else "unknown"
        host = {
            "id": host_id,
            "label": _redact_text(_clean_text(raw_host.get("label")) or raw_host_id or "unknown"),
            "kind": _known_value(raw_host.get("kind"), _ALLOWED_HOST_KINDS),
            "state": "unknown" if host_issues else _known_value(raw_host.get("state"), _ALLOWED_SOURCE_STATES),
            "path_count": 0,
            "issues": host_issues,
        }
        raw_paths_value = raw_host.get("paths", [])
        if raw_paths_value is None:
            raw_paths = []
        elif not isinstance(raw_paths_value, list):
            host["issues"].append("host paths field is malformed")
            host["state"] = "unknown"
            issues.append(f"host {host_id}: paths field is malformed")
            raw_paths = []
        else:
            raw_paths = raw_paths_value
        if raw_host_id:
            host_by_id[raw_host_id] = host
        hosts.append(host)
        for path_index, raw_path in enumerate(raw_paths):
            if not isinstance(raw_path, Mapping):
                issue = f"host {host_id}: path entry {path_index} is malformed"
                host["issues"].append(issue)
                issues.append(issue)
                continue
            raw_path_id = _clean_text(raw_path.get("id"))
            path_id = _redact_text(raw_path_id) if raw_path_id else "unknown"
            raw_capabilities = raw_path.get("capabilities")
            path_issues: list[str] = []
            if not raw_path_id:
                path_issues.append("missing path id")
                issues.append(f"host {host_id}: path entry {path_index}: missing path id")
            if raw_capabilities is None:
                capabilities = []
            elif isinstance(raw_capabilities, list):
                capabilities = raw_capabilities
            else:
                capabilities = []
                path_issues.append("path capabilities field is malformed")
                issues.append(f"host {host_id}: path {path_id}: capabilities field is malformed")
            path_record = {
                "id": path_id,
                "host_id": host_id,
                "label": _redact_text(_clean_text(raw_path.get("label")) or raw_path_id or "unknown"),
                "display_path": _redact_text(_clean_text(raw_path.get("path")) or "unknown"),
                "capabilities": [_redact_text(_clean_text(capability)) for capability in capabilities if _clean_text(capability)],
                "state": "unknown" if path_issues else _known_value(raw_path.get("state"), _ALLOWED_SOURCE_STATES),
                "issues": path_issues,
            }
            paths.append(path_record)
            if raw_path_id:
                path_by_id[raw_path_id] = path_record
            host["path_count"] += 1
    return hosts, paths, host_by_id, path_by_id, issues


def _parse_actions(raw_actions: Any, *, host_by_id: Mapping[str, dict[str, Any]], path_by_id: Mapping[str, dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
    dry_runs: list[dict[str, Any]] = []
    issues: list[str] = []
    if raw_actions is None:
        raw_actions = []
    if not isinstance(raw_actions, list):
        return dry_runs, ["allowlist proposed_actions field is malformed"]

    for index, raw_action in enumerate(raw_actions):
        if not isinstance(raw_action, Mapping):
            issues.append(f"proposed action {index} is malformed")
            continue
        raw_action_id = _clean_text(raw_action.get("id"))
        source_action_id = _redact_text(raw_action_id) if raw_action_id else "unknown"
        raw_host_id = _clean_text(raw_action.get("host_id"))
        raw_source_path_id = _clean_text(raw_action.get("source_path_id"))
        raw_destination_path_id = _clean_text(raw_action.get("destination_path_id"))
        action_issues: list[str] = []
        if not raw_action_id:
            action_issues.append("missing action id")
        if raw_host_id not in host_by_id:
            action_issues.append(f"missing host reference: {_redact_text(raw_host_id or 'unknown')}")
        if raw_source_path_id not in path_by_id:
            action_issues.append(f"missing source path reference: {_redact_text(raw_source_path_id or 'unknown')}")
        if not raw_destination_path_id:
            action_issues.append("missing destination path reference: unknown")
        elif raw_destination_path_id not in path_by_id:
            action_issues.append(f"missing destination path reference: {_redact_text(raw_destination_path_id)}")
        raw_action_name = _clean_text(raw_action.get("action")).lower()
        normalized_action = raw_action_name if raw_action_name in _ALLOWED_ACTIONS else "unknown"
        if normalized_action == "unknown" and raw_action_name not in {"", "unknown"}:
            action_issues.append(f"malformed action type: {_redact_text(raw_action_name)}")
        state = _known_value(raw_action.get("state"), _ALLOWED_DRY_RUN_STATES)
        if action_issues:
            state = "blocked"
        dry_run = {
            "id": _opaque_action_id(raw_action_id) if raw_action_id else "",
            "source_action_id": source_action_id,
            "action": normalized_action,
            "host_id": _redact_text(raw_host_id),
            "source_path_id": _redact_text(raw_source_path_id),
            "destination_path_id": _redact_text(raw_destination_path_id),
            "summary": _redact_text(_clean_text(raw_action.get("summary"))),
            "reason": _redact_text(_clean_text(raw_action.get("reason"))),
            "risk": _known_value(raw_action.get("risk"), _ALLOWED_RISKS),
            "approval_required": True,
            "state": state,
            "issues": action_issues,
            "preview_available": not action_issues,
            "would_execute": False,
        }
        dry_runs.append(dry_run)
        issues.extend(f"action {source_action_id}: {issue}" for issue in action_issues)
    return dry_runs, issues


def _read_receipts(*, now: float | None = None) -> tuple[dict[str, Any], list[dict[str, Any]], list[str]]:
    source = {
        "id": "receipts",
        "display_path": _RECEIPTS_DISPLAY_PATH,
        "state": "unknown",
        "issue": "missing receipt log",
        "count": 0,
    }
    if not _path_exists(RECEIPT_LOG_PATH):
        return source, [], ["device admin receipt log is missing"]
    try:
        lines = Path(RECEIPT_LOG_PATH).read_text(encoding="utf-8-sig", errors="replace").splitlines()
    except Exception as exc:  # pragma: no cover - permissions/platform edge
        source["issue"] = "unreadable receipt log"
        return source, [], [f"device admin receipt log is unreadable: {_short_error(exc)}"]

    receipts: list[dict[str, Any]] = []
    issues: list[str] = []
    latest_created_at: Any = None
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError:
            issues.append(f"malformed receipt line {line_number}")
            continue
        if not isinstance(raw, Mapping):
            issues.append(f"malformed receipt line {line_number}: expected object")
            continue
        status = _clean_text(raw.get("status")).lower() or "unknown"
        if status not in _VALID_RECEIPT_STATUSES:
            issues.append(f"unsupported receipt status on line {line_number}: {_redact_text(status)}")
            continue
        created_at = _clean_text(raw.get("created_at"))
        if created_at:
            latest_created_at = _newer_timestamp_value(latest_created_at, created_at)
        receipts.append(
            {
                "id": _redact_text(_clean_text(raw.get("id")) or "unknown"),
                "action_id": _redact_text(_clean_text(raw.get("action_id")) or "unknown"),
                "status": status,
                "created_at": _redact_text(created_at),
                "summary": _redact_text(_clean_text(raw.get("summary"))),
                "would_execute": False,
            }
        )

    stale_issue = _stale_source_issue(latest_created_at, now=now, label="receipt log") if receipts else ""
    if stale_issue:
        issues.append(stale_issue)
    source["count"] = len(receipts)
    if any("receipt" in issue.lower() and ("malformed" in issue.lower() or "unsupported" in issue.lower()) for issue in issues):
        source["state"] = "unknown"
        source["issue"] = f"{len(issues)} receipt issue{'s' if len(issues) != 1 else ''}"
    elif stale_issue:
        source["state"] = "stale"
        source["issue"] = stale_issue
    else:
        source["state"] = "live"
        source["issue"] = ""
    return source, list(reversed(receipts[-MAX_RECEIPTS:])), issues


def _filter_catalog(
    hosts: list[dict[str, Any]],
    paths: list[dict[str, Any]],
    dry_runs: list[dict[str, Any]],
    *,
    query_text: str,
    host: str,
    action: str,
    limit: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    query = query_text.lower()
    filtered_dry_runs = []
    for dry_run in dry_runs:
        if host != "all" and dry_run.get("host_id") != host:
            continue
        if action != "all" and str(dry_run.get("action") or "").lower() != action:
            continue
        if query and query not in _dry_run_haystack(dry_run).lower():
            continue
        filtered_dry_runs.append(dry_run)
    filtered_dry_runs = filtered_dry_runs[:limit]

    filters_active = bool(query) or host != "all" or action != "all"
    if filters_active:
        host_ids = {str(item.get("host_id") or "") for item in filtered_dry_runs}
        path_ids = {str(item.get("source_path_id") or "") for item in filtered_dry_runs}
        path_ids.update(str(item.get("destination_path_id") or "") for item in filtered_dry_runs)
        if host != "all":
            host_ids.add(host)
        filtered_hosts = [item for item in hosts if item.get("id") in host_ids][:limit]
        filtered_paths = [item for item in paths if item.get("host_id") in host_ids or item.get("id") in path_ids][:limit]
        return filtered_hosts, filtered_paths, filtered_dry_runs
    return hosts[:limit], paths[:limit], filtered_dry_runs


def _dry_run_haystack(dry_run: Mapping[str, Any]) -> str:
    fields = [
        dry_run.get("source_action_id"),
        dry_run.get("action"),
        dry_run.get("host_id"),
        dry_run.get("source_path_id"),
        dry_run.get("destination_path_id"),
        dry_run.get("summary"),
        dry_run.get("reason"),
        dry_run.get("risk"),
        dry_run.get("state"),
    ]
    return " ".join(_clean_text(field) for field in fields)


def _catalog_status(allowlist_source: Mapping[str, Any], receipt_source: Mapping[str, Any], dry_runs: list[dict[str, Any]], issues: list[str]) -> str:
    source_states = {_clean_text(allowlist_source.get("state")).lower(), _clean_text(receipt_source.get("state")).lower()}
    if "unknown" in source_states or "" in source_states:
        return "unknown"
    if "stale" in source_states:
        return "stale"
    if issues or any(item.get("issues") for item in dry_runs):
        return "unknown"
    return "live"


def _catalog_summary(status: str, hosts: list[dict[str, Any]], paths: list[dict[str, Any]], dry_runs: list[dict[str, Any]], issues: list[str]) -> str:
    if status == "live":
        return f"Device admin foundations loaded {len(hosts)} hosts, {len(paths)} paths, and {len(dry_runs)} dry-run actions; execution remains disabled."
    if status == "stale":
        return "Device admin foundations are stale and blocked until fresh allowlist and receipt sources are available; execution remains disabled."
    if issues:
        return f"Device admin foundations are blocked with {len(issues)} source issue{'s' if len(issues) != 1 else ''}; execution remains disabled."
    return "Device admin foundations are blocked until allowlist and per-action approval exist."


def _parse_source_timestamp(value: Any) -> float | None:
    cleaned = _clean_text(value)
    if not cleaned:
        return None
    try:
        return float(cleaned)
    except ValueError:
        pass
    try:
        normalized = cleaned.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except ValueError:
        return None


def _stale_source_issue(timestamp_value: Any, *, now: float | None, label: str) -> str:
    parsed = _parse_source_timestamp(timestamp_value)
    if parsed is None:
        return ""
    current = float(time.time() if now is None else now)
    if current - parsed > MAX_SOURCE_AGE_SECONDS:
        return f"stale {label}: source timestamp is older than {MAX_SOURCE_AGE_SECONDS} seconds"
    return ""


def _newer_timestamp_value(current: Any, candidate: Any) -> Any:
    current_timestamp = _parse_source_timestamp(current)
    candidate_timestamp = _parse_source_timestamp(candidate)
    if candidate_timestamp is None:
        return current
    if current_timestamp is None or candidate_timestamp > current_timestamp:
        return candidate
    return current


def _path_exists(path: Any) -> bool:
    if path is None:
        return False
    try:
        return Path(path).exists()
    except (OSError, TypeError, ValueError):
        return False


def _empty_preview() -> dict[str, Any]:
    return {
        "format": "dry-run-summary",
        "text": _NO_EXECUTION_TEXT,
        "truncated": False,
        "bytes_read": 0,
        "max_bytes": MAX_PREVIEW_BYTES,
    }


def _opaque_action_id(source_action_id: str) -> str:
    digest = hashlib.sha256(("device-admin\0" + source_action_id).encode("utf-8", errors="replace")).hexdigest()[:16]
    return f"dda_{digest}"


def _known_value(value: Any, allowed: set[str], default: str = "unknown") -> str:
    cleaned = _clean_text(value).lower()
    return cleaned if cleaned in allowed else default


def _clean_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).replace("\x00", "").strip()


def _clean_filter(value: Any) -> str:
    cleaned = _clean_text(value)
    return cleaned or "all"


def _coerce_limit(value: Any) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return MAX_LIST_ITEMS
    return max(1, min(parsed, MAX_QUERY_LIMIT))


def _redact_text(value: Any) -> str:
    original = _clean_text(value)
    text = original
    if not text:
        return ""
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(_SECRET_REDACTION, text)
    if _SECRET_NAME_PATTERN.search(original):
        return _SECRET_REDACTION
    if any(pattern.search(original) for pattern in _PATH_PATTERNS):
        return _PATH_REDACTION
    return text[:500]


def _short_error(exc: Exception) -> str:
    return _redact_text(str(exc) or exc.__class__.__name__)[:160]


__all__ = [
    "PAYLOAD_VERSION",
    "MODE",
    "PREVIEW_MODE",
    "MAX_LIST_ITEMS",
    "MAX_PREVIEW_BYTES",
    "WORKSPACE_ROOT",
    "ALLOWLIST_PATH",
    "RECEIPT_LOG_PATH",
    "build_operator_device_admin_payload",
    "build_operator_device_admin_preview_payload",
]
