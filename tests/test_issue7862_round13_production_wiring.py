"""#7862 re-gate (2026-10-20): production producer -> route admission wiring.

The master merge split continuation PRODUCTION from ADMISSION. The streaming
hook (api/streaming.py) and the Gateway hook (api/gateway_chat.py) register
through ``api.goals.register_pending_goal_continuation`` — which, after the
merge, wrote only master's in-memory ``PENDING_GOAL_CONTINUATION_PROMPTS``
mirror — while the chat-start route admits through
``api.goal_continuation_store``'s durable ``PENDING_GOAL_CONTINUATION_RECORDS``
registry. A real register -> real route composition therefore consumed
nothing: the durable consume answered ``not_matching``, no durable registry
was written, and the continuation turn launched with ``goal_related=False``,
leaving the producer's prompt record untouched.

These tests pin the PRODUCTION connection behaviorally, exactly as the
maintainer replayed it (no provider body or server ran):

- the REAL producer function the streaming/Gateway hooks call,
- the REAL route admission (``_start_chat_stream_for_session`` with a
  captured worker boundary, or the store consume exactly as the route wires
  it), and
- the REAL durable registry file (a cold reload restores the record).

Nothing here calls ``arm_pending_goal_continuation`` directly — directly
arming the durable store in a test would bypass the very connection these
tests exist to pin.
"""

from __future__ import annotations

import time

import pytest

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

PROMPT = "keep refining the release checklist"


@pytest.fixture
def clean_registry():
    """Isolate the registry between cases (round-12 shape)."""
    from api import goal_continuation_store as store
    from api.config import (
        PENDING_GOAL_CONTINUATION,
        PENDING_GOAL_CONTINUATION_PROMPTS,
        PENDING_GOAL_CONTINUATION_RECORDS,
    )

    def _reset():
        with store._LOCK:
            PENDING_GOAL_CONTINUATION.clear()
            PENDING_GOAL_CONTINUATION_PROMPTS.clear()
            PENDING_GOAL_CONTINUATION_RECORDS.clear()
            store._LAST_LOAD_ERROR = None
            store._LAST_WRITE_ERROR = None
            store._RETIRED_LOG.clear()
            store._ROLLBACK_RECEIPTS.clear()
            store._RETIRED_GENERATIONS.clear()
            store._TOMBSTONES.clear()
            store._CONTINUATION_HANDOFF_TOKENS.clear()
            store._ADOPTED_HANDOFF_SESSIONS.clear()
            store._GENERATION = 0
        store._PENDING_GOAL_FILE.unlink(missing_ok=True)

    _reset()
    yield store
    _reset()


def _cold_reset(store):
    """Simulate a fresh process: drop in-memory state, keep the registry file."""
    from api.config import (
        PENDING_GOAL_CONTINUATION,
        PENDING_GOAL_CONTINUATION_RECORDS,
    )

    with store._LOCK:
        PENDING_GOAL_CONTINUATION.clear()
        PENDING_GOAL_CONTINUATION_RECORDS.clear()
        store._ROLLBACK_RECEIPTS.clear()
        store._RETIRED_GENERATIONS.clear()
        store._TOMBSTONES.clear()
        store._CONTINUATION_HANDOFF_TOKENS.clear()
        store._ADOPTED_HANDOFF_SESSIONS.clear()
        store._GENERATION = 0


def _clean_journals(sid):
    from api.models import SESSION_DIR

    for sub in ("_run_journal", "_turn_journal"):
        target = SESSION_DIR / sub / sid
        try:
            for p in target.rglob("*"):
                if p.is_file():
                    p.unlink()
            target.rmdir()
        except FileNotFoundError:
            pass
        except OSError:
            pass


def _make_session(sid, workspace):
    """Create a REAL persisted session (the route serializes it)."""
    from api.models import SESSIONS, Session

    session = Session(
        session_id=sid,
        title="Goal continuation",
        workspace=str(workspace),
        model="test-model",
        model_provider="test-provider",
        messages=[{"role": "user", "content": "earlier", "timestamp": 1}],
    )
    session.save()
    SESSIONS[sid] = session
    return session


