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
every mutation to an append-only write-ahead log,
``<state_dir>/idempotency/store.json.wal`` (JSON lines, each op carrying a
monotonic ``seq``), fsync'd before the mutation is acknowledged. The full
snapshot ``<state_dir>/idempotency/store.json`` is rewritten atomically
(write-temp + ``os.replace`` + directory ``fsync``) only when the WAL
crosses its compaction threshold, so a 10k-record store no longer pays a
~0.7-1.7 s whole-file rewrite per claim/completion (#7782 re-gate review).
On startup the snapshot is loaded once and the WAL is replayed on top
(ops with ``seq`` <= the snapshot watermark are no-ops), so a crash
mid-write cannot corrupt the durable map and a crash between WAL append and
compaction loses nothing. Corrupt snapshot entries raise, not skip, so a
malformed line cannot silently forget durable claims; a torn WAL tail (an
append that was never fsync-acknowledged) is truncated at the last valid
line.

Expiry and tombstones (contract call 5): a record that completed past its
TTL is dropped from the record map, but its key is remembered in a separate
bounded ``expired_keys`` set that is persisted alongside the records.
Re-claiming that key with the same fingerprint does NOT silently admit a new
turn — ``claim()`` raises ``IdempotencyKeyExpired`` → 410 — because the
client retrying after losing a response must be told the turn is gone, not
handed a fresh execution of the same request. The tombstone set has its own
cap (``TOMBSTONE_MAX_RECORDS``) and lives outside the record cap: a full
record store must not be able to re-admit an already-run turn. The tradeoff
is documented and accepted — when the tombstone bound is exceeded the oldest
tombstone is dropped and that one key becomes re-admittable, versus evicting
tombstones by record pressure which would stall every fresh key with 503.

Crash-window hardening (contract call 1): the route mints a PROVISIONAL
turn identity and persists it with the claim (``claim_with_identity`` →
``pending_stream_id``), so a crash between the claim and the turn being
admitted leaves a pending record that still carries the provisional id — but
that id is never accepted by a turn, so it is NOT replayable. A pending
record recovered from disk with NO proven admitted stream (``stream_id``
empty) is marked outcome-unknown at LOAD time (``_load_locked``; the startup
``sweep_pending_claims`` remains as belt-and-braces) and fails closed with
``IdempotencyOutcomeUnknown`` → 409 WITHOUT a retry hint, so an operator is
never told to poll for a day on a turn whose outcome is unknowable — and the
provisional id is never promoted into a fabricated replay.

This module is deliberately small and dependency-free. The full chat-start
flow is still owned by ``api.routes._handle_chat_start``; this module
provides the claim/replay/release primitives that flow plugs in at the top
of the route.
"""
from __future__ import annotations

import errno
import json
import os
import threading
import time
from collections import OrderedDict
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Iterable

from api.paths import _fsync_directory

# Module-level logger placeholder; real logger injected lazily so importing
# this module never pulls the heavy routes module (avoid circular import).
import logging
logger = logging.getLogger(__name__)


# Status values for a stored record.
STATUS_PENDING = "pending"
STATUS_COMPLETE = "complete"
# Not a persisted status: a tombstone is a complete record whose key is
# remembered after expiry so a re-claim of the same key fails loudly rather
# than admitting what may be a duplicate turn (#7782 contract call 5).
# ``expired_keys`` is a separate bounded set that survives the record's
# removal from ``_records`` — see ``_remember_expired_key_locked``.
STATUS_TOMBSTONE = "complete_expired"

# Default retention for both pending and completed records. 24h is the
# contract's example; callers can override per-store for tests.
DEFAULT_TTL_SECONDS = 24 * 60 * 60

# Max records kept in memory + on disk. Oldest (by claimed_at) evicted first
# once this cap is hit, regardless of status, so a runaway caller cannot
# fill the disk. 10k is the contract's example; tests override to a small
# value to exercise eviction.
DEFAULT_MAX_RECORDS = 10_000

# Bound on the expired-key tombstone set (#7782 contract call 5). Tombstones
# live OUTSIDE the record cap on purpose — a full record store must not be
# able to silently re-admit a turn that already ran — but the set itself
# needs its own ceiling so a pathological caller cannot grow it forever.
# At worst the oldest tombstone is dropped and that one key becomes
# re-admittable; the alternative (evicting a tombstone only when the record
# store is full) would stall every fresh key with 503.
TOMBSTONE_MAX_RECORDS = 10_000

# Validation bounds for the caller-supplied key. Mirrors the contract
# language ("non-empty, bounded length, printable ASCII").
MAX_KEY_LENGTH = 200

# ── Tombstone origin statuses ────────────────────────────────────────────────
# A tombstone (an expired key that must not be re-admitted) remembers WHY it
# was created: the evicted record may have COMPLETED (a turn ran) or been
# PENDING (the #7782 crash window — the turn may never have run). The origin
# is persisted with the tombstone so the 410 refusal can state the truth:
# "previously completed" is a lie for a never-admitted pending claim.
# ``unknown`` is the legacy v2 spelling (bare keys, no origin recorded).
_TOMBSTONE_ORIGIN_COMPLETE = STATUS_COMPLETE  # "complete"
_TOMBSTONE_ORIGIN_PENDING = STATUS_PENDING  # "pending"
_TOMBSTONE_ORIGIN_UNKNOWN = "unknown"
_VALID_TOMBSTONE_ORIGINS = frozenset(
    {_TOMBSTONE_ORIGIN_COMPLETE, _TOMBSTONE_ORIGIN_PENDING, _TOMBSTONE_ORIGIN_UNKNOWN}
)

# ── Write-ahead log ──────────────────────────────────────────────────────────
# Every mutation is appended to ``store.json.wal`` as one JSON line per op
# (``{"seq": N, "op": "put"|"del"|"expire", ...}``) and fsync'd before the
# mutation is acknowledged; the full snapshot is only rewritten when the WAL
# crosses its compaction threshold (#7782 re-gate: whole-file rewrite per
# mutation cost 0.7-1.7 s on a 10k-record store). The load path applies the
# snapshot first, then replays WAL ops with ``seq`` above the snapshot
# watermark, so a crash between WAL append and compaction loses nothing.
_WAL_EXT = ".wal"
_WAL_OP_PUT = "put"
_WAL_OP_DEL = "del"
_WAL_OP_EXPIRE = "expire"
_WAL_OP_TYPES = frozenset({_WAL_OP_PUT, _WAL_OP_DEL, _WAL_OP_EXPIRE})
# Compaction threshold floor, and the adaptive part: compact when the WAL
# holds at least 2x the live state (records + tombstones) captured at the
# last compaction (``store._wal_compaction_baseline``), so the WAL stays
# bounded and the amortized compaction cost is O(1) per mutation — one
# ~0.7 s snapshot rewrite per ~2N mutations on an N-record store, then the
# WAL restarts from empty. ``max(2048, 2 * baseline)`` floors a small
# store's WAL at ~2048 ops.
_WAL_COMPACT_MIN_OPS = 2048

# Sentinel exception types — the route maps these onto HTTP status codes.
class IdempotencyConflict(Exception):
    """Same key, different request fingerprint → deterministic 409."""


class IdempotencyKeyMissing(Exception):
    """Key is empty after trim / contains disallowed chars → 400."""


class IdempotencyKeyExpired(Exception):
    """Key was previously claimed but its retention window elapsed → 410."""


class IdempotencyOutcomeUnknown(Exception):
    """A pending claim was recovered from disk with no bound stream identity.

    The durable store cannot say whether the turn ran (#7782 finding 1: a
    crash between the claim and the identity binding). Distinct from
    ``IdempotencyInFlight`` on purpose — that one means "a live attempt owns
    this key, retry in a second", while this one means "the outcome is
    unknowable; do NOT blind-retry". The route maps it to 409 WITHOUT a
    ``retry_after_seconds`` hint.
    """


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
    # #7782 finding 1: identity minted at claim time, BEFORE the turn is
    # admitted. Deliberately NOT read by the route's replay predicate, which
    # inspects ``stream_id`` only — a pending claim with a provisional
    # identity must still flow to ``_start_run`` instead of short-circuiting
    # as a replay of an identity no turn has accepted yet. The provisional
    # id is NOT replayable: it was never accepted by a turn, so a pending
    # record recovered from disk with no proven admitted stream is
    # outcome-unknown regardless of what ``pending_stream_id`` holds (see
    # ``_load_locked`` / ``sweep_pending_claims``).
    pending_stream_id: str = ""
    pending_turn_id: str = ""
    # #7782 finding 1: the pid that claimed this record. An in-flight guard
    # is a PROCESS-LOCAL guarantee (a second request in the same process
    # races the first for the same turn), so a pending record whose pid
    # matches ours means "a live sibling request owns this" → in-flight 409.
    # A different pid means the original process died before admitting the
    # turn; the pending record is then a durable artifact to replay, not a
    # live competitor. Without the pid the two cases are indistinguishable
    # and either replay (duplicate turns) or in-flight (a day-long 409) is
    # wrong for one of them.
    owner_pid: int = 0
    response_status: int = 0
    response_payload: dict[str, Any] = field(default_factory=dict)
    claimed_at: float = 0.0
    completed_at: float = 0.0
    # #7782 finding 1: set at LOAD time (``_load_locked``, with
    # ``sweep_pending_claims`` as belt-and-braces) when a pending record was
    # recovered from disk with no bound stream identity — the durable store
    # cannot say whether the turn ever ran. Such a record must NOT keep
    # answering ``idempotency_in_flight`` with a 1-second retry hint for the
    # rest of its TTL, and must never promote its provisional id into a
    # fabricated replay.
    outcome_unknown: bool = False

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
            pending_stream_id=str(raw.get("pending_stream_id") or ""),
            pending_turn_id=str(raw.get("pending_turn_id") or ""),
            owner_pid=int(raw.get("owner_pid") or 0),
            outcome_unknown=bool(raw.get("outcome_unknown") or False),
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
    normalized: dict[str, Any] = {
        "__profile": _canonical_profile_namespace(profile) or "default"
    }
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


def _canonical_profile_namespace(profile: str | None) -> str:
    """Canonical namespace for a profile: root aliases collapse to 'default'.

    Hermes Agent allows the root profile (~/.hermes) to carry a display name
    other than the literal ``'default'`` (a legacy rename). ``resolve_active
    _profile()`` then returns that display name, while a direct lookup keyed
    on the legacy literal — or any other code path that resolved the same
    root profile earlier under its old name — would produce a DIFFERENT
    namespace for the SAME profile. Two records for one real profile means
    the guard silently splits and a retry is admitted as a fresh turn
    (#7782 finding 2).

    ``api.profiles._is_root_profile`` is the canonical "does this name
    resolve to ~/.hermes?" test; when it answers yes the namespace is the
    stable ``'default'``. A miss (name unavailable, lookup failure) keeps the
    supplied name so the record still lands in a concrete namespace rather
    than a guessed one.
    """
    ns = str(profile or "").strip()
    if not ns or ns == "default":
        return "default"
    try:
        from api.profiles import _is_root_profile

        if _is_root_profile(ns):
            return "default"
    except Exception:
        # Lookup unavailable: keep the concrete name. A wrong-but-concrete
        # namespace isolates a record (at worst it is unreachable), whereas
        # collapsing every name to 'default' on a failed lookup would make
        # distinct profiles share one guard.
        pass
    return ns


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
    return f"{_canonical_profile_namespace(ns)}{_NAMESPACE_SEP}{raw_key}"


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
        tombstone_max_records: int = TOMBSTONE_MAX_RECORDS,
    ):
        self._path = Path(path) if path is not None else _store_path()
        self._ttl_seconds = float(ttl_seconds)
        self._max_records = int(max_records)
        self._tombstone_max_records = int(tombstone_max_records)
        self._lock = threading.RLock()
        # OrderedDict gives O(1) lookup + deterministic oldest-first
        # eviction order (insertion order). We re-insert on access so
        # "active" keys float to the end of the LRU.
        self._records: "OrderedDict[str, IdempotencyRecord]" = OrderedDict()
        # Keys whose record completed and then expired/evicted (#7782
        # contract call 5). An OrderedSet-with-payload (dict used as an
        # ordered map key → tombstone origin status, see
        # ``_TOMBSTONE_ORIGIN_*``) so the bound can evict oldest-first and
        # the 410 refusal can state whether the turn ever completed; kept
        # OUTSIDE the record cap so a full store cannot silently re-admit
        # an already-run turn.
        self._expired_keys: "OrderedDict[str, str]" = OrderedDict()
        # Write-ahead log state: the next op sequence number, the number
        # of un-compacted ops currently in the WAL (drives compaction), and
        # the live-state size (records + tombstones) captured at the last
        # compaction — the compaction threshold is 2x that baseline, so the
        # WAL stays bounded and the amortized compaction cost is O(1) per
        # mutation. A growing-store comparison against the CURRENT live
        # size never re-triggers (the WAL can never catch a moving target).
        self._seq = 0
        self._wal_lines = 0
        self._wal_compaction_baseline = 0
        self._loaded = False

    @property
    def _wal_path(self) -> Path:
        return self._path.with_name(self._path.name + _WAL_EXT)

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
        self._expired_keys.clear()
        # The in-memory sequence must reset too: a reload/build must not
        # skip WAL ops because the previous in-memory generation had already
        # consumed their seq values.
        self._seq = 0
        path = self._path
        snapshot_seq = 0
        if path.exists():
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
                    self._loaded = False
                    raise IdempotencyStoreUnavailable(
                        f"idempotency store {path} entry #{index} is not an object"
                    )
                try:
                    rec = IdempotencyRecord.from_json(entry)
                except Exception as exc:
                    self._loaded = False
                    raise IdempotencyStoreUnavailable(
                        f"idempotency store {path} entry #{index} is malformed: {exc}"
                    ) from exc
                self._validate_loaded_record_locked(rec, f"{path} entry #{index}")
                self._records[rec.key] = rec
                if rec.status == STATUS_PENDING and not rec.stream_id:
                    # #7782 finding 1: a pending record loaded from disk with no
                    # proven admitted stream — the crash window between the claim
                    # and the turn being admitted, regardless of whether a
                    # provisional identity was minted at claim time. The durable
                    # store cannot say whether the turn ran, so the record must
                    # fail closed as outcome-unknown: NOT replay the provisional
                    # id (no turn ever accepted it), NOT answer in-flight +
                    # retry_after=1 for the rest of the TTL. Marking at LOAD
                    # makes it independent of pid and provisional data; newly
                    # claimed records are never marked.
                    rec.outcome_unknown = True
            snapshot_seq = int(data.get("seq") or 0) if isinstance(data, dict) else 0
            # #7782 call 5: restore tombstones. A v1 file has none; that is
            # fine, those keys simply have no memory of completion. v2 files
            # store bare keys (origin unknown); v3 stores ``[key, origin]``
            # pairs so the 410 can state whether the turn ever completed.
            tombstones = data.get("expired_keys") if isinstance(data, dict) else None
            if tombstones is not None:
                if isinstance(tombstones, list):
                    for entry in tombstones:
                        if isinstance(entry, str) and entry:
                            self._expired_keys[entry] = _TOMBSTONE_ORIGIN_UNKNOWN
                        elif (
                            isinstance(entry, list)
                            and len(entry) == 2
                            and isinstance(entry[0], str)
                            and entry[0]
                            and entry[1] in _VALID_TOMBSTONE_ORIGINS
                        ):
                            self._expired_keys[entry[0]] = entry[1]
                        else:
                            self._loaded = False
                            raise IdempotencyStoreUnavailable(
                                f"idempotency store {path} has a malformed "
                                "tombstone entry"
                            )
                else:
                    self._loaded = False
                    raise IdempotencyStoreUnavailable(
                        f"idempotency store {path} has an unexpected "
                        "expired_keys shape"
                    )
        self._seq = max(self._seq, snapshot_seq)
        # The WAL is the tail of the durable state: ops with seq above the
        # snapshot watermark are replayed on top of it.
        wal = self._wal_path
        if wal.exists():
            self._replay_wal_locked(wal)
        self._wal_compaction_baseline = len(self._records) + len(self._expired_keys)
        self._loaded = True

    def _validate_loaded_record_locked(
        self, rec: IdempotencyRecord, where: str
    ) -> None:
        """Validate a record read from the durable state (snapshot or WAL).

        The durable key is the namespaced form; a stripped key, missing
        profile, or unknown status means we cannot prove which claim the
        entry belongs to. Raises ``IdempotencyStoreUnavailable``.
        """
        if _NAMESPACE_SEP not in rec.key:
            raise IdempotencyStoreUnavailable(
                f"idempotency store {where} is missing its profile namespace"
            )
        profile, _, raw_key = rec.key.partition(_NAMESPACE_SEP)
        if not raw_key or not rec.request_fingerprint:
            raise IdempotencyStoreUnavailable(
                f"idempotency store {where} is missing key or fingerprint"
            )
        if rec.status not in (STATUS_PENDING, STATUS_COMPLETE):
            raise IdempotencyStoreUnavailable(
                f"idempotency store {where} has unknown status {rec.status!r}"
            )
        if not rec.profile or rec.profile != profile:
            raise IdempotencyStoreUnavailable(
                f"idempotency store {where} has a profile that disagrees "
                "with its namespaced key"
            )

    def _repair_wal_locked(self, path: Path, size: int, why: str) -> None:
        """Truncate the WAL to ``size`` bytes through ONE descriptor.

        The only WAL repair primitive (#7782 review round 2): it removes
        just the uncommitted / invalid suffix and never rewrites the
        acknowledged prefix, so an I/O error mid-repair cannot lose an
        acknowledged op — the old ``Path.write_text`` repair opened the
        file with truncation, and an ENOSPC during that rewrite destroyed
        the very prefix it was meant to keep. The truncation is fsync'd
        before returning (recovery must be durable before the store
        admits more work), and ANY failure raises
        ``IdempotencyStoreUnavailable`` with the store left unloaded, so
        admission stops instead of continuing on in-memory-only state.

        Must be called with ``self._lock`` held.
        """
        try:
            fd = os.open(path, os.O_RDWR)
        except OSError as exc:
            self._loaded = False
            raise IdempotencyStoreUnavailable(
                f"could not open idempotency WAL {path} for repair "
                f"({why}): {exc}"
            ) from exc
        try:
            os.ftruncate(fd, size)
            try:
                os.fsync(fd)
            except OSError as exc:
                # Same tolerance as the append / snapshot paths: only the
                # two "this filesystem cannot fsync" errors are tolerable
                # (tmpfs / overlayfs). A REAL I/O error means the repair
                # may never reach the platter, so the store fails closed
                # rather than admitting work against a WAL whose framing
                # it cannot prove.
                if exc.errno not in {errno.EINVAL, errno.ENOTSUP}:
                    raise
        except OSError as exc:
            self._loaded = False
            raise IdempotencyStoreUnavailable(
                f"could not repair idempotency WAL {path} ({why}): {exc}"
            ) from exc
        finally:
            try:
                os.close(fd)
            except OSError:
                pass

    def _replay_wal_locked(self, path: Path) -> None:
        """Replay the write-ahead log on top of the loaded snapshot.

        Ops whose ``seq`` is at or below the snapshot watermark are no-ops
        (they are already in the snapshot — e.g. after a compaction whose
        WAL truncate failed).

        Recovery is conservative about the EOF framing: every acknowledged
        op is written as ONE line ending in ``"\\n"`` and fsync'd before
        the mutation returns (see ``_append_wal_locked``), so bytes after
        the last newline belong to an append that was never acknowledged —
        a torn write, or a power loss that ate the tail after fsync
        returned. A tail that happens to be complete JSON is no exception:
        replaying it would resurrect an op the store never acknowledged,
        and leaving the file un-terminated would make the NEXT append
        concatenate mid-line, turning the following restart into mid-file
        corruption (a permanent 503) or a torn-tail truncation that
        destroys acknowledged ops. The uncommitted suffix is therefore
        dropped durably — through ``_repair_wal_locked`` — before the
        committed lines are parsed. A malformed line with only a torn tail
        after it is repaired the same way; a malformed line FOLLOWED by
        valid lines is mid-file corruption and fails closed. Any repair
        failure keeps the store unloaded (``IdempotencyStoreUnavailable``)
        and admits zero new turns.

        Both truncation boundaries are byte lengths of the file's
        ORIGINAL on-disk bytes, so the WAL is read as bytes and decoded
        without newline translation: a producer on Windows writes CRLF
        terminators, and a normalizing read would shrink every line by
        one byte and cut the acknowledged prefix instead of the
        unacknowledged tail.
        """
        try:
            data = path.read_bytes()
            # Decode the ON-DISK bytes verbatim — NO universal-newline
            # translation. ``_append_wal_locked`` opens the WAL in
            # ordinary text append mode without explicit newline control,
            # so its "\n" is CRLF on Windows, and both repair boundaries
            # below are computed as byte lengths of this string. Reading
            # through ``Path.read_text`` (or any newline-translating read)
            # would turn every CRLF into one shorter LF line and hand
            # ``os.ftruncate`` a boundary INSIDE the acknowledged prefix
            # (#7782 round 3): the admitted completion lost its
            # terminator, the next cold load dropped it as an
            # uncommitted suffix, and a same-key retry answered 409
            # ``idempotency_outcome_unknown`` instead of replaying the
            # accepted 200 identity. Keeping the bytes verbatim also
            # makes the torn-line offset reconstruction below
            # byte-exact, since a decoded CRLF line still carries its
            # "\r" and rejoining with "\n" reproduces the original bytes.
            raw = data.decode("utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            self._loaded = False
            raise IdempotencyStoreUnavailable(
                f"could not read idempotency WAL {path}: {exc}"
            ) from exc
        if not raw.strip():
            return
        # Uncommitted suffix: everything after the last newline. Drop it
        # before anything else touches the WAL, so no later append can
        # concatenate onto an un-terminated line (#7782 review round 2).
        boundary = raw.rfind("\n") + 1
        if boundary < len(raw):
            prefix_bytes = len(raw[:boundary].encode("utf-8"))
            self._repair_wal_locked(
                path, prefix_bytes, "uncommitted tail after the last newline"
            )
            logger.warning(
                "idempotency: dropped %d uncommitted WAL bytes at %s "
                "(the append was never acknowledged)",
                len(raw.encode("utf-8")) - prefix_bytes,
                path,
            )
            raw = raw[:boundary]
            if not raw.strip():
                return
        lines = raw.split("\n")
        entries: list[dict] = []
        invalid_index: int | None = None
        for idx, line in enumerate(lines):
            if not line.strip():
                continue
            try:
                op = json.loads(line)
                if not isinstance(op, dict):
                    raise ValueError("not an object")
                if op.get("op") not in _WAL_OP_TYPES or not isinstance(
                    op.get("seq"), int
                ):
                    raise ValueError("bad op header")
                if op["op"] == _WAL_OP_PUT and not isinstance(
                    op.get("record"), dict
                ):
                    raise ValueError("put op without a record")
                entries.append(op)
            except (json.JSONDecodeError, ValueError):
                invalid_index = idx
                break
        if invalid_index is not None:
            if any(l.strip() for l in lines[invalid_index + 1 :]):
                self._loaded = False
                raise IdempotencyStoreUnavailable(
                    f"idempotency WAL {path} is corrupt at line "
                    f"{invalid_index + 1}"
                )
            # Torn final line: repair ONLY the invalid suffix, at the byte
            # offset where that line starts, so the acknowledged prefix is
            # never rewritten (a whole-file rewrite could destroy it
            # mid-write, and the old swallowed exception then let
            # admission continue on in-memory-only state).
            offset = len(
                "".join(f"{line}\n" for line in lines[:invalid_index]).encode(
                    "utf-8"
                )
            )
            self._repair_wal_locked(path, offset, "torn tail")
            logger.warning(
                "idempotency: truncated torn WAL tail at %s (line %d)",
                path,
                invalid_index + 1,
            )
        for op in entries:
            seq = int(op["seq"])
            if seq <= self._seq:
                # Already reflected in the snapshot watermark.
                continue
            key = str(op.get("key") or "")
            if _NAMESPACE_SEP not in key:
                self._loaded = False
                raise IdempotencyStoreUnavailable(
                    f"idempotency WAL {path} op seq {seq} has an "
                    "unnamespaced key"
                )
            op_type = op["op"]
            if op_type == _WAL_OP_PUT:
                try:
                    rec = IdempotencyRecord.from_json(op.get("record") or {})
                except Exception as exc:
                    self._loaded = False
                    raise IdempotencyStoreUnavailable(
                        f"idempotency WAL {path} op seq {seq} is malformed: {exc}"
                    ) from exc
                self._validate_loaded_record_locked(rec, f"{path} op seq {seq}")
                self._records[rec.key] = rec
                if rec.status == STATUS_PENDING and not rec.stream_id:
                    # Same load-time crash-window marking as the snapshot.
                    rec.outcome_unknown = True
            elif op_type == _WAL_OP_DEL:
                self._records.pop(key, None)
            else:  # _WAL_OP_EXPIRE
                self._records.pop(key, None)
                origin = str(op.get("origin") or _TOMBSTONE_ORIGIN_UNKNOWN)
                if origin not in _VALID_TOMBSTONE_ORIGINS:
                    self._loaded = False
                    raise IdempotencyStoreUnavailable(
                        f"idempotency WAL {path} op seq {seq} has an unknown "
                        f"tombstone origin {origin!r}"
                    )
                self._expired_keys[key] = origin
            self._seq = max(self._seq, seq)
        self._wal_lines = len(entries)

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        with self._lock:
            if self._loaded:
                return
            self._load_locked()

    def _evict_expired_locked(self) -> list[tuple[str, IdempotencyRecord]]:
        """Drop records past their TTL; must be called with the lock held.

        Returns the ``(stored_key, record)`` pairs evicted so the caller can
        persist the tombstones this sweep minted BEFORE honouring them, and
        roll the sweep back if that persist fails (#7782 re-gate review: a
        tombstone that never reached disk must not be honoured, or a
        restart forgets the refusal and re-admits an already-run turn).
        """
        if self._ttl_seconds <= 0:
            return []
        cutoff = time.time() - self._ttl_seconds
        # Sweep the OrderedDict once. Collect keys first to avoid mutating
        # during iteration.
        expired = [
            k for k, r in self._records.items()
            # ``claimed_at`` of 0 is the 1970 epoch — the dataclass default —
            # and must be treated as expired, matching the same test in
            # ``claim``. ``r.claimed_at and …`` would skip it (0 is falsy) and
            # strand an identity-less record until the cap sweep.
            if r.claimed_at < cutoff
        ]
        evicted: list[tuple[str, IdempotencyRecord]] = []
        for k in expired:
            rec = self._records.pop(k, None)
            if rec is None:
                continue
            evicted.append((k, rec))
            # The turn may have run (COMPLETE), or the claim was left
            # un-admitted when the process died (PENDING, #7782 finding 1's
            # crash window). Either way the KEY was used, so a later re-claim
            # must fail explicitly rather than silently admit a turn. Remember
            # it either way — pending records are exactly the case where a
            # blind re-admission is most dangerous, because a turn may have
            # been in flight when the claim was written. The origin status is
            # persisted with the tombstone so the 410 says the truth.
            self._remember_expired_key_locked(k, rec.status)
        return evicted

    def _remember_expired_key_locked(
        self, stored_key: str, origin: str = _TOMBSTONE_ORIGIN_UNKNOWN
    ) -> None:
        """Record that ``stored_key`` expired, and why (#7782 call 5).

        ``origin`` is the status the evicted record had (``complete`` /
        ``pending`` / legacy ``unknown``) — persisted so the 410 refusal can
        distinguish \"the turn ran\" from \"the turn may never have run\".

        Bounded by ``_TOMBSTONE_MAX`` with oldest-first eviction, so a
        pathological caller cannot grow the set without limit. When the
        bound is exceeded the OLDEST tombstone is dropped — the key becomes
        re-admittable, which is the documented cost of keeping tombstones
        outside the record cap (a cap-sized eviction would instead stall
        every new key with 503).
        """
        if stored_key in self._expired_keys:
            # Refresh recency so a repeatedly-retried key is not the first
            # tombstone evicted. The origin stays the FIRST record's status
            # (the record is gone; nothing can change it).
            del self._expired_keys[stored_key]
            self._expired_keys[stored_key] = origin
            return
        self._expired_keys[stored_key] = origin
        while len(self._expired_keys) > self._tombstone_max_records:
            self._expired_keys.popitem(last=False)

    def _evict_to_cap_locked(self) -> list[tuple[str, IdempotencyRecord]]:
        """Bound the in-memory map by the configured cap.

        Only drops records whose ``claimed_at`` is past TTL; an
        unexpired record is NEVER evicted under a cap. Pending
        records (status == ``STATUS_PENDING``) are also never
        evicted — they hold an in-flight claim that the caller
        will need to replay. If the cap is still exceeded after
        every expired record has been dropped, the store is full
        and the call must fail closed (the route will surface a
        503 and the caller can retry with a different key or
        after the next expiry sweep) — and nothing is popped when
        the raise fires, so the caller has no partial eviction to
        roll back.

        Returns the ``(stored_key, record)`` pairs dropped so the caller
        can persist any freshly-minted tombstones atomically with its own
        mutation and roll them back on a durable-write failure.
        """
        if self._max_records <= 0:
            return []
        if len(self._records) <= self._max_records:
            return []
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
        if len(self._records) - len(evictable) > self._max_records:
            raise IdempotencyStoreUnavailable(
                f"idempotency store at cap ({self._max_records}); "
                f"refusing to evict unexpired records"
            )
        evicted: list[tuple[str, IdempotencyRecord]] = []
        for k in evictable:
            rec = self._records.pop(k, None)
            if rec is None:
                continue
            evicted.append((k, rec))
            if rec.status == STATUS_COMPLETE:
                # A capacity-swept COMPLETE record still ran its turn; keep
                # the tombstone so a later claim of the same key fails
                # explicitly (#7782 contract call 5).
                self._remember_expired_key_locked(k, _TOMBSTONE_ORIGIN_COMPLETE)
        return evicted

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

        Since the #7782 re-gate this is the COMPACTION writer (see
        ``_append_wal_locked`` for the per-mutation durability path): it
        rewrites the whole snapshot and truncates the WAL, and is only run
        when the WAL crosses its compaction threshold (or explicitly via
        ``compact()`` / tests), so a large store no longer pays a full
        rewrite per claim/completion.
        """
        path = self._path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise IdempotencyStoreUnavailable(
                f"could not create store dir {path.parent}: {exc}"
            ) from exc
        payload = {
            "version": 3,
            "saved_at": time.time(),
            # WAL watermark: ops with seq <= this are already in the
            # snapshot and replayed as no-ops after a restart.
            "seq": self._seq,
            "records": [r.to_json() for r in self._records.values()],
            # #7782 call 5: keys whose record completed and then expired.
            # Persisted as [key, origin] pairs so the 410 can state whether
            # the turn ever completed; persisted so the protection survives
            # a restart.
            "expired_keys": [[k, origin] for k, origin in self._expired_keys.items()],
        }
        tmp_path = path.with_name(path.name + ".tmp")
        try:
            with open(tmp_path, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False, separators=(",", ":"))
                fh.flush()
                try:
                    os.fsync(fh.fileno())
                except OSError as exc:
                    # Repo convention (api.paths._fsync_directory): only the
                    # two "this filesystem cannot fsync" errors are tolerable.
                    # A REAL I/O error (EIO and friends) means the bytes may
                    # never reach the platter, so the rename below would put a
                    # half-written document on the durable path while the
                    # route answered 200 claiming idempotent replay — the
                    # worst possible outcome for this feature (#7782 finding 3).
                    # Re-raise so it surfaces as IdempotencyStoreUnavailable
                    # (503) and the caller retries.
                    if exc.errno not in {errno.EINVAL, errno.ENOTSUP}:
                        raise
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
        # Make the rename durable: fsync the parent directory so the
        # directory entry (and the new inode it points to) is on disk
        # before we answer 200. Repo convention (``api.paths._fsync_directory``):
        # only the two \"this filesystem cannot fsync\" errors (EINVAL /
        # ENOTSUP) are tolerable — a REAL I/O error (EIO and friends) means
        # the rename may never survive a power loss, so the durable path
        # cannot be trusted and the store fails closed (503) rather than
        # answering 200 for a claim that could vanish (#7782 finding 3).
        try:
            _fsync_directory(path.parent)
        except OSError as exc:
            raise IdempotencyStoreUnavailable(
                f"could not fsync store directory {path.parent}: {exc}"
            ) from exc
        # The snapshot is durable and carries every op up to ``self._seq``:
        # truncate the WAL so a restart replays nothing stale. A failure
        # here is NOT fatal — the seq watermark turns the leftover WAL into
        # a sequence of no-ops on load — so compaction never fails an
        # already-durable operation on account of bookkeeping.
        try:
            with open(self._wal_path, "w", encoding="utf-8") as fh:
                fh.flush()
                try:
                    os.fsync(fh.fileno())
                except OSError as exc:
                    if exc.errno not in {errno.EINVAL, errno.ENOTSUP}:
                        raise
        except OSError as exc:
            logger.warning(
                "idempotency: could not truncate WAL after compaction: %s", exc
            )
        self._wal_lines = 0
        self._wal_compaction_baseline = len(self._records) + len(self._expired_keys)

    def _append_wal_locked(self, ops: list[dict]) -> None:
        """Append ``ops`` to the write-ahead log and make them durable.

        Must be called with the lock held. This is the per-mutation
        durability boundary for claim / complete / reconcile / release /
        sweep: each op becomes one JSON line carrying a monotonic ``seq``,
        the batch is fsync'd before returning, and a failure raises
        ``IdempotencyStoreUnavailable`` AFTER the file has been rolled back
        to its pre-append length — so a failed append leaves no phantom
        line that a clean restart would replay as a committed claim (the
        in-memory rollback stays the caller's job). Tolerates EINVAL /
        ENOTSUP on the file fsync (tmpfs/overlayfs) and fsyncs the parent
        directory on first creation so the WAL's directory entry survives a
        power loss before its first acknowledged op can be trusted.
        """
        if not ops:
            return
        path = self._wal_path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise IdempotencyStoreUnavailable(
                f"could not create store dir {path.parent}: {exc}"
            ) from exc
        created = not path.exists()
        start_size = 0
        try:
            start_size = path.stat().st_size if path.exists() else 0
        except OSError:
            pass
        fh = None
        try:
            fh = open(path, "a", encoding="utf-8")
            for op in ops:
                self._seq += 1
                op["seq"] = self._seq
                fh.write(json.dumps(op, ensure_ascii=False, separators=(",", ":")) + "\n")
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except OSError as exc:
                # Same tolerance as the snapshot path: only the two "this
                # filesystem cannot fsync" errors are tolerable. A REAL
                # I/O error (EIO and friends) means the bytes may never
                # reach the platter — the mutation must fail closed (#7782
                # finding 3).
                if exc.errno not in {errno.EINVAL, errno.ENOTSUP}:
                    raise
            fh.close()
            fh = None
        except OSError as exc:
            if fh is not None:
                try:
                    fh.close()
                except OSError:
                    pass
            # Roll the file back to its pre-append length so a failed
            # append leaves no phantom line for a clean restart to replay
            # (the data may already be in the page cache even though the
            # fsync never acknowledged it — truncate regardless of whether
            # the file was empty before).
            try:
                if created:
                    path.unlink()
                else:
                    os.truncate(path, start_size)
            except OSError:
                pass
            raise IdempotencyStoreUnavailable(
                f"failed to append to idempotency store {path}: {exc}"
            ) from exc
        self._wal_lines += len(ops)
        if created:
            # First appearance of the WAL file: its directory entry must be
            # durable before any acknowledged op can survive a power loss.
            try:
                _fsync_directory(path.parent)
            except OSError as exc:
                # Best-effort rollback of the file itself, then fail closed
                # (a real I/O error — EINVAL/ENOTSUP never escapes
                # _fsync_directory).
                try:
                    path.unlink()
                except OSError:
                    pass
                self._wal_lines = max(0, self._wal_lines - len(ops))
                raise IdempotencyStoreUnavailable(
                    f"could not fsync store directory {path.parent}: {exc}"
                ) from exc

    def _maybe_compact_locked(self) -> None:
        """Compact the WAL into a fresh snapshot once it crosses the
        threshold. Must be called with the lock held, after a successful
        append.

        The threshold is 2x the live-state size captured at the LAST
        compaction (``_wal_compaction_baseline``), so the WAL stays bounded
        and the amortized compaction cost is O(1) per mutation: each
        compaction rewrites N records after 2N WAL ops have accumulated. A
        compaction failure is logged, not raised: the WAL append already
        made the mutation durable, and the next mutation retries the
        compaction.
        """
        threshold = max(
            _WAL_COMPACT_MIN_OPS, 2 * self._wal_compaction_baseline
        )
        if self._wal_lines < threshold:
            return
        try:
            self._persist_locked()
        except IdempotencyStoreUnavailable:
            logger.warning(
                "idempotency: could not compact the store; the WAL remains "
                "the durable state and the next mutation retries",
                exc_info=True,
            )

    # -- public api ----------------------------------------------------------

    def claim(
        self,
        key: str,
        fingerprint: str,
        *,
        profile: str | None = None,
        pending_identity: tuple[str, str] | None = None,
    ) -> IdempotencyRecord:
        """Atomically claim ``key`` for ``fingerprint``.

        The supplied ``key`` is a validated raw key (printable ASCII,
        length-bounded, non-empty) — this method namespaces it with
        the server-resolved active profile before any lookup or
        write. The fingerprint MUST already include the same profile
        (see ``compute_request_fingerprint``) so two profiles
        sending the same key produce different fingerprints and
        are isolated.

        ``profile`` pins the namespace once for a request lifecycle so
        the same profile is used here, by ``complete()`` and by
        ``release()`` even if the server's active profile changes mid
        request; when omitted it is resolved fresh via
        ``resolve_active_profile()``.

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
        stored_key = build_storage_key(key, profile=profile)
        profile = stored_key.split(_NAMESPACE_SEP, 1)[0]
        with self._lock:
            # Expire first so a tombstone is raised before the lookup below.
            # Any tombstone this sweep just minted must reach disk BEFORE we
            # can honour it as a refusal, otherwise a restart between the
            # sweep and the next successful write forgets the key and
            # re-admits the turn. When the tombstone write FAILS the sweep
            # is rolled back and the claim fails closed (503) — honouring an
            # in-memory-only tombstone with a 410 the disk cannot back up is
            # the same restart gap (#7782 re-gate review).
            _tombstones_before = set(self._expired_keys)
            evicted = self._evict_expired_locked()
            minted = [
                (k, self._expired_keys[k])
                for k, _ in evicted
                if k not in _tombstones_before
            ]
            if minted:
                try:
                    self._append_wal_locked(
                        [
                            {"op": _WAL_OP_EXPIRE, "key": k, "origin": origin}
                            for k, origin in minted
                        ]
                    )
                except IdempotencyStoreUnavailable:
                    # Roll the sweep back so memory == disk: the refusal is
                    # not durable, so neither the eviction nor the tombstone
                    # may survive in memory.
                    for k, rec in evicted:
                        self._records[k] = rec
                    for k, _ in minted:
                        self._expired_keys.pop(k, None)
                    raise
                _tombstones_before.update(k for k, _ in minted)
            existing = self._records.get(stored_key)
            # #7782 contract call 5: a key whose record completed and then
            # EXPIRED leaves the map but stays remembered in
            # ``_expired_keys``. The tombstone check must run whether or not
            # a record row survives — an expired row is exactly when the
            # claim would otherwise silently re-admit a duplicate turn.
            if stored_key in self._expired_keys:
                raise IdempotencyKeyExpired(
                    self._tombstone_refusal_message(
                        key, self._expired_keys[stored_key]
                    )
                )
            if existing is None:
                rec = IdempotencyRecord(
                    key=stored_key,
                    request_fingerprint=fingerprint,
                    status=STATUS_PENDING,
                    profile=profile,
                    # #7782 finding 1: the identity the caller minted before
                    # the turn was admitted, persisted in the SAME write as the
                    # claim. ``pending_*`` on purpose — the replay predicate
                    # below reads ``stream_id`` only, so a fresh claim still
                    # flows to ``_start_run``; a crash in the claim→admit
                    # window leaves this durable identity behind for a retry.
                    pending_stream_id=(pending_identity[0] if pending_identity else ""),
                    pending_turn_id=(pending_identity[1] if pending_identity else ""),
                    owner_pid=os.getpid(),
                    claimed_at=time.time(),
                )
                self._records[stored_key] = rec
                # Move to the end of the LRU ordering.
                self._records.move_to_end(stored_key)
                # The claim and any cap-eviction tombstones are persisted in
                # ONE WAL batch, so a restart cannot see one without the
                # other.
                ops: list[dict] = [
                    {"op": _WAL_OP_PUT, "key": stored_key, "record": rec.to_json()}
                ]
                try:
                    cap_evicted = self._evict_to_cap_locked()
                except IdempotencyStoreUnavailable:
                    self._records.pop(stored_key, None)
                    raise
                for ck, crec in cap_evicted:
                    ops.append(
                        {"op": _WAL_OP_EXPIRE, "key": ck, "origin": crec.status}
                    )
                try:
                    self._append_wal_locked(ops)
                except IdempotencyStoreUnavailable:
                    # Roll back the in-memory mutation: the durable
                    # store never saw the record, so neither should
                    # the caller. Without this rollback a transient
                    # write failure would leave a phantom claim
                    # that the next process restart would resurrect
                    # from disk (or fail to, depending on whether
                    # the file write raced with the rename).
                    self._records.pop(stored_key, None)
                    for ck, crec in cap_evicted:
                        self._records[ck] = crec
                    for ck, _ in cap_evicted:
                        if ck not in _tombstones_before:
                            self._expired_keys.pop(ck, None)
                    raise
                self._maybe_compact_locked()
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
                if existing.stream_id:
                    # A turn was admitted and its identity reconciled onto
                    # this pending claim (see ``reconcile``) — the
                    # completion write either failed or has not landed, but
                    # the same logical turn is already known to be running /
                    # started. Treat this as a replay of that identity, not
                    # an in-flight duplicate: the route returns the original
                    # identity and no second turn is admitted.
                    return existing
                if existing.outcome_unknown:
                    # Recovered from a crash between the claim and the
                    # identity binding. The turn MAY have run; refusing with
                    # a retry hint for the remaining TTL would have the
                    # operator polling for a day (#7782 finding 1). Raise the
                    # distinct "do not blind-retry" outcome instead. This
                    # check runs BEFORE the pid comparison on purpose: the
                    # mark is load-time (see ``_load_locked``) and must win
                    # whether the recorded owner_pid equals ours (the common
                    # same-PID restart) or belongs to a dead process. There
                    # is deliberately NO provisional-id promotion here: the
                    # minted ``pending_stream_id`` was never accepted by a
                    # turn, so replaying it would hand the caller a
                    # fabricated stream that 404s.
                    raise IdempotencyOutcomeUnknown(
                        f"idempotency key {key!r} has a pending claim with no "
                        "recorded outcome; the previous attempt may have run "
                        "— check the session before retrying, or use a new key"
                    )
                # Same process still owns an un-admitted claim: a sibling
                # request is mid-start, so this is an in-flight duplicate
                # (409 + retry hint), NOT a replay. Without this check the
                # provisional identity would make a concurrent retry look
                # like a replay of a turn that has not started.
                if (
                    existing.owner_pid
                    and existing.owner_pid == os.getpid()
                ):
                    raise IdempotencyInFlight(
                        f"idempotency key {key!r} is currently in flight"
                    )
                raise IdempotencyInFlight(
                    f"idempotency key {key!r} is currently in flight"
                )
            # Complete: caller should replay the stored result.
            return existing

    def claim_with_identity(
        self,
        key: str,
        fingerprint: str,
        *,
        profile: str | None = None,
        session_id: str,
        stream_id: str,
        turn_id: str,
    ) -> IdempotencyRecord:
        """Claim ``key`` and durably bind the minted turn identity up front.

        ``claim`` alone leaves a pending record with an EMPTY stream_id until
        ``_start_run`` has admitted the turn and ``reconcile`` runs. A crash
        in that window (process death, host power loss) leaves the record on
        disk as ``STATUS_PENDING`` with no identity — a restarted process then
        answers every retry for that key with ``409 idempotency_in_flight`` +
        ``retry_after_seconds: 1`` for the remaining TTL, and the operator
        cannot tell "ran" from "never ran" (#7782 finding 1, the feature's
        main use case).

        The route mints a PROVISIONAL identity and persists it WITH the
        claim. That id is NOT replayable: no turn ever accepted it (``_start_run``
        mints the real stream id and only ``complete`` / ``reconcile`` bind a
        proven admission), so a pending record recovered from disk with no
        proven admitted stream fails closed as ``IdempotencyOutcomeUnknown``
        (409 without a retry hint) instead of a day-long in-flight error or a
        fabricated replay of a stream that 404s.

        The minted identity is stored in ``pending_stream_id`` /
        ``pending_turn_id``, NOT ``stream_id`` / ``turn_id``. That split is
        load-bearing: the route's replay predicate (and the
        "was a turn admitted" test) reads ``stream_id`` only, so a
        freshly-claimed record — provisional identity bound, no turn admitted
        yet — must still flow into ``_start_run``. Writing the provisional id
        into ``stream_id`` would make the very first request look like a
        replay of itself and short-circuit the turn (#7782 CORE: zero turns
        would ever start).

        ``complete`` / ``reconcile`` keep writing the real admitted identity
        into ``stream_id`` / ``turn_id``; ``release`` drops the record
        entirely, provisional identity included, so a refused start leaks
        nothing.
        """
        return self.claim(
            key,
            fingerprint,
            profile=profile,
            pending_identity=(stream_id, turn_id),
        )

    def sweep_pending_claims(self) -> int:
        """Re-mark identity-less pending claims loaded from disk.

        A pending record with no stream_id can only exist because a process
        died between claiming and binding the admitted identity (older
        builds), or between binding and starting the turn. In both cases the
        durable store cannot say whether a turn ran, so the record must not
        keep answering ``idempotency_in_flight`` with a 1-second retry hint
        for the rest of its TTL.

        The sweep is intentionally conservative: it does NOT delete anything
        and does NOT invent an identity. It stamps the record so future
        ``claim`` calls raise a distinct, non-retryable
        ``IdempotencyOutcomeUnknown`` (mapped to 409 without
        ``retry_after_seconds``), which tells the caller "a previous attempt
        may have run; check the session / pick a new key" instead of "retry in
        one second". The identity-less mark is applied at LOAD time in
        ``_load_locked``; this sweep is belt-and-braces for records that
        reach memory unmarked (e.g. a store loaded before the load-time
        marking shipped). Returns the number of records swept.
        """
        self._ensure_loaded()
        swept = 0
        with self._lock:
            ops: list[dict] = []
            for record in self._records.values():
                if record.status != STATUS_PENDING:
                    continue
                if record.stream_id:
                    # A proven admitted stream was reconciled onto this
                    # pending claim — ``claim`` replays it, no sweep needed.
                    # ``pending_stream_id`` (provisional, minted at claim
                    # time, never accepted by a turn) does NOT exempt a
                    # record: without a proven admission the outcome is
                    # unknowable and must fail closed (#7782 finding 1).
                    continue
                if record.outcome_unknown:
                    continue
                record.outcome_unknown = True
                ops.append(
                    {"op": _WAL_OP_PUT, "key": record.key, "record": record.to_json()}
                )
                swept += 1
            if ops:
                try:
                    self._append_wal_locked(ops)
                    self._maybe_compact_locked()
                except IdempotencyStoreUnavailable:
                    # The sweep is observability hardening, not a correctness
                    # gate: the in-memory marks still apply for this process,
                    # and a failed persist must not block startup.
                    logger.warning(
                        "idempotency: could not persist pending-claim sweep",
                        exc_info=True,
                    )
        return swept

    def complete(
        self,
        key: str,
        *,
        profile: str | None = None,
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

        ``profile`` pins the namespace once per request; it MUST be
        the same value the route resolved at entry and passed to
        ``claim``/``release``/``reconcile``, so a profile switch mid
        request cannot scatter the lifecycle across namespaces. When
        omitted it is resolved fresh (callers outside the route may
        omit it).

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
        stored_key = build_storage_key(key, profile=profile)
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
            # rolled back to byte-for-byte the same record. ``replace()``
            # copies EVERY field — including the provisional identity
            # (``pending_stream_id`` / ``pending_turn_id``), ``owner_pid``
            # and the load-time ``outcome_unknown`` mark — so the rollback
            # restores exactly what the durable claim holds (#7782 re-gate
            # review: a hand-built snapshot used to drop those fields,
            # silently wiping them from memory on a failed completion).
            prior = replace(
                existing,
                response_payload=dict(existing.response_payload or {}),
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
            _tombstones_before = set(self._expired_keys)
            try:
                cap_evicted = self._evict_to_cap_locked()
            except IdempotencyStoreUnavailable:
                self._records[stored_key] = prior
                raise
            # The completion and any cap-eviction tombstones land in ONE
            # WAL batch so a restart cannot see one without the other.
            ops: list[dict] = [
                {"op": _WAL_OP_PUT, "key": stored_key, "record": rec.to_json()}
            ]
            for ck, crec in cap_evicted:
                ops.append(
                    {"op": _WAL_OP_EXPIRE, "key": ck, "origin": crec.status}
                )
            try:
                self._append_wal_locked(ops)
            except IdempotencyStoreUnavailable:
                # Roll back the in-memory mutation to the prior record
                # byte-for-byte, so a retry sees exactly the claim state
                # that the durable store can prove.
                self._records[stored_key] = prior
                for ck, crec in cap_evicted:
                    self._records[ck] = crec
                for ck, _ in cap_evicted:
                    if ck not in _tombstones_before:
                        self._expired_keys.pop(ck, None)
                raise
            self._maybe_compact_locked()
            return rec

    def reconcile(
        self,
        key: str,
        *,
        profile: str | None = None,
        session_id: str,
        stream_id: str,
        turn_id: str,
    ) -> IdempotencyRecord:
        """Durably bind the admitted stream/run identity onto a pending claim.

        This is called by the route when a turn has ALREADY been admitted
        (``_start_run`` returned a 2xx with a stream_id) but the completion
        write that would flip the claim to ``STATUS_COMPLETE`` failed. The
        agent turn cannot be undone, so the durable guard must carry the
        admitted identity; otherwise the only record of the turn is an
        identity-less ``STATUS_PENDING`` claim and the next retry with the
        same key would be refused as in-flight (or, worse, re-admitted as a
        fresh turn) instead of replaying the original acceptance.

        Effect: the stored record for ``key`` (same profile namespace as
        ``claim``) retains ``STATUS_PENDING`` but now also carries the
        reconciled ``session_id``/``stream_id``/``turn_id``. ``claim()``
        ``claim()`` treats a pending record that has a stream_id as a replay, so a
        retry returns the original identity without starting a second turn.
        Fail-closed: when the reconcile itself cannot be made durable, the
        in-memory identity fields are rolled back (the claim falls back to
        a plain pending record) and ``IdempotencyStoreUnavailable``
        propagates so the route can surface 503. The guard is preserved —
        never dropped — so the caller retries against an explicit refusal
        rather than silently re-admitting a duplicate turn.
        """
        self._ensure_loaded()
        stored_key = build_storage_key(key, profile=profile)
        with self._lock:
            existing = self._records.get(stored_key)
            if existing is None:
                raise IdempotencyStoreUnavailable(
                    f"cannot reconcile idempotency key {key!r}: no durable "
                    "claim exists to bind the admitted identity to"
                )
            # Snapshot the prior identity so a persist failure can undo it.
            prior_session, prior_stream, prior_turn = (
                existing.session_id, existing.stream_id, existing.turn_id,
            )
            existing.session_id = session_id
            existing.stream_id = stream_id
            existing.turn_id = turn_id
            self._records.move_to_end(stored_key)
            try:
                self._append_wal_locked(
                    [
                        {
                            "op": _WAL_OP_PUT,
                            "key": stored_key,
                            "record": existing.to_json(),
                        }
                    ]
                )
            except IdempotencyStoreUnavailable:
                # The identity could not be made durable. Roll it back so
                # memory and disk still agree; the pending guard remains.
                existing.session_id, existing.stream_id, existing.turn_id = (
                    prior_session, prior_stream, prior_turn,
                )
                raise
            self._maybe_compact_locked()
            return existing

    def release(self, key: str, *, profile: str | None = None) -> None:
        """Drop a pending claim. Used when validation fails before acceptance.

        A completed record is NEVER released — that's the whole point of
        idempotency: the original result must be replayable forever (up
        to TTL) so a retry after a lost response still gets identity.

        ``key`` is the validated raw key; the storage key is computed
        via ``build_storage_key`` so the same profile namespace is
        used as the one the original claim wrote under. Pass the same
        ``profile`` the claim used when the request has already
        resolved it (see ``claim``).

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
        stored_key = build_storage_key(key, profile=profile)
        with self._lock:
            existing = self._records.get(stored_key)
            if existing is None:
                return
            if existing.status == STATUS_PENDING:
                self._records.pop(stored_key, None)
                try:
                    self._append_wal_locked(
                        [{"op": _WAL_OP_DEL, "key": stored_key}]
                    )
                except IdempotencyStoreUnavailable:
                    # The drop is not durable. Restore the pending claim
                    # so memory and disk still agree, and refuse rather
                    # than losing the in-flight guard.
                    self._records[stored_key] = existing
                    raise
                self._maybe_compact_locked()

    def lookup(self, key: str, *, profile: str | None = None) -> IdempotencyRecord | None:
        """Return the stored record for ``key`` or ``None`` if absent / expired.

        ``key`` is the validated raw key; the same profile namespace
        is applied as for claim/complete. Does not raise; useful for
        diagnostics and tests.

        A diagnostic READ never mutates durable state: an expired record
        is reported absent without minting the in-memory tombstone that
        claim()'s sweep would (and persists) — the #7782 re-gate review
        found lookup() creating tombstones that never reached disk and
        disagreed with the file after a restart.
        """
        self._ensure_loaded()
        stored_key = build_storage_key(key, profile=profile)
        with self._lock:
            rec = self._records.get(stored_key)
            if rec is None or self._expired_locked(rec):
                return None
            self._records.move_to_end(stored_key)
            return rec

    def lookup_stored(self, stored_key: str) -> IdempotencyRecord | None:
        """Lookup using the FULL storage key (already profile-prefixed).

        Used by tests and diagnostics that already know the namespaced
        form. Production code should call ``lookup`` with the raw key.
        """
        self._ensure_loaded()
        with self._lock:
            rec = self._records.get(stored_key)
            if rec is None or self._expired_locked(rec):
                return None
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
            self._expired_keys.clear()
            self._seq = 0
            self._wal_lines = 0
            self._wal_compaction_baseline = 0
            self._loaded = True
            try:
                if self._path.exists():
                    self._path.unlink()
            except OSError:
                pass
            try:
                if self._wal_path.exists():
                    self._wal_path.unlink()
            except OSError:
                pass

    def reload_from_disk(self) -> None:
        """Force a re-read of the durable state. Used by tests to simulate a restart."""
        with self._lock:
            self._loaded = False
            self._load_locked()

    def compact(self) -> None:
        """Force a durable snapshot + WAL truncation.

        Called automatically when the WAL crosses its compaction
        threshold; public so operators and tests can trigger it
        explicitly. Raises ``IdempotencyStoreUnavailable`` if the
        snapshot cannot be written (the WAL stays the durable state).
        """
        self._ensure_loaded()
        with self._lock:
            self._persist_locked()

    def _expired_locked(self, rec: IdempotencyRecord) -> bool:
        """Pure TTL check — never mutates state (lookup path)."""
        return self._ttl_seconds > 0 and rec.claimed_at < time.time() - self._ttl_seconds

    @staticmethod
    def _tombstone_refusal_message(key: str, origin: str) -> str:
        """The 410 wording for a tombstoned key, honest about WHAT the turn
        might have done: a COMPLETE-origin tombstone says \"previously
        completed\"; a PENDING-origin tombstone (the #7782 crash window, the
        turn may never have run) says the outcome was never recorded; a
        legacy v2 tombstone whose origin is unknown stays neutral.
        """
        if origin == _TOMBSTONE_ORIGIN_PENDING:
            return (
                f"idempotency key {key!r} has an expired pending claim whose "
                "outcome was never recorded; it will not be re-admitted — "
                "check the session before retrying, or use a new key"
            )
        if origin == _TOMBSTONE_ORIGIN_COMPLETE:
            return (
                f"idempotency key {key!r} previously completed and has "
                "expired; it will not be re-admitted"
            )
        return f"idempotency key {key!r} has expired and will not be re-admitted"

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
            # #7782 finding 1: stamp pending records recovered from disk that
            # carry no identity at all (a crash between the claim and the
            # identity binding, from builds before ``claim_with_identity``).
            # Without the sweep such a record answers every retry with
            # in_flight + retry_after=1 for the rest of its TTL, which an
            # operator reads as "imminent" and polls for a day.
            try:
                _store.sweep_pending_claims()
            except Exception:  # pragma: no cover - startup must not die here
                logger.warning(
                    "idempotency: startup sweep of pending claims failed",
                    exc_info=True,
                )
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
