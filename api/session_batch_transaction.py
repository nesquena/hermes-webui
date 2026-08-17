"""Recoverable batch publication for lineage-wide session metadata changes.

A Session.save() publishes its sidecar before it publishes the sidebar index.
That ordering is safe for a single retryable save, but not for an all-or-none
lineage mutation.  This module stages every sidecar and the resulting index,
persists their complete old/new images in one write-ahead journal, and only
then begins publication.  A prepared/rollback journal is rolled back; a
committed journal is rolled forward.  Recovery is idempotent and runs at
server startup before ordinary session recovery.
"""
from __future__ import annotations

import base64
import copy
import json
import logging
import os
import threading
import uuid
from pathlib import Path

logger = logging.getLogger(__name__)

_JOURNAL_NAME = "_session_batch_transaction.json"
_JOURNAL_VERSION = 1
_BATCH_LOCK = threading.RLock()


class SessionBatchTransactionError(RuntimeError):
    """A batch failed, with an explicit durable-recovery disposition."""

    def __init__(
        self,
        message: str,
        *,
        transaction_id: str | None = None,
        phase: str,
        recovery_required: bool,
        recovery_errors: list[str] | None = None,
    ) -> None:
        super().__init__(message)
        self.transaction_id = transaction_id
        self.phase = phase
        self.recovery_required = recovery_required
        self.recovery_errors = list(recovery_errors or [])

    def response(self) -> dict:
        return {
            "error": str(self),
            "transaction_id": self.transaction_id,
            "phase": self.phase,
            "recovery_required": self.recovery_required,
            "recovery_errors": self.recovery_errors,
        }


