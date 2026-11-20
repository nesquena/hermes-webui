"""Cached-agent SessionDB adoption calls the compressor rebind hook the way the
Agent's own producers do (#9150): first two arguments positional, ``persistence``
only when the hook accepts it, and a failing hook never aborts the cached turn.
"""

import functools
import inspect
import logging
from types import SimpleNamespace

import pytest

from api import streaming

try:
    from hermes_state import SessionDB
except Exception:  # pragma: no cover - depends on the selected Agent tree
    SessionDB = None

needs_session_db = pytest.mark.skipif(
    SessionDB is None,
    reason="hermes_state.SessionDB is not importable from the selected Agent tree",
)

SESSION_ID = "sid-9150-hook"


class LegacyHandle:
    """Legacy-shaped handle: open while ``_conn`` is set."""

    def __init__(self):
        self._conn = object()
        self.close_calls = 0

    def close(self):
        self.close_calls += 1
        self._conn = None


class ShimHandle:
    """Newer shim-shaped handle: owns a store in ``_store`` until close."""

    def __init__(self):
        self._closed = False
        self._store = object()
        self.close_calls = 0

    def close(self):
        self.close_calls += 1
        self._closed = True
        self._store = None


class _Recorder:
    def __init__(self):
        self.calls = []


class KeywordHook(_Recorder):
    def bind_session_state(self, session_db, session_id):
        self.calls.append((session_db, session_id, {}))


class PersistenceHook(_Recorder):
    def bind_session_state(self, session_db=None, session_id="", *, persistence=None):
        self.calls.append((session_db, session_id, {"persistence": persistence}))


class PositionalOnlyHook(_Recorder):
    def bind_session_state(self, session_db, session_id, /):
        self.calls.append((session_db, session_id, {}))


class PositionalOnlyPersistenceHook(_Recorder):
    def bind_session_state(self, session_db, session_id, /, persistence=None):
        self.calls.append((session_db, session_id, {"persistence": persistence}))


class PositionalOnlyPersistenceParamHook(_Recorder):
    def bind_session_state(self, session_db, session_id, persistence=None, /):
        self.calls.append((session_db, session_id, {"positional_persistence": persistence}))


class _Uninspectable:
    __signature__ = "not a signature"  # inspect.signature() raises TypeError

    def __call__(self, *args, **kwargs):
        pass


def _keyword_contract_cases():
    def positional_only(persistence=None, /):
        pass

    def positional_or_keyword(persistence=None):
        pass

    def keyword_only(*, persistence=None):
        pass

    def var_keyword(**kwargs):
        pass

    def other_name(session_db=None):
        pass

    return [
        (positional_only, False),
        (positional_or_keyword, True),
        (keyword_only, True),
        (var_keyword, True),
        (other_name, False),
        (_Uninspectable(), False),
    ]


class RaisingHook(_Recorder):
    def __init__(self):
        super().__init__()
        self.fail = True

    def bind_session_state(self, session_db, session_id):
        self.calls.append((session_db, session_id, {}))
        if self.fail:
            raise RuntimeError("controlled hook failure")


class KeywordOnlyHook(_Recorder):
    """Refuses the positional call the Agent's reset path makes."""

    def bind_session_state(self, *, session_db=None, session_id="", persistence=None):
        self.calls.append((session_db, session_id, {"persistence": persistence}))


class TypeErrorHook(_Recorder):
    def bind_session_state(self, session_db=None, session_id=""):
        self.calls.append((session_db, session_id, {}))
        raise TypeError("controlled hook type error")


class BaseExceptionHook(_Recorder):
    def __init__(self, exc_type):
        super().__init__()
        self.exc_type = exc_type
        self.armed = False

    def bind_session_state(self, session_db, session_id):
        self.calls.append((session_db, session_id, {}))
        if self.armed:
            raise self.exc_type()
        raise RuntimeError("controlled hook failure")


def _agent(existing, hook, **extra):
    return SimpleNamespace(
        _session_db=existing, session_id=SESSION_ID, context_compressor=hook, **extra
    )


