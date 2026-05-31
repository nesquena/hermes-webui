"""Local commitment cards for the Hermes operator surface.

Slice 4 turns source-backed promises into durable local objects. This module is
intentionally isolated from Kanban, cron, goals, chat execution, shell commands,
and external services. Promotion writes only to the WebUI local state directory.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

STATUS_ORDER = {"live": 0, "stale": 1, "unknown": 2}
VALID_COMMITMENT_STATUSES = {"active", "blocked", "met", "halted", "superseded"}
ALLOWED_DISPATCH_KINDS = {"manual", "handoff", "human_review"}
FORBIDDEN_DISPATCH_TOKENS = {
    "cron",
    "goal",
    "goal_loop",
    "background",
    "background_loop",
    "auto",
    "auto_dispatch",
    "dispatcher",
    "kanban_dispatch",
    "kanban_dispatcher",
    "shell",
    "webhook",
    "aim",
    "aim_cron",
    "aim_runtime",
}
PLACEHOLDER_TEXT = {"", "unknown", "none", "null", "tbd", "todo", "unassigned"}
MAX_TEXT = 500
MAX_LIST_ITEMS = 12
STORE_VERSION = 1
_SESSION_MESSAGE_CONTENT_HASH_RE = re.compile(r"^sha256:[0-9a-fA-F]{64}$")
_SESSION_MESSAGE_INDEX_RE = re.compile(r"^\d+$")
_SESSION_MESSAGE_ID_RE = re.compile(r"^[0-9A-Za-z_\-]+$")
_RAW_SECRET_QUOTE_RE = re.compile(
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
_RAW_BEARER_SECRET_QUOTE_RE = re.compile(r"\bBearer\s+(?P<value>[A-Za-z0-9._~+/=-]{16,})(?=$|[\s,;])", re.IGNORECASE)
_RAW_OPENAI_SECRET_QUOTE_RE = re.compile(r"\bsk-[A-Za-z0-9][A-Za-z0-9_-]{16,}(?=$|[\s,;])", re.IGNORECASE)
_RAW_SLACK_SECRET_QUOTE_RE = re.compile(r"\bxox[abp]-[0-9A-Za-z-]{10,}(?=$|[\s,;])", re.IGNORECASE)
_RAW_GITHUB_SECRET_QUOTE_RE = re.compile(r"\b(?:ghp|github_pat)_[A-Za-z0-9_]{16,}(?![A-Za-z0-9_])", re.IGNORECASE)
_STORE_LOCK = threading.Lock()


def commitment_store_path() -> Path:
    """Return the local WebUI state path for operator commitment cards."""
    from api import config

    return Path(config.STATE_DIR) / "operator_commitments.json"


def build_operator_commitments_payload(
    *,
    session_id: str | None = None,
    ui_board_hint: str | None = None,
    now: float | None = None,
    profile: str | None = None,
) -> dict[str, Any]:
    """Build a versioned local commitment-card payload.

    Missing, malformed, or invalid local state degrades to unknown/stale. The
    function never fabricates sample cards and never creates the store while
    reading.
    """

    generated_at = float(time.time() if now is None else now)
    store = commitment_store_path()
    sources: list[dict[str, Any]] = []
    issues: list[str] = []
    notes: list[dict[str, Any]] = []

    truth = _operator_truth_summary(session_id=session_id, ui_board_hint=ui_board_hint, now=generated_at)
    sources.append({"id": "operator_truth", "kind": "api", "api": "/api/operator/truth", "state": truth.get("status", "unknown")})
    if truth.get("status") in {"stale", "unknown"}:
        issues.append(f"operator_truth is {truth.get('status')}")
    if truth.get("issues"):
        issues.extend(f"operator_truth: {issue}" for issue in truth.get("issues", [])[:5])

    store_source = _store_source(store)
    sources.insert(0, store_source)
    if store_source.get("state") == "unknown":
        issue = store_source.get("issue") or "missing or unavailable"
        issues.append(f"commitment_store: {issue}")
        return _payload(
            generated_at=generated_at,
            status="unknown",
            summary="Commitments unavailable — local store missing or unreadable",
            commitments=[],
            notes=notes,
            truth=truth,
            sources=sources,
            issues=_dedupe(issues),
        )

    data, read_issue = _read_store(store)
    if read_issue:
        store_source["state"] = "unknown"
        store_source["issue"] = read_issue
        issues.append(f"commitment_store: {read_issue}")
        return _payload(
            generated_at=generated_at,
            status="unknown",
            summary="Commitments unavailable — local store malformed",
            commitments=[],
            notes=notes,
            truth=truth,
            sources=sources,
            issues=_dedupe(issues),
        )

    raw_items = data.get("commitments", []) if isinstance(data, dict) else []
    commitments: list[dict[str, Any]] = []
    status_inputs = ["live", store_source.get("state", "unknown"), truth.get("status", "unknown")]
    for index, item in enumerate(raw_items):
        card, note = _normalize_stored_commitment(item, generated_at=generated_at, profile=profile)
        if card:
            commitments.append(card)
        if note:
            notes.append(note)
            missing_fields = note.get("missing") if isinstance(note.get("missing"), list) else []
            missing_text = f" missing={','.join(str(item) for item in missing_fields)}" if missing_fields else ""
            issues.append(f"commitment_store[{index}]: {note.get('reason', 'invalid commitment')}{missing_text}")
            status_inputs.append("stale")

    status = _worst_status(status_inputs)
    summary = f"{len(commitments)} commitment{'s' if len(commitments) != 1 else ''} from local state"
    if notes:
        summary += f"; {len(notes)} note{'s' if len(notes) != 1 else ''} need required fields"
    if not commitments and not notes:
        summary = "0 commitments from local state"

    return _payload(
        generated_at=generated_at,
        status=status,
        summary=summary,
        commitments=commitments,
        notes=notes,
        truth=truth,
        sources=sources,
        issues=_dedupe(issues),
    )


def promote_operator_commitment(
    body: dict[str, Any],
    *,
    now: float | None = None,
    client_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Promote a source-backed candidate into a local commitment card."""

    generated_at = float(time.time() if now is None else now)
    request = body if isinstance(body, dict) else {}
    missing: list[str] = []
    issues: list[str] = []

    owner = _clean_required_text(request.get("owner"), max_len=120)
    if not owner:
        missing.append("owner")

    deadline_at, review_at, date_issue = _normalize_date_fields(request.get("deadline_at"), request.get("review_at"))
    if date_issue:
        missing.append("deadline_or_review")
        issues.append(date_issue)

    dispatch, dispatch_issue = _normalize_dispatch_mechanism(request.get("dispatch_mechanism"))
    if dispatch_issue:
        missing.append("dispatch_mechanism")
        issues.append(dispatch_issue)

    acceptance = _normalize_text_list(request.get("acceptance_criteria"))
    if not acceptance:
        missing.append("acceptance_criteria")

    halt_policy = _clean_required_text(request.get("halt_policy"), max_len=1000)
    if not halt_policy:
        missing.append("halt_policy")

    status = _clean_text(request.get("status"), "active", max_len=40).lower()
    if status not in VALID_COMMITMENT_STATUSES:
        missing.append("status")
        issues.append("status must be one of: " + ", ".join(sorted(VALID_COMMITMENT_STATUSES)))

    source, source_evidence, source_defaults, source_issue = _resolve_source(request.get("source"), now=generated_at)
    if source_issue:
        missing.append("source")
        issues.append(source_issue)

    extra_evidence = _normalize_evidence(request.get("evidence"))
    evidence = source_evidence + extra_evidence
    if not evidence:
        missing.append("evidence")

    if missing:
        return {
            "ok": False,
            "classification": "note",
            "missing": sorted(set(missing)),
            "issues": _dedupe(issues),
            "would_execute": False,
        }

    title = _clean_required_text(request.get("title"), max_len=200) or source_defaults.get("title") or "Untitled commitment"
    summary = _clean_text(request.get("summary"), source_defaults.get("summary", ""), max_len=800)
    profile = _clean_text(request.get("profile"), "", max_len=80) or _clean_text((client_context or {}).get("profile"), "default", max_len=80) or "default"

    card_seed = json.dumps(
        {
            "owner": owner,
            "deadline_at": deadline_at,
            "review_at": review_at,
            "title": title,
            "source": source,
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    card_id = "c_" + hashlib.sha256(card_seed.encode("utf-8")).hexdigest()[:16]
    card = {
        "id": card_id,
        "created_at": generated_at,
        "updated_at": generated_at,
        "profile": profile,
        "title": title,
        "summary": summary,
        "owner": owner,
        "deadline_at": deadline_at,
        "review_at": review_at,
        "dispatch_mechanism": dispatch,
        "source": source,
        "acceptance_criteria": acceptance,
        "halt_policy": halt_policy,
        "evidence": evidence,
        "status": status,
        "would_execute": False,
    }

    store, store_issue = _load_store_for_write()
    if store_issue:
        return {
            "ok": False,
            "classification": "unknown",
            "missing": [],
            "issues": [store_issue],
            "would_execute": False,
        }

    items = store.setdefault("commitments", [])
    replaced = False
    for index, existing in enumerate(list(items)):
        if isinstance(existing, dict) and existing.get("id") == card_id:
            card["created_at"] = existing.get("created_at", generated_at)
            items[index] = card
            replaced = True
            break
    if not replaced:
        items.append(card)
    store["version"] = STORE_VERSION
    store["updated_at"] = generated_at
    _write_store(store)

    return {"ok": True, "commitment": card, "created": not replaced, "would_execute": False}


def _payload(
    *,
    generated_at: float,
    status: str,
    summary: str,
    commitments: list[dict[str, Any]],
    notes: list[dict[str, Any]],
    truth: dict[str, Any],
    sources: list[dict[str, Any]],
    issues: list[str],
) -> dict[str, Any]:
    return {
        "version": STORE_VERSION,
        "generated_at": generated_at,
        "status": status if status in STATUS_ORDER else "unknown",
        "summary": summary,
        "mode": "local-commitment-cards",
        "would_execute": False,
        "commitments": commitments,
        "notes": notes,
        "truth": truth,
        "sources": sources,
        "issues": issues,
    }


def _store_source(path: Path) -> dict[str, Any]:
    item = {
        "id": "commitment_store",
        "kind": "json",
        "path": str(path),
        "exists": False,
        "state": "unknown",
        "required": False,
    }
    try:
        item["exists"] = path.exists()
        if not item["exists"]:
            item["issue"] = "missing"
            return item
        if not path.is_file():
            item["issue"] = "not a regular file"
            return item
        stat = path.stat()
        item["mtime"] = stat.st_mtime
        item["state"] = "live"
        return item
    except Exception as exc:  # pragma: no cover - filesystem edge case
        item["issue"] = f"unreadable: {_short_error(exc)}"
        return item


def _read_store(path: Path) -> tuple[dict[str, Any], str | None]:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        return {}, f"malformed JSON: {exc.msg}"
    except Exception as exc:
        return {}, f"unreadable: {_short_error(exc)}"
    if not isinstance(data, dict):
        return {}, "malformed JSON: top-level object required"
    if data.get("version") != STORE_VERSION:
        return {}, f"unsupported version: {data.get('version')!r}"
    if not isinstance(data.get("commitments"), list):
        return {}, "malformed JSON: commitments list required"
    return data, None


def _load_store_for_write() -> tuple[dict[str, Any], str | None]:
    path = commitment_store_path()
    if not path.exists():
        return {"version": STORE_VERSION, "updated_at": 0.0, "commitments": []}, None
    source = _store_source(path)
    if source.get("state") == "unknown":
        return {}, f"commitment_store: {source.get('issue') or 'unreadable'}"
    return _read_store(path)


def _write_store(data: dict[str, Any]) -> None:
    path = commitment_store_path()
    payload = json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    with _STORE_LOCK:
        tmp = path.with_suffix(f".tmp.{os.getpid()}.{threading.current_thread().ident}")
        try:
            with open(tmp, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
        except Exception:
            try:
                tmp.unlink(missing_ok=True)
            except Exception:
                pass
            raise


def _normalize_stored_commitment(record: Any, *, generated_at: float, profile: str | None) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    if not isinstance(record, dict):
        return None, {"classification": "note", "reason": "record is not an object", "missing": ["object"]}

    missing: list[str] = []
    owner = _clean_required_text(record.get("owner"), max_len=120)
    if not owner:
        missing.append("owner")

    deadline_at, review_at, date_issue = _normalize_date_fields(record.get("deadline_at"), record.get("review_at"))
    if date_issue:
        missing.append("deadline_or_review")

    dispatch, dispatch_issue = _normalize_dispatch_mechanism(record.get("dispatch_mechanism"))
    if dispatch_issue:
        missing.append("dispatch_mechanism")

    source = record.get("source") if isinstance(record.get("source"), dict) else None
    if not _stored_source_has_proof(source):
        missing.append("source")

    acceptance = _normalize_text_list(record.get("acceptance_criteria"))
    if not acceptance:
        missing.append("acceptance_criteria")

    halt_policy = _clean_required_text(record.get("halt_policy"), max_len=1000)
    if not halt_policy:
        missing.append("halt_policy")

    evidence = _normalize_evidence(record.get("evidence"))
    if not _evidence_has_proof(evidence):
        missing.append("evidence")

    status = _clean_text(record.get("status"), "", max_len=40).lower()
    if status not in VALID_COMMITMENT_STATUSES:
        missing.append("status")

    if record.get("would_execute") is not False:
        missing.append("would_execute_false")
    if isinstance(record.get("dispatch_mechanism"), dict) and record["dispatch_mechanism"].get("would_execute") is not False:
        missing.append("dispatch_would_execute_false")

    if missing:
        title = _clean_text(record.get("title"), _clean_text(record.get("id"), "note", max_len=120), max_len=200)
        return None, {"id": _clean_text(record.get("id"), "", max_len=80), "title": title, "classification": "note", "missing": sorted(set(missing)), "reason": "missing required commitment fields"}

    card = {
        "id": _clean_text(record.get("id"), "", max_len=80) or _stable_record_id(record),
        "created_at": _number(record.get("created_at"), generated_at),
        "updated_at": _number(record.get("updated_at"), generated_at),
        "profile": _clean_text(record.get("profile"), profile or "default", max_len=80),
        "title": _clean_text(record.get("title"), "Untitled commitment", max_len=200),
        "summary": _clean_text(record.get("summary"), "", max_len=800),
        "owner": owner,
        "deadline_at": deadline_at,
        "review_at": review_at,
        "dispatch_mechanism": dispatch,
        "source": _normalize_source_for_display(source),
        "acceptance_criteria": acceptance,
        "halt_policy": halt_policy,
        "evidence": evidence,
        "status": status,
        "would_execute": False,
    }
    return card, None


def _stored_source_has_proof(source: Any) -> bool:
    if not isinstance(source, dict):
        return False
    kind = _clean_text(source.get("kind"), "", max_len=60).lower()
    content_hash = _clean_required_text(source.get("content_hash"), max_len=140)
    quote = _clean_required_text(source.get("quote"), max_len=MAX_TEXT)
    if kind == "operator_proposal":
        return bool(
            _clean_required_text(source.get("proposal_id"), max_len=120)
            and content_hash
            and quote
        )
    if kind == "session_message":
        session_id = _clean_required_text(source.get("session_id"), max_len=120)
        index, index_issue = _normalize_session_message_index(source.get("message_index"))
        strict_content_hash, hash_issue = _normalize_session_message_content_hash(source.get("content_hash"))
        strict_quote, quote_issue = _normalize_session_message_quote(source.get("quote"))
        return bool(
            session_id
            and _SESSION_MESSAGE_ID_RE.fullmatch(session_id)
            and index is not None
            and not index_issue
            and strict_content_hash
            and not hash_issue
            and strict_quote
            and not quote_issue
        )
    return False


def _evidence_has_proof(evidence: list[dict[str, Any]]) -> bool:
    for item in evidence:
        if not isinstance(item, dict):
            continue
        label = _clean_required_text(item.get("label"), max_len=160).lower()
        state = _clean_text(item.get("state"), "", max_len=40).lower()
        identifiers = ["path", "api", "proposal_id", "session_id", "message_index", "source_id"]
        if any(_clean_required_text(item.get(key), max_len=MAX_TEXT) for key in identifiers):
            return True
        if label and label not in {"evidence", "unknown"} and state not in PLACEHOLDER_TEXT:
            return True
    return False


def _resolve_source(value: Any, *, now: float) -> tuple[dict[str, Any] | None, list[dict[str, Any]], dict[str, str], str | None]:
    if not isinstance(value, dict):
        return None, [], {}, "source is required"
    kind = _clean_text(value.get("kind"), "", max_len=60).lower()
    if kind == "operator_proposal":
        return _resolve_operator_proposal(value, now=now)
    if kind == "session_message":
        return _resolve_session_message(value)
    return None, [], {}, "source kind must be operator_proposal or session_message"


def _resolve_operator_proposal(value: dict[str, Any], *, now: float) -> tuple[dict[str, Any] | None, list[dict[str, Any]], dict[str, str], str | None]:
    proposal_id = _clean_required_text(value.get("proposal_id"), max_len=120)
    if not proposal_id:
        return None, [], {}, "operator proposal source requires proposal_id"
    session_id = _clean_text(value.get("session_id"), "", max_len=120) or None
    ui_board_hint = _clean_text(value.get("ui_board"), "", max_len=120) or None
    try:
        from api.operator_proposals import build_operator_proposal_payload

        payload = build_operator_proposal_payload(session_id=session_id, ui_board_hint=ui_board_hint, now=now)
    except Exception as exc:
        return None, [], {}, f"operator proposal source unavailable: {_short_error(exc)}"
    proposals = payload.get("proposals") if isinstance(payload, dict) else None
    if not isinstance(proposals, list):
        return None, [], {}, "operator proposal source unavailable: malformed proposals payload"
    proposal = next((item for item in proposals if isinstance(item, dict) and item.get("id") == proposal_id), None)
    if not proposal:
        return None, [], {}, f"operator proposal source not found: {proposal_id}"

    quote = _clean_text(proposal.get("summary"), _clean_text(proposal.get("title"), proposal_id, max_len=MAX_TEXT), max_len=MAX_TEXT)
    digest = _hash_json({"kind": "operator_proposal", "proposal": proposal_id, "summary": quote})
    source = {
        "kind": "operator_proposal",
        "proposal_id": proposal_id,
        "session_id": session_id,
        "content_hash": digest,
        "quote": quote,
    }
    evidence = [
        {
            "kind": "source",
            "label": "Operator proposal",
            "state": _clean_text(payload.get("status"), "unknown", max_len=20),
            "proposal_id": proposal_id,
        }
    ]
    for ev in _normalize_evidence(proposal.get("evidence"))[:4]:
        evidence.append(ev)
    defaults = {
        "title": _clean_text(proposal.get("title"), proposal_id, max_len=200),
        "summary": quote,
    }
    return source, evidence, defaults, None


def _resolve_session_message(value: dict[str, Any]) -> tuple[dict[str, Any] | None, list[dict[str, Any]], dict[str, str], str | None]:
    """Validate an explicit UI/API session-message proof without reloading sessions."""

    session_id = _clean_required_text(value.get("session_id"), max_len=120)
    if not session_id:
        return None, [], {}, "session message source requires session_id"
    if not _SESSION_MESSAGE_ID_RE.fullmatch(session_id):
        return None, [], {}, "session message source has invalid session_id"

    index, index_issue = _normalize_session_message_index(value.get("message_index"))
    if index_issue:
        return None, [], {}, index_issue

    content_hash, hash_issue = _normalize_session_message_content_hash(value.get("content_hash"))
    if hash_issue:
        return None, [], {}, hash_issue

    quote, quote_issue = _normalize_session_message_quote(value.get("quote"))
    if quote_issue:
        return None, [], {}, quote_issue
    if index is None or content_hash is None or quote is None:  # defensive: helpers return an issue with None
        return None, [], {}, "session message source proof is invalid"

    source: dict[str, Any] = {
        "kind": "session_message",
        "session_id": session_id,
        "message_index": index,
        "content_hash": content_hash,
        "quote": quote,
    }
    message_role = _clean_required_text(value.get("message_role"), max_len=40)
    if message_role:
        source["message_role"] = message_role

    evidence = [
        {
            "kind": "source",
            "label": "Session message",
            "state": "present",
            "session_id": session_id,
            "message_index": index,
            "content_hash": content_hash,
        }
    ]
    defaults = {"title": quote[:120] or "Session commitment", "summary": quote}
    return source, evidence, defaults, None


def _normalize_session_message_index(value: Any) -> tuple[int | None, str | None]:
    if isinstance(value, bool):
        return None, "session message source requires strict non-negative integer message_index"
    if isinstance(value, int):
        if value >= 0:
            return value, None
        return None, "session message source requires strict non-negative integer message_index"
    if isinstance(value, str):
        text = value.strip()
        if _SESSION_MESSAGE_INDEX_RE.fullmatch(text):
            return int(text), None
    return None, "session message source requires strict non-negative integer message_index"


def _normalize_session_message_content_hash(value: Any) -> tuple[str | None, str | None]:
    if not isinstance(value, str):
        return None, "session message source requires content_hash matching sha256:<64 hex>"
    text = value.strip()
    if not _SESSION_MESSAGE_CONTENT_HASH_RE.fullmatch(text):
        return None, "session message source requires content_hash matching sha256:<64 hex>"
    return "sha256:" + text.split(":", 1)[1].lower(), None


def _normalize_session_message_quote(value: Any) -> tuple[str | None, str | None]:
    if not isinstance(value, str):
        return None, "session message source requires bounded redacted quote"
    quote = re.sub(r"\s+", " ", value).strip()
    if not quote or quote.lower() in PLACEHOLDER_TEXT:
        return None, "session message source requires bounded redacted quote"
    if len(quote) > MAX_TEXT:
        return None, f"session message source quote must be at most {MAX_TEXT} characters"
    if _quote_contains_raw_secret(quote):
        return None, "session message source quote appears to contain a raw secret; use a redacted quote"
    return quote, None


def _quote_contains_raw_secret(quote: str) -> bool:
    for match in _RAW_SECRET_QUOTE_RE.finditer(quote):
        if not _is_redacted_secret_value(match.group("value")):
            return True
    return any(
        pattern.search(quote)
        for pattern in (
            _RAW_BEARER_SECRET_QUOTE_RE,
            _RAW_OPENAI_SECRET_QUOTE_RE,
            _RAW_SLACK_SECRET_QUOTE_RE,
            _RAW_GITHUB_SECRET_QUOTE_RE,
        )
    )


def _is_redacted_secret_value(value: str) -> bool:
    token = value.strip().strip("'\"").strip()
    normalized = token.strip("[](){}<>").lower()
    if normalized in {"redacted", "masked", "hidden", "removed", "withheld", "scrubbed"}:
        return True
    compact = token.replace(" ", "")
    return bool(compact) and set(compact) <= {"*", "x", "X", "•", "…"}


def _operator_truth_summary(*, session_id: str | None, ui_board_hint: str | None, now: float) -> dict[str, Any]:
    try:
        from api.operator_truth import build_operator_truth_payload

        payload = build_operator_truth_payload(session_id=session_id, ui_board_hint=ui_board_hint, now=now)
    except Exception as exc:
        return {"status": "unknown", "verified_at": now, "summary": "Truth unavailable", "api": "/api/operator/truth", "issues": [_short_error(exc)]}
    raw_status = payload.get("status")
    status = raw_status if isinstance(raw_status, str) and raw_status in STATUS_ORDER else "unknown"
    return {
        "status": status,
        "verified_at": payload.get("verified_at", now),
        "summary": payload.get("summary") or f"Truth {status}",
        "api": "/api/operator/truth",
        "issues": [str(issue) for issue in payload.get("issues", [])[:5]] if isinstance(payload.get("issues"), list) else [],
    }


def _normalize_dispatch_mechanism(value: Any) -> tuple[dict[str, Any] | None, str | None]:
    if isinstance(value, str):
        raw_kind = value
        label = value
        would_execute = False
    elif isinstance(value, dict):
        raw_kind = value.get("kind") or value.get("type") or value.get("mode")
        label = value.get("label") or raw_kind
        would_execute = value.get("would_execute", False)
    else:
        return None, "dispatch_mechanism is required"

    kind = _clean_text(raw_kind, "", max_len=80).lower().replace("-", "_").replace(" ", "_")
    label_text = _clean_text(label, kind or "manual", max_len=160)
    combined = f"{kind} {label_text}".lower()
    if not kind or kind in PLACEHOLDER_TEXT:
        return None, "dispatch_mechanism is required"
    if kind not in ALLOWED_DISPATCH_KINDS:
        return None, "dispatch_mechanism kind must be one of: " + ", ".join(sorted(ALLOWED_DISPATCH_KINDS))
    if any(token in combined for token in FORBIDDEN_DISPATCH_TOKENS):
        return None, "dispatch_mechanism must not describe automation, cron, goal, Kanban dispatch, shell, webhook, or AIM runtime"
    if would_execute is not False:
        return None, "dispatch_mechanism.would_execute must be false"
    return {"kind": kind, "label": label_text, "would_execute": False}, None


def _normalize_date_fields(deadline: Any, review: Any) -> tuple[str | None, str | None, str | None]:
    deadline_text = _clean_text(deadline, "", max_len=80)
    review_text = _clean_text(review, "", max_len=80)
    if not deadline_text and not review_text:
        return None, None, "deadline_at or review_at is required"
    if deadline_text:
        normalized = _normalize_isoish_date(deadline_text)
        if not normalized:
            return None, None, "deadline_at must be an ISO date or datetime"
        deadline_text = normalized
    if review_text:
        normalized = _normalize_isoish_date(review_text)
        if not normalized:
            return None, None, "review_at must be an ISO date or datetime"
        review_text = normalized
    return deadline_text or None, review_text or None, None


def _normalize_isoish_date(value: str) -> str | None:
    text = value.strip()
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
            datetime.strptime(text, "%Y-%m-%d")
            return text
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    except Exception:
        return None


def _normalize_text_list(value: Any) -> list[str]:
    raw_items: list[Any]
    if isinstance(value, list):
        raw_items = value
    elif isinstance(value, str):
        raw_items = [line.strip(" -\t") for line in value.splitlines()]
    else:
        raw_items = []
    items = []
    for item in raw_items:
        text = _clean_required_text(item, max_len=MAX_TEXT)
        if text:
            items.append(text)
        if len(items) >= MAX_LIST_ITEMS:
            break
    return items


def _normalize_evidence(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    evidence: list[dict[str, Any]] = []
    for item in value[:MAX_LIST_ITEMS]:
        if isinstance(item, dict):
            label = _clean_text(item.get("label"), _clean_text(item.get("source_id"), "evidence", max_len=120), max_len=160)
            state = _clean_text(item.get("state") or item.get("status"), "unknown", max_len=40)
            ev = {"kind": _clean_text(item.get("kind"), "evidence", max_len=60), "label": label, "state": state}
            for key in ("path", "api", "proposal_id", "session_id", "message_index", "source_id"):
                if key in item and item.get(key) is not None:
                    ev[key] = _clean_text(item.get(key), str(item.get(key)), max_len=MAX_TEXT)
            evidence.append(ev)
        else:
            text = _clean_required_text(item, max_len=MAX_TEXT)
            if text:
                evidence.append({"kind": "evidence", "label": text, "state": "unknown"})
    return evidence


def _normalize_source_for_display(source: dict[str, Any]) -> dict[str, Any]:
    result = {}
    for key in ("kind", "proposal_id", "session_id", "message_index", "message_role", "content_hash", "quote"):
        if key in source and source.get(key) is not None:
            result[key] = _clean_text(source.get(key), str(source.get(key)), max_len=MAX_TEXT)
    return result


def _clean_required_text(value: Any, *, max_len: int) -> str:
    text = _clean_text(value, "", max_len=max_len)
    if text.strip().lower() in PLACEHOLDER_TEXT:
        return ""
    return text


def _clean_text(value: Any, fallback: str = "", *, max_len: int = MAX_TEXT) -> str:
    if value is None:
        text = fallback
    else:
        text = str(value)
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        text = fallback.strip() if isinstance(fallback, str) else str(fallback).strip()
    if len(text) > max_len:
        text = text[: max_len - 1].rstrip() + "…"
    return text


def _message_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                if isinstance(item.get("text"), str):
                    parts.append(item["text"])
                elif isinstance(item.get("content"), str):
                    parts.append(item["content"])
            elif isinstance(item, str):
                parts.append(item)
        return "\n".join(parts)
    return ""


def _number(value: Any, fallback: float) -> float:
    try:
        return float(value)
    except Exception:
        return fallback


def _stable_record_id(record: dict[str, Any]) -> str:
    return "c_" + hashlib.sha256(json.dumps(record, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:16]


def _hash_json(value: Any) -> str:
    return "sha256:" + hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")).hexdigest()


def _worst_status(values: list[Any]) -> str:
    worst = "live"
    for value in values:
        status = value if isinstance(value, str) and value in STATUS_ORDER else "unknown"
        if STATUS_ORDER[status] > STATUS_ORDER[worst]:
            worst = status
    return worst


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        text = str(item or "").strip()
        if text and text not in seen:
            seen.add(text)
            out.append(text)
    return out


def _short_error(exc: Exception) -> str:
    return str(exc).splitlines()[0][:180]
