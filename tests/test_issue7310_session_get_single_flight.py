"""#7310 — identical concurrent GET /api/session reloads must share one projection.

A reconnect storm fans out N identical session reloads at the same moment
(the browser refetches after every dropped socket, plus one reload per live
stream). Before this fix every one of them rebuilt the same *public*
projection from scratch — state.db rows, append-only merge, compact,
regeneration authority and the redaction pass — so the burst cost N times a
single read and the request threads stacked up faster than they drained.

The tests drive the real handler (real sidecar stat signature, real
state.db session signature, real window build) from several threads at once
and assert how many projections were actually built, plus the identity and
release rules that keep the coalescing fail-closed.
"""

import json
import sqlite3
import threading
import time
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlparse

import pytest

SESSION_ID = "sflight_001"
SAME_WINDOW = f"session_id={SESSION_ID}&messages=1&resolve_model=0&msg_limit=1"
OTHER_WINDOW = (
    f"session_id={SESSION_ID}&messages=1&resolve_model=0&msg_limit=1&msg_before=1"
)


class _FakeSession:
    def __init__(self, messages, sid=SESSION_ID):
        self.session_id = sid
        self.title = "Single flight"
        self.workspace = "/tmp"
        self.model = "gpt-test"
        self.model_provider = None
        self.messages = messages
        self.tool_calls = []
        self.input_tokens = 0
        self.output_tokens = 0
        self.estimated_cost = 0
        self.context_length = 1
        self.threshold_tokens = 0
        self.last_prompt_tokens = 0
        self.active_stream_id = None
        self.pending_user_message = None
        self.pending_attachments = []
        self.pending_started_at = None
        self.composer_draft = {}

    def compact(self):
        return {
            "session_id": self.session_id,
            "title": self.title,
            "workspace": self.workspace,
            "model": self.model,
            "model_provider": self.model_provider,
            "message_count": len(self.messages),
            "context_length": self.context_length,
            "threshold_tokens": self.threshold_tokens,
            "last_prompt_tokens": self.last_prompt_tokens,
            "active_stream_id": self.active_stream_id,
            "pending_user_message": self.pending_user_message,
            "composer_draft": self.composer_draft,
        }


@pytest.fixture()
def isolated_home(tmp_path, monkeypatch):
    """Isolate every path the handler and the generation key resolve."""
    import api.config as config
    import api.models as models
    import api.routes as routes

    home = tmp_path / "home"
    session_dir = home / "session"
    state_dir = tmp_path / "state"
    home.mkdir(parents=True)
    session_dir.mkdir(parents=True)
    state_dir.mkdir(parents=True)

    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_CONFIG", str(tmp_path / "config"))
    monkeypatch.setenv("HERMES_CACHE", str(tmp_path / "cache"))
    monkeypatch.setenv("HERMES_SESSIONS_DIR", str(tmp_path / "sessions"))
    monkeypatch.setattr(routes, "SESSION_DIR", session_dir, raising=False)
    monkeypatch.setattr(config, "STATE_DIR", state_dir, raising=False)
    monkeypatch.setattr(models, "STATE_DIR", state_dir, raising=False)

    # The generation key reads this session's sidecar stat, so the sidecar has
    # to exist exactly as it would for a real session.
    sidecar = session_dir / f"{SESSION_ID}.json"
    sidecar.write_text('{"session_id": "%s", "messages": []}' % SESSION_ID)

    # Own state.db with the schema the session revision query understands.
    # Pinned explicitly so the test never resolves the real profile's DB.
    state_db = tmp_path / "state.db"
    with sqlite3.connect(state_db) as conn:
        conn.execute(
            "CREATE TABLE sessions (id TEXT PRIMARY KEY, updated_at REAL)"
        )
        conn.execute(
            "CREATE TABLE messages ("
            "id INTEGER PRIMARY KEY, session_id TEXT, role TEXT,"
            " content TEXT, timestamp REAL)"
        )
        conn.commit()
    monkeypatch.setattr(
        models, "_agent_state_db_path", lambda *, profile=None: state_db
    )
    # Module caches outlive a single test otherwise, and every test here uses
    # the same session id, so a warm entry from an earlier test would fake a
    # verified parent generation (or a flight) for the next one.
    routes._lineage_display_cache.clear()
    routes._SESSION_GET_FLIGHTS.clear()
    return session_dir