def _closed_legacy():
    handle = LegacyHandle()
    handle.close()
    return handle


@pytest.mark.parametrize("hook_type, forwarded", [
    (PositionalOnlyHook, lambda fresh: {}),
    (PositionalOnlyPersistenceHook, lambda fresh: {"persistence": fresh._store}),
    # A positional-only ``persistence`` cannot take the keyword (#9150 follow-up).
    (PositionalOnlyPersistenceParamHook, lambda fresh: {"positional_persistence": None}),
])
def test_positional_only_hook_receives_new_handle(hook_type, forwarded):
    hook = hook_type()
    old, fresh = ShimHandle(), ShimHandle()
    old.close()
    agent = _agent(old, hook, persistence=None)

    assert streaming._adopt_session_db_for_cached_agent(agent, fresh) is fresh

    assert agent._session_db is fresh
    assert agent.persistence is fresh._store
    assert fresh.close_calls == 0
    assert hook.calls == [(fresh, SESSION_ID, forwarded(fresh))]


@pytest.mark.parametrize("func, expected", _keyword_contract_cases())
def test_supports_kwarg_matches_agent_keyword_contract(func, expected):
    assert streaming._supports_kwarg(func, "persistence") is expected


@pytest.mark.parametrize("func, expected", _keyword_contract_cases())
def test_local_keyword_helper_matches_agent_helper(func, expected):
    assert streaming._local_accepts_keyword_argument(func, "persistence") is expected
    try:
        from agent.context_compressor_summary import accepts_keyword_argument
    except Exception:
        pytest.skip("selected Agent tree has no accepts_keyword_argument helper")
    assert accepts_keyword_argument(func, "persistence") is expected


def test_raising_hook_does_not_abort_adoption(caplog):
    caplog.set_level(logging.DEBUG, logger=streaming.logger.name)
    hook = RaisingHook()
    old, fresh = ShimHandle(), ShimHandle()
    old.close()
    agent = _agent(old, hook, persistence=None)

    assert streaming._adopt_session_db_for_cached_agent(agent, fresh) is fresh

    assert hook.calls == [(fresh, SESSION_ID, {})]
    assert agent._session_db is fresh
    assert agent.persistence is fresh._store
    assert streaming._session_db_is_open(fresh)
    assert fresh.close_calls == 0
    assert any(
        "bind_session_state" in record.getMessage() and record.exc_info
        for record in caplog.records
        if record.levelno == logging.DEBUG
    )


def test_keyword_hook_still_receives_persistence():
    hook = PersistenceHook()
    old, fresh = ShimHandle(), ShimHandle()
    old.close()
    agent = _agent(old, hook, persistence=None)

    streaming._adopt_session_db_for_cached_agent(agent, fresh)

    assert hook.calls == [(fresh, SESSION_ID, {"persistence": fresh._store})]
    assert agent.persistence is fresh._store


def test_keep_live_existing_handle_skips_hook():
    hook = KeywordHook()
    live, unused = LegacyHandle(), LegacyHandle()
    agent = _agent(live, hook)

    assert streaming._adopt_session_db_for_cached_agent(agent, unused) is live

    assert agent._session_db is live
    assert live.close_calls == 0
    assert unused.close_calls == 1
    assert hook.calls == []


def test_later_adoption_recovers_after_hook_failure():
    hook = RaisingHook()
    old, first, second = ShimHandle(), ShimHandle(), ShimHandle()
    old.close()
    agent = _agent(old, hook, persistence=None)

    streaming._adopt_session_db_for_cached_agent(agent, first)
    assert agent._session_db is first and agent.persistence is first._store
    assert streaming._session_db_is_open(first)

    hook.fail = False
    first.close()
    assert streaming._adopt_session_db_for_cached_agent(agent, second) is second

    assert hook.calls[-1] == (second, SESSION_ID, {})
    assert agent._session_db is second and agent.persistence is second._store
    assert streaming._session_db_is_open(second)


