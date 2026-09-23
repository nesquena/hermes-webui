"""Cold-start warm-up: bounded startup warm for the sidebar's first load.

Covers the post-bind warm-up in ``api/startup.py`` and the routes-side pieces it
drives:

  * ``HERMES_WEBUI_NO_WARMUP=1`` kill switch, one attempt per process;
  * the warm-up claims the ROUTE'S REAL KEY for the default sidebar query
    (the ``static/sessions.js`` ``_sessionListQueryString()`` default shape:
    ``sidebar_source=webui`` + ``exclude_hidden=1``), and nothing else;
  * an already-owned key is never re-claimed with a home-made event;
  * the claimed event is completed and the claim released (no leaked waiter);
  * the warm order (sticky/active profile, then the process default, then the
    most-recently-used remaining profiles) and the serialized per-profile budget
    (one rebuild at a time, each wait bounded to its own slice of the deadline);
  * the route's own stale-path background rebuild runs under the profile its cache
    key names, so the entry it stores is fresh for that profile;
  * failures are logged, never raised, and never leave a claim behind;
  * the models warm is the disk-only provenance helper, once, and never enters
    the live catalog rebuild path (``_available_models_cache_lock`` /
    ``_cache_build_in_progress``).
"""

import io
import json
import os
import threading
import time
from types import SimpleNamespace
from urllib.parse import urlparse

import pytest

import api.config as config
import api.profiles as profiles
import api.routes as routes
import api.startup as startup


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


@pytest.fixture(autouse=True)
def _isolated_cache_state():
    routes._session_list_cache_clear()
    with routes._SESSIONS_CACHE_LOCK:
        routes._SESSIONS_CACHE_INFLIGHT.clear()
    yield
    routes._session_list_cache_clear()
    with routes._SESSIONS_CACHE_LOCK:
        routes._SESSIONS_CACHE_INFLIGHT.clear()


_DEFAULT_SETTINGS = {
    "show_cli_sessions": False,
    "show_claude_code_sessions": True,
    "show_previous_messaging_sessions": False,
    "show_cron_sessions": False,
    "show_webhook_sessions": False,
    "show_kanban_sessions": False,
    "agent_session_source_filter": None,
}


def _rows():
    return [
        {
            "session_id": "webui-warm",
            "title": "Warmed session",
            "profile": "default",
            "archived": False,
            "message_count": 3,
            "updated_at": 2000,
            "last_message_at": 2000,
            "source": "webui",
            "raw_source": "webui",
            "session_source": "webui",
            "source_tag": "webui",
        }
    ]


def _install_route_stubs(monkeypatch):
    """Stub the heavy session sources the real builder reads."""
    calls = {"all_sessions": 0}

    def _all_sessions(diag=None, **_kwargs):
        calls["all_sessions"] += 1
        return [dict(row) for row in _rows()]

    monkeypatch.setattr(routes, "all_sessions", _all_sessions)
    monkeypatch.setattr(routes, "_reconcile_stale_stream_state_for_session_rows", lambda _rows: False)
    monkeypatch.setattr(routes, "_enrich_sidebar_lineage_metadata", lambda _rows: None)
    monkeypatch.setattr(routes, "get_cli_sessions", lambda source_filter=None, all_profiles=False: [])
    monkeypatch.setattr(routes, "agent_session_rows_existing", lambda ids, profile=None: set())
    monkeypatch.setattr(routes, "load_settings", lambda: dict(_DEFAULT_SETTINGS))
    # Mirrors server.py: the browser's hermes_profile cookie lands in the
    # thread-local, which the route's profile resolution reads first.
    monkeypatch.setattr(
        profiles, "get_active_profile_name",
        lambda: getattr(profiles._tls, "profile", None) or "default",
    )
    # Pin the warm-up's enumeration seam: tests must never depend on the
    # profiles that happen to exist on the box running them.
    monkeypatch.setattr(routes, "_warmup_profile_names", lambda: (["default"], 1))
    return calls


def _pin_warmup_profiles(monkeypatch, names, known=None):
    """Pin the profiles the warm-up enumerates (the registry seam)."""
    monkeypatch.setattr(
        routes, "_warmup_profile_names",
        lambda: (list(names), known if known is not None else len(names)),
    )


def _profile_rows(*names, tmp_path=None):
    """Registry rows (name + path) for the given profiles."""
    return [
        {"name": name, "path": str(tmp_path / name) if tmp_path is not None else None}
        for name in names
    ]


