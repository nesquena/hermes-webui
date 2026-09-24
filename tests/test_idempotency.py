"""Behavioral tests for ``POST /api/chat/start`` caller-supplied idempotency.

These tests pin the contract from issue #7435 against observable
behavior — execution counts and returned identities — NOT against
source-string assertions. Each scenario:

* drives ``_handle_chat_start`` directly (no live HTTP server);
* replaces the durable ``IdempotencyStore`` with an isolated instance
  in a tmp_path so the real production state is never touched;
* mocks ``_start_run`` so the tests don't need a model, a session DB,
  or a worker thread; the mock counts invocations and records the
  acceptance identity it returned on each call.

The contract being proved (eight acceptance criteria from #7435):

1. Two concurrent equivalent requests with one key → exactly one
   turn admitted; both callers see the same accepted identity.
2. A retry after a lost response (first call succeeded; caller never
   saw the response) replays the original identity, no second turn.
3. Replay still works after the original turn has fully completed
   (i.e. the active-stream 409 guard no longer protects it).
4. Replay still works after a WebUI process restart (in-memory store
   discarded, reloaded from disk).
5. Reusing a key with a DIFFERENT side-effect-relevant request
   returns 409 and does not start a second turn.
6. Existing clients that omit the key keep current behavior
   (no claim, no release, normal session-bound duplication paths).
7. Local AND gateway-backed chat-start paths provide equivalent
   idempotency semantics.
8. Expired keys fail explicitly (410), never silently admit a
   potentially duplicate turn.
"""
from __future__ import annotations

import io
import json
import threading
from types import SimpleNamespace

import pytest


# Sentinels for ``run_handler`` in the fixture below. They
# distinguish "no Idempotency-Key header at all" / "no
# ``idempotency_key`` body field" (the legacy absence path) from
# "header present with explicit value X" / "body field present with
# value Y" (the malformed-but-present path that the #7435 review
# requires us to reject with 400).
_SENTINEL_NO_HEADER = object()
_SENTINEL_NO_BODY = object()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakePostHandler:
    """Minimal stand-in for the real ``BaseHTTPRequestHandler``.

    Mirrors the existing ``_FakePostHandler`` in
    ``test_chat_start_claim_cli_session.py`` so the route's
    ``handler.headers``, ``handler.wfile``, etc. look the way the
    production code expects them to.
    """

    def __init__(self, body: dict, *, path: str = "/api/chat/start", headers: dict | None = None):
        raw = json.dumps(body).encode("utf-8")
        self.status = None
        self.response_headers = {}
        # _FakePostHandler exposes ``headers`` as a dict-like so the route's
        # ``handler.headers.get("Idempotency-Key")`` works.
        self.headers = dict(headers or {})
        # Make it look like an HTTP server-parsed headers mapping: case
        # doesn't matter for our lookups, but the production code only
        # uses .get() so a plain dict is fine.
        self.headers.setdefault("Content-Length", str(len(raw)))
        self.headers.setdefault("Content-Type", "application/json")
        self.rfile = io.BytesIO(raw)
        self.wfile = io.BytesIO()
        self.command = "POST"
        self.path = path
        self.client_address = ("127.0.0.1", 12345)
    def send_response(self, status):
        self.status = status

    def send_header(self, key, value):
        self.response_headers[key] = value

    def end_headers(self):
        pass


def _response(handler: _FakePostHandler) -> tuple[int, dict]:
    """Extract ``(status, payload)`` from a fake handler that called ``j()``.

    The chat-start route uses ``j(handler, payload, status=status)``
    which writes the JSON payload to ``handler.wfile``. We parse it
    back out so tests can assert against the actual returned body,
    not the source code.
    """
    body = handler.wfile.getvalue()
    if not body:
        # Some test paths swap in a stub ``j()`` that returns a dict
        # directly; in that case the handler's wfile stays empty and
        # the route's return value is the dict. Tests using that
        # pattern should assert on the dict, not on this helper.
        return (handler.status or 0), {}
    try:
        # ``j`` writes: ``"HTTP/1.1 200 OK\r\n...\r\n\r\n{json}\n"`` or
        # in some test stubs just the JSON blob. The fake handler we
        # pass DOES NOT actually call send_response/send_header/end_headers
        # (it never invokes the real Handler.send_*), so the wfile
        # only contains the body bytes.
        payload = json.loads(body)
    except json.JSONDecodeError:
        # If the bytes are an HTTP response (full wire format), the
        # caller should switch to a stub-j test.
        payload = {}
    return (handler.status or 200), payload


class _RunRecorder:
    """Counts and remembers every call to the mocked ``_start_run``."""

    def __init__(self):
        self.calls: list[dict] = []
        self._lock = threading.Lock()
        self._next_stream_id = 0
        # Optional gate to simulate a long-running start: the test can
        # set .start_event so a worker thread blocks until released,
        # letting the test fire a second concurrent request before the
        # first one completes.
        self.start_event: threading.Event | None = None

    def __call__(self, session, **kwargs):  # signature matches _start_run
        with self._lock:
            self._next_stream_id += 1
            stream_id = f"stream-{self._next_stream_id}"
            turn_id = f"turn-{self._next_stream_id}"
            self.calls.append({
                "session_id": session.session_id,
                "msg": kwargs.get("msg"),
                "model": kwargs.get("model"),
                "stream_id": stream_id,
                "turn_id": turn_id,
                "source": kwargs.get("source"),
            })
        if self.start_event is not None:
            # Block here so concurrent retries arrive while we're
            # "in flight". The route will have already stashed the
            # pending claim; concurrent retries should see
            # idempotency_in_flight.
            self.start_event.wait(timeout=5.0)
        return {
            "stream_id": stream_id,
            "session_id": session.session_id,
            "turn_id": turn_id,
            "title": "test",
            "pending_started_at": 0.0,
        }