def _fsync_directory(directory: Path) -> None:
    """Make a replace/unlink durable on platforms that support directory fsync."""
    if os.name == "nt":
        return
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _replace_bytes(path: Path, payload: bytes) -> None:
    """Durably atomically replace *path* with already-staged bytes."""
    from api.models import _safe_replace

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.batch.{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex}.tmp")
    try:
        with open(tmp, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        _safe_replace(tmp, path)
        _fsync_directory(path.parent)
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
        raise


def _remove_path(path: Path) -> None:
    path.unlink(missing_ok=True)
    _fsync_directory(path.parent)


def _encode(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _decode(value: str) -> bytes:
    return base64.b64decode(value.encode("ascii"), validate=True)


def _journal_path(session_dir: Path) -> Path:
    return session_dir / _JOURNAL_NAME


def _write_journal(session_dir: Path, journal: dict) -> None:
    payload = json.dumps(journal, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")
    _replace_bytes(_journal_path(session_dir), payload)


def _read_journal(session_dir: Path) -> dict | None:
    path = _journal_path(session_dir)
    if not path.exists():
        return None
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("version") != _JOURNAL_VERSION:
        raise ValueError("unsupported session batch journal")
    if value.get("decision") not in {"rollback", "commit"}:
        raise ValueError("invalid session batch journal decision")
    if not isinstance(value.get("files"), list) or not value.get("transaction_id"):
        raise ValueError("invalid session batch journal shape")
    return value


def _validated_image_path(session_dir: Path, image: dict) -> Path:
    name = image.get("name")
    if not isinstance(name, str) or not name or Path(name).name != name:
        raise ValueError("unsafe session batch journal filename")
    if name != "_index.json" and (not name.endswith(".json") or name.startswith("_")):
        raise ValueError("invalid session batch journal target")
    return session_dir / name


def _evict_recovered_sessions(journal: dict) -> None:
    """Force later reads to observe recovered durable images, not stale objects."""
    try:
        import api.models as models

        session_ids = {
            str(image.get("name"))[:-5]
            for image in journal.get("files", [])
            if isinstance(image, dict)
            and str(image.get("name") or "").endswith(".json")
            and not str(image.get("name") or "").startswith("_")
        }
        with models.LOCK:
            for sid in session_ids:
                models.SESSIONS.pop(sid, None)
    except Exception:
        logger.exception("Failed to evict sessions after batch recovery")


def _recover_pending_locked(
    session_dir: Path,
    *,
    decision: str | None = None,
    evict_recovered: bool = True,
) -> dict:
    path = _journal_path(session_dir)
    try:
        journal = _read_journal(session_dir)
    except Exception as exc:
        return {
            "found": path.exists(),
            "recovered": False,
            "decision": None,
            "transaction_id": None,
            "errors": [f"journal:{type(exc).__name__}"],
        }
    if journal is None:
        return {"found": False, "recovered": True, "decision": None, "transaction_id": None, "errors": []}

    chosen = decision or str(journal["decision"])
    if chosen not in {"rollback", "commit"}:
        return {
            "found": True,
            "recovered": False,
            "decision": chosen,
            "transaction_id": journal.get("transaction_id"),
            "errors": ["journal:invalid-decision"],
        }

    errors: list[str] = []
    for image in journal["files"]:
        name = str(image.get("name") or "unknown") if isinstance(image, dict) else "unknown"
        try:
            if not isinstance(image, dict):
                raise ValueError("invalid image")
            target = _validated_image_path(session_dir, image)
            if chosen == "rollback" and not bool(image.get("old_exists")):
                _remove_path(target)
            else:
                encoded = image.get("old") if chosen == "rollback" else image.get("new")
                if not isinstance(encoded, str):
                    raise ValueError("missing image bytes")
                _replace_bytes(target, _decode(encoded))
        except Exception as exc:
            errors.append(f"{name}:{type(exc).__name__}")

    if not errors:
        try:
            _remove_path(path)
        except Exception as exc:
            errors.append(f"journal-cleanup:{type(exc).__name__}")
    if not errors and evict_recovered:
        _evict_recovered_sessions(journal)
    return {
        "found": True,
        "recovered": not errors,
        "decision": chosen,
        "transaction_id": journal.get("transaction_id"),
        "errors": errors,
    }


def recover_pending_session_batch(session_dir: Path) -> dict:
    """Recover the durable batch journal, if any (idempotent startup hook)."""
    import api.models as models

    session_dir = Path(session_dir)
    with _BATCH_LOCK, models._INDEX_WRITE_LOCK:
        return _recover_pending_locked(session_dir)


def _full_index_entries(session_dir: Path, update_map: dict[str, object]) -> list[dict]:
    import api.models as models

    entry_map: dict[str, dict] = {sid: session.compact() for sid, session in update_map.items()}
    for path in sorted(session_dir.glob("*.json")):
        if path.name.startswith("_") or path.stem in update_map:
            continue
        session = models._load_session_from_path(path)
        if not session:
            continue
        entry = session.compact()
        sid = entry.get("session_id")
        if sid:
            existing = entry_map.get(sid)
            if existing is None or entry.get("message_count", 0) > existing.get("message_count", 0):
                entry_map[sid] = entry
    return list(entry_map.values())


def _stage_index_payload(session_dir: Path, index_path: Path, sessions: list[object]) -> bytes:
    """Build and validate the exact index image without publishing it."""
    import api.models as models

    update_map = {str(session.session_id): session for session in sessions}
    try:
        existing = json.loads(index_path.read_text(encoding="utf-8"))
        if not isinstance(existing, list):
            raise ValueError("session index must be a list")
        on_disk_ids = {
            path.stem
            for path in session_dir.glob("*.json")
            if not path.name.startswith("_")
        } | set(update_map)
        with models.LOCK:
            in_memory_ids = set(models.SESSIONS)
        entries = [
            entry for entry in existing
            if isinstance(entry, dict)
            and (entry.get("session_id") in in_memory_ids or entry.get("session_id") in on_disk_ids)
        ]
        updated = {sid: session.compact() for sid, session in update_map.items()}
        existing_ids = {entry.get("session_id") for entry in entries}
        entries.extend(entry for sid, entry in updated.items() if sid not in existing_ids)
        entries = [updated.get(entry.get("session_id"), entry) for entry in entries]
    except (OSError, json.JSONDecodeError, ValueError):
        entries = _full_index_entries(session_dir, update_map)

    entries.sort(key=lambda entry: entry.get("updated_at", 0), reverse=True)
    payload = json.dumps(entries, ensure_ascii=False, indent=2).encode("utf-8")
    parsed = json.loads(payload)
    indexed = {entry.get("session_id"): entry for entry in parsed}
    for sid, session in update_map.items():
        entry = indexed.get(sid)
        if not isinstance(entry, dict) or bool(entry.get("archived", False)) != bool(session.archived):
            raise ValueError(f"staged index validation failed for {sid}")
    return payload


def _restore_memory(snapshots: list[tuple[object, dict]]) -> None:
    for session, state in snapshots:
        session.__dict__.clear()
        session.__dict__.update(copy.deepcopy(state))


def commit_session_archive_batch(sessions: list[object], archived: bool) -> str:
    """Atomically publish ``archived`` for every fully validated session.

    Callers must hold the sorted per-session mutation locks.  This function adds
    the process-wide journal/index authority needed by disjoint lineages.
    Returns the durable transaction id on success.
    """
    import api.models as models

    session_dir = Path(models.SESSION_DIR)
    index_path = Path(models.SESSION_INDEX_FILE)
    ordered = sorted(list(sessions), key=lambda session: str(getattr(session, "session_id", "")))
    transaction_id = uuid.uuid4().hex
    if not ordered:
        raise SessionBatchTransactionError(
            "Lineage archive transaction has no sessions",
            transaction_id=transaction_id,
            phase="validation",
            recovery_required=False,
        )

    with _BATCH_LOCK, models._INDEX_WRITE_LOCK:
        prior = _recover_pending_locked(session_dir)
        if prior["found"] and not prior["recovered"]:
            raise SessionBatchTransactionError(
                "A prior lineage archive transaction still requires recovery",
                transaction_id=prior.get("transaction_id"),
                phase="preflight-recovery",
                recovery_required=True,
                recovery_errors=prior.get("errors"),
            )

        seen: set[str] = set()
        snapshots: list[tuple[object, dict]] = []
        images: list[dict] = []
        try:
            for session in ordered:
                sid = str(getattr(session, "session_id", "") or "")
                if not models.is_safe_session_id(sid) or sid in seen:
                    raise ValueError(f"invalid or duplicate session id {sid!r}")
                if getattr(session, "_loaded_metadata_only", False):
                    raise RuntimeError(f"metadata-only session {sid!r}")
                seen.add(sid)
                snapshots.append((session, copy.deepcopy(session.__dict__)))
                target = session_dir / f"{sid}.json"
                old_exists = target.exists()
                images.append({
                    "name": target.name,
                    "old_exists": old_exists,
                    "old": _encode(target.read_bytes()) if old_exists else None,
                })
            index_exists = index_path.exists()
            index_image = {
                "name": index_path.name,
                "old_exists": index_exists,
                "old": _encode(index_path.read_bytes()) if index_exists else None,
            }

            for session in ordered:
                session.archived = bool(archived)
            for image, session in zip(images, ordered, strict=True):
                payload = session._serialize_payload().encode("utf-8")
                parsed = json.loads(payload)
                if parsed.get("session_id") != session.session_id or bool(parsed.get("archived", False)) != bool(archived):
                    raise ValueError(f"staged sidecar validation failed for {session.session_id}")
                image["new"] = _encode(payload)
            index_image["new"] = _encode(_stage_index_payload(session_dir, index_path, ordered))
            images.append(index_image)
        except Exception as exc:
            _restore_memory(snapshots)
            raise SessionBatchTransactionError(
                f"Lineage archive transaction staging failed ({type(exc).__name__})",
                transaction_id=transaction_id,
                phase="staging",
                recovery_required=False,
            ) from exc

        journal = {
            "version": _JOURNAL_VERSION,
            "transaction_id": transaction_id,
            "decision": "rollback",
            "files": images,
        }
        try:
            _write_journal(session_dir, journal)
        except Exception as exc:
            _restore_memory(snapshots)
            raise SessionBatchTransactionError(
                f"Lineage archive transaction journal staging failed ({type(exc).__name__})",
                transaction_id=transaction_id,
                phase="journal",
                recovery_required=_journal_path(session_dir).exists(),
            ) from exc

        try:
            for image in images:
                target = _validated_image_path(session_dir, image)
                _replace_bytes(target, _decode(image["new"]))
            journal["decision"] = "commit"
            _write_journal(session_dir, journal)
        except Exception as exc:
            # Restore every preimage, including the member whose replace/index
            # failed. The durable rollback decision remains authoritative if
            # compensation itself cannot finish in this request.
            journal["decision"] = "rollback"
            try:
                _write_journal(session_dir, journal)
            except Exception:
                logger.exception("Failed to persist rollback decision for session batch %s", transaction_id)
            _restore_memory(snapshots)
            recovery = _recover_pending_locked(
                session_dir,
                decision="rollback",
                evict_recovered=False,
            )
            raise SessionBatchTransactionError(
                f"Lineage archive transaction publication failed ({type(exc).__name__})",
                transaction_id=transaction_id,
                phase="publication",
                recovery_required=not recovery["recovered"],
                recovery_errors=recovery["errors"],
            ) from exc

        try:
            _remove_path(_journal_path(session_dir))
        except Exception:
            # The commit decision and both image sets are durable. Leaving the
            # journal is safe: startup recovery will idempotently roll forward.
            logger.exception("Committed session batch %s left a recovery journal", transaction_id)

    return transaction_id
