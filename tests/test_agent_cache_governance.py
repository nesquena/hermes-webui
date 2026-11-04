"""Unit tests for api.agent_cache_governance (memory-pressure + idle-TTL valves).

Run (per AGENTS.md, from the repo root):
    ./scripts/test.sh tests/test_agent_cache_governance.py
"""
import logging
import sys
import threading
import time

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
    # The governor must share the cache lock with every writer: a churn thread
    # mutating the OrderedDict under a DIFFERENT lock still races the locked
    # snapshot (RuntimeError: dictionary changed size during iteration).
    lock = kw.pop("lock", None) or threading.Lock()
    # Accept the old ``running_check`` kwarg for legacy tests; translate to the
    # new snapshot-based active-sids contract.
    if "running_check" in kw:
        rc = kw.pop("running_check")

        def _snapshot():
            return {k for k in list(cache) if rc(k)}

        kw["active_sids_fn"] = _snapshot
    return AgentCacheGovernor(cache, lock, **kw)


def _wait_release(gov, timeout=10.0):
    """Wait for the governor's pressure-release batch to finish.

    The pass itself never waits (the batch is provider I/O), so a test that
    asserts on released transcripts must join it first.
    """
    done = getattr(gov, "_last_release_done", None)
    if done is not None:
        assert done.wait(timeout), "pressure release thread did not finish"


# ── resolve_memory_high_mb ────────────────────────────────────────────────


def test_memory_high_auto_with_total_ram(monkeypatch):
    import api.agent_cache_governance as gmod

    # Host-independent: inside a finite cgroup the resolver would use that limit
    # instead of the injected total_ram_mb (a 1 GiB cap yields 665, not 1300).
    monkeypatch.setattr(gmod, "_cgroup_memory_limit_bytes", lambda: None)
    assert resolve_memory_high_mb("auto", total_ram_mb=2000) == 1300
    assert resolve_memory_high_mb("auto", total_ram_mb=512) >= 512  # floor
    # ...and a real cgroup cap still wins over total RAM.
    monkeypatch.setattr(gmod, "_cgroup_memory_limit_bytes", lambda: 4 * 1024 * 1024 * 1024)
    assert resolve_memory_high_mb("auto", total_ram_mb=2000) == 2662


def test_memory_high_number():
    assert resolve_memory_high_mb("512", total_ram_mb=2000) == 512
    assert resolve_memory_high_mb(400, total_ram_mb=2000) == 400


def test_memory_high_disabled():
    assert resolve_memory_high_mb("0") is None
    assert resolve_memory_high_mb("off") is None
    assert resolve_memory_high_mb("") is None
    assert resolve_memory_high_mb(None) is None
    assert resolve_memory_high_mb("abc") is None


# ── cgroup limit discovery ────────────────────────────────────────────────
def test_own_cgroup_path_prefers_the_memory_controller(monkeypatch):
    """On a v1/hybrid host the first line belongs to another controller
    (commonly cpuset at /); returning it would look for the limit at the
    unlimited root instead of the group this process actually runs in."""
    import builtins
    import io

    import api.agent_cache_governance as gmod

    monkeypatch.setattr(builtins, "open",
                        lambda path, *a, **k: io.StringIO("12:cpuset:/\n4:memory:/capped/child\n"),
                        raising=True)
    assert gmod._own_cgroup_path() == "/capped/child"

    monkeypatch.setattr(builtins, "open",
                        lambda path, *a, **k: io.StringIO("0::/user.slice/session.scope\n"),
                        raising=True)
    assert gmod._own_cgroup_path() == "/user.slice/session.scope"


