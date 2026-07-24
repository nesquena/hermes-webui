"""#6192 gate regression: sidebar-tab badges stay authoritative on the
sidebar_source=webui shortcut.

The shortcut skips the expensive ``get_cli_sessions()`` projection for the
returned rows, but external state.db / Claude-Code sessions exist ONLY in
that projection — so the badge counts (``cli_session_count`` /
``archived_cli_count``) must still incorporate them, via the churn-tolerant
badge cache, and be identical between a ``sidebar_source=webui`` and a
``sidebar_source=cli`` request over the same seeded store.
"""

import io
import json
from urllib.parse import urlparse

import api.profiles as profiles
import api.routes as routes
import pytest


class _FakeHandler:
    def __init__(self):
        self.status = None
        self.headers = {}
        self.wfile = io.BytesIO()

    def send_response(self, status):
        self.status = status

    def send_header(self, key, value):
        self.headers[key] = value

    def end_headers(self):
        pass

    def json_body(self):
        return json.loads(self.wfile.getvalue().decode("utf-8"))


def _handle_sessions(url):
    handler = _FakeHandler()
    routes.handle_get(handler, urlparse(url))
    return handler


def _external_cli_rows(count, archived_count=0):
    """Rows shaped like the state.db / Claude-Code projection output —
    deliberately NOT present in ``all_sessions()``."""
    rows = []
    for index in range(count):
        rows.append(
            {
                "session_id": f"external-cli-{index}",
                "title": f"External CLI {index}",
                "profile": "default",
                "archived": index < archived_count,
                "message_count": 3,
                "updated_at": 5000 + index,
                "last_message_at": 5000 + index,
                "source": "cli",
                "raw_source": "cli",
                "session_source": "cli",
                "source_tag": "cli",
                "source_label": "CLI",
                "is_cli_session": True,
            }
        )
    return rows


def _local_webui_rows(count):
    return [
        {
            "session_id": f"webui-{index}",
            "title": "WebUI Session",
            "profile": "default",
            "archived": False,
            "message_count": 1,
            "updated_at": 1000 + index,
            "last_message_at": 1000 + index,
            "source": "webui",
            "raw_source": "webui",
            "session_source": "webui",
            "source_tag": "webui",
        }
        for index in range(count)
    ]


@pytest.fixture(autouse=True)
def _clear_caches(monkeypatch):
    # TTL 0: every request refreshes the badge cache, so the equality
    # assertions never depend on refresh timing.
    monkeypatch.setenv("HERMES_WEBUI_CLI_BADGE_TTL_SECONDS", "0")
    routes._session_list_cache_clear()
    routes._reset_cli_badge_cache_for_tests()
    yield
    routes._session_list_cache_clear()
    routes._reset_cli_badge_cache_for_tests()


def _install(monkeypatch, webui_rows, external_rows):
    row_ids = {str(r["session_id"]) for r in webui_rows}
    monkeypatch.setattr(routes, "all_sessions", lambda diag=None: list(webui_rows))
    monkeypatch.setattr(
        routes, "_reconcile_stale_stream_state_for_session_rows", lambda _rows: False
    )
    monkeypatch.setattr(routes, "_enrich_sidebar_lineage_metadata", lambda rows: None)
    monkeypatch.setattr(
        routes,
        "get_cli_sessions",
        lambda source_filter=None, all_profiles=False: list(external_rows),
    )
    monkeypatch.setattr(
        routes,
        "agent_session_rows_existing",
        lambda ids, profile=None: set(row_ids & {str(sid) for sid in ids}),
    )
    monkeypatch.setattr(routes, "load_settings", lambda: {"show_cli_sessions": True})
    monkeypatch.setattr(profiles, "get_active_profile_name", lambda: "default")


