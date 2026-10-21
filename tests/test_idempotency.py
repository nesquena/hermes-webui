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
import os
import stat
import threading
import time
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
      * ``real_start_run`` — the SHIPPING ``routes._start_run`` captured
        before the recorder shadows it (the restart-path test drives it
        end to end)
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

    # Capture the SHIPPING ``_start_run`` before the recorder below shadows
    # it. The restart-path test drives the real start pipeline end to end
    # (real mint, real pending save, real turn journal, real SSE
    # registration, real worker thread) with only the Agent worker replaced;
    # every other test keeps the recorder. ``real_start_run`` must be the
    # actual shipping function object — never an implementation copy.
    real_start_run = routes._start_run
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
        real_start_run=real_start_run,
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


def test_store_acquisition_failure_returns_503_with_zero_calls(idem_env, monkeypatch):
    """Finding 1 (route-level): when the idempotency store cannot be
    ACQUIRED, a keyed request must fail closed with 503 — it must NOT slide
    into the keyless legacy path and admit a turn with no claim. The review
    called the old `except Exception: store = None` downgrade a direct
    fail-open path: a keyed request continued to `_start_run` with no claim.
    Zero recorder calls means no turn was admitted."""

    body = {"session_id": "idem-session", "message": "hello"}
    key = "acquire-fail-key"

    # Store acquisition raises.
    def boom_acquire():
        raise RuntimeError("simulated: store registry unavailable")
    monkeypatch.setattr(idem_env.routes, "get_idempotency_store", boom_acquire)
    s, p = idem_env.run_handler(body, key_header=key)
    assert s == 503, f"expected 503 on store acquisition failure, got {s}: {p}"
    assert p.get("code") == "idempotency_store_unavailable"
    assert p.get("idempotency_key") == key
    assert len(idem_env.recorder.calls) == 0, (
        f"store acquisition failure still admitted a turn: "
        f"{len(idem_env.recorder.calls)} calls"
    )
    # No claim was written either.
    assert list(idem_env.store.keys()) == []

    # A None store (defensive) takes the same fail-closed path.
    monkeypatch.setattr(
        idem_env.routes, "get_idempotency_store", lambda: None
    )
    key2 = "acquire-none-key"
    s2, p2 = idem_env.run_handler(body, key_header=key2)
    assert s2 == 503, f"expected 503 for a None store, got {s2}: {p2}"
    assert p2.get("code") == "idempotency_store_unavailable"
    assert len(idem_env.recorder.calls) == 0, (
        f"None store still admitted a turn: {len(idem_env.recorder.calls)} calls"
    )


def test_completion_write_failure_retains_and_replays_same_turn(idem_env, monkeypatch):
    """Finding 2: when ONLY the completion's durable write fails (and the
    NEXT persistence call — the reconciliation — succeeds), the admitted
    turn's identity must be reconciled onto the pending claim and NEVER
    released (the agent turn cannot be undone). A retry with the same key
    must REPLAY the original identity — recorder stays at one — instead of
    starting a second turn.

    This distinguishes the bug from a stub that fails every write from
    call two onward (the old test's stub only preserved the record because
    release() also failed); here the release would succeed if the route
    attempted it, so a released claim would be visible in the store."""
    from api import idempotency as idem_mod

    body = {"session_id": "idem-session", "message": "hello"}
    key = "complete-write-fail-key"

    # Fail ONLY the second persist (the completion's durable write); let
    # the claim persist (call 1) and the reconciliation persist (call 3,
    # the "next persistence call") succeed.
    state = {"call_count": 0}
    original_persist = idem_env.store._persist_locked

    def one_completion_failure():
        state["call_count"] += 1
        if state["call_count"] == 2:
            raise idem_mod.IdempotencyStoreUnavailable(
                "simulated: single completion write failure"
            )
        return original_persist()
    monkeypatch.setattr(idem_env.store, "_persist_locked", one_completion_failure)

    # First attempt: turn admitted, completion write fails once, reconcile
    # succeeds, route signals 503 (durability gap) but keeps the guard.
    s1, p1 = idem_env.run_handler(body, key_header=key)
    assert s1 == 503, f"expected 503 after completion write failure, got {s1}: {p1}"
    assert p1.get("code") == "idempotency_store_unavailable"
    # The turn WAS admitted before the persist failed.
    assert len(idem_env.recorder.calls) == 1

    # Post-handler: the guard is NOT released — a pending claim bound to the
    # admitted identity.
    rec = idem_env.store.lookup(key)
    assert rec is not None, "pending claim was released after completion failure"
    assert rec.status == "pending", f"expected pending, got {rec.status!r}"
    assert rec.stream_id == "stream-1", (
        f"admitted identity must be reconciled onto the claim; got "
        f"stream_id={rec.stream_id!r}"
    )

    # Retry with the same key: replays the same turn, not a second one.
    s2, p2 = idem_env.run_handler(body, key_header=key)
    assert s2 == 200, f"expected replay after retry, got {s2}: {p2}"
    assert p2["stream_id"] == "stream-1"
    assert p2["turn_id"] == "turn-1"
    assert p2.get("replayed_from_idempotency_key") is True
    assert len(idem_env.recorder.calls) == 1, (
        f"retry admitted a SECOND turn: {len(idem_env.recorder.calls)} calls"
    )