def test_cgroup_v1_child_limit_is_read(monkeypatch):
    """A server in a capped cgroup-v1 child under an unlimited v1 root must
    report the CHILD limit: the own path is a v1 memory-mount subdirectory, so
    v2 filenames alone miss it and the pressure pass silently never fires."""
    import builtins
    import io

    import api.agent_cache_governance as gmod

    gib = 1024 * 1024 * 1024
    files = {
        "/proc/self/cgroup": "4:memory:/capped\n",
        # v2 filenames for the own path do not exist on a v1 host.
        "/sys/fs/cgroup/memory/capped/memory.limit_in_bytes": str(gib),
        # v1 root is unlimited (near-2^63 sentinel).
        "/sys/fs/cgroup/memory/memory.limit_in_bytes": "9223372036854771712",
    }
    real_open = builtins.open

    def _open(path, *a, **k):
        if path in files:
            return io.StringIO(files[path])
        return real_open(path, *a, **k)

    monkeypatch.setattr(builtins, "open", _open, raising=True)
    monkeypatch.setattr(gmod, "sys_platform_linux", lambda: True)

    assert gmod._cgroup_memory_limit_bytes() == gib


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


def test_plan_protect_recent_clamped_to_half_cache():
    """protect_recent is an upper bound clamped to half the cache (gateway
    parity), so a cache smaller than protect_recent can never invert the LRU
    protection window.  With default protect_recent=8:

      cache=1  -> protect 0, 1 candidate    (was: 0 — over-protected)
      cache=5  -> protect 2, 3 candidates   (was: 2 — inverted window)
      cache=7  -> protect 3, 4 candidates   (was: 6 — inverted window)
      cache=8  -> protect 4, 4 candidates   (was: 0 — over-protected)
      cache=9  -> protect 4, 5 candidates   (was: 1 — over-protected)
    """
    # (cache_size, expected_candidate_count) at protect_recent=8
    expected = {1: 1, 5: 3, 7: 4, 8: 4, 9: 5}
    for n, want in expected.items():
        agents = [(f"k{i}", _FakeAgent()) for i in range(n)]
        plan = plan_pressure_evictions(agents, lambda k, a: True, protect_recent=8)
        assert len(plan) == want, f"cache={n}: expected {want} candidates, got {len(plan)}"


def test_plan_protect_recent_zero_disables_protection():
    """protect_recent=0 must shed every evictable entry (no protection)."""
    agents = [(f"k{i}", _FakeAgent()) for i in range(10)]
    plan = plan_pressure_evictions(agents, lambda k, a: True, protect_recent=0)
    assert len(plan) == 10


def test_plan_protect_recent_negative_treated_as_zero():
    """A negative protect_recent is clamped to 0 (never a negative slice)."""
    agents = [(f"k{i}", _FakeAgent()) for i in range(5)]
    plan = plan_pressure_evictions(agents, lambda k, a: True, protect_recent=-3)
    assert len(plan) == 5


def test_plan_max_evictions_zero_returns_empty():
    agents = [(f"k{i}", _FakeAgent()) for i in range(10)]
    plan = plan_pressure_evictions(
        agents, lambda k, a: True, protect_recent=0, max_evictions=0
    )
    assert plan == []


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
def test_pressure_sweep_sheds_lru_and_keeps_the_protected_mru():
    """LRU-first, capped by protect_recent, and the shed entries leave the cache.

    The gateway pops the planned entries out of the cache and soft-releases
    them; the agent handle survives, the transcript does not.
    """
    agents = [(f"k{i}", _FakeAgent()) for i in range(5)]
    cache = _cache_with(agents)
    g = _governor(
        cache,
        memory_high_mb=100,
        protect_recent=1,
        running_check=lambda k: False,
    )
    dropped = g.sweep_pressure(rss_mb=500)
    _wait_release(g)

    assert dropped == 4  # k0..k3 shed, k4 protected
    assert set(cache.keys()) == {"k4"}
    assert cache["k4"][0]._session_messages  # protected: transcript intact
    for key, agent in agents[:4]:
        assert agent._session_messages == [], f"{key} still holds a transcript"
        assert agent._db_flush_scan_prefix is None





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
    agent = _FakeAgent(flushed=True)
    cache = _cache_with([("k", agent)])
    g = _governor(cache, memory_high_mb=100, protect_recent=0)
    assert g.sweep_pressure(rss_mb=500) == 1
    _wait_release(g)
    assert agent._session_messages == []
    assert cache == {}  # the entry left the cache; the handle is kept by the caller