class TestProducerRegistrationWritesTheAuthoritativeRecord:
    """``register_pending_goal_continuation`` is the production producer; the
    route admits through the durable store. One register must land ONE record
    with the SAME token in both the durable registry and master's mirror."""

    def test_register_arms_the_durable_registry_with_the_same_token(
        self, clean_registry
    ):
        store = clean_registry
        from api import goals
        from api.config import (
            PENDING_GOAL_CONTINUATION,
            PENDING_GOAL_CONTINUATION_PROMPTS,
            PENDING_GOAL_CONTINUATION_RECORDS,
        )

        sid = "sess-r13-register"
        cont_id = goals.register_pending_goal_continuation(sid, PROMPT)
        assert cont_id and len(cont_id) == 32

        # The durable registry — the store the chat-start route consumes —
        # holds the producer's record.
        assert sid in PENDING_GOAL_CONTINUATION_RECORDS
        durable = PENDING_GOAL_CONTINUATION_RECORDS[sid]
        assert durable.get("continuation_id") == cont_id
        assert durable.get("prompt") == PROMPT
        # ... and it is ON DISK, so a restart restores it.
        records, _tombstones, _handoffs = store._load_file_raw()
        assert records.get(sid, {}).get("continuation_id") == cont_id
        # Master's marker + prompt mirror carry the SAME token (marker /
        # prompt / token atomicity preserved through the merge).
        assert sid in PENDING_GOAL_CONTINUATION
        assert PENDING_GOAL_CONTINUATION_PROMPTS[sid]["continuation_id"] == cont_id

        # The route's admission primitive consumes the producer's record:
        # the turn IS the continuation.
        assert (
            store.consume_pending_goal_continuation(sid, PROMPT, cont_id, "att-1")
            == store.CONSUME_COMMITTED
        )

    def test_register_fails_closed_when_the_durable_snapshot_cannot_commit(
        self, clean_registry, monkeypatch
    ):
        """An ENOSPC on the registry write must not hand out an admission token
        the route can never admit (round-7 durability contract)."""
        import os

        from api import goals
        from api.config import PENDING_GOAL_CONTINUATION_PROMPTS

        sid = "sess-r13-enospc"

        def _boom(src, dst):
            raise OSError("disk full (simulated)")

        monkeypatch.setattr(os, "replace", _boom)
        assert goals.register_pending_goal_continuation(sid, PROMPT) is None
        # The goal_continue SSE event is gated on the return value, so a
        # non-durable registration must not publish an identity.
        assert sid not in PENDING_GOAL_CONTINUATION_PROMPTS

    def test_an_unrelated_message_without_the_token_stays_ordinary(
        self, clean_registry
    ):
        """A genuine user turn (no token) keeps user priority and the intent
        stays pending for the browser's real dispatch."""
        store = clean_registry
        from api import goals

        sid = "sess-r13-unrelated"
        cont_id = goals.register_pending_goal_continuation(sid, PROMPT)
        assert (
            store.consume_pending_goal_continuation(
                sid, "what is the status?", "", "att-1"
            )
            == store.CONSUME_NOT_MATCHING
        )
        records, _tombstones, _handoffs = store._load_file_raw()
        assert records.get(sid, {}).get("continuation_id") == cont_id


class TestRealRegisterToRealRouteAdmission:
    """The maintainer's composition: real register -> real
    ``_start_chat_stream_for_session`` (synthetic state, captured worker
    boundary). The route must launch the continuation turn with
    ``goal_related=True`` and consume the producer's durable record."""

    def test_the_route_launches_the_continuation_turn_goal_related(
        self, clean_registry, monkeypatch, tmp_path
    ):
        from api import goals
        from api.config import PENDING_GOAL_CONTINUATION_RECORDS

        sid = "sess-r13-route"
        cont_id = goals.register_pending_goal_continuation(sid, PROMPT)
        session = _make_session(sid, tmp_path)

        captured = []
        import api.routes as routes

        monkeypatch.setattr(routes, "_agent_runtime_barrier_response", lambda **kw: None)
        monkeypatch.setattr(routes, "webui_gateway_chat_enabled", lambda _cfg: False)

        def _recorder(session_id, msg, model, workspace, stream_id, attachments, **kwargs):
            captured.append(kwargs)

        monkeypatch.setattr(routes, "_run_agent_streaming", _recorder)
        try:
            response = routes._start_chat_stream_for_session(
                session,
                msg=PROMPT,
                workspace=str(tmp_path),
                model="test-model",
                model_provider="test-provider",
                goal_continuation_id=cont_id,
            )
            assert int(response.get("_status", 200) or 200) == 200
            deadline = time.time() + 2.0
            while not captured and time.time() < deadline:
                time.sleep(0.01)
            assert captured, "the worker boundary never ran"
            assert captured[0].get("goal_related") is True, (
                "the real register -> real route composition launched the "
                "continuation with goal_related=False: the producer record was "
                "never consumed (integration blocker 2026-10-20)"
            )
            # The admission consumed the authoritative durable record.
            assert sid not in PENDING_GOAL_CONTINUATION_RECORDS
        finally:
            _clean_journals(sid)

    def test_durable_reload_restores_the_producer_record_and_admits(
        self, clean_registry
    ):
        """A restart between register and the browser's dispatch restores the
        SAME token from disk; the route's admission then consumes it."""
        store = clean_registry
        from api import goals
        from api.config import (
            PENDING_GOAL_CONTINUATION,
            PENDING_GOAL_CONTINUATION_RECORDS,
        )

        sid = "sess-r13-reload"
        cont_id = goals.register_pending_goal_continuation(sid, PROMPT)
        _cold_reset(store)
        assert store.restore_goal_continuations() == 1, (
            "the producer's registration left no durable registry to restore"
        )
        restored = PENDING_GOAL_CONTINUATION_RECORDS[sid]
        assert restored.get("continuation_id") == cont_id
        assert restored.get("prompt") == PROMPT
        assert sid in PENDING_GOAL_CONTINUATION
        # The route admission (the store consume wired into
        # _start_chat_stream_for_session) consumes the restored record.
        assert (
            store.consume_pending_goal_continuation(sid, PROMPT, cont_id, "att-2")
            == store.CONSUME_COMMITTED
        )