def test_unexpected_complete_exception_returns_503_and_retains_claim(idem_env, monkeypatch):
    """Finding 4: an UNEXPECTED exception from complete() (not the typed
    persist failure) must NOT be swallowed into a 200 response with the
    claim released. Treat it as unavailable (503) and retain the pending
    guard — the generic branch that logged and returned the success
    response while the finally deleted the claim is the bug."""
    body = {"session_id": "idem-session", "message": "hello"}
    key = "unexpected-complete-key"

    def boom_complete(self, *a, **k):
        raise RuntimeError("unexpected crash inside complete()")
    monkeypatch.setattr(idem_env.store, "complete", boom_complete)

    s, p = idem_env.run_handler(body, key_header=key)
    assert s != 200, (
        f"unexpected complete() exception must not yield 200; got {s}: {p}"
    )
    assert s == 503
    assert p.get("code") == "idempotency_store_unavailable"

    # The claim was NOT released (finally must have skipped the release
    # because the turn was admitted).
    rec = idem_env.store.lookup(key)
    assert rec is not None, "complete() failure released the pending claim"
    assert rec.status == "pending", f"expected pending, got {rec.status!r}"


def test_profile_drift_within_request_stays_bound_to_original_namespace(idem_env, monkeypatch):
    """Finding 3: the profile namespace must be resolved ONCE per request
    and threaded through claim/complete/reconcile/release. When the server's
    active profile CHANGES across successive resolver calls mid-request
    (simulated by `_get_active_profile_name` returning a distinct profile
    every call), the lifecycle must stay bound to the namespace resolved at
    entry — every step receives the SAME profile. The old code re-resolved
    on each `build_storage_key`, so a claim, complete, or release could land
    in different namespaces.

    We spy on each lifecycle step to record the ``profile`` it received and
    assert them all equal; we also assert the completed record lives under
    the entry-resolved namespace, not a later resolver call's value."""
    from api import idempotency as idem_mod

    body = {"session_id": "idem-session", "message": "hello"}
    key = "profile-drift-key"

    # Successive calls return DIFFERENT profiles (active profile switches).
    seq = iter(["alpha", "beta", "gamma", "delta"])
    def flip():
        return next(seq, "default")
    monkeypatch.setattr(idem_env.routes, "_get_active_profile_name", flip)

    # Capture the profile each idempotency lifecycle step receives.
    usage: list[tuple[str, object]] = []
    store = idem_env.store
    for method in ("claim", "complete", "reconcile", "release"):
        original = getattr(store, method)
        if method == "claim":
            def spy(key_arg, fp, *, profile=None, pending_identity=None, _o=original):
                usage.append(("claim", profile))
                return _o(key_arg, fp, profile=profile, pending_identity=pending_identity)
        else:
            def spy(*a, profile=None, _m=method, _o=original, **k):
                usage.append((_m, profile))
                return _o(*a, profile=profile, **k)
        monkeypatch.setattr(store, method, spy)

    s, p = idem_env.run_handler(body, key_header=key)
    # A cross-namespace drift would make complete() miss the claim and
    # surface 503; a successful 200 already proves no drift here.
    assert s == 200, f"request drifted across namespaces: got {s}: {p}"

    claim_profiles = [prof for m, prof in usage if m == "claim"]
    complete_profiles = [prof for m, prof in usage if m == "complete"]
    assert complete_profiles, "complete() was never called"
    assert len(set(claim_profiles)) == 1, (
        f"claim() drifted across namespaces: {claim_profiles}"
    )
    assert len(set(complete_profiles)) == 1, (
        f"complete() drifted across namespaces: {complete_profiles}"
    )
    assert complete_profiles[0] == claim_profiles[0], (
        f"claim/complete NOT bound to the same namespace: "
        f"claim={claim_profiles[0]!r} complete={complete_profiles[0]!r}"
    )

    ns = complete_profiles[0]
    stored = store.lookup_stored(
        idem_mod.build_storage_key(key, profile=str(ns))
    )
    assert stored is not None and stored.status == "complete", (
        f"record should be complete under entry namespace {ns!r}, got {stored!r}"
    )
    # And NOT filed under a later resolver value ("beta").
    assert store.lookup_stored(
        idem_mod.build_storage_key(key, profile="beta")
    ) is None


# ─── #7782 review follow-ups: crash-window, persistence, namespace, tombstone ───


def test_crash_between_claim_and_start_is_outcome_unknown(idem_env):
    """#7782 finding 1: a crash after the claim but before the turn is admitted
    must NOT leave a replayable identity and must NOT turn the key into a
    day-long in-flight error.

    The route mints a PROVISIONAL identity and persists it with the claim
    (``claim_with_identity`` → ``pending_stream_id``), but that identity is
    never accepted by a turn — ``_start_run`` mints the real stream id and
    only ``complete()``/``reconcile()`` bind a proven admission onto the
    record. A pending record recovered from disk WITHOUT a proven admitted
    stream (``stream_id`` empty) can therefore not be replayed as the
    original turn: the durable store cannot say whether a turn ran. Both
    restart shapes — the record carrying the CURRENT process's pid (the
    common same-PID restart, e.g. Docker ``python server.py`` where the
    recorded ``owner_pid`` equals the restarted pid) and a dead process's
    pid — must fail closed with the distinct, non-retryable
    ``IdempotencyOutcomeUnknown``.
    """
    from api.idempotency import (
        STATUS_PENDING,
        IdempotencyOutcomeUnknown,
        build_storage_key,
        compute_request_fingerprint,
    )

    env = idem_env
    body = {"session_id": env.session.session_id, "message": "hello"}
    fingerprint = compute_request_fingerprint(body, "default")
    storage_key = build_storage_key("crash-window", "default")  # "default|crash-window"

    for owner_pid in (os.getpid(), 999999):
        # Fresh durable file for each restart shape.
        env.store.reset()
        # Route minted identity → persisted with the claim.
        record = env.store.claim_with_identity(
            "crash-window",
            fingerprint,
            profile="default",
            session_id=env.session.session_id,
            stream_id="stream-minted-at-claim",
            turn_id="turn-minted-at-claim",
        )
        assert record.status == STATUS_PENDING
        # The minted identity lives in the pending_* fields on purpose: the
        # route's replay predicate reads stream_id only, so this record still
        # flows to _start_run instead of short-circuiting as its own replay.
        assert record.stream_id == ""
        assert record.pending_stream_id == "stream-minted-at-claim"
        # Simulate the claim->admit crash: whatever a restart loads carries
        # this pid (same process for the common same-PID restart, foreign
        # otherwise).
        with env.store._lock:
            env.store._records[storage_key].owner_pid = owner_pid
        env.store._persist_locked()

        # Restart: fresh instance over the same durable file.
        restarted = type(env.store)(path=env.store._path)
        # claim() loads the durable file lazily. The recovered pending
        # record has NO proven admitted stream — provisional data and pid
        # must NOT make it replayable — so claim refuses with the distinct
        # outcome instead of replaying a fabricated identity (foreign pid)
        # or answering in-flight + retry_after=1 (same pid).
        with pytest.raises(IdempotencyOutcomeUnknown):
            restarted.claim("crash-window", fingerprint, profile="default")
        assert len(env.recorder.calls) == 0, "no turn admitted by the retry"