def _touch_session_store(home, *, age_seconds: float) -> None:
    """Give a profile home a ``state.db`` with a chosen age (the recency signal)."""
    home.mkdir(parents=True, exist_ok=True)
    db = home / "state.db"
    db.write_bytes(b"x")
    stamp = time.time() - age_seconds
    os.utime(db, (stamp, stamp))


def _install_slow_builder(monkeypatch, *, slow_profiles, gate):
    """Wrap the real builder: record per-profile start/end and park the slow ones.

    A slow profile's build blocks on ``gate`` (the test releases it at the end so
    no build outlives the test body); the others take a small but real amount of
    time, so a slice has something to bound.
    """
    real_build = routes._build_session_list_cache_payload
    builds: dict[str, dict] = {}

    def _build(**kwargs):
        name = kwargs.get("active_profile")
        record = builds.setdefault(name, {"start": None, "end": None, "calls": 0})
        record["start"] = time.monotonic()
        record["calls"] += 1
        if name in slow_profiles:
            gate.wait(10.0)
        else:
            time.sleep(0.02)
        record["end"] = time.monotonic()
        return real_build(**kwargs)

    monkeypatch.setattr(routes, "_build_session_list_cache_payload", _build)
    return builds


def _warm_rows(*profiles):
    return [
        {**_rows()[0], "session_id": f"webui-{name}", "profile": name}
        for name in profiles
    ]


def _route_key_for_query(query: str, settings: dict | None = None) -> tuple:
    """Build the cache key exactly as the /api/sessions route does for ``query``."""
    parsed = SimpleNamespace(query=query, path="/api/sessions")
    shape = routes._session_list_request_shape(parsed)
    key, _builder_kwargs = routes._session_list_cache_request_plan(
        settings if settings is not None else dict(_DEFAULT_SETTINGS), **shape
    )
    return key


def _default_shape_key() -> tuple:
    key, _builder_kwargs = routes._session_list_cache_request_plan(
        dict(_DEFAULT_SETTINGS), **routes._DEFAULT_SIDEBAR_REQUEST_SHAPE
    )
    return key


def _handle_sessions(url: str) -> _FakeHandler:
    handler = _FakeHandler()
    routes.handle_get(handler, urlparse(url))
    return handler


# ── startup entry point guards ────────────────────────────────────────────────

def test_start_cold_start_warmup_kill_switch_honored(monkeypatch):
    monkeypatch.setenv("HERMES_WEBUI_NO_WARMUP", "1")
    monkeypatch.setattr(startup, "_warmup_started", False)
    started = []
    monkeypatch.setattr(startup, "_run_cold_start_warmup", lambda: started.append(1))

    assert startup.start_cold_start_warmup() is None
    assert started == [], "kill switch must not start the warm-up thread"


def test_start_cold_start_warmup_one_attempt_per_process(monkeypatch):
    monkeypatch.delenv("HERMES_WEBUI_NO_WARMUP", raising=False)
    monkeypatch.setattr(startup, "_warmup_started", False)
    ran = threading.Event()
    monkeypatch.setattr(startup, "_run_cold_start_warmup", lambda: ran.set())

    first = startup.start_cold_start_warmup()
    assert first is not None
    assert ran.wait(5), "warm-up thread did not run"
    assert startup.start_cold_start_warmup() is None, "second call must not start another attempt"


# ── post-bind entry point (the one line server.py calls) ─────────────────────

def test_start_after_bind_returns_the_started_thread(monkeypatch):
    monkeypatch.delenv("HERMES_WEBUI_NO_WARMUP", raising=False)
    monkeypatch.setattr(startup, "_warmup_started", False)
    ran = threading.Event()
    monkeypatch.setattr(startup, "_run_cold_start_warmup", lambda: ran.set())

    thread = startup.start_cold_start_warmup_after_bind()
    assert thread is not None, "the post-bind entry point must start the warm-up"
    assert ran.wait(5), "warm-up thread did not run"
    assert thread.daemon is True, "the warm-up must never keep the process alive"


def test_start_after_bind_kill_switch_honored(monkeypatch):
    monkeypatch.setenv("HERMES_WEBUI_NO_WARMUP", "1")
    monkeypatch.setattr(startup, "_warmup_started", False)
    started = []
    monkeypatch.setattr(startup, "_run_cold_start_warmup", lambda: started.append(1))

    assert startup.start_cold_start_warmup_after_bind() is None
    assert started == [], "kill switch must not start the warm-up thread"