class _CountingRedact:
    """Stand in for the redaction stage and record each projection built."""

    def __init__(self, delay=0.3, fail_first=False):
        self.delay = delay
        self.fail_first = fail_first
        self.calls = 0
        self._lock = threading.Lock()

    def __call__(self, raw):
        with self._lock:
            self.calls += 1
            is_first = self.calls == 1
        if self.delay:
            time.sleep(self.delay)
        if self.fail_first and is_first:
            raise RuntimeError("projection build failed")
        return raw


def _run_concurrent(queries, redact):
    """Fire every query at the real handler simultaneously; return results."""
    import api.routes as routes

    session = _FakeSession(
        [
            {"role": "user", "content": "older"},
            {"role": "assistant", "content": "visible"},
            {"role": "assistant", "content": "third"},
            {"role": "assistant", "content": "fourth"},
        ]
    )
    results = {}
    errors = []
    lock = threading.Lock()
    barrier = threading.Barrier(len(queries))

    def fake_j(_handler, data, status=200, extra_headers=None):
        with lock:
            results[threading.get_ident()] = (status, data)
        return data

    def worker(query):
        parsed = urlparse(f"/api/session?{query}")
        try:
            barrier.wait(timeout=10)
            routes.handle_get(SimpleNamespace(), parsed)
        except BaseException as exc:  # noqa: BLE001 - surfaced through `errors`
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=worker, args=(q,)) for q in queries]
    with patch(
        "api.routes.get_session", return_value=session
    ), patch(
        "api.routes._clear_stale_stream_state", return_value=False
    ), patch(
        "api.routes._lookup_cli_session_metadata", return_value={}
    ), patch(
        "api.routes.get_state_db_session_messages", return_value=[]
    ), patch(
        "api.routes.redact_session_data", side_effect=redact
    ), patch(
        "api.routes.j", side_effect=fake_j
    ):
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

    assert not any(thread.is_alive() for thread in threads), (
        "concurrent /api/session reads did not finish"
    )
    return results, errors, session


def test_identical_concurrent_session_reads_build_one_projection(isolated_home):
    redact = _CountingRedact()
    results, errors, session = _run_concurrent(
        [SAME_WINDOW] * 8, redact
    )

    assert errors == [], errors
    assert len(results) == 8, "every reader must still receive a payload"
    # The whole point of #7310: eight identical reloads must not each rebuild
    # the same window. Before the fix this counter reached 8.
    assert redact.calls == 1, (
        f"expected one shared projection, built {redact.calls}"
    )

    payloads = [data for _status, data in results.values()]
    assert all(data == payloads[0] for data in payloads)
    assert payloads[0]["session"]["messages"] == [session.messages[-1]]


def test_different_window_reads_are_not_coalesced(isolated_home):
    redact = _CountingRedact()
    results, errors, _session = _run_concurrent(
        [SAME_WINDOW] * 4 + [OTHER_WINDOW] * 4, redact
    )

    assert errors == [], errors
    assert len(results) == 8
    # Different pagination/window is a different public projection: it must
    # get its own leader, never the other window's result.
    assert redact.calls == 2, (
        f"expected one projection per distinct window, built {redact.calls}"
    )


def test_leader_failure_releases_waiters_and_registry(isolated_home):
    import api.routes as routes

    # The leading build raises after the seven followers have joined it. They
    # must fall through to their own build instead of waiting on a payload
    # nobody will ever publish, and the leader entry must not outlive the
    # handler.
    redact = _CountingRedact(delay=0.2, fail_first=True)
    results, errors, _session = _run_concurrent([SAME_WINDOW] * 8, redact)

    assert len(errors) == 1, errors
    assert len(results) == 7, "waiters must fall through and build their own"
    assert routes._SESSION_GET_FLIGHTS == {}, (
        "flight registry leaked a leader entry"
    )


def test_flight_registry_is_empty_after_identical_reads(isolated_home):
    import api.routes as routes

    results, errors, _session = _run_concurrent(
        [SAME_WINDOW] * 4, _CountingRedact()
    )
    assert errors == [], errors
    assert len(results) == 4
    assert routes._SESSION_GET_FLIGHTS == {}


