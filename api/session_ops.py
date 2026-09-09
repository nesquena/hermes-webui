"""Session-mutation operations for slash commands (/retry, /undo) and
read-only aggregators (/status, /usage). Operates on the webui's own
JSON Session store (api/models.py), not on hermes-agent's SQLite.

Behavior parity reference: gateway/run.py:_handle_*_command in
the hermes-agent repo.
"""
from __future__ import annotations
import json
import logging
import uuid
import copy
import hashlib
import math
import os
import threading
from dataclasses import dataclass
from contextlib import ExitStack, nullcontext
from bisect import bisect_left
from pathlib import Path
from typing import Any

from api.config import LOCK, _get_session_agent_lock
from api.models import get_session, SESSIONS
from api.agent_sessions import normalize_agent_session_source

logger = logging.getLogger(__name__)

AUTO_TITLE_LABELS = {'untitled', 'new chat'}

_PROVIDER_ERROR_PUBLIC_ONLY_FIELDS = {
    "_provider_error_dismiss_ref",
    "_provider_error_dismissed",
    "_anchor_activity_scene",
    "_anchor_stream_id",
    "api_content",
}

_PROVIDER_ERROR_PROJECTION_CACHE: dict[tuple, tuple[list[dict], str]] = {}
_PROVIDER_ERROR_PROJECTION_CACHE_LOCK = threading.Lock()
_PROVIDER_ERROR_PROJECTION_CACHE_MAX = 32


class RegenerationUnavailable(Exception):
    def __init__(self, code: str, status: int = 409, message: str | None = None):
        super().__init__(message or code)
        self.code = code
        self.status = status


class ProviderErrorDismissalUnavailable(Exception):
    """Typed failure for a stale, foreign, or non-durable dismissal target."""

    def __init__(self, code: str, status: int = 409, message: str | None = None):
        super().__init__(message or code)
        self.code = code
        self.status = status


@dataclass(frozen=True)
class ProviderErrorDismissalPlan:
    viewed_session_id: str
    owner_session_id: str
    owner_index: int
    owner_revision: str
    stable_id: str | None
    row_digest: str
    dismiss_ref: str


