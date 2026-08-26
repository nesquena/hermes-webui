"""Recoverable publication for lineage batches and backup transcript repair.

A Session.save() publishes its sidecar before it publishes the sidebar index.
That ordering is safe for a single retryable save, but not for an all-or-none
lineage mutation.  This module stages every sidecar and the resulting index,
persists their complete old/new images in one write-ahead journal, and only
then begins publication.  A prepared journal is completed in its recorded
direction; once every image is applied, its replay payload is replaced by a
compact cleanup-only marker.  Recovery is idempotent and runs at server startup
before ordinary session recovery.
"""
from __future__ import annotations

import base64
import copy
import json
import logging
import os
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path

try:  # pragma: no cover - platform-specific import
    import fcntl as _fcntl
except ImportError:  # pragma: no cover - Windows
    _fcntl = None

try:  # pragma: no cover - platform-specific import
    import msvcrt as _msvcrt
except ImportError:  # pragma: no cover - POSIX
    _msvcrt = None

logger = logging.getLogger(__name__)

_JOURNAL_NAME = "_session_batch_transaction.json"
_LEGACY_JOURNAL_VERSION = 1
_JOURNAL_VERSION = 2
_STORE_LOCK_NAME = "_session_store_transaction.lock"
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


@contextmanager
def _session_store_process_lock(session_dir: Path):
    """Own the shared session store across WebUI worker processes.

    Keep the lock inode permanently: unlinking it while another process waits
    could split later callers across different inodes. Fail closed on platforms
    without a supported advisory lock instead of publishing an unserialized
    lineage transaction or backup recovery.
    """
    session_dir.mkdir(parents=True, exist_ok=True)
    lock_path = session_dir / _STORE_LOCK_NAME
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(fd, "r+b", buffering=0) as lock_file:
        if _fcntl is not None:
            _fcntl.flock(lock_file.fileno(), _fcntl.LOCK_EX)
            try:
                yield
            finally:
                _fcntl.flock(lock_file.fileno(), _fcntl.LOCK_UN)
            return

        if _msvcrt is not None:
            if os.fstat(lock_file.fileno()).st_size == 0:
                lock_file.write(b"\0")
                os.fsync(lock_file.fileno())
            lock_file.seek(0)
            _msvcrt.locking(  # type: ignore[attr-defined]
                lock_file.fileno(), _msvcrt.LK_LOCK, 1  # type: ignore[attr-defined]
            )
            try:
                yield
            finally:
                lock_file.seek(0)
                _msvcrt.locking(  # type: ignore[attr-defined]
                    lock_file.fileno(), _msvcrt.LK_UNLCK, 1  # type: ignore[attr-defined]
                )
            return

        raise RuntimeError("cross-process session store locking is unavailable")


@contextmanager
def session_store_transaction_lock(session_dir: Path):
    """Serialize sidecar/index authority in-process and across processes."""
    import api.models as models

    session_dir = Path(session_dir)
    with _BATCH_LOCK:
        with _session_store_process_lock(session_dir):
            with models._INDEX_WRITE_LOCK:
                yield


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
    if (
        not isinstance(value, dict)
        or value.get("version") not in {_LEGACY_JOURNAL_VERSION, _JOURNAL_VERSION}
    ):
        raise ValueError("unsupported session batch journal")
    if value.get("decision") not in {"rollback", "commit"}:
        raise ValueError("invalid session batch journal decision")
    if not isinstance(value.get("files"), list) or not value.get("transaction_id"):
        raise ValueError("invalid session batch journal shape")
    if value.get("version") == _JOURNAL_VERSION:
        if value.get("kind") not in {"archive", "session_recovery"}:
            raise ValueError("invalid session batch journal kind")
        if not isinstance(value.get("applied"), bool):
            raise ValueError("invalid session batch journal applied state")
    return value


def _validated_image_path(session_dir: Path, image: dict) -> Path:
    name = image.get("name")
    if not isinstance(name, str) or not name or Path(name).name != name:
        raise ValueError("unsafe session batch journal filename")
    if name != "_index.json" and (not name.endswith(".json") or name.startswith("_")):
        raise ValueError("invalid session batch journal target")
    return session_dir / name


def _read_optional_bytes(path: Path) -> tuple[bool, bytes | None]:
    try:
        return True, path.read_bytes()
    except FileNotFoundError:
        return False, None


def _image_matches(
    current_exists: bool,
    current_payload: bytes | None,
    expected_exists: bool,
    expected_payload: bytes | None,
) -> bool:
    return current_exists == expected_exists and (
        not expected_exists or current_payload == expected_payload
    )