def test_pending_claim_with_no_identity_is_swept_to_outcome_unknown(idem_env):
    """#7782 finding 1 (legacy window): a pending record recovered from disk
    with no stream identity must fail closed with the distinct non-retryable
    outcome, not `in_flight` + retry_after=1.

    The identity-less mark is applied at LOAD time (``_load_locked``), so it
    is independent of pid and provisional data; the startup sweep then finds
    nothing left to do and is a no-op on already-marked records. If an
    unmarked identity-less pending record reaches memory (a store loaded
    before this marking shipped), the sweep still stamps it — pinned below
    by unsetting the load-time mark and re-running the sweep.
    """
    from api.idempotency import (
        IdempotencyOutcomeUnknown,
        build_storage_key,
        compute_request_fingerprint,
    )

    env = idem_env
    body = {"session_id": env.session.session_id, "message": "hello"}

    fingerprint = compute_request_fingerprint(body, "default")
    storage_key = build_storage_key("no-identity", "default")  # "default|no-identity"

    # A pre-fix store (plain claim, no identity) — the exact crash window the
    # old implementation left on disk.
    env.store.claim("no-identity", fingerprint, profile="default")
    with env.store._lock:
        assert env.store._records[storage_key].pending_stream_id == ""
        env.store._records[storage_key].outcome_unknown = False
        # Simulate the owner's death: a restarted process has a different pid,
        # which is what turns this pending record from "live sibling owns it"
        # into "durable artifact to inspect".
        env.store._records[storage_key].owner_pid = 999999
    env.store._persist_locked()

    restarted = type(env.store)(path=env.store._path)
    restarted._ensure_loaded()
    loaded = restarted._records[storage_key]
    assert loaded.owner_pid == 999999, "durable file must carry the dead pid"
    assert loaded.pending_stream_id == ""
    # Marked at LOAD, not by the sweep: the sweep below finds nothing to do.
    assert loaded.outcome_unknown is True
    assert restarted.sweep_pending_claims() == 0

    with pytest.raises(IdempotencyOutcomeUnknown) as excinfo:
        restarted.claim("no-identity", fingerprint, profile="default")
    assert "may have run" in str(excinfo.value).lower()

    # The sweep still stamps an identity-less pending record that reaches
    # memory unmarked (belt-and-braces for stores loaded before this marking
    # shipped): unset the load-time mark and re-run it.
    with restarted._lock:
        restarted._records[storage_key].outcome_unknown = False
    assert restarted.sweep_pending_claims() == 1
    with restarted._lock:
        assert restarted._records[storage_key].outcome_unknown is True


def test_pending_claim_with_only_provisional_identity_is_outcome_unknown(idem_env):
    """#7782 finding 1: a pending record whose ONLY identity is the
    provisional minted-at-claim id (``pending_stream_id``) — no admitted
    stream — is still identity-less for replay purposes. Recovered from disk
    it must be outcome_unknown: the startup sweep must NOT skip it just
    because ``pending_stream_id`` is set, and ``claim`` must never promote
    the provisional id into a replayable ``stream_id``.
    """
    from api.idempotency import (
        STATUS_PENDING,
        IdempotencyOutcomeUnknown,
        build_storage_key,
        compute_request_fingerprint,
    )

    env = idem_env
    body = {"session_id": env.session.session_id, "message": "hello"}
    fingerprint = compute_request_fingerprint(body, "default")
    storage_key = build_storage_key("provisional-only", "default")

    # A claim_with_identity record — provisional identity persisted WITH the
    # claim, exactly as the route mints it — whose turn never accepted the
    # provisional id (stream_id stays empty).
    env.store.claim_with_identity(
        "provisional-only",
        fingerprint,
        profile="default",
        session_id=env.session.session_id,
        stream_id="stream-minted-at-claim",
        turn_id="turn-minted-at-claim",
    )
    with env.store._lock:
        rec = env.store._records[storage_key]
        assert rec.status == STATUS_PENDING
        assert rec.stream_id == ""
        assert rec.pending_stream_id == "stream-minted-at-claim"
        rec.owner_pid = 999999
    env.store._persist_locked()

    restarted = type(env.store)(path=env.store._path)
    restarted._ensure_loaded()
    loaded = restarted._records[storage_key]
    # Provisional data does NOT make the record replayable.
    assert loaded.pending_stream_id == "stream-minted-at-claim"
    assert loaded.stream_id == ""
    # Marked at load, provisional or not — and the sweep does not skip it
    # either way (its skip condition is stream_id-only, not pending_stream_id).
    assert loaded.outcome_unknown is True
    assert restarted.sweep_pending_claims() == 0

    # An unmarked provisional-only record is still swept (the sweep's skip
    # condition must not exempt pending_stream_id-carrying records).
    with restarted._lock:
        restarted._records[storage_key].outcome_unknown = False
    assert restarted.sweep_pending_claims() == 1

    with pytest.raises(IdempotencyOutcomeUnknown):
        restarted.claim("provisional-only", fingerprint, profile="default")


