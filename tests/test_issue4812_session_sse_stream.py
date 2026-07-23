from __future__ import annotations

import io
import queue
from types import SimpleNamespace
from urllib.parse import urlparse

from api.run_journal import append_run_event, read_session_run_events


class _FakeHandler:
    def __init__(self):
        self.status = None
        self.headers = {}
        self.wfile = io.BytesIO()
        self.command = "GET"
        self.path = "/"
        self.client_address = ("127.0.0.1", 12345)

    def send_response(self, status):
        self.status = status

    def send_header(self, key, value):
        self.headers[key] = value

    def end_headers(self):
        pass


def _capture(monkeypatch):
    cap = {}

    def _j(_h, obj, *_, **kwargs):
        cap["ok"] = obj
        cap["status"] = kwargs.get("status", 200)
        return True

    def _bad(_h, msg, code=400):
        cap["bad"] = (msg, code)
        return True

    monkeypatch.setattr("api.routes.j", _j)
    monkeypatch.setattr("api.routes.bad", _bad)
    return cap


def _stop_after_first_heartbeat(monkeypatch):
    calls = {"count": 0}

    def _sleep(_seconds):
        calls["count"] += 1
        raise BrokenPipeError("stop after the first heartbeat")

    monkeypatch.setattr("api.routes.time.sleep", _sleep)
    return calls


def test_session_route_and_global_route_stay_separate(monkeypatch):
    import api.routes as routes

    calls = {"global": 0, "session": 0}

    monkeypatch.setattr(routes, "_handle_extension_sidecar_proxy", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(
        routes,
        "_handle_session_events_stream",
        lambda *_args, **_kwargs: calls.__setitem__("global", calls["global"] + 1) or True,
    )
    monkeypatch.setattr(
        routes,
        "_handle_session_sse_stream_for_session",
        lambda *_args, **_kwargs: calls.__setitem__("session", calls["session"] + 1) or True,
    )

    assert routes._session_events_path_session_id("/api/sessions/events") is None
    assert routes._session_events_path_session_id("/api/sessions/session_1/events") == "session_1"
    assert routes._session_events_path_session_id("/api/sessions/session_1/events/extra") is None

    handler = _FakeHandler()
    routes.handle_get(handler, urlparse("/api/sessions/events"))
    routes.handle_get(handler, urlparse("/api/sessions/session_1/events"))

    assert calls == {"global": 1, "session": 1}


def test_session_journal_replays_later_runs_after_cursor(tmp_path):
    append_run_event(
        "session_1",
        "run_a",
        "token",
        {"text": "a1"},
        session_dir=tmp_path,
        seq=1,
        created_at=100.0,
    )
    append_run_event(
        "session_1",
        "run_a",
        "token",
        {"text": "a2"},
        session_dir=tmp_path,
        seq=2,
        created_at=101.0,
    )
    append_run_event(
        "session_1",
        "run_b",
        "token",
        {"text": "b1"},
        session_dir=tmp_path,
        seq=1,
        created_at=200.0,
    )
    append_run_event(
        "session_1",
        "run_b",
        "done",
        {"session": {"session_id": "session_1"}},
        session_dir=tmp_path,
        seq=2,
        created_at=201.0,
    )
    append_run_event(
        "session_2",
        "run_other",
        "token",
        {"text": "other"},
        session_dir=tmp_path,
        seq=1,
        created_at=150.0,
    )

    replay = read_session_run_events("session_1", after_event_id="run_a:1", session_dir=tmp_path)

    assert replay["status"] == "ok"
    assert [event["event_id"] for event in replay["events"]] == ["run_a:2", "run_b:1", "run_b:2"]


def test_session_journal_distinguishes_missing_and_foreign_cursor(tmp_path):
    append_run_event(
        "session_1",
        "run_a",
        "token",
        {"text": "a1"},
        session_dir=tmp_path,
        seq=1,
        created_at=100.0,
    )
    append_run_event(
        "session_2",
        "run_b",
        "token",
        {"text": "b1"},
        session_dir=tmp_path,
        seq=1,
        created_at=200.0,
    )

    missing = read_session_run_events("session_1", after_event_id="run_missing:1", session_dir=tmp_path)
    foreign = read_session_run_events("session_1", after_event_id="run_b:1", session_dir=tmp_path)

    assert missing["status"] == "cursor_run_missing"
    assert foreign["status"] == "cursor_session_mismatch"


def test_session_journal_rejects_hostile_cursor_without_replay(tmp_path, monkeypatch):
    append_run_event(
        "session_1",
        "run_a",
        "token",
        {"text": "a1"},
        session_dir=tmp_path,
        seq=1,
        created_at=100.0,
    )
    monkeypatch.setattr(
        "api.run_journal.find_run_summary",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("invalid cursor should not scan summaries")),
    )

    malformed = read_session_run_events("session_1", after_event_id="bad/run:1", session_dir=tmp_path)
    negative = read_session_run_events("session_1", after_event_id="run_a:-5", session_dir=tmp_path)
    zero = read_session_run_events("session_1", after_event_id="run_a:0", session_dir=tmp_path)

    assert malformed["status"] == "cursor_invalid"
    assert malformed["events"] == []
    assert negative["status"] == "cursor_invalid"
    assert negative["events"] == []
    assert zero["status"] == "cursor_invalid"
    assert zero["events"] == []