def test_webui_and_cli_requests_report_identical_cli_counts(monkeypatch):
    """The gate's exact demand: identical counts over the same seeded store
    containing rows that exist only in the external projection."""
    _install(monkeypatch, _local_webui_rows(3), _external_cli_rows(5, archived_count=2))

    webui = _handle_sessions(
        "http://example.com/api/sessions?sidebar_source=webui&include_archived=1"
    ).json_body()
    routes._session_list_cache_clear()
    cli = _handle_sessions(
        "http://example.com/api/sessions?sidebar_source=cli&include_archived=1"
    ).json_body()

    assert webui["cli_session_count"] == cli["cli_session_count"]
    assert webui["archived_cli_count"] == cli["archived_cli_count"]
    assert webui["cli_session_count"] > 0, (
        "external-only rows must be counted on the webui-tab request"
    )


def test_webui_request_still_returns_no_cli_rows(monkeypatch):
    """Counting must not leak the projection rows back into the payload."""
    _install(monkeypatch, _local_webui_rows(2), _external_cli_rows(4))

    body = _handle_sessions(
        "http://example.com/api/sessions?sidebar_source=webui"
    ).json_body()

    returned_ids = {s["session_id"] for s in body["sessions"]}
    assert not any(sid.startswith("external-cli-") for sid in returned_ids)
    assert body["cli_session_count"] == 4


def test_badge_cache_generation_bumps_only_on_count_changes(monkeypatch):
    """A refresh with unchanged rows must NOT churn the response-cache stamp;
    a real change must bump it exactly once."""
    from api import route_session_list_cache as cache_mod

    _install(monkeypatch, _local_webui_rows(1), _external_cli_rows(2))
    gen0 = cache_mod._cli_badge_cache_generation()
    cache_mod.get_cli_sessions_for_badges(profile_key="default")
    gen1 = cache_mod._cli_badge_cache_generation()
    cache_mod.get_cli_sessions_for_badges(profile_key="default")
    gen2 = cache_mod._cli_badge_cache_generation()
    assert gen1 == gen0 + 1  # first fill counts as a change from empty
    assert gen2 == gen1  # identical rows: no stamp churn

    monkeypatch.setattr(
        routes,
        "get_cli_sessions",
        lambda source_filter=None, all_profiles=False: _external_cli_rows(3),
    )
    cache_mod.get_cli_sessions_for_badges(profile_key="default")
    assert cache_mod._cli_badge_cache_generation() == gen2 + 1


def test_parallel_cold_misses_run_exactly_one_projection(monkeypatch):
    """Review round 2: concurrent cold misses must not fan the projection
    out -- one leader loads, followers get last-known state immediately."""
    import threading as _threading

    from api import route_session_list_cache as cache_mod

    monkeypatch.setenv("HERMES_WEBUI_CLI_BADGE_TTL_SECONDS", "3600")
    cache_mod._reset_cli_badge_cache_for_tests()

    load_calls = []
    release = _threading.Event()

    def slow_loader(source_filter=None, all_profiles=False):
        load_calls.append(1)
        release.wait(timeout=5)
        return _external_cli_rows(2)

    monkeypatch.setattr(routes, "get_cli_sessions", slow_loader)

    results = {}

    def worker(name):
        results[name] = cache_mod.get_cli_sessions_for_badges(profile_key="default")

    threads = [_threading.Thread(target=worker, args=(f"t{i}",)) for i in range(4)]
    for t in threads:
        t.start()
    import time as _time

    deadline = _time.time() + 2
    while len(load_calls) == 0 and _time.time() < deadline:
        _time.sleep(0.01)
    release.set()
    for t in threads:
        t.join(timeout=5)

    assert len(load_calls) == 1, "parallel misses duplicated the projection"
    leader_rows = [r for r in results.values() if r]
    assert leader_rows and len(leader_rows[0]) == 2