def test_hook_without_persistence_parameter_never_receives_it():
    hook = KeywordHook()
    old, fresh = ShimHandle(), ShimHandle()
    old.close()
    agent = _agent(old, hook, persistence=None)

    streaming._adopt_session_db_for_cached_agent(agent, fresh)

    assert hook.calls == [(fresh, SESSION_ID, {})]


@needs_session_db
@pytest.mark.parametrize("kind", ["positional-only", "raising", "keep-live"])
def test_real_session_db_adoption_hook_boundary(kind, tmp_path):
    for name in ("old", "fresh"):
        (tmp_path / name).mkdir()
    old = SessionDB(db_path=tmp_path / "old" / "state.db")
    fresh = SessionDB(db_path=tmp_path / "fresh" / "state.db")
    hook = {"positional-only": PositionalOnlyHook, "raising": RaisingHook}.get(kind, KeywordHook)()
    agent = _agent(old, hook)
    try:
        if kind != "keep-live":
            old.close()
        assert streaming._session_db_is_open(old) == (kind == "keep-live")

        result = streaming._adopt_session_db_for_cached_agent(agent, fresh)

        if kind == "keep-live":
            assert result is old and agent._session_db is old
            assert not streaming._session_db_is_open(fresh)
            assert hook.calls == []
        else:
            assert result is fresh and agent._session_db is fresh
            assert streaming._session_db_is_open(fresh)
            assert [call[:2] for call in hook.calls] == [(fresh, SESSION_ID)]
    finally:
        old.close()
        fresh.close()


def _adopt_after_closed(hook):
    old, fresh = ShimHandle(), ShimHandle()
    old.close()
    agent = _agent(old, hook, persistence=None)
    return agent, fresh


def test_hook_refusing_the_positional_call_fails_like_any_other_hook():
    # No retry in another calling convention (it could run a hook twice when a
    # TypeError follows a state change): the refusal is a failed bind, logged and
    # retried on the next cached turn, and the turn continues on the new handle.
    hook = KeywordOnlyHook()
    agent, fresh = _adopt_after_closed(hook)

    assert streaming._adopt_session_db_for_cached_agent(agent, fresh) is fresh

    assert hook.calls == []
    assert getattr(agent, streaming._BIND_FAILED_ATTR, False) is True
    assert agent._session_db is fresh and streaming._session_db_is_open(fresh)


def test_type_error_inside_the_hook_is_not_retried():
    # The hook accepts the positional call and raises TypeError from its own
    # body, after it may already have changed state: one call, recorded as a
    # failed bind for the next-turn retry, never a second call in this rebind.
    hook = TypeErrorHook()
    agent, fresh = _adopt_after_closed(hook)

    assert streaming._adopt_session_db_for_cached_agent(agent, fresh) is fresh

    assert hook.calls == [(fresh, SESSION_ID, {})]
    assert getattr(agent, streaming._BIND_FAILED_ATTR, False) is True
    assert agent._session_db is fresh and streaming._session_db_is_open(fresh)


def test_side_effect_before_an_internal_type_error_happens_once():
    effects = []

    class SideEffectHook(_Recorder):
        def bind_session_state(self, session_db, session_id):
            effects.append(session_id)
            return None + 1  # TypeError from inside the hook body

    agent, fresh = _adopt_after_closed(SideEffectHook())

    assert streaming._adopt_session_db_for_cached_agent(agent, fresh) is fresh
    assert effects == [SESSION_ID]


@pytest.mark.parametrize(
    "hook_cls,calls",
    [(KeywordOnlyHook, 0), (TypeErrorHook, 1)],
    ids=["refused-call", "error-inside-hook"],
)
def test_no_type_error_is_retried_in_the_same_rebind(hook_cls, calls):
    hook = hook_cls()
    agent, fresh = _adopt_after_closed(hook)

    assert streaming._adopt_session_db_for_cached_agent(agent, fresh) is fresh

    assert len(hook.calls) == calls
    assert all(call[:2] == (fresh, SESSION_ID) for call in hook.calls)
    assert getattr(agent, streaming._BIND_FAILED_ATTR, False) is True


def _positional_target(session_db, session_id):
    return (session_db, session_id)


def _keyword_target(*, session_db, session_id):
    return (session_db, session_id)


