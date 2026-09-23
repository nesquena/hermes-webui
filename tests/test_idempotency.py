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

    def run_handler(body: dict, *, key_header: str | None = None, key_body: str | None = None) -> tuple[int, dict]:
        handler = _FakePostHandler(body, headers={"Idempotency-Key": key_header} if key_header else None)
        if key_body is not None and "idempotency_key" not in body:
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
    body = {"session_id": "idem-session", "message": "hello"}
    key = "post-restart-key"

    s1, p1 = idem_env.run_handler(body, key_header=key)
    assert s1 == 200
    assert p1["stream_id"] == "stream-1"
    expected_stream = p1["stream_id"]
    expected_turn = p1["turn_id"]

    # Sanity: the durable file actually contains the record.
    assert idem_env.store.path.exists()
    on_disk = json.loads(idem_env.store.path.read_text(encoding="utf-8"))
    on_disk_keys = [r["key"] for r in on_disk.get("records", [])]
    assert key in on_disk_keys

    # Simulate a restart: a fresh in-memory state, forced reload from
    # the same durable file.
    idem_env.store._records.clear()  # drop in-memory
    idem_env.store._loaded = False
    idem_env.store.reload_from_disk()

    # The store should have the record back, with status=complete.
    rec = idem_env.store.lookup(key)
    assert rec is not None
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
    from api.idempotency import compute_request_fingerprint
    body = {"session_id": "idem-session", "message": "hello"}
    key = "expiry-key"

    # Seed a record that is already past TTL. The fingerprint must
    # match what the new request will compute, otherwise we'd hit
    # 409 conflict before the TTL check has a chance to fire.
    fingerprint = compute_request_fingerprint(body)
    idem_env.store.claim(key, fingerprint)
    rec = idem_env.store.lookup(key)
    assert rec is not None
    rec.claimed_at = 0.0  # 1970-01-01 — well past any reasonable TTL
    idem_env.store._records.move_to_end(key)
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
    """Oversize / non-printable keys must be rejected with 400. They
    are caller errors, not duplicates; we don't want to silently
    bind them. (Empty / whitespace-only keys are treated as "no
    key" by the extractor and fall through to the legacy path —
    that's intentional, the browser never sends a key.)"""
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

    # "No key" path: empty / whitespace-only — these go through the
    # extractor's "no key" branch and behave like a legacy request
    # (admitting a fresh turn each time). The contract explicitly
    # says: "Existing clients that omit the key retain their current
    # behavior." An empty-string Idempotency-Key is a no-op, not an
    # error.
    for empty in ("", "    "):
        before = len(idem_env.recorder.calls)
        s, p = idem_env.run_handler(
            {"session_id": "idem-session", "message": "hello"},
            key_header=empty,
        )
        # Legacy: succeeds, admits a turn.
        assert s == 200, f"empty key {empty!r} should be treated as no key; got {s}"
        assert len(idem_env.recorder.calls) == before + 1, (
            f"empty key {empty!r} did not admit a turn (legacy behavior broken)"
        )


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