def test_session_get_flight_key_scopes_by_shape_and_generation(isolated_home):
    import api.routes as routes

    session = _FakeSession([{"role": "user", "content": "hi"}])
    key = routes._session_get_flight_key(session, None, {}, ("1",))
    assert key is not None

    # Pagination/window is part of the identity.
    assert routes._session_get_flight_key(
        session, None, {}, ("2",)
    ) != key
    assert routes._session_get_flight_key(
        session, "other-profile", {}, ("1",)
    ) != key

    # Stream ownership is part of the identity.
    session.active_stream_id = "stream-1"
    assert routes._session_get_flight_key(
        session, None, {}, ("1",)
    ) != key
    session.active_stream_id = None

    # CLI metadata merged into the payload is part of the identity.
    assert routes._session_get_flight_key(
        session, None, {"message_count": 3}, ("1",)
    ) != key

    # Sidecar generation is part of the identity.
    sidecar = routes.SESSION_DIR / f"{SESSION_ID}.json"
    sidecar.write_text(
        '{"session_id": "%s", "messages": [], "mtime": 1}' % SESSION_ID
    )
    key_after_sidecar_write = routes._session_get_flight_key(
        session, None, {}, ("1",)
    )
    assert key_after_sidecar_write != key

    # An unresolvable generation fails closed instead of sharing a result.
    sidecar.unlink()
    assert (
        routes._session_get_flight_key(session, None, {}, ("1",)) is None
    )


def test_run_journal_growth_changes_the_flight_key(isolated_home):
    """A live run appending journal rows must not be served from an earlier
    in-flight build (#7310 review: live run updates go missing)."""
    import api.routes as routes

    session = _FakeSession([{"role": "user", "content": "hi"}])
    key_before = routes._session_get_flight_key(session, None, {}, ("1",))
    assert key_before is not None

    # A run writes its first journal row while the leader is still building.
    journal_dir = routes.SESSION_DIR / "_run_journal" / SESSION_ID
    journal_dir.mkdir(parents=True, exist_ok=True)
    (journal_dir / "run-1.jsonl").write_text('{"event": "tool"}\n')

    key_after = routes._session_get_flight_key(session, None, {}, ("1",))
    assert key_after is not None
    assert key_after != key_before, (
        "journal growth must change the flight key"
    )


def test_active_stream_without_a_visible_journal_fails_closed(isolated_home):
    """The journal contributes to the payload, so a run whose file is not under
    this session's own journal directory cannot be fingerprinted — refuse to
    share rather than hand out a projection built before it moved."""
    import api.routes as routes

    session = _FakeSession([{"role": "user", "content": "hi"}])
    session.active_stream_id = "run_elsewhere"
    assert routes._session_get_flight_key(session, None, {}, ("1",)) is None


def test_parent_sidecar_generation_guards_the_flight_key(isolated_home):
    """Compression-snapshot parents are stitched into the transcript, so a
    flight may only be shared while those parents are provably unchanged
    (#7310 review: parent transcript updates go missing)."""
    import api.routes as routes
    from api.models import _sidecar_stat_signature

    session = _FakeSession([{"role": "user", "content": "hi"}])
    session.parent_session_id = "sflight_parent"

    # Cold lineage cache: nothing proves what the parent chain looked like, so
    # sharing would be a guess.
    assert routes._session_get_flight_key(session, None, {}, ("1",)) is None

    # Warm cache: the lineage stitch already recorded child + parent stats.
    parent_path = routes.SESSION_DIR / "sflight_parent.json"
    parent_path.write_text('{"session_id": "sflight_parent", "messages": []}')
    routes._lineage_display_cache[SESSION_ID] = {
        "messages": [],
        "self_sig": _sidecar_stat_signature(
            routes.SESSION_DIR / f"{SESSION_ID}.json"
        ),
        "parent_sigs": [(str(parent_path), _sidecar_stat_signature(parent_path))],
        "provenance_complete": True,
    }
    key = routes._session_get_flight_key(session, None, {}, ("1",))
    assert key is not None

    # A rewritten parent transcript must invalidate it.
    parent_path.write_text(
        '{"session_id": "sflight_parent", "messages": ["rewritten"]}'
    )
    key_after_parent_write = routes._session_get_flight_key(
        session, None, {}, ("1",)
    )
    assert key_after_parent_write != key, (
        "a parent sidecar rewrite must change the flight key"
    )

    # An incomplete provenance record proves nothing: fail closed.
    routes._lineage_display_cache[SESSION_ID] = {
        "messages": [],
        "self_sig": _sidecar_stat_signature(
            routes.SESSION_DIR / f"{SESSION_ID}.json"
        ),
        "parent_sigs": [(str(parent_path), _sidecar_stat_signature(parent_path))],
        "provenance_complete": False,
    }
    assert routes._session_get_flight_key(session, None, {}, ("1",)) is None