def test_session_journal_blank_summary_session_stays_missing(tmp_path, monkeypatch):
    monkeypatch.setattr("api.run_journal.find_run_summary", lambda *_args, **_kwargs: {"session_id": ""})

    replay = read_session_run_events("session_1", after_event_id="run_missing:1", session_dir=tmp_path)

    assert replay["status"] == "cursor_run_missing"


def test_session_journal_rejects_bounds_and_invalid_contiguous_rows(tmp_path):
    append_run_event("session_1", "run_a", "token", {"text": "one"}, session_dir=tmp_path, seq=1)
    append_run_event("session_1", "run_a", "token", {"text": "two"}, session_dir=tmp_path, seq=2)

    rows_limited = read_session_run_events("session_1", after_event_id="run_a:1", session_dir=tmp_path, max_rows=1)
    bytes_limited = read_session_run_events("session_1", after_event_id="run_a:1", session_dir=tmp_path, max_bytes=1)
    assert rows_limited["status"] != "ok" and rows_limited["events"] == []
    assert bytes_limited["status"] != "ok" and bytes_limited["events"] == []

    path = tmp_path / "_run_journal" / "session_1" / "run_a.jsonl"
    path.write_text(path.read_text(encoding="utf-8").replace('"event_id":"run_a:2"', '"event_id":"wrong:2"'), encoding="utf-8")
    invalid = read_session_run_events("session_1", after_event_id="run_a:1", session_dir=tmp_path)
    assert invalid["status"] == "replay_noncontiguous"
    assert invalid["events"] == []


def test_session_journal_rejects_oversized_line_before_decode(tmp_path, monkeypatch):
    path = tmp_path / "_run_journal" / "session_1" / "run_a.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = '{"event_id":"run_a:1","seq":1,"run_id":"run_a","session_id":"session_1","event":"done","type":"done","payload":{"text":"' + ("x" * 5000) + '"}}\n'
    path.write_bytes(payload.encode("utf-8"))
    monkeypatch.setattr("api.run_journal.json.loads", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("oversized line should not decode")))

    limited = read_session_run_events("session_1", after_event_id="run_a:1", session_dir=tmp_path, max_bytes=1024)

    assert limited["status"] == "replay_limit_bytes"
    assert limited["events"] == []


def test_session_journal_accepts_line_at_cap_and_rejects_next_byte(tmp_path):
    path = tmp_path / "_run_journal" / "session_1" / "run_a.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = '{"event_id":"run_a:1","seq":1,"run_id":"run_a","session_id":"session_1","event":"done","type":"done","payload":{"text":"ok"}}\n'
    path.write_bytes(payload.encode("utf-8"))
    cap = len(payload.encode("utf-8"))

    at_cap = read_session_run_events("session_1", after_event_id="run_a:1", session_dir=tmp_path, max_bytes=cap)
    over = read_session_run_events("session_1", after_event_id="run_a:1", session_dir=tmp_path, max_bytes=cap - 1)

    assert at_cap["status"] == "ok"
    assert at_cap["events"] == []
    assert over["status"] == "replay_limit_bytes"
    assert over["events"] == []