def test_pressure_shed_never_touches_a_replacement_agent():
    """An entry re-inserted after planning is a different object, never shed.

    The plan is claimed (popped) under the cache lock, so the only objects the
    release thread can touch are the ones validated at plan time.  A turn that
    starts in between misses the cache and rebuilds its own agent.
    """
    cache = _cache_with([("k", _FakeAgent(flushed=True))])
    g = _governor(cache, memory_high_mb=100, protect_recent=0)
    original = cache["k"][0]
    replacement = _FakeAgent(flushed=True)

    import api.agent_cache_governance as gmod

    orig_plan = gmod.plan_pressure_evictions

    def _plan_then_replace(ordered, is_evictable, **kw):
        plan = orig_plan(ordered, is_evictable, **kw)
        cache["k"] = (replacement, "sig")  # a new turn rebuilt the agent
        return plan

    gmod.plan_pressure_evictions = _plan_then_replace
    try:
        dropped = g.sweep_pressure(rss_mb=500)
        _wait_release(g)
    finally:
        gmod.plan_pressure_evictions = orig_plan

    assert dropped == 1
    assert cache["k"][0] is replacement
    assert replacement._session_messages  # the live replacement is untouched
    assert original._session_messages == []  # only the planned handle was shed





def test_a_turn_starting_after_the_plan_builds_its_own_agent():
    """The pop under the lock is the race protection (there is no re-check).

    A turn that starts between planning and release does not find the handle in
    the cache any more, so it builds its own agent; the batch only ever sheds
    transcripts nobody is serving.
    """
    agent = _FakeAgent(flushed=True)
    cache = _cache_with([("k", agent)])
    g = _governor(cache, memory_high_mb=100, protect_recent=0)
    started = {}

    import api.agent_cache_governance as gmod

    orig_plan = gmod.plan_pressure_evictions

    def _plan_then_start_turn(ordered, is_evictable, **kw):
        plan = orig_plan(ordered, is_evictable, **kw)
        # The mid-turn lease is published for the cache's CURRENT entry; the
        # planned one is already claimed, so the pass cannot see it again.
        rebuilt = _FakeAgent(flushed=True, turn_active=True)
        started["agent"] = rebuilt
        cache.setdefault("k", (rebuilt, "sig"))
        return plan

    gmod.plan_pressure_evictions = _plan_then_start_turn
    try:
        assert g.sweep_pressure(rss_mb=500) == 1
        _wait_release(g)
    finally:
        gmod.plan_pressure_evictions = orig_plan

    assert started["agent"]._session_messages  # the running turn's agent
    assert agent._session_messages == []  # the un-served handle was shed





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


def test_pressure_sweep_never_plans_a_mid_turn_session():
    """The lease (per-entry mid-turn marker) overrides a stale empty snapshot."""
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
    # One lock for the governor AND the churn thread: with separate locks the
    # locked snapshot still races this writer, so the test could fail at random
    # even though every production caller uses the shared lock.
    lock = threading.Lock()
    g = _governor(cache, memory_high_mb=100, protect_recent=0, lock=lock)
    stop = threading.Event()
    errors = []

    def churn():
        i = 0
        while not stop.is_set():
            try:
                with lock:
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