class _GatedRedact:
    """Parks the FIRST projection build until the test releases it.

    Lets a test overlap other work with a flight that is provably in flight
    (claimed, past ``compact()``, still building) instead of guessing with
    sleeps.
    """

    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()
        self.calls = 0
        self._lock = threading.Lock()

    def __call__(self, raw):
        with self._lock:
            self.calls += 1
            is_first = self.calls == 1
        if is_first:
            self.entered.set()
            assert self.release.wait(timeout=30), "leader build was never released"
        return raw


class _UpdatableSession(_FakeSession):
    """Cached session whose ``save()`` can be held open mid-update.

    ``/api/session/update`` mutates the cached Session under the per-session
    lock and only then calls ``save()``. Holding ``save()`` open reproduces
    exactly the window the gate review describes: a GET can observe the
    already-mutated object while the sidecar still reads as the previous
    generation.
    """

    def __init__(self, messages, session_dir, sid=SESSION_ID, block_save=True):
        super().__init__(messages, sid=sid)
        self._session_dir = session_dir
        self.save_entered = threading.Event()
        self.release_save = threading.Event()
        self.save_count = 0
        if not block_save:
            self.release_save.set()

    def save(self, *args, **kwargs):
        self.save_entered.set()
        assert self.release_save.wait(timeout=30), "save() was never released"
        with self._lock_free():
            self.save_count += 1
        (self._session_dir / f"{self.session_id}.json").write_text(
            json.dumps(
                {
                    "session_id": self.session_id,
                    "model": self.model,
                    "messages": self.messages,
                }
            )
        )

    class _lock_free:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False


class _SessionRouteEnv:
    """Installs every route collaborator stub once, around the whole test.

    ``unittest.mock.patch`` mutates *module globals*, so per-thread patch stacks
    race one another: the first worker's ``ExitStack`` exit restores the real
    callable while a second worker is still inside the route. The stubs
    therefore live in the test body, and the worker threads below only call the
    real handlers.
    """

    def __init__(self, session, redact):
        self.session = session
        self.redact = redact
        self.capture = {}
        self.errors = []
        self.flight_keys = []
        self._lock = threading.Lock()
        self._stack = None

    # -- stubs ---------------------------------------------------------------
    def fake_j(self, _handler, data, status=200, extra_headers=None):
        ident = threading.get_ident()
        with self._lock:
            self.capture[ident] = (status, data)
        return data

    def apply_model(self, sess, *, model, provider=None, provider_specified=True,
                    already_resolved=False):
        # Stand-in for the real normalizer. The contract under test is
        # "mutate the cached Session under the lock, then save()".
        if model is not None:
            sess.model = model
        return True

    def record_flight_key(self, *args, **kwargs):
        key = self._real_key(*args, **kwargs)
        with self._lock:
            self.flight_keys.append((threading.get_ident(), key))
        return key

    # -- optional instrumentation -------------------------------------------
    def track_claims(self, on_join):
        """Invoke ``on_join(ident, flight)`` for every non-leader claim."""
        real = self._real_claim

        def claim(key):
            is_leader, flight = real(key)
            if not is_leader:
                on_join(threading.get_ident(), flight)
            return is_leader, flight

        self._stack.enter_context(
            patch("api.routes._claim_session_get_flight", side_effect=claim)
        )

    def wait_for_flight_keys(self, count, timeout=30):
        """Block until ``count`` requests have computed their flight key."""
        deadline = time.time() + timeout
        while True:
            with self._lock:
                if len(self.flight_keys) >= count:
                    return True
            if time.time() >= deadline:
                return False
            time.sleep(0.005)

    # -- lifecycle -----------------------------------------------------------
    def __enter__(self):
        import api.routes as routes

        self._real_key = routes._session_get_flight_key
        self._real_claim = routes._claim_session_get_flight
        s = self.session
        self._stack = ExitStack()
        self._stack.__enter__()
        stack = self._stack
        # GET /api/session collaborators
        stack.enter_context(patch("api.routes.get_session", return_value=s))
        stack.enter_context(
            patch("api.routes._clear_stale_stream_state", return_value=False)
        )
        stack.enter_context(
            patch("api.routes._lookup_cli_session_metadata", return_value={})
        )
        stack.enter_context(
            patch("api.routes.get_state_db_session_messages", return_value=[])
        )
        stack.enter_context(
            patch("api.routes.redact_session_data", side_effect=self.redact)
        )
        stack.enter_context(
            patch("api.routes._session_get_flight_key", side_effect=self.record_flight_key)
        )
        stack.enter_context(patch("api.routes.j", side_effect=self.fake_j))
        # POST /api/session/update collaborators
        stack.enter_context(patch("api.routes._check_csrf", lambda _h: True))
        stack.enter_context(
            patch("api.routes._guard_request_session_visibility", lambda *a, **k: True)
        )
        stack.enter_context(
            patch(
                "api.routes.read_body",
                lambda _h: {"session_id": s.session_id, "model": "gpt-new"},
            )
        )
        stack.enter_context(
            patch("api.routes._get_or_materialize_session", lambda _sid: s)
        )
        stack.enter_context(
            patch("api.routes.resolve_trusted_workspace", lambda ws, profile=None: ws)
        )
        stack.enter_context(
            patch("api.routes._invalidate_session_route_state_if_changed", self.apply_model)
        )
        stack.enter_context(
            patch("api.routes.set_last_workspace", lambda *a, **k: None)
        )
        stack.enter_context(
            patch("api.routes.public_session_projection", lambda raw: raw)
        )
        return self

    def __exit__(self, *exc):
        return self._stack.__exit__(*exc)