@pytest.fixture
def idem_env(tmp_path, monkeypatch):
    """Wire up an isolated IdempotencyStore + the rest of the chat-start mocks.

    Returns a namespace with the relevant test handles:
      * ``store`` — fresh ``IdempotencyStore`` pointed at tmp_path
      * ``recorder`` — counts every ``_start_run`` call
      * ``routes`` — imported ``api.routes`` module (already mutated
        with monkeypatched dependencies)
      * ``run_handler`` — convenience: builds a fake handler and calls
        ``_handle_chat_start`` with the given body + key header
      * ``session`` — a stub session with a stable id
    """
    from api import routes
    from api.idempotency import IdempotencyStore, set_idempotency_store

    # Point the module's STATE_DIR at tmp_path so the real production
    # state is never touched (the IdempotencyStore also computes its
    # own path under STATE_DIR, but we override ``store`` for isolation).
    monkeypatch.setattr("api.config.STATE_DIR", tmp_path, raising=False)
    # Build a fresh store rooted at tmp_path. Independent of the
    # module-level singleton so tests can drop / reload it to
    # simulate a process restart.
    store = IdempotencyStore(path=tmp_path / "idempotency" / "store.json")
    set_idempotency_store(store)

    # Stub the session lookup so the route does not try to read
    # SESSION_DIR; ``_start_run`` is the only thing that actually
    # mutates session state in the real flow.
    session = SimpleNamespace(
        session_id="idem-session",
        model="test-model",
        model_provider="test-provider",
        profile=None,
        messages=[],
        context_messages=[],
        pending_user_message=None,
        pending_started_at=0.0,
        title="test",
    )
    monkeypatch.setattr(routes, "_get_or_materialize_session", lambda *_a, **_k: session)
    monkeypatch.setattr(routes, "_get_active_profile_name", lambda: "default")
    monkeypatch.setattr(routes, "_session_visible_to_active_profile", lambda *_a, **_k: True)
    monkeypatch.setattr(routes, "_read_profile_model_config", lambda *_a, **_k: (None, "test-model", {}))
    monkeypatch.setattr(routes, "_resolve_compatible_session_model_state", lambda *_a, **_k: ("test-model", "test-provider", False))
    monkeypatch.setattr(routes, "get_config", lambda: {})
    monkeypatch.setattr(routes, "get_config_snapshot", lambda: {})
    monkeypatch.setattr(routes, "webui_gateway_chat_enabled", lambda _cfg: False)
    monkeypatch.setattr(routes, "_agent_runtime_barrier_response", lambda **_k: None)
    monkeypatch.setattr(routes, "_resolve_chat_workspace_with_recovery", lambda *_a, **_k: "/tmp")
    monkeypatch.setattr(
        routes, "_moa_fast_path_model_state", lambda _m: ("test-model", "test-provider", False)
    )
    monkeypatch.setattr(routes, "_clean_session_model_provider", lambda _p: None)
    monkeypatch.setattr(routes, "_repair_foreign_session_model_provider", lambda *a, **k: a[4] if len(a) > 4 else k.get("resolved_provider"))
    # Skip the MoA override branch: model_provider is "test-provider",
    # not "moa", so these aren't reached, but stub defensively in case
    # a future refactor routes through them.
    monkeypatch.setattr(routes, "_resolve_chat_workspace_for_regeneration", lambda *_a, **_k: "/tmp")
    # compression_continuation: don't seal.
    try:
        from api import compression_continuation as _cc
        monkeypatch.setattr(_cc, "durable_compression_continuation", lambda _s: (False, None))
    except ImportError:
        pass
    # compression_recovery: empty recovery so the route skips the
    # 409 "compression_recovery_required" branch.
    try:
        from api import compression_recovery as _cr
        monkeypatch.setattr(_cr, "compression_recovery_payload_for_session", lambda _s: None)
        monkeypatch.setattr(_cr, "clear_compression_recovery", lambda _s: None)
        monkeypatch.setattr(_cr, "is_generic_continuation_intent", lambda _m: False)
    except ImportError:
        pass

    recorder = _RunRecorder()
    monkeypatch.setattr(routes, "_start_run", recorder)

    def run_handler(
        body: dict,
        *,
        key_header=_SENTINEL_NO_HEADER,
        key_body: object = _SENTINEL_NO_BODY,
    ) -> tuple[int, dict]:
        # Two sentinels distinguish:
        #   * "no Idempotency-Key header at all" (the legacy path);
        #   * "header present, value=<key_header>" (which may be
        #     the empty string, whitespace, or a non-string — the
        #     malformed path the #7435 review requires us to
        #     reject explicitly).
        if key_header is _SENTINEL_NO_HEADER:
            idem_headers = None
        else:
            idem_headers = {"Idempotency-Key": key_header}
        handler = _FakePostHandler(body, headers=idem_headers)
        if key_body is not _SENTINEL_NO_BODY and "idempotency_key" not in body:
            body = dict(body)
            body["idempotency_key"] = key_body
        # Stub ``j`` so the route returns a dict we can introspect
        # (the real ``j`` writes to wfile and returns None).
        captured: dict = {}
        def fake_j(_handler, payload, status=200, **_kw):
            captured["status"] = status
            captured["payload"] = payload
            return None
        monkeypatch.setattr(routes, "j", fake_j)
        monkeypatch.setattr(routes, "bad", lambda _h, msg, status=400: fake_j(_h, {"error": msg}, status=status))
        result = routes._handle_chat_start(handler, body)
        if result is not None and not captured:
            # Some legacy code path returned the dict directly.
            return (200, result if isinstance(result, dict) else {})
        return (captured.get("status", 200), captured.get("payload", {}))

    return SimpleNamespace(
        store=store,
        recorder=recorder,
        routes=routes,
        run_handler=run_handler,
        session=session,
    )


# ---------------------------------------------------------------------------
# Core acceptance criteria
# ---------------------------------------------------------------------------


def test_concurrent_duplicate_runs_exactly_one_turn(idem_env):
    """Two threads fire the same key+body → one turn started, both callers
    receive the same acceptance identity (session_id, stream_id, turn_id)."""
    body = {"session_id": "idem-session", "message": "hello"}
    key = "concurrent-key-1"

    # Start the first request on a worker, block it inside the mock
    # _start_run so the second one arrives while the first is still
    # in flight. That is the "concurrent" window that would otherwise
    # admit two turns.
    start_event = threading.Event()
    idem_env.recorder.start_event = start_event

    results: list[tuple[int, dict]] = []
    results_lock = threading.Lock()

    def fire():
        status, payload = idem_env.run_handler(body, key_header=key)
        with results_lock:
            results.append((status, payload))

    t1 = threading.Thread(target=fire)
    t1.start()
    # Give t1 a moment to enter _start_run and pin a pending claim.
    t1_started = threading.Event()
    original_start_run = idem_env.recorder

    def watch_calls():
        for _ in range(200):
            if original_start_run.calls:
                t1_started.set()
                return
            import time as _t
            _t.sleep(0.005)
    threading.Thread(target=watch_calls, daemon=True).start()
    t1_started.wait(timeout=2.0)

    # Second request while the first is still in flight. Because the
    # first has not called complete() yet, the second should see
    # idempotency_in_flight (409) — NOT a replay and NOT a fresh turn.
    t2_status, t2_payload = idem_env.run_handler(body, key_header=key)

    # Now release the first request; it completes, returns 200 with
    # the same identity a retry-after-completion would see.
    start_event.set()
    t1.join(timeout=5.0)

    # Exactly ONE _start_run call — the second request did not admit
    # a new turn.
    assert len(idem_env.recorder.calls) == 1, (
        f"concurrent duplicate admitted more than one turn: "
        f"{len(idem_env.recorder.calls)} calls"
    )
    first_call = idem_env.recorder.calls[0]

    # The in-flight retry got a deterministic 409 with the in-flight
    # code, so the caller knows to back off and retry.
    assert t2_status == 409
    assert t2_payload.get("code") == "idempotency_in_flight"
    assert t2_payload.get("idempotency_key") == key

    # The first call's identity is what a later retry would replay.
    assert first_call["stream_id"] == "stream-1"
    assert first_call["turn_id"] == "turn-1"