# ── turn-lease publication order (governor race) ─────────────────────────────
def test_register_active_run_publishes_lease_before_registry(monkeypatch):
    """The turn lease must be visible no later than the ACTIVE_RUNS row.

    The governor reads the cached agent's lease under SESSION_AGENT_CACHE_LOCK
    and treats it as authoritative.  If ``register_active_run`` published the
    registry row first, a pass landing in that window would see a live turn
    whose lease still held the previous turn's ``False`` and could idle-evict
    the agent that just started its next turn — the request then misses the
    cache and rebuilds the agent, losing cache-resident state such as
    ``_user_turn_count``.
    """
    import api.config as config
    from collections import OrderedDict

    agent = _FakeAgent(turn_active=False)
    monkeypatch.setattr(config, "SESSION_AGENT_CACHE", OrderedDict({"s1": (agent, "sig")}))

    lease_at_publish = []

    class _ObservingRuns(dict):
        def __setitem__(self, key, value):
            # Snapshot the lease at the exact moment the row becomes visible.
            lease_at_publish.append(getattr(agent, "_turn_active", None))
            super().__setitem__(key, value)

    runs = _ObservingRuns()
    monkeypatch.setattr(config, "ACTIVE_RUNS", runs)

    config.register_active_run("stream-1", session_id="s1")

    assert lease_at_publish == [True], (
        "the registry row became visible while the lease still read "
        f"{lease_at_publish}: the governor can idle-evict a just-started turn"
    )
    assert runs["stream-1"]["session_id"] == "s1"
    assert agent._turn_active is True


# ── lease lifecycle (finish-race / compression-rotation) ─────────────────────
def test_finishing_stream_does_not_overwrite_successor_lease(monkeypatch):
    """A finishing stream's teardown must not overwrite a successor's lease.

    unregister_active_run() pops the row, recomputes still_active and writes the
    lease in one ACTIVE_RUNS_LOCK section.  This pins the post-pop schedule: a
    successor that registers AFTER that section must not be clobbered by the
    finishing stream's earlier False, and the successor finishing last clears
    the lease.

    Deterministic interleaving: hook the ACTIVE_RUNS_LOCK release so the
    successor registers exactly after the finishing stream's critical section.
    """
    import api.config as config
    from collections import OrderedDict

    agent = _FakeAgent(turn_active=True)
    monkeypatch.setattr(config, "SESSION_AGENT_CACHE", OrderedDict({"s1": (agent, "sig")}))

    real_lock = config.ACTIVE_RUNS_LOCK
    releases = {"n": 0}

    class _ReleaseHookLock:
        def __init__(self):
            self._lock = real_lock

        def acquire(self, *a, **k):
            return self._lock.acquire(*a, **k)

        def release(self):
            self._lock.release()
            releases["n"] += 1
            if releases["n"] == 2:
                # Second release = the finishing stream's unregister just
                # popped its row.  A successor registers before that stream
                # writes its lease.
                config.register_active_run("stream-B", session_id="s1")

        def __enter__(self):
            self.acquire()
            return self

        def __exit__(self, *a):
            self.release()

    monkeypatch.setattr(config, "ACTIVE_RUNS_LOCK", _ReleaseHookLock())

    config.register_active_run("stream-A", session_id="s1")
    assert agent._turn_active is True
    config.unregister_active_run("stream-A")

    assert agent._turn_active is True, (
        "finishing stream A overwrote live successor B's lease with False"
    )
    assert "stream-B" in config.ACTIVE_RUNS
    # The successor finishing last clears the lease.
    config.unregister_active_run("stream-B")
    assert agent._turn_active is False


def test_successor_publication_shares_one_lock_with_retirement(monkeypatch):
    """The lease write and the row publication must occupy ONE locked section.

    Retirement recomputes and writes the lease under ACTIVE_RUNS_LOCK.  When the
    successor wrote its lease OUTSIDE that lock, a finishing stream could retire
    in the gap — it saw no successor row, wrote False, and the successor then
    published with registry=True, lease=False.  The maintainer's re-gate drove
    that interleaving end to end and observed the live successor actually
    idle-evicted (idle_evicted=1).
    """
    import api.config as config
    from collections import OrderedDict

    agent = _FakeAgent(turn_active=False)
    monkeypatch.setattr(config, "SESSION_AGENT_CACHE", OrderedDict({"s1": (agent, "sig")}))
    monkeypatch.setattr(config, "ACTIVE_RUNS", {})

    real_lock = config.ACTIVE_RUNS_LOCK
    depth = {"n": 0}

    class _DepthLock:
        def acquire(self, *a, **k):
            result = real_lock.acquire(*a, **k)
            depth["n"] += 1
            return result

        def release(self):
            depth["n"] -= 1
            real_lock.release()

        def __enter__(self):
            self.acquire()
            return self

        def __exit__(self, *a):
            self.release()

    monkeypatch.setattr(config, "ACTIVE_RUNS_LOCK", _DepthLock())

    real_lease = config._set_agent_cache_turn_lease
    held_during_lease_write = []

    def _observed(session_id, active):
        held_during_lease_write.append(depth["n"] > 0)
        return real_lease(session_id, active)

    monkeypatch.setattr(config, "_set_agent_cache_turn_lease", _observed)

    config.register_active_run("stream-B", session_id="s1")

    assert held_during_lease_write == [True], (
        "the successor's lease was written outside ACTIVE_RUNS_LOCK: a "
        "finishing stream can retire in that window and clear a live lease"
    )


