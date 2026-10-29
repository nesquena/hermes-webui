"""Unit tests for api.agent_cache_governance (memory-pressure + idle-TTL valves).

Run (per AGENTS.md, from the repo root):
    ./scripts/test.sh tests/test_agent_cache_governance.py
"""
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


def test_pressure_sweep_does_not_replan_released_entries():
    """Released (empty) entries must not be re-selected on later passes.

    soft_release_transcript() empties _session_messages, and
    transcript_persistence_caught_up() then reads ``flushed >= 0`` == True
    forever, so the same 16 empty entries stay at the LRU front and every
    subsequent pass re-drops them while the entries that still hold memory
    are never reached.  An entry with no resident transcript (and no scan
    prefix) must be excluded from eviction planning until the transcript is
    rebuilt on the next turn.

    Regression: two consecutive passes with >16 evictable entries.  Pass 1
    releases the first 16; pass 2 must advance to the next entries instead of
    re-releasing the same 16.
    """
    from collections import OrderedDict

    agents = [(f"k{i}", _FakeAgent(flushed=True)) for i in range(24)]
    cache = OrderedDict((k, (a, "sig")) for k, a in agents)
    g = _governor(
        cache,
        memory_high_mb=100,
        protect_recent=0,
        running_check=lambda k: False,
    )
    # Pass 1: first 16 released (LRU front).
    dropped_1 = g.sweep_pressure(rss_mb=500)
    assert dropped_1 == 16
    # Pass 2: must advance past the released 16 and drop the remaining 8,
    # NOT re-drop the same 16 (which would report 16 and free nothing).
    dropped_2 = g.sweep_pressure(rss_mb=500)
    assert dropped_2 == 8, f"pass 2 dropped {dropped_2}: released entries were re-planned"
    # All 24 transcripts released exactly once; the cache still holds all agents.
    for k, (a, _sig) in cache.items():
        assert a._session_messages == [], f"{k} still holds a transcript"


def test_pressure_release_commits_pending_memory_before_drop(monkeypatch):
    """Pending lifecycle work must be committed with the captured transcript
    BEFORE the release empties _session_messages.

    The pressure release clears the agent's transcript; a later
    shutdown_memory_provider / commit_memory_session boundary then sees [] and
    providers with empty-input guards skip fact extraction, losing the
    session's memories.  The governor must commit pending lifecycle work
    first (so the provider sees the captured transcript).
    """
    import api.agent_cache_governance as gmod
    from api.session_lifecycle import register_agent, _reset_for_tests

    _reset_for_tests()
    agent = _FakeAgent(flushed=True)
    cache = _cache_with([("k0", agent)])
    register_agent("k0", agent)

    committed = {}

    def _fake_commit(session_id, agent=None, wait=False):
        committed["sid"] = session_id
        committed["messages"] = list(getattr(agent, "_session_messages", []))
        return True

    monkeypatch.setattr(gmod, "_commit_pending_memory", _fake_commit)

    g = _governor(cache, memory_high_mb=100, protect_recent=0,
                  running_check=lambda k: False)
    dropped = g.sweep_pressure(rss_mb=500)

    assert dropped == 1
    assert committed.get("sid") == "k0"
    assert committed.get("messages"), "commit must see the captured transcript"
    assert agent._session_messages == []


def test_pressure_release_skipped_when_commit_fails(monkeypatch):
    """When the pending-memory commit does not happen (in-flight elsewhere or
    provider failure), the release must be skipped — a skipped release costs
    memory, a wrong release costs the user's memories."""
    import api.agent_cache_governance as gmod

    agent = _FakeAgent(flushed=True)
    cache = _cache_with([("k0", agent)])

    monkeypatch.setattr(gmod, "_commit_pending_memory", lambda sid, agent=None, wait=False: False)

    g = _governor(cache, memory_high_mb=100, protect_recent=0,
                  running_check=lambda k: False)
    dropped = g.sweep_pressure(rss_mb=500)

    assert dropped == 0
    assert agent._session_messages  # transcript intact — retry next pass


