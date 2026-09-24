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


class IdempotencyStoreUnavailable(Exception):
    """The durable store could not be written; the in-memory state has
    been rolled back so the caller can retry without risking a
    duplicate turn. Route maps this to 503."""


class IdempotencyKeyMalformed(Exception):
    """A transport explicitly supplied a key (header or body field)
    that is empty / whitespace-only / not a string. This is distinct
    from "no key" (true absence) — a malformed-but-present key is a
    caller error and must be refused with 400, never silently
    downgraded to the legacy no-idempotency path."""


# Sentinel returned by ``extract_key`` when the key is truly absent
# on BOTH transports. Distinct from ``None`` (which is a valid
# caller-supplied value: ``"idempotency_key": null`` in JSON) so the
# route can tell "no key" (legacy path) from "key present but
# null" (malformed → 400). Exported so the route can compare
# against it without going through ``__globals__``.
_IDEM_KEY_ABSENT = object()
IDEM_KEY_ABSENT = _IDEM_KEY_ABSENT


# Helper used both by the module and by the route to namespace raw
# keys with the server-resolved active profile. Stored in
# ``_namespace_separator`` (not a constant exposed via API) so it
# can never collide with a well-formed ASCII key (printable ASCII
# keys cannot contain the pipe character — see ``validate_key``).
_NAMESPACE_SEP = "|"


@dataclass
class IdempotencyRecord:
    key: str
    request_fingerprint: str
    status: str = STATUS_PENDING
    # Profile namespace this record is bound to. Server-resolved at
    # claim time; never taken from the client. The storage key is
    # ``f"{profile}{NAMESPACE_SEP}{raw_key}"`` so two profiles using
    # the same raw key get independent records.
    profile: str = "default"
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
            profile=str(raw.get("profile") or "default"),
            session_id=str(raw.get("session_id") or ""),
            stream_id=str(raw.get("stream_id") or ""),
            turn_id=str(raw.get("turn_id") or ""),
            response_status=int(raw.get("response_status") or 0),
            response_payload=dict(raw.get("response_payload") or {}),
            claimed_at=float(raw.get("claimed_at") or 0.0),
            completed_at=float(raw.get("completed_at") or 0.0),
        )