def test_start_after_bind_logs_and_swallows_a_start_failure(monkeypatch, capsys):
    """A failure to START the thread must not stop the server from serving."""
    monkeypatch.setattr(
        startup, "start_cold_start_warmup",
        lambda: (_ for _ in ()).throw(RuntimeError("thread unavailable")),
    )

    assert startup.start_cold_start_warmup_after_bind() is None  # must not raise
    out = capsys.readouterr().out
    assert "cold-start warm-up failed to start" in out
    assert "thread unavailable" in out


# ── key identity: the warm-up must fill the slot the route reads ──────────────

def test_warmup_default_shape_key_matches_the_route_key_for_the_sidebar_query():
    route_key = _route_key_for_query("sidebar_source=webui&exclude_hidden=1")
    assert _default_shape_key() == route_key, (
        "warm-up key drifted from the key the route builds for the frontend's "
        "default /api/sessions query"
    )
    # The shape is load-bearing: a different sidebar source is a different slot.
    assert _route_key_for_query("sidebar_source=cli&exclude_hidden=1") != route_key


def test_warmup_fills_the_slot_the_route_reads(monkeypatch):
    calls = _install_route_stubs(monkeypatch)

    stats = routes.warm_default_session_list_cache(wait_timeout=10.0)
    assert stats["owner"] is True
    assert stats["completed"] is True
    assert stats["status"] == "ok", "a fresh entry landed: the warm-up must report ok"
    assert stats["profiles"][0]["fresh"] is True
    assert stats["profiles"][0]["status"] == "ok"
    assert stats["key"] == _route_key_for_query("sidebar_source=webui&exclude_hidden=1")
    assert calls["all_sessions"] >= 1, "warm-up must drive the real builder once"
    warmed_calls = calls["all_sessions"]

    handler = _handle_sessions("http://example.com/api/sessions?sidebar_source=webui&exclude_hidden=1")
    assert handler.status == 200
    body = handler.json_body()
    assert [row["session_id"] for row in body["sessions"]] == ["webui-warm"]
    assert calls["all_sessions"] == warmed_calls, (
        "route rebuilt instead of reading the warmed cache entry — warm-up key "
        "does not match the route key"
    )


def test_warmup_does_not_claim_or_fill_a_wrong_shape_key(monkeypatch):
    _install_route_stubs(monkeypatch)
    stats = routes.warm_default_session_list_cache(wait_timeout=10.0)
    assert stats["completed"] is True

    wrong_key = _route_key_for_query("sidebar_source=cli&exclude_hidden=1")
    assert wrong_key != stats["key"]
    with routes._SESSIONS_CACHE_LOCK:
        assert wrong_key not in routes._SESSIONS_CACHE_INFLIGHT
    assert routes._session_list_cache_get(wrong_key, allow_stale=True) == (None, False), (
        "a wrong-shape key must not be claimed or filled by the warm-up"
    )


# ── profile coverage (P1): every profile a cookie can name gets a warm slot ──

def test_warmup_profile_names_sticky_first_then_default_then_recency(monkeypatch, tmp_path):
    """Warm order: sticky/active profile, then the process default, then the rest
    most-recently-used — the registry rows' own session-store mtime, no new store.

    The cap takes that order's prefix, so it keeps the sticky profile and the
    most-recently-used names instead of `default` + the alphabetically-first ones.
    """
    monkeypatch.setattr(profiles, "get_active_profile_name", lambda: "default")
    monkeypatch.setattr(profiles, "get_sticky_active_profile_name", lambda: "beta")
    monkeypatch.setattr(
        profiles, "list_profiles_api",
        lambda: _profile_rows(
            "default", "alpha", "beta", "gamma", "delta", tmp_path=tmp_path,
        ),
    )
    _touch_session_store(tmp_path / "alpha", age_seconds=60)
    _touch_session_store(tmp_path / "beta", age_seconds=300)
    _touch_session_store(tmp_path / "gamma", age_seconds=600)
    _touch_session_store(tmp_path / "delta", age_seconds=7200)

    names, known = routes._warmup_profile_names()
    assert names == ["beta", "default", "alpha", "gamma"], (
        "warm order must be sticky/active first, then the process default, then "
        "the most-recently-used remaining profiles — and the cap must keep those, "
        "not the alphabetically-first names"
    )
    assert known == 5, "the pre-cap count is returned so callers can log the cap"
    assert len(names) <= routes._WARMUP_MAX_PROFILES
    # The least-recently-used profile is the one the cap drops. (An alphabetical
    # cap would have warmed default, alpha, beta, delta: no sticky profile, no
    # gamma.)
    assert "delta" not in names