def test_commit_pending_memory_wiring(monkeypatch):
    """_commit_pending_memory consults the lifecycle registry: no pending
    work → True (release proceeds); pending work → the release-gate commit for
    the cached agent (the lifecycle picks the payload's owner)."""
    import api.agent_cache_governance as gmod
    import api.streaming as streaming

    agent = _FakeAgent(flushed=True)
    seen = {}

    monkeypatch.setattr(streaming, "_lifecycle_has_uncommitted_work", lambda sid: True)

    def _fake_commit(session_id, agent=None):
        seen["sid"] = session_id
        seen["agent"] = agent
        return True

    monkeypatch.setattr(streaming, "_lifecycle_commit_pending_for_release", _fake_commit)

    assert gmod._commit_pending_memory("s1", agent) is True
    assert seen == {"sid": "s1", "agent": agent}

    monkeypatch.setattr(streaming, "_lifecycle_has_uncommitted_work", lambda sid: False)
    seen.clear()
    assert gmod._commit_pending_memory("s1", agent) is True
    assert seen == {}  # nothing pending — no commit call needed


# ── pre-release memory extraction (owner payload + durable release gate) ─────
class _RecordingCommitAgent(_FakeAgent):
    """FakeAgent carrying the installed AIAgent commit contract.

    ``AIAgent.commit_memory_session(messages=None)`` forwards ``messages or []``
    to the provider's on_session_end() and returns None, wrapping provider I/O
    in a swallow-all helper — so a provider failure looks exactly like success.
    Returning a truthy value models the acknowledgement the release gate needs;
    the default (None) is the installed behaviour.
    """

    def __init__(self, acknowledged=None, **kw):
        super().__init__(**kw)
        self.commits = []
        self._acknowledged = acknowledged

    def commit_memory_session(self, messages=None):
        self.commits.append(None if messages is None else list(messages))
        return self._acknowledged


class _SwallowingCommitAgent(_FakeAgent):
    """Installed contract with a FAILING provider.

    The real agent runs the provider call through ``_quietly(...)``, so a throw
    is swallowed and the hook still returns None: the caller cannot tell the
    extraction never happened.
    """

    def __init__(self, **kw):
        super().__init__(**kw)
        self.attempts = 0

    def commit_memory_session(self, messages=None):
        self.attempts += 1
        try:
            raise RuntimeError("provider exploded")
        except RuntimeError:
            pass  # _quietly()
        return None


def _pending_session(session_id, agent, text="remember this"):
    """Register a session with one completed (therefore committable) turn."""
    from api.session_lifecycle import mark_turn_completed, register_agent

    messages = [{"role": "user", "content": text}]
    agent._session_messages = list(messages)
    agent._last_flushed_db_idx = len(messages)
    register_agent(session_id, agent)
    mark_turn_completed(session_id, agent=agent)
    return messages


def test_commit_pending_for_release_delivers_the_selected_owner_transcript():
    """The pre-release commit must hand the provider the real transcript.

    Calling the hook with no payload (or an empty list) makes a
    batch-extraction provider skip extraction behind its empty-input guard while
    the governor treats the work as done.
    """
    from api.session_lifecycle import _reset_for_tests, commit_pending_for_release

    _reset_for_tests()
    agent = _RecordingCommitAgent(acknowledged=True, flushed=True)
    captured = _pending_session("s1", agent)

    assert commit_pending_for_release("s1", agent) is True
    assert agent.commits == [captured], "the provider did not receive the transcript"


def test_commit_pending_for_release_retains_transcript_without_acknowledgement():
    """A normal return is not extraction acknowledgement.

    The installed agent swallows provider errors and returns None, so success
    and failure are indistinguishable from here.  Nothing acknowledged the
    delivery, so the release is refused — and the refusal must be durable: the
    generation stays dirty, otherwise the next caller takes the "nothing
    pending" shortcut and clears the transcript with no further attempt.
    """
    from api.session_lifecycle import (
        _reset_for_tests,
        commit_pending_for_release,
        has_uncommitted_work,
    )

    _reset_for_tests()
    agent = _RecordingCommitAgent(acknowledged=None, flushed=True)
    captured = _pending_session("s1", agent)

    assert commit_pending_for_release("s1", agent) is False
    assert agent.commits == [captured], "the transcript was still delivered"
    assert has_uncommitted_work("s1") is True, (
        "the obligation was marked committed without an acknowledgement: the "
        "next pass would clear the transcript with no extraction attempt"
    )


