"""Local memory/skill mutation review queue for the Hermes operator surface.

Slice 5 makes proposed memory/skill changes reviewable. It writes only local
review queue state and never applies memory/skill mutations.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from api.helpers import _redact_value

STORE_VERSION = 1
STATUS_ORDER = {"live": 0, "stale": 1, "unknown": 2}
VALID_DECISIONS = {"pending", "approved", "denied", "stale", "superseded", "invalid"}
VALID_DURABILITY = {"durable", "transient", "unknown"}
VALID_TARGET_KINDS = {"memory", "skill"}
VALID_MEMORY_SECTIONS = {"memory", "user", "soul"}
VALID_OPERATIONS = {"append", "edit", "delete"}
VALID_TRANSIENT_RISK = {"low", "medium", "high"}
VALID_STALE_STATES = {"current", "review_required", "expired"}
VALID_EVIDENCE_KINDS = {"session_message", "tool_receipt", "file_receipt", "manual_note"}
SAFE_SEGMENT_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
SECRETISH_RE = re.compile(r"(?i)(api[_-]?key|api[_-]?token|token|secret|password|passwd|hunter2|sk-[A-Za-z0-9_.-]+)")
SHA256_RE = re.compile(r"^sha256:[0-9a-fA-F]{64}$")
RAW_KEYED_SECRET_RE = re.compile(
    r"""
    \b(?:
        password|passwd|pwd|
        token|access[_\-\s]?token|refresh[_\-\s]?token|auth[_\-\s]?token|
        api[_\-\s]?key|apikey|
        secret|client[_\-\s]?secret|private[_\-\s]?key|
        authorization
    )\b
    \s*[:=]\s*
    (?P<value>["']?[^"'\s,;]+["']?)
    """,
    re.IGNORECASE | re.VERBOSE,
)
RAW_BEARER_SECRET_RE = re.compile(r"\bBearer\s+(?P<value>[A-Za-z0-9._~+/=-]{8,})(?=$|[^A-Za-z0-9._~+/=-])", re.IGNORECASE)
RAW_OPENAI_SECRET_RE = re.compile(r"\bsk-[A-Za-z0-9][A-Za-z0-9_-]{16,}(?=$|[^A-Za-z0-9_-])", re.IGNORECASE)
RAW_SLACK_SECRET_RE = re.compile(r"\bxox[abp]-[0-9A-Za-z-]{10,}(?=$|[^0-9A-Za-z-])", re.IGNORECASE)
RAW_GITHUB_SECRET_RE = re.compile(r"\b(?:ghp|github_pat)_[A-Za-z0-9_]{16,}(?![A-Za-z0-9_])", re.IGNORECASE)
_STORE_LOCK = threading.RLock()


def review_store_path() -> Path:
    """Return the local WebUI state path for the memory/skill review queue."""
    from api import config

    return Path(config.STATE_DIR) / "operator_memory_skill_review.json"


def build_operator_memory_skill_review_payload(
    *,
    session_id: str | None = None,
    ui_board_hint: str | None = None,
    now: float | None = None,
    profile: str | None = None,
) -> dict[str, Any]:
    """Build a versioned local memory/skill review queue payload."""

    generated_at = float(time.time() if now is None else now)
    store = review_store_path()
    sources = [{"id": "memory_skill_review_store", "kind": "local_json", "path": str(store), "state": "unknown"}]
    issues: list[str] = []
    notes: list[dict[str, Any]] = []

    if not store.exists():
        issues.append("memory_skill_review_store: missing or unavailable")
        return _payload(
            generated_at=generated_at,
            status="unknown",
            summary="Memory/skill review queue unavailable — local store missing",
            items=[],
            notes=notes,
            sources=sources,
            issues=issues,
        )

    data, read_issue = _read_store(store)
    if read_issue:
        sources[0]["issue"] = read_issue
        issues.append(f"memory_skill_review_store: {read_issue}")
        return _payload(
            generated_at=generated_at,
            status="unknown",
            summary="Memory/skill review queue unavailable — local store malformed",
            items=[],
            notes=notes,
            sources=sources,
            issues=issues,
        )

    sources[0]["state"] = "live"
    raw_items = data.get("items", []) if isinstance(data, dict) else []
    items: list[dict[str, Any]] = []
    status_inputs = ["live"]
    for index, record in enumerate(raw_items):
        item, note = _normalize_stored_item(record, generated_at=generated_at, profile=profile)
        if item:
            items.append(_redacted_payload(item))
            stale_state = item.get("stale_risk", {}).get("state") if isinstance(item.get("stale_risk"), dict) else None
            if stale_state in {"expired", "review_required"}:
                status_inputs.append("stale")
                issues.append(f"memory_skill_review_store[{index}]: stale_risk {stale_state}")
        if note:
            notes.append(note)
            missing = note.get("missing") if isinstance(note.get("missing"), list) else []
            suffix = f" missing={','.join(str(field) for field in missing)}" if missing else ""
            issues.append(f"memory_skill_review_store[{index}]: {note.get('reason', 'invalid review item')}{suffix}")
            raw_note_issues = note.get("issues")
            note_issues = raw_note_issues if isinstance(raw_note_issues, list) else []
            for issue in note_issues:
                issues.append(f"memory_skill_review_store[{index}]: {issue}")
            status_inputs.append("stale")

    summary = f"{len(items)} memory/skill review item{'s' if len(items) != 1 else ''} from local state"
    if notes:
        summary += f"; {len(notes)} note{'s' if len(notes) != 1 else ''} need required fields"
    if not items and not notes:
        summary = "0 memory/skill review items from local state"

    return _payload(
        generated_at=generated_at,
        status=_worst_status(status_inputs),
        summary=summary,
        items=items,
        notes=notes,
        sources=sources,
        issues=_dedupe(issues),
    )


def propose_operator_memory_skill_review(
    body: dict[str, Any],
    *,
    now: float | None = None,
    client_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create or replace a local review-queue item without applying it."""

    generated_at = float(time.time() if now is None else now)
    request = body if isinstance(body, dict) else {}
    target, target_issue = _resolve_target_previous_content(request.get("target"))
    if target_issue:
        return {"ok": False, "error": target_issue, "issues": [target_issue], "would_execute": False}

    proposed_change = _copy_dict(request.get("proposed_change"))
    source_evidence = _copy_list(request.get("source_evidence"))
    classification = _copy_dict(request.get("classification"))
    stale_risk = _copy_dict(request.get("stale_risk"))
    raw_secret_fields = _raw_secret_request_fields(
        proposed_change=proposed_change,
        classification=classification,
        stale_risk=stale_risk,
    )
    if raw_secret_fields:
        return {
            "ok": False,
            "error": "raw secret-looking text is not allowed in review proposals",
            "missing": raw_secret_fields,
            "issues": ["raw secret-looking text is not allowed in review proposals"],
            "would_execute": False,
        }
    previous_content = _sanitize_store_value(target.pop("previous_content", ""))

    item = {
        "id": "msr_" + hashlib.sha256(
            json.dumps(
                {
                    "profile": _profile_from_context(client_context),
                    "target": target,
                    "proposed_change": proposed_change,
                    "source_evidence": source_evidence,
                },
                sort_keys=True,
                ensure_ascii=False,
            ).encode("utf-8")
        ).hexdigest()[:16],
        "created_at": generated_at,
        "updated_at": generated_at,
        "profile": _profile_from_context(client_context),
        "target": target,
        "proposed_change": proposed_change,
        "previous_content": previous_content,
        "source_evidence": source_evidence,
        "classification": classification,
        "stale_risk": stale_risk,
        "decision": {"state": "pending", "decided_at": None, "decided_by": None, "reason": ""},
        "rollback": {
            "previous_hash": _sha256(previous_content),
            "previous_excerpt": _excerpt(previous_content),
        },
        "would_execute": False,
    }

    normalized, note = _normalize_stored_item(item, generated_at=generated_at, profile=item["profile"])
    if not normalized:
        return {
            "ok": False,
            "error": note.get("reason", "invalid review item") if note else "invalid review item",
            "missing": note.get("missing", []) if note else [],
            "issues": [note.get("reason", "invalid review item")] if note else ["invalid review item"],
            "would_execute": False,
        }

    with _STORE_LOCK:
        store, store_issue = _load_store_for_write()
        if store_issue:
            return {"ok": False, "error": store_issue, "issues": [store_issue], "would_execute": False}
        items = store.setdefault("items", [])
        replaced = False
        for index, existing in enumerate(list(items)):
            if isinstance(existing, dict) and existing.get("id") == normalized["id"]:
                normalized["created_at"] = existing.get("created_at", normalized["created_at"])
                normalized["decision"] = existing.get("decision", normalized["decision"])
                items[index] = normalized
                replaced = True
                break
        if not replaced:
            items.append(normalized)
        store["version"] = STORE_VERSION
        store["updated_at"] = generated_at
        write_issue = _write_store(store)
        if write_issue:
            return {"ok": False, "error": write_issue, "issues": [write_issue], "would_execute": False}
    return {"ok": True, "id": normalized["id"], "item": _redacted_payload(normalized), "would_execute": False}


