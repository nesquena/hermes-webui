"""#8613: follow-ups to the profile-scoped memory fix (#8517 / PR #8585).

1. A session-end commit the Agent refuses at hook time must stay pending. The only
   signal is the Agent's refusal (agent/memory_caller_scope._refuse, which logs a
   WARNING). Detecting it from that log record failed when the deployer's logging
   did not create the record (root/``agent``/the logger above WARNING, the logger
   disabled by ``dictConfig``, ``logging.disable``), so the refused commit counted
   as done. WebUI now wraps ``_refuse`` once and notes the refusal before the
   Agent's function runs, unchanged; no logger level, ``disabled`` flag, filter or
   handler is touched (Codex r1: a temporary logger change leaked suppressed
   records and undid deliberate reconfiguration). An Agent without ``_refuse`` keeps
   the pre-#8613 log capture.
2. Evicting an agent whose session owner cannot be resolved runs nothing: no session
   end and no provider teardown (Codex r1: a provider instance may be shared by
   several agents of the same profile; tearing it down broke the survivor).
3. docs/troubleshooting.md documents the implemented warnings.

The tests marked RED were RED on the r1 head 8c4020c2c.
"""
# ruff: noqa: F811  (pytest fixtures imported from the #8517 modules)
from __future__ import annotations

import ast
import contextvars
import logging
import logging.config
import logging.handlers
import queue
import re
import sys
import threading
import types
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