def test_warmup_profile_names_skips_a_sticky_name_the_registry_does_not_report(
    monkeypatch, tmp_path,
):
    """A stale ``active_profile`` must not burn one of the capped warm slots."""
    monkeypatch.setattr(profiles, "get_active_profile_name", lambda: "default")
    monkeypatch.setattr(profiles, "get_sticky_active_profile_name", lambda: "ghost")
    monkeypatch.setattr(
        profiles, "list_profiles_api",
        lambda: _profile_rows("default", "alpha", tmp_path=tmp_path),
    )

    names, known = routes._warmup_profile_names()
    assert names == ["default", "alpha"]
    assert known == 2


def test_warmup_profile_names_default_first_deduped_and_capped(monkeypatch):
    monkeypatch.setattr(profiles, "get_active_profile_name", lambda: "default")
    monkeypatch.setattr(profiles, "get_sticky_active_profile_name", lambda: "default")
    rows = [{"name": "default"}, {"name": "default"}] + [
        {"name": f"p{i}"} for i in range(1, 8)
    ]
    monkeypatch.setattr(profiles, "list_profiles_api", lambda: rows)

    names, known = routes._warmup_profile_names()
    assert names[0] == "default", "the process default must be warmed first"
    assert names == ["default", "p1", "p2", "p3"], (
        "enumeration must be bounded to _WARMUP_MAX_PROFILES and deduped"
    )
    assert len(names) <= routes._WARMUP_MAX_PROFILES
    assert known == 8, "the pre-cap count is returned so callers can log the cap"


def test_warmup_profile_names_fall_back_to_the_process_default(monkeypatch):
    monkeypatch.setattr(profiles, "get_active_profile_name", lambda: "rooty")
    monkeypatch.setattr(profiles, "get_sticky_active_profile_name", lambda: "ghost")
    monkeypatch.setattr(
        profiles, "list_profiles_api",
        lambda: (_ for _ in ()).throw(RuntimeError("registry unavailable")),
    )

    names, known = routes._warmup_profile_names()
    assert names == ["rooty"], "a failed enumeration must still warm the process default"
    assert known == 1


def test_warmup_fills_the_slot_a_non_default_cookie_profile_reads(monkeypatch):
    """The warm-up runs with no request context, so it must warm EVERY profile a
    ``hermes_profile`` cookie can name — not only the process default."""
    _install_route_stubs(monkeypatch)
    _pin_warmup_profiles(monkeypatch, ["default", "alpha"])

    rows = [
        {**_rows()[0], "session_id": "webui-default", "profile": "default"},
        {**_rows()[0], "session_id": "webui-alpha", "profile": "alpha"},
    ]
    calls = {"all_sessions": 0}

    def _all_sessions(diag=None, **_kwargs):
        calls["all_sessions"] += 1
        return [dict(row) for row in rows]

    monkeypatch.setattr(routes, "all_sessions", _all_sessions)

    stats = routes.warm_default_session_list_cache(wait_timeout=10.0)
    assert {entry["profile"] for entry in stats["profiles"]} == {"default", "alpha"}
    assert all(entry["completed"] for entry in stats["profiles"])
    assert stats["capped"] is False

    # The key the route builds for a request carrying the alpha cookie is the
    # key the warm-up filled, so that request reads the warmed slot.
    profiles.set_request_profile("alpha")
    try:
        alpha_key = _route_key_for_query("sidebar_source=webui&exclude_hidden=1")
        assert any(entry["key"] == alpha_key for entry in stats["profiles"])
        payload, fresh = routes._session_list_cache_get(alpha_key, allow_stale=True)
        assert payload is not None and fresh, "the alpha-profile slot was not warmed"
        assert [row["session_id"] for row in payload["sessions"]] == ["webui-alpha"]

        warmed_calls = calls["all_sessions"]
        handler = _handle_sessions(
            "http://example.com/api/sessions?sidebar_source=webui&exclude_hidden=1"
        )
    finally:
        profiles.clear_request_profile()

    assert handler.status == 200
    assert [row["session_id"] for row in handler.json_body()["sessions"]] == ["webui-alpha"]
    assert calls["all_sessions"] == warmed_calls, (
        "the alpha-profile request rebuilt instead of reading the warmed slot"
    )


def test_warmup_reports_when_the_profile_cap_was_hit(monkeypatch):
    _install_route_stubs(monkeypatch)
    _pin_warmup_profiles(monkeypatch, ["default", "p1"], known=9)

    stats = routes.warm_default_session_list_cache(wait_timeout=10.0)
    assert stats["capped"] is True
    assert stats["profiles_known"] == 9
    assert stats["profiles_considered"] == 2