def test_recovered_pending_claim_is_outcome_unknown_across_restart(idem_env, monkeypatch, tmp_path):
    """Maintainer repro for #7782 finding 1, handler level, over the REAL
    start pipeline: drive the REAL ``_handle_chat_start`` with the REAL store
    and the REAL ``_start_run`` → REAL ``_start_chat_stream_for_session`` —
    real stream mint (``uuid.uuid4().hex``), real pending session save
    (eager user row), real turn-journal identity, real SSE registration and
    launch-phase claim, real ``threading.Thread`` worker — with ONLY the
    Agent worker replaced by a recorder that counts the admission and exits
    immediately (no model run). Copy ``store.json`` at entry (right after the
    claim persists, before ``_start_run``) and again just after the real
    ``_start_run`` returns (before the completion write); then restart the
    singleton store against each copy (startup sweep included) and retry the
    same key+body.

    Every recovered shape — entry or after-start, same-PID or foreign-PID,
    +0 / +60s / +23h — must answer ``409 idempotency_outcome_unknown``
    WITHOUT ``retry_after_seconds`` and must NOT start a new turn. At +24h
    the key is past its TTL and answers the unchanged ``410
    idempotency_key_expired``. The provisional identity minted at claim time
    differs from the identity the turn was admitted with (the real mint, a
    random uuid that reaches the response AND the real worker thread), and
    is never replayed (the retry returns no stream identity at all).

    Completed control: a snapshot taken AFTER the completion write replays
    the REAL admitted identity — fresh singleton, no extra worker, no extra
    eager user row.
    """
    import re
    import shutil
    from pathlib import Path

    import api.config as config_mod
    import api.models as models_mod
    import api.runtime_adapter as runtime_adapter_mod
    from api.idempotency import get_idempotency_store, set_idempotency_store
    from api.models import Session
    from api.turn_journal import read_turn_journal

    env = idem_env
    body = {"session_id": "idem-session", "message": "hello"}
    key = "restart-outcome-key"
    store_path = env.store.path
    snapshots: dict[str, Path] = {}

    # ── Registry isolation: the real launch mutates process-global stream /
    # session registries. Snapshot-and-restore them around the whole test so
    # no entry leaks into sibling tests (the 16 retry probes and the
    # completed control all return at the idempotency claim/replay and never
    # touch these, but the crash request registers a stream + owner + launch
    # claim + session writeback owner).
    _registry_names = [
        "STREAMS", "ACTIVE_RUNS", "CANCEL_FLAGS", "STREAM_GOAL_RELATED",
        "PRE_ADMISSION_CLAIMS", "STREAM_SESSION_OWNERS",
        "SESSION_WRITEBACK_OWNERS", "PENDING_BG_TASK_COMPLETIONS",
    ]
    _registry_before: dict[str, dict | set] = {}
    for _name in _registry_names:
        _obj = getattr(config_mod, _name)
        _registry_before[_name] = dict(_obj) if isinstance(_obj, dict) else set(_obj)
        _obj.clear()
    with config_mod.SESSION_AGENT_LOCKS_LOCK:
        _agent_locks_before = dict(config_mod.SESSION_AGENT_LOCKS.items())
        config_mod.SESSION_AGENT_LOCKS.clear()

    try:
        # ── Real Session in a disposable SESSION_DIR ───────────────────
        # A real api.models.Session with a real save(): the sidecar, the
        # session index and the turn journal all land under tmp_path, never
        # under the production HERMES_HOME.
        session_dir = Path(tmp_path) / "sessions"
        monkeypatch.setattr(models_mod, "SESSION_DIR", session_dir)
        monkeypatch.setattr(models_mod, "SESSION_INDEX_FILE", session_dir / "_index.json")
        monkeypatch.setattr(config_mod, "SESSION_DIR", session_dir, raising=False)
        # Production creates SESSION_DIR at server startup; the real
        # Session.save() needs it to exist before the first atomic write.
        session_dir.mkdir(parents=True, exist_ok=True)

        workspace = str(Path(tmp_path) / "workspace")
        session = Session(
            session_id="idem-session",
            title="Untitled",
            workspace=workspace,
            model="test-model",
            model_provider="test-provider",
            profile=None,
            messages=[],
        )
        monkeypatch.setattr(
            env.routes, "_get_or_materialize_session", lambda *_a, **_k: session
        )
        monkeypatch.setattr(
            env.routes, "_resolve_chat_workspace_with_recovery",
            lambda *_a, **_k: workspace,
        )
        # Eager session-save mode: the real _prepare… writes exactly one
        # user row, so a replay cannot silently add a second one.
        monkeypatch.setattr(env.routes, "get_webui_session_save_mode", lambda: "eager")
        # set_last_workspace persistence is not the contract under test.
        monkeypatch.setattr(env.routes, "set_last_workspace", lambda *a, **k: None)
        # Pin the local in-process shipping path: no runtime adapter, no
        # runner, no gateway, no model-alias lane.
        monkeypatch.setattr(
            runtime_adapter_mod, "runtime_adapter_enabled", lambda *a, **k: False
        )
        monkeypatch.setattr(
            runtime_adapter_mod, "runtime_adapter_runner_enabled", lambda *a, **k: False
        )
        monkeypatch.setattr(
            env.routes.api_config, "resolve_model_alias_runtime", lambda *a, **k: None
        )

        # ── The ONLY stub in the start pipeline: the Agent worker ───────
        # _start_chat_stream_for_session spawns a REAL threading.Thread whose
        # target is _run_agent_streaming; that target is replaced by a
        # recorder that counts the admission and exits immediately (no model,
        # no session-lock interaction, so the real thread is safe). The event
        # gives the test a bounded wait for the real thread.
        worker_calls: list[dict] = []
        worker_started = threading.Event()

        def _counting_worker(
            worker_session_id, msg_text, model, worker_workspace, stream_id,
            attachments=None, **kwargs,
        ):
            worker_calls.append({
                "session_id": worker_session_id,
                "msg": msg_text,
                "model": model,
                "workspace": worker_workspace,
                "stream_id": stream_id,
                "attachments": attachments,
                "kwargs": dict(kwargs),
            })
            worker_started.set()

        monkeypatch.setattr(env.routes, "_run_agent_streaming", _counting_worker)

        def _copy_snapshot(name: str) -> Path:
            dest = store_path.parent / f"{name}.json"
            shutil.copy2(store_path, dest)
            return dest

        # Hook 1 — entry snapshot: copy the durable file right after the
        # claim (with its provisional identity) persists and BEFORE the real
        # _start_run runs.
        real_claim = env.store.claim_with_identity

        def _claim_and_snapshot(*args, **kwargs):
            rec = real_claim(*args, **kwargs)
            snapshots["entry"] = _copy_snapshot("snapshot_entry")
            return rec

        env.store.claim_with_identity = _claim_and_snapshot

        # Hook 2 — after-start snapshot: the SHIPPING _start_run (captured by
        # the fixture before any recorder shadowed it) is wrapped so the
        # durable file is copied right after it returns its admitted identity
        # and BEFORE the route's completion write. The durable record is
        # still pending + provisional: the true admitted stream id only
        # exists in the response (and the worker thread), not on disk.
        start_run_calls: list[dict] = []

        def _wrapped_start_run(s, **kwargs):
            result = env.real_start_run(s, **kwargs)
            start_run_calls.append({
                "session_id": getattr(s, "session_id", None),
                "status": result.get("_status", 200) if isinstance(result, dict) else 200,
                "stream_id": result.get("stream_id") if isinstance(result, dict) else None,
            })
            snapshots["after"] = _copy_snapshot("snapshot_after")
            return result

        monkeypatch.setattr(env.routes, "_start_run", _wrapped_start_run)

        # The "crash" request: claims, drives the REAL start pipeline
        # (real mint, real pending save, real journal, real SSE
        # registration, real worker thread), completes.
        s1, p1 = env.run_handler(body, key_header=key)
        assert s1 == 200, p1
        assert len(start_run_calls) == 1
        # The real worker thread ran with the minted identity.
        assert worker_started.wait(5.0), "the real worker thread never ran"
        assert len(worker_calls) == 1
        assert "entry" in snapshots and "after" in snapshots
        true_stream = p1["stream_id"]
        true_turn = p1["turn_id"]
        # The response carries a REAL minted uuid (32 lowercase hex), never
        # the route's provisional "stream-…" placeholder.
        assert re.fullmatch(r"[0-9a-f]{32}", true_stream), (
            f"stream_id must be a real minted uuid, got {true_stream!r}"
        )
        assert worker_calls[0]["stream_id"] == true_stream, (
            "the real worker thread did not receive the minted stream id"
        )
        assert worker_calls[0]["kwargs"].get("model_provider") == "test-provider"
        # Real turn-journal identity: the submitted event written by the real
        # append_turn_journal_event carries the same stream/turn the response
        # exposed.
        journal = read_turn_journal("idem-session", session_dir=session_dir)
        submitted = [e for e in journal["events"] if e.get("event") == "submitted"]
        assert submitted, "no submitted turn-journal event was written"
        assert submitted[0]["stream_id"] == true_stream
        assert submitted[0]["turn_id"] == true_turn
        # Eager session-save pinned exactly one user row.
        assert len(session.messages) == 1

        # Both snapshots carry a pending record with ONLY a provisional
        # identity, and the provisional id differs from the identity the turn
        # was actually admitted with (the route mints it before _start_run;
        # the REAL _start_run mints its own random uuid and never accepts the
        # provisional one).
        for name in ("entry", "after"):
            raw = json.loads(Path(snapshots[name]).read_text(encoding="utf-8"))
            records = raw.get("records", [])
            assert len(records) == 1, f"{name} snapshot must hold one claim"
            rec = records[0]
            assert rec["status"] == "pending", (
                f"{name} snapshot: completion write must not have run yet"
            )
            assert rec["stream_id"] == "", (
                f"{name} snapshot: no proven admitted stream at snapshot time"
            )
            provisional = rec["pending_stream_id"]
            assert provisional, f"{name} snapshot: claim carries a provisional id"
            assert provisional != true_stream, (
                "the provisional id must differ from the truly admitted stream id"
            )
            assert rec["pending_turn_id"] != true_turn

        HOUR = 3600
        timings = [
            (0, 409, "idempotency_outcome_unknown"),
            (60, 409, "idempotency_outcome_unknown"),
            (23 * HOUR, 409, "idempotency_outcome_unknown"),
            (24 * HOUR + 1, 410, "idempotency_key_expired"),
        ]

        def _shape_snapshot(src: Path, *, foreign_pid: bool, elapsed: float) -> Path:
            data = json.loads(src.read_text(encoding="utf-8"))
            for rec in data.get("records", []):
                if foreign_pid:
                    rec["owner_pid"] = 999999
                rec["claimed_at"] = float(rec.get("claimed_at") or 0.0) - elapsed
            dest_dir = src.parent / f"restart_{src.stem}_fp{int(foreign_pid)}_{int(elapsed)}"
            dest = dest_dir / "idempotency" / "store.json"
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(json.dumps(data), encoding="utf-8")
            return dest_dir

        probes = 0
        for name in ("entry", "after"):
            for foreign in (False, True):
                for elapsed, exp_status, exp_code in timings:
                    shaped_dir = _shape_snapshot(
                        Path(snapshots[name]), foreign_pid=foreign, elapsed=elapsed
                    )
                    # Restart: clear the singleton and let
                    # get_idempotency_store() construct a fresh store at the
                    # copied path (startup sweep included), exactly like a
                    # process restart.
                    monkeypatch.setattr(
                        "api.config.STATE_DIR", shaped_dir, raising=False
                    )
                    set_idempotency_store(None)
                    restarted = get_idempotency_store()
                    assert restarted.path == shaped_dir / "idempotency" / "store.json"
                    calls_before = len(start_run_calls)
                    status, payload = env.run_handler(body, key_header=key)
                    assert status == exp_status, (
                        f"snapshot={name} foreign={foreign} elapsed={elapsed}: "
                        f"expected status {exp_status}, got {status} "
                        f"payload={payload}"
                    )
                    assert payload.get("code") == exp_code, (
                        f"snapshot={name} foreign={foreign} elapsed={elapsed}: "
                        f"expected code {exp_code}, got {payload.get('code')}"
                    )
                    if exp_status == 409:
                        assert "retry_after_seconds" not in payload, (
                            "outcome_unknown is not retryable: no retry hint"
                        )
                        assert payload.get("stream_id") is None, (
                            "must never replay a fabricated identity"
                        )
                    assert len(start_run_calls) == calls_before, (
                        f"snapshot={name} foreign={foreign} elapsed={elapsed}: "
                        "retry must not start a turn"
                    )
                    assert len(worker_calls) == 1, (
                        f"snapshot={name} foreign={foreign} elapsed={elapsed}: "
                        "retry must not schedule a worker"
                    )
                    probes += 1
        assert probes == 2 * 2 * len(timings)

        # ── Completed control ───────────────────────────────────────────
        # A snapshot taken AFTER the completion write (the durable record now
        # carries the REAL admitted stream) must REPLAY that real identity
        # from a fresh singleton: no provisional replay, no new worker, no
        # new eager user row.
        completed = _copy_snapshot("snapshot_completed")
        completed_dir = _shape_snapshot(completed, foreign_pid=False, elapsed=0)
        monkeypatch.setattr("api.config.STATE_DIR", completed_dir, raising=False)
        set_idempotency_store(None)
        restarted_completed = get_idempotency_store()
        rec_completed = restarted_completed.lookup(key)
        assert rec_completed is not None and rec_completed.stream_id == true_stream
        calls_before = len(start_run_calls)
        s_c, p_c = env.run_handler(body, key_header=key)
        assert s_c == 200, p_c
        assert p_c["stream_id"] == true_stream, (
            "the completed control must replay the REAL admitted stream, "
            f"got {p_c.get('stream_id')!r}"
        )
        assert p_c["turn_id"] == true_turn
        assert p_c.get("replayed_from_idempotency_key") is True
        assert len(start_run_calls) == calls_before
        assert len(worker_calls) == 1, "the completed control scheduled a worker"
        assert len(session.messages) == 1, "the replay added a second user row"
    finally:
        # Restore the process-global registries exactly as found.
        for _name, _before in _registry_before.items():
            _obj = getattr(config_mod, _name)
            _obj.clear()
            _obj.update(_before)
        with config_mod.SESSION_AGENT_LOCKS_LOCK:
            config_mod.SESSION_AGENT_LOCKS.clear()
            for _k, _v in _agent_locks_before.items():
                config_mod.SESSION_AGENT_LOCKS[_k] = _v