def _spawn(fn, *args):
    thread = threading.Thread(target=fn, args=args)
    thread.start()
    return thread


def _join_all(env, threads):
    for thread in threads:
        thread.join(timeout=30)
    assert not any(t.is_alive() for t in threads), "route call did not finish"
    assert env.errors == [], env.errors


def test_gets_overlapped_by_session_update_never_return_the_old_model(isolated_home):
    """#7310 gate review (blocking): a GET landing inside /api/session/update's
    mutation window must not be handed a flight built before the update.

    The route mutates the cached Session and only then saves, so a GET in that
    window still computes the PRE-save key — identical to the key a leader
    claimed before the update — and would otherwise receive that leader's
    payload carrying the old model.
    """
    import api.routes as routes

    session = _UpdatableSession([{"role": "user", "content": "hi"}], isolated_home)
    assert session.model == "gpt-test"
    redact = _GatedRedact()
    env = _SessionRouteEnv(session, redact)
    idents = {}

    with env:
        def read(tag):
            ident = threading.get_ident()
            idents[tag] = ident
            try:
                routes.handle_get(SimpleNamespace(), urlparse(f"/api/session?{SAME_WINDOW}"))
            except BaseException as exc:  # noqa: BLE001 - surfaced via env.errors
                env.errors.append(exc)

        def update():
            try:
                routes.handle_post(SimpleNamespace(), urlparse("/api/session/update"))
            except BaseException as exc:  # noqa: BLE001
                env.errors.append(exc)

        # 1. A leader GET claims the pre-update key and parks mid-build.
        leader = _spawn(read, "leader")
        assert redact.entered.wait(timeout=30), "leader never started building"

        # 2. The update takes the lock, mutates the cached Session, and parks in
        #    save(): new model in memory, sidecar still the old generation.
        updater = _spawn(update)
        assert session.save_entered.wait(timeout=30), "update never reached save()"
        assert session.model == "gpt-new"

        # 3. Two reloads arrive inside that window — the reconnect-storm shape.
        followers = [_spawn(read, f"follower_{i}") for i in range(2)]
        # Hold the update inside save() until both followers have computed
        # their flight key, so they provably observe the mutated Session with
        # the pre-update sidecar generation.
        assert env.wait_for_flight_keys(3), (
            "followers never reached the flight key inside the update window: "
            f"{env.flight_keys!r}"
        )

        session.release_save.set()
        redact.release.set()
        _join_all(env, [leader, updater, *followers])

        assert session.save_count == 1, "the update must have committed"
        # The leader compacted before the update mutated the cached Session, so
        # it legitimately holds the pre-update snapshot. The followers arrived
        # inside the mutation window and must build their own projection rather
        # than inherit that stale payload.
        leader_status, leader_data = env.capture[idents["leader"]]
        assert leader_status == 200, (leader_status, leader_data)
        assert leader_data["session"]["model"] == "gpt-test", leader_data
        for name in ("follower_0", "follower_1"):
            status, data = env.capture[idents[name]]
            assert status == 200, (name, status, data)
            assert data["session"]["model"] == "gpt-new", (
                f"{name} arrived inside the update window and inherited the "
                f"leader's pre-update model {data['session']['model']!r}"
            )
    assert routes._SESSION_GET_FLIGHTS == {}, "flight registry leaked an entry"


