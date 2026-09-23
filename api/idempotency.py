"""Caller-supplied idempotency for ``POST /api/chat/start``.

A small, durable claim store that lets external callers retry a chat-start
request after a lost connection without re-admitting the same logical turn
twice. Contract (issue #7435):

* First valid request atomically claims the key BEFORE any agent turn is
  admitted. Two concurrent requests with the same key admit one turn.
* Retry with the same key + equivalent request replays the original
  acceptance identity (session_id, stream_id, turn_id) without starting a
  new turn, including after the original turn completes or the WebUI
  process restarts.
* Reusing a key with a DIFFERENT side-effect-relevant request returns 409
  (deterministic conflict).
* Expired or unparseable keys fail explicitly, never silently admit a
  potentially duplicate turn.
* Clients that omit the key keep their current behavior (no idempotency
  guarantee).
* Local and gateway-backed chat-start share the same chokepoint, so the
  semantics are equivalent across both backends.

Persistence: the store keeps an in-memory map of the active set and writes
through to ``<state_dir>/idempotency/store.json`` after every mutation. The
file is rewritten atomically (write-temp + ``os.replace`` + ``fsync`` on the
parent directory) so a crash mid-write cannot corrupt the durable map.
On startup the file is loaded once; corrupt entries are skipped, not
raised, so a malformed line cannot block WebUI from serving requests.

This module is deliberately small and dependency-free. The full chat-start
flow is still owned by ``api.routes._handle_chat_start``; this module
provides the claim/replay/release primitives that flow plugs in at the top
of the route.
"""
from __future__ import annotations

import json
import os
import threading
import time
from collections import OrderedDict
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

# Module-level logger placeholder; real logger injected lazily so importing
# this module never pulls the heavy routes module (avoid circular import).
import logging
logger = logging.getLogger(__name__)


# Status values for a stored record.
STATUS_PENDING = "pending"
STATUS_COMPLETE = "complete"

# Default retention for both pending and completed records. 24h is the
# contract's example; callers can override per-store for tests.
DEFAULT_TTL_SECONDS = 24 * 60 * 60

# Max records kept in memory + on disk. Oldest (by claimed_at) evicted first
# once this cap is hit, regardless of status, so a runaway caller cannot
# fill the disk. 10k is the contract's example; tests override to a small
# value to exercise eviction.
DEFAULT_MAX_RECORDS = 10_000

# Validation bounds for the caller-supplied key. Mirrors the contract
# language ("non-empty, bounded length, printable ASCII").
MAX_KEY_LENGTH = 200

# Sentinel exception types — the route maps these onto HTTP status codes.
class IdempotencyConflict(Exception):
    """Same key, different request fingerprint → deterministic 409."""


class IdempotencyKeyMissing(Exception):
    """Key is empty after trim / contains disallowed chars → 400."""


class IdempotencyKeyExpired(Exception):
    """Key was previously claimed but its retention window elapsed → 410."""


class IdempotencyInFlight(Exception):
    """Another in-flight claim for the same key+fingerprint → 409 with a
    deterministic message; caller can retry once the first completes."""


@dataclass
class IdempotencyRecord:
    key: str
    request_fingerprint: str
    status: str = STATUS_PENDING
    session_id: str = ""
    stream_id: str = ""
    turn_id: str = ""
    response_status: int = 0
    response_payload: dict[str, Any] = field(default_factory=dict)
    claimed_at: float = 0.0
    completed_at: float = 0.0

    def to_json(self) -> dict[str, Any]:
        d = asdict(self)
        # OrderedDict -> regular dict for json.dump; status stays a string.
        return d

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> "IdempotencyRecord":
        return cls(
            key=str(raw.get("key") or ""),
            request_fingerprint=str(raw.get("request_fingerprint") or ""),
            status=str(raw.get("status") or STATUS_PENDING),
            session_id=str(raw.get("session_id") or ""),
            stream_id=str(raw.get("stream_id") or ""),
            turn_id=str(raw.get("turn_id") or ""),
            response_status=int(raw.get("response_status") or 0),
            response_payload=dict(raw.get("response_payload") or {}),
            claimed_at=float(raw.get("claimed_at") or 0.0),
            completed_at=float(raw.get("completed_at") or 0.0),
        )


