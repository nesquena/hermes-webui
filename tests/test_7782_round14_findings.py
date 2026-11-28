# ruff: noqa: F811 — the imported ``idem_env`` is a pytest fixture; ruff
# reads the same-named test parameters as redefinitions.
"""#7782 round-14 gate contracts.

The 2026-11-25 gate certificate certified the rebased integration RED with
three reviewer contract classes:

1. **rollback-state restoration** — a failed store mutation must restore the
   COMPLETE ordered in-memory state, including retained tombstone metadata;
   re-inserting evicted rows one by one changes the LRU order and can drop a
   tombstone that the mutation's own bound eviction displaced.
2. **replay authorization** — a stored acceptance may only be replayed when
   the CURRENT session exists and its ownership metadata positively matches
   the request's profile. An idempotency key is not an authorization
   credential: an absent/unsafe session id, a deleted session, or unreadable
   ownership metadata must fail closed instead of replaying.
3. **key validation** — the documented rule ("printable ASCII 0x21-0x7E, a
   pipe is not allowed") and the implemented rule must agree.

Every test drives the production path (the real ``IdempotencyStore``
mutation methods, or the real ``_handle_chat_start`` route through the
``idem_env`` harness). No source-string assertions.
"""

import time

import pytest

from api import idempotency as idem_mod
from api.idempotency import (
    STATUS_COMPLETE,
    STATUS_PENDING,
    IdempotencyKeyMalformed,
    IdempotencyRecord,
    IdempotencyStore,
    IdempotencyStoreUnavailable,
    validate_key,
)
# The chat-start harness (real route + real store + recorder in place of the
# Agent worker) from the feature's main test module.
from tests.test_idempotency import idem_env  # noqa: F401  (pytest fixture)


# ---------------------------------------------------------------------------
# Blocker 1 — rollback must restore the complete ordered state
# ---------------------------------------------------------------------------


def _record(key: str, *, status: str, claimed_at: float) -> IdempotencyRecord:
    return IdempotencyRecord(
        key=key,
        request_fingerprint=f"fp::{key}",
        status=status,
        profile="default",
        claimed_at=claimed_at,
    )


def test_claim_sweep_rollback_restores_order_and_retained_tombstones(
    tmp_path, monkeypatch
):
    """A failed tombstone-append during the claim sweep must restore both
    OrderedDicts byte-for-byte: the LRU order of records and every retained
    tombstone (including the one the sweep's bound eviction displaced).
    """
    store = IdempotencyStore(
        path=tmp_path / "idempotency" / "store.json",
        ttl_seconds=100.0,
        max_records=10,
        tombstone_max_records=2,
    )
    store._ensure_loaded()
    now = time.time()
    with store._lock:
        # Retained metadata already at its bound: minting one more tombstone
        # during the sweep displaces the OLDEST entry.
        store._expired_keys["default|oldest-tombstone"] = "complete"
        store._expired_keys["default|newer-tombstone"] = "pending"
        # Deliberate LRU order with the expiring record in the MIDDLE: a
        # re-insert-to-tail rollback changes the order even though the same
        # records survive.
        store._records["default|keep-a"] = _record(
            "default|keep-a", status=STATUS_COMPLETE, claimed_at=now
        )
        store._records["default|expire-me"] = _record(
            "default|expire-me", status=STATUS_COMPLETE, claimed_at=now - 200
        )
        store._records["default|keep-b"] = _record(
            "default|keep-b", status=STATUS_COMPLETE, claimed_at=now
        )

    expected_records = list(store._records.keys())
    expected_tombstones = list(store._expired_keys.items())

    def boom(_ops):
        raise IdempotencyStoreUnavailable("simulated append failure")

    monkeypatch.setattr(store, "_append_wal_locked", boom)

    with pytest.raises(IdempotencyStoreUnavailable):
        store.claim("fresh-key", "fp::fresh", profile="default")

    assert list(store._records.keys()) == expected_records, (
        "rollback changed the record LRU order: "
        f"{list(store._records.keys())!r} != {expected_records!r}"
    )
    assert list(store._expired_keys.items()) == expected_tombstones, (
        "rollback lost/reshuffled retained tombstone metadata: "
        f"{list(store._expired_keys.items())!r} != {expected_tombstones!r}"
    )


def test_claim_cap_rollback_restores_order_and_retained_tombstones(
    tmp_path, monkeypatch
):
    """The capacity-eviction branch has the same contract as the TTL sweep:
    when its append fails, records come back in their original positions and
    the tombstones displaced by the tombstone bound come back too.
    """
    store = IdempotencyStore(
        path=tmp_path / "idempotency" / "store.json",
        # ttl=0 disables the TTL sweep; the cap sweep (cutoff=None) evicts
        # every non-pending record.
        ttl_seconds=0.0,
        max_records=2,
        tombstone_max_records=1,
    )
    store._ensure_loaded()
    now = time.time()
    with store._lock:
        store._expired_keys["default|retained"] = "complete"
        # [complete, pending, complete]: evictable records sit on BOTH sides
        # of the pending record, so re-inserting them at the tail produces a
        # visibly different order.
        store._records["default|old-a"] = _record(
            "default|old-a", status=STATUS_COMPLETE, claimed_at=now - 10
        )
        store._records["default|pending-p"] = _record(
            "default|pending-p", status=STATUS_PENDING, claimed_at=now
        )
        store._records["default|old-b"] = _record(
            "default|old-b", status=STATUS_COMPLETE, claimed_at=now - 10
        )

    expected_records = list(store._records.keys())
    expected_tombstones = list(store._expired_keys.items())

    def boom(_ops):
        raise IdempotencyStoreUnavailable("simulated append failure")

    monkeypatch.setattr(store, "_append_wal_locked", boom)

    with pytest.raises(IdempotencyStoreUnavailable):
        store.claim("fresh-cap", "fp::fresh-cap", profile="default")

    assert list(store._records.keys()) == expected_records, (
        "cap rollback changed the record order: "
        f"{list(store._records.keys())!r} != {expected_records!r}"
    )
    assert list(store._expired_keys.items()) == expected_tombstones, (
        "cap rollback lost/reshuffled retained tombstone metadata: "
        f"{list(store._expired_keys.items())!r} != {expected_tombstones!r}"
    )