def test_loader_failure_keeps_last_known_good(monkeypatch):
    """Review round 2: an error must never overwrite a good badge state
    with zeros for a TTL window."""
    from api import route_session_list_cache as cache_mod

    monkeypatch.setenv("HERMES_WEBUI_CLI_BADGE_TTL_SECONDS", "0")
    cache_mod._reset_cli_badge_cache_for_tests()

    monkeypatch.setattr(
        routes, "get_cli_sessions",
        lambda source_filter=None, all_profiles=False: _external_cli_rows(3),
    )
    good = cache_mod.get_cli_sessions_for_badges(profile_key="default")
    assert len(good) == 3

    def broken(source_filter=None, all_profiles=False):
        raise RuntimeError("projection exploded")

    monkeypatch.setattr(routes, "get_cli_sessions", broken)
    after_failure = cache_mod.get_cli_sessions_for_badges(profile_key="default")
    assert len(after_failure) == 3, "failure clobbered last-known-good"
    # And the generation did not churn on the failure path.
    gen_before = cache_mod._cli_badge_cache_generation()
    cache_mod.get_cli_sessions_for_badges(profile_key="default")
    assert cache_mod._cli_badge_cache_generation() == gen_before


# ── Bounded-projection contract (option B) ──────────────────────────────────
# The cache must be bounded PER SCOPE, single-flight WITH result sharing,
# completion-TTL'd, and failure-aware. `get_cli_sessions()` normalizes its
# failures into stale rows or `[]` instead of raising, so "returned something"
# is not success — treating a failure-normalized `[]` as a good load replaced
# last-known-good counts with zeros and published them for a whole TTL.


@pytest.fixture
def badge_cache(monkeypatch):
    from api import route_session_list_cache as cache_mod

    monkeypatch.setenv("HERMES_WEBUI_CLI_BADGE_TTL_SECONDS", "30")
    cache_mod._reset_cli_badge_cache_for_tests()
    yield cache_mod
    cache_mod._reset_cli_badge_cache_for_tests()


def _install_loader(monkeypatch, fn):
    monkeypatch.setattr(routes, "get_cli_sessions", fn)


def test_production_normalized_failure_keeps_last_known_good(monkeypatch, badge_cache):
    """The REAL failure contract: the loader returns [], it does not raise.

    `api/models._load_and_cache_cli_sessions()` catches projection errors and
    returns stale rows or `[]`, and the outer `get_cli_sessions()` fallback does
    the same. A badge cache that only treats a RAISING loader as failure never
    sees production failure at all.
    """
    from api import models as models_mod

    calls = {"n": 0}

    def loader(source_filter=None, all_profiles=False):
        calls["n"] += 1
        if calls["n"] == 1:
            return _external_cli_rows(4)
        # Exactly what production does: swallow, record, return [].
        models_mod._note_cli_projection_failure()
        return []

    _install_loader(monkeypatch, loader)
    monkeypatch.setenv("HERMES_WEBUI_CLI_BADGE_TTL_SECONDS", "0")

    good = badge_cache.get_cli_sessions_for_badges(profile_key="default")
    assert len(good) == 4
    gen_after_good = badge_cache._cli_badge_cache_generation()

    after_failure = badge_cache.get_cli_sessions_for_badges(profile_key="default")
    assert len(after_failure) == 4, "a failure-normalized [] clobbered last-known-good"
    assert badge_cache._cli_badge_cache_generation() == gen_after_good, (
        "a failed projection bumped the generation and invalidated the bucket"
    )


def test_genuinely_successful_empty_result_does_replace_good_rows(monkeypatch, badge_cache):
    """The other side of the same coin: a real "no CLI sessions" must apply."""
    calls = {"n": 0}

    def loader(source_filter=None, all_profiles=False):
        calls["n"] += 1
        return _external_cli_rows(4) if calls["n"] == 1 else []

    _install_loader(monkeypatch, loader)
    monkeypatch.setenv("HERMES_WEBUI_CLI_BADGE_TTL_SECONDS", "0")

    assert len(badge_cache.get_cli_sessions_for_badges(profile_key="default")) == 4
    gen_after_good = badge_cache._cli_badge_cache_generation()
    assert badge_cache.get_cli_sessions_for_badges(profile_key="default") == []
    assert badge_cache._cli_badge_cache_generation() > gen_after_good


