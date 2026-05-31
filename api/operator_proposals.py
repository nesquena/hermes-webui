"""Manual read-only operator proposals for the composer action popover.

Slice 2 deliberately stops at suggestion/drafting. This module reads only the
allowlisted local sources and Slice 1 truth payload, then returns deterministic
proposal metadata. It never dispatches, posts, shells out, starts cron, or writes
approval state.
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

WORKSPACE_ROOT = Path("/mnt/c/Users/malac/.openclaw/workspace/main")
SOURCE_SPECS: dict[str, Path] = {
    "active_plan": WORKSPACE_ROOT / "obsidian-vault" / "Agent-Shared" / "ACTIVE PLAN.md",
    "wake_state": WORKSPACE_ROOT / "obsidian-vault" / "Agent-Kimi" / "WAKE_STATE.md",
    "kanban_hardening": WORKSPACE_ROOT / "obsidian-vault" / "Agent-Kimi" / "Hermes Kanban Pilot Hardening.md",
    "action_summary": WORKSPACE_ROOT / "artifacts" / "hermes-video-RoBD7Lc-0MI" / "action-summary.json",
}

MAX_SOURCE_BYTES = 200_000
MAX_PROPOSALS = 3
STALE_AFTER_SECONDS = 72 * 60 * 60
STATUS_ORDER = {"live": 0, "stale": 1, "unknown": 2}
_REQUIRED_SOURCE_IDS = {"active_plan", "wake_state", "kanban_hardening", "action_summary"}
_REQUIRED_ACTION_KEYS = {"id", "rank", "type", "title", "summary", "owner", "side_effect_level", "status"}
_DATE_RE = re.compile(r"\b(20\d{2}-\d{2}-\d{2})(?:[T ][0-2]\d:[0-5]\d(?::[0-5]\d)?Z?)?\b")


def build_operator_proposal_payload(
    *,
    session_id: str | None = None,
    ui_board_hint: str | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """Build the versioned manual Reverse Prompt proposal payload.

    Missing or malformed required sources degrade to ``unknown``. The function
    may return zero proposals, but it never fabricates fallback actions.
    """

    generated_at = float(time.time() if now is None else now)
    source_specs = dict(SOURCE_SPECS)
    sources: list[dict[str, Any]] = []
    issues: list[str] = []

    for source_id in ("active_plan", "wake_state", "kanban_hardening"):
        path = source_specs.get(source_id)
        _text, source = _read_text_source(source_id, path, now=generated_at)
        sources.append(source)
        if source.get("issue"):
            issues.append(f"{source_id}: {source['issue']}")

    action_data, action_source = _read_json_source("action_summary", source_specs.get("action_summary"), now=generated_at)
    sources.append(action_source)
    if action_source.get("issue"):
        issues.append(f"action_summary: {action_source['issue']}")

    truth = _operator_truth_summary(session_id=session_id, ui_board_hint=ui_board_hint, now=generated_at)
    if truth.get("issue"):
        issues.append(f"operator_truth: {truth['issue']}")

    proposals: list[dict[str, Any]] = []
    action_issue = _validate_action_summary(action_data)
    if action_issue:
        issues.append(f"action_summary: {action_issue}")
        action_source["state"] = "unknown"
        action_source["issue"] = action_issue
    elif truth.get("status") == "unknown":
        issues.append("operator_truth: unknown; refusing to produce proposals without proof state")
    else:
        action_summary = action_data if isinstance(action_data, dict) else {}
        source_map = {source["id"]: source for source in sources}
        ranked_actions = sorted(
            [item for item in action_summary.get("ranked_actions", []) if isinstance(item, dict)],
            key=lambda item: _rank_key(item.get("rank")),
        )
        for action in ranked_actions[:MAX_PROPOSALS]:
            proposals.append(_proposal_from_ranked_action(action, len(proposals) + 1, source_map, truth, action_summary))

    status_inputs = [source.get("state", "unknown") for source in sources]
    status_inputs.append(str(truth.get("status") or "unknown"))
    if action_issue or not proposals:
        status_inputs.append("unknown")
    status = _worst_status(status_inputs)

    readable_source_count = sum(1 for source in sources if source.get("exists") and source.get("state") != "unknown")
    if proposals:
        summary = f"{len(proposals)} proposal{'s' if len(proposals) != 1 else ''} from {readable_source_count} sources"
    elif status == "unknown":
        summary = "No safe proposals — source unavailable"
    else:
        summary = "No proposals available"

    return {
        "version": 1,
        "generated_at": generated_at,
        "status": status,
        "summary": summary,
        "mode": "manual-read-only",
        "would_execute": False,
        "truth": truth,
        "proposals": proposals,
        "sources": sources,
        "issues": issues,
    }


def _source_stat(source_id: str, path: Any, *, kind: str, required: bool = True, now: float | None = None) -> dict[str, Any]:
    state = "unknown"
    item: dict[str, Any] = {
        "id": source_id,
        "kind": kind,
        "path": _safe_display_path(path),
        "exists": False,
        "required": bool(required),
        "state": state,
    }
    if not path:
        if required:
            item["issue"] = "path unavailable"
        return item
    try:
        p = Path(path)
        item["exists"] = p.exists()
        if not item["exists"]:
            if required:
                item["issue"] = "missing"
            return item
        if not p.is_file():
            item["state"] = "unknown"
            item["issue"] = "not a regular file"
            return item
        stat = p.stat()
        item["mtime"] = stat.st_mtime
        item["state"] = "live"
        return item
    except Exception as exc:  # pragma: no cover - platform/path edge case
        item["issue"] = f"unreadable: {_short_error(exc)}"
        return item


def _read_text_source(source_id: str, path: Any, *, now: float) -> tuple[str | None, dict[str, Any]]:
    source = _source_stat(source_id, path, kind="markdown", required=True, now=now)
    if source.get("state") == "unknown":
        return None, source
    try:
        text = _read_bounded_text(Path(path))
    except Exception as exc:
        source["state"] = "unknown"
        source["issue"] = f"unreadable: {_short_error(exc)}"
        return None, source
    embedded = _extract_embedded_timestamp(text)
    if embedded:
        source["embedded_at"] = embedded.isoformat().replace("+00:00", "Z")
        if now >= embedded.timestamp() and now - embedded.timestamp() > STALE_AFTER_SECONDS:
            source["state"] = "stale"
            source["issue"] = "embedded timestamp is stale"
    return text, source


def _read_json_source(source_id: str, path: Any, *, now: float) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    source = _source_stat(source_id, path, kind="json", required=True, now=now)
    if source.get("state") == "unknown":
        return None, source
    try:
        raw = _read_bounded_text(Path(path))
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        source["state"] = "unknown"
        source["issue"] = f"malformed JSON: {exc.msg}"
        return None, source
    except Exception as exc:
        source["state"] = "unknown"
        source["issue"] = f"unreadable: {_short_error(exc)}"
        return None, source
    if not isinstance(data, dict):
        source["state"] = "unknown"
        source["issue"] = "malformed JSON: top-level object required"
        return None, source
    embedded = _extract_action_summary_timestamp(data)
    if embedded:
        source["embedded_at"] = embedded.isoformat().replace("+00:00", "Z")
        if now >= embedded.timestamp() and now - embedded.timestamp() > STALE_AFTER_SECONDS:
            source["state"] = "stale"
            source["issue"] = "action summary date is stale"
    return data, source


def _read_bounded_text(path: Path) -> str:
    size = path.stat().st_size
    if size > MAX_SOURCE_BYTES:
        raise ValueError(f"source exceeds {MAX_SOURCE_BYTES} byte limit")
    return path.read_text(encoding="utf-8-sig")


def _operator_truth_summary(*, session_id: str | None, ui_board_hint: str | None, now: float) -> dict[str, Any]:
    try:
        from api.operator_truth import build_operator_truth_payload

        payload = build_operator_truth_payload(session_id=session_id, ui_board_hint=ui_board_hint, now=now)
    except Exception as exc:
        return {
            "status": "unknown",
            "verified_at": now,
            "summary": "Truth unavailable",
            "api": "/api/operator/truth",
            "issue": _short_error(exc),
        }
    raw_status = payload.get("status")
    status = raw_status if isinstance(raw_status, str) and raw_status in STATUS_ORDER else "unknown"
    truth = {
        "status": status,
        "verified_at": payload.get("verified_at", now),
        "summary": payload.get("summary") or _summary_for_truth_status(status),
        "api": "/api/operator/truth",
    }
    if payload.get("issues"):
        truth["issues"] = [str(issue) for issue in payload.get("issues", [])[:5]]
    return truth


def _validate_action_summary(data: dict[str, Any] | None) -> str | None:
    if not isinstance(data, dict):
        return "missing or malformed action summary"
    ranked_actions = data.get("ranked_actions")
    if not isinstance(ranked_actions, list) or not ranked_actions:
        return "ranked_actions missing or empty"
    seen_ids: set[str] = set()
    for index, action in enumerate(ranked_actions):
        if not isinstance(action, dict):
            return f"ranked_actions[{index}] is not an object"
        missing = sorted(_REQUIRED_ACTION_KEYS - set(action))
        if missing:
            return f"ranked_actions[{index}] missing keys: {', '.join(missing)}"
        if not isinstance(action.get("rank"), int):
            return f"ranked_actions[{index}].rank must be an integer"
        for key in sorted(_REQUIRED_ACTION_KEYS - {"rank"}):
            if not isinstance(action.get(key), str) or not action.get(key, "").strip():
                return f"ranked_actions[{index}].{key} must be a non-empty string"
        action_id = action["id"].strip()
        if action_id in seen_ids:
            return f"ranked_actions[{index}].id duplicate: {action_id}"
        seen_ids.add(action_id)
    return None


def _proposal_from_ranked_action(
    action: dict[str, Any],
    rank: int,
    source_map: dict[str, dict[str, Any]],
    truth: dict[str, Any],
    action_data: dict[str, Any],
) -> dict[str, Any]:
    proposal_id = _clean_id(action.get("id"))
    title = _clean_text(action.get("title"), fallback=proposal_id or "Proposal")
    summary = _clean_text(action.get("summary"), fallback="Source-backed operator proposal.")
    side_effect_level = _clean_text(action.get("side_effect_level"), fallback="manual draft only")
    source_index = _source_action_index(action_data.get("ranked_actions", []), action)
    avoid = [str(item).strip() for item in action_data.get("avoid", []) if str(item).strip()]
    evidence = [
        {
            "source_id": "action_summary",
            "path": source_map.get("action_summary", {}).get("path", ""),
            "field": f"ranked_actions[{source_index}]",
        },
        {"source_id": "active_plan", "path": source_map.get("active_plan", {}).get("path", "")},
        {"source_id": "wake_state", "path": source_map.get("wake_state", {}).get("path", "")},
        {"source_id": "kanban_hardening", "path": source_map.get("kanban_hardening", {}).get("path", "")},
        {
            "source_id": "operator_truth",
            "api": "/api/operator/truth",
            "status": truth.get("status", "unknown"),
            "summary": truth.get("summary", ""),
        },
    ]
    safety_notes = [
        "Manual only; no automatic execution",
        "Draft handoff only; does not send or execute",
        "No AIM cron/board/runtime revival",
    ]
    safety_notes.extend(f"Avoid: {item}" for item in avoid[:3])
    return {
        "id": proposal_id,
        "rank": rank,
        "source_rank": action.get("rank"),
        "title": title,
        "summary": summary,
        "type": _clean_text(action.get("type"), fallback="proposal"),
        "owner": _clean_text(action.get("owner"), fallback="future Hermes session"),
        "side_effect_level": side_effect_level,
        "source_status": _clean_text(action.get("status"), fallback="proposal"),
        "would_execute": False,
        "approval": {"kind": "draft_only", "label": "Draft handoff", "executes": False},
        "decline": {"kind": "client_only", "label": "Dismiss", "executes": False},
        "evidence": evidence,
        "safety_notes": safety_notes,
        "handoff_prompt": _build_handoff_prompt(action, truth, source_map, safety_notes),
    }


def _build_handoff_prompt(
    action: dict[str, Any], truth: dict[str, Any], source_map: dict[str, dict[str, Any]], safety_notes: list[str]
) -> str:
    title = _clean_text(action.get("title"), fallback="Manual operator proposal")
    summary = _clean_text(action.get("summary"), fallback="Review the source-backed proposal and decide next steps.")
    source_lines = []
    for source_id in ("active_plan", "wake_state", "kanban_hardening", "action_summary"):
        path = source_map.get(source_id, {}).get("path", "")
        source_lines.append(f"- {source_id}: {path}")
    safety = "\n".join(f"- {note}" for note in safety_notes[:6])
    return (
        "Load hermes-agent, test-driven-development, systematic-debugging, and requesting-code-review.\n"
        f"Review this manual read-only operator proposal without executing it yet: {title}.\n\n"
        f"Summary: {summary}\n"
        f"Truth status: {truth.get('status', 'unknown')} — {truth.get('summary', '')}\n\n"
        "Source receipts:\n"
        + "\n".join(source_lines)
        + "\n\nSafety constraints:\n"
        + safety
        + "\n\nIf you proceed, use TDD where code changes are required. Do not send, dispatch, create cron jobs, revive AIM runtime, or execute anything automatically from this draft."
    )


def _worst_status(statuses: Iterable[str]) -> str:
    worst = "live"
    for status in statuses:
        normalized = status if status in STATUS_ORDER else "unknown"
        if STATUS_ORDER[normalized] > STATUS_ORDER[worst]:
            worst = normalized
    return worst


def _rank_key(rank: Any) -> tuple[int, str]:
    if isinstance(rank, int):
        return (rank, "")
    try:
        return (int(rank), "")
    except Exception:
        return (999_999, str(rank))


def _source_action_index(actions: Any, target: dict[str, Any]) -> int:
    if not isinstance(actions, list):
        return -1
    target_id = target.get("id")
    target_rank = target.get("rank")
    for index, action in enumerate(actions):
        if isinstance(action, dict) and action.get("id") == target_id and action.get("rank") == target_rank:
            return index
    return -1


def _extract_embedded_timestamp(text: str) -> datetime | None:
    match = _DATE_RE.search(text[:5000])
    if not match:
        return None
    return _parse_date(match.group(1))


def _extract_action_summary_timestamp(data: dict[str, Any]) -> datetime | None:
    raw = data.get("date")
    if isinstance(raw, str):
        return _parse_date(raw[:10])
    return None


def _parse_date(text: str) -> datetime | None:
    try:
        return datetime.strptime(text, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _summary_for_truth_status(status: str) -> str:
    if status == "live":
        return "Truth live"
    if status == "stale":
        return "Truth stale"
    return "Truth unknown"


def _clean_id(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return "proposal"
    text = re.sub(r"[^A-Za-z0-9_.-]+", "_", text)
    return text[:96] or "proposal"


def _clean_text(value: Any, *, fallback: str) -> str:
    text = str(value or "").strip()
    if not text:
        return fallback
    return " ".join(text.split())[:1000]


def _safe_display_path(path: Any) -> str:
    if not path:
        return ""
    text = str(path)
    try:
        home = str(Path.home())
        if text == home:
            text = "~"
        elif text.startswith(home + "/"):
            text = "~" + text[len(home) :]
    except Exception:
        pass
    if len(text) <= 120:
        return text
    parts = Path(text).parts
    if len(parts) >= 4:
        return "…/" + "/".join(parts[-4:])
    return "…" + text[-117:]


def _short_error(exc: BaseException) -> str:
    text = str(exc).strip()
    return text or exc.__class__.__name__