def test_session_journal_requires_exact_cursor_and_contiguous_suffix(tmp_path):
    append_run_event("session_1", "run_a", "token", {}, session_dir=tmp_path, seq=1)
    exact_missing = read_session_run_events("session_1", after_event_id="run_a:2", session_dir=tmp_path)
    append_run_event("session_1", "run_a", "token", {}, session_dir=tmp_path, seq=3)

    gap = read_session_run_events("session_1", after_event_id="run_a:1", session_dir=tmp_path)

    assert exact_missing["status"] != "ok"
    assert exact_missing["events"] == []
    assert gap["events"] == []


def test_session_journal_accepts_final_cursor_and_replays_later_runs(tmp_path):
    append_run_event("session_1", "run_a", "token", {}, session_dir=tmp_path, seq=1, created_at=100.0)
    append_run_event("session_1", "run_b", "token", {}, session_dir=tmp_path, seq=1, created_at=200.0)

    replay = read_session_run_events("session_1", after_event_id="run_a:1", session_dir=tmp_path)

    assert replay["status"] == "ok"
    assert [event["event_id"] for event in replay["events"]] == ["run_b:1"]


def test_session_route_prefers_last_event_id_then_query_fallback(monkeypatch):
    import api.routes as routes

    captured = []
    stop = _stop_after_first_heartbeat(monkeypatch)

    monkeypatch.setattr(routes, "_session_id_visible_to_request_profile", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        routes,
        "get_session",
        lambda sid, metadata_only=False: SimpleNamespace(
            session_id=sid,
            compact=lambda **_kwargs: {"session_id": sid, "title": "Session"},
        ),
    )
    monkeypatch.setattr(routes, "_active_run_stream_for_session", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        routes,
        "read_session_run_events",
        lambda _session_id, *, after_event_id=None, session_dir=None: captured.append(after_event_id) or {"status": "ok", "events": []},
    )

    handler = _FakeHandler()
    handler.headers["Last-Event-ID"] = "run_a:2"
    routes._handle_session_sse_stream_for_session(
        handler,
        urlparse("/api/sessions/session_1/events?after_event_id=run_b:1"),
        "session_1",
    )

    handler = _FakeHandler()
    routes._handle_session_sse_stream_for_session(
        handler,
        urlparse("/api/sessions/session_1/events?after_event_id=run_b:1"),
        "session_1",
    )

    assert stop["count"] >= 2
    assert captured == ["run_a:2", "run_b:1"]