def test_unacknowledged_refusal_is_retried_by_the_next_caller():
    """The next caller must re-attempt the same owner/generation, not take the
    "nothing pending" shortcut."""
    from api.session_lifecycle import _reset_for_tests, commit_pending_for_release

    _reset_for_tests()
    agent = _RecordingCommitAgent(acknowledged=None, flushed=True)  # installed contract
    captured = _pending_session("s1", agent)

    assert commit_pending_for_release("s1", agent) is False
    assert commit_pending_for_release("s1", agent) is False
    assert agent.commits == [captured, captured], "the retry must reuse the same payload"


def test_commit_pending_for_release_fails_closed_when_the_hook_rejects_messages():
    """An agent whose commit hook cannot accept a transcript gets no
    extraction; the release must then be refused rather than assumed safe."""
    from api.session_lifecycle import _reset_for_tests, commit_pending_for_release

    _reset_for_tests()
    calls = []

    class _LegacySignatureAgent(_FakeAgent):
        def commit_memory_session(self):  # pre-messages signature
            calls.append(True)

    agent = _LegacySignatureAgent(flushed=True)
    _pending_session("s1", agent)

    assert commit_pending_for_release("s1", agent) is False
    # The arity mismatch raises before the hook body runs: nothing was
    # extracted, so nothing may authorize clearing the transcript.
    assert calls == []
    assert agent._session_messages


def test_commit_pending_for_release_keeps_transcript_when_a_successor_registers_mid_io():
    """The commit runs outside the cache lock, so a successor stream can start
    AND finish while it is in flight; that work still needs the transcript."""
    from api.session_lifecycle import (
        _reset_for_tests,
        commit_pending_for_release,
        mark_turn_completed,
    )

    _reset_for_tests()
    agent = _RecordingCommitAgent(acknowledged=True, flushed=True)
    _pending_session("s1", agent)

    original = agent.commit_memory_session

    def _commit_then_successor(messages=None):
        result = original(messages)
        mark_turn_completed("s1", agent=agent)  # successor ran to completion mid-I/O
        return result

    agent.commit_memory_session = _commit_then_successor

    assert commit_pending_for_release("s1", agent) is False


def test_dirty_owners_each_receive_their_own_transcript():
    """Owner, generation and payload are selected together.

    A rebuild leaves the older handle owning its earlier dirty generation; the
    older owner must receive ITS transcript, not the cached replacement's (the
    re-gate measured both hooks receiving the NEW owner's memory).
    """
    from api.session_lifecycle import (
        _reset_for_tests,
        commit_pending_for_release,
        register_agent,
    )

    _reset_for_tests()
    old = _RecordingCommitAgent(acknowledged=True, flushed=True)
    new = _RecordingCommitAgent(acknowledged=True, flushed=True)
    old_msgs = _pending_session("s1", old, text="OLD owner memory")
    register_agent("s1", new)
    new_msgs = _pending_session("s1", new, text="NEW owner memory")

    # First call commits the earliest dirty segment (the OLD owner) with the OLD
    # owner's own payload; the newer generation is still pending, so the
    # transcript is kept.
    assert commit_pending_for_release("s1", new) is False
    assert old.commits == [old_msgs], "the old owner received the wrong payload"
    assert new.commits == []

    # Second call commits the NEW segment with the NEW owner's payload.
    assert commit_pending_for_release("s1", new) is True
    assert new.commits == [new_msgs]
    assert old.commits == [old_msgs], "the old owner must not be re-committed"