def compute_request_fingerprint(body: dict[str, Any]) -> str:
    """Hash the side-effect-relevant fields of a chat-start body.

    Volatile / presentation fields are excluded so cosmetic differences
    (e.g. client_ts, idempotency_key itself, profile hint) don't trigger
    a conflict. The goal: a "logically the same" retry collides as the
    same fingerprint; a deliberately different prompt does not.
    """
    side_effect_fields = (
        "session_id",
        "message",
        "attachments",
        "workspace",
        "model",
        "model_provider",
        "explicit_model_pick",
        "regenerate",
        "regeneration_revision",
        "moa_config",
        "keep_count",
        "prompt",
        "prompt_index",
    )
    normalized: dict[str, Any] = {}
    for name in side_effect_fields:
        if name in body:
            normalized[name] = body[name]
    encoded = json.dumps(normalized, sort_keys=True, separators=(",", ":"), default=str)
    # sha256 hex digest; the full digest is 64 chars, well within any
    # practical size budget, and collision-free enough for a per-key
    # comparison in a single process.
    import hashlib
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def validate_key(raw: Any) -> str:
    """Return the trimmed key or raise ``IdempotencyKeyMissing``.

    Accepts only non-empty printable ASCII; bounded to ``MAX_KEY_LENGTH``.
    """
    if raw is None:
        raise IdempotencyKeyMissing("idempotency key required")
    if not isinstance(raw, str):
        raise IdempotencyKeyMissing("idempotency key must be a string")
    key = raw.strip()
    if not key:
        raise IdempotencyKeyMissing("idempotency key required")
    if len(key) > MAX_KEY_LENGTH:
        raise IdempotencyKeyMissing(
            f"idempotency key too long (>{MAX_KEY_LENGTH} chars)"
        )
    # Printable ASCII only (no whitespace, no control chars). Reject
    # anything outside 0x21-0x7E.
    for ch in key:
        if ord(ch) < 0x21 or ord(ch) > 0x7E:
            raise IdempotencyKeyMissing(
                "idempotency key must be printable ASCII (0x21-0x7E)"
            )
    return key


def extract_key(handler: Any, body: dict[str, Any]) -> str | None:
    """Pull the caller-supplied key from the header OR the body field.

    Body field wins (per the contract — header is the universal transport,
    body is the explicit opt-in). Returns ``None`` when neither is present
    OR when the value is empty / whitespace-only, so the route knows to
    skip idempotency entirely (legacy behavior).
    """
    header_value = None
    if handler is not None:
        headers = getattr(handler, "headers", None)
        if headers is not None:
            try:
                header_value = headers.get("Idempotency-Key")
            except Exception:
                header_value = None
    body_value = body.get("idempotency_key") if isinstance(body, dict) else None
    # Body wins.
    chosen = body_value if body_value not in (None, "") else header_value
    if chosen is None:
        return None
    # Treat empty / whitespace-only as "no key" so a stray
    # ``Idempotency-Key:  `` header from a misconfigured client does
    # not surface a 400. Validation of the actual content (length,
    # charset) happens later in ``validate_key``.
    if not isinstance(chosen, str):
        return None
    stripped = chosen.strip()
    if not stripped:
        return None
    return chosen


def _state_dir() -> Path:
    """Return the WebUI state dir; matches ``api.config.STATE_DIR`` at runtime."""
    from api.config import STATE_DIR
    return Path(STATE_DIR)


def _store_path() -> Path:
    return _state_dir() / "idempotency" / "store.json"