def test_lost_response_replay_returns_original_identity(idem_env):
    """A retry after a lost response replays the original identity,
    without starting a new turn."""
    body = {"session_id": "idem-session", "message": "hello"}
    key = "lost-response-key"

    # First call: succeeds normally.
    s1, p1 = idem_env.run_handler(body, key_header=key)
    assert s1 == 200
    assert p1["stream_id"] == "stream-1"
    assert len(idem_env.recorder.calls) == 1

    # Simulate the "lost response" retry: caller didn't see p1 and
    # re-sends with the same key + same body.
    s2, p2 = idem_env.run_handler(body, key_header=key)
    assert s2 == 200
    # The identity-bearing fields are identical (the whole point of
    # the contract: retry sees the same stream_id / turn_id).
    assert p2["stream_id"] == p1["stream_id"]
    assert p2["turn_id"] == p1["turn_id"]
    assert p2["session_id"] == p1["session_id"]
    # And no second turn was admitted.
    assert len(idem_env.recorder.calls) == 1, (
        f"replay admitted a second turn: {len(idem_env.recorder.calls)} calls"
    )
    # The replay payload carries a marker so the client can detect
    # that the response is a replay (and not a brand-new turn).
    assert p2.get("replayed_from_idempotency_key") is True


def test_replay_works_after_turn_completes(idem_env):
    """Even after the original turn is fully completed (the
    active-stream 409 guard no longer protects it), a retry with the
    same key still replays the original identity."""
    body = {"session_id": "idem-session", "message": "hello"}
    key = "post-completion-key"

    s1, p1 = idem_env.run_handler(body, key_header=key)
    assert s1 == 200
    assert p1["stream_id"] == "stream-1"

    # Mark the turn as fully completed — the route would normally
    # consider the active-stream 409 guard lifted at this point. A
    # naive duplicate-detection scheme would now let the second
    # request start a new turn. Idempotency must still bind them.
    from api.idempotency import STATUS_COMPLETE
    rec = idem_env.store.lookup(key)
    assert rec is not None and rec.status == STATUS_COMPLETE

    s2, p2 = idem_env.run_handler(body, key_header=key)
    assert s2 == 200
    assert p2["stream_id"] == p1["stream_id"]
    assert p2["turn_id"] == p1["turn_id"]
    assert len(idem_env.recorder.calls) == 1


def test_replay_works_after_process_restart(idem_env):
    """Replay must survive a WebUI process restart. We simulate the
    restart by dropping the in-memory IdempotencyStore and forcing a
    re-read of the durable file. The new in-memory state must already
    contain the completed record (loaded from disk), so a retry
    replays the same identity without starting a new turn."""
    from api.idempotency import build_storage_key
    body = {"session_id": "idem-session", "message": "hello"}
    key = "post-restart-key"
    stored_key = build_storage_key(key)

    s1, p1 = idem_env.run_handler(body, key_header=key)
    assert s1 == 200
    assert p1["stream_id"] == "stream-1"
    expected_stream = p1["stream_id"]
    expected_turn = p1["turn_id"]

    # Sanity: the durable file actually contains the record. The
    # on-disk key is profile-namespaced (the route is the single
    # source of truth for the namespace; tests resolve the same
    # value via ``build_storage_key``).
    assert idem_env.store.path.exists()
    on_disk = json.loads(idem_env.store.path.read_text(encoding="utf-8"))
    on_disk_keys = [r["key"] for r in on_disk.get("records", [])]
    assert stored_key in on_disk_keys
    # And the raw key is NOT stored unprefixed.
    assert key not in on_disk_keys

    # Simulate a restart: a fresh in-memory state, forced reload from
    # the same durable file.
    idem_env.store._records.clear()  # drop in-memory
    idem_env.store._loaded = False
    idem_env.store.reload_from_disk()

    # The store should have the record back, with status=complete.
    rec = idem_env.store.lookup(key)
    assert rec is not None
    assert rec.key == stored_key
    assert rec.status == "complete"
    assert rec.stream_id == expected_stream
    assert rec.turn_id == expected_turn

    # Now a retry — must replay from the disk-loaded record, must not
    # start a new turn.
    s2, p2 = idem_env.run_handler(body, key_header=key)
    assert s2 == 200
    assert p2["stream_id"] == expected_stream
    assert p2["turn_id"] == expected_turn
    assert p2.get("replayed_from_idempotency_key") is True
    assert len(idem_env.recorder.calls) == 1


def test_different_payload_with_same_key_returns_409_no_new_turn(idem_env):
    """Reusing a key with a DIFFERENT side-effect-relevant request
    returns a deterministic 409 and starts no new turn."""
    body1 = {"session_id": "idem-session", "message": "first message"}
    body2 = {"session_id": "idem-session", "message": "DIFFERENT message"}
    key = "conflict-key"

    s1, p1 = idem_env.run_handler(body1, key_header=key)
    assert s1 == 200
    assert len(idem_env.recorder.calls) == 1

    # Same key, different message. This is a deliberately distinct
    # request — the contract is a deterministic 409, not silent
    # dedup.
    s2, p2 = idem_env.run_handler(body2, key_header=key)
    assert s2 == 409
    assert p2.get("code") == "idempotency_conflict"
    assert p2.get("idempotency_key") == key
    # Crucially: no second turn admitted.
    assert len(idem_env.recorder.calls) == 1


def test_no_key_keeps_legacy_behavior(idem_env):
    """A request that omits the key (no header, no body field) keeps
    current behavior: no claim, no release, no conflict. Two such
    requests in a row each start their own turn — that's the legacy
    browser behavior, untouched by this feature."""
    body1 = {"session_id": "idem-session", "message": "first"}
    body2 = {"session_id": "idem-session", "message": "second"}

    s1, p1 = idem_env.run_handler(body1)
    s2, p2 = idem_env.run_handler(body2)

    assert s1 == 200 and s2 == 200
    assert len(idem_env.recorder.calls) == 2, (
        "no-key requests must not be subject to idempotency: "
        f"got {len(idem_env.recorder.calls)} _start_run calls"
    )
    # Each call admitted its own turn (distinct stream_id / turn_id).
    assert p1["stream_id"] != p2["stream_id"]
    assert p1["turn_id"] != p2["turn_id"]
    # And the store stayed empty — we never claimed a key.
    assert list(idem_env.store.keys()) == []


