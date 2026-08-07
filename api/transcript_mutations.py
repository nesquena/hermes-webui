"""Authoritative, durable transcript point mutations.

The dismissal ledger is deliberately separate from compression recovery.  A
row can be hidden only when its server-owned identity and source are known.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
import uuid
import copy
from dataclasses import dataclass
from typing import Any


CAPABILITY_VERSION = 1
LEDGER_SCHEMA_VERSION = 2
LEDGER_VERSION = CAPABILITY_VERSION
PROVIDER_ERROR_PROVENANCE = "webui.generated.provider_error"
MAX_PROJECTED_MESSAGE_COUNT = 50000
_NON_PROVIDER_ERROR_LABELS = {
    "Cancellation details",
    "Interruption details",
    "Terminal state details",
}


def _text(value: Any) -> str:
    return str(value or "").strip()


def message_identity(message: dict | None) -> str | None:
    if not isinstance(message, dict):
        return None
    value = message.get("id") or message.get("message_id")
    return _text(value) or None


def source_classification(session) -> str | None:
    """Return the canonical source class used by session metadata."""
    try:
        from api.agent_sessions import normalize_agent_session_source

        raw = (
            getattr(session, "source_tag", None)
            or getattr(session, "raw_source", None)
            or getattr(session, "session_source", None)
        )
        meta = normalize_agent_session_source(raw)
        return _text(meta.get("session_source")).casefold() or None
    except Exception:
        return None


def is_webui_owned_provider_error(
    message: dict | None,
    session,
    *,
    source_session_id: str | None = None,
) -> bool:
    if not isinstance(message, dict) or message.get("role") != "assistant":
        return False
    if message.get("_error") is not True:
        return False
    if message.get("_generated_error_provenance") != PROVIDER_ERROR_PROVENANCE:
        return False
    if message.get("_webui_generated_provider_error") is not True:
        return False
    if source_classification(session) != "webui":
        return False
    row_source = _text(message.get("_generated_error_source_session_id"))
    owner = _text(source_session_id or getattr(session, "session_id", ""))
    if not row_source or row_source != owner:
        return False
    return bool(message_identity(message))


def ensure_generated_error_identity(message: dict, *, source_session_id: str) -> dict:
    """Stamp a durable provider error exactly once at its persistence boundary."""
    source_session_id = _text(source_session_id)
    if not isinstance(message, dict) or message.get("_error") is not True:
        return message
    if not source_session_id:
        return message
    if message.get("_generated_error_provenance") == PROVIDER_ERROR_PROVENANCE:
        return message
    if message.get("_webui_generated_provider_error") is not True:
        return message
    if message.get("role") != "assistant":
        return message
    if not message_identity(message):
        message["id"] = "provider-error-" + uuid.uuid4().hex
    message["_generated_error_provenance"] = PROVIDER_ERROR_PROVENANCE
    message["_generated_error_source_session_id"] = _text(source_session_id)
    return message


def admit_generated_provider_error(message: dict, session) -> dict:
    """Apply the WebUI provenance gate and stable identity at the producer seam."""
    mark_generated_provider_error(message, session)
    return ensure_generated_error_identity(
        message,
        source_session_id=getattr(session, "session_id", ""),
    )


def mark_generated_provider_error(message: dict, session) -> dict:
    """Mark only a WebUI provider failure before the persistence boundary."""
    if (
        isinstance(message, dict)
        and message.get("role") == "assistant"
        and message.get("_error") is True
        and source_classification(session) == "webui"
        and not message.get("_compressionRecovery")
        and message.get("provider_details_label") not in _NON_PROVIDER_ERROR_LABELS
    ):
        message["_webui_generated_provider_error"] = True
    return message


def _ledger(session) -> dict:
    raw = getattr(session, "transcript_dismissals", None)
    if not isinstance(raw, dict):
        raw = {}
    entries = raw.get("entries")
    if not isinstance(entries, list):
        entries = []
    active_keys = raw.get("active_keys")
    if not isinstance(active_keys, list):
        active_keys = []
    normalized = []
    seen = set()
    for item in active_keys:
        if isinstance(item, (list, tuple)) and len(item) == 2:
            key = _entry_key(item[0], item[1])
        elif isinstance(item, dict):
            key = _entry_key(item.get("source_session_id"), item.get("message_id"))
        else:
            continue
        if all(key) and key not in seen:
            seen.add(key)
            normalized.append(list(key))
    if "active_keys" not in raw:
        for entry in entries:
            if isinstance(entry, dict) and entry.get("active") is not False:
                key = _entry_key(entry.get("source_session_id"), entry.get("message_id"))
                if all(key) and key not in seen:
                    seen.add(key)
                    normalized.append(list(key))
    return {"version": LEDGER_SCHEMA_VERSION, "entries": entries, "active_keys": normalized}


def active_dismissal_count(session) -> int:
    raw = getattr(session, "transcript_dismissal_active_count", None)
    try:
        value = int(raw)
    except (TypeError, ValueError, OverflowError):
        return 0
    if value < 0 or value > MAX_PROJECTED_MESSAGE_COUNT:
        return 0
    return value


def projected_message_count(raw_count, active_count, *, has_pending_user_message=False) -> int:
    """Return the bounded visible count while keeping raw storage untouched."""
    try:
        raw = int(raw_count)
    except (TypeError, ValueError, OverflowError):
        raw = 0
    if raw < 0:
        raw = 0
    try:
        active = int(active_count)
    except (TypeError, ValueError, OverflowError):
        active = 0
    if active < 0 or active > MAX_PROJECTED_MESSAGE_COUNT:
        active = 0
    active = min(active, raw)
    projected = raw - active
    if has_pending_user_message:
        projected = max(projected, 1)
    return projected


def reconcile_dismissals(session, messages) -> bool:
    """Rebase the bounded active index against an authoritative coordinate."""
    raw_ledger = getattr(session, "transcript_dismissals", None)
    has_active_index = isinstance(raw_ledger, dict) and "active_keys" in raw_ledger
    ledger = _ledger(session)
    eligible = set()
    for row in list(messages or []):
        if not isinstance(row, dict):
            continue
        source = _text(row.get("_generated_error_source_session_id"))
        identity = message_identity(row)
        if source and identity and is_webui_owned_provider_error(row, session, source_session_id=source):
            eligible.add(_entry_key(source, identity))
    old_active = {tuple(item) for item in ledger.get("active_keys", []) if isinstance(item, list) and len(item) == 2}
    if has_active_index:
        candidates = {
            tuple(item)
            for item in ledger.get("active_keys", [])
            if isinstance(item, list) and len(item) == 2 and all(item)
        }
    else:
        candidates = set()
        for entry in ledger["entries"]:
            if isinstance(entry, dict):
                key = _entry_key(entry.get("source_session_id"), entry.get("message_id"))
                if all(key):
                    candidates.add(key)
    active = sorted(candidates & eligible)
    active_set = set(active)
    changed = (
        ledger.get("version") != LEDGER_SCHEMA_VERSION
        or old_active != active_set
        or any(isinstance(e, dict) and bool(e.get("active")) != (_entry_key(e.get("source_session_id"), e.get("message_id")) in active_set) for e in ledger["entries"])
        or getattr(session, "transcript_dismissal_active_count", None) != len(active)
    )
    for entry in ledger["entries"]:
        if isinstance(entry, dict):
            key = _entry_key(entry.get("source_session_id"), entry.get("message_id"))
            entry["active"] = key in active_set
    ledger["version"] = LEDGER_SCHEMA_VERSION
    ledger["active_keys"] = [list(key) for key in active]
    session.transcript_dismissals = ledger
    session.transcript_dismissal_active_count = len(active)
    return changed


def _entry_key(source_session_id: str, message_id: str) -> tuple[str, str]:
    return (_text(source_session_id), _text(message_id))


def is_dismissed(session, source_session_id: str, message: dict) -> bool:
    source_session_id = _text(source_session_id)
    row_source = _text((message or {}).get("_generated_error_source_session_id"))
    identity = message_identity(message)
    if not source_session_id or not row_source or row_source != source_session_id or not identity:
        return False
    target = _entry_key(source_session_id, identity)
    return list(target) in _ledger(session).get("active_keys", [])


def _capability_secret(session) -> bytes:
    return ("hermes-webui:" + _text(getattr(session, "session_id", ""))).encode()


def make_capability(session, source_session_id: str, message: dict) -> str | None:
    source_session_id = _text(source_session_id)
    if source_session_id != _text((message or {}).get("_generated_error_source_session_id")):
        return None
    if not is_webui_owned_provider_error(message, session, source_session_id=source_session_id):
        return None
    identity = message_identity(message)
    if not identity:
        return None
    payload = json.dumps(
        {"v": CAPABILITY_VERSION, "s": _text(source_session_id), "m": identity},
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    body = payload.hex()
    signature = hmac.new(_capability_secret(session), body.encode(), hashlib.sha256).hexdigest()
    return body + "." + signature


def parse_capability(session, capability: Any) -> tuple[str, str] | None:
    try:
        body, signature = _text(capability).split(".", 1)
        expected = hmac.new(_capability_secret(session), body.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            return None
        payload = json.loads(bytes.fromhex(body))
        if not isinstance(payload, dict):
            return None
        if payload.get("v") != CAPABILITY_VERSION:
            return None
        source = _text(payload.get("s"))
        identity = _text(payload.get("m"))
        return (source, identity) if source and identity else None
    except (ValueError, TypeError, json.JSONDecodeError):
        return None


def record_dismissal(session, source_session_id: str, message: dict) -> dict:
    source_session_id = _text(source_session_id)
    if not source_session_id or _text((message or {}).get("_generated_error_source_session_id")) != source_session_id:
        raise ValueError("provider-error row is not source-qualified")
    if not is_webui_owned_provider_error(message, session, source_session_id=source_session_id):
        raise ValueError("provider-error row is not an eligible WebUI provider error")
    identity = message_identity(message)
    if not identity:
        raise ValueError("provider-error row has no stable identity")
    ledger = _ledger(session)
    target = _entry_key(source_session_id, identity)
    found = next((entry for entry in ledger["entries"] if isinstance(entry, dict) and _entry_key(entry.get("source_session_id"), entry.get("message_id")) == target), None)
    if found is None:
        ledger["entries"].append(
            {
                "source_session_id": _text(source_session_id),
                "message_id": identity,
                "provenance": PROVIDER_ERROR_PROVENANCE,
                "dismissed_at": time.time(),
                "active": True,
            }
        )
    else:
        found["active"] = True
    active_keys = {tuple(key) for key in ledger.get("active_keys", []) if isinstance(key, list) and len(key) == 2}
    active_keys.add(target)
    ledger["active_keys"] = [list(key) for key in sorted(active_keys)]
    ledger["version"] = LEDGER_SCHEMA_VERSION
    session.transcript_dismissals = ledger
    session.transcript_dismissal_active_count = len(ledger["active_keys"])
    return ledger


@dataclass(frozen=True)
class TranscriptProjection:
    """One immutable visible coordinate for rows and message-indexed artifacts."""

    messages: list
    projected_count: int
    visible_to_raw: dict[int, int]
    raw_to_visible: dict[int, int]
    tool_calls: list


def projected_count_for_session(session, raw_count=None, messages=None) -> int:
    """Return the canonical visible count for detail, index, and sidebar paths."""
    if messages is None:
        messages = getattr(session, "messages", None)
    if (
        isinstance(messages, list)
        and not getattr(session, "_loaded_metadata_only", False)
    ):
        return project_transcript(session, messages).projected_count
    if raw_count is None:
        raw_count = getattr(session, "_metadata_message_count", None)
        if raw_count is None:
            raw_count = len(messages or [])
    return projected_message_count(
        raw_count,
        active_dismissal_count(session),
        has_pending_user_message=bool(getattr(session, "pending_user_message", None)),
    )


def _rebased_tool_calls(
    tool_calls,
    raw_to_visible: dict[int, int],
    source_to_input: dict[int, int] | None = None,
) -> list:
    result = []
    for call in tool_calls or []:
        if not isinstance(call, dict):
            continue
        raw_index = call.get("assistant_msg_idx")
        if isinstance(raw_index, bool) or not isinstance(raw_index, int):
            continue
        input_index = raw_index
        if source_to_input is not None:
            input_index = source_to_input.get(raw_index)
            if input_index is None:
                continue
        visible_index = raw_to_visible.get(input_index)
        if visible_index is None:
            continue
        rebased = copy.deepcopy(call)
        rebased["assistant_msg_idx"] = visible_index
        result.append(rebased)
    return result


def project_transcript(
    session,
    messages,
    *,
    tool_calls=None,
    source_session_id: str | None = None,
    source_messages=None,
    merge_key=None,
) -> TranscriptProjection:
    """Build a pure clean projection and rebase every retained tool anchor."""
    source = _text(source_session_id or getattr(session, "session_id", ""))
    projected = []
    visible_to_raw = {}
    raw_to_visible = {}
    for raw_index, message in enumerate(list(messages or [])):
        if not isinstance(message, dict):
            continue
        row = copy.deepcopy(message)
        row.pop("_provider_error_dismissal_capability", None)
        identity = message_identity(row)
        row_source = _text(row.get("_generated_error_source_session_id"))
        projection_source = row_source or source
        owned_source = bool(projection_source)
        eligible = owned_source and is_webui_owned_provider_error(
            row, session, source_session_id=projection_source
        )
        if eligible and identity and is_dismissed(session, projection_source, row):
            continue
        visible_index = len(projected)
        visible_to_raw[visible_index] = raw_index
        raw_to_visible[raw_index] = visible_index
        projected.append(row)
    source_to_input = None
    if source_messages is not None and merge_key is not None:
        input_positions = {}
        ambiguous = set()
        for input_index, message in enumerate(list(messages or [])):
            key = merge_key(message)
            if key in input_positions:
                ambiguous.add(key)
            else:
                input_positions[key] = input_index
        for key in ambiguous:
            input_positions.pop(key, None)
        source_to_input = {}
        source_seen = set()
        source_ambiguous = set()
        for source_index, message in enumerate(list(source_messages or [])):
            key = merge_key(message)
            if key in source_seen:
                source_ambiguous.add(key)
            source_seen.add(key)
            if key in input_positions:
                source_to_input[source_index] = input_positions[key]
        for source_index, message in enumerate(list(source_messages or [])):
            if merge_key(message) in source_ambiguous:
                source_to_input.pop(source_index, None)
    projected_count = len(projected)
    if getattr(session, "pending_user_message", None):
        projected_count = max(projected_count, 1)
    return TranscriptProjection(
        messages=projected,
        projected_count=projected_count,
        visible_to_raw=visible_to_raw,
        raw_to_visible=raw_to_visible,
        tool_calls=_rebased_tool_calls(
            getattr(session, "tool_calls", None) if tool_calls is None else tool_calls,
            raw_to_visible,
            source_to_input,
        ),
    )


def lineage_messages_for_projection(session):
    """Reconstruct compression-snapshot lineage without importing routes."""
    from api.models import Session, merge_session_messages_append_only

    current = session
    own_messages = list(getattr(session, "messages", None) or [])
    source = str(getattr(session, "session_source", "") or "").strip().lower()
    root_is_fork = source == "fork"
    seen = {str(getattr(session, "session_id", "") or "")}
    parents = []
    for _ in range(20):
        parent_id = str(getattr(current, "parent_session_id", "") or "").strip()
        if not parent_id or parent_id in seen:
            break
        parent = Session.load(parent_id)
        if not parent or not getattr(parent, "pre_compression_snapshot", False):
            break
        parent_source = str(getattr(parent, "session_source", "") or "").strip().lower()
        if root_is_fork and parent_source != "fork":
            break
        parents.append(parent)
        seen.add(parent_id)
        current = parent
    if not parents:
        return own_messages, own_messages
    merged = []
    for parent in reversed(parents):
        merged = merge_session_messages_append_only(
            merged,
            list(getattr(parent, "messages", None) or []),
            truncation_watermark=getattr(parent, "truncation_watermark", None),
            truncation_boundary=getattr(parent, "truncation_boundary", None),
        )
    merged = merge_session_messages_append_only(
        merged,
        own_messages,
        truncation_watermark=None,
    )
    return merged, own_messages


def decorate_projection(session, projection: TranscriptProjection) -> list:
    """Add dismissal capabilities only to a transport copy of clean rows."""
    source = _text(getattr(session, "session_id", ""))
    decorated = []
    for message in projection.messages:
        row = copy.deepcopy(message)
        identity = message_identity(row)
        row_source = _text(row.get("_generated_error_source_session_id"))
        if identity and row_source == source and is_webui_owned_provider_error(
            row, session, source_session_id=source
        ):
            capability = make_capability(session, source, row)
            if validate_capability(session, capability, row):
                row["_provider_error_dismissal_capability"] = capability
        decorated.append(row)
    return decorated


def project_messages(session, messages, *, source_session_id: str | None = None) -> list:
    """Compatibility adapter returning transport rows only."""
    return decorate_projection(
        session,
        project_transcript(session, messages, source_session_id=source_session_id),
    )


def materialize_duplicate(projection: TranscriptProjection, *, source_session_id: str, destination_session_id: str) -> tuple[list, list]:
    """Copy clean rows and rehome only retained eligible generated errors."""
    rows = copy.deepcopy(projection.messages)
    for row in rows:
        if (
            row.get("_generated_error_provenance") == PROVIDER_ERROR_PROVENANCE
            and row.get("_webui_generated_provider_error") is True
            and row.get("_error") is True
        ):
            row["_generated_error_source_session_id"] = _text(destination_session_id)
        row.pop("_provider_error_dismissal_capability", None)
    calls = copy.deepcopy(projection.tool_calls)
    return rows, calls


def materialize_fork(projection: TranscriptProjection) -> tuple[list, list]:
    """Copy clean rows while retaining parent-qualified ownership."""
    rows = copy.deepcopy(projection.messages)
    for row in rows:
        row.pop("_provider_error_dismissal_capability", None)
    return rows, copy.deepcopy(projection.tool_calls)


def validate_capability(session, capability: Any, message: dict) -> tuple[str, str] | None:
    parsed = parse_capability(session, capability)
    identity = message_identity(message)
    row_source = _text(message.get("_generated_error_source_session_id"))
    if (
        parsed is None
        or identity is None
        or parsed[1] != identity
        or not row_source
        or parsed[0] != row_source
        or parsed[0] != _text(getattr(session, "session_id", ""))
    ):
        return None
    return parsed