class IdempotencyStore:
    """Thread-safe, durable claim store for chat-start idempotency.

    A single ``RLock`` guards the in-memory map AND every write to the
    durable file. Within one process, that lock + dict lookup is the
    atomicity guarantee: two threads racing on the same key see the same
    record state because the second is blocked until the first releases.
    Across processes, the durable file is rewritten after every mutation;
    a fresh process loads the file at startup so post-restart replay
    keeps working.
    """

    def __init__(
        self,
        *,
        path: Path | None = None,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        max_records: int = DEFAULT_MAX_RECORDS,
    ):
        self._path = Path(path) if path is not None else _store_path()
        self._ttl_seconds = float(ttl_seconds)
        self._max_records = int(max_records)
        self._lock = threading.RLock()
        # OrderedDict gives O(1) lookup + deterministic oldest-first
        # eviction order (insertion order). We re-insert on access so
        # "active" keys float to the end of the LRU.
        self._records: "OrderedDict[str, IdempotencyRecord]" = OrderedDict()
        self._loaded = False

    # -- persistence ---------------------------------------------------------

    def _load_locked(self) -> None:
        """Load the durable file; tolerate missing / corrupt entries.

        Must be called with ``self._lock`` held.
        """
        self._records.clear()
        path = self._path
        if not path.exists():
            self._loaded = True
            return
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as exc:
            logger.warning(
                "idempotency: could not read store file %s: %s; starting empty",
                path, exc,
            )
            self._loaded = True
            return
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            logger.warning(
                "idempotency: store file %s is corrupt (%s); starting empty",
                path, exc,
            )
            self._loaded = True
            return
        entries = data.get("records") if isinstance(data, dict) else None
        if not isinstance(entries, list):
            self._loaded = True
            return
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            try:
                rec = IdempotencyRecord.from_json(entry)
            except Exception as exc:  # malformed line; skip, do not raise
                logger.warning(
                    "idempotency: skipping malformed record in %s: %s",
                    path, exc,
                )
                continue
            if not rec.key:
                continue
            self._records[rec.key] = rec
        self._loaded = True

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        with self._lock:
            if self._loaded:
                return
            self._load_locked()

    def _evict_expired_locked(self) -> None:
        """Drop records past their TTL; must be called with the lock held."""
        if self._ttl_seconds <= 0:
            return
        cutoff = time.time() - self._ttl_seconds
        # Sweep the OrderedDict once. Collect keys first to avoid mutating
        # during iteration.
        expired = [
            k for k, r in self._records.items()
            if (r.claimed_at and r.claimed_at < cutoff)
        ]
        for k in expired:
            self._records.pop(k, None)

    def _evict_to_cap_locked(self) -> None:
        """Bound the in-memory map by the configured cap; oldest first."""
        while len(self._records) > self._max_records:
            oldest_key, _ = self._records.popitem(last=False)
            logger.debug(
                "idempotency: cap %d reached, evicted key %s",
                self._max_records, oldest_key,
            )

    def _persist_locked(self) -> None:
        """Atomically rewrite the durable file. Must hold the lock."""
        path = self._path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.warning(
                "idempotency: could not create store dir %s: %s",
                path.parent, exc,
            )
            return
        payload = {
            "version": 1,
            "saved_at": time.time(),
            "records": [r.to_json() for r in self._records.values()],
        }
        tmp_path = path.with_name(path.name + ".tmp")
        try:
            with open(tmp_path, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False, separators=(",", ":"))
                fh.flush()
                try:
                    os.fsync(fh.fileno())
                except OSError:
                    # fsync can fail on some filesystems; the temp file is
                    # local and will be renamed regardless.
                    pass
            os.replace(tmp_path, path)
        except OSError as exc:
            logger.warning(
                "idempotency: failed to persist store to %s: %s",
                path, exc,
            )
            try:
                if tmp_path.exists():
                    tmp_path.unlink()
            except OSError:
                pass
            return
        # Try to fsync the directory so the rename is durable across
        # power loss on filesystems that support it.
        try:
            dir_fd = os.open(path.parent, getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            pass

    # -- public api ----------------------------------------------------------

    def claim(self, key: str, fingerprint: str) -> IdempotencyRecord:
        """Atomically claim ``key`` for ``fingerprint``.

        Returns:
            * a new ``STATUS_PENDING`` record if this is the first claim;
            * the stored record if a previous claim matches the fingerprint
              and is still within TTL (caller then checks ``status`` /
              decides whether to replay, in-flight, etc.);
            * raises ``IdempotencyConflict`` on fingerprint mismatch;
            * raises ``IdempotencyKeyExpired`` on a stale prior claim
              (the contract requires an explicit failure rather than
              silently re-admitting a potentially duplicate turn);
            * raises ``IdempotencyInFlight`` on a still-pending duplicate
              (so the caller can return 409 and ask the user to retry
              once the first attempt completes).
        """
        self._ensure_loaded()
        with self._lock:
            existing = self._records.get(key)
            if existing is None:
                rec = IdempotencyRecord(
                    key=key,
                    request_fingerprint=fingerprint,
                    status=STATUS_PENDING,
                    claimed_at=time.time(),
                )
                self._records[key] = rec
                # Move to the end of the LRU ordering.
                self._records.move_to_end(key)
                self._evict_to_cap_locked()
                self._persist_locked()
                return rec
            # Existing record: check whether it has aged out BEFORE
            # any other comparison. The contract is "fail explicitly
            # rather than silently admitting a potentially duplicate
            # turn" — so a previously-claimed key whose TTL elapsed
            # must be refused, not transparently re-bound. The caller
            # is expected to pick a new key for what may or may not
            # be a fresh request. A record whose claimed_at is 0.0
            # (the dataclass default) is also considered expired,
            # since 0.0 is the 1970-01-01 epoch — well past any TTL.
            if self._ttl_seconds > 0:
                if (time.time() - existing.claimed_at) > self._ttl_seconds:
                    raise IdempotencyKeyExpired(
                        f"idempotency key {key!r} has expired; pick a new key"
                    )
            # Compare fingerprints, then status.
            if existing.request_fingerprint != fingerprint:
                raise IdempotencyConflict(
                    f"idempotency key {key!r} already used with a different request"
                )
            # Refresh LRU position so an actively-replayed key is not
            # evicted under a cap.
            self._records.move_to_end(key)
            if existing.status == STATUS_PENDING:
                raise IdempotencyInFlight(
                    f"idempotency key {key!r} is currently in flight"
                )
            # Complete: caller should replay the stored result.
            return existing

    def complete(
        self,
        key: str,
        *,
        session_id: str,
        stream_id: str,
        turn_id: str,
        response_status: int,
        response_payload: dict[str, Any],
    ) -> IdempotencyRecord:
        """Persist the result of a successful claim so retries can replay it.

        Idempotent on the (session_id, stream_id, turn_id) triple: if the
        stored record already matches, this is a no-op. Otherwise the
        stored record is overwritten (last-writer-wins on completion).
        """
        self._ensure_loaded()
        with self._lock:
            existing = self._records.get(key)
            now = time.time()
            if existing is None:
                # No pending claim — synthesize a complete record so a
                # post-hoc retry that arrives after a process restart
                # (where the in-memory state was lost) still gets a
                # replay. We have to bind it to a fingerprint, so reuse
                # an empty fingerprint marker; the caller (route) should
                # always call ``claim`` first, so this branch is a
                # defense-in-depth fallback only.
                rec = IdempotencyRecord(
                    key=key,
                    request_fingerprint="",
                    status=STATUS_COMPLETE,
                    session_id=session_id,
                    stream_id=stream_id,
                    turn_id=turn_id,
                    response_status=response_status,
                    response_payload=dict(response_payload or {}),
                    claimed_at=now,
                    completed_at=now,
                )
                self._records[key] = rec
            else:
                existing.status = STATUS_COMPLETE
                existing.session_id = session_id
                existing.stream_id = stream_id
                existing.turn_id = turn_id
                existing.response_status = response_status
                existing.response_payload = dict(response_payload or {})
                existing.completed_at = now
                rec = existing
            self._records.move_to_end(key)
            self._evict_to_cap_locked()
            self._persist_locked()
            return rec

    def release(self, key: str) -> None:
        """Drop a pending claim. Used when validation fails before acceptance.

        A completed record is NEVER released — that's the whole point of
        idempotency: the original result must be replayable forever (up
        to TTL) so a retry after a lost response still gets identity.
        """
        self._ensure_loaded()
        with self._lock:
            existing = self._records.get(key)
            if existing is None:
                return
            if existing.status == STATUS_PENDING:
                self._records.pop(key, None)
                self._persist_locked()

    def lookup(self, key: str) -> IdempotencyRecord | None:
        """Return the stored record for ``key`` or ``None`` if absent / expired.

        Does not raise; useful for diagnostics and tests.
        """
        self._ensure_loaded()
        with self._lock:
            self._evict_expired_locked()
            rec = self._records.get(key)
            if rec is not None:
                self._records.move_to_end(key)
            return rec

    def keys(self) -> Iterable[str]:
        self._ensure_loaded()
        with self._lock:
            return list(self._records.keys())

    def reset(self) -> None:
        """Drop all in-memory and on-disk state. Tests only."""
        with self._lock:
            self._records.clear()
            self._loaded = True
            try:
                if self._path.exists():
                    self._path.unlink()
            except OSError:
                pass

    def reload_from_disk(self) -> None:
        """Force a re-read of the durable file. Used by tests to simulate a restart."""
        with self._lock:
            self._loaded = False
            self._load_locked()

    @property
    def path(self) -> Path:
        return self._path


# Module-level singleton — constructed lazily so the env-driven STATE_DIR is
# resolved at request time, not at import time. Tests patch the constructor
# via the ``get_idempotency_store`` hook to install an isolated store.
_store: IdempotencyStore | None = None
_store_lock = threading.Lock()


def get_idempotency_store() -> IdempotencyStore:
    global _store
    if _store is not None:
        return _store
    with _store_lock:
        if _store is None:
            _store = IdempotencyStore()
        return _store


def set_idempotency_store(store: IdempotencyStore | None) -> None:
    """Inject a custom store (e.g. for tests); pass ``None`` to reset."""
    global _store
    with _store_lock:
        _store = store


def build_response_payload(
    record: IdempotencyRecord,
) -> dict[str, Any]:
    """Shape a stored record into the JSON the route returns on replay.

    Mirrors the dict that ``_start_chat_stream_for_session`` produces, so
    a client retry sees byte-identical identity-bearing fields.
    """
    payload = dict(record.response_payload or {})
    payload.setdefault("session_id", record.session_id)
    payload.setdefault("stream_id", record.stream_id)
    payload.setdefault("turn_id", record.turn_id)
    payload["replayed_from_idempotency_key"] = True
    return payload
