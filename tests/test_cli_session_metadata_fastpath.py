"""Single-session CLI metadata lookups must not re-project the whole catalog.

perf(webui/session-load-latency): ``_lookup_cli_session_metadata()`` answered by
calling ``get_cli_sessions()`` — a projection over the whole agent ``state.db``
— and then scanning the result. That projection costs seconds once the database
grows, and its cache key embeds a live ``state.db`` fingerprint, so an agent
write makes the cached snapshot unreachable again. The gateway SSE re-announces
the active session on every tick, so a single page load issued three of these
lookups and paid the projection each time.

These tests pin the two properties that make the fast path safe:

1. it returns exactly what the enumeration returns (no behavioural drift), and
2. it answers from a cached snapshot instead of re-running the projection.
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


def test_lookup_matches_full_enumeration(stubbed_catalog):
    """The fast path returns exactly the row the enumeration returns."""
    expected = {row["session_id"]: row for row in models.get_cli_sessions()}
    assert expected, "fixture should produce rows"

    for session_id, row in expected.items():
        assert models.lookup_cli_session_row(session_id) == row

    # Unknown and empty ids keep the enumeration's "not found" answer ({}), which
    # every call site already treats as "no CLI metadata".
    assert models.lookup_cli_session_row("no-such-session-id") == {}
    assert models.lookup_cli_session_row("") == {}


def test_metadata_lookup_does_not_enumerate_when_snapshot_is_cached(
    stubbed_catalog, monkeypatch
):
    """A cached catalog answers the lookup without re-running the projection.

    This is the behavioural assertion: on the pre-fix code the second call below
    re-entered ``get_cli_sessions()`` (the spy raises), because the lookup had no
    cache path at all. It deliberately primes through the public route helper so
    it fails for the right reason on old code instead of tripping over a missing
    name.
    """
    target = ROWS[0]["session_id"]

    # Prime the catalog through the same helper the request path uses.
    assert routes._lookup_cli_session_metadata(target)["session_id"] == target

    enumerations = []

    def _boom(*args, **kwargs):
        enumerations.append(1)
        raise AssertionError(
            "_lookup_cli_session_metadata re-ran the CLI catalog projection; "
            "a cached snapshot must answer a single-session lookup"
        )

    # The pre-fix implementation resolved this name on every call.
    monkeypatch.setattr(routes, "get_cli_sessions", _boom)

    meta = routes._lookup_cli_session_metadata(target)
    assert meta["session_id"] == target
    assert enumerations == []


def test_metadata_lookup_honours_a_replaced_enumeration(stubbed_catalog, monkeypatch):
    """A caller that replaces ``get_cli_sessions()`` must still drive the lookup.

    The fast path is an optimisation, not a second source of truth: when nothing
    is cached, the module-level enumeration decides. Upstream callers rely on
    this — ``test_git_info_untrusted_state_db_workspace_is_not_inspected`` stubs
    ``routes.get_cli_sessions`` and expects ``/api/git-info`` to see the row.
    Answering ``{}`` here turned that into a 404 "Session not found".
    """
    rows = [{"session_id": "replaced-enumeration-1", "profile": "default"}]

    # Cache nothing, so the fast path cannot answer and the fallback must run.
    models.clear_cli_sessions_cache()
    monkeypatch.setattr(models, "_load_cli_sessions_uncached", lambda *a, **kw: [])
    monkeypatch.setattr(routes, "get_cli_sessions", lambda **kw: [dict(r) for r in rows])

    assert routes._lookup_cli_session_metadata("replaced-enumeration-1") == rows[0]

    # And a genuinely absent id still reports "no CLI metadata".
    assert routes._lookup_cli_session_metadata("not-in-the-enumeration") == {}


def test_lookup_survives_a_cache_key_churn(stubbed_catalog, monkeypatch):
    """A state.db fingerprint change must not force a re-projection.

    The primary cache key embeds a live ``state.db`` fingerprint. The
    last-known-good map is keyed by the stable identity precisely so a single
    agent write does not invalidate an otherwise usable snapshot.
    """
    target = ROWS[1]["session_id"]
    assert models.lookup_cli_session_row(target)["session_id"] == target

    # Drop every primary-cache entry while keeping the last-known-good map, which
    # is what a changed fingerprint looks like to a lookup: exact-key miss.
    with models._CLI_SESSIONS_CACHE_LOCK:
        models._CLI_SESSIONS_CACHE.clear()

    enumerations = []

    def _count(*args, **kwargs):
        enumerations.append(1)
        return [dict(row) for row in ROWS]

    monkeypatch.setattr(models, "get_cli_sessions", _count)

    row = models.lookup_cli_session_row(target)
    assert row["session_id"] == target
    assert enumerations == [], (
        "the stable-identity snapshot should have answered this lookup"
    )
