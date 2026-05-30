"""Wakeup tests for the renamed ``bg_task_complete`` SSE event.

This file replaces the legacy ``test_process_complete_wakeup.py`` after the
R2 §Q1 / Q4 contract update with the maintainer:

  - Q1: the canonical SSE event is now ``bg_task_complete`` carrying the
        minimal ``{session_id, task_id, completed_at, summary?, event_id}``
        payload (the legacy ``process_complete`` name is dual-emitted under
        PR (a) only as a 1-PR-cycle compatibility shim and is removed in
        PR (b)).
  - Q4: each emit must carry a fresh server-side ``event_id`` so the WebUI
        can build a consumer-side TTL ring buffer for cross-disconnect
        dedupe in a follow-up PR.

The tests below cover the wakeup-emit hot path end to end:

  1. A completion event flowing through ``_process_one`` produces the
     canonical ``bg_task_complete`` SSE emission with the trimmed payload
     and a non-empty ``event_id``.
  2. The same call also emits the legacy ``process_complete`` shim with the
     same payload + same ``event_id``, so a consumer running an old
     listener still wakes exactly once.
  3. When ``_process_one`` runs while no per-session emit-coalesce window
     is pending, the event is emitted immediately (i.e. wakeup is not
     dropped by the throttle gate on a single completion).
  4. The previous file name ``tests/test_process_complete_wakeup.py`` must
     remain absent in the BACKEND-tier slice.
"""
from __future__ import annotations

import os
import queue
import threading
import types


class _FakeProcessRegistry:
    """Minimal stand-in for tools.process_registry.process_registry."""

    def __init__(self):
        self._lock = threading.Lock()
        self._completion_consumed: set[str] = set()
        self.completion_queue: queue.Queue = queue.Queue()
        self._procs: dict[str, types.SimpleNamespace] = {}

    def register(self, process_id: str, session_key: str) -> None:
        self._procs[process_id] = types.SimpleNamespace(session_key=session_key)

    def get(self, process_id: str):
        return self._procs.get(process_id)

    def is_completion_consumed(self, process_id: str) -> bool:
        with self._lock:
            return process_id in self._completion_consumed


def _install_fake_registry(monkeypatch, fake):
    """Inject the fake registry under ``tools.process_registry``.

    Uses ONLY monkeypatch.setitem (no sys.modules.setdefault) so both
    ``tools`` and ``tools.process_registry`` are restored on teardown —
    matches the rebase-isolation rule documented in
    ``test_bg_task_complete_ab_coexistence.py``.
    """
    import sys
    mod = types.ModuleType("tools.process_registry")
    mod.process_registry = fake
    tools_mod = types.ModuleType("tools")
    tools_mod.process_registry = mod  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "tools", tools_mod)
    monkeypatch.setitem(sys.modules, "tools.process_registry", mod)


def _reset_cfg_state():
    from api import config as _cfg
    from api import background_process as bp
    with _cfg.PROCESS_SESSION_INDEX_LOCK:
        _cfg.PROCESS_SESSION_INDEX.clear()
    _cfg.PENDING_BG_TASK_COMPLETIONS.clear()
    _cfg.BG_TASK_COMPLETE_EVENTS_SEEN.clear()
    with _cfg.STREAMS_LOCK:
        _cfg.STREAMS.clear()
    if hasattr(_cfg, "ACTIVE_RUNS"):
        with _cfg.ACTIVE_RUNS_LOCK:
            _cfg.ACTIVE_RUNS.clear()
    if hasattr(bp, "_LAST_EMIT_TS"):
        bp._LAST_EMIT_TS.clear()
    if hasattr(bp, "_PENDING_EMIT_PAYLOADS"):
        bp._PENDING_EMIT_PAYLOADS.clear()
    if hasattr(bp, "_PENDING_EMIT_TIMERS"):
        bp._PENDING_EMIT_TIMERS.clear()


def _capture_emits(monkeypatch):
    """Replace the per-session emit fan-out with a capturing list."""
    from api import background_process as bp

    emits: list[tuple[str, dict]] = []

    def _capture(session_id: str, event: str, data: dict) -> int:
        emits.append((event, data))
        return 1

    monkeypatch.setattr(bp, "_emit_to_session_streams", _capture)
    # Run the coalesce gate in pass-through mode so a single completion
    # exercises the immediate-emit branch (the throttle behaviour itself is
    # covered exhaustively by tests/test_bg_task_complete_throttle.py).
    monkeypatch.setattr(bp, "_EMIT_COALESCE_WINDOW_SECS", 0.0)
    return emits