def decide_operator_memory_skill_review(
    body: dict[str, Any],
    *,
    now: float | None = None,
    client_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Update only the local review decision state for an existing item."""

    generated_at = float(time.time() if now is None else now)
    request = body if isinstance(body, dict) else {}
    item_id = str(request.get("id") or "").strip()
    decision = str(request.get("decision") or "").strip().lower()
    reason = str(request.get("reason") or "").strip()[:1000]
    if not item_id:
        return {"ok": False, "error": "id is required", "would_execute": False}
    if decision not in {"approved", "denied"}:
        return {"ok": False, "error": "decision must be approved or denied", "would_execute": False}

    with _STORE_LOCK:
        store, store_issue = _load_store_for_write()
        if store_issue:
            return {"ok": False, "error": store_issue, "issues": [store_issue], "would_execute": False}
        items = store.setdefault("items", [])
        for index, record in enumerate(list(items)):
            if not isinstance(record, dict) or record.get("id") != item_id:
                continue
            normalized, note = _normalize_stored_item(record, generated_at=generated_at, profile=record.get("profile"))
            if decision == "approved" and (not normalized or _item_is_stale(normalized)):
                issue = "cannot approve invalid or stale review item"
                if note:
                    issue = note.get("reason", issue)
                return {"ok": False, "error": issue, "issues": [issue], "would_execute": False}
            updated = copy.deepcopy(record)
            updated["decision"] = {
                "state": decision,
                "decided_at": generated_at,
                "decided_by": _decider_from_context(client_context),
                "reason": _sanitize_store_value(reason),
            }
            items[index] = updated
            store["version"] = STORE_VERSION
            store["updated_at"] = generated_at
            write_issue = _write_store(store)
            if write_issue:
                return {"ok": False, "error": write_issue, "issues": [write_issue], "would_execute": False}
            return {"ok": True, "id": item_id, "decision": _redacted_payload(updated["decision"]), "would_execute": False}
    return {"ok": False, "error": "review item not found", "would_execute": False}


def _payload(*, generated_at: float, status: str, summary: str, items: list, notes: list, sources: list, issues: list) -> dict[str, Any]:
    return {
        "version": STORE_VERSION,
        "generated_at": generated_at,
        "mode": "local-memory-skill-review-queue",
        "status": status if status in STATUS_ORDER else "unknown",
        "summary": summary,
        "would_execute": False,
        "items": items,
        "notes": notes,
        "sources": sources,
        "issues": _dedupe(issues),
    }


def _read_store(path: Path) -> tuple[dict[str, Any], str | None]:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return {}, "missing or unavailable"
    except Exception as exc:
        return {}, f"malformed json: {exc}"
    if not isinstance(data, dict):
        return {}, "store root must be an object"
    if data.get("version") != STORE_VERSION:
        return {}, "unsupported store version"
    if not isinstance(data.get("items", []), list):
        return {}, "items must be a list"
    return data, None


def _load_store_for_write() -> tuple[dict[str, Any], str | None]:
    path = review_store_path()
    if not path.exists():
        return {"version": STORE_VERSION, "updated_at": 0.0, "items": []}, None
    return _read_store(path)


def _write_store(data: dict[str, Any]) -> str | None:
    path = review_store_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False)
        tmp = path.with_name(f".{path.name}.tmp")
        with _STORE_LOCK:
            with tmp.open("w", encoding="utf-8") as fh:
                fh.write(payload)
                fh.write("\n")
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
        return None
    except Exception as exc:
        return f"failed to write review store: {exc}"


def _normalize_stored_item(record: Any, *, generated_at: float, profile: str | None = None) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    if not isinstance(record, dict):
        return None, _note(record, reason="review item must be an object", missing=_required_fields())
    missing = [field for field in _required_fields() if field not in record]
    issues: list[str] = []
    target = _copy_dict(record.get("target"))
    proposed_change = _copy_dict(record.get("proposed_change"))
    evidence = _copy_list(record.get("source_evidence"))
    classification = _copy_dict(record.get("classification"))
    stale_risk = _copy_dict(record.get("stale_risk"))
    decision = _copy_dict(record.get("decision"))
    rollback = _copy_dict(record.get("rollback"))

    if not _target_has_proof(target):
        missing.append("target")
        issues.append("target must include valid kind and resolved file_path/path")
    if not _proposed_change_has_proof(proposed_change):
        missing.append("proposed_change")
        issues.append("proposed_change must include operation, summary, diff, and proposed content unless delete")
    if not _source_evidence_has_proof(evidence):
        missing.append("source_evidence")
        issues.append("source_evidence must include source-backed quote/hash proof")
    if not _classification_is_reviewable(classification):
        missing.append("classification")
        issues.append("classification must include durability, reason, and transient_risk")
    stale_state, stale_issue = _stale_risk_state(stale_risk, now=generated_at)
    if stale_issue:
        missing.append("stale_risk")
        issues.append(stale_issue)
    if not _decision_has_proof(decision):
        missing.append("decision")
        issues.append("decision must include a valid state")
    op = str(proposed_change.get("operation") or "").strip().lower()
    previous_content = record.get("previous_content")
    if op in {"edit", "delete"} and not isinstance(previous_content, str):
        missing.append("previous_content")
        issues.append(f"{op} proposals require previous_content")
    if not _rollback_has_proof(rollback):
        missing.append("rollback")
        issues.append("rollback must include previous_hash and previous_excerpt")

    if missing:
        return None, _note(record, reason="invalid memory/skill review item", missing=sorted(set(missing)), issues=_dedupe(issues))

    item = copy.deepcopy(record)
    item["id"] = str(item.get("id") or _stable_item_id(item))
    item["created_at"] = _number(item.get("created_at"), generated_at)
    item["updated_at"] = _number(item.get("updated_at"), item["created_at"])
    item["profile"] = str(item.get("profile") or profile or "default")
    item["target"] = target
    item["proposed_change"] = proposed_change
    item["source_evidence"] = evidence
    item["classification"] = classification
    if stale_state:
        stale_risk["state"] = stale_state
    item["stale_risk"] = stale_risk
    item["decision"] = decision
    item["rollback"] = rollback
    item["would_execute"] = False
    return item, None


def _target_has_proof(target: dict[str, Any]) -> bool:
    kind = str(target.get("kind") or "").strip().lower()
    if kind not in VALID_TARGET_KINDS:
        return False
    if not target.get("file_path") or not target.get("path"):
        return False
    if kind == "memory":
        return str(target.get("section") or "").strip().lower() in VALID_MEMORY_SECTIONS
    if kind == "skill":
        return bool(str(target.get("name") or "").strip())
    return False


def _proposed_change_has_proof(change: dict[str, Any]) -> bool:
    operation = str(change.get("operation") or "").strip().lower()
    if operation not in VALID_OPERATIONS:
        return False
    if not str(change.get("summary") or "").strip():
        return False
    if not str(change.get("diff") or "").strip():
        return False
    if operation != "delete" and not isinstance(change.get("proposed_content"), str):
        return False
    return True


def _source_evidence_has_proof(items: list[Any]) -> bool:
    if not items:
        return False
    for entry in items:
        if not isinstance(entry, dict):
            return False
        if not isinstance(entry.get("kind"), str):
            return False
        if not isinstance(entry.get("quote"), str):
            return False
        if not isinstance(entry.get("content_hash"), str):
            return False
        kind = entry.get("kind", "").strip()
        quote = entry.get("quote", "").strip()
        content_hash = entry.get("content_hash", "").strip()
        if kind not in VALID_EVIDENCE_KINDS or not quote or not _is_sha256_hash(content_hash):
            return False
        if _quote_contains_raw_secret(quote):
            return False
        if kind == "session_message" and not _session_message_evidence_has_proof(entry):
            return False
    return True


def _session_message_evidence_has_proof(entry: dict[str, Any]) -> bool:
    if not isinstance(entry.get("session_id"), str):
        return False
    session_id = entry.get("session_id", "").strip()
    if not session_id:
        return False
    index = entry.get("message_index")
    if isinstance(index, bool):
        return False
    if isinstance(index, int):
        return index >= 0
    if isinstance(index, str):
        text = index.strip()
        return text.isdigit()
    return False


def _secret_value_redacted(value: str) -> bool:
    token = str(value or "").strip().strip("\"'")
    normalized = token.strip("[](){}<>").lower()
    if normalized in {"redacted", "masked", "hidden", "removed", "withheld", "scrubbed"}:
        return True
    compact = re.sub(r"\s+", "", token)
    return bool(compact) and bool(re.fullmatch(r"[*xX•…]+", compact))


def _quote_contains_raw_secret(quote: str) -> bool:
    text = str(quote or "")
    for match in RAW_KEYED_SECRET_RE.finditer(text):
        if not _secret_value_redacted(match.group("value")):
            return True
    for pattern in (RAW_BEARER_SECRET_RE, RAW_OPENAI_SECRET_RE, RAW_SLACK_SECRET_RE, RAW_GITHUB_SECRET_RE):
        if pattern.search(text):
            return True
    return False


def _raw_secret_request_fields(*, proposed_change: dict[str, Any], classification: dict[str, Any], stale_risk: dict[str, Any]) -> list[str]:
    missing: list[str] = []
    if _payload_contains_raw_secret(proposed_change):
        missing.append("proposed_change")
    if _payload_contains_raw_secret(classification):
        missing.append("classification")
    if _payload_contains_raw_secret(stale_risk):
        missing.append("stale_risk")
    return missing


def _payload_contains_raw_secret(value: Any) -> bool:
    if isinstance(value, str):
        return _quote_contains_raw_secret(value)
    if isinstance(value, dict):
        return any(_payload_contains_raw_secret(key) or _payload_contains_raw_secret(item) for key, item in value.items())
    if isinstance(value, list):
        return any(_payload_contains_raw_secret(item) for item in value)
    return False


def _sanitize_store_value(value: Any) -> Any:
    return _redact_secretish(_redact_value(copy.deepcopy(value)))


def _classification_is_reviewable(classification: dict[str, Any]) -> bool:
    durability = str(classification.get("durability") or "").strip().lower()
    reason = str(classification.get("reason") or "").strip()
    risk = str(classification.get("transient_risk") or "").strip().lower()
    return durability in VALID_DURABILITY and bool(reason) and risk in VALID_TRANSIENT_RISK


def _stale_risk_state(stale_risk: dict[str, Any], *, now: float) -> tuple[str | None, str | None]:
    state = str(stale_risk.get("state") or "").strip().lower()
    expires_at = str(stale_risk.get("expires_at") or "").strip()
    reason = str(stale_risk.get("reason") or "").strip()
    if state not in VALID_STALE_STATES:
        return None, "stale_risk must include a valid state"
    if not expires_at:
        return None, "stale_risk requires expires_at"
    if not reason:
        return None, "stale_risk requires reason"
    expires_ts = _parse_iso_ts(expires_at)
    if expires_ts is None:
        return None, "stale_risk expires_at must be an ISO timestamp"
    if expires_ts <= now:
        return "expired", None
    return state, None


def _decision_has_proof(decision: dict[str, Any]) -> bool:
    state = str(decision.get("state") or "").strip().lower()
    return state in VALID_DECISIONS


def _rollback_has_proof(rollback: dict[str, Any]) -> bool:
    previous_hash = str(rollback.get("previous_hash") or "").strip()
    excerpt = str(rollback.get("previous_excerpt") or "").strip()
    return _is_sha256_hash(previous_hash) and bool(excerpt)


def _is_sha256_hash(value: str) -> bool:
    return bool(SHA256_RE.fullmatch(str(value or "").strip()))


def _resolve_target_previous_content(raw_target: Any) -> tuple[dict[str, Any], str | None]:
    target = _copy_dict(raw_target)
    kind = str(target.get("kind") or "").strip().lower()
    if kind == "memory":
        return _resolve_memory_target(target)
    if kind == "skill":
        return _resolve_skill_target(target)
    return {}, "target.kind must be memory or skill"


def _resolve_memory_target(target: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
    from api.profiles import get_active_hermes_home

    section = str(target.get("section") or "").strip().lower()
    if section not in VALID_MEMORY_SECTIONS:
        return {}, "memory target section must be memory, user, or soul"
    home = Path(get_active_hermes_home()).resolve()
    if section == "memory":
        path = home / "memories" / "MEMORY.md"
        file_path = "MEMORY.md"
    elif section == "user":
        path = home / "memories" / "USER.md"
        file_path = "USER.md"
    else:
        path = home / "SOUL.md"
        file_path = "SOUL.md"
    previous = _read_text_if_exists(path)
    return {
        "kind": "memory",
        "section": section,
        "file_path": file_path,
        "path": str(path),
        "previous_content": previous,
    }, None


def _resolve_skill_target(target: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
    from api.profiles import get_active_hermes_home

    name = str(target.get("name") or "").strip()
    category = str(target.get("category") or "").strip()
    if not SAFE_SEGMENT_RE.fullmatch(name):
        return {}, "skill target name is invalid"
    if category and not SAFE_SEGMENT_RE.fullmatch(category):
        return {}, "skill target category is invalid"
    home = Path(get_active_hermes_home()).resolve()
    skills_root = (home / "skills").resolve()
    path = (skills_root / category / name / "SKILL.md").resolve() if category else (skills_root / name / "SKILL.md").resolve()
    try:
        path.relative_to(skills_root)
    except ValueError:
        return {}, "skill target escapes active profile skills directory"
    if not path.exists():
        return {}, "skill target SKILL.md not found"
    previous = _read_text_if_exists(path)
    resolved = {
        "kind": "skill",
        "name": name,
        "file_path": "SKILL.md",
        "path": str(path),
        "previous_content": previous,
    }
    if category:
        resolved["category"] = category
    return resolved, None


def _redacted_payload(value: Any) -> Any:
    copied = copy.deepcopy(value)
    return _redact_secretish(_redact_value(copied))


def _redact_secretish(value: Any) -> Any:
    if isinstance(value, str):
        if SECRETISH_RE.search(value):
            return "[redacted sensitive text]"
        return value
    if isinstance(value, dict):
        return {key: _redact_secretish(val) for key, val in value.items()}
    if isinstance(value, list):
        return [_redact_secretish(item) for item in value]
    return value


def _note(record: Any, *, reason: str, missing: list[str], issues: list[str] | None = None) -> dict[str, Any]:
    return {
        "id": record.get("id") if isinstance(record, dict) else None,
        "classification": "invalid",
        "reason": reason,
        "missing": sorted(set(missing)),
        "issues": _dedupe(issues or []),
        "would_execute": False,
    }


def _required_fields() -> list[str]:
    return ["target", "proposed_change", "source_evidence", "classification", "stale_risk", "decision", "rollback", "would_execute"]


def _item_is_stale(item: dict[str, Any] | None) -> bool:
    if not item:
        return True
    state = item.get("stale_risk", {}).get("state") if isinstance(item.get("stale_risk"), dict) else None
    return state in {"expired", "review_required"}


def _copy_dict(value: Any) -> dict[str, Any]:
    return copy.deepcopy(value) if isinstance(value, dict) else {}


def _copy_list(value: Any) -> list[Any]:
    return copy.deepcopy(value) if isinstance(value, list) else []


def _read_text_if_exists(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return ""
    except Exception:
        return ""


def _parse_iso_ts(value: str) -> float | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _number(value: Any, default: float) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


def _sha256(text: str) -> str:
    return "sha256:" + hashlib.sha256(str(text or "").encode("utf-8")).hexdigest()


def _excerpt(text: str, limit: int = 500) -> str:
    cleaned = str(text or "").strip()
    return cleaned[:limit] if cleaned else "[empty previous content]"


def _stable_item_id(item: dict[str, Any]) -> str:
    seed = json.dumps({"target": item.get("target"), "proposed_change": item.get("proposed_change")}, sort_keys=True, ensure_ascii=False)
    return "msr_" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]


def _profile_from_context(client_context: dict[str, Any] | None) -> str:
    value = str((client_context or {}).get("profile") or "").strip()
    return value or "default"


def _decider_from_context(client_context: dict[str, Any] | None) -> str:
    client_ip = str((client_context or {}).get("client_ip") or "").strip()
    return client_ip or "local"


def _dedupe(items: list[Any]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        text = str(item)
        if text and text not in seen:
            seen.add(text)
            out.append(text)
    return out


def _worst_status(statuses: list[str]) -> str:
    worst = "live"
    for status in statuses:
        if STATUS_ORDER.get(status, 2) > STATUS_ORDER.get(worst, 2):
            worst = status
    return worst