def _legacy_commit_was_fully_applied(journal: dict, decision: str) -> bool:
    """Recognize v1 archive commits, whose commit decision followed publication.

    V1 transcript-recovery journals also used ``decision=commit`` but wrote that
    decision *before* publication and omitted preimages. They cannot be assumed
    applied; divergent targets must fail closed rather than be overwritten.
    """
    return (
        journal.get("version") == _LEGACY_JOURNAL_VERSION
        and decision == "commit"
        and bool(journal.get("files"))
        and all(
            isinstance(image, dict)
            and "old_exists" in image
            and "old" in image
            for image in journal["files"]
        )
    )


def _journal_is_applied(journal: dict, decision: str) -> bool:
    if journal.get("version") == _JOURNAL_VERSION:
        return bool(journal.get("applied"))
    return _legacy_commit_was_fully_applied(journal, decision)


def _completed_journal(journal: dict, decision: str) -> dict:
    """Return a cleanup-only marker with no stale replay payload."""
    kind = journal.get("kind")
    if kind not in {"archive", "session_recovery"}:
        kind = "archive" if all(
            isinstance(image, dict) and "old_exists" in image
            for image in journal.get("files", [])
        ) else "session_recovery"
    return {
        "version": _JOURNAL_VERSION,
        "transaction_id": journal["transaction_id"],
        "kind": kind,
        "decision": decision,
        "applied": True,
        "files": [
            {"name": image.get("name")}
            for image in journal.get("files", [])
            if isinstance(image, dict)
        ],
    }


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
            "applied": False,
            "errors": [f"journal:{type(exc).__name__}"],
        }
    if journal is None:
        return {
            "found": False,
            "recovered": True,
            "decision": None,
            "transaction_id": None,
            "applied": False,
            "errors": [],
        }

    recorded = str(journal["decision"])
    chosen = decision or recorded
    applied = _journal_is_applied(journal, recorded)
    if chosen not in {"rollback", "commit"}:
        return {
            "found": True,
            "recovered": False,
            "decision": chosen,
            "transaction_id": journal.get("transaction_id"),
            "applied": applied,
            "errors": ["journal:invalid-decision"],
        }
    if applied and chosen != recorded:
        return {
            "found": True,
            "recovered": False,
            "decision": chosen,
            "transaction_id": journal.get("transaction_id"),
            "applied": True,
            "errors": ["journal:applied-decision-conflict"],
        }

    errors: list[str] = []
    reconciled_images = False
    if not applied:
        for image in journal["files"]:
            name = str(image.get("name") or "unknown") if isinstance(image, dict) else "unknown"
            try:
                if not isinstance(image, dict):
                    raise ValueError("invalid image")
                target = _validated_image_path(session_dir, image)
                current_exists, current_payload = _read_optional_bytes(target)

                if chosen == "rollback":
                    if "old_exists" not in image:
                        raise ValueError("missing rollback preimage")
                    desired_exists = bool(image["old_exists"])
                    desired_encoded = image.get("old")
                    if desired_exists:
                        if not isinstance(desired_encoded, str):
                            raise ValueError("missing old image bytes")
                        desired_payload = _decode(desired_encoded)
                    else:
                        desired_payload = None
                    alternate_exists = True
                    alternate_encoded = image.get("new")
                    if not isinstance(alternate_encoded, str):
                        raise ValueError("missing new image bytes")
                    alternate_payload = _decode(alternate_encoded)
                else:
                    desired_exists = True
                    desired_encoded = image.get("new")
                    if not isinstance(desired_encoded, str):
                        raise ValueError("missing new image bytes")
                    desired_payload = _decode(desired_encoded)
                    if "old_exists" not in image:
                        # Legacy transcript-recovery journals have no durable
                        # precondition. Replaying a divergent target could erase
                        # successful later work, so only accept an image that is
                        # already at the intended bytes.
                        if _image_matches(
                            current_exists,
                            current_payload,
                            desired_exists,
                            desired_payload,
                        ):
                            continue
                        raise RuntimeError("legacy commit image changed")
                    alternate_exists = bool(image["old_exists"])
                    alternate_encoded = image.get("old")
                    if alternate_exists:
                        if not isinstance(alternate_encoded, str):
                            raise ValueError("missing old image bytes")
                        alternate_payload = _decode(alternate_encoded)
                    else:
                        alternate_payload = None

                if _image_matches(
                    current_exists,
                    current_payload,
                    desired_exists,
                    desired_payload,
                ):
                    continue
                if not _image_matches(
                    current_exists,
                    current_payload,
                    alternate_exists,
                    alternate_payload,
                ):
                    raise RuntimeError("session batch image precondition failed")
                if desired_exists:
                    assert desired_payload is not None
                    _replace_bytes(target, desired_payload)
                else:
                    _remove_path(target)
                reconciled_images = True
            except Exception as exc:
                errors.append(f"{name}:{type(exc).__name__}")

        if not errors:
            try:
                journal = _completed_journal(journal, chosen)
                _write_journal(session_dir, journal)
                applied = True
            except Exception as exc:
                errors.append(f"journal-completion:{type(exc).__name__}")

    if not errors and applied:
        try:
            _remove_path(path)
        except Exception as exc:
            errors.append(f"journal-cleanup:{type(exc).__name__}")
    if not errors and evict_recovered and reconciled_images:
        _evict_recovered_sessions(journal)
    return {
        "found": True,
        "recovered": not errors,
        "decision": chosen,
        "transaction_id": journal.get("transaction_id"),
        "applied": applied,
        "reconciled_images": reconciled_images,
        "errors": errors,
    }


