"""Unit tests for api.agent_cache_governance (memory-pressure + idle-TTL valves).

Run: HERMES_WEBUI_PYTHON=/usr/local/lib/hermes-agent/venv/bin/python3.11 \
     python -m pytest tests/test_agent_cache_governance.py -v
"""
import sys
import threading
import time
from types import SimpleNamespace

sys.path.insert(0, ".")

from api.agent_cache_governance import (
    AgentCacheGovernor,
    _positive_int,
    plan_pressure_evictions,
    resolve_memory_high_mb,
    soft_release_transcript,
)


class _FakeAgent:
    def __init__(self, last_activity_ts=None, flushed=True, turn_active=None):
        self._last_activity_ts = last_activity_ts
        self._session_messages = [{"role": "user", "content": "x" * 1000}]
        self._db_flush_scan_prefix = [{"role": "user", "content": "y" * 1000}]
        # Persistence parity with AIAgent: _last_flushed_db_idx advances to
        # len(_session_messages) only on a fully successful DB write.
        self._last_flushed_db_idx = len(self._session_messages) if flushed else 0
        # Per-entry turn lease: absent (None) by default so injected agents
        # exercise the snapshot fallback; tests that simulate a mid-turn
        # state set it explicitly (streaming.py always initializes it).
        if turn_active is not None:
            self._turn_active = turn_active


def _cache_with(entries):
    """entries: list[(key, agent)] — oldest first (LRU front)."""
    c = {}
    for k, a in entries:
        c[k] = (a, "sig")
    return c


def _governor(cache, **kw):
    # Accept the old ``running_check`` kwarg for legacy tests; translate to the
    # new snapshot-based active-sids contract.
    if "running_check" in kw:
        rc = kw.pop("running_check")

        def _snapshot():
            return {k for k in list(cache) if rc(k)}

        kw["active_sids_fn"] = _snapshot
    return AgentCacheGovernor(cache, threading.Lock(), **kw)


# ── resolve_memory_high_mb ────────────────────────────────────────────────
def test_memory_high_auto_with_total_ram():
    assert resolve_memory_high_mb("auto", total_ram_mb=2000) == 1300
    assert resolve_memory_high_mb("auto", total_ram_mb=512) >= 512  # floor


def test_memory_high_number():
    assert resolve_memory_high_mb("512", total_ram_mb=2000) == 512
    assert resolve_memory_high_mb(400, total_ram_mb=2000) == 400


def test_memory_high_disabled():
    assert resolve_memory_high_mb("0") is None
    assert resolve_memory_high_mb("off") is None
    assert resolve_memory_high_mb("") is None
    assert resolve_memory_high_mb(None) is None
    assert resolve_memory_high_mb("abc") is None


# ── plan_pressure_evictions ───────────────────────────────────────────────
def test_plan_evicts_lru_skips_recent():
    agents = [(f"k{i}", _FakeAgent()) for i in range(10)]
    plan = plan_pressure_evictions(agents, lambda k, a: True, protect_recent=3)
    keys = [k for k, _ in plan]
    assert keys == [f"k{i}" for i in range(7)]  # last 3 protected


def test_plan_never_evicts_midrun():
    agents = [(f"k{i}", _FakeAgent()) for i in range(10)]
    plan = plan_pressure_evictions(
        agents, lambda k, a: k != "k1", protect_recent=0
    )
    keys = [k for k, _ in plan]
    assert "k1" not in keys


def test_plan_max_evictions():
    agents = [(f"k{i}", _FakeAgent()) for i in range(50)]
    plan = plan_pressure_evictions(agents, lambda k, a: True, protect_recent=0)
    assert len(plan) <= 16


# ── soft_release_transcript ───────────────────────────────────────────────
def test_soft_release_drops_transcript_keeps_agent():
    a = _FakeAgent()
    soft_release_transcript(a)
    assert a._session_messages == []
    assert a._db_flush_scan_prefix is None


# ── idle TTL sweep ─────────────────────────────────────────────────────────
def test_idle_sweep_evicts_stale_keeps_fresh():
    now = time.time()
    cache = _cache_with([
        ("stale", _FakeAgent(last_activity_ts=now - 7200)),
        ("fresh", _FakeAgent(last_activity_ts=now - 10)),
    ])
    closed = []

    def close(key, agent):
        closed.append(key)

    g = _governor(cache, idle_ttl_secs=3600, close_agent_fn=close)
    assert g.sweep_idle(now=now) == 1
    assert closed == ["stale"]
    assert "stale" not in cache
    assert "fresh" in cache


def test_idle_sweep_skips_running():
    now = time.time()
    cache = _cache_with([
        ("running", _FakeAgent(last_activity_ts=now - 7200)),
    ])
    g = _governor(
        cache, idle_ttl_secs=3600,
        running_check=lambda k: k == "running",
        close_agent_fn=lambda k, a: None,
    )
    assert g.sweep_idle(now=now) == 0
    assert "running" in cache