def test_slow_cold_followers_consume_the_leader_result(monkeypatch, badge_cache):
    """Every concurrent caller for the same key gets the CORRECT rows.

    A cold follower used to be handed stale rows or `[]` while the leader was
    seconds away from the real answer — so a first-load burst published zero
    badges to most of the tabs that asked.
    """
    import threading

    started = threading.Event()
    release = threading.Event()
    calls = {"n": 0}

    def slow_loader(source_filter=None, all_profiles=False):
        calls["n"] += 1
        started.set()
        assert release.wait(timeout=5)
        return _external_cli_rows(6)

    _install_loader(monkeypatch, slow_loader)

    results = {}

    def ask(tag):
        results[tag] = badge_cache.get_cli_sessions_for_badges(profile_key="default")

    leader = threading.Thread(target=ask, args=("leader",))
    leader.start()
    assert started.wait(timeout=5)
    followers = [threading.Thread(target=ask, args=(f"f{i}",)) for i in range(3)]
    for thread in followers:
        thread.start()
    # Give the followers time to reach the wait rather than racing past it.
    threading.Event().wait(0.15)
    release.set()
    for thread in [leader, *followers]:
        thread.join(timeout=10)
        assert not thread.is_alive()

    assert calls["n"] == 1, "single-flight broken: the projection ran more than once"
    assert len(results) == 4
    for tag, rows in results.items():
        assert len(rows) == 6, f"{tag} got {len(rows)} rows instead of the leader's 6"


def test_a_b_all_profiles_alternation_projects_once_per_scope(monkeypatch, badge_cache):
    """A → B → all-profiles → A within one TTL must not re-project.

    A single global slot evicted the previous scope on every switch, so this
    rotation ran the expensive projection on every single request.
    """
    calls = []

    def loader(source_filter=None, all_profiles=False):
        calls.append(bool(all_profiles))
        return _external_cli_rows(2)

    _install_loader(monkeypatch, loader)

    badge_cache.get_cli_sessions_for_badges(profile_key="alpha")
    badge_cache.get_cli_sessions_for_badges(profile_key="beta")
    badge_cache.get_cli_sessions_for_badges(all_profiles=True, profile_key=None)
    badge_cache.get_cli_sessions_for_badges(profile_key="alpha")
    badge_cache.get_cli_sessions_for_badges(profile_key="beta")

    assert len(calls) == 3, f"expected one projection per scope, got {len(calls)}"


def test_a_different_key_neither_blocks_on_nor_inherits_an_in_flight_load(
    monkeypatch, badge_cache
):
    """Overlapping loads for different scopes must not leak into each other."""
    import threading

    started = threading.Event()
    release = threading.Event()

    def loader(source_filter=None, all_profiles=False):
        if not started.is_set():
            started.set()
            assert release.wait(timeout=5)
            return _external_cli_rows(7)
        return _external_cli_rows(2)

    _install_loader(monkeypatch, loader)

    slow = {}
    fast = {}
    slow_thread = threading.Thread(
        target=lambda: slow.update(
            rows=badge_cache.get_cli_sessions_for_badges(profile_key="alpha")
        )
    )
    slow_thread.start()
    assert started.wait(timeout=5)

    # A different scope resolves immediately and correctly, with its own rows.
    fast["rows"] = badge_cache.get_cli_sessions_for_badges(profile_key="beta")
    assert len(fast["rows"]) == 2, "a different key inherited or waited on alpha's load"

    release.set()
    slow_thread.join(timeout=10)
    assert not slow_thread.is_alive()
    assert len(slow["rows"]) == 7


