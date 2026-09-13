"""POST /api/chat/start must not build the provider catalog for a start it will reject.

`_handle_chat_start` resolved the session model — which on the human path
triggers a full live provider-catalog rebuild with network calls — *before*
`_start_run` reached the guard that rejects a session already owning an active
stream. A request destined for `409 session already has an active stream` still
paid the entire catalog cost (~4.0s of budget timeout in production) on the way
to being thrown away.

The fix is ordering, not discovery: the blocking-active-stream decision is a
pure read of session/run state and is hoisted ahead of model resolution. The
authoritative check stays in `_start_chat_stream_for_session` under the
per-session lock, so both entry points behave identically and the TOCTOU window
stays closed.

Coverage:

1. A blocking `active_stream_id` rejects with the same 409 body, without calling
   `get_available_models()` and without entering `_start_run`.
2. The cancel-unwind sibling (sidecar stream id already cleared, but the worker
   still owns the session in ACTIVE_RUNS) rejects the same way.
3. A *stale* `active_stream_id` must NOT be short-circuited — it still falls
   through to the full path so `_clear_stale_stream_state` runs and the turn
   starts (the anti-permanent-409 guarantee of #3822).
4. An idle session still performs full live discovery — no change to resolved
   model/provider for human chat/start.
"""

import queue
import time
from types import SimpleNamespace

import pytest

import api.config as config
import api.routes as routes


@pytest.fixture(autouse=True)
def _clean_stream_registries():
    with config.STREAMS_LOCK:
        config.STREAMS.clear()
    with config.ACTIVE_RUNS_LOCK:
        config.ACTIVE_RUNS.clear()
    yield
    with config.STREAMS_LOCK:
        config.STREAMS.clear()
    with config.ACTIVE_RUNS_LOCK:
        config.ACTIVE_RUNS.clear()


def _session(tmp_path, *, active_stream_id=None):
    return SimpleNamespace(
        session_id="prefilter-session",
        workspace=str(tmp_path),
        model="gpt-5.5",
        model_provider=None,
        profile="default",
        messages=[],
        context_messages=[],
        pending_user_message=None,
        pending_started_at=None,
        active_stream_id=active_stream_id,
        save=lambda **_kwargs: None,
    )


def _install_chat_start_stubs(monkeypatch, session, tmp_path):
    """Wire _handle_chat_start onto in-memory doubles and record the slow paths."""
    calls = {"catalog": 0, "start_run": 0, "start_run_kwargs": None}

    def _catalog(*, prefer_cache=False):
        calls["catalog"] += 1
        return {"groups": [{"provider_id": "openai", "models": [{"id": "gpt-5.5"}]}]}

    def _start_run(s, **kwargs):
        calls["start_run"] += 1
        calls["start_run_kwargs"] = kwargs
        return {"stream_id": "prefilter-new-stream"}

    monkeypatch.setattr(routes, "_get_or_materialize_session", lambda _sid, **_kw: session)
    monkeypatch.setattr(routes, "_resolve_chat_workspace_with_recovery", lambda _s, _w: str(tmp_path))
    monkeypatch.setattr(routes, "_read_profile_model_config", lambda _s, _p: (None, None, None))
    monkeypatch.setattr(routes, "get_available_models", _catalog)
    monkeypatch.setattr(routes, "_start_run", _start_run)
    monkeypatch.setattr(routes, "j", lambda _handler, payload, status=200: (status, payload))
    return calls


def _post_chat_start(session):
    return routes._handle_chat_start(
        None,
        {"session_id": session.session_id, "message": "continue"},
    )


def test_blocking_active_stream_rejects_before_provider_catalog(monkeypatch, tmp_path):
    """A live stream 409s without paying for provider discovery."""
    session = _session(tmp_path, active_stream_id="live-stream")
    calls = _install_chat_start_stubs(monkeypatch, session, tmp_path)
    with config.STREAMS_LOCK:
        config.STREAMS["live-stream"] = queue.Queue()

    status, payload = _post_chat_start(session)

    assert calls["catalog"] == 0, "blocking active stream must skip provider catalog"
    assert calls["start_run"] == 0, "rejected start must not reach _start_run"
    assert status == 409
    assert payload["error"] == "session already has an active stream"
    assert payload["active_stream_id"] == "live-stream"


def test_cancel_unwind_active_run_rejects_before_provider_catalog(monkeypatch, tmp_path):
    """Sidecar id cleared but the worker still owns the session: same early 409."""
    session = _session(tmp_path, active_stream_id=None)
    calls = _install_chat_start_stubs(monkeypatch, session, tmp_path)
    with config.ACTIVE_RUNS_LOCK:
        config.ACTIVE_RUNS["unwinding-stream"] = {
            "stream_id": "unwinding-stream",
            "session_id": session.session_id,
            "started_at": time.time(),
        }

    status, payload = _post_chat_start(session)

    assert calls["catalog"] == 0, "cancel-unwind rejection must skip provider catalog"
    assert calls["start_run"] == 0
    assert status == 409
    assert payload["error"] == "session already has an active stream"
    assert payload["active_stream_id"] == "unwinding-stream"


def test_stale_active_stream_still_falls_through_to_full_start(monkeypatch, tmp_path):
    """A stale id is not a rejection — it must reach the cleanup + start path."""
    session = _session(tmp_path, active_stream_id="stale-stream")
    calls = _install_chat_start_stubs(monkeypatch, session, tmp_path)

    status, payload = _post_chat_start(session)

    assert status == 200
    assert payload == {"stream_id": "prefilter-new-stream"}
    assert calls["start_run"] == 1, "stale stream must still fall through to _start_run"
    assert calls["catalog"] == 1, "stale stream must still resolve the model normally"


def test_idle_start_still_performs_live_discovery(monkeypatch, tmp_path):
    """An accepted start keeps the existing live-discovery behaviour."""
    session = _session(tmp_path, active_stream_id=None)
    calls = _install_chat_start_stubs(monkeypatch, session, tmp_path)

    status, payload = _post_chat_start(session)

    assert status == 200
    assert payload == {"stream_id": "prefilter-new-stream"}
    assert calls["catalog"] == 1, "idle starts must still build the live catalog"
    assert calls["start_run"] == 1
    assert calls["start_run_kwargs"]["model"] == "gpt-5.5"