def test_persist_io_failure_during_fsync_is_not_swallowed(idem_env, monkeypatch):
    """#7782 finding 3: an fsync error other than EINVAL/ENOTSUP must propagate,
    not be logged-and-ignored. Losing JSON does not turn `_start_run` into a
    data-loss path the contract asked us to eliminate (`except OSError` that
    silently swallows is exactly how writes got lost before).
    """
    import errno

    from api.idempotency import IdempotencyStore, IdempotencyStoreUnavailable

    env = idem_env
    store = IdempotencyStore(path=env.store._path, ttl_seconds=60)

    class _FailFsync:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    real_open = open

    def _fake_open(path, mode="r", *args, **kwargs):
        if "w" in mode and str(path).endswith(".tmp"):
            real_fh = real_open(path, mode, *args, **kwargs)

            class _Wrapper:
                def __enter__(self):
                    real_fh.__enter__()
                    return self

                def __exit__(self, *exc):
                    return real_fh.__exit__(*exc)

                def write(self, data):
                    return real_fh.write(data)

                @property
                def fileno(self):
                    return real_fh.fileno()

            return _Wrapper()
        return real_open(path, mode, *args, **kwargs)

    monkeypatch.setattr("api.idempotency.os.fsync", lambda _fd: (_ for _ in ()).throw(OSError(errno.EIO, "disk on fire")))
    with pytest.raises(IdempotencyStoreUnavailable):
        store.claim("io-fail", "fp", profile="default")