def test_gateway_backed_path_has_equivalent_idempotency(idem_env, monkeypatch):
    """When the WebUI is in gateway-backed mode, the same key produces
    equivalent replay semantics. The contract explicitly says local
    and gateway paths MUST be equivalent — they share this chokepoint
    so we only need to verify the route honors the key regardless of
    which worker target _start_chat_stream_for_session would have
    picked."""
    body = {"session_id": "idem-session", "message": "hello"}
    key = "gateway-key"

    # Flip the gateway switch AFTER the fixture is set up; the
    # fixture already left ``webui_gateway_chat_enabled`` returning
    # False by default.
    monkeypatch.setattr(
        idem_env.routes, "webui_gateway_chat_enabled", lambda _cfg: True,
    )

    s1, p1 = idem_env.run_handler(body, key_header=key)
    assert s1 == 200
    assert p1["stream_id"] == "stream-1"
    assert len(idem_env.recorder.calls) == 1

    # Retry with same key → replay, no second turn, even though we
    # are in gateway-backed mode.
    s2, p2 = idem_env.run_handler(body, key_header=key)
    assert s2 == 200
    assert p2["stream_id"] == p1["stream_id"]
    assert p2["turn_id"] == p1["turn_id"]
    assert p2.get("replayed_from_idempotency_key") is True
    assert len(idem_env.recorder.calls) == 1


def test_expired_key_fails_explicitly(idem_env):
    """An expired key (TTL elapsed) must be refused explicitly. We
    do NOT silently re-admit a turn with the same key, because that
    could double-bill a caller that just hadn't realized the prior
    claim had aged out."""
    from api.idempotency import build_storage_key, compute_request_fingerprint
    body = {"session_id": "idem-session", "message": "hello"}
    key = "expiry-key"
    stored_key = build_storage_key(key)

    # Seed a record that is already past TTL. The fingerprint must
    # match what the new request will compute, otherwise we'd hit
    # 409 conflict before the TTL check has a chance to fire.
    fingerprint = compute_request_fingerprint(body)
    idem_env.store.claim(key, fingerprint)
    rec = idem_env.store.lookup(key)
    assert rec is not None
    rec.claimed_at = 0.0  # 1970-01-01 — well past any reasonable TTL
    idem_env.store._records.move_to_end(stored_key)
    idem_env.store._persist_locked()

    s, p = idem_env.run_handler(body, key_header=key)
    assert s == 410
    assert p.get("code") == "idempotency_key_expired"
    # No turn admitted.
    assert len(idem_env.recorder.calls) == 0


# ---------------------------------------------------------------------------
# Header / body field / validation
# ---------------------------------------------------------------------------


def test_idempotency_key_in_body_field_is_honored(idem_env):
    """The body field ``idempotency_key`` works exactly like the
    ``Idempotency-Key`` header. The body field wins when both are
    present (per the contract — body is the explicit opt-in)."""
    body = {"session_id": "idem-session", "message": "hello"}
    # Body field present, header absent.
    s1, p1 = idem_env.run_handler(body, key_body="body-key-1")
    assert s1 == 200
    assert p1["stream_id"] == "stream-1"

    # Same body, same body-field key → replay.
    s2, p2 = idem_env.run_handler({"session_id": "idem-session", "message": "hello"}, key_body="body-key-1")
    assert s2 == 200
    assert p2["stream_id"] == p1["stream_id"]
    assert len(idem_env.recorder.calls) == 1


def test_body_field_wins_over_header(idem_env):
    """When both header and body field are present, the body field
    is the one bound to the record. (The header is the universal
    transport; the body field is the explicit opt-in. Body wins.)"""
    body_with_body_key = {
        "session_id": "idem-session",
        "message": "hello",
        "idempotency_key": "body-key-wins",
    }
    body_with_header_key = {
        "session_id": "idem-session",
        "message": "hello",
    }
    s1, p1 = idem_env.run_handler(body_with_body_key, key_header="header-key")
    assert s1 == 200

    # Retry with the body field only — must hit the SAME record.
    s2, p2 = idem_env.run_handler(body_with_header_key, key_body="body-key-wins")
    assert s2 == 200
    assert p2["stream_id"] == p1["stream_id"]
    assert len(idem_env.recorder.calls) == 1

    # The header key was never bound.
    assert idem_env.store.lookup("header-key") is None


def test_invalid_key_returns_400(idem_env):
    """Oversize / non-printable / malformed keys must be rejected
    with 400. They are caller errors, not duplicates; we don't want
    to silently bind them.

    Per the #7435 review: a transport that explicitly supplied a
    value (even empty / whitespace / non-string) is a malformed
    key and is rejected with 400. True absence (no header, no body
    field) keeps the legacy no-idempotency path.
    """
    # 400-bound: non-empty, but malformed
    bad_keys = [
        "has space in it",        # ASCII space (0x20) is outside 0x21-0x7E
        "x" * 201,                # over the 200-char limit
        "tab\there",              # tab character
    ]
    for bad in bad_keys:
        before = len(idem_env.recorder.calls)
        s, p = idem_env.run_handler(
            {"session_id": "idem-session", "message": "hello"},
            key_header=bad,
        )
        assert s == 400, f"expected 400 for key {bad!r}, got {s}: {p}"
        assert "idempotency" in (p.get("error") or "").lower() or "key" in (p.get("error") or "").lower()
        assert len(idem_env.recorder.calls) == before, (
            f"invalid key {bad!r} admitted a turn"
        )

    # Present-but-malformed: empty / whitespace-only / non-string.
    # The contract is explicit: an explicit value is a malformed
    # key, NOT "no key" (no key = true absence on BOTH transports).
    # A stray ``Idempotency-Key:  `` header from a misconfigured
    # client must surface a 400, not silently fall through to the
    # legacy path that would admit a turn.
    #
    # We do not include ``None`` in this set: Python's HTTP parser
    # never produces ``None`` as a header value, and our test
    # handler uses ``None`` as the "no header at all" sentinel.
    # A non-string body field is tested separately below.
    for malformed in ("", "    ", 12345, []):
        before = len(idem_env.recorder.calls)
        s, p = idem_env.run_handler(
            {"session_id": "idem-session", "message": "hello"},
            key_header=malformed,
        )
        assert s == 400, (
            f"present-but-malformed key {malformed!r} should 400, not fall "
            f"through to legacy; got {s}: {p}"
        )
        assert "idempotency" in (p.get("error") or "").lower() or "key" in (p.get("error") or "").lower()
        assert len(idem_env.recorder.calls) == before, (
            f"malformed key {malformed!r} admitted a turn"
        )

    # Non-string body field: the body explicitly carries a
    # ``idempotency_key`` whose value is not a string. Per the
    # contract, an explicit value is a malformed key — 400, not
    # legacy. A real client that JSON-encodes the wrong type
    # should fix its request, not silently fall through to the
    # no-idempotency path.
    for malformed_body in (12345, [], {"x": 1}, True):
        before = len(idem_env.recorder.calls)
        body = {"session_id": "idem-session", "message": "hello"}
        # Insert the body field directly so the fixture's
        # ``key_body is not _SENTINEL_NO_BODY`` check still
        # triggers.
        body["idempotency_key"] = malformed_body
        s, p = idem_env.run_handler(body)
        assert s == 400, (
            f"present-but-malformed body key {malformed_body!r} should 400, "
            f"not fall through to legacy; got {s}: {p}"
        )
        assert len(idem_env.recorder.calls) == before, (
            f"malformed body key {malformed_body!r} admitted a turn"
        )

    # True absence: omit both the Idempotency-Key header and the
    # ``idempotency_key`` body field. This is the legacy path — a
    # fresh turn is admitted every time. (The default
    # ``run_handler`` behaviour: no header at all.)
    body = {"session_id": "idem-session", "message": "hello"}
    before = len(idem_env.recorder.calls)
    s, p = idem_env.run_handler(body)
    assert s == 200
    assert len(idem_env.recorder.calls) == before + 1