# Method-shaped targets, as a decorator on an engine method would wrap them: the
# bound hook's advertised signature (``__wrapped__``, minus ``self``) is theirs.
def _positional_method_target(self, session_db, session_id):
    return (session_db, session_id)


def _keyword_method_target(self, *, session_db, session_id):
    return (session_db, session_id)


class KeywordWrapperOverPositionalHook(_Recorder):
    """A decorator whose own signature is keyword-only around a positional function:
    ``__wrapped__`` advertises a positional signature the call itself refuses."""

    @functools.wraps(_positional_method_target)
    def bind_session_state(self, *, session_db, session_id):
        self.calls.append((session_db, session_id, {}))


class ForwardingWrapperOverKeywordHook(_Recorder):
    """A ``*args, **kwargs`` wrapper that changes state, then forwards positionally
    to a keyword-only function, which raises TypeError inside the wrapper body."""

    @functools.wraps(_keyword_method_target)
    def bind_session_state(self, *args, **kwargs):
        self.calls.append(("wrapper ran", None, {}))
        return _keyword_target(*args, **kwargs)


class PositionalOnlyWrapperOverKeywordHook(_Recorder):
    """A positional-only adapter whose ``__wrapped__`` is keyword-only: its
    advertised signature refuses the positional call the adapter needs."""

    @functools.wraps(_keyword_method_target)
    def bind_session_state(self, session_db, session_id, /):
        self.calls.append((session_db, session_id, {}))


class PartialOverPositionalOnlyWrapperHook(PositionalOnlyWrapperOverKeywordHook):
    def __init__(self):
        super().__init__()
        self.bind_session_state = functools.partial(
            PositionalOnlyWrapperOverKeywordHook.bind_session_state.__get__(self)
        )


class KeywordWrapperRaisingHook(_Recorder):
    """Its own signature refuses the positional call; its body changes state and
    then raises TypeError."""

    @functools.wraps(_positional_method_target)
    def bind_session_state(self, *, session_db, session_id):
        self.calls.append((session_db, session_id, {}))
        return None + 1


def test_decorated_hook_shapes_advertise_a_different_signature():
    # Guards the fixtures above: each advertises (via ``__wrapped__``) a
    # signature that disagrees with what its call accepts.
    assert str(inspect.signature(KeywordWrapperOverPositionalHook().bind_session_state)) == "(session_db, session_id)"
    assert str(inspect.signature(PositionalOnlyWrapperOverKeywordHook().bind_session_state)) == "(*, session_db, session_id)"
    assert str(inspect.signature(PartialOverPositionalOnlyWrapperHook().bind_session_state)) == "(*, session_db, session_id)"
    assert str(inspect.signature(ForwardingWrapperOverKeywordHook().bind_session_state)) == "(*, session_db, session_id)"


@pytest.mark.parametrize(
    "hook_cls,calls,bound",
    [
        # Refuses the positional call: never entered, unbound, as on master.
        (KeywordWrapperOverPositionalHook, 0, False),
        # Advertise a keyword-only signature but take the positional call: bind.
        (PositionalOnlyWrapperOverKeywordHook, 1, True),
        (PartialOverPositionalOnlyWrapperHook, 1, True),
        # Forwards positionally to a keyword-only target: fails inside the
        # wrapper after it ran, so it runs once and stays unbound, as on master.
        (ForwardingWrapperOverKeywordHook, 1, False),
        (KeywordWrapperRaisingHook, 0, False),
    ],
    ids=[
        "keyword-wrapper-refuses",
        "positional-only-wrapper-binds",
        "partial-positional-only-wrapper-binds",
        "forwarding-wrapper-runs-once",
        "raising-keyword-wrapper-refuses",
    ],
)
def test_decorated_hooks_get_one_positional_call(hook_cls, calls, bound):
    hook = hook_cls()
    agent, fresh = _adopt_after_closed(hook)

    assert streaming._adopt_session_db_for_cached_agent(agent, fresh) is fresh

    assert len(hook.calls) == calls
    assert (getattr(agent, streaming._BIND_FAILED_ATTR, False) is True) is (not bound)


