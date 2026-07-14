"""
Regression tests for #4633 / #2476 — streaming meter session lifecycle.

Before this fix, ``_run_agent_streaming`` called ``meter().begin_session(stream_id)``
but never called a paired ``end_session``. A streaming turn that produced zero
output tokens (pre-flight cancel, a setup exception before the first token) left
a ``_SessionMeter`` in ``GlobalMeter._sessions`` forever:

  * ``get_stats()`` only prunes sessions with ``first_token_ts > 0`` — a
    zero-token session has ``first_token_ts == 0.0`` and is therefore NEVER
    reclaimed.
  * The leaked session inflates ``get_stats()['active']`` (``len(_sessions)``),
    which is reported to the browser via the SSE ``metering`` event.

The fix registers ``begin_session`` as the first statement inside the worker's
outer ``try`` and pairs it with ``meter().end_session(stream_id, 0)`` in that
try's outer ``finally`` (the same block that pops ``STREAMS``/``CANCEL_FLAGS``),
so every exit path — normal completion, exception, or pre-flight cancel — tears
the session down. ``end_session`` only pops ``_sessions[stream_id]``; the
metering payload is unchanged.

These tests exercise the ``GlobalMeter`` contract directly, since that is where
the leak invariant lives.
"""
import pytest

from api.metering import GlobalMeter


@pytest.fixture()
def m():
    """A fresh, isolated meter per test (avoids touching the module singleton)."""
    return GlobalMeter()


class TestZeroTokenLeak:

    def test_get_stats_does_not_prune_zero_token_session(self, m):
        """Documents the leak: a session with no token is never pruned by get_stats()."""
        m.begin_session('s1')
        # No record_token() -> first_token_ts stays 0.0.
        m.get_stats()  # pruning pass
        assert 's1' in m._sessions
        assert m.get_stats()['active'] == 1

    def test_end_session_reclaims_zero_token_session(self, m):
        """The fix's mechanism: end_session pops the leaked zero-token session."""
        m.begin_session('s1')
        m.end_session('s1', 0)
        assert 's1' not in m._sessions
        assert m.get_stats()['active'] == 0

    def test_end_session_is_idempotent(self, m):
        """The outer finally may run after the inner path already ended the session."""
        m.begin_session('s1')
        m.end_session('s1', 0)
        m.end_session('s1', 0)  # must not raise
        assert 's1' not in m._sessions

    def test_end_session_on_never_begun_stream_is_safe(self, m):
        """finally runs even if begin_session never executed (early raise)."""
        m.end_session('never', 0)  # must not raise / KeyError
        assert m._sessions == {}


class TestBeginCancelBeforeToken:

    def test_begin_then_cancel_before_token_leaves_no_session(self, m):
        """begin -> cancel-before-first-token -> end_session -> _sessions empty."""
        stream_id = 'cancel_preflight'
        m.begin_session(stream_id)
        assert len(m._sessions) == 1
        # No token recorded (cancelled before the model emitted anything).
        # The worker's outer finally calls end_session(stream_id, 0):
        m.end_session(stream_id, 0)
        assert len(m._sessions) == 0

    def test_normal_turn_still_reclaimed(self, m):
        """A turn that DID produce tokens is also reclaimed by end_session."""
        stream_id = 'normal'
        m.begin_session(stream_id)
        m.record_token(stream_id, 10)
        m.record_token(stream_id, 42)
        assert m._sessions[stream_id].output_tokens == 42
        m.end_session(stream_id, 42)
        assert len(m._sessions) == 0