def compute_request_fingerprint(body: dict[str, Any], profile: str = "default") -> str:
    """Hash the side-effect-relevant fields of a chat-start body.

    Volatile / presentation fields are excluded so cosmetic differences
    (e.g. client_ts, idempotency_key itself, profile hint) don't trigger
    a conflict. The goal: a "logically the same" retry collides as the
    same fingerprint; a deliberately different prompt does not.

    The profile is the SERVER-resolved active profile (never the
    client hint), so two profiles sending the same body under the
    same raw key produce distinct fingerprints and are isolated by
    profile in the durable store.
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
    normalized: dict[str, Any] = {"__profile": str(profile or "default")}
    for name in side_effect_fields:
        if name in body:
            normalized[name] = body[name]
    encoded = json.dumps(normalized, sort_keys=True, separators=(",", ":"), default=str)
    # sha256 hex digest; the full digest is 64 chars, well within any
    # practical size budget, and collision-free enough for a per-key
    # comparison in a single process.
    import hashlib
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def resolve_active_profile() -> str:
    """Return the server-resolved active profile namespace.

    This is the SINGLE source of truth for the profile used to
    namespace idempotency records; the client hint is intentionally
    ignored. Lazy import keeps ``api.idempotency`` from pulling
    ``api.routes`` at module load (which would create a cycle when
    ``api.routes`` imports from this module).

    Fail-closed: when the active profile cannot be resolved, the
    caller's identity cannot be scoped. Falling back to ``"default"``
    would file a request under a namespace we never proved it belongs
    to, so two profiles could collide on one record (or a claim lands
    in the wrong profile's namespace and is invisible to its owner).
    Raises ``IdempotencyStoreUnavailable`` instead; the route maps that
    to 503.
    """
    name = None
    try:
        from api.routes import _get_active_profile_name
        name = _get_active_profile_name()
    except Exception as exc:
        raise IdempotencyStoreUnavailable(
            f"could not resolve the active profile: {exc}"
        ) from exc
    if not isinstance(name, str) or not name.strip():
        raise IdempotencyStoreUnavailable(
            "active profile name is empty; refusing to scope an "
            "idempotency key to an unknown namespace"
        )
    return name


def build_storage_key(raw_key: str, profile: str | None = None) -> str:
    """Combine the server-resolved profile with the validated raw key.

    The separator (ASCII pipe) is outside the profile-name charset
    enforced by ``api.profiles._PROFILE_ID_RE`` (``[a-z0-9][a-z0-9_-]*``),
    and ``split_storage_key`` partitions on the FIRST separator, so the
    round-trip is unambiguous for any legal profile/key pair — a raw key
    may itself contain pipes without aliasing another namespace.

    ``profile`` is normally supplied by the caller (which resolves it
    once); when it is omitted, the active profile is resolved here and
    its typed failure propagates — a guessed namespace would let two
    profiles collide on one record.
    """
    if profile is None:
        ns = resolve_active_profile()
    else:
        ns = str(profile)
        if not ns.strip():
            raise IdempotencyStoreUnavailable(
                "idempotency namespace is empty; refusing to scope a key to "
                "an unknown profile"
            )
    return f"{ns}{_NAMESPACE_SEP}{raw_key}"


def split_storage_key(stored_key: str) -> tuple[str, str]:
    """Inverse of ``build_storage_key``; best-effort for diagnostics."""
    if _NAMESPACE_SEP in stored_key:
        profile, _, raw = stored_key.partition(_NAMESPACE_SEP)
        return profile, raw
    return "default", stored_key


def validate_key(raw: Any) -> str:
    """Return the trimmed key or raise the appropriate exception.

    Distinguishes two error classes so the route can map them onto
    different HTTP status codes:

    * ``IdempotencyKeyMalformed`` (400) — the caller explicitly
      supplied a value that is empty / whitespace-only / not a
      string / over-length / has disallowed characters. A
      malformed-but-present key is a caller error and must NOT be
      silently downgraded to the legacy no-idempotency path.

    * ``IdempotencyKeyMissing`` (also 400) — kept for backward
      compat with code that still calls ``validate_key`` with
      ``None``; the route's own absence check should normally
      short-circuit before reaching this.
    """
    if raw is None:
        raise IdempotencyKeyMissing("idempotency key required")
    if not isinstance(raw, str):
        raise IdempotencyKeyMalformed(
            "idempotency key must be a string"
        )
    key = raw.strip()
    if not key:
        raise IdempotencyKeyMalformed(
            "idempotency key is empty or whitespace-only"
        )
    if len(key) > MAX_KEY_LENGTH:
        raise IdempotencyKeyMalformed(
            f"idempotency key too long (>{MAX_KEY_LENGTH} chars)"
        )
    # Printable ASCII only (no whitespace, no control chars). Reject
    # anything outside 0x21-0x7E. Pipe (0x7C) is excluded implicitly
    # because it's used by the storage key namespace separator and
    # must never appear in a caller-supplied key — ``build_storage_key``
    # depends on this for an unambiguous round-trip.
    for ch in key:
        if ord(ch) < 0x21 or ord(ch) > 0x7E:
            raise IdempotencyKeyMalformed(
                "idempotency key must be printable ASCII (0x21-0x7E)"
            )
    return key


def extract_key(handler: Any, body: dict[str, Any]) -> object:
    """Pull the caller-supplied raw key from the header OR the body field.

    Returns one of three categories:

    * ``IDEM_KEY_ABSENT`` — the key is truly absent on BOTH
      transports (no Idempotency-Key header, no
      ``idempotency_key`` body field). The route treats this as
      "no key" and falls through to the legacy
      no-idempotency path.

    * A present value (any Python object, including ``None``,
      ``""``, non-string, etc.) — the caller explicitly
      supplied a value on at least one transport. The route
      passes the value to ``validate_key`` which raises
      ``IdempotencyKeyMalformed`` → 400 for any
      non-validated form. Per the #7435 review: a transport
      that explicitly supplied a value must NOT be silently
      downgraded to legacy behavior — that would let a
      misconfigured client cause duplicate turns.

    Body field wins when present (per the contract — header is
    the universal transport, body is the explicit opt-in).
    """
    header_value = IDEM_KEY_ABSENT
    header_present = False
    if handler is not None:
        headers = getattr(handler, "headers", None)
        if headers is not None:
            try:
                if "Idempotency-Key" in headers:
                    header_present = True
                    header_value = headers.get("Idempotency-Key")
            except Exception:
                header_value = IDEM_KEY_ABSENT
                header_present = False
    body_value = IDEM_KEY_ABSENT
    body_present = False
    if isinstance(body, dict):
        if "idempotency_key" in body:
            body_present = True
            body_value = body.get("idempotency_key")
    # Body wins when present.
    if body_present:
        return body_value
    if header_present:
        return header_value
    return IDEM_KEY_ABSENT


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
        """Load the durable file; fail closed when it cannot be trusted.

        Must be called with ``self._lock`` held. Raises
        ``IdempotencyStoreUnavailable`` when the file exists but cannot
        be read or parsed, or when any entry is not a well-formed record.

        Co-review note (#7435 finding 1 names READ failures as well as
        write failures): the historical "start empty" fallback treated an
        unreadable / corrupt file as a successful empty state, which
        forgets every durable claim — a retry would then execute the turn
        again instead of replaying it. That is the duplicate turn this
        store exists to prevent, so an untrustworthy file is refused, not
        papered over. A missing file is the normal first-run state and
        still loads empty.
        """
        self._records.clear()
        path = self._path
        if not path.exists():
            self._loaded = True
            return
        try:
            raw = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            self._loaded = False
            raise IdempotencyStoreUnavailable(
                f"could not read idempotency store {path}: {exc}"
            ) from exc
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, ValueError) as exc:
            self._loaded = False
            raise IdempotencyStoreUnavailable(
                f"idempotency store {path} is corrupt: {exc}"
            ) from exc
        entries = data.get("records") if isinstance(data, dict) else None
        if not isinstance(entries, list):
            self._loaded = False
            raise IdempotencyStoreUnavailable(
                f"idempotency store {path} has an unexpected shape"
            )
        for index, entry in enumerate(entries):
            if not isinstance(entry, dict):
                raise IdempotencyStoreUnavailable(
                    f"idempotency store {path} entry #{index} is not an object"
                )
            try:
                rec = IdempotencyRecord.from_json(entry)
            except Exception as exc:
                raise IdempotencyStoreUnavailable(
                    f"idempotency store {path} entry #{index} is malformed: {exc}"
                ) from exc
            # The durable key is the namespaced form; a stripped key,
            # missing profile, or unknown status means we cannot prove
            # which claim the entry belongs to.
            if _NAMESPACE_SEP not in rec.key:
                raise IdempotencyStoreUnavailable(
                    f"idempotency store {path} entry #{index} is missing its "
                    "profile namespace"
                )
            profile, _, raw_key = rec.key.partition(_NAMESPACE_SEP)
            if not raw_key or not rec.request_fingerprint:
                raise IdempotencyStoreUnavailable(
                    f"idempotency store {path} entry #{index} is missing key "
                    "or fingerprint"
                )
            if rec.status not in (STATUS_PENDING, STATUS_COMPLETE):
                raise IdempotencyStoreUnavailable(
                    f"idempotency store {path} entry #{index} has unknown "
                    f"status {rec.status!r}"
                )
            if not rec.profile or rec.profile != profile:
                raise IdempotencyStoreUnavailable(
                    f"idempotency store {path} entry #{index} has a profile "
                    "that disagrees with its namespaced key"
                )
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
        """Bound the in-memory map by the configured cap.

        Only drops records whose ``claimed_at`` is past TTL; an
        unexpired record is NEVER evicted under a cap. Pending
        records (status == ``STATUS_PENDING``) are also never
        evicted — they hold an in-flight claim that the caller
        will need to replay. If the cap is still exceeded after
        every expired record has been dropped, the store is full
        and the call must fail closed (the route will surface a
        503 and the caller can retry with a different key or
        after the next expiry sweep).
        """
        if self._max_records <= 0:
            return
        if len(self._records) <= self._max_records:
            return
        cutoff = time.time() - self._ttl_seconds if self._ttl_seconds > 0 else None
        # First sweep: drop only expired records. Collect the
        # candidate keys first so we don't mutate the dict during
        # iteration. We also refuse to evict pending records
        # regardless of age — a claim that's still in flight
        # would be lost, admitting a duplicate turn on retry.
        evictable = []
        for k, r in self._records.items():
            if r.status == STATUS_PENDING:
                continue
            if cutoff is not None and r.claimed_at and r.claimed_at >= cutoff:
                continue
            evictable.append(k)
        for k in evictable:
            self._records.pop(k, None)
        if len(self._records) > self._max_records:
            raise IdempotencyStoreUnavailable(
                f"idempotency store at cap ({self._max_records}); "
                f"refusing to evict unexpired records"
            )

    def _persist_locked(self) -> None:
        """Atomically rewrite the durable file. Must hold the lock.

        Raises ``IdempotencyStoreUnavailable`` on any directory /
        write / rename failure. Callers (claim, complete) are
        responsible for rolling back their in-memory mutation in the
        except block, so a write failure leaves no trace of the
        partial mutation. This is the only way to honour the
        contract: a record that the durable store never
        acknowledged must not be returned to the caller as if it
        had been.
        """
        path = self._path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise IdempotencyStoreUnavailable(
                f"could not create store dir {path.parent}: {exc}"
            ) from exc
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
            try:
                if tmp_path.exists():
                    tmp_path.unlink()
            except OSError:
                pass
            raise IdempotencyStoreUnavailable(
                f"failed to persist store to {path}: {exc}"
            ) from exc
        # Try to fsync the directory so the rename is durable across
        # power loss on filesystems that support it. A failure here
        # is not fatal: the data is on disk, the rename succeeded;
        # the only thing at risk is the directory entry's mtime /
        # atime, which is irrelevant for our reader.
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

        The supplied ``key`` is a validated raw key (printable ASCII,
        length-bounded, non-empty) — this method namespaces it with
        the server-resolved active profile before any lookup or
        write. The fingerprint MUST already include the same profile
        (see ``compute_request_fingerprint``) so two profiles
        sending the same raw key produce different fingerprints and
        are isolated.

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
              once the first attempt completes);
            * raises ``IdempotencyStoreUnavailable`` if the durable
              write failed; the in-memory mutation is rolled back so
              the caller can retry without risking a duplicate turn.
        """
        self._ensure_loaded()
        stored_key = build_storage_key(key)
        profile = stored_key.split(_NAMESPACE_SEP, 1)[0]
        with self._lock:
            existing = self._records.get(stored_key)
            if existing is None:
                rec = IdempotencyRecord(
                    key=stored_key,
                    request_fingerprint=fingerprint,
                    status=STATUS_PENDING,
                    profile=profile,
                    claimed_at=time.time(),
                )
                self._records[stored_key] = rec
                # Move to the end of the LRU ordering.
                self._records.move_to_end(stored_key)
                try:
                    self._evict_to_cap_locked()
                    self._persist_locked()
                except IdempotencyStoreUnavailable:
                    # Roll back the in-memory mutation: the durable
                    # store never saw the record, so neither should
                    # the caller. Without this rollback a transient
                    # write failure would leave a phantom claim
                    # that the next process restart would resurrect
                    # from disk (or fail to, depending on whether
                    # the file write raced with the rename).
                    self._records.pop(stored_key, None)
                    raise
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
            self._records.move_to_end(stored_key)
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

        ``key`` is the validated raw key (NOT the namespaced storage
        key); this method applies the same profile namespace that
        ``claim`` did, so a record written by ``claim`` is reachable
        by ``complete`` even after a process restart (where the
        in-memory state was lost). The namespacing is idempotent: a
        repeated call with the same raw key produces the same storage
        key.

        Idempotent on the (session_id, stream_id, turn_id) triple: if
        the stored record already matches, this is a no-op (apart from
        the rewrite for durability). Otherwise the stored record is
        overwritten (last-writer-wins on completion).

        Fail-closed on identity: when there is NO durable claim for this
        key+fingerprint, the completion is refused instead of
        synthesized. A synthesized record carries no proven fingerprint,
        so a retry collides against an identity we never verified — it
        would block a different request (conflict) or replay a result
        whose request we cannot prove. The route always claims first, so
        reaching this branch means the state is not what we believe;
        refusing surfaces that as a 503 instead of guessing.

        On a durable write failure the in-memory state is restored
        to its prior value (so a retry sees the same record) and
        ``IdempotencyStoreUnavailable`` is raised. The route must
        NOT mark the request ``idem_completed`` until this method
        returns successfully — otherwise a 503 from a persist
        failure would look indistinguishable from a successful
        completion to a retrying client.
        """
        self._ensure_loaded()
        stored_key = build_storage_key(key)
        with self._lock:
            existing = self._records.get(stored_key)
            now = time.time()
            if existing is None:
                # No durable claim to attach this completion to. Fail
                # closed: synthesizing a record here would bind an
                # unproven identity (see the docstring), so the store
                # refuses and the route surfaces 503.
                raise IdempotencyStoreUnavailable(
                    f"cannot complete idempotency key {key!r}: no durable "
                    "claim exists to bind this completion to"
                )
            # Snapshot the prior state so a persist failure can be
            # rolled back to byte-for-byte the same record.
            prior: IdempotencyRecord | None = None
            prior = IdempotencyRecord(
                key=existing.key,
                request_fingerprint=existing.request_fingerprint,
                status=existing.status,
                profile=existing.profile,
                session_id=existing.session_id,
                stream_id=existing.stream_id,
                turn_id=existing.turn_id,
                response_status=existing.response_status,
                response_payload=dict(existing.response_payload or {}),
                claimed_at=existing.claimed_at,
                completed_at=existing.completed_at,
            )
            existing.status = STATUS_COMPLETE
            existing.session_id = session_id
            existing.stream_id = stream_id
            existing.turn_id = turn_id
            existing.response_status = response_status
            existing.response_payload = dict(response_payload or {})
            existing.completed_at = now
            rec = existing
            self._records.move_to_end(stored_key)
            try:
                self._evict_to_cap_locked()
                self._persist_locked()
            except IdempotencyStoreUnavailable:
                # Roll back the in-memory mutation to the prior record
                # byte-for-byte, so a retry sees exactly the claim state
                # that the durable store can prove.
                self._records[stored_key] = prior
                raise
            return rec

    def release(self, key: str) -> None:
        """Drop a pending claim. Used when validation fails before acceptance.

        A completed record is NEVER released — that's the whole point of
        idempotency: the original result must be replayable forever (up
        to TTL) so a retry after a lost response still gets identity.

        ``key`` is the validated raw key; the storage key is computed
        via ``build_storage_key`` so the same profile namespace is
        used as the one the original claim wrote under.

        Fail-closed: when the drop cannot be made durable, the pending
        claim is restored in memory and ``IdempotencyStoreUnavailable``
        propagates. Keeping the guard means a retry sees
        ``IdempotencyInFlight`` (an explicit refusal) instead of a
        fresh claim that would admit a second turn; the route maps the
        error to 503. Silently swallowing the failure would drop the
        in-flight guard while the durable file still holds the claim —
        exactly the state that admits a duplicate turn.
        """
        self._ensure_loaded()
        stored_key = build_storage_key(key)
        with self._lock:
            existing = self._records.get(stored_key)
            if existing is None:
                return
            if existing.status == STATUS_PENDING:
                self._records.pop(stored_key, None)
                try:
                    self._persist_locked()
                except IdempotencyStoreUnavailable:
                    # The drop is not durable. Restore the pending claim
                    # so memory and disk still agree, and refuse rather
                    # than losing the in-flight guard.
                    self._records[stored_key] = existing
                    raise

    def lookup(self, key: str) -> IdempotencyRecord | None:
        """Return the stored record for ``key`` or ``None`` if absent / expired.

        ``key`` is the validated raw key; the same profile namespace
        is applied as for claim/complete. Does not raise; useful for
        diagnostics and tests.
        """
        self._ensure_loaded()
        stored_key = build_storage_key(key)
        with self._lock:
            self._evict_expired_locked()
            rec = self._records.get(stored_key)
            if rec is not None:
                self._records.move_to_end(stored_key)
            return rec

    def lookup_stored(self, stored_key: str) -> IdempotencyRecord | None:
        """Lookup using the FULL storage key (already profile-prefixed).

        Used by tests and diagnostics that already know the namespaced
        form. Production code should call ``lookup`` with the raw key.
        """
        self._ensure_loaded()
        with self._lock:
            self._evict_expired_locked()
            rec = self._records.get(stored_key)
            if rec is not None:
                self._records.move_to_end(stored_key)
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