def test_bg_task_complete_wakeup_emits_canonical_event_with_event_id(monkeypatch):
    """``_process_one`` emits the canonical ``bg_task_complete`` SSE event
    with the R2 §Q1 trimmed payload and a fresh server-side ``event_id``
    (R2 §Q4).
    """
    fake = _FakeProcessRegistry()
    fake.register("task-wakeup-1", "sess-wakeup-1")
    _install_fake_registry(monkeypatch, fake)
    _reset_cfg_state()

    from api import background_process as bp

    bp.register_process_session("sess-wakeup-1", "sess-wakeup-1")
    emits = _capture_emits(monkeypatch)

    evt = {
        "type": "completion",
        "session_id": "task-wakeup-1",
        "session_key": "sess-wakeup-1",
        "command": "sleep 1",
        "exit_code": 0,
        "output": "done",
    }
    bp._process_one(evt)

    names = [e[0] for e in emits]
    assert "bg_task_complete" in names, (
        f"canonical bg_task_complete emit missing: {names}"
    )

    canonical_payloads = [d for ev, d in emits if ev == "bg_task_complete"]
    assert canonical_payloads, "no canonical bg_task_complete payload captured"
    payload = canonical_payloads[0]

    expected_required = {"session_id", "task_id", "completed_at", "event_id"}
    allowed = expected_required | {"summary"}
    assert expected_required <= set(payload), (
        f"missing required keys in bg_task_complete payload: {payload}"
    )
    assert set(payload) <= allowed, (
        f"unexpected keys in trimmed bg_task_complete payload: {payload}"
    )

    # The legacy/dropped keys must NOT survive the T1 trim.
    for dropped in (
        "command",
        "exit_code",
        "type",
        "stdout_preview",
        "wakeup_prompt",
        "emitted_at",
        "process_id",
    ):
        assert dropped not in payload, (
            f"{dropped!r} should be dropped from bg_task_complete payload"
        )

    # Field-rename invariants.
    assert payload["session_id"] == "sess-wakeup-1"
    assert payload["task_id"] == "task-wakeup-1"
    assert isinstance(payload["completed_at"], float)
    # R2 §Q4: ``event_id`` is a non-empty string (uuid4().hex => 32 chars).
    assert isinstance(payload["event_id"], str)
    assert len(payload["event_id"]) >= 8


def test_bg_task_complete_wakeup_dual_emits_legacy_alias_with_shared_event_id(monkeypatch):
    """The dual-emit shim fires the legacy ``process_complete`` event with
    the *same* payload and the *same* ``event_id``. This is what lets a
    consumer that still listens on the old name dedupe against the new
    canonical event during the PR (a) -> PR (b) cutover.
    """
    fake = _FakeProcessRegistry()
    fake.register("task-wakeup-2", "sess-wakeup-2")
    _install_fake_registry(monkeypatch, fake)
    _reset_cfg_state()

    from api import background_process as bp

    bp.register_process_session("sess-wakeup-2", "sess-wakeup-2")
    emits = _capture_emits(monkeypatch)

    evt = {
        "type": "completion",
        "session_id": "task-wakeup-2",
        "session_key": "sess-wakeup-2",
        "command": "echo hi",
        "exit_code": 0,
        "output": "hi",
    }
    bp._process_one(evt)

    names = [e[0] for e in emits]
    assert "bg_task_complete" in names
    assert "process_complete" in names, (
        "dual-emit shim must still fire ``process_complete`` until PR (b) "
        f"removes it; got {names}"
    )

    relevant = [d for ev, d in emits if ev in ("bg_task_complete", "process_complete")]
    assert len({d["event_id"] for d in relevant}) == 1, (
        "dual-emit must share a single event_id so consumers dedupe across "
        "the old/new event names"
    )

    # Old payload keys MUST NOT regress on either name.
    for d in relevant:
        for dropped in (
            "command",
            "exit_code",
            "type",
            "stdout_preview",
            "wakeup_prompt",
            "emitted_at",
            "process_id",
        ):
            assert dropped not in d, (
                f"{dropped!r} present in dual-emit payload: {d}"
            )


class _FakeHandler:
    """Minimal handler stub for exercising ``handle_post`` directly.

    Mirrors the pattern in ``tests/test_issue1909_csp_report_only.py`` —
    captures status + response headers + body without spinning a real HTTP
    server.
    """

    def __init__(self, body: bytes = b"{}", headers: dict | None = None):
        import io as _io
        self.headers = {
            "Content-Length": str(len(body)),
            "Content-Type": "application/json",
            **(headers or {}),
        }
        self.rfile = _io.BytesIO(body)
        self.wfile = _io.BytesIO()
        self.client_address = ("127.0.0.1", 12345)
        self.status: int | None = None
        self.sent_headers: dict[str, str] = {}

    def send_response(self, status: int) -> None:
        self.status = status

    def send_header(self, key: str, value: str) -> None:
        self.sent_headers[key] = value

    def end_headers(self) -> None:
        pass


def test_legacy_process_complete_ack_returns_410_gone_with_x_replaced_by():
    """T1 deprecation alias: the old ``/api/process-complete-ack`` POST path
    must return HTTP 410 Gone and an ``X-Replaced-By`` header pointing at
    ``/api/bg-task-complete-ack`` (V-a-final criterion #6 + D-a-fix item #1).
    """
    import json as _json
    from urllib.parse import urlparse

    import api.routes as routes

    handler = _FakeHandler()
    parsed = urlparse("/api/process-complete-ack")

    result = routes.handle_post(handler, parsed)

    assert result is True, "deprecated ack endpoint must claim the request"
    assert handler.status == 410, (
        f"expected HTTP 410 Gone for deprecated ack path, got {handler.status}"
    )
    assert handler.sent_headers.get("X-Replaced-By") == "/api/bg-task-complete-ack", (
        f"X-Replaced-By header missing or wrong: {handler.sent_headers}"
    )

    body = handler.wfile.getvalue()
    # Body is gzip-wrapped only if Accept-Encoding allowed it; the fake
    # handler does not set Accept-Encoding so the body is plain JSON.
    payload = _json.loads(body.decode("utf-8"))
    assert payload.get("replaced_by") == "/api/bg-task-complete-ack"
    assert "gone" in payload.get("error", "").lower()


def test_old_process_complete_wakeup_test_file_is_absent():
    """The legacy filename ``tests/test_process_complete_wakeup.py`` must
    remain absent on this branch — the rename is part of the BACKEND-tier
    T1 contract and is required by V-a-final criterion #9.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    legacy = os.path.join(here, "test_process_complete_wakeup.py")
    assert not os.path.exists(legacy), (
        f"legacy {legacy!r} must not exist after the T1 rename"
    )