def test_idle_sweep_disabled_when_ttl_zero():
    cache = _cache_with([("k", _FakeAgent(last_activity_ts=0))])
    g = _governor(cache, idle_ttl_secs=0, close_agent_fn=lambda k, a: None)
    assert g.sweep_idle(now=time.time()) == 0


# ── memory pressure sweep ──────────────────────────────────────────────────
def test_pressure_sweep_soft_evicts_lru():
    cache = _cache_with([(f"k{i}", _FakeAgent()) for i in range(5)])
    g = _governor(
        cache,
        memory_high_mb=100,
        protect_recent=1,
        running_check=lambda k: False,
    )
    dropped = g.sweep_pressure(rss_mb=500)
    assert dropped == 4  # k0..k3 dropped, k4 protected
    # agents stay in cache (soft) but transcripts are gone
    assert set(cache.keys()) == {"k0", "k1", "k2", "k3", "k4"}
    assert cache["k0"][0]._session_messages == []
    assert cache["k4"][0]._session_messages  # protected: transcript intact


def test_pressure_sweep_noop_below_budget():
    cache = _cache_with([("k", _FakeAgent())])
    g = _governor(cache, memory_high_mb=100, protect_recent=0)
    assert g.sweep_pressure(rss_mb=50) == 0
    assert cache["k"][0]._session_messages


def test_pressure_sweep_skips_running():
    cache = _cache_with([("busy", _FakeAgent())])
    g = _governor(
        cache, memory_high_mb=100, protect_recent=0,
        running_check=lambda k: k == "busy",
    )
    assert g.sweep_pressure(rss_mb=500) == 0
    assert cache["busy"][0]._session_messages


def test_pressure_sweep_disabled_when_budget_none():
    cache = _cache_with([("k", _FakeAgent())])
    g = _governor(cache, memory_high_mb=None, protect_recent=0)
    assert g.sweep_pressure(rss_mb=10 ** 9) == 0


def test_positive_int_parses():
    assert _positive_int("10", 1) == 10
    assert _positive_int("0", 1) == 0  # 0 allowed = disabled
    assert _positive_int("x", 1) == 1
    assert _positive_int(None, 1) == 1


# ── persistence gate (gateway parity) ─────────────────────────────────────
def test_pressure_sweep_skips_unflushed():
    """A transcript that has not fully flushed to the session DB must not be
    dropped — clearing the sole complete in-memory copy loses history."""
    cache = _cache_with([("k", _FakeAgent(flushed=False))])
    g = _governor(cache, memory_high_mb=100, protect_recent=0)
    assert g.sweep_pressure(rss_mb=500) == 0
    assert cache["k"][0]._session_messages  # still there


def test_pressure_sweep_drops_flushed():
    cache = _cache_with([("k", _FakeAgent(flushed=True))])
    g = _governor(cache, memory_high_mb=100, protect_recent=0)
    assert g.sweep_pressure(rss_mb=500) == 1
    assert cache["k"][0]._session_messages == []


def test_pressure_sweep_revalidates_replaced_entry():
    """A plan entry replaced by a new turn between planning and release must
    not be released (the current agent object differs from the planned one)."""
    cache = _cache_with([("k", _FakeAgent(flushed=True))])
    g = _governor(cache, memory_high_mb=100, protect_recent=0)

    original_agent = cache["k"][0]

    class _ReplacingGovernor(g.__class__):
        _replaced = False

        def _active_sids_fn(self):
            # On the second plan (the sweep re-runs inside revalidation? no —
            # replace the entry once between plan and release via hook):
            return set()

    # Simulate replacement: plan_pressure_evictions is pure; we intercept by
    # swapping the cache entry after the snapshot but before release. Easiest
    # deterministic route: wrap plan_pressure_evictions.
    import api.agent_cache_governance as gmod

    calls = {"n": 0}

    def _plan_wrapper(ordered, is_evictable, **kw):
        plan = orig_plan(ordered, is_evictable, **kw)
        calls["n"] += 1
        if calls["n"] == 1:
            # Between planning and release, a new turn replaces the entry.
            cache["k"] = (_FakeAgent(flushed=True), "sig")
        return plan

    orig_plan = gmod.plan_pressure_evictions
    gmod.plan_pressure_evictions = _plan_wrapper
    try:
        dropped = g.sweep_pressure(rss_mb=500)
    finally:
        gmod.plan_pressure_evictions = orig_plan
    # The replaced agent must survive; the ORIGINAL planned one was cleared.
    assert dropped == 0
    assert cache["k"][0] is not original_agent
    assert cache["k"][0]._session_messages  # replacement untouched


