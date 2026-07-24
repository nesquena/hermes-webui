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