import api.memory_profile_scope as scope
import api.session_lifecycle as session_lifecycle
import api.streaming as streaming
from tests.test_issue8517_profile_scoped_memory import (  # noqa: F401 (fixtures)
    _key,
    hc,
    homes,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
SCOPE_LOGGER = "agent.memory_caller_scope"


def _caller_home() -> str:
    return str(sys.modules["hermes_constants"].get_hermes_home())


_STAND_IN_CALLER_SCOPE = '''
import logging
logger = logging.getLogger("agent.memory_caller_scope")
CALLER_PROFILE_REFUSAL = "memory_caller_profile_unresolved"


def _refuse(label, reason, caller_home, bound_home, multiplexed):
    logger.warning("Memory %s refused (%s): %s; caller profile home %r, provider bound to %r, multiplexed=%s",
                   label, CALLER_PROFILE_REFUSAL, reason, caller_home, bound_home, multiplexed)
'''


@pytest.fixture(autouse=True)
def caller_scope_mod(monkeypatch):
    """The Agent's ``agent.memory_caller_scope``: the installed one, else (agent-free) a
    stand-in with the same ``_refuse``. Its ``_refuse`` is restored after each test."""
    try:
        import agent.memory_caller_scope as mod  # noqa: PLC0415
    except Exception:
        mod = None
    if mod is None or not callable(getattr(mod, "_refuse", None)):
        mod = types.ModuleType(SCOPE_LOGGER)
        exec(compile(_STAND_IN_CALLER_SCOPE, "<stand-in agent/memory_caller_scope.py>", "exec"), mod.__dict__)
        monkeypatch.setitem(sys.modules, SCOPE_LOGGER, mod)
    monkeypatch.setattr(mod, "_refuse", mod._refuse)
    return mod


def _agent_refuses(hook: str = "on_session_end") -> None:
    """The Agent's hook-time refusal, reached the way its ``caller_scope`` reaches it:
    the module's ``_refuse`` looked up at call time."""
    sys.modules[SCOPE_LOGGER]._refuse(f"hook {hook}", "test refusal", None, None, False)


def _real_memory_manager():
    """The installed Agent's ``agent.memory_manager`` and ``MemoryProvider``; skips
    without an Agent (the ``hc`` fixture may have put a claim-seam twin in its place)."""
    mm_mod = pytest.importorskip("agent.memory_manager")
    if not isinstance(getattr(mm_mod, "MemoryManager", None), type):
        pytest.skip("no installed Hermes Agent MemoryManager")
    provider_mod = pytest.importorskip("agent.memory_provider")
    return mm_mod, provider_mod.MemoryProvider


def _logger_state(lg: logging.Logger) -> tuple:
    return (lg.level, lg.disabled, lg.propagate, list(lg.filters), list(lg.handlers))


@pytest.fixture
def lifecycle(monkeypatch):
    session_lifecycle._reset_for_tests()
    monkeypatch.setattr(session_lifecycle, "_draining", False)
    yield session_lifecycle
    session_lifecycle._reset_for_tests()


@pytest.fixture
def log_config():
    """Restores every logger this module raises or disables, and ``logging.disable``."""
    names = ["", "agent", SCOPE_LOGGER]
    saved = {n: (logging.getLogger(n).level, logging.getLogger(n).disabled) for n in names}
    saved_disable = logging.root.manager.disable
    yield
    logging.disable(saved_disable)
    for n, (level, disabled) in saved.items():
        lg = logging.getLogger(n)
        lg.setLevel(level)
        lg.disabled = disabled
    captures = getattr(scope, "_refusal_captures", None)
    assert captures is None or captures.get() == (), "a refusal capture was left active"


class _Sink(logging.Handler):
    """A deployer's handler: what the deployer's logging configuration emits."""

    def __init__(self):
        super().__init__(logging.NOTSET)
        self.records: list[logging.LogRecord] = []

    def emit(self, record):
        self.records.append(record)


def _raise_levels(how: str) -> None:
    if how == "root_error":
        logging.getLogger().setLevel(logging.ERROR)
    elif how == "agent_critical":
        logging.getLogger("agent").setLevel(logging.CRITICAL)
    elif how == "scope_logger_error":
        logging.getLogger(SCOPE_LOGGER).setLevel(logging.ERROR)
    elif how == "scope_logger_disabled":  # dictConfig(disable_existing_loggers=True)
        logging.getLogger(SCOPE_LOGGER).disabled = True
    elif how == "logging_disable":  # process-wide logging.disable(WARNING) or above
        logging.disable(logging.CRITICAL)
    else:  # pragma: no cover
        raise AssertionError(how)


RAISED = ["root_error", "agent_critical", "scope_logger_error", "scope_logger_disabled", "logging_disable"]


class RefusingManager:
    """MemoryManager lifecycle stand-in for an Agent whose binding WebUI cannot read
    (no ``_bound_scope``): WebUI runs the hook and the Agent refuses it with its
    WARNING, the only signal."""

    def __init__(self, log, refuse, inside=None):
        self._providers = ["sbxmem"]
        self.log = log
        self.refuse = refuse
        self.inside = inside if inside is not None else []

    def on_session_end(self, messages):
        self.inside.append(_logger_state(logging.getLogger(SCOPE_LOGGER)))
        if self.refuse["on"]:
            _agent_refuses("on_session_end")
            return
        self.log.append(("on_session_end", _caller_home()))


class CommitAgent:
    def __init__(self, manager):
        self._memory_manager = manager
        self._session_messages = []
        self._session_db = None

    def commit_memory_session(self, messages=None):
        if self._memory_manager:
            self._memory_manager.on_session_end(messages or [])


def _bound_agent(home, fn_log=None):
    return SimpleNamespace(_memory_manager=SimpleNamespace(
        _bound_scope={"hermes_home": str(home), "home_key": _key(home)}))


# ── 1. refusal accounting does not depend on the deployer's logger levels ──────


@pytest.mark.parametrize("how", RAISED)
def test_refused_commit_stays_pending_whatever_the_logger_levels(hc, homes, lifecycle, log_config, how):
    """Re-gate probe a4: the root logger above WARNING; the refusal was missed and the
    commit counted as done. Same for ``agent``/the scope logger raised or disabled and
    for ``logging.disable``. RED on 8c4020c2c: the logger was changed during the call
    (and ``logging.disable`` still hid the refusal)."""
    log: list = []
    inside: list = []
    refuse = {"on": True}
    sid = f"i8613-raised-{how}"
    agent = CommitAgent(RefusingManager(log, refuse, inside))
    lifecycle.mark_turn_completed(sid, agent=agent, profile_home=str(homes["alpha"]))
    scope_logger = logging.getLogger(SCOPE_LOGGER)
    sink = _Sink()
    root = logging.getLogger()
    root.addHandler(sink)
    try:
        _raise_levels(how)
        before = _logger_state(scope_logger)
        assert lifecycle.commit_session_memory(sid, wait=True) is False
        assert inside == [before], "no logger state may change while the session end runs"
        assert _logger_state(scope_logger) == before
    finally:
        root.removeHandler(sink)
    assert lifecycle.has_uncommitted_work(sid), "a refused commit must stay pending"
    assert log == []
    # The deployer's handlers saw exactly what their configuration emits: nothing.
    assert not [r for r in sink.records if r.name == SCOPE_LOGGER], sink.records

    refuse["on"] = False
    assert lifecycle.commit_session_memory(sid, wait=True) is True
    assert not lifecycle.has_uncommitted_work(sid)
    assert log == [("on_session_end", str(homes["alpha"]))]


def test_refusal_line_still_reaches_the_deployer_at_default_levels(hc, homes, log_config):
    """Guard (GREEN on origin/master, RED on 8c4020c2c which added its filter): with
    WARNING admitted the deployer's handlers receive the refusal once and the logger is
    not touched."""
    logging.getLogger().setLevel(logging.WARNING)
    scope_logger = logging.getLogger(SCOPE_LOGGER)
    before = _logger_state(scope_logger)
    sink = _Sink()
    logging.getLogger().addHandler(sink)
    seen: list = []
    try:
        ok = scope.call_as_session_owner(
            _bound_agent(homes["alpha"]),
            lambda: (seen.append(_logger_state(scope_logger)), _agent_refuses()),
            session_id="i8613-default", owner_home=str(homes["alpha"]),
        )
    finally:
        logging.getLogger().removeHandler(sink)
    assert ok is False
    assert seen == [before], "the logger must not be changed"
    assert len([r for r in sink.records if r.name == SCOPE_LOGGER and "refused" in r.getMessage()]) == 1
    assert _logger_state(scope_logger) == before


def test_real_agent_refusal_under_a_raised_root_level_stays_pending(hc, homes, lifecycle, log_config, monkeypatch):
    """The installed Agent's own MemoryManager refusing ``on_session_end`` (bound to
    beta, called as alpha) on an Agent whose binding WebUI cannot read: its
    ``caller_scope`` reaches the wrapped ``_refuse`` through the module globals."""
    mm_mod, MemoryProvider = _real_memory_manager()
    mcs = sys.modules[SCOPE_LOGGER]
    assert mcs.caller_scope.__globals__ is vars(mcs), "caller_scope must resolve _refuse at call time"

    events: list = []

    class Recorder(MemoryProvider):
        name = "rec8613"

        def is_available(self):
            return True

        def get_tool_schemas(self):
            return []

        def initialize(self, session_id, **kwargs):
            events.append(("initialize", kwargs.get("hermes_home")))

        def on_session_end(self, messages):
            events.append(("on_session_end", _caller_home()))

    manager = mm_mod.MemoryManager()
    manager.add_provider(Recorder())
    manager.initialize_all(session_id="s", hermes_home=str(homes["beta"]))
    monkeypatch.setattr(scope, "_bound_memory_home_key", lambda agent: ("unverifiable", None))
    agent = CommitAgent(manager)
    sid = "i8613-real-refusal"
    lifecycle.mark_turn_completed(sid, agent=agent, profile_home=str(homes["alpha"]))
    logging.getLogger().setLevel(logging.ERROR)
    logging.getLogger(SCOPE_LOGGER).disabled = True
    assert lifecycle.commit_session_memory(sid, wait=True) is False
    assert lifecycle.has_uncommitted_work(sid)
    assert not [e for e in events if e[0] == "on_session_end"]


@pytest.mark.parametrize("refuser", ["a", "b"])
def test_concurrent_sessions_each_capture_only_their_own_refusal(hc, homes, log_config, refuser):
    """Two sessions' commits overlap on two threads with the root above WARNING; only
    one is refused. Each call counts only its own refusal; no logger is touched."""
    logging.getLogger().setLevel(logging.ERROR)
    scope_logger = logging.getLogger(SCOPE_LOGGER)
    before = _logger_state(scope_logger)
    both_open, both_done = threading.Barrier(2, timeout=5), threading.Barrier(2, timeout=5)
    results: dict = {}
    inside: dict = {}

    def fn(name):
        both_open.wait()
        if name == refuser:
            _agent_refuses()
        both_done.wait()  # the other call is still open while this one refuses
        inside[name] = _logger_state(scope_logger)

    def run(name, home):
        try:
            results[name] = scope.call_as_session_owner(
                _bound_agent(home), lambda: fn(name), session_id=f"i8613-{name}", owner_home=str(home))
        except BaseException as exc:  # surfaced below
            results[name] = exc

    threads = [threading.Thread(target=run, args=("a", homes["alpha"])),
               threading.Thread(target=run, args=("b", homes["beta"]))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    other = "b" if refuser == "a" else "a"
    assert results == {refuser: False, other: True}
    assert inside == {"a": before, "b": before}
    assert _logger_state(scope_logger) == before


def test_nested_captures(hc, homes, log_config):
    logging.getLogger("agent").setLevel(logging.CRITICAL)
    scope_logger = logging.getLogger(SCOPE_LOGGER)
    before = _logger_state(scope_logger)
    inner: dict = {}

    def outer_fn():
        inner["ok"] = scope.call_as_session_owner(
            _bound_agent(homes["alpha"]), _agent_refuses, session_id="i8613-inner", owner_home=str(homes["alpha"]))

    outer = scope.call_as_session_owner(
        _bound_agent(homes["alpha"]), outer_fn, session_id="i8613-outer", owner_home=str(homes["alpha"]))
    assert inner == {"ok": False}
    # The inner refusal ran inside the outer call too: the outer is not counted either.
    assert outer is False
    assert _logger_state(scope_logger) == before
    # A later call on the same thread is not affected by the finished ones.
    assert scope.call_as_session_owner(_bound_agent(homes["alpha"]), lambda: None, session_id="i8613-after",
                                       owner_home=str(homes["alpha"])) is True


def test_capture_ends_when_the_hook_raises(hc, homes, log_config):
    logging.getLogger().setLevel(logging.ERROR)
    scope_logger = logging.getLogger(SCOPE_LOGGER)
    before = _logger_state(scope_logger)

    def boom():
        raise RuntimeError("hook failed")

    with pytest.raises(RuntimeError, match="hook failed"):
        scope.call_as_session_owner(_bound_agent(homes["alpha"]), boom, session_id="i8613-raise",
                                    owner_home=str(homes["alpha"]))
    assert _logger_state(scope_logger) == before
    assert scope._refusal_captures.get() == ()
    _agent_refuses()  # a refusal outside any call notes nothing and does not fail


def test_reconfiguration_during_the_call_is_kept(hc, homes, log_config):
    logging.getLogger().setLevel(logging.ERROR)
    scope_logger = logging.getLogger(SCOPE_LOGGER)

    def reconfigure():
        scope_logger.setLevel(logging.INFO)

    scope.call_as_session_owner(_bound_agent(homes["alpha"]), reconfigure, session_id="i8613-reconf",
                                owner_home=str(homes["alpha"]))
    assert scope_logger.level == logging.INFO


def test_other_refusals_do_not_count_against_the_hook(log_config, caller_scope_mod):
    """GREEN guard: a refused tool call (or another hook) during the call is not this hook."""
    ok = scope.call_as_session_owner(
        SimpleNamespace(), lambda: caller_scope_mod._refuse("tool memory", "r", None, None, False),
        session_id="i8613-tool")
    assert ok is True


# Codex r1 probes (wt-8613-evidence/test_gate8613_probes.py), on this module only.


class _ProbeSink(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record)


@contextmanager
def _probe_logging():
    """Root at ERROR, the scope logger NOTSET with its own handler, no propagation."""
    loggers = [logging.getLogger(n) for n in ("", "agent", SCOPE_LOGGER, SCOPE_LOGGER + ".child")]
    saved = [(lg, lg.level, lg.disabled, lg.propagate, lg.filters, lg.handlers) for lg in loggers]
    sink = _ProbeSink()
    old_factory = logging.getLogRecordFactory()
    old_disable = logging.root.manager.disable
    try:
        logging.disable(logging.NOTSET)
        for lg in loggers:
            lg.setLevel(logging.NOTSET)
            lg.disabled = False
            lg.filters = []
            lg.handlers = []
            lg.propagate = True
        loggers[0].setLevel(logging.ERROR)
        lg = logging.getLogger(SCOPE_LOGGER)
        lg.propagate = False
        lg.addHandler(sink)
        yield lg, sink
    finally:
        logging.setLogRecordFactory(old_factory)
        logging.disable(old_disable)
        for lg, level, disabled, propagate, filters, handlers in saved:
            lg.setLevel(level)
            lg.disabled, lg.propagate, lg.filters, lg.handlers = disabled, propagate, filters, handlers


def _invoke(fn):
    return scope.call_as_session_owner(SimpleNamespace(), fn, session_id="i8613-probe")


def test_probe_child_logger_keeps_the_deployers_suppression():
    """RED on 8c4020c2c: lowering the scope logger's level enabled its child."""
    with _probe_logging() as (lg, sink):
        child = logging.getLogger(SCOPE_LOGGER + ".child")
        child.warning("before")
        _invoke(lambda: child.warning("inside"))
        child.warning("after")
        assert [(r.name, r.getMessage()) for r in sink.records] == []


def test_probe_inflight_record_keeps_the_deployers_suppression():
    """RED on 8c4020c2c: a warning admitted on another thread during the window
    reached the handlers after the window's filter was gone."""
    with _probe_logging() as (lg, sink):
        admitted_or_done = threading.Event()
        continue_record = threading.Event()
        factory = logging.getLogRecordFactory()

        def delayed_factory(*args, **kwargs):
            record = factory(*args, **kwargs)
            if record.name == SCOPE_LOGGER and record.msg == "suppressed warning":
                admitted_or_done.set()
                assert continue_record.wait(5)
            return record

        logging.setLogRecordFactory(delayed_factory)

        def log_on_other_thread():
            try:
                lg.warning("suppressed warning")
            finally:
                admitted_or_done.set()

        thread = threading.Thread(target=log_on_other_thread)

        def hook():
            thread.start()
            assert admitted_or_done.wait(5)

        try:
            _invoke(hook)
        finally:
            continue_record.set()
            thread.join(5)
        assert not thread.is_alive()
        assert [(r.levelname, r.getMessage()) for r in sink.records] == []


def test_probe_logger_reconfiguration_applies_immediately():
    """RED on 8c4020c2c: the window kept filtering against the saved level."""
    with _probe_logging() as (lg, sink):
        def hook():
            lg.setLevel(logging.INFO)
            lg.info("newly enabled")

        _invoke(hook)
        assert lg.level == logging.INFO
        assert [r.getMessage() for r in sink.records] == ["newly enabled"]


def test_probe_dictconfig_enable_is_not_undone():
    """RED on 8c4020c2c: closing the window disabled the logger again."""
    with _probe_logging() as (lg, sink):
        lg.disabled = True

        def hook():
            logging.config.dictConfig({"version": 1, "incremental": True, "loggers": {SCOPE_LOGGER: {"level": "INFO"}}})
            assert lg.disabled is False

        _invoke(hook)
        assert lg.disabled is False


@pytest.mark.parametrize("configuration", ["handler_filter", "logger_filter", "queue_handler", "propagate_false"])
def test_probe_refusal_detected_independent_of_handlers(configuration):
    class Drop(logging.Filter):
        def filter(self, record):
            return False

    with _probe_logging() as (lg, sink):
        logging.getLogger().setLevel(logging.WARNING)
        listener = None
        if configuration == "handler_filter":
            sink.addFilter(Drop())
        elif configuration == "logger_filter":
            lg.addFilter(Drop())
        elif configuration == "queue_handler":
            records: queue.Queue = queue.Queue()
            lg.handlers = [logging.handlers.QueueHandler(records)]
            listener = logging.handlers.QueueListener(records, sink)
            listener.start()
        try:
            ok = _invoke(_agent_refuses)
        finally:
            if listener:
                listener.stop()
        assert ok is False
        assert len(sink.records) == (0 if configuration.endswith("filter") else 1)


# The wrapper around the Agent's _refuse: installed once, delegates unchanged.


def test_refusal_wrapper_is_installed_once_and_delegates_unchanged(caller_scope_mod, log_config, monkeypatch):
    mod = caller_scope_mod
    calls: list = []
    sentinel = object()

    class Boom(Exception):
        pass

    boom = Boom("the Agent's refusal path failed")

    def original(*args, **kwargs):
        calls.append((args, kwargs))
        if "boom" in args or kwargs.get("reason") == "boom":
            raise boom
        return sentinel

    monkeypatch.setattr(mod, "_refuse", original)
    barrier = threading.Barrier(8, timeout=5)
    installed: list = []

    def install():
        barrier.wait()
        installed.append(scope._install_refusal_hook())

    threads = [threading.Thread(target=install) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    assert installed == [True] * 8
    wrapper = mod._refuse
    assert wrapper is not original and wrapper.__wrapped__ is original, "exactly one wrapper layer"
    assert wrapper.__name__ == original.__name__
    assert scope._install_refusal_hook() is True and mod._refuse is wrapper

    out: dict = {}

    def hook():
        out["positional"] = mod._refuse("hook on_session_end", "r", "c", "b", True)
        out["keyword"] = mod._refuse(label="hook on_session_end", reason="r", caller_home=None,
                                     bound_home=None, multiplexed=False)
        with pytest.raises(Boom) as raised:
            mod._refuse("hook on_session_end", "boom", None, None, False)
        out["exc"] = raised.value

    assert scope.call_as_session_owner(SimpleNamespace(), hook, session_id="i8613-wrap") is False
    assert mod._refuse is wrapper, "a call must not wrap again"
    assert out == {"positional": sentinel, "keyword": sentinel, "exc": boom}
    assert calls == [
        (("hook on_session_end", "r", "c", "b", True), {}),
        ((), {"label": "hook on_session_end", "reason": "r", "caller_home": None, "bound_home": None,
              "multiplexed": False}),
        (("hook on_session_end", "boom", None, None, False), {}),
    ]
    # Outside any call it delegates the same way.
    assert mod._refuse("hook on_session_end", "r", None, None, False) is sentinel
    assert len(calls) == 4


def test_refusal_wrapper_follows_module_reloads(caller_scope_mod, log_config, monkeypatch):
    mod = caller_scope_mod
    calls: list = []

    def fresh(*args, **kwargs):
        calls.append(args)

    def refused():
        return scope.call_as_session_owner(SimpleNamespace(), _agent_refuses, session_id="i8613-reload")

    assert refused() is False
    # Reloading the Agent's module rebinds _refuse to a new, unwrapped function.
    monkeypatch.setattr(mod, "_refuse", fresh)
    assert refused() is False
    assert mod._refuse.__wrapped__ is fresh and len(calls) == 1
    # Reloading this module creates a new capture registry; the old wrapper stays inert.
    monkeypatch.setattr(scope, "_refusal_captures", contextvars.ContextVar("reloaded", default=()))
    assert refused() is False
    assert len(calls) == 2, "the Agent's function runs exactly once per refusal"
    wrapper = mod._refuse
    assert scope._install_refusal_hook() is True and mod._refuse is wrapper


@pytest.mark.parametrize("agent_module", ["no_refuse", "unimportable"])
def test_agent_without_refuse_falls_back_to_the_pre_8613_log_capture(log_config, monkeypatch, agent_module):
    """An Agent without ``_refuse`` (none has a caller-scope check without it): the
    origin/master log capture, a handler on the scope logger for the call only; no
    level, ``disabled`` flag or filter change, and no error."""
    monkeypatch.setitem(sys.modules, SCOPE_LOGGER,
                        types.ModuleType(SCOPE_LOGGER) if agent_module == "no_refuse" else None)
    assert scope._install_refusal_hook() is False
    scope_logger = logging.getLogger(SCOPE_LOGGER)
    logging.getLogger().setLevel(logging.WARNING)
    before = _logger_state(scope_logger)
    inside: list = []

    def refuse_by_log():
        state = _logger_state(scope_logger)
        inside.append((state[:4], len(state[4]) - len(before[4])))
        scope_logger.warning("Memory %s refused (%s): %s", "hook on_session_end", "memory_caller_profile_unresolved", "x")

    assert scope.call_as_session_owner(SimpleNamespace(), refuse_by_log, session_id="i8613-old") is False
    assert inside == [(before[:4], 1)]
    assert _logger_state(scope_logger) == before
    # As on origin/master, that capture only sees what the deployer's levels admit.
    logging.getLogger().setLevel(logging.ERROR)
    assert scope.call_as_session_owner(SimpleNamespace(), refuse_by_log, session_id="i8613-old-raised") is True
    assert _logger_state(scope_logger) == before


# ── 2. unresolvable-owner eviction runs nothing (no session end, no teardown) ──


def _manager(home, log):
    """MemoryManager stand-in recording every lifecycle call and the caller's home."""
    m = SimpleNamespace(_providers=["sbxmem"])
    m._bound_scope = {"hermes_home": str(home), "home_key": _key(home)}
    m.on_session_end = lambda messages: log.append(("on_session_end", _caller_home()))
    m.shutdown_all = lambda: log.append(("shutdown_all", _caller_home()))
    m.claim_teardown = lambda *, session_end: log.append(("claim_teardown", session_end)) or True
    m.release_soft = lambda: log.append(("release_soft", _caller_home()))
    return m


class EvictedAgent:
    def __init__(self, manager, log):
        self._memory_manager = manager
        self._session_messages = [{"role": "user", "content": "x"}]
        self._session_db = None
        self.log = log

    def commit_memory_session(self, messages=None):
        self.log.append(("commit_memory_session", _caller_home()))

    def shutdown_memory_provider(self, messages=None):
        self.log.append(("shutdown_memory_provider", _caller_home()))


def _evict_from(hc, home, sid, agent):
    """Evict ``agent`` from another profile's turn (``home``'s override installed)."""
    token = hc.set_hermes_home_override(str(home))
    try:
        closed = streaming._close_evicted_agent_at_session_boundary(sid, agent)
        assert _caller_home() == str(home), "the evicting turn's scope must be restored"
    finally:
        hc.reset_hermes_home_override(token)
    return closed


def test_unresolvable_owner_eviction_runs_nothing(hc, homes, lifecycle, caplog):
    """RED on 8c4020c2c (it soft-released the provider). No recorded owner, no session
    record: no commit, no session end and no provider teardown; logged as not run."""
    caplog.set_level(logging.WARNING, logger="api.memory_profile_scope")
    log: list = []
    sid = "i8613-unowned"
    agent = EvictedAgent(_manager(homes["alpha"], log), log)
    assert scope.session_owner_profile_home(sid) == ""
    assert _evict_from(hc, homes["beta"], sid, agent) is True
    assert log == [], log
    assert [r for r in caplog.records if sid in r.getMessage() and "not run" in r.getMessage()]


def test_unowned_eviction_keeps_a_shared_provider_working_for_the_survivor(hc, homes, lifecycle):
    """Codex r1 repro, RED on 8c4020c2c: two installed-Agent MemoryManagers of ONE
    profile share a provider instance (the #8517 guard allows that). Evicting the one
    whose owner is unknown must not shut the provider down under the survivor."""
    mm_mod, MemoryProvider = _real_memory_manager()

    class SharedProvider(MemoryProvider):
        name = "i8613-shared"

        def __init__(self):
            self.alive = True
            self.shutdown_count = 0
            self.session_ends = 0
            self.saves: list = []

        def is_available(self):
            return True

        def get_tool_schemas(self):
            return []

        def initialize(self, session_id, **kwargs):
            self.alive = True

        def on_session_end(self, messages):
            self.session_ends += 1

        def sync_turn(self, user_content, assistant_content, **kwargs):
            if not self.alive:
                raise RuntimeError("shared provider client was closed by another manager")
            self.saves.append(user_content)

        def shutdown(self):
            self.shutdown_count += 1
            self.alive = False

    assert scope.install_shared_provider_instance_guard()
    provider = SharedProvider()
    managers = []
    for sid in ("i8613-unowned-shared", "i8613-survivor"):
        manager = mm_mod.MemoryManager()
        manager.add_provider(provider)
        manager.initialize_all(session_id=sid, hermes_home=str(homes["alpha"]))
        assert provider in manager._providers
        managers.append(manager)

    class Agent:
        _session_messages: list = []
        _session_db = None

        def __init__(self, manager):
            self._memory_manager = manager

        def commit_memory_session(self):
            pytest.fail("an unknown owner's session must never be committed")

        def shutdown_memory_provider(self, messages):
            pytest.fail("an unknown owner's shutdown must not run")

    old, survivor = managers
    try:
        assert scope.session_owner_profile_home("i8613-unowned-shared") == ""
        assert _evict_from(hc, homes["beta"], "i8613-unowned-shared", Agent(old))
        token = hc.set_hermes_home_override(str(homes["alpha"]))
        try:
            survivor.sync_all("surviving agent's turn", "reply", session_id="i8613-survivor")
            assert survivor.flush_pending(timeout=5)
        finally:
            hc.reset_hermes_home_override(token)
        assert provider.saves == ["surviving agent's turn"]
        assert provider.shutdown_count == 0 and provider.session_ends == 0
    finally:
        for manager in managers:
            manager.release_soft()


def test_known_owner_eviction_is_unchanged(hc, homes, lifecycle):
    """GREEN guard: the owner is known, the normal shutdown runs as that owner."""
    log: list = []
    sid = "i8613-owned"
    agent = EvictedAgent(_manager(homes["alpha"], log), log)
    lifecycle.record_owner_profile_home(sid, str(homes["alpha"]))
    assert _evict_from(hc, homes["beta"], sid, agent) is True
    assert log == [("shutdown_memory_provider", str(homes["alpha"]))], log


def test_owner_known_but_provider_bound_elsewhere_runs_nothing(hc, homes, lifecycle):
    """GREEN guard: the provider is bound to another profile than the session's."""
    log: list = []
    sid = "i8613-misbound"
    agent = EvictedAgent(_manager(homes["beta"], log), log)
    lifecycle.record_owner_profile_home(sid, str(homes["alpha"]))
    assert _evict_from(hc, homes["beta"], sid, agent) is True
    assert log == [], log


def test_no_unowned_provider_release_fallback():
    """RED on 8c4020c2c. Deferred until WebUI can prove a provider instance is used by
    the evicted agent alone."""
    src = (REPO_ROOT / "api" / "streaming.py").read_text(encoding="utf-8")
    assert "release_unowned_memory_provider" not in src
    assert not hasattr(scope, "release_unowned_memory_provider")


# ── 3. docs: the implemented warnings are documented, verbatim ─────────────────


def _string_constants(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)]


def test_troubleshooting_documents_the_memory_scope_warnings():
    doc = (REPO_ROOT / "docs" / "troubleshooting.md").read_text(encoding="utf-8")
    m = re.search(r"\n## Memory is off.*?(?=\n## )", doc, re.S)
    assert m, "docs/troubleshooting.md has no memory-scope entry"
    entry = m.group(0)
    for needle in ("v0.18.0", "claim_instance", "first profile", "pending", "#8517", "#8613"):
        assert needle in entry, needle
    # Every log fragment the entry quotes is one the code actually logs.
    constants = _string_constants(REPO_ROOT / "api" / "memory_profile_scope.py")
    prose = re.sub(r"```.*?```", "", entry, flags=re.S)
    quoted = [span for span in re.findall(r"`([^`]+)`", prose)
              if re.search(r"refused|older than|shared provider instance|session end|pending|restarts", span)]
    assert len(quoted) >= 5, quoted
    for fragment in quoted:
        assert any(fragment in c for c in constants), f"documented log text not in the code: {fragment!r}"