def _provider_error_row_digest(row) -> str:
    encoded = json.dumps(
        row if isinstance(row, dict) else {},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _provider_error_reference_digest(row) -> str:
    """Hash the durable row while excluding transport-only projection fields."""
    reference_row = copy.deepcopy(row) if isinstance(row, dict) else {}
    for key in {"_dismissed", *_PROVIDER_ERROR_PUBLIC_ONLY_FIELDS}:
        reference_row.pop(key, None)
    return _provider_error_row_digest(reference_row)


def _provider_error_stable_id(row) -> str | None:
    if not isinstance(row, dict):
        return None
    for key in ("id", "_stable_id", "stable_id", "message_id"):
        value = row.get(key)
        if value not in (None, "") and isinstance(value, (str, int)) and not isinstance(value, bool):
            return str(value)
    return None


def _provider_error_stable_id_conflicted(row) -> bool:
    if not isinstance(row, dict):
        return False
    values = set()
    for key in ("id", "_stable_id", "stable_id", "message_id"):
        if key not in row or row[key] is None:
            continue
        if not isinstance(row[key], (str, int)) or isinstance(row[key], bool):
            return True
        if isinstance(row[key], str) and not row[key].strip():
            return True
        values.add(str(row[key]))
    return len(values) > 1


def _provider_error_api_content_matches(left, right) -> bool:
    left_present = isinstance(left, dict) and left.get("api_content") not in (None, "")
    right_present = isinstance(right, dict) and right.get("api_content") not in (None, "")
    if left_present != right_present:
        return False
    return not left_present or left.get("api_content") == right.get("api_content")


def _provider_error_row_is_dismissible(row) -> bool:
    """Classify only terminal provider failures at the mutation owner."""
    if not isinstance(row, dict):
        return False
    if row.get("role") != "assistant" or row.get("_error") is not True:
        return False
    if _provider_error_stable_id_conflicted(row):
        return False
    if any(row.get(key) is not None for key in ("_compressionRecovery", "_statusCard", "recovery_control", "_pending_journal_recovery")):
        return False
    if row.get("type") == "interrupted" or row.get("interruption_cause"):
        return False
    label = str(row.get("provider_details_label") or "").strip().lower()
    if label in {"cancellation details", "interruption details", "terminal state details"}:
        return False
    provider_type = str(row.get("_provider_error_type") or "").strip().lower()
    if provider_type:
        return provider_type in {
            "error", "quota_exhausted", "rate_limit", "auth_mismatch", "model_not_found",
            "no_response", "credential_pool_empty", "gateway_error", "gateway_http_error",
            "gateway_auth_error", "gateway_empty_response",
        }
    if "provider_details" not in row:
        return False
    return not str(row.get("content") or "").strip().lower().startswith(
        ("**task cancelled:", "**task canceled:", "**response interrupted:",
         "**connection interrupted:", "**tool iteration limit reached:",
         "**context compression exhausted:")
    )


def _provider_error_session_source(session) -> str:
    values = {
        _regeneration_source_class(value)
        for value in (
            getattr(session, "session_source", None),
            getattr(session, "raw_source", None),
            getattr(session, "source_tag", None),
        )
        if value not in (None, "")
    }
    try:
        from api.routes import _state_db_session_source

        state_source = _regeneration_source_class(
            _state_db_session_source(str(getattr(session, "session_id", "") or ""))
        )
    except Exception:
        state_source = ""
    gateway_webui_source = values == {"webui"} and state_source == "api"
    if state_source and state_source not in {"webui", "fork"} and not gateway_webui_source:
        raise ProviderErrorDismissalUnavailable("read_only_session", 403)
    explicit_source = next(iter(values)) if values else ""
    source_conflict = bool(
        values
        and state_source
        and explicit_source != state_source
        and not (
            (explicit_source == "fork" and state_source == "webui")
            or (explicit_source == "webui" and state_source == "api")
        )
    )
    if len(values) > 1 or source_conflict:
        raise ProviderErrorDismissalUnavailable("ambiguous_source", 403)
    if values:
        return next(iter(values))
    if state_source:
        return state_source
    raise ProviderErrorDismissalUnavailable("unknown_source", 403)


def _provider_error_session_is_idle(session) -> bool:
    if any(
        getattr(session, key, None)
        for key in (
            "active_stream_id",
            "pending_user_message",
            "pending_started_at",
            "pending_user_source",
        )
    ):
        return False
    if getattr(session, "pending_attachments", None):
        return False
    for row in getattr(session, "messages", None) or []:
        if isinstance(row, dict) and row.get("_pending_journal_recovery"):
            return False
    return True


def _provider_error_sidecar_is_available(session) -> bool:
    try:
        sidecar_path = session.path
    except AttributeError:
        return True
    return bool(
        not getattr(session, "_loaded_metadata_only", False)
        and sidecar_path.exists()
        and isinstance(getattr(session, "messages", None), list)
    )


def _provider_error_lineage_sessions(session) -> list:
    """Resolve the durable sidecar owners for a viewed compression continuation."""
    if getattr(session, "read_only", False) or getattr(session, "is_cli_session", False):
        raise ProviderErrorDismissalUnavailable("read_only_session", 403)
    source = _provider_error_session_source(session)
    if not _provider_error_sidecar_is_available(session):
        raise ProviderErrorDismissalUnavailable("owner_unavailable", 409)
    if source not in {"webui", "fork"} or getattr(session, "pre_compression_snapshot", False):
        raise ProviderErrorDismissalUnavailable("read_only_session", 403)
    if not _provider_error_session_is_idle(session):
        raise ProviderErrorDismissalUnavailable("session_active", 409)

    sessions = [session]
    seen = {str(getattr(session, "session_id", "") or "")}
    current = session
    for _ in range(20):
        parent_id = str(getattr(current, "parent_session_id", "") or "").strip()
        if not parent_id:
            break
        if parent_id in seen:
            raise ProviderErrorDismissalUnavailable("ambiguous_lineage", 409)
        try:
            parent = get_session(parent_id)
        except (KeyError, OSError, ValueError) as exc:
            raise ProviderErrorDismissalUnavailable("lineage_unavailable", 409) from exc
        if parent is None:
            raise ProviderErrorDismissalUnavailable("lineage_unavailable", 409)
        if not getattr(parent, "pre_compression_snapshot", False):
            break
        if (
            getattr(parent, "read_only", False)
            or getattr(parent, "is_cli_session", False)
            or str(getattr(parent, "profile", None) or "default")
            != str(getattr(session, "profile", None) or "default")
            or not _provider_error_sidecar_is_available(parent)
        ):
            raise ProviderErrorDismissalUnavailable("lineage_unavailable", 409)
        parent_source = _provider_error_session_source(parent)
        if source == "fork" and parent_source != "fork":
            break
        if source != "fork" and parent_source not in {"webui", "fork"}:
            raise ProviderErrorDismissalUnavailable("lineage_unavailable", 409)
        if not _provider_error_session_is_idle(parent):
            raise ProviderErrorDismissalUnavailable("session_active", 409)
        if len(sessions) == 1:
            try:
                from api.models import _session_message_visible_key

                child_rows = getattr(session, "messages", None) or []
                parent_rows = getattr(parent, "messages", None) or []
                if len(child_rows) >= len(parent_rows) and all(
                    _session_message_visible_key(child_rows[index])
                    == _session_message_visible_key(parent_rows[index])
                    for index in range(len(parent_rows))
                ):
                    return [session]
            except Exception:
                raise ProviderErrorDismissalUnavailable("lineage_unavailable", 409) from None
        sessions.append(parent)
        seen.add(parent_id)
        current = parent
    else:
        raise ProviderErrorDismissalUnavailable("lineage_unavailable", 409)
    return list(reversed(sessions))


def _provider_error_owner_entries(session, *, projection_cache: bool = False) -> tuple[list[dict], str]:
    owners = _provider_error_lineage_sessions(session)
    if projection_cache and any(hasattr(owner, "path") for owner in owners):
        try:
            from api.models import Session

            persisted_owners = []
            for owner in owners:
                persisted = Session.load(str(getattr(owner, "session_id", "") or ""))
                if persisted is None:
                    raise ProviderErrorDismissalUnavailable("owner_unavailable", 409)
                persisted_owners.append(persisted)
            owners = persisted_owners
        except ProviderErrorDismissalUnavailable:
            raise
        except (KeyError, OSError, TypeError, ValueError) as exc:
            raise ProviderErrorDismissalUnavailable("owner_unavailable", 409) from exc
    cache_key = None
    cache_requires_signature = projection_cache and any(
        hasattr(owner, "path") for owner in owners
    )
    if projection_cache:
        try:
            from api.models import _sidecar_stat_signature

            owner_signatures = []
            for owner in owners:
                sidecar_path = owner.path
                signature = _sidecar_stat_signature(sidecar_path)
                if signature is None:
                    raise ValueError("sidecar signature unavailable")
                owner_signatures.append(
                    (
                        str(getattr(owner, "session_id", "") or ""),
                        tuple(signature),
                        len(getattr(owner, "messages", None) or []),
                    )
                )
            cache_key = (
                str(getattr(session, "session_id", "") or ""),
                tuple(owner_signatures),
            )
            with _PROVIDER_ERROR_PROJECTION_CACHE_LOCK:
                cached = _PROVIDER_ERROR_PROJECTION_CACHE.get(cache_key)
            if cached is not None:
                return copy.deepcopy(cached[0]), cached[1]
        except (AttributeError, OSError, TypeError, ValueError) as exc:
            if cache_requires_signature:
                raise ProviderErrorDismissalUnavailable("owner_unavailable", 409) from exc
            cache_key = None
    entries = []
    for owner in owners:
        rows = getattr(owner, "messages", None)
        if not isinstance(rows, list):
            raise ProviderErrorDismissalUnavailable("noncanonical_transcript", 409)
        for owner_index, row in enumerate(rows):
            entries.append(
                {
                    "session": owner,
                    "session_id": str(getattr(owner, "session_id", "") or ""),
                    "index": owner_index,
                    "row": row,
                    "stable_id": _provider_error_stable_id(row),
                    "row_digest": _provider_error_reference_digest(row),
                }
            )
    revision_payload = [
        {
            "session_id": entry["session_id"],
            "index": entry["index"],
            "stable_id": entry["stable_id"],
            "row_digest": entry["row_digest"],
        }
        for entry in entries
    ]
    revision = _provider_error_row_digest(
        {
            "viewed_session_id": str(getattr(session, "session_id", "") or ""),
            "entries": revision_payload,
        }
    )
    if cache_key is not None:
        cached_entries = [
            dict(entry, row=copy.deepcopy(entry["row"]))
            for entry in entries
        ]
        with _PROVIDER_ERROR_PROJECTION_CACHE_LOCK:
            _PROVIDER_ERROR_PROJECTION_CACHE[cache_key] = (cached_entries, revision)
            while len(_PROVIDER_ERROR_PROJECTION_CACHE) > _PROVIDER_ERROR_PROJECTION_CACHE_MAX:
                _PROVIDER_ERROR_PROJECTION_CACHE.pop(next(iter(_PROVIDER_ERROR_PROJECTION_CACHE)))
    return entries, revision


def _provider_error_reference_payload(viewed_session_id, owner_session_id, revision, stable_id, owner_index, row_digest):
    return {
        "version": 1,
        "viewed_session_id": str(viewed_session_id),
        "owner_session_id": str(owner_session_id),
        "owner_revision": str(revision),
        "stable_id": stable_id,
        "owner_index": int(owner_index),
        "row_digest": str(row_digest),
    }


def _provider_error_reference(**parts) -> str:
    encoded = json.dumps(parts, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


def provider_error_dismissal_plan(session, row_index: int, *, lock_held: bool = False):
    lock = nullcontext() if lock_held else _get_session_agent_lock(session.session_id)
    with lock:
        entries, revision = _provider_error_owner_entries(session)
        viewed_id = str(getattr(session, "session_id", "") or "")
        if isinstance(row_index, bool) or not isinstance(row_index, int):
            raise ProviderErrorDismissalUnavailable("message_not_found", 404)
        matches = [
            entry
            for entry in entries
            if entry["session_id"] == viewed_id and entry["index"] == row_index
        ]
        if len(matches) != 1:
            raise ProviderErrorDismissalUnavailable("message_not_found", 404)
        entry = matches[0]
        row = entry["row"]
        if not _provider_error_row_is_dismissible(row):
            raise ProviderErrorDismissalUnavailable("not_dismissible", 409)
        stable_id = entry["stable_id"]
        payload = _provider_error_reference_payload(
            viewed_id, entry["session_id"], revision, stable_id, row_index, entry["row_digest"],
        )
        return ProviderErrorDismissalPlan(
            viewed_session_id=viewed_id, owner_session_id=entry["session_id"],
            owner_index=row_index, owner_revision=revision, stable_id=stable_id,
            row_digest=entry["row_digest"], dismiss_ref=_provider_error_reference(**payload),
        )


def provider_error_dismissal_ref(session, row_index: int, *, lock_held: bool = False) -> str | None:
    try:
        return provider_error_dismissal_plan(session, row_index, lock_held=lock_held).dismiss_ref
    except ProviderErrorDismissalUnavailable:
        return None


def apply_provider_error_dismissal(session, dismiss_ref: str, *, lock_held: bool = False):
    if not isinstance(dismiss_ref, str) or len(dismiss_ref) != 64 or any(c not in "0123456789abcdef" for c in dismiss_ref):
        raise ProviderErrorDismissalUnavailable("invalid_dismiss_ref", 400)
    initial_owner_ids = [
        str(getattr(owner, "session_id", "") or "")
        for owner in _provider_error_lineage_sessions(session)
    ]
    lock_ids = sorted(set(initial_owner_ids))
    if not lock_held:
        with ExitStack() as stack:
            acquired_locks = set()
            for lock_id in lock_ids:
                lock = _get_session_agent_lock(lock_id)
                lock_key = id(lock)
                if lock_key in acquired_locks:
                    continue
                acquired_locks.add(lock_key)
                stack.enter_context(lock)
            fresh_owner_ids = [
                str(getattr(owner, "session_id", "") or "")
                for owner in _provider_error_lineage_sessions(session)
            ]
            if fresh_owner_ids != initial_owner_ids:
                raise ProviderErrorDismissalUnavailable("lineage_changed", 409)
            return apply_provider_error_dismissal(session, dismiss_ref, lock_held=True)

    entries, revision = _provider_error_owner_entries(session)
    matches = []
    viewed_id = str(getattr(session, "session_id", "") or "")
    for entry in entries:
        if not _provider_error_row_is_dismissible(entry["row"]):
            continue
        payload = _provider_error_reference_payload(
            viewed_id,
            entry["session_id"],
            revision,
            entry["stable_id"],
            entry["index"],
            entry["row_digest"],
        )
        if _provider_error_reference(**payload) == dismiss_ref:
            matches.append(entry)
    if len(matches) != 1:
        raise ProviderErrorDismissalUnavailable(
            "ambiguous_dismiss_ref" if len(matches) > 1 else "stale_dismiss_ref",
            409,
        )
    entry = matches[0]
    owner = entry["session"]
    owner_rows = getattr(owner, "messages", None)
    if not isinstance(owner_rows, list) or entry["index"] >= len(owner_rows):
        raise ProviderErrorDismissalUnavailable("stale_dismiss_ref", 409)
    current = owner_rows[entry["index"]]
    if (
        not _provider_error_row_is_dismissible(current)
        or _provider_error_reference_digest(current) != entry["row_digest"]
        or _provider_error_stable_id(current) != entry["stable_id"]
    ):
        raise ProviderErrorDismissalUnavailable("stale_dismiss_ref", 409)
    plan = ProviderErrorDismissalPlan(
        viewed_session_id=viewed_id,
        owner_session_id=entry["session_id"],
        owner_index=entry["index"],
        owner_revision=revision,
        stable_id=entry["stable_id"],
        row_digest=entry["row_digest"],
        dismiss_ref=dismiss_ref,
    )
    if current.get("_dismissed") is True:
        return plan
    snapshot = copy.deepcopy(owner.__dict__)
    sidecar_snapshot = _provider_error_sidecar_snapshot(owner)
    if sidecar_snapshot is not None and not sidecar_snapshot[2]:
        raise ProviderErrorDismissalUnavailable("persistence_failed", 409)
    try:
        current["_dismissed"] = True
        owner.save(touch_updated_at=False, skip_index=True)
    except Exception as exc:
        restored = _restore_provider_error_sidecar(sidecar_snapshot)
        if not restored and _provider_error_sidecar_contains_new_dismissed_row(sidecar_snapshot, current):
            return plan
        restore_regeneration_state(owner, snapshot)
        raise ProviderErrorDismissalUnavailable("persistence_failed", 409) from exc
    return plan


def project_provider_error_dismissal_capabilities(session, projected: dict) -> dict:
    """Add only detached public references to an already-scrubbed projection."""
    result = copy.deepcopy(projected) if isinstance(projected, dict) else {}
    messages = result.get("messages")
    if not isinstance(messages, list):
        return result
    for message in messages:
        if isinstance(message, dict):
            message.pop("_provider_error_dismiss_ref", None)
            message.pop("_provider_error_dismissed", None)
    try:
        entries, revision = _provider_error_owner_entries(session, projection_cache=True)
    except ProviderErrorDismissalUnavailable:
        return result
    used: set[tuple[str, int]] = set()
    viewed_id = str(getattr(session, "session_id", "") or "")
    for message in messages:
        if not isinstance(message, dict):
            continue
        stable_id = _provider_error_stable_id(message)
        digest = _provider_error_reference_digest(message)
        candidates = [
            entry
            for entry in entries
            if (entry["session_id"], entry["index"]) not in used
            and _provider_error_row_is_dismissible(entry["row"])
            and entry["row_digest"] == digest
            and entry["stable_id"] == stable_id
            and _provider_error_api_content_matches(message, entry["row"])
        ]
        if not candidates:
            continue
        if stable_id is None and len(candidates) != 1:
            continue
        if stable_id is not None:
            if len(candidates) != 1:
                continue
        entry = candidates[0]
        used.add((entry["session_id"], entry["index"]))
        source_row = entry["row"]
        payload = _provider_error_reference_payload(
            viewed_id, entry["session_id"], revision,
            entry["stable_id"], entry["index"], entry["row_digest"],
        )
        message["_provider_error_dismiss_ref"] = _provider_error_reference(**payload)
        if source_row.get("_dismissed") is True:
            message["_dismissed"] = True
        else:
            message.pop("_dismissed", None)
    return result


def settle_provider_error_session(session, error_message: dict, *, save=True, snapshot=None) -> bool:
    """Append and durably settle one provider-error row atomically.

    Producers use this adapter so an error notification never carries a dirty
    in-memory transcript after the sidecar write fails.
    """
    snapshot = copy.deepcopy(session.__dict__) if snapshot is None else copy.deepcopy(snapshot)
    sidecar_snapshot = _provider_error_sidecar_snapshot(session) if save else None
    if sidecar_snapshot is not None and not sidecar_snapshot[2]:
        restore_regeneration_state(session, snapshot)
        return False
    row = None
    try:
        row = copy.deepcopy(error_message)
        if isinstance(row, dict) and not _provider_error_stable_id(row):
            try:
                from api.streaming import _assign_stable_message_ids

                _assign_stable_message_ids(
                    [row],
                    getattr(session, "messages", None) or [],
                    getattr(session, "context_messages", None) or [],
                )
            except Exception:
                logger.debug("Failed to stamp stable provider-error row id", exc_info=True)
        if not isinstance(getattr(session, "messages", None), list):
            session.messages = []
        session.messages.append(row)
        if save:
            session.save()
        return True
    except Exception:
        restored = _restore_provider_error_sidecar(sidecar_snapshot)
        if not restored and _provider_error_sidecar_contains_row(sidecar_snapshot, row):
            return True
        restore_regeneration_state(session, snapshot)
        return False


def _regeneration_source_class(value):
    raw = str(value or "").strip().lower()
    if not raw:
        return ""
    if raw == "fork":
        return "fork"
    normalized = normalize_agent_session_source(raw).get("session_source")
    return str(normalized or raw).strip().lower()


def _regeneration_source_allowed(value):
    return _regeneration_source_class(value) in {"webui", "fork"}


def _selected_regeneration_turn_owned(session, row) -> bool:
    """Accept only a final row whose provenance proves WebUI ownership."""
    if getattr(session, "read_only", False) or not isinstance(row, dict):
        return False
    session_source = _regeneration_source_class(getattr(session, "session_source", None))
    imported_session = bool(
        getattr(session, "is_cli_session", False)
        or session_source not in {"", "webui", "fork"}
    )
    raw_sources = (
        getattr(session, "raw_source", None),
        getattr(session, "source_tag", None),
    )
    row_source = row.get("_source") or row.get("source")
    if session_source == "fork":
        if any(source and not _regeneration_source_allowed(source) for source in raw_sources):
            return False
        if row_source and not _regeneration_source_allowed(row_source):
            return False
        return bool(
            getattr(session, "parent_session_id", None)
            and row.get("_fork_child_turn") == getattr(session, "session_id", None)
        )
    if imported_session:
        token = row.get("_active_turn_token")
        if not isinstance(token, str) or not token.strip() or ":" not in token:
            return False
        stream_id, started_at = token.rsplit(":", 1)
        try:
            started = float(started_at)
        except (TypeError, ValueError):
            return False
        from api.process_event_utils import build_active_turn_token
        if not math.isfinite(started) or started <= 0:
            return False
        if build_active_turn_token(stream_id, started) != token:
            return False
    else:
        if any(source and not _regeneration_source_allowed(source) for source in raw_sources):
            return False
        if row_source and not _regeneration_source_allowed(row_source):
            return False
    return True


@dataclass(frozen=True)
class RegenerationTurn:
    user_index: int
    assistant_index: int
    message: dict
    message_text: str
    attachments: list
    source: str
    message_count: int
    revision: str
    row_digest: str


@dataclass(frozen=True)
class RegenerationPlan:
    canonical_rows: list
    canonical_context: list
    turn: RegenerationTurn
    revision: str
    row_digest: str
    message_count: int
    truncation_boundary: int


def plan_regeneration(session, *, expected_revision=None, lock_held=False):
    """Prepare one canonical display/context pair for a locked regeneration."""
    lock_context = nullcontext() if lock_held else _get_session_agent_lock(session.session_id)
    with lock_context:
        rows, context = regeneration_state(session, use_sidecar=True)
        revision = regeneration_revision_for(rows, session=session, context=context)
        if expected_revision is not None and expected_revision != revision:
            raise RegenerationUnavailable("stale_regeneration_revision")
        turn = resolve_regeneration_turn(
            rows, session=session, expected_revision=revision,
            lock_held=True, context=context,
        )
        return RegenerationPlan(
            canonical_rows=copy.deepcopy(rows),
            canonical_context=copy.deepcopy(context),
            turn=turn,
            revision=revision,
            row_digest=turn.row_digest,
            message_count=len(rows),
            truncation_boundary=turn.user_index + 1,
        )


def apply_regeneration_plan(
    session,
    plan: RegenerationPlan,
    *,
    return_context_user: bool = False,
):
    """Install the prepared pair and truncate it without a second authority read."""
    def _result(success, context_user=None):
        return (success, context_user) if return_context_user else success

    if not isinstance(plan, RegenerationPlan):
        return _result(False)
    rows = copy.deepcopy(plan.canonical_rows)
    context = copy.deepcopy(plan.canonical_context)
    if len(rows) != plan.message_count or plan.truncation_boundary != plan.turn.user_index + 1:
        return _result(False)
    if regeneration_revision_for(rows, session=session, context=context) != plan.revision:
        return _result(False)
    session.messages = rows
    session.context_messages = context
    current = session.messages[plan.turn.user_index]
    if not isinstance(current, dict) or current.get("role") != "user":
        return _result(False)
    truncate_session_at_keep(session, plan.truncation_boundary)
    prepared_context, context_boundary_index = truncate_context_for_display_keep(
        context,
        rows,
        plan.truncation_boundary,
        return_boundary_index=True,
    )
    session.context_messages = prepared_context if prepared_context or not context else context[: plan.truncation_boundary]
    retained_context_user = None
    if context_boundary_index is not None:
        for context_row in reversed(session.context_messages[: context_boundary_index + 1]):
            if isinstance(context_row, dict) and context_row.get("role") == "user":
                retained_context_user = context_row
                break
    return _result(True, retained_context_user)


def snapshot_regeneration_state(session):
    return copy.deepcopy(session.__dict__)


def restore_regeneration_state(session, snapshot):
    session.__dict__.clear()
    session.__dict__.update(copy.deepcopy(snapshot))


def _provider_error_sidecar_snapshot(session):
    try:
        path = Path(session.path)
    except (AttributeError, TypeError, ValueError, OSError):
        return None
    try:
        if not path.exists():
            return path, None, True
        return path, path.read_bytes(), True
    except OSError:
        return path, None, False


def _restore_provider_error_sidecar(snapshot) -> bool:
    if snapshot is None:
        return True
    path, contents, readable = snapshot
    if not readable:
        return False
    tmp = path.with_suffix(
        f".rollback.tmp.{os.getpid()}.{threading.current_thread().ident}"
    )
    try:
        if contents is None:
            path.unlink(missing_ok=True)
            return True
        with open(tmp, "wb") as handle:
            handle.write(contents)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        return True
    except Exception:
        logger.debug("Failed to restore provider-error sidecar", exc_info=True)
        return False
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def _provider_error_sidecar_contains_row(snapshot, row) -> bool:
    if snapshot is None or row is None:
        return False
    path, original_contents, readable = snapshot
    if not readable:
        return False
    try:
        expected_digest = _provider_error_reference_digest(row)
        def matching_count(contents) -> int:
            payload = json.loads(contents)
            rows = payload.get("messages") if isinstance(payload, dict) else None
            return sum(
                1
                for candidate in rows or []
                if (
                    isinstance(candidate, dict)
                    and _provider_error_reference_digest(candidate) == expected_digest
                    and _provider_error_stable_id(candidate) == _provider_error_stable_id(row)
                )
            )

        try:
            original_count = matching_count(original_contents) if original_contents is not None else 0
        except (TypeError, ValueError, json.JSONDecodeError):
            original_count = 0
        current_count = matching_count(path.read_bytes())
        return current_count > original_count
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return False


def _provider_error_sidecar_contains_new_dismissed_row(snapshot, row) -> bool:
    if snapshot is None or row is None:
        return False
    path, original_contents, readable = snapshot
    if not readable:
        return False
    try:
        expected_digest = _provider_error_reference_digest(row)
        expected_stable_id = _provider_error_stable_id(row)

        def dismissed_count(contents) -> int:
            payload = json.loads(contents)
            rows = payload.get("messages") if isinstance(payload, dict) else None
            return sum(
                1
                for candidate in rows or []
                if (
                    isinstance(candidate, dict)
                    and candidate.get("_dismissed") is True
                    and _provider_error_reference_digest(candidate) == expected_digest
                    and _provider_error_stable_id(candidate) == expected_stable_id
                )
            )

        try:
            original_count = dismissed_count(original_contents) if original_contents is not None else 0
        except (TypeError, ValueError, json.JSONDecodeError):
            original_count = 0
        current_count = dismissed_count(path.read_bytes())
        return current_count > original_count
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return False


def regeneration_revision_for(rows, *, session=None, context=None) -> str:
    """Hash the canonical writable transcript and its aligned context."""
    payload = json.dumps(
        {
            "session_id": str(getattr(session, "session_id", "") or "") if session is not None else "",
            "messages": list(rows or []),
            "context_messages": list(context or []),
            "truncation_watermark": getattr(session, "truncation_watermark", None) if session is not None else None,
            "truncation_boundary": getattr(session, "truncation_boundary", None) if session is not None else None,
        },
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def regeneration_transcript(session, *, state_messages=None):
    """Return the state.db-reconciled transcript used by every authority consumer."""
    if state_messages is None:
        return regeneration_state(session)[0]
    from api.models import reconciled_state_db_messages_for_session
    return reconciled_state_db_messages_for_session(session, state_messages=state_messages)


def regeneration_context(session):
    return regeneration_state(session)[1]


_REGENERATION_SIDECAR_ANCHOR_BUDGET = 200


def _sidecar_regeneration_read_floor(session):
    """Return a state.db tail-read floor anchored by the already-loaded sidecar.

    #6826: regenerating a large session must not re-materialize the full
    state.db transcript (a >1min stall on big sessions).  When the in-memory
    sidecar is a usable reconciliation base — append-only session with no
    active truncation markers and timestamped rows — return the timestamp
    floor for a bounded ``since_timestamp`` tail read.  Rows at/after the
    floor (including any gateway/server-applied tail the sidecar has not seen
    yet) are re-read and merged, and rows the sidecar already carries are
    deduplicated by the append-only merge, so the #6611 reconciliation
    authority is preserved on the fast path.

    Returns ``None`` when the sidecar cannot anchor a tail read; callers then
    fall back to the full state.db read (unchanged behavior).
    """
    if getattr(session, "truncation_watermark", None) not in (None, ""):
        return None
    if getattr(session, "truncation_boundary", None) not in (None, ""):
        return None
    messages = getattr(session, "messages", None)
    if not isinstance(messages, list) or not messages:
        return None
    from api.models import _message_timestamp_as_float

    timestamps = [_message_timestamp_as_float(message) for message in messages]
    if any(timestamp is None for timestamp in timestamps):
        return None
    # Conservative anchor: re-read a bounded tip window so sub-second/clock
    # drift near the sidecar tip cannot hide a concurrently appended state.db
    # row, while the raw read stays tiny for huge sessions.
    return min(timestamps[-_REGENERATION_SIDECAR_ANCHOR_BUDGET:])


def _bounded_tail_snapshot_if_safe(session, read_floor):
    """Return the bounded tail rows ONLY when it is provably identical to the
    full read; otherwise None (caller must fall back to the full read).

    #6826 r3: the skipped state.db prefix (rows older than the floor) must be
    represented identically in the sidecar — same count AND same ordered
    visible identity — and the bounded tail must not repeat any skipped key
    (occurrence-count collision: a new tail turn repeating an older prompt
    would be mistaken for the old sidecar duplicate and dropped). The prefix
    proof and the tail data come from ONE read transaction (no TOCTOU).

    Any mismatch, missing database, or uncertainty returns None, so the #6611
    regeneration authority never operates on an unreconciled view.
    """
    sid = getattr(session, "session_id", None)
    if not sid:
        return None
    profile = getattr(session, "profile", None)
    from api.models import (
        _session_message_visible_key,
        get_state_db_regeneration_tail_snapshot,
    )

    snap = get_state_db_regeneration_tail_snapshot(sid, read_floor, profile=profile)
    if snap is None:
        return None  # cannot obtain a stable single-snapshot → full read
    # Compression-anchor coverage: if the anchor predates the floor the bounded
    # read can drop compacted-tail context rows (display may still match).
    anchor = getattr(session, "compression_anchor_message_key", None)
    if isinstance(anchor, dict):
        try:
            anchor_ts = float(anchor.get("ts"))
        except (TypeError, ValueError):
            anchor_ts = None
        if anchor_ts is None or anchor_ts < read_floor:
            return None
    prefix = snap["prefix"]
    if prefix.get("count") == 0 and prefix.get("null_timestamp_count") == 0:
        # Empty skipped prefix: the bounded read already covers every row.
        return snap["tail"]
    # Non-empty skipped prefix: prove identical ordered visible identity.
    sidecar_keys = []
    for message in getattr(session, "messages", None) or []:
        if not isinstance(message, dict):
            continue
        try:
            ts = float(message.get("timestamp"))
        except (TypeError, ValueError):
            ts = None
        if ts is not None and ts < read_floor:
            key = _session_message_visible_key(message)
            if key is None:
                return None
            sidecar_keys.append(key)
    if list(snap["prefix_keys"]) != sidecar_keys:
        return None  # mismatch → full read
    # Occurrence-count collision (#6826 r3 #1): if any bounded-tail key ALSO
    # occurs in the skipped prefix, the reconciler may drop the repeated tail
    # row — fall back conservatively.
    prefix_key_set = set(snap["prefix_keys"])
    for key in snap["tail_keys"]:
        if key in prefix_key_set:
            return None
    # In-tail duplicates (#6826 r5): a repeated message wholly inside the
    # bounded tail makes the reconciler's context dedup diverge from the full
    # read (display may still match) → refuse the bounded path.
    if len(snap["tail_keys"]) != len(set(snap["tail_keys"])):
        return None
    return snap["tail"]


def regeneration_state(session, *, use_sidecar=False):
    """Read one immutable state.db snapshot and reconcile both transcript views.

    ``use_sidecar=True`` (#6826) anchors the state.db read to the already
    loaded in-memory sidecar: only a bounded tail (``since_timestamp`` floor)
    is re-read instead of the full transcript, and both views still route
    through :func:`reconciled_state_db_messages_for_session`, so the #6611
    reconciliation authority (recovered display/context pair survives local
    and gateway apply) is preserved on the fast path.

    The bounded tail is only trusted when
    :func:`_bounded_tail_snapshot_if_safe` proves the skipped state.db prefix
    is identical in the sidecar (count + ordered visible identity + no
    occurrence collision + compression anchor coverage), and the tail rows
    come from the SAME single read transaction as the proof (no TOCTOU);
    otherwise the read falls back to the full transcript.
    """
    from api.models import (
        get_state_db_session_messages,
        reconciled_state_db_messages_for_session,
    )

    bounded_tail = None
    if use_sidecar:
        read_floor = _sidecar_regeneration_read_floor(session)
        if read_floor is not None:
            bounded_tail = _bounded_tail_snapshot_if_safe(session, read_floor)
    if bounded_tail is not None:
        state_messages = bounded_tail
    else:
        state_messages = get_state_db_session_messages(
            getattr(session, "session_id", None),
            profile=getattr(session, "profile", None),
        )
    return (
        reconciled_state_db_messages_for_session(
            session,
            state_messages=state_messages,
        ),
        reconciled_state_db_messages_for_session(
            session,
            prefer_context=True,
            state_messages=state_messages,
        ),
    )


def regeneration_revision(session) -> str:
    rows, context = regeneration_state(session, use_sidecar=True)
    return regeneration_revision_for(
        rows,
        session=session,
        context=context,
    )


def regeneration_authority(
    session,
    rows=None,
    *,
    context=None,
    full_transcript=True,
    canonical_state=None,
):
    """Mint a revision only for a complete, writable, canonical transcript."""
    if not full_transcript:
        return None
    if getattr(session, "active_stream_id", None) or getattr(session, "pending_user_message", None):
        return None
    canonical_rows, canonical_context = canonical_state or regeneration_state(session)
    rows = list(canonical_rows if rows is None else rows)
    if not rows:
        return None
    if rows != canonical_rows:
        return None
    if context is not None and list(context or []) != canonical_context:
        return None
    try:
        resolve_regeneration_turn(
            canonical_rows,
            session=session,
            context=canonical_context,
        )
    except RegenerationUnavailable:
        return None
    return regeneration_revision_for(
        canonical_rows,
        session=session,
        context=canonical_context,
    )


def resolve_regeneration_turn(
    rows,
    *,
    session=None,
    expected_revision=None,
    lock_held=False,
    context=None,
):
    """Select the current session's final complete local exchange under its lock."""
    legacy_session_call = session is None and not isinstance(rows, (list, tuple))
    legacy_context = None
    if legacy_session_call:
        session = rows
        rows, legacy_context = regeneration_state(session)
    lock_context = (
        _get_session_agent_lock(session.session_id)
        if legacy_session_call and not lock_held
        else nullcontext()
    )
    with lock_context:
        rows = list(rows or [])
        if context is None:
            context = legacy_context
        if context is None:
            _, context = regeneration_state(session)
        context = list(context)
        revision = regeneration_revision_for(rows, session=session, context=context)
        if expected_revision is not None and expected_revision != revision:
            raise RegenerationUnavailable("stale_regeneration_revision")
        if getattr(session, "active_stream_id", None):
            raise RegenerationUnavailable("session_active")
        if getattr(session, "pending_user_message", None):
            raise RegenerationUnavailable("session_active")
        assistant_index = next(
            (
                index
                for index in range(len(rows) - 1, -1, -1)
                if isinstance(rows[index], dict)
                and rows[index].get("role") == "assistant"
                and _assistant_message_has_final_visible_text(rows[index])
            ),
            None,
        )
        if assistant_index is not None:
            index = next(
                (
                    candidate
                    for candidate in range(assistant_index - 1, -1, -1)
                    if isinstance(rows[candidate], dict)
                    and rows[candidate].get("role") == "user"
                ),
                None,
            )
        else:
            index = None
        if index is not None:
            if any(
                isinstance(row, dict) and row.get("role") == "user"
                for row in rows[assistant_index + 1:]
            ):
                raise RegenerationUnavailable("no_regenerable_turn", 400)
            if any(
                isinstance(row, dict) and row.get("role") in {"assistant", "tool"}
                for row in rows[assistant_index + 1:]
            ):
                raise RegenerationUnavailable("no_regenerable_turn", 400)
            row = rows[index]
            if not _selected_regeneration_turn_owned(session, row):
                raise RegenerationUnavailable("regeneration_read_only", 403)
            content = _extract_text(row.get("content", ""))
            if content:
                row_digest = hashlib.sha256(
                    json.dumps(
                        row,
                        sort_keys=True,
                        separators=(",", ":"),
                        default=str,
                    ).encode("utf-8")
                ).hexdigest()
                return RegenerationTurn(
                    index,
                    assistant_index,
                    copy.deepcopy(row),
                    content,
                    copy.deepcopy(row.get("attachments") or []),
                    str(row.get("_source") or "webui"),
                    len(rows),
                    revision,
                    row_digest,
                )
    raise RegenerationUnavailable("no_regenerable_turn", 400)


def _assistant_message_has_final_visible_text(message) -> bool:
    from api.streaming import _assistant_message_has_final_visible_text as _has_final_text

    return _has_final_text(message)


def _live_active_stream_id(session) -> str | None:
    """Return session.active_stream_id ONLY if that stream is live in THIS
    process; else None.

    After a restart/crash the persisted active_stream_id survives in the
    session JSON but the in-memory STREAMS / ACTIVE_RUNS that actually drive a
    live turn were wiped. Exposing that dead id (e.g. via /api/session/status to
    the hidden-tab poller) would make a client attach its renderer to a stream
    that never emits — a permanent fake "thinking" state. Liveness test mirrors
    routes._clear_stale_stream_state: live iff present in STREAMS (open SSE
    channel) or ACTIVE_RUNS (worker bookkeeping) — except that a
    ``phase="cancelling"`` run is excluded on both paths, because the worker may
    still be unwinding while the client already reached a terminal state for
    that stream.
    """
    stream_id = getattr(session, 'active_stream_id', None)
    if not stream_id:
        return None
    try:
        from api import config as _cfg
        with _cfg.ACTIVE_RUNS_LOCK:
            _active_run_present = stream_id in (_cfg.ACTIVE_RUNS or {})
            _active_run_entry = (_cfg.ACTIVE_RUNS or {}).get(stream_id)
        if _active_run_present and not _cfg.active_run_is_attachable(_active_run_entry):
            return None
        with _cfg.STREAMS_LOCK:
            if stream_id in _cfg.STREAMS:
                return stream_id
        if _active_run_present:
            return stream_id
    except Exception:
        # On any introspection failure, fail SAFE (report no live stream) rather
        # than surfacing a possibly-stale id.
        return None
    return None


def session_has_manual_title(session) -> bool:
    """Return whether adaptive title refresh should leave this title alone."""
    return getattr(session, 'manual_title', False) is True


def apply_session_title_rename(session, raw_title) -> str:
    """Apply user-driven rename semantics to a Session object.

    Non-empty custom titles are protected from adaptive refresh. Clearing the
    title, or resetting it to an automatic label, removes that protection so the
    normal auto-title path can run again.
    """
    title = str(raw_title or '').strip()[:80]
    if not title:
        title = 'Untitled'
    manual_title = title.strip().casefold() not in AUTO_TITLE_LABELS
    session.title = title
    session.manual_title = manual_title
    session.llm_title_generated = False
    return title


def mark_session_title_generated(session) -> None:
    """Mark a session title as generated by the title model."""
    session.llm_title_generated = True
    session.manual_title = False


def _truncate_at_last_user(messages):
    history = messages or []
    last_user_idx = None
    for i in range(len(history) - 1, -1, -1):
        if isinstance(history[i], dict) and history[i].get('role') == 'user':
            last_user_idx = i
            break
    if last_user_idx is None:
        return None
    return history[:last_user_idx]


def _truncation_watermark_for(messages):
    history = list(messages or [])
    if not history:
        return 0.0
    try:
        return float(history[-1].get('timestamp') or 0)
    except (AttributeError, TypeError, ValueError):
        return 0.0


def _stamp_intentional_shrink_generation(session, old_message_count: int, new_message_count: int) -> bool:
    """Stamp a new generation only when the visible message list shrinks."""
    if new_message_count >= old_message_count:
        return False
    session.intentional_shrink_generation = uuid.uuid4().hex
    return True


def truncate_context_for_display_keep(
    context_messages: list | None,
    full_messages: list | None,
    keep: int,
    *,
    return_boundary_index: bool = False,
) -> list:
    """Align model context with display prefix ``full_messages[:keep]``."""
    def _result(rows, boundary_index=None):
        return (rows, boundary_index) if return_boundary_index else rows

    if keep <= 0:
        return _result([])
    ctx = context_messages if isinstance(context_messages, list) else []
    msgs = full_messages if isinstance(full_messages, list) else []
    if not ctx:
        return _result([])
    if len(msgs) == 0:
        return _result([])
    # Only the perfectly-parallel case (display and context row-for-row) can be
    # sliced at the raw display index. When the two arrays differ in length —
    # in EITHER direction — they have diverged and need alignment:
    #   * context LONGER than display  → an injected summary/system prefix, etc.
    #   * context SHORTER than display → large-session context trimming dropped
    #     turns from the model context that the display still shows.
    # The shorter-context case is the one that broke forked large sessions: the
    # old ``len(ctx) <= len(msgs)`` guard short-circuited to ``ctx[:keep]``,
    # slicing the shorter context at the display index (landing mid-turn, e.g.
    # on an assistant tool_call whose result was past the cut). Fall through to
    # the signature matcher for both divergent cases so the cut lands on a real
    # turn boundary. Any residual dangling tool_use in the persisted context is
    # made wire-safe on the send path (streaming: ``_sanitize_messages_for_api``
    # strips unanswered tool_calls; gateway: it forwards no tool_calls/tool rows
    # at all), so we do not re-do that trimming here.
    if len(ctx) == len(msgs):
        return _result(ctx[:keep], min(keep, len(ctx)) - 1)

    def _row_signature(row: Any) -> tuple[str, ...] | None:
        if not isinstance(row, dict):
            return None
        tool_calls = row.get('tool_calls')
        tool_calls_sig = json.dumps(tool_calls, sort_keys=True, default=str) if tool_calls else ''
        return (
            str(row.get('role') or ''),
            str(row.get('content') or ''),
            str(row.get('tool_call_id') or ''),
            str(row.get('tool_use_id') or ''),
            str(row.get('tool_name') or row.get('name') or ''),
            tool_calls_sig,
        )

    # Materialize signatures once.  The matcher deliberately keeps the original
    # rows in ``ctx``; these records are only an alignment index. A signature
    # failure is deferred because the old matcher may return before reaching it.
    context_records = []
    deferred_signature_positions: list[int] = []
    for idx, row in enumerate(ctx):
        try:
            row_signature = _row_signature(row)
        except Exception:
            row_signature = None
            deferred_signature_positions.append(idx)
        context_records.append((row, row_signature))
    message_signatures = [_row_signature(message) for message in msgs]
    id_positions: dict[Any, list[int]] = {}
    signature_positions: dict[tuple[str, ...], list[int]] = {}
    signature_no_id_positions: dict[tuple[str, ...], list[int]] = {}
    signature_no_timestamp_positions: dict[tuple[str, ...], list[int]] = {}
    signature_no_id_no_timestamp_positions: dict[tuple[str, ...], list[int]] = {}
    signature_timestamp_positions: dict[
        tuple[tuple[str, ...], Any], list[int]
    ] = {}
    signature_timestamp_no_id_positions: dict[
        tuple[tuple[str, ...], Any], list[int]
    ] = {}
    unsafe_id_positions: list[int] = []
    unsafe_timestamp_positions: dict[tuple[str, ...], list[int]] = {}
    unsafe_timestamp_no_id_positions: dict[tuple[str, ...], list[int]] = {}

    def _safe_raw_value(value: Any) -> bool:
        # Keep dict lookup semantics aligned with the old explicit ``==`` scan:
        # only ordinary built-in metadata values may use the raw-value indexes.
        # In particular, custom objects and non-reflexive NaN values can make a
        # dict find a key that the old equality check rejected.
        if value is None:
            return True
        if type(value) not in (str, int, float):
            return False
        try:
            hash(value)
            return value == value
        except Exception:
            return False

    for idx, (context_row, context_sig) in enumerate(context_records):
        if context_sig is not None:
            signature_positions.setdefault(context_sig, []).append(idx)
        if not isinstance(context_row, dict):
            continue
        context_id = context_row.get('id')
        context_ts = context_row.get('timestamp')
        if context_sig is not None:
            if context_id is None:
                signature_no_id_positions.setdefault(context_sig, []).append(idx)
            if context_ts is None:
                signature_no_timestamp_positions.setdefault(context_sig, []).append(idx)
            if context_id is None and context_ts is None:
                signature_no_id_no_timestamp_positions.setdefault(
                    context_sig, []
                ).append(idx)
        if context_id is not None and _safe_raw_value(context_id):
            id_positions.setdefault(context_id, []).append(idx)
        elif context_id is not None:
            unsafe_id_positions.append(idx)
        if (
            context_sig is not None
            and context_ts is not None
            and _safe_raw_value(context_ts)
        ):
            timestamp_key = (context_sig, context_ts)
            signature_timestamp_positions.setdefault(timestamp_key, []).append(idx)
            if context_id is None:
                signature_timestamp_no_id_positions.setdefault(
                    timestamp_key, []
                ).append(idx)
        elif context_sig is not None and context_ts is not None:
            unsafe_timestamp_positions.setdefault(context_sig, []).append(idx)
            if context_id is None:
                unsafe_timestamp_no_id_positions.setdefault(
                    context_sig, []
                ).append(idx)

    def _first_at_or_after(positions: list[int] | None, start_idx: int) -> int | None:
        if not positions:
            return None
        offset = bisect_left(positions, start_idx)
        return positions[offset] if offset < len(positions) else None

    def _lazy_first_match_from(
        message: Any,
        start_idx: int,
    ) -> tuple[int | None, int | None]:
        """Match exactly as the original ordered scan did."""
        msg_sig = _row_signature(message)
        if msg_sig is None:
            return None, None
        weak_matches: list[int] = []
        for idx in range(start_idx, len(ctx)):
            context_row = ctx[idx]
            context_sig = _row_signature(context_row)
            if context_sig is None or not isinstance(context_row, dict):
                continue
            context_id = context_row.get('id')
            msg_id = message.get('id')
            if context_id is not None and msg_id is not None:
                if context_id == msg_id:
                    return idx, None
                continue
            if context_sig != msg_sig:
                continue
            context_ts = context_row.get('timestamp')
            msg_ts = message.get('timestamp')
            if context_ts is not None and msg_ts is not None:
                if context_ts == msg_ts:
                    return idx, None
                continue
            weak_matches.append(idx)
            if len(weak_matches) > 1:
                return None, weak_matches[0]
        return (weak_matches[0], None) if len(weak_matches) == 1 else (None, None)

    def _first_match_from(
        message_idx: int,
        message: Any,
        start_idx: int,
    ) -> tuple[int | None, int | None]:
        msg_sig = message_signatures[message_idx]
        if msg_sig is None:
            return None, None
        msg_id = message.get('id')
        msg_ts = message.get('timestamp')
        deferred_reachable = _first_at_or_after(
            deferred_signature_positions, start_idx
        ) is not None
        unsafe_id_reachable = (
            msg_id is not None
            and _first_at_or_after(unsafe_id_positions, start_idx) is not None
        )
        unsafe_timestamp_candidates = (
            unsafe_timestamp_no_id_positions.get(msg_sig, [])
            if msg_id is not None
            else unsafe_timestamp_positions.get(msg_sig, [])
        )
        unsafe_timestamp_reachable = (
            msg_ts is not None
            and _first_at_or_after(unsafe_timestamp_candidates, start_idx) is not None
        )
        if not _safe_raw_value(msg_id) or not _safe_raw_value(msg_ts):
            return _lazy_first_match_from(message, start_idx)
        if deferred_reachable or unsafe_id_reachable or unsafe_timestamp_reachable:
            return _lazy_first_match_from(message, start_idx)

        exact_positions: list[int] = []
        if msg_id is not None:
            id_idx = _first_at_or_after(id_positions.get(msg_id), start_idx)
            if id_idx is not None:
                exact_positions.append(id_idx)
        if msg_ts is not None:
            timestamp_key = (msg_sig, msg_ts)
            if msg_id is not None:
                timestamp_positions = signature_timestamp_no_id_positions.get(
                    timestamp_key, []
                )
            else:
                timestamp_positions = signature_timestamp_positions.get(
                    timestamp_key, []
                )
            timestamp_idx = _first_at_or_after(timestamp_positions, start_idx)
            if timestamp_idx is not None:
                exact_positions.append(timestamp_idx)
        exact_idx = min(exact_positions, default=None)

        if msg_id is not None and msg_ts is not None:
            weak_candidates = signature_no_id_no_timestamp_positions.get(msg_sig)
        elif msg_id is not None:
            weak_candidates = signature_no_id_positions.get(msg_sig)
        elif msg_ts is not None:
            weak_candidates = signature_no_timestamp_positions.get(msg_sig)
        else:
            weak_candidates = signature_positions.get(msg_sig)
        weak_start = bisect_left(weak_candidates, start_idx) if weak_candidates else 0
        weak_positions = weak_candidates[weak_start:weak_start + 2] if weak_candidates else []
        second_weak_idx = weak_positions[1] if len(weak_positions) > 1 else None
        if second_weak_idx is not None and (
            exact_idx is None or second_weak_idx < exact_idx
        ):
            return None, weak_positions[0]
        if exact_idx is not None:
            return exact_idx, None
        return (weak_positions[0], None) if len(weak_positions) == 1 else (None, None)

    matches = [None] * len(msgs)
    ambiguous_matches = [None] * len(msgs)
    next_ctx_idx = 0
    for msg_idx, message in enumerate(msgs):
        match_idx, ambiguous_idx = _first_match_from(msg_idx, message, next_ctx_idx)
        matches[msg_idx] = match_idx
        ambiguous_matches[msg_idx] = ambiguous_idx
        if match_idx is not None:
            next_ctx_idx = match_idx + 1

    # Cut at the first unkept display turn, or fallback to the last kept turn
    # if the boundary is not directly alignable.
    if keep < len(msgs):
        last_kept = None
        if keep > 0:
            last_kept = matches[keep - 1]
        first_unkept = matches[keep]
        if first_unkept is not None:
            if (
                last_kept is not None
                and isinstance(msgs[keep - 1], dict)
                and msgs[keep - 1].get('role') == 'user'
            ):
                return _result(ctx[:last_kept + 1], last_kept)
            return _result(ctx[:first_unkept], first_unkept - 1)
        if last_kept is not None:
            ambiguous_first_unkept = ambiguous_matches[keep]
            if (
                ambiguous_first_unkept is not None
                and isinstance(msgs[keep - 1], dict)
                and msgs[keep - 1].get('role') != 'user'
            ):
                return _result(ctx[:ambiguous_first_unkept], ambiguous_first_unkept - 1)
            return _result(ctx[:last_kept + 1], last_kept)

        # Both boundary rows were ambiguous/unmatched (common in large sessions
        # where context rows have lost their id/timestamp so the matcher can't
        # disambiguate structurally-identical rows). Only for the shorter-context
        # case: cut just past the LAST display row in the kept prefix that
        # resolved to a context index — preferring an exact match but accepting
        # an ambiguous (weak) one, mirroring how the sibling branches above fold
        # ``ambiguous_matches`` into the boundary. Accepting the weak match keeps
        # the forked boundary turn's own context (often exactly that ambiguous
        # row) instead of dropping back to an earlier exact match. It still errs
        # toward UNDER-keeping rather than slicing at the raw display index,
        # which would over-keep and mis-attribute later context rows to the kept
        # display turns. The context-longer case (injected summary prefix) is
        # left to the #5096 fallback below, which preserves that prefix.
        if len(ctx) < len(msgs):
            for i in range(keep - 1, -1, -1):
                resolved = matches[i] if matches[i] is not None else ambiguous_matches[i]
                if resolved is not None:
                    return _result(ctx[:resolved + 1], resolved)

    # Final fallback preserves #5096 behavior when alignment is unreliable
    # (no display row resolved to a context index, or keep >= len(msgs)).
    prefix_len = max(0, len(ctx) - len(msgs))
    prefix = ctx[:prefix_len]
    suffix = ctx[prefix_len:]
    result = prefix + suffix[:keep]
    return _result(result, len(result) - 1 if result else None)


def truncate_session_at_keep(session, keep: int) -> tuple[int, int]:
    """Truncate display + context; set watermark/boundary. Returns old counts."""
    full_messages = list(session.messages or [])
    old_msg_count = len(full_messages)
    old_ctx_count = len(getattr(session, 'context_messages', None) or [])
    session.messages = full_messages[:keep]
    _stamp_intentional_shrink_generation(session, old_msg_count, len(session.messages))
    if isinstance(getattr(session, 'context_messages', None), list):
        session.context_messages = truncate_context_for_display_keep(
            session.context_messages,
            full_messages,
            keep,
        )
    session.truncation_watermark = _truncation_watermark_for(session.messages)
    session.truncation_boundary = session.truncation_watermark
    return old_msg_count, old_ctx_count


def retry_last(session_id: str) -> dict[str, Any]:
    """Truncate the session to before the last user message, return its text.

    Mirrors gateway/run.py:_handle_retry_command. Caller (webui frontend)
    is expected to put the returned text back in the composer and call
    send() to resume the conversation -- the agent's gateway calls its own
    _handle_message; the webui has no equivalent in-process pipeline.

    Raises:
        KeyError: session not found
        ValueError: no user message in transcript
    """
    # Acquire the per-session agent lock as the outermost lock so that the
    # read-modify-write of s.messages is serialised with the periodic
    # checkpoint thread, cancel_stream, and all other session writers.
    # Lock ordering: _agent_lock → LOCK → _write_session_index (LOCK).
    with _get_session_agent_lock(session_id):
        # get_session() and Session.save() both acquire the module-level LOCK
        # internally (the latter via _write_session_index()), and LOCK is a
        # non-reentrant threading.Lock — so they MUST be called outside our
        # own `with LOCK:` block to avoid self-deadlocking.
        #
        # The race we close is the read-modify-write of s.messages: two
        # concurrent /api/session/retry calls could otherwise both compute the
        # same last_user_idx from the same history and double-truncate. We
        # serialize just the in-memory mutation; persistence happens inside
        # the per-session lock so the checkpoint thread cannot race us.
        #
        # Stale-object guard: on a cache miss, two concurrent get_session()
        # calls can each load and cache a *different* Session instance for the
        # same session_id (the second store clobbers the first). Re-bind to
        # the canonical cached instance inside the lock so the mutation lands
        # on the object the next reader will see, not a stale parallel copy.
        s = get_session(session_id)  # raises KeyError if missing
        with LOCK:
            s = SESSIONS.get(session_id, s)
            history = s.messages or []
            last_user_idx = None
            for i in range(len(history) - 1, -1, -1):
                if history[i].get('role') == 'user':
                    last_user_idx = i
                    break
            if last_user_idx is None:
                raise ValueError('No previous message to retry.')

            last_user_text = _extract_text(history[last_user_idx].get('content', ''))
            removed_count = len(history) - last_user_idx
            s.messages = history[:last_user_idx]
            _stamp_intentional_shrink_generation(s, len(history), len(s.messages))
            s.truncation_watermark = _truncation_watermark_for(s.messages)
            # Persist the original truncate cutoff so empty-sidecar recovery
            # can distinguish legitimate prefix from deleted suffix.
            s.truncation_boundary = s.truncation_watermark
            if isinstance(getattr(s, 'context_messages', None), list) and s.context_messages:
                truncated_context = _truncate_at_last_user(s.context_messages)
                if truncated_context is not None:
                    s.context_messages = truncated_context
        s.save()
    return {'last_user_text': last_user_text, 'removed_count': removed_count}


def undo_last(session_id: str) -> dict[str, Any]:
    """Remove the most recent user message and everything after it.

    Mirrors gateway/run.py:_handle_undo_command. Returns a preview of the
    removed text so the UI can confirm to the user.

    Raises:
        KeyError: session not found
        ValueError: no user message in transcript
    """
    # Acquire the per-session agent lock as the outermost lock so that the
    # read-modify-write of s.messages is serialised with the periodic
    # checkpoint thread, cancel_stream, and all other session writers.
    # Lock ordering: _agent_lock → LOCK → _write_session_index (LOCK).
    with _get_session_agent_lock(session_id):
        s = get_session(session_id)  # acquires LOCK transiently
        with LOCK:
            # Stale-object guard — see retry_last for the rationale.
            s = SESSIONS.get(session_id, s)
            history = s.messages or []
            last_user_idx = None
            for i in range(len(history) - 1, -1, -1):
                if history[i].get('role') == 'user':
                    last_user_idx = i
                    break
            if last_user_idx is None:
                raise ValueError('Nothing to undo.')

            removed_text = _extract_text(history[last_user_idx].get('content', ''))
            removed_count = len(history) - last_user_idx
            s.messages = history[:last_user_idx]
            _stamp_intentional_shrink_generation(s, len(history), len(s.messages))
            s.truncation_watermark = _truncation_watermark_for(s.messages)
            # Persist the original truncate cutoff.
            s.truncation_boundary = s.truncation_watermark
            if isinstance(getattr(s, 'context_messages', None), list) and s.context_messages:
                truncated_context = _truncate_at_last_user(s.context_messages)
                if truncated_context is not None:
                    s.context_messages = truncated_context
        s.save()  # outside LOCK -- save() re-acquires LOCK via _write_session_index()
    preview = (removed_text[:40] + '...') if len(removed_text) > 40 else removed_text
    return {
        'removed_count': removed_count,
        'removed_preview': preview,
    }


def session_status(session_id: str) -> dict[str, Any]:
    """Return a snapshot of session state for /status.

    Webui equivalent of gateway/run.py:_handle_status_command. The agent's
    "agent_running" comes from `session_key in self._running_agents`; the
    webui equivalent is whether the session has an active stream
    (active_stream_id is set).
    """
    s = get_session(session_id)
    inp = int(s.input_tokens or 0)
    out = int(s.output_tokens or 0)
    profile = getattr(s, 'profile', None) or 'default'
    try:
        from api.profiles import get_hermes_home_for_profile
        hermes_home = str(get_hermes_home_for_profile(profile))
    except Exception:
        hermes_home = ''
    return {
        'session_id': s.session_id,
        'title': s.title,
        'model': s.model,
        'profile': profile,
        'hermes_home': hermes_home,
        'workspace': s.workspace,
        'personality': s.personality,
        'message_count': len(s.messages or []),
        'created_at': s.created_at,
        'updated_at': s.updated_at,
        'agent_running': bool(getattr(s, 'active_stream_id', None)),
        # Expose the stream id itself (not just the agent_running bool) so a
        # hidden-tab poller can attach the live renderer to a server-initiated
        # turn (self-wake / cron / restart hook) without opening the persistent
        # per-session SSE while the tab is hidden. See messages.js hidden-tab
        # active-stream poll. Additive field — existing consumers ignore it.
        #
        # CRITICAL: only expose a stream id that is actually LIVE in this
        # process. After a restart/crash the persisted active_stream_id is stale
        # (the in-memory STREAMS/ACTIVE_RUNS were wiped) — handing that dead id
        # to the poller would make it attach a renderer to a stream that never
        # produces tokens (a permanent fake "thinking" state). Mirror
        # _clear_stale_stream_state's liveness test: a stream counts as live
        # only if it's in STREAMS (SSE channel open) or ACTIVE_RUNS (worker
        # bookkeeping). Otherwise report None so the poller waits for a REAL
        # server_turn_started instead of latching a ghost.
        'active_stream_id': _live_active_stream_id(s),
        'input_tokens': inp,
        'output_tokens': out,
        'total_tokens': inp + out,
        'estimated_cost': s.estimated_cost,
    }


def session_usage(session_id: str) -> dict[str, Any]:
    """Return token usage and cost for /usage.

    Mirrors gateway/run.py:_handle_usage_command's basic counters. The
    agent shows additional fields (rate-limit headroom etc.) that depend
    on provider API responses we don't have in webui -- those are deferred.
    """
    s = get_session(session_id)
    inp = int(s.input_tokens or 0)
    out = int(s.output_tokens or 0)
    return {
        'input_tokens': inp,
        'output_tokens': out,
        'total_tokens': inp + out,
        'estimated_cost': s.estimated_cost,
        'model': s.model,
    }


def _extract_text(content: Any) -> str:
    """Flatten message content to plain text. Agent stores either a string
    or a list of {type, text|...} parts; webui needs the user-typed text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for p in content:
            if not isinstance(p, dict):
                continue
            part_type = str(p.get('type') or '').lower()
            if part_type not in ('', 'text', 'input_text', 'output_text'):
                continue
            part_text = (
                p.get('text')
                or p.get('content')
                or p.get('input_text')
                or p.get('output_text')
                or ''
            )
            parts.append(str(part_text))
        return ' '.join(parts)
    return str(content)