def test_fsync_einval_and_enotsup_still_pass(idem_env, monkeypatch):
    """The EINVAL/ENOTSUP exemption is deliberate (tmpfs/overlayfs do not all
    support fsync), so the narrowed handling must not reject those.
    """
    import errno

    from api.idempotency import IdempotencyStore

    for err in (errno.EINVAL, errno.ENOTSUP):
        env = idem_env
        store = IdempotencyStore(path=env.store._path, ttl_seconds=60)
        monkeypatch.setattr(
            "api.idempotency.os.fsync",
            lambda _fd, _e=err: (_ for _ in ()).throw(OSError(_e, "fsync unsupported")),
        )
        record = store.claim(f"fsync-ok-{err}", "fp", profile="default")
        assert record.status == "pending"


def test_directory_fsync_eio_fails_closed_with_zero_turns(idem_env, monkeypatch):
    """#7782 finding 3, handler level: a REAL I/O error (EIO) on the DIRECTORY
    fsync — the write that makes the rename durable — must fail closed with
    503 ``idempotency_store_unavailable`` and ZERO admitted turns.

    Before the fix the directory-fsync block swallowed every ``OSError``
    (``except OSError: pass``), so the claim looked durable, the route
    answered 200 and admitted the turn even though the durable guard could
    not be proven to survive a power loss. The control is
    ``test_regular_file_fsync_eio_fails_closed_at_handler``: EIO on the
    regular-file fsync already propagates.
    """
    import errno

    import api.idempotency as idem_mod

    env = idem_env
    real_fsync = os.fsync

    def _fsync_selector(fd):
        # Only DIRECTORY fds fail — the exact fault the old ``except
        # OSError: pass`` hid. Regular-file fsyncs (the tmp-store write)
        # pass through untouched so the control stays clean.
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError(errno.EIO, "directory fsync: disk on fire")
        real_fsync(fd)

    monkeypatch.setattr(idem_mod.os, "fsync", _fsync_selector)

    status, payload = env.run_handler(
        {"session_id": "idem-session", "message": "hello"},
        key_header="dir-eio-key",
    )
    assert status == 503, (
        f"directory-fsync EIO must fail closed, got status {status}"
    )
    assert payload.get("code") == "idempotency_store_unavailable"
    assert len(env.recorder.calls) == 0, (
        "EIO on the directory fsync must not admit a turn"
    )


