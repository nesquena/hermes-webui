"""start_session_turn honors runtime_adapter_enabled() via the shared
_start_run helper — Q-2979-A2 / Copilot discussion_r3305864087/r3305864173.

Before this fix start_session_turn (Option Z drain-thread wakeup entrypoint)
called _start_chat_stream_for_session directly, bypassing the runtime-adapter
selection block that /api/chat/start (_handle_chat_start) already ran. As a
result a process-wakeup turn skipped the adapter that a human-typed turn
would have hit when ``HERMES_RUNTIME_ADAPTER=legacy-journal`` was set.

The refactor factors a shared ``_start_run`` helper used by both entrypoints,
so flipping the env to ``legacy-journal`` now routes process-wakeup turns
through the same LegacyJournalRuntimeAdapter as the browser path.

These tests exercise that contract directly without spinning up a real HTTP
server (precedent: tests/test_wakeup_defer_race.py — monkeypatch the heavy
deps, call the function under test, assert on adapter selection).
"""
from __future__ import annotations

import types

import pytest


@pytest.fixture
def _stub_routes(monkeypatch):
    """Patch the heavy deps inside start_session_turn so the call collapses
    to one observable: did it go through the runtime adapter or not."""
    from api import routes as routes_mod

    # 1. Fake session lookup — returns a minimal object with the attributes
    #    _start_run touches via the s.* attribute names.
    s = types.SimpleNamespace(
        session_id="sess-test",
        model="opus",
        model_provider="anthropic",
        profile="developer-general",
        workspace="/tmp/ws-test",
    )
    monkeypatch.setattr(routes_mod, "get_session", lambda _sid: s)

    # 2. Workspace resolution — short-circuit to the persisted workspace.
    monkeypatch.setattr(
        routes_mod,
        "_resolve_chat_workspace_with_recovery",
        lambda _s, _req: "/tmp/ws-test",
    )

    # 3. Model resolution — pass through.
    monkeypatch.setattr(
        routes_mod,
        "_resolve_compatible_session_model_state",
        lambda model, provider, **kwargs: (model, provider, model),
    )

    # 4. Block the per-session live-view fan-out (it pokes a real registry).
    monkeypatch.setattr(
        routes_mod,
        "_start_chat_stream_for_session",
        lambda *a, **kw: {"_status": 200, "stream_id": "stream-direct", "session_id": "sess-test"},
    )

    # 5. Silence the channel emit.
    import api.background_process as bp_mod

    monkeypatch.setattr(bp_mod, "get_session_channel", lambda _sid: None)

    return routes_mod


def test_start_session_turn_uses_direct_path_by_default(_stub_routes, monkeypatch):
    """With HERMES_WEBUI_RUNTIME_ADAPTER unset (legacy-direct default), the helper
    must NOT go through the adapter — it falls through to the direct
    _start_chat_stream_for_session call, same as before."""
    monkeypatch.delenv("HERMES_WEBUI_RUNTIME_ADAPTER", raising=False)

    calls = {"adapter": 0}
    from api import runtime_adapter as ra_mod

    real_build = ra_mod.build_runtime_adapter

    def _track(*a, **kw):
        calls["adapter"] += 1
        return real_build(*a, **kw)

    monkeypatch.setattr(ra_mod, "build_runtime_adapter", _track)

    resp = _stub_routes.start_session_turn("sess-test", "wakeup msg")
    assert resp["_status"] == 200
    assert calls["adapter"] == 0, "default mode must not build a runtime adapter"


def test_start_session_turn_routes_through_adapter_when_enabled(
    _stub_routes, monkeypatch
):
    """With HERMES_WEBUI_RUNTIME_ADAPTER=legacy-journal, start_session_turn must
    construct + invoke the LegacyJournalRuntimeAdapter — same path
    _handle_chat_start exercises. This is the regression that Q-2979-A2 fixes:
    before the _start_run refactor, this env flip had no effect on the
    process-wakeup path."""
    monkeypatch.setenv("HERMES_WEBUI_RUNTIME_ADAPTER", "legacy-journal")

    from api import runtime_adapter as ra_mod

    invoked = {"adapter": 0, "start_run": 0}

    class _SpyAdapter:
        def start_run(self, request):
            invoked["start_run"] += 1
            assert request.session_id == "sess-test"
            assert request.message == "wakeup msg"
            assert request.source == "process_wakeup"
            assert request.metadata == {"route": "start_session_turn"}
            return ra_mod.RunStartResult(
                run_id="run-test",
                stream_id="stream-via-adapter",
                session_id=request.session_id,
                payload={"_status": 200, "stream_id": "stream-via-adapter"},
            )

    def _fake_build(**kw):
        invoked["adapter"] += 1
        return _SpyAdapter()

    monkeypatch.setattr(ra_mod, "build_runtime_adapter", _fake_build)

    resp = _stub_routes.start_session_turn("sess-test", "wakeup msg")
    assert resp["_status"] == 200
    assert resp["stream_id"] == "stream-via-adapter"
    assert invoked == {"adapter": 1, "start_run": 1}