def test_warmup_respects_an_existing_claim_without_a_home_made_event(monkeypatch):
    _install_route_stubs(monkeypatch)
    key = _default_shape_key()
    event, is_owner = routes._session_list_cache_claim_rebuild(key)
    assert is_owner

    started = []
    monkeypatch.setattr(
        routes, "_start_session_list_cache_background_rebuild", lambda *a, **k: started.append(a)
    )
    try:
        stats = routes.warm_default_session_list_cache(wait_timeout=0.05)
        assert stats["owner"] is False, "an already-owned key must not be re-claimed"
        assert stats["status"] == "skipped", (
            "a key owned by another rebuild is neither ok nor failed — it was skipped"
        )
        assert stats["profiles"][0]["status"] == "skipped"
        assert started == [], "no second rebuild may be started for an owned key"
        assert routes._SESSIONS_CACHE_INFLIGHT.get(key) is event, (
            "the warm-up must not replace or drop the registered claim event"
        )
    finally:
        routes._session_list_cache_done(key, event)


def test_warmup_completes_the_claimed_event_and_releases_the_claim(monkeypatch):
    _install_route_stubs(monkeypatch)
    captured = {}
    real_claim = routes._session_list_cache_claim_rebuild

    def _claim(key):
        event, is_owner = real_claim(key)
        captured["event"] = event
        captured["key"] = key
        return event, is_owner

    monkeypatch.setattr(routes, "_session_list_cache_claim_rebuild", _claim)
    stats = routes.warm_default_session_list_cache(wait_timeout=10.0)
    assert stats["completed"] is True
    assert captured["event"].is_set(), "waiters must be released: the claimed event is never set"
    with routes._SESSIONS_CACHE_LOCK:
        assert captured["key"] not in routes._SESSIONS_CACHE_INFLIGHT, (
            "the claim must be released after the rebuild"
        )


# ── the route's own stale-path background rebuild (B-R1) ─────────────────────