def test_session_route_arms_deadline_immediately_after_headers(monkeypatch):
    import api.routes as routes

    calls = []
    stop = _stop_after_first_heartbeat(monkeypatch)
    monkeypatch.setattr(routes, "_session_id_visible_to_request_profile", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(routes, "get_session", lambda sid, metadata_only=False: SimpleNamespace(session_id=sid, compact=lambda **_kwargs: {}))
    monkeypatch.setattr(routes, "_active_run_stream_for_session", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(routes, "end_sse_headers", lambda _handler: calls.append("headers"))
    monkeypatch.setattr(routes, "_sse_set_write_deadline", lambda _handler: calls.append("deadline"))

    routes._handle_session_sse_stream_for_session(_FakeHandler(), urlparse("/api/sessions/session_1/events"), "session_1")

    assert calls[:2] == ["headers", "deadline"]
    assert stop["count"] == 1


def test_session_route_emits_snapshot_without_id_for_missing_cursor_and_keepalive(monkeypatch):
    import api.routes as routes

    stop = _stop_after_first_heartbeat(monkeypatch)
    monkeypatch.setattr(routes, "_session_id_visible_to_request_profile", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        routes,
        "get_session",
        lambda sid, metadata_only=False: SimpleNamespace(
            session_id=sid,
            compact=lambda **_kwargs: {"session_id": sid, "title": "Session"},
        ),
    )
    monkeypatch.setattr(routes, "_active_run_stream_for_session", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        routes,
        "read_session_run_events",
        lambda *_args, **_kwargs: {"status": "cursor_run_missing", "events": []},
    )

    handler = _FakeHandler()
    routes._handle_session_sse_stream_for_session(
        handler,
        urlparse("/api/sessions/session_1/events?after_event_id=run_missing:1"),
        "session_1",
    )

    body = handler.wfile.getvalue().decode("utf-8")
    assert "event: session_snapshot\n" in body
    assert ": keepalive\n\n" in body
    assert "id: " not in body
    assert stop["count"] == 1


def test_session_route_resyncs_when_run_completes_inside_idle_wait(monkeypatch):
    """Idle no-cursor subscriber: a run that starts+finishes inside one keepalive
    tick advances the journal with no live stream to attach. The handler must emit
    a session_snapshot re-sync so the client catches that run instead of silently
    missing it until manual refresh (Fable gate finding on #5677)."""
    import api.routes as routes

    stop = _stop_after_first_heartbeat(monkeypatch)
    monkeypatch.setattr(routes, "_session_id_visible_to_request_profile", lambda *_a, **_k: True)
    monkeypatch.setattr(
        routes,
        "get_session",
        lambda sid, metadata_only=False: SimpleNamespace(
            session_id=sid,
            compact=lambda **_kwargs: {"session_id": sid, "title": "Session"},
        ),
    )
    # No live stream ever attaches (the run completed before we could subscribe).
    monkeypatch.setattr(routes, "_active_run_stream_for_session", lambda *_a, **_k: None)
    # No cursor → no replay; the missing-cursor path returns ok/empty.
    monkeypatch.setattr(routes, "read_session_run_events", lambda *_a, **_k: {"status": "ok", "events": []})
    # Fingerprint advances between the pre-loop baseline and the in-loop check,
    # simulating a run completing during the wait.
    fps = iter([(0, 0.0, 0), (1, 123.0, 42)])
    monkeypatch.setattr(routes, "session_journal_fingerprint", lambda *_a, **_k: next(fps, (1, 123.0, 42)))

    handler = _FakeHandler()
    routes._handle_session_sse_stream_for_session(
        handler,
        urlparse("/api/sessions/session_1/events"),
        "session_1",
    )

    body = handler.wfile.getvalue().decode("utf-8")
    assert "event: session_snapshot\n" in body, "idle journal advance must emit a snapshot re-sync"
    assert ": keepalive\n\n" in body
    assert stop["count"] == 1


def test_session_route_baselines_journal_before_first_attach(monkeypatch):
    """TOCTOU regression: the idle-wait fingerprint baseline must be captured BEFORE
    the first live-attach lookup. If it were captured afterward, a run completing
    during that first attach would fold into the baseline and be silently missed.
    Assert the call ORDER: session_journal_fingerprint fires before the attach loop's
    active-stream lookup, and a subsequent journal advance still emits a snapshot."""
    import api.routes as routes

    stop = _stop_after_first_heartbeat(monkeypatch)
    monkeypatch.setattr(routes, "_session_id_visible_to_request_profile", lambda *_a, **_k: True)
    monkeypatch.setattr(
        routes,
        "get_session",
        lambda sid, metadata_only=False: SimpleNamespace(
            session_id=sid,
            compact=lambda **_kwargs: {"session_id": sid, "title": "Session"},
        ),
    )
    order = []
    fp_calls = {"n": 0}

    def _active(*_a, **_k):
        order.append("attach")
        return None

    def _fp(*_a, **_k):
        fp_calls["n"] += 1
        order.append("fp")
        # First fp call = pre-attach baseline (old); any later call = advanced.
        return (0, 0.0, 0) if fp_calls["n"] == 1 else (1, 123.0, 42)

    monkeypatch.setattr(routes, "_active_run_stream_for_session", _active)
    monkeypatch.setattr(routes, "read_session_run_events", lambda *_a, **_k: {"status": "ok", "events": []})
    monkeypatch.setattr(routes, "session_journal_fingerprint", _fp)

    handler = _FakeHandler()
    routes._handle_session_sse_stream_for_session(
        handler,
        urlparse("/api/sessions/session_1/events"),
        "session_1",
    )

    body = handler.wfile.getvalue().decode("utf-8")
    # The baseline fp must precede the FIRST in-loop attach that follows it. Concretely:
    # the first fp call happens before the attach-lookup that enters the idle wait.
    assert "fp" in order, "the idle path must baseline the journal fingerprint"
    first_fp = order.index("fp")
    # There must be an attach recorded AFTER the baseline (the loop attach) and the
    # subsequent advance must surface as a snapshot re-sync.
    assert "attach" in order[first_fp:], "an attach must follow the baseline in the idle loop"
    assert body.count("event: session_snapshot\n") == 1, "a journal advance after baseline must emit one re-sync"
    assert stop["count"] == 1


def test_session_route_no_resync_when_idle_journal_unchanged(monkeypatch):
    """The idle-wait re-sync must fire ONLY on a genuine journal advance — a quiet
    idle connection (fingerprint unchanged) must NOT spam snapshots every tick."""
    import api.routes as routes

    stop = _stop_after_first_heartbeat(monkeypatch)
    monkeypatch.setattr(routes, "_session_id_visible_to_request_profile", lambda *_a, **_k: True)
    monkeypatch.setattr(
        routes,
        "get_session",
        lambda sid, metadata_only=False: SimpleNamespace(
            session_id=sid,
            compact=lambda **_kwargs: {"session_id": sid, "title": "Session"},
        ),
    )
    monkeypatch.setattr(routes, "_active_run_stream_for_session", lambda *_a, **_k: None)
    monkeypatch.setattr(routes, "read_session_run_events", lambda *_a, **_k: {"status": "ok", "events": []})
    # Fingerprint never changes → no re-sync.
    monkeypatch.setattr(routes, "session_journal_fingerprint", lambda *_a, **_k: (2, 99.0, 77))

    handler = _FakeHandler()
    routes._handle_session_sse_stream_for_session(
        handler,
        urlparse("/api/sessions/session_1/events"),
        "session_1",
    )

    body = handler.wfile.getvalue().decode("utf-8")
    assert "event: session_snapshot\n" not in body, "quiet idle wait must not emit a snapshot"
    assert ": keepalive\n\n" in body
    assert stop["count"] == 1


def test_session_route_blocks_hidden_sessions_before_replay_or_live_attach(monkeypatch):
    import api.routes as routes

    cap = _capture(monkeypatch)
    monkeypatch.setattr(
        routes,
        "get_session",
        lambda sid, metadata_only=False: SimpleNamespace(session_id=sid, profile="other"),
    )
    monkeypatch.setattr(routes, "_get_active_profile_name", lambda: "default")
    monkeypatch.setattr(
        routes,
        "read_session_run_events",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("replay should not run")),
    )
    monkeypatch.setattr(
        routes,
        "_active_run_stream_for_session",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("live attach should not run")),
    )

    handler = _FakeHandler()
    routes.handle_get(handler, urlparse("/api/sessions/hidden_session/events"))

    assert cap["bad"] == ("Session not found", 404)