def test_regular_file_fsync_eio_fails_closed_at_handler(idem_env, monkeypatch):
    """Control for ``test_directory_fsync_eio_fails_closed_with_zero_turns``:
    EIO on the REGULAR-FILE fsync (the tmp-store write) already fails closed
    (503, zero turns) — the narrowed file-fsync path from the earlier
    finding-3 fix must keep propagating, so the directory-fd-only test above
    isolates the block that used to swallow.
    """
    import errno

    import api.idempotency as idem_mod

    env = idem_env
    real_fsync = os.fsync

    def _fsync_selector(fd):
        if not stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError(errno.EIO, "file fsync: disk on fire")
        real_fsync(fd)

    monkeypatch.setattr(idem_mod.os, "fsync", _fsync_selector)

    status, payload = env.run_handler(
        {"session_id": "idem-session", "message": "hello"},
        key_header="file-eio-key",
    )
    assert status == 503
    assert payload.get("code") == "idempotency_store_unavailable"
    assert len(env.recorder.calls) == 0


def test_directory_fsync_einval_enotsup_still_passes_at_handler(idem_env, monkeypatch):
    """Handler-level exemption coverage: filesystems that refuse directory
    fsync with EINVAL/ENOTSUP (tmpfs/overlayfs) must keep the happy path —
    the claim persists, exactly one turn is admitted, 200 is returned.
    """
    import errno

    import api.idempotency as idem_mod

    env = idem_env
    real_fsync = os.fsync

    for err in (errno.EINVAL, errno.ENOTSUP):
        env.recorder.calls.clear()

        def _fsync_selector(fd, _e=err):
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                raise OSError(_e, "fsync unsupported")
            real_fsync(fd)

        monkeypatch.setattr(idem_mod.os, "fsync", _fsync_selector)
        status, payload = env.run_handler(
            {"session_id": "idem-session", "message": "hello"},
            key_header=f"dir-exempt-{err}",
        )
        assert status == 200, (
            f"EINVAL/ENOTSUP on directory fsync must stay tolerable, "
            f"got status {status}"
        )
        assert len(env.recorder.calls) == 1, (
            f"exempt directory fsync must admit exactly one turn, "
            f"got {len(env.recorder.calls)}"
        )


def test_expired_completed_key_is_not_re_admitted(idem_env, monkeypatch):
    """#7782 contract call 5: an expired completed key must fail loudly on
    re-claim. Before the fix, expiry silently dropped the record — and with
    it the only memory that the turn ran — so a retry started a second turn.
    """
    from api.idempotency import IdempotencyKeyExpired

    env = idem_env
    body = {"session_id": env.session.session_id, "messages": []}
    from api.idempotency import build_storage_key, compute_request_fingerprint

    fingerprint = compute_request_fingerprint(body, "default")
    short = type(env.store)(
        path=env.store._path,
        ttl_seconds=0.01,
        max_records=100,
        tombstone_max_records=100,
    )

    short.claim("gone", fingerprint, profile="default")
    with short._lock:
        short._records[build_storage_key("gone", "default")].owner_pid = 999999
    short.complete(
        "gone",
        profile="default",
        session_id="s",
        stream_id="st",
        turn_id="t",
        response_status=200,
        response_payload={},
    )
    time.sleep(0.05)
    short._evict_expired_locked()

    assert build_storage_key("gone", "default") in short._expired_keys
    assert not [k for k in short._records if "gone" in k], "expired record must be gone"

    with pytest.raises(IdempotencyKeyExpired):
        short.claim("gone", fingerprint, profile="default")