def recover_pending_session_batch(session_dir: Path) -> dict:
    """Recover the durable batch journal, if any (idempotent startup hook)."""
    session_dir = Path(session_dir)
    with session_store_transaction_lock(session_dir):
        return _recover_pending_locked(session_dir)


def recover_pending_session_batch_locked(session_dir: Path) -> dict:
    """Recover the shared journal while the caller owns the session-store lock."""
    return _recover_pending_locked(Path(session_dir))


def run_startup_batch_recovery(session_dir: Path) -> dict:
    """Replay the durable batch journal at boot, failing startup closed.

    Unlike ordinary best-effort .bak repair, the batch journal is the authority
    for resolving a possibly mixed lineage publication.  Serving the mixed
    images would violate the archive endpoint's all-or-none contract, so an
    unrecoverable journal aborts startup instead of merely being logged.
    """
    result = recover_pending_session_batch(session_dir)
    if result.get("found"):
        if not result.get("recovered"):
            raise RuntimeError(
                "session batch recovery remains incomplete: "
                + ", ".join(result.get("errors") or ["unknown error"])
            )
        print(
            f"[recovery] Recovered session batch {result.get('transaction_id')} "
            f"via {result.get('decision')}.",
            flush=True,
        )
    return result


def commit_session_recovery_locked(
    session_path: Path,
    sidecar_payload: bytes,
    index_payload: bytes,
) -> str:
    """Publish one recovered sidecar and its exact index under a commit intent.

    The caller owns ``session_store_transaction_lock``. Transcript recovery has
    no safe rollback target when the old sidecar is missing or malformed, so the
    durable decision is ``commit`` before the first publication. A crash after
    the sidecar replace leaves this same journal for startup to replay through
    the index image; equal live/backup message counts cannot hide the intent.
    """
    import api.models as models

    session_path = Path(session_path)
    session_dir = session_path.parent
    index_path = session_dir / "_index.json"
    transaction_id = uuid.uuid4().hex

    if (
        session_path.name.startswith("_")
        or session_path.suffix != ".json"
        or not models.is_safe_session_id(session_path.stem)
    ):
        raise ValueError("invalid recovered session path")
    try:
        sidecar = json.loads(sidecar_payload)
        index = json.loads(index_payload)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError("invalid recovery transaction payload") from exc
    if not isinstance(sidecar, dict) or sidecar.get("session_id") != session_path.stem:
        raise ValueError("recovered sidecar session id mismatch")
    if not isinstance(index, list):
        raise ValueError("recovered session index must be a list")
    matches = [
        entry
        for entry in index
        if isinstance(entry, dict) and entry.get("session_id") == session_path.stem
    ]
    if len(matches) != 1:
        raise ValueError("recovered session index must contain exactly one matching row")
    if matches[0].get("message_count") != len(sidecar.get("messages") or []):
        raise ValueError("recovered sidecar/index message counts differ")

    prior = _recover_pending_locked(session_dir)
    if prior["found"] and not prior["recovered"]:
        raise SessionBatchTransactionError(
            "A prior session transaction still requires recovery",
            transaction_id=prior.get("transaction_id"),
            phase="preflight-recovery",
            recovery_required=True,
            recovery_errors=prior.get("errors"),
        )

    # Recovery is commit-only, but preimages fence replay: an unapplied journal
    # may advance only bytes that still match the state observed under the store
    # lock. Divergent bytes are newer/unknown authority and fail closed.
    images = []
    for target, new_payload in (
        (session_path, sidecar_payload),
        (index_path, index_payload),
    ):
        old_exists, old_payload = _read_optional_bytes(target)
        images.append(
            {
                "name": target.name,
                "old_exists": old_exists,
                "old": _encode(old_payload) if old_exists and old_payload is not None else None,
                "new": _encode(new_payload),
            }
        )
    journal = {
        "version": _JOURNAL_VERSION,
        "transaction_id": transaction_id,
        "kind": "session_recovery",
        "decision": "commit",
        "applied": False,
        "files": images,
    }
    try:
        _write_journal(session_dir, journal)
    except Exception as exc:
        # _replace_bytes can make the journal rename durable before a later
        # directory-fsync error surfaces. If a complete intent exists, finish it
        # rather than returning while a pending authority remains.
        recovery = _recover_pending_locked(
            session_dir,
            decision="commit",
            evict_recovered=False,
        )
        if recovery["found"] and (
            recovery["recovered"]
            or (recovery.get("applied") and recovery.get("decision") == "commit")
        ):
            if not recovery["recovered"]:
                logger.error(
                    "Committed session recovery %s left a cleanup-only journal: %s",
                    transaction_id,
                    recovery["errors"],
                )
            return transaction_id
        raise SessionBatchTransactionError(
            f"Session recovery journal staging failed ({type(exc).__name__})",
            transaction_id=transaction_id,
            phase="journal",
            recovery_required=recovery["found"],
            recovery_errors=recovery["errors"],
        ) from exc

    try:
        # File order is part of the recovery contract: never publish an index
        # row before the recovered sidecar it describes exists.
        for image in images:
            target = _validated_image_path(session_dir, image)
            _replace_bytes(target, _decode(image["new"]))
    except Exception as exc:
        recovery = _recover_pending_locked(
            session_dir,
            decision="commit",
            evict_recovered=False,
        )
        if not recovery["recovered"] and not (
            recovery.get("applied") and recovery.get("decision") == "commit"
        ):
            raise SessionBatchTransactionError(
                f"Session recovery publication failed ({type(exc).__name__})",
                transaction_id=transaction_id,
                phase="publication",
                recovery_required=True,
                recovery_errors=recovery["errors"],
            ) from exc
        if not recovery["recovered"]:
            logger.error(
                "Committed session recovery %s left a cleanup-only journal: %s",
                transaction_id,
                recovery["errors"],
            )
        return transaction_id

    try:
        # Durably disarm replay before cleanup. The compact applied marker has no
        # old/new payload, so a failed unlink can coexist safely with later saves.
        _write_journal(session_dir, _completed_journal(journal, "commit"))
    except Exception as exc:
        recovery = _recover_pending_locked(
            session_dir,
            decision="commit",
            evict_recovered=False,
        )
        if not recovery["recovered"] and not (
            recovery.get("applied") and recovery.get("decision") == "commit"
        ):
            raise SessionBatchTransactionError(
                f"Session recovery completion failed ({type(exc).__name__})",
                transaction_id=transaction_id,
                phase="completion",
                recovery_required=True,
                recovery_errors=recovery["errors"],
            ) from exc
        if not recovery["recovered"]:
            logger.error(
                "Committed session recovery %s left a cleanup-only journal: %s",
                transaction_id,
                recovery["errors"],
            )
        return transaction_id

    try:
        _remove_path(_journal_path(session_dir))
    except Exception:
        # Retry consumption under the same lock. Persistent cleanup failure is
        # safe because the durable marker is applied and contains no replay bytes.
        recovery = _recover_pending_locked(
            session_dir,
            decision="commit",
            evict_recovered=False,
        )
        if not recovery["recovered"]:
            logger.exception(
                "Committed session recovery %s left a cleanup-only journal",
                transaction_id,
            )
    return transaction_id


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

    with session_store_transaction_lock(session_dir):
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
            "kind": "archive",
            "decision": "rollback",
            "applied": False,
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
            # The commit decision is written only after every image is durable.
            # Publish it as a compact cleanup-only marker, so a failed unlink
            # cannot replay stale sidecar/index bytes over later saves.
            _write_journal(session_dir, _completed_journal(journal, "commit"))
        except Exception as exc:
            # Restore every preimage, including the member whose replace/index
            # failed. The durable rollback decision remains authoritative if
            # compensation itself cannot finish in this request.
            journal["decision"] = "rollback"
            journal["applied"] = False
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
            # Retry consumption under the same lock. Persistent cleanup failure
            # leaves only an applied marker with no stale replay payload.
            recovery = _recover_pending_locked(
                session_dir,
                decision="commit",
                evict_recovered=False,
            )
            if not recovery["recovered"]:
                logger.exception(
                    "Committed session batch %s left a cleanup-only journal",
                    transaction_id,
                )

    return transaction_id