def _wait_for_claim_release(key, timeout: float = 10.0) -> bool:
    """Wait until no rebuild claim is registered for ``key``."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with routes._SESSIONS_CACHE_LOCK:
            if key not in routes._SESSIONS_CACHE_INFLIGHT:
                return True
        time.sleep(0.01)
    return False


def test_stale_route_rebuild_stores_under_the_key_profile(monkeypatch):
    """The route's stale-path background rebuild must run the WHOLE rebuild under
    the profile the cache key names (``key[0]``).

    Otherwise the rebuild — a non-default profile's key — reads and stamps the
    process default's home, so the entry it stores is ``source``-stale for the
    profile that key belongs to and the slot is thrown away on first read: the
    rebuild is wasted and the request that triggered it still pays a full build.
    Asserts the stored stamp / resolved home, not the helper's kwarg.
    """
    _install_route_stubs(monkeypatch)
    cache = routes._route_session_list_cache
    real_build = routes._build_session_list_cache_payload
    seen: list[dict] = []

    def _recording_build(**kwargs):
        seen.append({
            "profile": profiles.get_active_profile_name(),
            "home": str(profiles.get_active_hermes_home()),
        })
        return real_build(**kwargs)

    monkeypatch.setattr(routes, "_build_session_list_cache_payload", _recording_build)

    # The key a request carrying the alpha cookie builds, plus the alpha-resolved
    # source stamp — the stamp that request will compare the entry against.
    alpha_key, alpha_kwargs = routes._session_list_cache_request_plan(
        dict(_DEFAULT_SETTINGS), active_profile="alpha",
        **routes._DEFAULT_SIDEBAR_REQUEST_SHAPE,
    )
    assert alpha_key[0] == "alpha", "precondition: the key's first element is the profile"

    profiles.set_request_profile("alpha")
    try:
        alpha_stamp = routes._session_list_cache_source_stamp(alpha_key)
        alpha_home = str(profiles.get_active_hermes_home())
        routes._session_list_cache_set(
            alpha_key, {"sessions": []},
            expected_invalidation_stamp=routes._session_list_cache_invalidation_stamp(alpha_key),
        )
    finally:
        profiles.clear_request_profile()

    # The default context stamps this same key differently — that is the stamp the
    # pre-fix rebuild stored (and why the slot was invalidated on first read).
    default_stamp = routes._session_list_cache_source_stamp(alpha_key)
    assert default_stamp != alpha_stamp, (
        "precondition: the profile-resolved source stamp must differ from the "
        "process default's for this assertion to mean anything"
    )

    # Expire the TTL only: the entry stays stamp-fresh, so the route takes its
    # stale-serve path (serve stale + rebuild in the background). Everything below
    # runs under the alpha thread-local, exactly like a request carrying the
    # hermes_profile cookie.
    monkeypatch.setattr(cache, "_SESSIONS_CACHE_TTL_SECONDS", 0.0)

    profiles.set_request_profile("alpha")
    try:
        assert routes._session_list_cache_stale_reason(alpha_key) == "age"
        stale = routes._get_cached_session_list_payload(
            key=alpha_key,
            builder=lambda: routes._build_session_list_cache_payload(**alpha_kwargs),
        )
        assert stale == {"sessions": []}, "the stale payload is served while the rebuild runs"
        assert _wait_for_claim_release(alpha_key), "the background rebuild never released its claim"
        with routes._SESSIONS_CACHE_LOCK:
            _ts, stored_stamp, _payload = routes._SESSIONS_CACHE.get(alpha_key, (None, None, None))
        monkeypatch.setattr(cache, "_SESSIONS_CACHE_TTL_SECONDS", 2.5)
        payload, fresh = routes._session_list_cache_get(alpha_key, allow_stale=False)
    finally:
        profiles.clear_request_profile()

    assert seen == [{"profile": "alpha", "home": alpha_home}], (
        "the stale-path rebuild ran outside the key's profile context"
    )
    assert stored_stamp == alpha_stamp and stored_stamp != default_stamp, (
        "the rebuilt entry was stamped with the process default's home — the "
        "alpha slot is source-stale and the rebuild is wasted"
    )
    assert fresh is True and payload is not None, (
        "the rebuilt entry must be fresh for the profile that key belongs to"
    )


# ── serialized rebuilds with a per-profile slice (B-R2) ──────────────────────

def test_warmup_serializes_rebuilds_and_keeps_the_first_slot_warm(monkeypatch):
    """One profile at a time, in warm order: a later profile that exhausts the
    deadline cannot cost the first (sticky) profile its slot.

    Before: every profile's rebuild was launched up front and waited on against
    ONE shared deadline, so on a loaded box the builds contended with each other
    (and with the first real request) and everything behind a slow profile timed
    out. Now the sticky profile is warmed first, alone, and a slow profile burns
    only its own slice of what is left.
    """
    _install_route_stubs(monkeypatch)
    _pin_warmup_profiles(monkeypatch, ["alpha", "default", "beta"])
    rows = _warm_rows("alpha", "default", "beta")
    monkeypatch.setattr(
        routes, "all_sessions", lambda diag=None, **_kw: [dict(r) for r in rows]
    )
    gate = threading.Event()
    builds = _install_slow_builder(monkeypatch, slow_profiles={"beta"}, gate=gate)

    stats = routes.warm_default_session_list_cache(wait_timeout=1.2)
    try:
        assert [entry["profile"] for entry in stats["profiles"]] == ["alpha", "default", "beta"], (
            "the warm-up must warm in the order it enumerates (sticky/active first)"
        )
        assert [entry["status"] for entry in stats["profiles"]] == ["ok", "ok", "timeout"]
        assert stats["status"] == "timeout", "a timed-out profile must be reported, not hidden"
        assert stats["profiles_warmed"] == 2
        assert stats["profiles_considered"] == 3
        # Serialized: each build started only after the previous profile settled.
        assert builds["alpha"]["end"] <= builds["default"]["start"]
        assert builds["default"]["end"] <= builds["beta"]["start"]
        assert builds["beta"]["calls"] == 1, "a timed-out profile must not be rebuilt"
        # The budget the two fast profiles left unused went to the slow one.
        assert stats["profiles"][2]["slice_seconds"] > 0.5

        # The sticky profile's slot is the one a cookie request reads — no rebuild.
        profiles.set_request_profile("alpha")
        try:
            alpha_key = _route_key_for_query("sidebar_source=webui&exclude_hidden=1")
            assert any(entry["key"] == alpha_key for entry in stats["profiles"])
            warmed_calls = builds["alpha"]["calls"]
            handler = _handle_sessions(
                "http://example.com/api/sessions?sidebar_source=webui&exclude_hidden=1"
            )
        finally:
            profiles.clear_request_profile()
        assert handler.status == 200
        assert [row["session_id"] for row in handler.json_body()["sessions"]] == ["webui-alpha"]
        assert builds["alpha"]["calls"] == warmed_calls, (
            "the sticky profile's request rebuilt instead of reading the warmed slot"
        )
    finally:
        gate.set()
        assert _wait_for_claim_release(stats["profiles"][2]["key"]), (
            "the parked slow build never released its claim"
        )


def test_warmup_slow_first_profile_does_not_starve_the_rest(monkeypatch):
    """Each profile's wait is bounded to its OWN slice of the deadline, so a slow
    first profile cannot consume the window the profiles behind it need."""
    _install_route_stubs(monkeypatch)
    _pin_warmup_profiles(monkeypatch, ["alpha", "default", "beta"])
    rows = _warm_rows("alpha", "default", "beta")
    monkeypatch.setattr(
        routes, "all_sessions", lambda diag=None, **_kw: [dict(r) for r in rows]
    )
    gate = threading.Event()
    _install_slow_builder(monkeypatch, slow_profiles={"alpha"}, gate=gate)

    stats = routes.warm_default_session_list_cache(wait_timeout=1.2)
    try:
        assert [entry["status"] for entry in stats["profiles"]] == ["timeout", "ok", "ok"]
        assert stats["profiles_warmed"] == 2
        # 1.2 s over 3 profiles: the first slice is ~0.4 s (its own share, not the
        # whole window), and the profiles behind it get real slices of their own.
        assert stats["profiles"][0]["slice_seconds"] <= 0.45
        assert stats["profiles"][1]["slice_seconds"] >= 0.3

        profiles.set_request_profile("beta")
        try:
            beta_key = _route_key_for_query("sidebar_source=webui&exclude_hidden=1")
            payload, fresh = routes._session_list_cache_get(beta_key, allow_stale=True)
        finally:
            profiles.clear_request_profile()
        assert fresh is True and payload is not None, (
            "the profiles behind the slow one must still be warmed inside the deadline"
        )
        assert [row["session_id"] for row in payload["sessions"]] == ["webui-beta"]
    finally:
        gate.set()
        assert _wait_for_claim_release(stats["profiles"][0]["key"]), (
            "the parked slow build never released its claim"
        )


def test_cold_start_warmup_logs_the_warm_order_and_the_honest_outcome(monkeypatch, capsys):
    """The [warmup] line carries the warm order plus the real per-profile outcome."""
    _install_route_stubs(monkeypatch)
    _stub_models_warm(monkeypatch)
    _pin_warmup_profiles(monkeypatch, ["alpha", "default", "beta"])
    rows = _warm_rows("alpha", "default", "beta")
    monkeypatch.setattr(
        routes, "all_sessions", lambda diag=None, **_kw: [dict(r) for r in rows]
    )
    gate = threading.Event()
    _install_slow_builder(monkeypatch, slow_profiles={"beta"}, gate=gate)
    monkeypatch.setattr(startup, "_WARMUP_SESSION_WAIT_SECONDS", 1.2)

    stats = startup._run_cold_start_warmup()
    out = capsys.readouterr().out
    gate.set()
    assert _wait_for_claim_release(stats["session_list"]["profiles"][2]["key"])

    assert stats["session_list"]["status"] == "timeout"
    assert "session_list=timeout" in out
    assert "session_list=ok" not in out
    assert "profiles=2/3" in out
    assert "order=alpha,default,beta" in out


# ── failure containment ──────────────────────────────────────────────────────

def test_warmup_builder_failure_does_not_raise_and_releases_the_claim(monkeypatch):
    _install_route_stubs(monkeypatch)
    monkeypatch.setattr(
        routes, "_build_session_list_cache_payload",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("builder exploded")),
    )
    stats = routes.warm_default_session_list_cache(wait_timeout=10.0)
    assert stats["error"] is None
    assert stats["completed"] is True, "the rebuild's finally must still complete the event"
    # ...but a signaled event is NOT success: the slot must hold a fresh entry.
    assert stats["status"] == "failed", (
        "a failed rebuild must not be reported as ok — the first request is still cold"
    )
    assert stats["profiles"][0]["fresh"] is False
    assert stats["profiles"][0]["status"] == "failed"
    with routes._SESSIONS_CACHE_LOCK:
        assert stats["key"] not in routes._SESSIONS_CACHE_INFLIGHT


def test_warmup_plan_failure_does_not_raise_and_claims_nothing(monkeypatch):
    monkeypatch.setattr(
        routes, "load_settings",
        lambda: (_ for _ in ()).throw(RuntimeError("settings unavailable")),
    )
    stats = routes.warm_default_session_list_cache(wait_timeout=0.01)
    assert stats["owner"] is False
    assert stats["error"] and "settings unavailable" in stats["error"]
    with routes._SESSIONS_CACHE_LOCK:
        assert not routes._SESSIONS_CACHE_INFLIGHT, "a failed plan must not claim a key"


# ── models disk-cache warm (B2: disk-only, never the catalog rebuild) ────────

def _stub_session_warm(monkeypatch, status="ok"):
    monkeypatch.setattr(
        routes, "warm_default_session_list_cache",
        lambda **_kw: {"owner": True, "completed": status == "ok", "status": status,
                       "elapsed_ms": 1, "error": None, "key": None,
                       "profiles": [], "profiles_warmed": 1, "profiles_considered": 1,
                       "profiles_known": 1, "capped": False},
    )


def _stub_models_warm(monkeypatch):
    monkeypatch.setattr(config, "warm_models_catalog_provenance_if_cold", lambda: None)


def test_cold_start_warmup_logs_ok_only_when_a_fresh_entry_landed(monkeypatch, capsys):
    """The [warmup] line must reflect the cache outcome, not just the event."""
    _install_route_stubs(monkeypatch)
    _stub_models_warm(monkeypatch)

    stats = startup._run_cold_start_warmup()
    out = capsys.readouterr().out
    assert stats["session_list"]["status"] == "ok"
    assert "session_list=ok" in out
    assert "profiles=1/1" in out


def test_cold_start_warmup_logs_failed_when_no_fresh_entry_landed(monkeypatch, capsys):
    """A builder exception still signals the claim event; the log must not say ok."""
    _install_route_stubs(monkeypatch)
    _stub_models_warm(monkeypatch)
    monkeypatch.setattr(
        routes, "_build_session_list_cache_payload",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("builder exploded")),
    )

    stats = startup._run_cold_start_warmup()
    out = capsys.readouterr().out
    assert stats["session_list"]["status"] == "failed"
    assert "session_list=failed" in out
    assert "session_list=ok" not in out


def test_cold_start_warmup_logs_the_profile_cap(monkeypatch, capsys):
    _install_route_stubs(monkeypatch)
    _stub_models_warm(monkeypatch)
    monkeypatch.setattr(
        routes, "warm_default_session_list_cache",
        lambda **_kw: {"owner": True, "completed": True, "status": "ok",
                       "elapsed_ms": 1, "error": None, "key": None,
                       "profiles": [], "profiles_warmed": 4, "profiles_considered": 4,
                       "profiles_known": 9, "capped": True},
    )

    stats = startup._run_cold_start_warmup()
    out = capsys.readouterr().out
    assert stats["session_list"]["status"] == "ok"
    assert "profiles=4/4" in out
    assert "capped of 9 known" in out


def test_cold_start_warmup_warms_models_provenance_from_disk_once(monkeypatch):
    calls = []
    monkeypatch.setattr(config, "warm_models_catalog_provenance_if_cold", lambda: calls.append(1))

    def _boom(*_a, **_k):
        raise AssertionError("warm-up must never enter the live models catalog rebuild path")

    # get_available_models can hold _available_models_cache_lock +
    # _cache_build_in_progress for up to 60s; the warm-up must not touch it.
    monkeypatch.setattr(config, "get_available_models", _boom)
    _stub_session_warm(monkeypatch)

    stats = startup._run_cold_start_warmup()
    assert calls == [1], "models provenance must be warmed exactly once per warm-up"
    assert stats["models_provenance"]["status"] == "ok"
    assert stats["session_list"]["status"] == "ok"


def test_cold_start_warmup_models_warm_never_blocks_on_the_catalog_lock(monkeypatch):
    """Bounded: the disk warm takes the models lock non-blocking, so a held lock
    (a concurrent live rebuild) must not delay the warm-up."""
    old_prov = config._models_cache_provenance
    config._models_cache_provenance = None
    got = config._available_models_cache_lock.acquire(blocking=False)
    assert got, "precondition: could not take the models cache lock"
    try:
        _stub_session_warm(monkeypatch)
        started = time.monotonic()
        stats = startup._run_cold_start_warmup()
        elapsed = time.monotonic() - started
    finally:
        config._available_models_cache_lock.release()
        config._models_cache_provenance = old_prov
    assert elapsed < 2.0, f"models warm blocked on a held catalog lock ({elapsed:.2f}s)"
    assert stats["models_provenance"]["status"] == "ok"


def test_cold_start_warmup_models_failure_is_logged_not_raised(monkeypatch):
    monkeypatch.setattr(
        config, "warm_models_catalog_provenance_if_cold",
        lambda: (_ for _ in ()).throw(RuntimeError("disk warm failed")),
    )
    _stub_session_warm(monkeypatch)

    stats = startup._run_cold_start_warmup()  # must not raise
    assert "failed" in stats["models_provenance"]["status"]
    assert "disk warm failed" in stats["models_provenance"]["status"]
    assert stats["session_list"]["status"] == "ok", (
        "a models failure must not skip the session-list warm"
    )