def test_session_route_live_delivery_skips_replayed_active_run_items(monkeypatch):
    import api.routes as routes

    class _FakeStream:
        def __init__(self):
            self.q = queue.Queue()
            self.q.put_nowait(("token", {"text": "live replayed"}, "run_active:1"))
            self.q.put_nowait(("stream_end", {"status": "done"}, "run_active:2"))
            self.unsubscribed = False

        def subscribe_with_snapshot(self):
            return self.q, {"last_event_id": "run_active:1", "offline_buffered_events": 1}

        def unsubscribe(self, q):
            self.unsubscribed = q is self.q

    stream = _FakeStream()
    monkeypatch.setattr(routes, "_session_id_visible_to_request_profile", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        routes,
        "get_session",
        lambda sid, metadata_only=False: SimpleNamespace(
            session_id=sid,
            compact=lambda **_kwargs: {"session_id": sid, "title": "Session"},
        ),
    )
    monkeypatch.setattr(routes, "_active_run_stream_for_session", lambda *_args, **_kwargs: "run_active")
    monkeypatch.setattr(routes, "STREAMS", {"run_active": stream})
    monkeypatch.setattr(
        routes,
        "read_session_run_events",
        lambda *_args, **_kwargs: {
            "status": "ok",
            "events": [
                {
                    "run_id": "run_prev",
                    "seq": 1,
                    "event": "token",
                    "payload": {"text": "replayed"},
                    "event_id": "run_prev:1",
                },
                {
                    "run_id": "run_active",
                    "seq": 1,
                    "event": "token",
                    "payload": {"text": "replayed current"},
                    "event_id": "run_active:1",
                },
            ],
        },
    )

    handler = _FakeHandler()
    routes._handle_session_sse_stream_for_session(
        handler,
        urlparse("/api/sessions/session_1/events?after_event_id=run_prev:0"),
        "session_1",
    )

    body = handler.wfile.getvalue().decode("utf-8")
    assert "id: run_prev:1\n" in body
    assert body.count("id: run_active:1\n") == 1
    assert "id: run_active:2\n" in body
    assert "event: stream_end\n" in body
    assert stream.unsubscribed is True


