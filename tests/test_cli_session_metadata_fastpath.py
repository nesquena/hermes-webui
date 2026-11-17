"""Single-session CLI metadata lookups must not re-project the whole catalog.

``_lookup_cli_session_metadata()`` used to call ``get_cli_sessions()`` — a
projection over the whole agent ``state.db``, returning a deep copy of every
sidebar row — and then scan it for one id. That projection costs seconds once
the database grows; even a cache *hit* pays a full deep copy. A single page
load issues several of these lookups.

``lookup_cli_session_row()`` answers from the producer's own cache entry, and
only while the producer itself would serve that entry. That makes its answer
equal to the enumeration's **by definition** rather than by argument, and it
returns ``{}`` otherwise so the caller enumerates.

These tests pin both halves: it answers without re-projecting, and it never
answers from an entry the producer would have rebuilt.
"""
import pytest

from api import models, routes


ROWS = [
    {
        "session_id": "cli-fastpath-a",
        "profile": "default",
        "source_tag": "cli",
        "raw_source": "cli",
        "session_source": "cli",
        "source_label": "CLI",
        "parent_session_id": None,
        "workspace": None,
        "title": "Fastpath A",
    },
    {
        "session_id": "cli-fastpath-b",
        "profile": "default",
        "source_tag": "cron",
        "raw_source": "cron",
        "session_source": "cron",
        "source_label": "Cron",
        "parent_session_id": None,
        "workspace": None,
        "title": "Fastpath B",
    },
]


@pytest.fixture
def stubbed_catalog(monkeypatch):
    """Serve a fixed CLI catalog so no real ``state.db`` is required."""

    def _load(*args, **kwargs):
        return [dict(row) for row in ROWS]

    monkeypatch.setattr(models, "_load_cli_sessions_uncached", _load)
    models.clear_cli_sessions_cache()
    try:
        yield ROWS
    finally:
        models.clear_cli_sessions_cache()


def _prime():
    return models.get_cli_sessions()


def _expire_primary():
    with models._CLI_SESSIONS_CACHE_LOCK:
        for key in list(models._CLI_SESSIONS_CACHE):
            entry = models._CLI_SESSIONS_CACHE[key]
            models._CLI_SESSIONS_CACHE[key] = (0.0, entry[1], entry[2])


def _count_enumerations(monkeypatch, rows=None):
    """Make both enumeration paths record a call and answer ``rows``."""
    calls = []

    def _enum(*args, **kwargs):
        calls.append(1)
        return [dict(r) for r in (rows or [])]

    monkeypatch.setattr(models, "get_cli_sessions", _enum)
    monkeypatch.setattr(routes, "get_cli_sessions", _enum)
    return calls


def _block_fallback_reads(monkeypatch):
    """Silence the route's pre-existing ``oneshot`` fallback read."""
    monkeypatch.setattr(routes, "get_cli_session_by_id", lambda *a, **kw: {})


# ── It answers, and answers exactly like the enumeration ──────────────────


def test_lookup_matches_full_enumeration(stubbed_catalog):
    """The fast path returns exactly the row the enumeration returns."""
    expected = {row["session_id"]: row for row in _prime()}
    assert expected

    for session_id, row in expected.items():
        assert models.lookup_cli_session_row(session_id) == row

    assert models.lookup_cli_session_row("no-such-session-id") == {}
    assert models.lookup_cli_session_row("") == {}


def test_metadata_lookup_does_not_enumerate_when_snapshot_is_cached(
    stubbed_catalog, monkeypatch
):
    """A cached catalog answers the lookup without re-running the projection.

    The behavioural assertion: on the pre-fix code the second call below
    re-entered ``get_cli_sessions()`` (the spy raises).
    """
    target = ROWS[0]["session_id"]
    assert routes._lookup_cli_session_metadata(target)["session_id"] == target

    enumerations = []

    def _boom(*args, **kwargs):
        enumerations.append(1)
        raise AssertionError(
            "_lookup_cli_session_metadata re-ran the CLI catalog projection; "
            "a cached snapshot must answer a single-session lookup"
        )

    monkeypatch.setattr(routes, "get_cli_sessions", _boom)
    assert routes._lookup_cli_session_metadata(target)["session_id"] == target
    assert enumerations == []


def test_returned_row_is_a_defensive_copy(stubbed_catalog):
    """Mutating the answer must not poison the cached snapshot."""
    target = ROWS[0]["session_id"]
    assert _prime()

    with models._CLI_SESSIONS_CACHE_LOCK:
        cached = [
            r
            for entry in models._CLI_SESSIONS_CACHE.values()
            for r in entry[2]
            if r["session_id"] == target
        ]
    assert cached, "the positive cache entry should still be there"
    aliased = cached[0]

    row = models.lookup_cli_session_row(target)
    assert row["title"] == "Fastpath A"
    assert row is not aliased
    row["title"] = "mutated by the caller"
    assert aliased["title"] == "Fastpath A"