def test_pressure_sweep_goes_active_between_plan_and_release():
    """A session that becomes mid-turn after planning must be skipped at
    release time (no transcript dropped while the agent is running).

    This exercises the per-entry lease: the plan-time snapshot is empty (the
    session looked idle), then the turn starts BETWEEN planning and release.
    The release-time revalidation reads the agent's lease (newest state) and
    must skip the soft-release.  The old test flipped the active flag BEFORE
    calling sweep_pressure, so the single snapshot already saw the session
    active and no plan was ever built — it could never fail.
    """
    cache = _cache_with([("k", _FakeAgent(flushed=True))])
    g = _governor(cache, memory_high_mb=100, protect_recent=0)

    import api.agent_cache_governance as gmod

    orig_plan = gmod.plan_pressure_evictions
    calls = {"n": 0}

    def _plan_wrapper(ordered, is_evictable, **kw):
        plan = orig_plan(ordered, is_evictable, **kw)
        calls["n"] += 1
        if calls["n"] == 1:
            # Between planning and release the session goes mid-turn: the
            # streaming layer sets the lease (register_active_run).  This is
            # the window the old test never opened.
            cache["k"][0]._turn_active = True
        return plan

    gmod.plan_pressure_evictions = _plan_wrapper
    try:
        dropped = g.sweep_pressure(rss_mb=500)
    finally:
        gmod.plan_pressure_evictions = orig_plan
    # The plan targeted k, but the release-time lease says mid-turn: skip.
    assert dropped == 0
    assert cache["k"][0]._session_messages  # transcript intact


def test_pressure_sweep_fail_closed_when_registry_unavailable():
    """An unavailable liveness registry must skip the pass (fail CLOSED).

    The old empty-set fallback read as 'no one is running' and soft-released
    EVERY transcript — the exact 'can't determine liveness' case that must
    never evict.
    """
    cache = _cache_with([("k", _FakeAgent(flushed=True))])

    def _broken_fn():
        raise RuntimeError("registry down")

    g = _governor(
        cache, memory_high_mb=100, protect_recent=0, active_sids_fn=_broken_fn
    )
    assert g.sweep_pressure(rss_mb=500) == 0
    assert cache["k"][0]._session_messages  # nothing dropped


def test_idle_sweep_fail_closed_when_registry_unavailable():
    cache = _cache_with([("k", _FakeAgent(last_activity_ts=time.time() - 7200))])

    def _broken_fn():
        raise RuntimeError("registry down")

    g = _governor(
        cache, idle_ttl_secs=3600, active_sids_fn=_broken_fn,
        close_agent_fn=lambda k, a: None,
    )
    assert g.sweep_idle(now=time.time()) == 0
    assert "k" in cache


def test_pressure_sweep_respects_lease_at_release():
    """The lease (per-entry mid-turn marker) overrides a stale empty plan-time
    snapshot: a session that went active after planning is never released.
    """
    cache = _cache_with([("k", _FakeAgent(flushed=True, turn_active=True))])
    g = _governor(cache, memory_high_mb=100, protect_recent=0)
    assert g.sweep_pressure(rss_mb=500) == 0
    assert cache["k"][0]._session_messages


def test_idle_sweep_respects_lease():
    """Idle sweep must not tear down an agent whose lease says mid-turn, even
    when the plan-time snapshot missed it."""
    now = time.time()
    cache = _cache_with([
        ("busy", _FakeAgent(last_activity_ts=now - 7200, turn_active=True)),
        ("stale", _FakeAgent(last_activity_ts=now - 7200)),
    ])
    closed = []
    g = _governor(cache, idle_ttl_secs=3600, close_agent_fn=lambda k, a: closed.append(k))
    assert g.sweep_idle(now=now) == 1
    assert closed == ["stale"]
    assert "busy" in cache


def test_pressure_sweep_concurrent_churn_no_crash():
    """The snapshot must be taken under the lock; unlocked iteration over a
    churning OrderedDict races (RuntimeError: dictionary changed size during
    iteration). Deterministic check: churn from another thread while sweeping."""
    from collections import OrderedDict

    n = 500
    cache = OrderedDict(
        (f"k{i}", (_FakeAgent(flushed=True), "sig")) for i in range(n)
    )
    g = _governor(cache, memory_high_mb=100, protect_recent=0)
    stop = threading.Event()
    errors = []

    def churn():
        i = 0
        while not stop.is_set():
            try:
                if len(cache) > n:
                    cache.popitem(last=False)
                cache[f"churn{i % 200}"] = (_FakeAgent(flushed=True), "sig")
                i += 1
            except Exception:
                pass

    t = threading.Thread(target=churn, daemon=True)
    t.start()
    try:
        for _ in range(100):
            try:
                g.sweep_pressure(rss_mb=500)
            except Exception as e:  # noqa: BLE001
                errors.append(e)
    finally:
        stop.set()
        t.join(timeout=5)
    assert not errors, f"sweep crashed under churn: {errors[0]}"