def test_session_route_breaks_on_terminal_even_when_reconciliation_replayed_it(monkeypatch):
    """CORE regression (#5677 gate r3): if reconciliation replays the active run's
    TERMINAL event at the subscribe cutoff, the live queue's copy is deduped — but
    the loop must STILL break on it and unsubscribe. Otherwise the handler stays
    blocked on the dead run's queue and misses subsequent session runs."""
    import api.routes as routes

    class _FakeStream:
        def __init__(self):
            self.q = queue.Queue()
            # The live queue re-delivers the terminal event that reconciliation
            # already replayed at/below the cutoff (same event_id) — it must be
            # deduped for output yet still end the loop.
            self.q.put_nowait(("stream_end", {"status": "done"}, "run_active:1"))
            self.unsubscribed = False

        def subscribe_with_snapshot(self):
            return self.q, {"last_event_id": "run_active:1", "offline_buffered_events": 0}

        def unsubscribe(self, q):
            self.unsubscribed = q is self.q

    stream = _FakeStream()
    monkeypatch.setattr(routes, "_session_id_visible_to_request_profile", lambda *_a, **_k: True)
    monkeypatch.setattr(
        routes,
        "get_session",
        lambda sid, metadata_only=False: SimpleNamespace(
            session_id=sid,
            compact=lambda **_kwargs: {"session_id": sid, "title": "Session"},
        ),
    )
    monkeypatch.setattr(routes, "_active_run_stream_for_session", lambda *_a, **_k: "run_active")
    monkeypatch.setattr(routes, "STREAMS", {"run_active": stream})
    # Reconciliation replays the active run's terminal event (run_active:1) — same id
    # as the queued live copy, at the cutoff.
    monkeypatch.setattr(
        routes,
        "read_session_run_events",
        lambda *_a, **_k: {
            "status": "ok",
            "events": [
                {"run_id": "run_active", "seq": 1, "event": "stream_end", "payload": {"status": "done"}, "event_id": "run_active:1"},
            ],
        },
    )

    handler = _FakeHandler()
    # If the fix regresses, the loop never breaks and blocks on subscriber.get();
    # the _stop_after_first_heartbeat safety turns an unexpected wait into a failure.
    _stop_after_first_heartbeat(monkeypatch)
    routes._handle_session_sse_stream_for_session(
        handler,
        urlparse("/api/sessions/session_1/events?after_event_id=run_active:0"),
        "session_1",
    )

    # The handler must have exited cleanly and unsubscribed — not hung on the queue.
    assert stream.unsubscribed is True, "handler must unsubscribe after a terminal, even when deduped"