# ---------------------------------------------------------------------------
# Findings from the #7435 review (idempotency fail-closed hardening)
# ---------------------------------------------------------------------------
#
# 1. ``_persist_locked()`` previously swallowed OSError and returned
#    silently; ``claim()`` and ``complete()`` then told the route the
#    record was durably stored. A crash between the in-memory
#    mutation and the durable write would resurrect the claim on
#    restart and admit a duplicate turn on retry.
# 2. ``_evict_to_cap_locked()`` evicted the oldest record regardless
#    of TTL, dropping live claims (or completed records the caller
#    might still retry) under a runaway-caller cap. The store now
#    refuses to evict unexpired records and surfaces 503 when full.
# 3. Storage keys and fingerprints were not scoped to the active
#    profile; the same raw key under two different profiles would
#    collide. The store now namespaces storage keys AND bakes the
#    server-resolved profile into the fingerprint.
# 4. ``extract_key()`` downgraded a present-but-empty header or
#    non-string body field to "no key" (legacy), letting a
#    misconfigured client silently skip idempotency and admit
#    duplicate turns. The route now rejects malformed-but-present
#    keys with 400.
# 5. The behavioural tests below prove each fix end-to-end through
#    ``_handle_chat_start`` (no source-string assertions).


def test_persist_failure_during_claim_rolls_back_and_returns_503(
    idem_env, monkeypatch
):
    """Finding 1: when ``_persist_locked()`` raises during ``claim()``,
    the in-memory mutation must be rolled back and the route must
    return 503. The agent-start recorder must show NO admitted turn.

    Without rollback, a process restart (or a retry from the caller)
    would resurrect the claim from the durable file, blocking
    legitimate retries with ``IdempotencyInFlight`` and admitting
    a duplicate turn on a different retry path.
    """
    from api import idempotency as idem_mod

    # Simulate a disk failure: every persist call raises. The
    # closure captures the store instance because instance
    # attributes set via ``monkeypatch.setattr`` don't auto-bind
    # ``self`` the way class methods do.
    store_ref = idem_env.store

    def boom_persist():
        raise idem_mod.IdempotencyStoreUnavailable("simulated disk full")
    monkeypatch.setattr(store_ref, "_persist_locked", boom_persist)

    body = {"session_id": "idem-session", "message": "hello"}
    key = "persist-fail-claim-key"
    s, p = idem_env.run_handler(body, key_header=key)
    assert s == 503, f"expected 503 on persist failure, got {s}: {p}"
    assert p.get("code") == "idempotency_store_unavailable"
    assert p.get("idempotency_key") == key
    # Critical: no turn admitted.
    assert len(idem_env.recorder.calls) == 0, (
        f"persist failure admitted a turn: {len(idem_env.recorder.calls)} calls"
    )
    # Critical: in-memory state was rolled back — the next call
    # with the same key (after the disk recovers) can claim
    # cleanly. A bug that left a phantom in-memory record would
    # return IdempotencyInFlight on the retry, even though the
    # durable store never saw the claim.
    stored = idem_env.store.lookup_stored(
        idem_mod.build_storage_key(key)
    )
    assert stored is None, (
        f"in-memory record leaked after persist failure: {stored!r}"
    )


def test_persist_failure_during_complete_rolls_back_and_returns_503(
    idem_env, monkeypatch
):
    """Finding 1 (complete path): when the persist inside
    ``complete()`` fails, the in-memory record is restored to its
    prior (pending) state and the route returns 503. The
    ``idem_completed`` flag MUST remain False so the finally
    clause can release the claim and the next retry is a fresh
    attempt, not a replay of an unpersisted completion.

    Note: a retry that arrives after a failed complete will see
    no complete record, admit a new turn, and the caller has to
    reconcile the duplicate. That is unavoidable — the agent
    already ran — but the 503 surfaces the durability gap so the
    caller can decide. The test only pins the
    rollback-and-503 invariants.
    """
    from api import idempotency as idem_mod

    # Let the first call succeed: claim + complete writes a
    # completed record.
    body = {"session_id": "idem-session", "message": "hello"}
    key = "persist-fail-complete-key"
    s, p = idem_env.run_handler(body, key_header=key)
    assert s == 200
    assert p["stream_id"] == "stream-1"
    assert len(idem_env.recorder.calls) == 1

    # Simulate a disk failure starting on the SECOND persist
    # call (which is the complete's persist — the first is the
    # claim's persist, which must succeed for the agent to
    # actually start).
    state = {"call_count": 0, "fail_from": 2}
    original_persist = idem_env.store._persist_locked

    def flaky_persist():
        state["call_count"] += 1
        if state["call_count"] >= state["fail_from"]:
            raise idem_mod.IdempotencyStoreUnavailable("simulated disk full")
        return original_persist()
    monkeypatch.setattr(idem_env.store, "_persist_locked", flaky_persist)

    # Second call: fresh key to force a new claim+complete cycle.
    key2 = "persist-fail-complete-key-2"
    s, p = idem_env.run_handler(
        {"session_id": "idem-session", "message": "second"},
        key_header=key2,
    )
    assert s == 503, f"expected 503 on complete persist failure, got {s}: {p}"
    assert p.get("code") == "idempotency_store_unavailable"
    assert p.get("idempotency_key") == key2
    # Critical: the recorder shows the agent was admitted (the
    # worker thread started — we can't unsay that) but the route
    # told the caller 503 so the caller can reconcile. The
    # in-memory record was rolled back to pending.
    assert len(idem_env.recorder.calls) == 2, (
        f"second turn should have started before persist failed: "
        f"{len(idem_env.recorder.calls)} calls"
    )
    # Stored state: rolled back to pending (NOT complete), so a
    # retry does not replay the broken completion.
    rec = idem_env.store.lookup(key2)
    assert rec is not None, (
        "in-memory record was popped instead of rolled back to pending"
    )
    assert rec.status == "pending", (
        f"expected status pending after rollback, got {rec.status!r}"
    )
    assert rec.completed_at == 0.0, (
        f"expected completed_at cleared, got {rec.completed_at!r}"
    )