def test_failed_old_owner_stays_retryable_with_its_own_payload():
    """A throwing older owner keeps ITS generation dirty, and the retry reuses
    the same owner/generation/payload — never the replacement's."""
    from api.session_lifecycle import (
        _reset_for_tests,
        commit_pending_for_release,
        has_uncommitted_work,
        register_agent,
    )

    _reset_for_tests()
    old = _RecordingCommitAgent(acknowledged=True, flushed=True)
    new = _RecordingCommitAgent(acknowledged=True, flushed=True)
    old_msgs = _pending_session("s1", old, text="OLD owner memory")
    register_agent("s1", new)
    _pending_session("s1", new, text="NEW owner memory")

    attempts = {"n": 0}
    original = old.commit_memory_session

    def _flaky(messages=None):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise RuntimeError("provider down")
        return original(messages)

    old.commit_memory_session = _flaky

    assert commit_pending_for_release("s1", new) is False   # old owner threw
    assert has_uncommitted_work("s1") is True
    assert commit_pending_for_release("s1", new) is False   # retried, still the new segment pending
    assert attempts["n"] == 2
    assert old.commits == [old_msgs], "the retry must reuse the old owner's own payload"


def test_pressure_release_delivers_the_transcript_before_dropping(monkeypatch):
    """End-to-end through the real lifecycle: a session with pending work is
    released only after the provider received the captured transcript."""
    from api.session_lifecycle import _reset_for_tests

    _reset_for_tests()
    agent = _RecordingCommitAgent(acknowledged=True, flushed=True)
    cache = _cache_with([("k0", agent)])
    _pending_session("k0", agent)

    g = _governor(cache, memory_high_mb=100, protect_recent=0,
                  running_check=lambda k: False)
    assert g.sweep_pressure(rss_mb=500) == 1
    assert agent.commits and agent.commits[0], "the provider saw an empty transcript"
    assert agent._session_messages == []


def test_pressure_release_retains_transcript_when_commit_is_unacknowledged():
    """Provider-input/failure control: an unacknowledged commit must not
    authorize clearing the transcript."""
    from api.session_lifecycle import _reset_for_tests

    _reset_for_tests()
    agent = _RecordingCommitAgent(acknowledged=None, flushed=True)
    cache = _cache_with([("k0", agent)])
    _pending_session("k0", agent)

    g = _governor(cache, memory_high_mb=100, protect_recent=0,
                  running_check=lambda k: False)
    assert g.sweep_pressure(rss_mb=500) == 0
    assert agent._session_messages, "transcript dropped without a confirmed commit"
    assert agent.commits and agent.commits[0]


def test_pressure_never_releases_an_unacknowledged_commit_across_passes():
    """Three PASSES with the installed method (returns None).

    The previous head retained the transcript in pass one but had already marked
    the obligation committed, so pass two took the "nothing pending" shortcut
    and cleared it with no extraction attempt (the re-gate measured
    ``drops=[0,1,0]`` for both a successful and a throwing provider).  Every
    pass must refuse the release AND keep the obligation dirty, so each pass
    retries the delivery.
    """
    from api.session_lifecycle import _reset_for_tests

    _reset_for_tests()
    agent = _RecordingCommitAgent(acknowledged=None, flushed=True)
    cache = _cache_with([("k0", agent)])
    _pending_session("k0", agent)

    g = _governor(cache, memory_high_mb=100, protect_recent=0,
                  running_check=lambda k: False)
    drops = [g.sweep_pressure(rss_mb=500) for _ in range(3)]

    assert drops == [0, 0, 0], f"an unconfirmed commit authorised a release: {drops}"
    assert agent._session_messages, "transcript cleared although nothing acknowledged extraction"
    assert len(agent.commits) == 3, "each pass must retry the delivery"