def test_session_route_unsubscribes_when_replay_disconnects(monkeypatch):
    import api.routes as routes

    class _DisconnectingWfile:
        def write(self, _data):
            raise BrokenPipeError("client closed")

        def flush(self):
            pass

    class _FakeStream:
        def __init__(self):
            self.q = queue.Queue()
            self.unsubscribed = False

        def subscribe_with_snapshot(self):
            return self.q, {"last_event_id": "run_active:1"}

        def unsubscribe(self, q):
            self.unsubscribed = q is self.q

    stream = _FakeStream()
    monkeypatch.setattr(routes, "_session_id_visible_to_request_profile", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        routes,
        "get_session",
        lambda sid, metadata_only=False: SimpleNamespace(
            session_id=sid,
            compact=lambda **_kwargs: {"session_id": sid, "title": "Session"},
        ),
    )
    monkeypatch.setattr(routes, "_active_run_stream_for_session", lambda *_args, **_kwargs: "run_active")
    monkeypatch.setattr(routes, "STREAMS", {"run_active": stream})
    monkeypatch.setattr(
        routes,
        "read_session_run_events",
        lambda *_args, **_kwargs: {
            "status": "ok",
            "events": [
                {
                    "run_id": "run_prev",
                    "seq": 1,
                    "event": "token",
                    "payload": {"text": "replayed"},
                    "event_id": "run_prev:1",
                },
            ],
        },
    )

    handler = _FakeHandler()
    handler.wfile = _DisconnectingWfile()
    assert routes._handle_session_sse_stream_for_session(
        handler,
        urlparse("/api/sessions/session_1/events?after_event_id=run_prev:0"),
        "session_1",
    )
    assert stream.unsubscribed is True


def test_session_route_live_delivery_without_cursor_keeps_buffered_active_items(monkeypatch):
    import api.routes as routes

    class _FakeStream:
        def __init__(self):
            self.q = queue.Queue()
            self.q.put_nowait(("token", {"text": "buffered"}, "run_active:1"))
            self.q.put_nowait(("stream_end", {"status": "done"}, "run_active:2"))

        def subscribe_with_snapshot(self):
            return self.q, {"last_event_id": "run_active:1", "offline_buffered_events": 1}

    stream = _FakeStream()
    monkeypatch.setattr(routes, "_session_id_visible_to_request_profile", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        routes,
        "get_session",
        lambda sid, metadata_only=False: SimpleNamespace(
            session_id=sid,
            compact=lambda **_kwargs: {"session_id": sid, "title": "Session"},
        ),
    )
    monkeypatch.setattr(routes, "_active_run_stream_for_session", lambda *_args, **_kwargs: "run_active")
    monkeypatch.setattr(routes, "STREAMS", {"run_active": stream})
    monkeypatch.setattr(
        routes,
        "read_session_run_events",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("replay should not run without a cursor")),
    )

    handler = _FakeHandler()
    routes._handle_session_sse_stream_for_session(
        handler,
        urlparse("/api/sessions/session_1/events"),
        "session_1",
    )

    body = handler.wfile.getvalue().decode("utf-8")
    assert "id: run_active:1\n" in body
    assert "id: run_active:2\n" in body
    assert "event: stream_end\n" in body