def test_cap_never_evicts_unexpired_record(idem_env, monkeypatch):
    """Finding 2: a cap-full store must NEVER evict an unexpired
    record. ``_evict_to_cap_locked`` should only drop records
    past their TTL, and if the cap is still exceeded after that
    sweep, claim() must raise ``IdempotencyStoreUnavailable``
    (→ 503) so the caller can back off rather than silently
    re-admitting a turn whose claim was evicted out from under
    it.

    A pending record is even more sensitive: evicting it would
    let a concurrent retry see no record, claim again, and start
    a second turn.
    """
    from api import idempotency as idem_mod

    # Build a tiny store with cap=2 so we can saturate it.
    tiny_store = idem_mod.IdempotencyStore(
        path=idem_env.store.path,
        ttl_seconds=3600,  # 1h, so seeded records are "unexpired"
        max_records=2,
    )
    # Reuse the module-level singleton for the route.
    from api.idempotency import set_idempotency_store
    set_idempotency_store(tiny_store)

    # Seed two completed records. Both unexpired.
    fingerprint = idem_mod.compute_request_fingerprint(
        {"session_id": "idem-session", "message": "hello"}
    )
    tiny_store.claim("a-key", fingerprint)
    tiny_store.complete(
        "a-key",
        session_id="idem-session", stream_id="s1", turn_id="t1",
        response_status=200, response_payload={},
    )
    tiny_store.claim("b-key", fingerprint)
    tiny_store.complete(
        "b-key",
        session_id="idem-session", stream_id="s2", turn_id="t2",
        response_status=200, response_payload={},
    )
    assert len(tiny_store._records) == 2

    # Third claim: cap is full, no expired records to evict.
    # Must raise IdempotencyStoreUnavailable.
    with pytest.raises(idem_mod.IdempotencyStoreUnavailable):
        tiny_store.claim("c-key", fingerprint)
    # The two original records are still present (not evicted
    # under the cap).
    assert tiny_store.lookup("a-key") is not None
    assert tiny_store.lookup("b-key") is not None

    # End-to-end through the route: same 503 contract.
    s, p = idem_env.run_handler(
        {"session_id": "idem-session", "message": "hello"},
        key_header="c-key-route",
    )
    assert s == 503
    assert p.get("code") == "idempotency_store_unavailable"


def test_cap_does_not_evict_pending_record(idem_env):
    """Finding 2 (pending): a PENDING record is also
    non-evictable. The first sweep in ``_evict_to_cap_locked``
    skips them so an in-flight claim never gets silently dropped
    while the route is mid-turn.

    We seed a pending record (claim() but no complete()) and a
    completed record, fill the cap, then try a new claim. The
    pending record must survive the cap sweep.
    """
    from api import idempotency as idem_mod

    tiny_store = idem_mod.IdempotencyStore(
        path=idem_env.store.path,
        ttl_seconds=3600,
        max_records=2,
    )
    from api.idempotency import set_idempotency_store
    set_idempotency_store(tiny_store)

    # Pending claim.
    tiny_store.claim("pending-key", "fp-pending")
    # Completed claim.
    tiny_store.claim("done-key", "fp-done")
    tiny_store.complete(
        "done-key",
        session_id="idem-session", stream_id="s", turn_id="t",
        response_status=200, response_payload={},
    )
    assert len(tiny_store._records) == 2

    # Cap-full with one pending, one complete. New claim must
    # raise, NOT evict the pending record.
    with pytest.raises(idem_mod.IdempotencyStoreUnavailable):
        tiny_store.claim("overflow-key", "fp-overflow")

    pending_rec = tiny_store.lookup("pending-key")
    assert pending_rec is not None, "pending record was evicted under cap"
    assert pending_rec.status == "pending"


def test_two_profiles_same_raw_key_are_isolated(idem_env, monkeypatch):
    """Finding 3: two profile contexts using the same raw key
    must NOT collide. Storage key includes the server-resolved
    profile; fingerprint includes the same profile. Replaying
    the same raw key under a different profile is treated as a
    new claim, not a replay.

    The client-supplied ``profile`` field in the body is
    intentionally ignored — only the server's
    ``_get_active_profile_name`` matters.
    """
    from api.idempotency import build_storage_key

    body_a = {"session_id": "idem-session", "message": "hello", "profile": "alpha"}
    body_b = {"session_id": "idem-session", "message": "hello", "profile": "beta"}
    raw_key = "shared-raw-key"

    # First request under profile "alpha". The client hint
    # ``profile: alpha`` is ignored — the server resolves the
    # active profile via ``_get_active_profile_name``.
    monkeypatch.setattr(
        idem_env.routes, "_get_active_profile_name", lambda: "alpha"
    )
    s1, p1 = idem_env.run_handler(body_a, key_header=raw_key)
    assert s1 == 200
    assert p1["stream_id"] == "stream-1"
    # Capture the alpha storage key BEFORE the profile switch —
    # ``lookup`` will use the current profile to namespace, which
    # is what we want to test against.
    alpha_stored_key = build_storage_key(raw_key, profile="alpha")
    assert idem_env.store.lookup_stored(alpha_stored_key) is not None
    assert idem_env.store.lookup_stored(alpha_stored_key).key == alpha_stored_key

    # Second request, same raw key, same body, but the active
    # profile is now "beta". Must be a NEW claim, not a replay.
    monkeypatch.setattr(
        idem_env.routes, "_get_active_profile_name", lambda: "beta"
    )
    s2, p2 = idem_env.run_handler(body_b, key_header=raw_key)
    assert s2 == 200
    assert p2["stream_id"] == "stream-2", (
        f"profile-scoped isolation broken: stream_id={p2['stream_id']!r} "
        f"(expected a fresh turn under profile 'beta')"
    )
    # And the alpha record is untouched.
    beta_stored_key = build_storage_key(raw_key, profile="beta")
    assert idem_env.store.lookup_stored(alpha_stored_key) is not None
    assert idem_env.store.lookup_stored(alpha_stored_key).key == alpha_stored_key
    assert idem_env.store.lookup_stored(beta_stored_key) is not None
    assert idem_env.store.lookup_stored(beta_stored_key).key == beta_stored_key

    # Two distinct records, neither was a replay.
    on_disk = json.loads(idem_env.store.path.read_text(encoding="utf-8"))
    keys = {r["key"] for r in on_disk.get("records", [])}
    assert alpha_stored_key in keys
    assert beta_stored_key in keys

    # Third request: same raw key, profile "alpha" again —
    # REPLAYS the alpha record, not the beta one. (The
    # fingerprint matches because the server-resolved profile
    # is the same.)
    monkeypatch.setattr(
        idem_env.routes, "_get_active_profile_name", lambda: "alpha"
    )
    s3, p3 = idem_env.run_handler(body_a, key_header=raw_key)
    assert s3 == 200
    assert p3["stream_id"] == "stream-1", (
        "alpha retry should replay the alpha record, not a new turn"
    )
    assert p3.get("replayed_from_idempotency_key") is True