def test_ttl_is_stamped_on_completion_not_on_entry(monkeypatch, badge_cache):
    """A load slower than the TTL must not be published already expired."""
    import time as time_mod

    ttl = 0.4
    monkeypatch.setenv("HERMES_WEBUI_CLI_BADGE_TTL_SECONDS", str(ttl))
    calls = {"n": 0}

    def slow_loader(source_filter=None, all_profiles=False):
        calls["n"] += 1
        time_mod.sleep(ttl * 1.5)
        return _external_cli_rows(3)

    _install_loader(monkeypatch, slow_loader)

    assert len(badge_cache.get_cli_sessions_for_badges(profile_key="default")) == 3
    # Immediately after a load that outlived the TTL, the result must still be
    # fresh — an entry-stamped expiry would already be in the past here.
    assert len(badge_cache.get_cli_sessions_for_badges(profile_key="default")) == 3
    assert calls["n"] == 1, "a slow load was born expired and re-projected at once"


def test_publication_failure_releases_ownership_and_allows_retry(monkeypatch, badge_cache):
    """An exception after the load must not strand `loading` for this key.

    Stranded ownership wedges every later caller for that scope into the
    bounded wait and then into serving `[]`.
    """
    calls = {"n": 0}

    class _Hostile(dict):
        # Explodes while the publication path builds the signature.
        def get(self, *_a, **_kw):
            raise RuntimeError("signature exploded")

    def loader(source_filter=None, all_profiles=False):
        calls["n"] += 1
        if calls["n"] == 1:
            return [_Hostile()]
        return _external_cli_rows(5)

    _install_loader(monkeypatch, loader)
    monkeypatch.setenv("HERMES_WEBUI_CLI_BADGE_TTL_SECONDS", "0")

    with pytest.raises(RuntimeError):
        badge_cache.get_cli_sessions_for_badges(profile_key="default")

    # Ownership released → the next call may load, and does.
    rows = badge_cache.get_cli_sessions_for_badges(profile_key="default")
    assert len(rows) == 5
    assert calls["n"] == 2


def test_generation_bump_is_key_local(monkeypatch, badge_cache):
    """A refresh in one scope must not invalidate another scope's bucket."""
    counts = {"alpha": 1, "beta": 1}
    current = {"profile": "alpha"}

    def loader(source_filter=None, all_profiles=False):
        return _external_cli_rows(counts[current["profile"]])

    _install_loader(monkeypatch, loader)
    monkeypatch.setenv("HERMES_WEBUI_CLI_BADGE_TTL_SECONDS", "0")

    scope_alpha = badge_cache._cli_badge_scope(None, False, "alpha")
    scope_beta = badge_cache._cli_badge_scope(None, False, "beta")

    current["profile"] = "alpha"
    badge_cache.get_cli_sessions_for_badges(profile_key="alpha")
    current["profile"] = "beta"
    badge_cache.get_cli_sessions_for_badges(profile_key="beta")

    beta_gen = badge_cache._cli_badge_cache_generation(scope_beta)
    alpha_gen = badge_cache._cli_badge_cache_generation(scope_alpha)

    # Alpha's counts change; only alpha's generation may move.
    counts["alpha"] = 4
    current["profile"] = "alpha"
    badge_cache.get_cli_sessions_for_badges(profile_key="alpha")

    assert badge_cache._cli_badge_cache_generation(scope_alpha) > alpha_gen
    assert badge_cache._cli_badge_cache_generation(scope_beta) == beta_gen


def test_cache_is_bounded_and_never_evicts_an_in_flight_entry(monkeypatch, badge_cache):
    """Many scopes must not grow the map without limit."""
    _install_loader(
        monkeypatch,
        lambda source_filter=None, all_profiles=False: _external_cli_rows(1),
    )

    for index in range(badge_cache._CLI_BADGE_CACHE_MAX_KEYS * 2):
        badge_cache.get_cli_sessions_for_badges(profile_key=f"p{index}")

    with badge_cache._CLI_BADGE_CACHE_LOCK:
        assert len(badge_cache._CLI_BADGE_CACHE) <= badge_cache._CLI_BADGE_CACHE_MAX_KEYS
