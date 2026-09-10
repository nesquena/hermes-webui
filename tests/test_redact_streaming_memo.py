"""Streaming (TTL'd) redact memo: plan cache layer B (Task 5a).

The inactive-session redact memo (``_session_redact_cache``) can never serve
an ACTIVE session safely: the merged transcript tail mutates between deltas,
so a plain signature-match memo could pin a stale window for the whole turn.
The streaming twin adds a short TTL (5s, mirroring the display-merge
streaming TTL) on top of the exact same ``_session_redact_signature`` key
family, and lives in its OWN OrderedDict with its OWN lock so streaming churn
can never evict inactive entries (defense in depth per the approved plan).

Fail-closed contract: a miss, an expired entry, a signature mismatch, or any
internal exception all return ``None`` — the caller then falls back to a
fresh ``redact_session_data()`` walk and never serves a stale transcript.
"""

import pytest

import api.helpers as helpers


SID = "20260830_000000_stream-redact"
OTHER_SID = "20260830_000000_inactive-redact"
STATE_SIG = ("target-session-revision-v1", 42)
MSG_LIMIT = 50
OFFSET = 0

REDACTED = [
    {"role": "user", "content": "m0 (redacted)", "timestamp": 1.0, "id": "a"},
    {"role": "assistant", "content": "m1 (redacted)", "timestamp": 2.0, "id": "b"},
]


def _signature(sid=SID, *, messages=None, state_db_signature=STATE_SIG,
               redact_enabled=True, msg_limit=MSG_LIMIT, offset=OFFSET):
    return helpers._session_redact_signature(
        sid,
        messages=messages if messages is not None else [
            {"role": "user", "timestamp": 1.0, "id": "a"},
            {"role": "assistant", "timestamp": 2.0, "id": "b"},
        ],
        state_db_signature=state_db_signature,
        redact_enabled=redact_enabled,
        msg_limit=msg_limit,
        messages_offset=offset,
    )


@pytest.fixture()
def clean_cache():
    """Empty both redact memos before and after each test (no cross-test leaks)."""
    for cache, lock in (
        (helpers._session_redact_cache, helpers._session_redact_cache_lock),
        (
            helpers._session_redact_streaming_cache,
            helpers._session_redact_streaming_cache_lock,
        ),
    ):
        with lock:
            cache.clear()
    yield
    for cache, lock in (
        (helpers._session_redact_cache, helpers._session_redact_cache_lock),
        (
            helpers._session_redact_streaming_cache,
            helpers._session_redact_streaming_cache_lock,
        ),
    ):
        with lock:
            cache.clear()


def test_put_get_roundtrip_returns_exact_redacted_list(clean_cache):
    """(a) A store/probe cycle hands back the exact previously-redacted list."""
    sig = _signature()
    assert sig is not None

    helpers._session_redact_streaming_cached_put(SID, sig, REDACTED)
    hit = helpers._session_redact_streaming_cached_get(SID, sig)

    assert hit is REDACTED  # entry contract: the caller-supplied list object
    assert hit == REDACTED


def test_expired_entry_misses_and_is_evicted(clean_cache, monkeypatch):
    """(b) Beyond the 5s TTL the probe misses and the entry is evicted."""
    now = [1000.0]
    monkeypatch.setattr(helpers.time, "monotonic", lambda: now[0])
    sig = _signature()

    helpers._session_redact_streaming_cached_put(SID, sig, REDACTED)
    assert helpers._session_redact_streaming_cached_get(SID, sig) is REDACTED

    now[0] += helpers._SESSION_REDACT_STREAMING_TTL_SECONDS + 0.01
    assert helpers._session_redact_streaming_cached_get(SID, sig) is None
    with helpers._session_redact_streaming_cache_lock:
        assert SID not in helpers._session_redact_streaming_cache  # evicted on probe

    # Clock walked back inside the TTL: still a miss — the entry is gone for good.
    now[0] = 1001.0
    assert helpers._session_redact_streaming_cached_get(SID, sig) is None


def test_signature_mismatch_fails_closed(clean_cache):
    """(c) A different window/store/flag signature must never serve the entry."""
    sig = _signature()
    helpers._session_redact_streaming_cached_put(SID, sig, REDACTED)

    grown_tail = [
        {"role": "user", "timestamp": 1.0, "id": "a"},
        {"role": "assistant", "timestamp": 2.0, "id": "b"},
        {"role": "assistant", "timestamp": 3.0, "id": "c"},  # tail marker changed
    ]
    assert (
        helpers._session_redact_streaming_cached_get(SID, _signature(messages=grown_tail))
        is None
    )
    assert (
        helpers._session_redact_streaming_cached_get(SID, _signature(msg_limit=MSG_LIMIT + 10))
        is None
    )
    assert (
        helpers._session_redact_streaming_cached_get(SID, _signature(offset=OFFSET + 1))
        is None
    )
    assert (
        helpers._session_redact_streaming_cached_get(
            SID, _signature(state_db_signature=None)
        )
        is None
    )
    assert (
        helpers._session_redact_streaming_cached_get(
            SID, _signature(redact_enabled=False)
        )
        is None
    )
    # None signature is always a miss (fail-closed by construction).
    assert helpers._session_redact_streaming_cached_get(SID, None) is None
    # The original entry must survive every failed probe untouched.
    assert helpers._session_redact_streaming_cached_get(SID, sig) is REDACTED


