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

import sqlite3
import threading
import time
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