# ---------------------------------------------------------------------------
# Blocker 2 — replay requires positive session existence + ownership evidence
# ---------------------------------------------------------------------------


def test_replay_fails_closed_when_the_session_no_longer_exists(
    idem_env, monkeypatch
):
    """A deleted session cannot authorize a stored acceptance: the replay
    answers 404 instead of handing back the old identity."""
    from api import routes

    body = {"session_id": "idem-session", "message": "hello"}
    key = "replay-auth-missing-session"
    s1, p1 = idem_env.run_handler(body, key_header=key)
    assert s1 == 200, p1
    assert p1.get("stream_id")

    def missing(_sid):
        raise KeyError(_sid)

    monkeypatch.setattr(routes, "get_session_profile_readonly", missing)
    s2, p2 = idem_env.run_handler(body, key_header=key)
    assert s2 == 404, f"replay returned {s2} without a live session: {p2}"
    assert "stream_id" not in p2


def test_replay_fails_closed_when_ownership_cannot_be_established(
    idem_env, monkeypatch
):
    """Unreadable / absent ownership metadata is NOT proof of ownership —
    the documented contract answers 404 ("ownership cannot be established")."""
    from api import routes

    body = {"session_id": "idem-session", "message": "hello"}
    key = "replay-auth-no-owner-metadata"
    s1, _p1 = idem_env.run_handler(body, key_header=key)
    assert s1 == 200

    monkeypatch.setattr(routes, "get_session_profile_readonly", lambda _sid: None)
    s2, p2 = idem_env.run_handler(body, key_header=key)
    assert s2 == 404, f"replay returned {s2} with no ownership evidence: {p2}"
    assert "stream_id" not in p2


def test_replay_refuses_a_foreign_profile_owner(idem_env, monkeypatch):
    """A session owned by another profile keeps the keyless-parity 409."""
    from api import routes

    body = {"session_id": "idem-session", "message": "hello"}
    key = "replay-auth-foreign-profile"
    s1, _p1 = idem_env.run_handler(body, key_header=key)
    assert s1 == 200

    monkeypatch.setattr(
        routes, "get_session_profile_readonly", lambda _sid: "other-profile"
    )
    s2, p2 = idem_env.run_handler(body, key_header=key)
    assert s2 == 409, f"replay crossed the profile boundary: {s2} {p2}"
    assert p2.get("code") == "session_profile_mismatch"


def test_replay_refuses_an_acceptance_with_no_session_id(idem_env, monkeypatch):
    """A stored acceptance whose session id is empty has no existence
    evidence at all; replaying it would fabricate authorization."""
    store = idem_env.store
    body = {"session_id": "idem-session", "message": "hello"}
    key = "replay-auth-empty-sid"
    fingerprint = idem_mod.compute_request_fingerprint(body, profile="default")
    store.claim(key, fingerprint, profile="default")
    store.complete(
        key,
        profile="default",
        session_id="",
        stream_id="stream-empty",
        turn_id="turn-empty",
        response_status=200,
        response_payload={"stream_id": "stream-empty", "turn_id": "turn-empty"},
    )

    before = len(idem_env.recorder.calls)
    s, p = idem_env.run_handler(body, key_header=key)
    assert s == 404, f"replay returned {s} without a session id: {p}"
    assert len(idem_env.recorder.calls) == before


# ---------------------------------------------------------------------------
# Blocker 3 — documented and implemented key validation agree
# ---------------------------------------------------------------------------


def test_pipe_is_rejected_as_documented(idem_env):
    """The doc says "a pipe (|) is not allowed"; ``validate_key`` must
    enforce it, not rely on a comment that says it does."""
    for bad in ("pipe|key", "a|b|c", "|", "ends|"):
        with pytest.raises(IdempotencyKeyMalformed):
            validate_key(bad)

    before = len(idem_env.recorder.calls)
    s, p = idem_env.run_handler(
        {"session_id": "idem-session", "message": "hello"},
        key_header="pipe|key",
    )
    assert s == 400, f"a pipe key reached the store: {s} {p}"
    assert len(idem_env.recorder.calls) == before, "a pipe key admitted a turn"


def test_documented_key_boundaries_match_the_implementation():
    """200 chars is allowed; 201 is not. Printable ASCII 0x21-0x7E is
    allowed; whitespace/control/non-ASCII are not."""
    assert validate_key("x" * 200) == "x" * 200
    with pytest.raises(IdempotencyKeyMalformed):
        validate_key("x" * 201)

    assert validate_key("!~") == "!~"
    for bad in ("has space", "tab\there", "line\nbreak", "caf\u00e9", "ctrl\x7f"):
        with pytest.raises(IdempotencyKeyMalformed):
            validate_key(bad)

    # Trimming is documented ("trimmed"): surrounding whitespace is removed.
    assert validate_key("  trimmed-key  ") == "trimmed-key"