def test_pressure_refusal_survives_a_new_chat_background_commit():
    """Mixed schedule: pressure refusal → New Chat background commit → pressure.

    ``POST /api/session/new`` fire-and-forgets an ORDINARY
    ``commit_session_memory()`` for ``prev_session_id`` (api/routes.py).  That
    call cannot acknowledge, so it must not erase the obligation the release
    gate refused: it used to advance/remove the same dirty generation, after
    which the next pass read "nothing pending" and cleared the transcript with
    no extraction attempt (the re-gate measured ``drops=[0,1]`` over this exact
    composition).  Reproduced here with both installed-shaped agents.
    """
    from api.session_lifecycle import _reset_for_tests, commit_session_memory

    for build, label in ((_RecordingCommitAgent, "success-None"),
                         (_SwallowingCommitAgent, "swallowed-failure-None")):
        _reset_for_tests()
        agent = build(flushed=True)
        cache = _cache_with([("k0", agent)])
        _pending_session("k0", agent)

        g = _governor(cache, memory_high_mb=100, protect_recent=0,
                      running_check=lambda k: False)
        first = g.sweep_pressure(rss_mb=500)

        # The real New Chat handler's fire-and-forget commit (no acknowledgement).
        commit_session_memory("k0", agent=agent)

        second = g.sweep_pressure(rss_mb=500)

        # The discriminator: the second pass must not clear a transcript no
        # extraction confirmed (this read [0, 1] on the previous head).
        assert [first, second] == [0, 0], (
            f"{label}: an ordinary commit authorised the second release ({[first, second]})"
        )
        assert agent._session_messages, label

        # ...and the reason it held is the latched obligation.
        from api.session_lifecycle import has_release_debt, has_uncommitted_work

        assert has_uncommitted_work("k0") is False, f"{label}: the ordinary commit advanced the work"
        assert has_release_debt("k0") is True, (
            f"{label}: the ordinary commit erased the refused obligation"
        )


def test_an_acknowledged_commit_leaves_no_release_debt():
    """Control: a hook that acknowledges keeps the release path free — the debt
    exists only for deliveries nothing confirmed."""
    from api.session_lifecycle import (
        _reset_for_tests,
        commit_pending_for_release,
        has_release_debt,
    )

    _reset_for_tests()
    agent = _RecordingCommitAgent(acknowledged=True, flushed=True)
    _pending_session("s1", agent)

    assert commit_pending_for_release("s1", agent) is True
    assert has_release_debt("s1") is False


def test_release_debt_clears_once_its_owner_confirms():
    """The debt is latched to the exact owner/generation; an acknowledgement for
    that delivery frees the release again."""
    from api.session_lifecycle import (
        _reset_for_tests,
        commit_pending_for_release,
        has_release_debt,
    )

    _reset_for_tests()
    agent = _RecordingCommitAgent(acknowledged=None, flushed=True)
    _pending_session("s1", agent)

    assert commit_pending_for_release("s1", agent) is False
    assert has_release_debt("s1") is True

    agent._acknowledged = True  # the provider can confirm on a later attempt
    assert commit_pending_for_release("s1", agent) is True
    assert has_release_debt("s1") is False


def test_release_debt_retries_from_its_own_state_when_the_segment_is_gone():
    """Once an ordinary commit advanced the segment away, the debt's retained
    owner/payload is the only retry route — and it must be the same payload."""
    from api.session_lifecycle import (
        _reset_for_tests,
        commit_pending_for_release,
        commit_session_memory,
        has_release_debt,
        has_uncommitted_work,
    )

    _reset_for_tests()
    agent = _RecordingCommitAgent(acknowledged=None, flushed=True)
    _pending_session("s1", agent)

    assert commit_pending_for_release("s1", agent) is False   # latches the debt
    latched_payload = agent.commits[0]

    commit_session_memory("s1", agent=agent)                  # ordinary commit advances it
    assert has_uncommitted_work("s1") is False

    agent._acknowledged = True
    assert commit_pending_for_release("s1", agent) is True
    assert has_release_debt("s1") is False
    assert agent.commits[-1] == latched_payload, "the debt retry used a different payload"


def test_pressure_never_releases_a_swallowed_provider_failure_across_passes():
    """Same three-pass shape with a provider that throws inside the agent's
    swallow-all wrapper: indistinguishable from success from here, so it must
    stay retained and retried rather than released one pass later."""
    from api.session_lifecycle import _reset_for_tests

    _reset_for_tests()
    agent = _SwallowingCommitAgent(flushed=True)
    cache = _cache_with([("k0", agent)])
    _pending_session("k0", agent)

    g = _governor(cache, memory_high_mb=100, protect_recent=0,
                  running_check=lambda k: False)
    drops = [g.sweep_pressure(rss_mb=500) for _ in range(3)]

    assert drops == [0, 0, 0], f"a swallowed provider failure authorised a release: {drops}"
    assert agent._session_messages
    assert agent.attempts == 3