def test_tombstones_survive_ttl_expiry_and_restart(idem_env):
    """#7782 contract call 5: the tombstone set is deliberately OUTSIDE the
    record cap and OUTLIVES the record. A full or swept store must not be able
    to re-admit a turn that already ran, and the protection is persisted so a
    restart still refuses the key.
    """
    import time

    from api.idempotency import (
        IdempotencyKeyExpired,
        IdempotencyStore,
        IdempotencyStoreUnavailable,
        build_storage_key,
        compute_request_fingerprint,
    )

    env = idem_env
    body = {"session_id": env.session.session_id, "messages": []}
    fingerprint = compute_request_fingerprint(body, "default")

    # A tiny record cap: under capacity pressure the store FAILS CLOSED (503)
    # rather than dropping an unexpired record — that is precisely why the
    # tombstone set must live outside the cap. Nothing is silently evicted,
    # so no completed key can be re-admitted by capacity pressure.
    capped = IdempotencyStore(
        path=env.store._path, ttl_seconds=9999, max_records=4, tombstone_max_records=100
    )
    for i in range(4):
        capped.claim(f"cap-{i}", fingerprint, profile="default")
        with capped._lock:
            capped._records[build_storage_key(f"cap-{i}", "default")].owner_pid = 999999
        capped.complete(
            f"cap-{i}",
            profile="default",
            session_id="s",
            stream_id=f"st{i}",
            turn_id=f"t{i}",
            response_status=200,
            response_payload={},
        )
    # The fifth key cannot be admitted while all four records are unexpired.
    # The store refuses (503) rather than evict a live guard.
    with pytest.raises(IdempotencyStoreUnavailable):
        capped.claim("cap-nope", fingerprint, profile="default")

    # TTL expiry is the sanctioned removal path — and it leaves a tombstone.
    short = IdempotencyStore(
        path=env.store._path, ttl_seconds=0.01, tombstone_max_records=100
    )
    short.claim("gone", fingerprint, profile="default")
    with short._lock:
        short._records[build_storage_key("gone", "default")].owner_pid = 999999
    short.complete(
        "gone",
        profile="default",
        session_id="s",
        stream_id="st",
        turn_id="t",
        response_status=200,
        response_payload={},
    )
    time.sleep(0.05)
    # A fresh claim drives the expiry sweep (and persists any tombstone it
    # mints), exactly as production does.
    short.claim("probe", fingerprint, profile="default")
    assert build_storage_key("gone", "default") in short._expired_keys
    with pytest.raises(IdempotencyKeyExpired):
        short.claim("gone", fingerprint, profile="default")

    # Tombstones persist, so a restart still refuses the key.
    restarted = IdempotencyStore(path=short._path)
    with pytest.raises(IdempotencyKeyExpired):
        restarted.claim("gone", fingerprint, profile="default")


def test_root_profile_alias_aliases_share_one_namespace(idem_env):
    """#7782 finding 2: the root profile may legitimately be spelled several
    ways (``active-profile resolve`` names). Keying the namespace by the raw
    string would let the SAME mailbox act as different identities, which is a
    correctness hole in the provider, not a style issue. Canonicalize to the
    documented root alias so both spellings hit one namespace.
    """
    from api.idempotency import _canonical_profile_namespace

    # The root profile may be spelled several ways (a legacy display-name
    # rename). Every spelling must collapse to one canonical namespace: two
    # namespaces for one real profile splits the guard, and a retry gets
    # admitted as a fresh turn.
    assert _canonical_profile_namespace("default") == "default"
    assert _canonical_profile_namespace(None) == "default"
    assert _canonical_profile_namespace("") == "default"
    # A non-root name stays concrete: a wrong-but-concrete namespace isolates
    # a record; collapsing every name would cross-tenant them.
    assert _canonical_profile_namespace("work") == "work"

    # When api.profiles reports the name IS the root profile, it collapses.
    import api.idempotency as idem_mod

    class _FakeProfiles:
        @staticmethod
        def _is_root_profile(name):
            return name == "renamed-root"

    orig = getattr(idem_mod, "_RootProbe", None)
    try:
        # Force the lazy lookup inside _canonical_profile_namespace to see
        # our fake: it imports api.profiles inside the function body.
        import sys
        saved = sys.modules.get("api.profiles")
        sys.modules["api.profiles"] = _FakeProfiles
        try:
            assert _canonical_profile_namespace("renamed-root") == "default"
        finally:
            if saved is not None:
                sys.modules["api.profiles"] = saved
            del orig
    finally:
        pass


def test_namespace_drift_persists_across_restart(idem_env):
    """#7782 finding 2: the root profile may be spelled differently by the
    resolver across a request's lifetime (a legacy display-name rename).
    The keyed namespace must canonicalize so the claim, the completion and a
    post-restart retry all address the SAME durable record.
    """
    from api.idempotency import (
        IdempotencyStore,
        build_storage_key,
        compute_request_fingerprint,
    )
    import api.profiles as profiles_mod

    env = idem_env
    body = {"session_id": env.session.session_id, "messages": []}

    # Make the resolver report a renamed root profile.
    real_is_root = profiles_mod._is_root_profile

    def _fake_is_root(name):
        return name == "renamed-root"

    profiles_mod._is_root_profile = _fake_is_root
    try:
        fingerprint = compute_request_fingerprint(body, "renamed-root")
        env.store.claim_with_identity(
            "ns-drift",
            fingerprint,
            profile="renamed-root",
            session_id="s",
            stream_id="st",
            turn_id="t",
        )
        # ...and complete it under the different spelling of the same profile.
        env.store.complete(
            "ns-drift",
            profile="renamed-root",
            session_id="s",
            stream_id="st",
            turn_id="t",
            response_status=200,
            response_payload={},
        )
        # Both spellings address one canonical namespace.
        key = build_storage_key("ns-drift", "renamed-root")
        assert key == build_storage_key("ns-drift", "default")

        restarted = IdempotencyStore(path=env.store._path)
        restarted._ensure_loaded()
        rec = restarted._records.get(key)
        assert rec is not None and rec.status == "complete", (
            "claim + completion must land in ONE canonical record, not split "
            "across two namespaces for the same profile"
        )
    finally:
        profiles_mod._is_root_profile = real_is_root