def test_session_route_reconciles_late_attach_cutoff_once(monkeypatch):
    import api.routes as routes

    class _FakeStream:
        def __init__(self):
            self.q = queue.Queue()
            self.q.put_nowait(("token", {"text": "six"}, "run_active:6"))
            self.q.put_nowait(("stream_end", {"status": "done"}, "run_active:7"))
            self.unsubscribed = 0

        def subscribe_with_snapshot(self):
            return self.q, {"last_event_id": "run_active:6"}

        def unsubscribe(self, q):
            self.unsubscribed += q is self.q

    stream = _FakeStream()
    available = {"value": False}
    reads = []
    monkeypatch.setattr(routes, "_session_id_visible_to_request_profile", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(routes, "get_session", lambda sid, metadata_only=False: SimpleNamespace(session_id=sid, compact=lambda **_kwargs: {}))
    monkeypatch.setattr(routes, "_active_run_stream_for_session", lambda *_args, **_kwargs: "run_active" if available["value"] else None)
    monkeypatch.setattr(routes, "STREAMS", {"run_active": stream})
    monkeypatch.setattr(
        routes,
        "read_session_run_events",
        lambda *_args, **_kwargs: reads.append(1) or {"status": "ok", "events": [] if len(reads) == 1 else [{"event": "token", "payload": {"text": "six"}, "event_id": "run_active:6"}]},
    )
    monkeypatch.setattr(routes.time, "sleep", lambda _seconds: available.__setitem__("value", True))

    handler = _FakeHandler()
    routes._handle_session_sse_stream_for_session(handler, urlparse("/api/sessions/session_1/events?after_event_id=run_active:5"), "session_1")

    body = handler.wfile.getvalue().decode("utf-8")
    assert body.count("id: run_active:6\n") == 1
    assert "id: run_active:7\n" in body
    assert stream.unsubscribed == 1


def test_session_route_emits_snapshot_when_reconciliation_fails(monkeypatch):
    import api.routes as routes

    class _FakeStream:
        def __init__(self):
            self.q = queue.Queue()
            self.q.put_nowait(("token", {"text": "buffered"}, "run_active:1"))
            self.q.put_nowait(("stream_end", {"status": "done"}, "run_active:2"))

        def subscribe_with_snapshot(self):
            return self.q, {"last_event_id": "run_active:1"}

    stream = _FakeStream()
    reads = []
    sessions = iter(
        [
            SimpleNamespace(session_id="session_1", compact=lambda **_kwargs: {"session_id": "session_1", "title": "stale"}),
            SimpleNamespace(session_id="session_1", compact=lambda **_kwargs: {"session_id": "session_1", "title": "fresh"}),
        ]
    )
    monkeypatch.setattr(routes, "_session_id_visible_to_request_profile", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        routes,
        "get_session",
        lambda sid, metadata_only=False: next(sessions),
    )
    monkeypatch.setattr(routes, "_active_run_stream_for_session", lambda *_args, **_kwargs: "run_active")
    monkeypatch.setattr(routes, "STREAMS", {"run_active": stream})
    monkeypatch.setattr(
        routes,
        "read_session_run_events",
        lambda *_args, **_kwargs: reads.append(1) or (
            {"status": "ok", "events": [{"event": "token", "payload": {"text": "replayed"}, "event_id": "run_prev:1"}]}
            if len(reads) == 1
            else {"status": "replay_noncontiguous", "events": []}
        ),
    )

    handler = _FakeHandler()
    routes._handle_session_sse_stream_for_session(
        handler,
        urlparse("/api/sessions/session_1/events?after_event_id=run_prev:0"),
        "session_1",
    )

    body = handler.wfile.getvalue().decode("utf-8")
    assert "event: session_snapshot\n" in body
    assert '"title": "fresh"' in body
    assert '"title": "stale"' not in body
    assert "id: run_prev:1\n" not in body
    assert body.count("id: run_active:1\n") == 0
    assert "id: run_active:2\n" in body


def test_session_route_bounds_sent_event_id_deduplication(monkeypatch):
    import api.routes as routes

    class _FakeStream:
        def __init__(self):
            self.q = queue.Queue()
            for seq in (1, 2, 3, 1):
                self.q.put_nowait(("token", {"seq": seq}, f"run_active:{seq}"))
            self.q.put_nowait(("stream_end", {}, "run_active:4"))

        def subscribe_with_snapshot(self):
            return self.q, {}

    stream = _FakeStream()
    monkeypatch.setattr(routes, "_SESSION_SSE_SENT_EVENT_ID_LIMIT", 2)
    monkeypatch.setattr(routes, "_session_id_visible_to_request_profile", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(routes, "get_session", lambda sid, metadata_only=False: SimpleNamespace(session_id=sid, compact=lambda **_kwargs: {}))
    monkeypatch.setattr(routes, "_active_run_stream_for_session", lambda *_args, **_kwargs: "run_active")
    monkeypatch.setattr(routes, "STREAMS", {"run_active": stream})

    handler = _FakeHandler()
    routes._handle_session_sse_stream_for_session(handler, urlparse("/api/sessions/session_1/events"), "session_1")

    body = handler.wfile.getvalue().decode("utf-8")
    assert body.count("id: run_active:1\n") == 2
    assert "id: run_active:4\n" in body