def test_pop_removes_entry_and_is_idempotent(clean_cache):
    """(d) Pop removes the entry; popping an unknown/absent sid must not raise."""
    sig = _signature()
    helpers._session_redact_streaming_cached_put(SID, sig, REDACTED)
    assert sig is not None
    helpers._session_redact_streaming_cached_put(SID, sig, REDACTED)
    assert helpers._session_redact_streaming_cached_get(SID, sig) is REDACTED

    helpers._session_redact_cache_pop(SID)
    with helpers._session_redact_streaming_cache_lock:
        assert SID not in helpers._session_redact_streaming_cache
    assert helpers._session_redact_streaming_cached_get(SID, sig) is None

    helpers._session_redact_cache_pop(SID)  # idempotent — tolerant of missing key
    helpers._session_redact_cache_pop("20260830_000000_never-stored")  # no raise


def test_pop_clears_inactive_memo_too(clean_cache):
    """routes.evict_streaming_redact_entry pops ONE memo per sid; cover both.

    The turn-end belt (routes.py Task 1 hook) forwards the sid to this helper;
    the inactive memo uses the same key family, so the pop must clear whichever
    memo currently holds the sid without touching the other sid's entry.
    """
    sig = _signature()
    other_sig = _signature(OTHER_SID)
    helpers._session_redact_cached_put(OTHER_SID, other_sig, REDACTED)
    assert helpers._session_redact_cached_get(OTHER_SID, other_sig) is REDACTED

    helpers._session_redact_cache_pop(SID)  # absent everywhere: pure no-op
    assert helpers._session_redact_cached_get(OTHER_SID, other_sig) is REDACTED

    helpers._session_redact_cached_put(SID, sig, REDACTED)
    helpers._session_redact_cache_pop(SID)
    assert helpers._session_redact_cached_get(SID, sig) is None
    assert helpers._session_redact_cached_get(OTHER_SID, other_sig) is REDACTED


def test_streaming_and_inactive_caches_are_independent(clean_cache):
    """(e) Same sid in both memos: one put must not evict the other's entry."""
    sig = _signature()
    helpers._session_redact_cached_put(SID, sig, REDACTED)
    helpers._session_redact_streaming_cached_put(SID, sig, REDACTED)

    assert helpers._session_redact_cached_get(SID, sig) is REDACTED
    assert helpers._session_redact_streaming_cached_get(SID, sig) is REDACTED

    # Streaming churn beyond the cap evicts only streaming entries.
    for i in range(helpers._SESSION_REDACT_CACHE_MAX + 10):
        helpers._session_redact_streaming_cached_put(f"{SID}-{i}", sig, REDACTED)

    assert helpers._session_redact_cached_get(SID, sig) is REDACTED
    assert helpers._session_redact_streaming_cached_get(SID, sig) is None


def test_put_is_noop_on_none_signature(clean_cache):
    """Fail-closed: an unbuildable signature stores nothing and probes miss."""
    helpers._session_redact_streaming_cached_put(SID, None, REDACTED)
    with helpers._session_redact_streaming_cache_lock:
        assert helpers._session_redact_streaming_cache == {}
    assert helpers._session_redact_streaming_cached_get(SID, None) is None


def test_redact_session_data_accepts_messages_override(clean_cache):
    """(f) redact_session_data's _messages_override kwarg stays intact (Task 5a
    is additive; the full override contract is covered by its own suite)."""
    session = {
        "session_id": SID,
        "title": "hello",
        "messages": [
            {"role": "user", "content": "github_pat_AAAA0123456789012345678901", "id": "a"},
        ],
    }
    override = [{"role": "user", "content": "PRESERVED", "id": "a"}]

    result = helpers.redact_session_data(session, _messages_override=override)
    assert result["messages"] is override
    assert result["messages"] == override

    # Default call (no kwarg) keeps walking/redacting every field.
    default = helpers.redact_session_data(session)
    assert default["messages"] is not override
    assert "github_pat_" not in _dump(default["messages"])


def _dump(value):
    import json

    return json.dumps(value)
