"""Cached-agent compressor rebind tidy-up (#9271, follow-up to #9215).

Four points from the issue:

1. A hook that keeps failing on every cached turn logs its traceback once per
   agent, alongside the single WARNING, not on every retry.
2. A turn with no own store to adopt (``new_session_db is None``) keeps the
   documented behaviour: no hook retry, agent still marked as needing a
   rebind, next cached turn with a usable store retries it.
3. The warn-once claim is atomic, so two concurrent failures for the same
   agent cannot both log.
4. ``_add_supported_run_conversation_kwarg`` never stuffs a keyword into a
   positional-only parameter.
"""

import logging
import threading
from types import SimpleNamespace

import pytest

from api import streaming

SESSION_ID = "sid-9271-tidy"


class Handle:
    """Legacy-shaped SessionDB handle: open while ``_conn`` is set."""

    def __init__(self):
        self._conn = object()
        self.close_calls = 0

    def close(self):
        self.close_calls += 1
        self._conn = None


def _closed_handle():
    handle = Handle()
    handle.close()
    return handle


class FailingHook:
    def __init__(self):
        self.calls = []

    def bind_session_state(self, session_db, session_id):
        self.calls.append((session_db, session_id))
        raise RuntimeError("controlled hook failure")


def _agent(existing, hook):
    return SimpleNamespace(
        _session_db=existing,
        session_id=SESSION_ID,
        context_compressor=hook,
        persistence=None,
    )


def _bind_debug_records(caplog):
    return [
        record
        for record in caplog.records
        if record.levelno == logging.DEBUG
        and "bind_session_state" in record.getMessage()
    ]


def _bind_warning_records(caplog):
    return [
        record
        for record in caplog.records
        if record.levelno == logging.WARNING
        and "bind_session_state" in record.getMessage()
    ]


def test_three_failing_turns_log_one_warning_and_one_traceback(caplog):
    caplog.set_level(logging.DEBUG, logger=streaming.logger.name)
    hook = FailingHook()
    agent = _agent(_closed_handle(), hook)

    adopted = Handle()
    assert streaming._adopt_session_db_for_cached_agent(agent, adopted) is adopted
    for _ in range(2):  # the adopted handle stays open; the hook keeps failing
        unused = Handle()
        assert streaming._adopt_session_db_for_cached_agent(agent, unused) is adopted
        assert unused.close_calls == 1

    assert hook.calls == [(adopted, SESSION_ID)] * 3
    warnings = _bind_warning_records(caplog)
    assert len(warnings) == 1
    assert warnings[0].exc_info is None
    debug_records = _bind_debug_records(caplog)
    # One failure is still observable per turn at DEBUG, but only the first
    # one carries the traceback that repeats identically on every retry.
    assert len(debug_records) == 3
    assert sum(1 for record in debug_records if record.exc_info) == 1


def test_flag_claim_has_a_single_winner_under_simulated_concurrency():
    agent = SimpleNamespace()
    barrier = threading.Barrier(8)
    winners = []
    errors = []

    def claim():
        try:
            barrier.wait(timeout=5)
            if streaming._claim_agent_flag(agent, "_one_shot_flag"):
                winners.append(threading.get_ident())
        except BaseException as exc:  # surfaced below, never swallowed
            errors.append(exc)

    threads = [threading.Thread(target=claim) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert errors == []
    assert getattr(agent, "_one_shot_flag") is True
    assert len(winners) == 1


def test_concurrent_failing_rebinds_log_one_warning(caplog):
    caplog.set_level(logging.DEBUG, logger=streaming.logger.name)
    gate = threading.Barrier(2)

    class GatedFailingHook:
        def __init__(self):
            self.calls = 0

        def bind_session_state(self, session_db, session_id):
            self.calls += 1
            # Both turns are inside the failing hook at the same time, so the
            # warn-once claim races in production shape rather than serially.
            gate.wait(timeout=5)
            raise RuntimeError("controlled hook failure")

    hook = GatedFailingHook()
    handle = Handle()
    agent = _agent(handle, hook)
    errors = []

    def rebind():
        try:
            streaming._rebind_context_engine_session(agent, handle)
        except BaseException as exc:  # surfaced below, never swallowed
            errors.append(exc)

    threads = [threading.Thread(target=rebind) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert errors == []
    assert hook.calls == 2
    assert len(_bind_warning_records(caplog)) == 1
    debug_records = _bind_debug_records(caplog)
    assert len(debug_records) == 2
    assert sum(1 for record in debug_records if record.exc_info) == 1
    assert getattr(agent, streaming._BIND_FAILED_ATTR) is True


def test_missing_store_turn_keeps_failed_marker_without_retrying():
    hook = FailingHook()
    live = Handle()
    agent = _agent(live, hook)
    streaming._set_agent_flag(agent, streaming._BIND_FAILED_ATTR, True)
    kwargs = {"session_db": object()}

    assert streaming._reuse_cached_agent_session_db(agent, None, kwargs) is live

    # Nothing to adopt: the hook is not retried, the live handle is untouched,
    # and the agent stays marked so the next turn with a store does retry.
    assert hook.calls == []
    assert live.close_calls == 0
    assert kwargs["session_db"] is live
    assert getattr(agent, streaming._BIND_FAILED_ATTR) is True

    # The next cached turn does have a usable store: the pending rebind is
    # retried on the still-open handle, as documented.
    unused = Handle()
    assert streaming._adopt_session_db_for_cached_agent(agent, unused) is live
    assert hook.calls == [(live, SESSION_ID)]
    assert unused.close_calls == 1


def _run_of_kind(kind):
    if kind == "positional_only":

        def run_conversation(self, revision=None, /):
            return None

    elif kind == "positional_or_keyword":

        def run_conversation(self, revision=None):
            return None

    elif kind == "keyword_only":

        def run_conversation(self, *, revision=None):
            return None

    else:

        def run_conversation(self, **kwargs):
            return None

    return SimpleNamespace(run_conversation=run_conversation.__get__(SimpleNamespace()))


@pytest.mark.parametrize(
    "kind, accepted",
    [
        ("positional_only", False),
        ("positional_or_keyword", True),
        ("keyword_only", True),
        ("var_keyword", True),
    ],
)
def test_positional_only_parameter_never_receives_the_kwarg(kind, accepted):
    kwargs = {}
    supported = streaming._add_supported_run_conversation_kwarg(
        _run_of_kind(kind).run_conversation, kwargs, "revision", {"session_id": "s"}
    )

    assert supported is accepted
    if accepted:
        assert kwargs == {"revision": {"session_id": "s"}}
    else:
        assert kwargs == {}