def test_live_successor_is_not_idle_evicted_after_a_retirement_gap(monkeypatch):
    """A registered successor keeps a live lease across a retirement.

    Pins the observable consequence the re-gate drove end to end: after a
    finishing stream retires and a successor publishes, the idle pass must not
    remove the successor's agent (the ordering itself is pinned by
    test_successor_publication_shares_one_lock_with_retirement).
    """
    import api.config as config
    from collections import OrderedDict

    import api.agent_cache_governance as gmod

    agent = _FakeAgent(last_activity_ts=time.time() - 10_000, turn_active=False)
    cache = OrderedDict({"s1": (agent, "sig")})
    monkeypatch.setattr(config, "SESSION_AGENT_CACHE", cache)
    monkeypatch.setattr(config, "ACTIVE_RUNS", {})

    config.register_active_run("stream-A", session_id="s1")
    config.unregister_active_run("stream-A")          # A finishes
    config.register_active_run("stream-B", session_id="s1")  # successor publishes

    assert "stream-B" in config.ACTIVE_RUNS
    assert agent._turn_active is True, "the live successor published with a cleared lease"

    evicted = []
    g = gmod.AgentCacheGovernor(
        cache, threading.Lock(),
        idle_ttl_secs=60,
        close_agent_fn=lambda k, a: evicted.append(k),
        active_sids_fn=lambda: {(e or {}).get("session_id")
                                for e in config.ACTIVE_RUNS.values()},
    )
    assert g.sweep_idle() == 0
    assert evicted == [], "the live successor's agent was idle-evicted mid-turn"


def test_compression_rotation_updates_run_row_session_id(monkeypatch):
    """When compression rotates the session id, the ACTIVE_RUNS row must
    carry the moved cache key so the final unregister clears the lease
    under the NEW id.

    The cache key moves from old_sid to new_sid mid-turn (streaming.py
    moves the entry).  If the registry row keeps old_sid, the final
    unregister_active_run clears the lease under old_sid — which no longer
    holds a cache entry — and the agent under new_sid keeps ``_turn_active``
    True forever, so both governor passes skip it permanently.

    The row's ``session_id`` itself deliberately does NOT rotate: steer and
    ownership checks read it as the stream's owning session, and rewriting
    it flips late/stale steer classification (pinned by
    test_steer_worker_boundaries).  The moved cache key travels in its own
    ``lease_session_id`` field instead.

    Two halves:
    1. config-layer contract: update_active_run records the row's
       lease_session_id (the row is keyed by stream id, so the update is
       unconditional) and unregister_active_run clears the lease under it.
    2. wiring contract: the rotation site in streaming.py carries the
       ``update_active_run(stream_id, lease_session_id=new_sid)`` call
       (static AST check — the codebase has no runtime harness for this
       private worker block, so the assertion pins the exact wiring to
       prevent it being dropped in a future refactor).
    """
    import api.config as config
    import ast
    import os
    from collections import OrderedDict

    agent = _FakeAgent(turn_active=True)
    monkeypatch.setattr(
        config, "SESSION_AGENT_CACHE", OrderedDict({"new-sid": (agent, "sig")})
    )

    config.register_active_run("stream-1", session_id="old-sid")
    assert agent._turn_active is True

    # Rotation: record the new cache key for lease clearing; the row's
    # session_id must keep the original id (steer/ownership reads it).
    config.update_active_run("stream-1", lease_session_id="new-sid")
    assert config.ACTIVE_RUNS["stream-1"]["session_id"] == "old-sid"

    assert agent._turn_active is True
    # Final unregister finds the row under new-sid and clears the lease.
    config.unregister_active_run("stream-1")
    assert agent._turn_active is False

    # Wiring: the rotation block in streaming.py must follow the registry row.
    _streaming_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "api", "streaming.py",
    )
    with open(_streaming_path, encoding="utf-8") as _fh:
        _tree = ast.parse(_fh.read())
    _rotation_wired = False
    for _node in ast.walk(_tree):
        if not isinstance(_node, ast.If):
            continue
        _test_src = ast.unparse(_node.test)
        if "_agent_sid" in _test_src and "session_id" in _test_src:
            # Inside the rotation block: look for the update call.
            for _call in ast.walk(_node):
                if (
                    isinstance(_call, ast.Call)
                    and isinstance(_call.func, ast.Name)
                    and _call.func.id == "update_active_run"
                ):
                    _kw = {k.arg: ast.unparse(k.value) for k in _call.keywords}
                    if _kw.get("lease_session_id") == "new_sid":
                        _rotation_wired = True
    assert _rotation_wired, (
        "streaming.py rotation block must call "
        "update_active_run(stream_id, lease_session_id=new_sid)"
    )