def test_follower_that_joined_before_an_update_refuses_the_stale_flight(isolated_home):
    """#7310 gate review, point 2: a follower must re-check the generation
    before it uses a shared payload.

    This follower joins while the session is still un-updated, so its key is
    legitimate on arrival; the update then completes during its wait. Using the
    leader's payload without re-checking is what hands it the old model.
    """
    import api.routes as routes

    session = _UpdatableSession(
        [{"role": "user", "content": "hi"}], isolated_home, block_save=False
    )
    redact = _GatedRedact()
    env = _SessionRouteEnv(session, redact)
    idents = {}
    joined = threading.Event()

    with env:
        def on_join(ident, flight):
            if idents.get("follower") == ident:
                joined.set()

        env.track_claims(on_join)

        def read(tag):
            ident = threading.get_ident()
            idents[tag] = ident
            try:
                routes.handle_get(SimpleNamespace(), urlparse(f"/api/session?{SAME_WINDOW}"))
            except BaseException as exc:  # noqa: BLE001
                env.errors.append(exc)

        def update():
            try:
                routes.handle_post(SimpleNamespace(), urlparse("/api/session/update"))
            except BaseException as exc:  # noqa: BLE001
                env.errors.append(exc)

        # The leader claims the pre-update key and parks mid-build.
        leader = _spawn(read, "leader")
        assert redact.entered.wait(timeout=30), "leader never started building"

        # The follower joins that flight while the session is still un-updated.
        follower = _spawn(read, "follower")
        assert joined.wait(timeout=30), "follower never joined the in-flight read"

        # The update lands while the follower is still waiting on the flight.
        updater = _spawn(update)
        _join_all(env, [updater])
        assert session.model == "gpt-new"
        assert session.save_count == 1, "the update must have committed"

        redact.release.set()
        _join_all(env, [leader, follower])

        leader_status, leader_data = env.capture[idents["leader"]]
        follower_status, follower_data = env.capture[idents["follower"]]
        assert leader_status == 200, leader_data
        assert follower_status == 200, follower_data
        assert leader_data["session"]["model"] == "gpt-test", (
            "the leader built before the update, so it legitimately holds the old model"
        )
        assert follower_data["session"]["model"] == "gpt-new", (
            "a follower that waited across the update must not reuse the leader's "
            f"pre-update payload (got {follower_data['session']['model']!r})"
        )


def test_settings_rewrite_changes_the_flight_key(isolated_home):
    """Redaction and display rules come from settings.json, so a rewrite while
    a build is in flight must not be served to a later reader."""
    import api.routes as routes

    session = _FakeSession([{"role": "user", "content": "hi"}])
    owned_settings = routes.SESSION_DIR / "settings-sig.json"
    owned_settings.write_text('{"theme": "dark"}')
    original = routes.SETTINGS_FILE
    try:
        routes.SETTINGS_FILE = owned_settings
        key_before = routes._session_get_flight_key(session, None, {}, ("1",))
        assert key_before is not None
        time.sleep(0.01)
        owned_settings.write_text('{"theme": "oled"}')
        key_after = routes._session_get_flight_key(session, None, {}, ("1",))
        assert key_after != key_before, (
            "settings rewrite must change the flight key"
        )
    finally:
        routes.SETTINGS_FILE = original