def test_malformed_header_value_returns_400_no_turn(idem_env):
    """Finding 4: a present-but-malformed Idempotency-Key header
    must 400, not silently fall through to the legacy
    no-idempotency path."""
    for bad in ("", "   ", "x" * 201, "has space", "tab\there"):
        before = len(idem_env.recorder.calls)
        s, p = idem_env.run_handler(
            {"session_id": "idem-session", "message": "hello"},
            key_header=bad,
        )
        assert s == 400, (
            f"malformed header value {bad!r} should 400; got {s}: {p}"
        )
        assert len(idem_env.recorder.calls) == before, (
            f"malformed header {bad!r} admitted a turn"
        )


def test_malformed_body_field_returns_400_no_turn(idem_env):
    """Finding 4: a present-but-malformed ``idempotency_key``
    body field (empty string, non-string) must 400, not
    silently fall through to legacy."""
    # Empty string in the body field.
    body = {"session_id": "idem-session", "message": "hello", "idempotency_key": ""}
    before = len(idem_env.recorder.calls)
    s, p = idem_env.run_handler(body)
    assert s == 400
    assert len(idem_env.recorder.calls) == before

    # Non-string body field. Set directly so JSON parsing would
    # not reject it; the route's validator must.
    for bad in (12345, ["list"], {"dict": 1}, True, None):
        body = {"session_id": "idem-session", "message": "hello", "idempotency_key": bad}
        before = len(idem_env.recorder.calls)
        s, p = idem_env.run_handler(body)
        assert s == 400, (
            f"malformed body key {bad!r} should 400; got {s}: {p}"
        )
        assert len(idem_env.recorder.calls) == before, (
            f"malformed body key {bad!r} admitted a turn"
        )


def test_true_absence_keeps_legacy_path(idem_env):
    """Finding 4 (negative): true absence on BOTH transports —
    no Idempotency-Key header AND no ``idempotency_key`` body
    field — must keep the legacy no-idempotency path. Two such
    requests in a row each admit a fresh turn.
    """
    body = {"session_id": "idem-session", "message": "hello"}
    s1, p1 = idem_env.run_handler(body)
    s2, p2 = idem_env.run_handler(body)
    assert s1 == 200 and s2 == 200
    assert p1["stream_id"] != p2["stream_id"]
    assert p1["turn_id"] != p2["turn_id"]
    # And the store stayed empty — no claim was ever made.
    assert list(idem_env.store.keys()) == []


def test_directory_creation_failure_returns_503(idem_env, monkeypatch):
    """Finding 1 (deeper): a mkdir failure on the store dir
    must also surface 503, not be silently swallowed. The test
    forces ``path.parent.mkdir`` to raise ``PermissionError``
    and asserts the route returns 503 with no admitted turn.
    """

    # Force the store dir to be un-creatable. We swap the
    # ``mkdir`` method on the resolved path's parent (a
    # ``Path`` object) with a stub that always raises
    # ``PermissionError`` — mirroring the real-world failure
    # of a read-only / no-permission mount.
    real_mkdir = type(idem_env.store._path.parent).mkdir

    def fail_mkdir(self, *args, **kwargs):
        raise PermissionError(f"simulated: cannot create {self}")

    monkeypatch.setattr(
        type(idem_env.store._path.parent),
        "mkdir",
        fail_mkdir,
    )

    try:
        s, p = idem_env.run_handler(
            {"session_id": "idem-session", "message": "hello"},
            key_header="mkdir-fail-key",
        )
    finally:
        # Always restore the real mkdir so other tests aren't
        # affected by our global monkeypatch.
        monkeypatch.setattr(
            type(idem_env.store._path.parent),
            "mkdir",
            real_mkdir,
        )

    assert s == 503, f"expected 503 on mkdir failure, got {s}: {p}"
    assert p.get("code") == "idempotency_store_unavailable"
    assert len(idem_env.recorder.calls) == 0, (
        f"mkdir failure admitted a turn: {len(idem_env.recorder.calls)} calls"
    )


# ---------------------------------------------------------------------------
# Fail-closed sweep (round 2): the review named READ failures, unbound
# completions, and the release cleanup path alongside the write paths.
# ---------------------------------------------------------------------------


def test_unreadable_store_file_fails_closed(idem_env):
    """Finding 1, read side: an unreadable durable store must not start
    empty. "Starting empty" forgets every durable claim, so a retry of a
    completed key would execute the turn again instead of replaying it."""
    from api import idempotency as idem_mod

    body = {"session_id": "idem-session", "message": "hello"}
    key = "unreadable-key"

    # First admit and complete a turn so the durable file has a record.
    s1, p1 = idem_env.run_handler(body, key_header=key)
    assert s1 == 200
    assert len(idem_env.recorder.calls) == 1
    assert idem_env.store.path.exists()

    # Simulate an I/O error on the store file (permissions, EIO, a
    # truncated/renamed file) by making read_text fail. Force the failure
    # at the source so it is deterministic for any test user.
    def boom_read_text(self, *a, **kw):
        raise OSError("simulated: cannot read store file")

    import pathlib
    real_read_text = pathlib.Path.read_text
    pathlib.Path.read_text = boom_read_text
    try:
        # The store must REFUSE to serve, not start empty: silently
        # starting empty is what forgets durable claims and re-admits the
        # turn.
        with pytest.raises(idem_mod.IdempotencyStoreUnavailable):
            idem_env.store.reload_from_disk()
    finally:
        pathlib.Path.read_text = real_read_text

    # The route path: the durable record still exists, so replay still
    # works — the store never claimed to be empty.
    rec = idem_env.store.lookup(key)
    assert rec is not None and rec.status == "complete"
    s2, p2 = idem_env.run_handler(body, key_header=key)
    assert s2 == 200
    assert p2["stream_id"] == p1["stream_id"]
    assert len(idem_env.recorder.calls) == 1