class EitherConventionHook(_Recorder):
    def bind_session_state(self, *args, **kwargs):
        self.calls.append((args, sorted(kwargs), {}))


def test_hook_accepting_both_conventions_keeps_the_positional_call():
    # Positional first, as before this change and as the Agent's reset path
    # calls it: an existing hook that takes either convention sees no change.
    hook = EitherConventionHook()
    agent, fresh = _adopt_after_closed(hook)

    assert streaming._adopt_session_db_for_cached_agent(agent, fresh) is fresh

    assert len(hook.calls) == 1
    args, keywords, _ = hook.calls[0]
    assert args == (fresh, SESSION_ID)
    assert "session_db" not in keywords and "session_id" not in keywords


class _UnreadableSignatureHook:
    """A callable whose signature metadata raises when read."""

    def __init__(self):
        self.calls = []

    @property
    def __signature__(self):
        raise RuntimeError("unreadable signature metadata")

    def __call__(self, session_db, session_id):
        self.calls.append((session_db, session_id))


def test_hook_whose_signature_metadata_raises_still_binds():
    bind = _UnreadableSignatureHook()
    agent, fresh = _adopt_after_closed(SimpleNamespace(bind_session_state=bind))

    assert streaming._adopt_session_db_for_cached_agent(agent, fresh) is fresh

    # The persistence check can't read the signature, so persistence isn't offered.
    assert bind.calls == [(fresh, SESSION_ID)]
    assert getattr(agent, streaming._BIND_FAILED_ATTR, False) is not True


@pytest.mark.parametrize("exc_type", [KeyboardInterrupt, SystemExit])
def test_base_exceptions_from_hook_propagate(exc_type):
    # Only Exception is best-effort; an interrupt or exit must not be swallowed
    # (nor retried) on adoption or on the later-turn rebind retry.
    hook = BaseExceptionHook(exc_type)
    hook.armed = True
    agent, fresh = _adopt_after_closed(hook)
    with pytest.raises(exc_type):
        streaming._adopt_session_db_for_cached_agent(agent, fresh)
    assert hook.calls == [(fresh, SESSION_ID, {})]


@pytest.mark.parametrize("exc_type", [KeyboardInterrupt, SystemExit])
def test_base_exceptions_from_rebind_retry_propagate(exc_type):
    hook = BaseExceptionHook(exc_type)
    agent, fresh = _adopt_after_closed(hook)
    assert streaming._adopt_session_db_for_cached_agent(agent, fresh) is fresh
    hook.armed = True
    unused = ShimHandle()
    with pytest.raises(exc_type):
        streaming._adopt_session_db_for_cached_agent(agent, unused)
    assert hook.calls == [(fresh, SESSION_ID, {})] * 2


def test_failed_rebind_is_retried_on_later_turn_that_keeps_open_handle(caplog):
    caplog.set_level(logging.DEBUG, logger=streaming.logger.name)
    hook = RaisingHook()
    agent, first = _adopt_after_closed(hook)

    assert streaming._adopt_session_db_for_cached_agent(agent, first) is first
    for _ in range(2):  # the adopted handle stays open; the hook keeps failing
        unused = ShimHandle()
        assert streaming._adopt_session_db_for_cached_agent(agent, unused) is first
        assert unused.close_calls == 1
    assert hook.calls == [(first, SESSION_ID, {})] * 3

    hook.fail = False
    assert streaming._adopt_session_db_for_cached_agent(agent, ShimHandle()) is first
    assert hook.calls == [(first, SESSION_ID, {})] * 4
    # Recovered: later turns keep the live handle without calling the hook.
    assert streaming._adopt_session_db_for_cached_agent(agent, ShimHandle()) is first
    assert len(hook.calls) == 4
    assert first.close_calls == 0

    warnings = [
        record for record in caplog.records
        if record.levelno == logging.WARNING and "bind_session_state" in record.getMessage()
    ]
    assert len(warnings) == 1
    assert "controlled hook failure" not in warnings[0].getMessage()
    assert warnings[0].exc_info is None