# ── persistence gate + best-effort commit (gateway contract) ─────────────────
class _RecordingCommitAgent(_FakeAgent):
    """FakeAgent carrying the installed AIAgent commit contract.

    ``AIAgent.commit_memory_session(messages=None)`` forwards ``messages or []``
    to the provider's on_session_end() and returns None, wrapping provider I/O
    in a swallow-all helper.  ``commits`` records the payloads the governor
    delivered; ``_acknowledged`` only proves the return value is ignored.
    """

    def __init__(self, acknowledged=None, **kw):
        super().__init__(**kw)
        self.commits = []
        self._acknowledged = acknowledged

    def commit_memory_session(self, messages=None):
        self.commits.append(None if messages is None else list(messages))
        return self._acknowledged


class _RaisingCommitAgent(_FakeAgent):
    """The hook itself raises (owner scope refused / arity mismatch)."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.attempts = 0

    def commit_memory_session(self, messages=None):
        self.attempts += 1
        raise RuntimeError("commit hook unavailable")


def _pending_session(session_id, agent, text="remember this", flushed=True):
    """Register a session with one completed (therefore committable) turn."""
    from api.session_lifecycle import mark_turn_completed, register_agent

    messages = [{"role": "user", "content": text}]
    agent._session_messages = list(messages)
    agent._last_flushed_db_idx = len(messages) if flushed else 0
    register_agent(session_id, agent)
    mark_turn_completed(session_id, agent=agent)
    return messages


def test_commit_session_memory_delivers_the_owner_transcript():
    """The boundary commit hands the provider the real transcript.

    Calling the hook with no payload (or an empty list) makes a
    batch-extraction provider skip extraction behind its empty-input guard.
    """
    from api.session_lifecycle import _reset_for_tests, commit_session_memory

    _reset_for_tests()
    agent = _RecordingCommitAgent(acknowledged=None)
    captured = _pending_session("s1", agent)

    assert commit_session_memory("s1", agent=agent) is True
    assert agent.commits == [captured], "the provider did not receive the transcript"


def test_commit_session_memory_ignores_the_hook_return_value():
    """Installed contract: the hook returns None and the ledger still advances.

    The gateway never gates on the hook's return value, because the installed
    Agent returns None even when extraction worked — and None again when the
    provider threw behind its swallow-all helper.
    """
    from api.session_lifecycle import (
        _reset_for_tests,
        commit_session_memory,
        has_uncommitted_work,
    )

    _reset_for_tests()
    agent = _RecordingCommitAgent(acknowledged=None)
    _pending_session("s1", agent)

    assert commit_session_memory("s1", agent=agent) is True
    assert has_uncommitted_work("s1") is False
    assert commit_session_memory("s1", agent=agent) is False  # nothing pending
    assert len(agent.commits) == 1


def test_unflushed_session_is_never_shed_and_is_counted_in_the_warning(caplog):
    """(a) The persistence gate is the only losslessness condition.

    An un-flushed transcript is skipped, counted in the gateway's warning, and
    costs no provider call: there is nothing to commit for a session whose live
    transcript is the only complete copy.
    """
    from api.session_lifecycle import _reset_for_tests

    _reset_for_tests()
    agent = _RecordingCommitAgent(acknowledged=None)
    cache = _cache_with([("k0", agent)])
    _pending_session("k0", agent, flushed=False)  # dirty work AND un-flushed

    g = _governor(cache, memory_high_mb=100, protect_recent=0,
                  running_check=lambda k: False)
    with caplog.at_level(logging.WARNING, logger="api.agent_cache_governance"):
        assert g.sweep_pressure(rss_mb=500) == 0
        _wait_release(g)

    assert agent.commits == [], "an un-flushed session must cost no provider call"
    assert agent._session_messages, "the transcript was dropped before it persisted"
    assert cache.get("k0", (None,))[0] is agent
    assert "1 cached, 0 mid-turn, 1 blocked on un-flushed persistence" in caplog.text
    assert "cannot shed sessions until they persist" in caplog.text


def test_flushed_idle_session_is_shed_and_the_next_turn_rebuilds_it():
    """(b) A fully flushed, idle session above budget IS shed.

    The transcript is freed (measured), the handle is kept, the entry leaves the
    cache, and the next turn rebuilds the transcript from the persisted session.
    """
    import gc
    import hashlib
    import json
    import tracemalloc

    def _digest(messages):
        return hashlib.sha256(json.dumps(messages).encode()).hexdigest()

    tracemalloc.start()
    try:
        # The session DB holds a serialized copy (distinct objects/strings, as a
        # real reload produces); the agent holds the live transcript.
        db_json = json.dumps([{"role": "user", "content": "x" * (1 << 20)} for _ in range(6)])
        agent = _FakeAgent(flushed=True)
        agent._session_messages = json.loads(db_json)
        agent._last_flushed_db_idx = len(agent._session_messages)
        want = _digest(agent._session_messages)
        cache = _cache_with([("k0", agent)])
        gc.collect()

        before = tracemalloc.get_traced_memory()[0]
        closed = []
        g = _governor(cache, memory_high_mb=100, protect_recent=0,
                      running_check=lambda k: False,
                      close_agent_fn=lambda k, a: closed.append(k))
        assert g.sweep_pressure(rss_mb=500) == 1
        _wait_release(g)
        gc.collect()
        after = tracemalloc.get_traced_memory()[0]

        assert after <= before - (3 << 20), (
            f"the shed freed nothing: before={before} after={after}"
        )
        assert agent._session_messages == [], "the transcript was not freed"
        assert agent._db_flush_scan_prefix is None
        assert cache == {}, "the entry must leave the cache (next turn rebuilds)"
        assert closed == [], "the pressure pass must not tear the agent down"
        # The next turn rebuilds from the persisted session (the gate guarantees
        # the DB holds every message the shed transcript had).
        rebuilt = _FakeAgent(flushed=True)
        rebuilt._session_messages = json.loads(db_json)
        assert _digest(rebuilt._session_messages) == want, "the rebuilt transcript differs"
    finally:
        tracemalloc.stop()


def test_mid_turn_session_is_never_shed():
    """(c) A mid-turn session is never touched, in either liveness spelling."""
    busy = _FakeAgent(flushed=True, turn_active=True)
    registered = _FakeAgent(flushed=True)  # no lease: registry snapshot decides
    cache = _cache_with([("busy", busy), ("registered", registered)])

    g = _governor(
        cache,
        memory_high_mb=100,
        protect_recent=0,
        running_check=lambda k: k == "registered",
    )
    assert g.sweep_pressure(rss_mb=500) == 0
    _wait_release(g)

    assert cache["busy"][0] is busy and busy._session_messages
    assert cache["registered"][0] is registered and registered._session_messages


def test_best_effort_commit_runs_once_per_shed_session_and_is_never_retried(monkeypatch):
    """(d) At most ONE commit per shed session per pass; no retry anywhere.

    After the shed the session is out of the cache (a later pass cannot reach
    it) and the lifecycle ledger has advanced, so the next session boundary
    finds nothing pending and calls the provider no further time.
    """
    from api.session_lifecycle import _reset_for_tests, commit_session_memory

    _reset_for_tests()
    agent = _RecordingCommitAgent(acknowledged=None)
    cache = _cache_with([("k0", agent)])
    captured = _pending_session("k0", agent)

    g = _governor(cache, memory_high_mb=100, protect_recent=0,
                  running_check=lambda k: False)
    assert g.sweep_pressure(rss_mb=500) == 1
    _wait_release(g)
    assert agent.commits == [captured], "the commit must deliver the transcript once"
    assert agent._session_messages == []

    # A later pass has nothing to reach and must not re-deliver.
    assert g.sweep_pressure(rss_mb=500) == 0
    _wait_release(g)
    assert len(agent.commits) == 1, "a later pass re-delivered the commit"

    # A later session boundary finds the generation committed: no new call.
    assert commit_session_memory("k0", agent=agent) is False
    assert len(agent.commits) == 1, "the boundary retried a finished commit"


def test_a_raising_commit_hook_is_swallowed_and_not_retried_by_the_pass():
    """(d, failure half) The hook raises, the shed still happens, once."""
    from api.session_lifecycle import _reset_for_tests

    _reset_for_tests()
    agent = _RaisingCommitAgent()
    cache = _cache_with([("k0", agent)])
    _pending_session("k0", agent)

    g = _governor(cache, memory_high_mb=100, protect_recent=0,
                  running_check=lambda k: False)
    assert g.sweep_pressure(rss_mb=500) == 1
    _wait_release(g)
    assert agent._session_messages == []
    assert agent.attempts == 1

    assert g.sweep_pressure(rss_mb=500) == 0
    _wait_release(g)
    assert agent.attempts == 1, "the governor retried a shed session"


def test_unflushed_session_gets_no_provider_call_over_ten_passes():
    """(e) No per-pass re-delivery: 10 over-budget passes, 0 provider calls."""
    from api.session_lifecycle import _reset_for_tests

    _reset_for_tests()
    agent = _RecordingCommitAgent(acknowledged=None)
    cache = _cache_with([("k0", agent)])
    _pending_session("k0", agent, flushed=False)

    g = _governor(cache, memory_high_mb=100, protect_recent=0,
                  running_check=lambda k: False)
    for _ in range(10):
        assert g.sweep_pressure(rss_mb=500) == 0
        _wait_release(g)

    assert agent.commits == [], "an un-flushed session must never reach the provider"
    assert agent._session_messages


def test_protect_recent_clamp_and_lru_order_at_governor_level():
    """(f) protect_recent clamps to half the cache and the LRU goes first."""
    agents = [(f"k{i}", _FakeAgent(flushed=True)) for i in range(10)]
    cache = _cache_with(agents)

    g = _governor(cache, memory_high_mb=100, protect_recent=8,
                  running_check=lambda k: False)
    assert g.sweep_pressure(rss_mb=500) == 5  # clamp: 10 // 2 = 5 protected
    _wait_release(g)

    assert set(cache.keys()) == {"k5", "k6", "k7", "k8", "k9"}  # the MRU five
    for key, agent in agents[:5]:
        assert agent._session_messages == [], f"{key} (LRU front) was not shed first"
    for key, agent in agents[5:]:
        assert agent._session_messages, f"{key} (protected) was shed"