def test_start_session_turn_direct_normalizes_response_with_status_200(
    _stub_routes, monkeypatch
):
    """Defect A regression: ``_start_run`` documents its return contract as
    ``_status`` + legacy fields, but the default direct code path was
    returning ``_start_chat_stream_for_session`` unchanged, which produces
    a dict with ``stream_id``/``session_id`` but NO ``_status`` key.

    ``api.kanban_notifications._is_dispatch_accepted`` correctly requires
    an integer ``_status`` plus a nonempty ``stream_id`` — so a real
    successful direct wake was being rejected, never advancing its cursor,
    and never firing the agent.

    This test mocks the direct starter to return EXACTLY the production
    ``_start_chat_stream_for_session`` shape (no ``_status``) and asserts
    that ``start_session_turn`` normalizes the response so a successful
    direct wake is contract-compliant. The pre-fix code returned the
    underlying dict unchanged, so the assertion fails with ``KeyError`` /
    ``_status != 200`` before the fix lands.
    """
    monkeypatch.delenv("HERMES_WEBUI_RUNTIME_ADAPTER", raising=False)
    monkeypatch.delenv("HERMES_WEBUI_RUNTIME_ADAPTER_RUNNER", raising=False)

    from api import routes as routes_mod

    # Mirror the EXACT shape that _start_chat_stream_for_session builds
    # in production: a successful start returns stream_id/session_id plus
    # some metadata, but NO _status. That is the documented contract the
    # Kanban watcher depends on (which requires _status to consider the
    # dispatch accepted).
    def _direct_starter(_s, **kwargs):
        return {
            "stream_id": "stream-direct-real",
            "session_id": "sess-test",
            "pending_started_at": 1700000000.0,
            "turn_id": "turn-direct-real",
            "title": "Test session",
        }

    monkeypatch.setattr(routes_mod, "_start_chat_stream_for_session", _direct_starter)

    # Also silence the runtime adapter path so the call goes through
    # _start_chat_stream_for_session directly.
    from api import runtime_adapter as ra_mod

    monkeypatch.setattr(ra_mod, "build_runtime_adapter", lambda **kw: None)

    resp = _stub_routes.start_session_turn("sess-test", "wakeup msg")

    # The normalized contract: _status must be present and equal to 200,
    # stream_id must be preserved, and the legacy fields must round-trip.
    assert resp.get("_status") == 200, (
        "start_session_turn direct-path response must include _status=200; "
        "kanban_notifications._is_dispatch_accepted requires an integer "
        "_status in 100..399 to consider the dispatch accepted. Got: "
        f"{resp!r}"
    )
    assert resp["stream_id"] == "stream-direct-real"
    assert resp["session_id"] == "sess-test"


def test_start_session_turn_does_not_normalize_error_without_stream_id(
    _stub_routes, monkeypatch
):
    """Boundary guard for the defensive normalization: a response that has
    no ``_status`` AND no non-empty ``stream_id`` (e.g. an error-only
    dict from a legacy/custom adapter) must NOT be silently rewritten
    to ``_status=200``. Only successful-direct wakes (stream_id present)
    are normalized — error responses keep their original shape so the
    kanban watcher can correctly reject the dispatch.
    """
    monkeypatch.delenv("HERMES_WEBUI_RUNTIME_ADAPTER", raising=False)
    monkeypatch.delenv("HERMES_WEBUI_RUNTIME_ADAPTER_RUNNER", raising=False)

    from api import routes as routes_mod

    def _direct_starter(_s, **kwargs):
        return {"error": "nope"}

    monkeypatch.setattr(routes_mod, "_start_chat_stream_for_session", _direct_starter)

    # Also silence the runtime adapter path so the call goes through
    # _start_chat_stream_for_session directly.
    from api import runtime_adapter as ra_mod

    monkeypatch.setattr(ra_mod, "build_runtime_adapter", lambda **kw: None)

    resp = _stub_routes.start_session_turn("sess-test", "wakeup msg")

    # Error-only responses (no stream_id) must NOT be normalized to 200:
    # the watcher relies on _status being absent/error to reject the dispatch.
    assert resp.get("_status") != 200, (
        "start_session_turn must not normalize an error-only response "
        "(no stream_id) to _status=200. Got: "
        f"{resp!r}"
    )
    assert "_status" not in resp, (
        "start_session_turn must not synthesize _status for a response with "
        "no stream_id — the watcher rejects dispatches lacking an accepted "
        "_status, so an absent key is the correct signal. Got: "
        f"{resp!r}"
    )
    assert resp == {"error": "nope"}