def test_aggregate_duplicate_sid_first_match_wins(stubbed_catalog, monkeypatch):
    """Duplicate SIDs answer with the FIRST row, as the enumeration scans."""
    rows = [dict(ROWS[0], title="first"), dict(ROWS[0], title="last"), ROWS[1]]
    monkeypatch.setattr(
        models, "_load_cli_sessions_uncached", lambda *a, **kw: [dict(r) for r in rows]
    )
    models.clear_cli_sessions_cache()
    enumerated = _prime()
    expected = next(r for r in enumerated if r["session_id"] == ROWS[0]["session_id"])
    assert expected["title"] == "first"
    assert models.lookup_cli_session_row(ROWS[0]["session_id"])["title"] == "first"


def test_lookup_keeps_the_enumerators_exact_id_comparison(stubbed_catalog):
    """Ids compare exactly as the enumeration compares them."""
    assert _prime()
    for sid in (" cli-fastpath-a ", "CLI-FASTPATH-A", 123, "", None):
        assert models.lookup_cli_session_row(sid) == {}, sid


# ── It never answers from an entry the producer would rebuild ─────────────


def test_expired_snapshot_defers_to_the_enumeration(stubbed_catalog, monkeypatch):
    """Expiry is the producer's rebuild trigger, so the lookup must defer.

    A rebuild can disagree with the snapshot through inputs the key never sees
    — sidecar contents, in-place row UPDATEs, another session entering the
    window — so an expired entry is not evidence of what the enumeration would
    return.
    """
    target = ROWS[0]["session_id"]
    assert _prime()
    _expire_primary()

    assert models.lookup_cli_session_row(target) == {}

    _block_fallback_reads(monkeypatch)
    enumerations = _count_enumerations(monkeypatch)
    assert routes._lookup_cli_session_metadata(target) == {}
    assert enumerations == [1], "an expired snapshot must defer to enumeration"


def test_cold_cache_defers(stubbed_catalog, monkeypatch):
    """With nothing cached the lookup cannot vouch for anything."""
    target = ROWS[0]["session_id"]
    models.clear_cli_sessions_cache()
    _block_fallback_reads(monkeypatch)
    enumerations = _count_enumerations(monkeypatch, [dict(ROWS[0])])
    assert routes._lookup_cli_session_metadata(target)["session_id"] == target
    assert enumerations == [1]


def test_disabled_cache_defers(stubbed_catalog, monkeypatch):
    """``get_cli_sessions`` gates its whole cache path on ``ttl > 0``."""
    assert _prime()
    monkeypatch.setattr(models, "_CLI_SESSIONS_CACHE_TTL_SECONDS", 0)
    assert models.lookup_cli_session_row(ROWS[0]["session_id"]) == {}


def test_stale_invalidation_stamp_defers(stubbed_catalog):
    """An explicitly cleared catalog leaves no usable entry."""
    assert _prime()
    with models._CLI_SESSIONS_CACHE_LOCK:
        for key in list(models._CLI_SESSIONS_CACHE):
            entry = models._CLI_SESSIONS_CACHE[key]
            models._CLI_SESSIONS_CACHE[key] = (entry[0], entry[1] - 1, entry[2])
    assert models.lookup_cli_session_row(ROWS[0]["session_id"]) == {}


def test_lookup_does_not_answer_across_profiles(stubbed_catalog, monkeypatch):
    """A snapshot primed under one identity must not answer another."""
    target = ROWS[0]["session_id"]
    assert _prime()
    assert models.lookup_cli_session_row(target)["session_id"] == target

    monkeypatch.setattr(
        models, "_cli_sessions_current_cache_key", lambda **kw: ("switched-profile",)
    )
    assert models.lookup_cli_session_row(target) == {}


def test_metadata_lookup_honours_a_replaced_enumeration(stubbed_catalog, monkeypatch):
    """A caller that replaces ``get_cli_sessions()`` still drives the lookup."""
    rows = [{"session_id": "replaced-enumeration-1", "profile": "default"}]
    models.clear_cli_sessions_cache()
    monkeypatch.setattr(models, "_load_cli_sessions_uncached", lambda *a, **kw: [])
    monkeypatch.setattr(routes, "get_cli_sessions", lambda **kw: [dict(r) for r in rows])

    assert routes._lookup_cli_session_metadata("replaced-enumeration-1") == rows[0]
    assert routes._lookup_cli_session_metadata("not-in-the-enumeration") == {}