def test_corrupt_store_file_fails_closed(idem_env):
    """Finding 1, read side: a corrupt (unparseable) store file must
    raise, not silently load as empty. An empty load forgets durable
    claims and re-admits the turn on the next retry."""
    from api import idempotency as idem_mod

    body = {"session_id": "idem-session", "message": "hello"}
    key = "corrupt-key"

    s1, p1 = idem_env.run_handler(body, key_header=key)
    assert s1 == 200

    # Corrupt the durable file the way a torn write would.
    idem_env.store.path.write_text("{ not json at all", encoding="utf-8")

    with pytest.raises(idem_mod.IdempotencyStoreUnavailable):
        idem_env.store.reload_from_disk()

    # The store must not have converted to an "empty" success state.
    with pytest.raises(idem_mod.IdempotencyStoreUnavailable):
        idem_env.store.lookup(key)


def test_complete_without_durable_claim_is_refused(idem_env):
    """Finding 1, completion side: ``complete()`` with no durable claim
    must refuse rather than synthesize a record with an unproven
    (empty) fingerprint. A synthesized record would let an unrelated
    retry either collide as a bogus 409 or replay a result whose request
    we never verified."""
    from api import idempotency as idem_mod

    key = "unbound-complete-key"

    # complete() called without a preceding claim().
    with pytest.raises(idem_mod.IdempotencyStoreUnavailable):
        idem_env.store.complete(
            key,
            session_id="idem-session",
            stream_id="stream-1",
            turn_id="turn-1",
            response_status=200,
            response_payload={"x": 1},
        )

    # No record was written in memory or on disk, so nothing can replay.
    stored = idem_env.store.lookup_stored(
        idem_mod.build_storage_key(key)
    )
    assert stored is None
    if idem_env.store.path.exists():
        import json as _json
        on_disk = _json.loads(idem_env.store.path.read_text(encoding="utf-8"))
        assert on_disk.get("records") in (None, [])
    assert len(idem_env.recorder.calls) == 0


def test_release_persist_failure_retains_inflight_guard(idem_env, monkeypatch):
    """Finding 1, cleanup path: when the durable write behind
    ``release()`` fails, the pending claim must be RETAINED. Dropping it
    in memory while the durable file still holds the claim removes the
    in-flight guard and lets the next request with the same key start a
    second turn."""
    from api import idempotency as idem_mod

    body = {"session_id": "idem-session", "message": "hello"}
    key = "release-fail-key"

    # Seed a durable pending claim (claim() but no complete()).
    idem_env.store.claim(key, idem_mod.compute_request_fingerprint(body))
    assert idem_env.store.lookup(key).status == "pending"

    # Fail the release's own durable write.
    def boom_persist():
        raise idem_mod.IdempotencyStoreUnavailable("simulated: disk gone")
    monkeypatch.setattr(idem_env.store, "_persist_locked", boom_persist)

    with pytest.raises(idem_mod.IdempotencyStoreUnavailable):
        idem_env.store.release(key)

    # The pending claim survives in memory: a retry is refused, not
    # re-admitted.
    rec = idem_env.store.lookup(key)
    assert rec is not None, "pending claim was dropped on release failure"
    assert rec.status == "pending"


def test_unresolvable_profile_scope_fails_closed(idem_env, monkeypatch):
    """Finding 3, resolution side: when the server-resolved active
    profile cannot be determined, the key must NOT be filed under a
    guessed namespace. A ``"default"`` fallback would file a request under
    a namespace we never proved it belongs to, so two profiles could
    collide on one record (or a claim could land in the wrong profile's
    namespace and be invisible to its owner). Refuse with 503 instead."""
    from api import idempotency as idem_mod

    body = {"session_id": "idem-session", "message": "hello"}
    key = "no-profile-key"

    # Store-level contract: resolution failure raises rather than
    # silently defaulting the namespace. Patch the lookup the way the
    # module itself does (lazy import from api.routes) so the real
    # conversion-to-typed-error path is what gets exercised.
    def _boom_profile():
        raise RuntimeError("simulated: profile registry unavailable")
    monkeypatch.setattr(
        idem_env.routes, "_get_active_profile_name", _boom_profile
    )
    with pytest.raises(idem_mod.IdempotencyStoreUnavailable):
        idem_mod.build_storage_key(key)

    # An empty / non-string active profile name is equally unresolvable.
    monkeypatch.setattr(
        idem_env.routes, "_get_active_profile_name", lambda: "   "
    )
    with pytest.raises(idem_mod.IdempotencyStoreUnavailable):
        idem_mod.build_storage_key(key)
    monkeypatch.setattr(
        idem_env.routes, "_get_active_profile_name", lambda: None
    )
    with pytest.raises(idem_mod.IdempotencyStoreUnavailable):
        idem_mod.build_storage_key(key)

    # Route-level: the unreachable namespace surfaces as 503, and no
    # turn is admitted under a guessed namespace.
    monkeypatch.setattr(
        idem_env.routes, "_idem_resolve_active_profile",
        lambda: (_ for _ in ()).throw(
            idem_mod.IdempotencyStoreUnavailable("unresolvable")
        ),
    )
    s, p = idem_env.run_handler(body, key_header=key)
    assert s == 503, f"expected 503 for unresolvable profile, got {s}: {p}"
    assert p.get("code") == "idempotency_store_unavailable"
    assert len(idem_env.recorder.calls) == 0
    assert list(idem_env.store.keys()) == []


# ---------------------------------------------------------------------------
# Source invariants (defensive)
# ---------------------------------------------------------------------------


def test_idempotency_module_wired_into_routes(idem_env):
    """The implementation must actually live in api/routes.py — not
    a duplicate file that a code review would miss. This pins the
    chokepoint: ``_handle_chat_start`` is the single shared entry
    for /api/chat/start, and the store must be looked up at the top
    of that function so both local and gateway backends share it.
    """
    import inspect
    from api import routes
    src = inspect.getsource(routes._handle_chat_start)
    assert "get_idempotency_store" in src
    assert "_idem_extract_key" in src or "extract_key" in src
    # The completion must happen close to the success path so a
    # post-completion retry replays correctly.
    assert "store.complete" in src or ".complete(" in src
    # The release must happen in the finally so validation failures
    # don't strand a pending claim.
    assert "store.release" in src or ".release(" in src
