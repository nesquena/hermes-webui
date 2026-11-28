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
    _commit_before_soft_release,
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

    def _open(path, *a, **k):
        # Hermetic: anything outside the fake tree is absent, never the host's
        # real cgroup files (the parent walk reads many more of them).
        if path in files:
            return io.StringIO(files[path])
        raise FileNotFoundError(2, "no such file or directory", path)

    monkeypatch.setattr(builtins, "open", _open, raising=True)
    monkeypatch.setattr(gmod, "sys_platform_linux", lambda: True)

    assert gmod._cgroup_memory_limit_bytes() == gib


def _fake_cgroup_tree(monkeypatch, own_path, files):
    """Install a hermetic fake cgroup tree; returns the governance module.

    Only ``files`` exist: any other ``/sys/fs/cgroup`` or ``/proc/self/cgroup``
    path raises FileNotFoundError instead of falling through to the host's real
    files, so neither a pass nor a failure can come from the machine pytest runs
    on.
    """
    import builtins
    import io

    import api.agent_cache_governance as gmod

    tree = dict(files)
    tree.setdefault("/proc/self/cgroup", f"0::{own_path}\n")
    real_open = builtins.open

    def _open(path, *a, **k):
        if path in tree:
            return io.StringIO(tree[path])
        if str(path).startswith("/sys/fs/cgroup") or path == "/proc/self/cgroup":
            raise FileNotFoundError(2, "no such file or directory", path)
        return real_open(path, *a, **k)

    monkeypatch.setattr(builtins, "open", _open, raising=True)
    monkeypatch.setattr(gmod, "sys_platform_linux", lambda: True)
    return gmod


def test_cgroup_limit_comes_from_a_parent_group(monkeypatch):
    """A pod/container cap lands on a PARENT while the own group reads ``max``.

    Reading own + root only (the pre-fix behaviour) returns None here, so
    ``auto`` falls back to HOST RAM and the budget exceeds the service's real
    limit — the process is OOM-killed before the valve sheds anything.  This is
    the regression for that gap and it fails without the parent walk.
    """
    mib = 1024 * 1024
    gmod = _fake_cgroup_tree(
        monkeypatch,
        "/kubepods/burstable/podA/ctr1",
        {
            "/sys/fs/cgroup/kubepods/burstable/podA/ctr1/memory.max": "max",
            "/sys/fs/cgroup/kubepods/burstable/podA/memory.max": str(4096 * mib),
            "/sys/fs/cgroup/memory.max": "max",
        },
    )

    assert gmod._cgroup_memory_limit_bytes() == 4096 * mib
    # The consequence, not just the number: the budget is derived from the
    # inherited 4 GiB cap, never from the 64 GiB host (which would be 41600).
    assert resolve_memory_high_mb("auto", total_ram_mb=64 * 1024) == 2662


def test_cgroup_limit_keeps_the_tightest_level(monkeypatch):
    """Smallest finite limit wins: a roomier parent never widens the budget."""
    gib = 1024 * 1024 * 1024
    gmod = _fake_cgroup_tree(
        monkeypatch,
        "/a/b/c",
        {
            "/sys/fs/cgroup/a/b/c/memory.max": str(2 * gib),
            "/sys/fs/cgroup/a/b/memory.max": str(4 * gib),
            # A grandparent throttling point is tighter than any child max.
            "/sys/fs/cgroup/a/memory.high": str(gib),
            "/sys/fs/cgroup/memory.max": "max",
        },
    )
    assert gmod._cgroup_memory_limit_bytes() == gib


def test_cgroup_max_at_one_level_does_not_hide_a_finite_grandparent(monkeypatch):
    """``max`` is "no cap at THIS level", not "no cap on the chain"."""
    mib = 1024 * 1024
    gmod = _fake_cgroup_tree(
        monkeypatch,
        "/slice/scope/sub",
        {
            "/sys/fs/cgroup/slice/scope/sub/memory.max": "max",
            "/sys/fs/cgroup/slice/scope/memory.max": "max",
            "/sys/fs/cgroup/slice/memory.high": str(256 * mib),
            "/sys/fs/cgroup/memory.max": "max",
        },
    )
    assert gmod._cgroup_memory_limit_bytes() == 256 * mib


def test_cgroup_all_levels_unlimited_returns_none(monkeypatch):
    """Every level unlimited (both sentinel spellings) → uncapped, not a bogus cap."""
    gmod = _fake_cgroup_tree(
        monkeypatch,
        "/x/y",
        {
            "/sys/fs/cgroup/x/y/memory.max": "max",
            "/sys/fs/cgroup/x/memory.max": "9223372036854771712",
            "/sys/fs/cgroup/memory/memory.limit_in_bytes": "9223372036854771712",
        },
    )
    assert gmod._cgroup_memory_limit_bytes() is None


# ── plan_pressure_evictions ────────────────────────────────────────────────
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
    """The lease (per-entry mid-turn marker) overrides a stale empty snapshot.

    The pass still sheds the idle peer in the SAME pass: one busy session must
    not stop the valve, and the busy one must be skipped on its lease alone (the
    registry snapshot here is the real, empty one).  Both carry pending work, so
    the debt-era code — which waited for an acknowledgement the installed Agent
    cannot give — shed nothing at all.
    """
    from api.session_lifecycle import _reset_for_tests

    _reset_for_tests()
    busy = _RecordingCommitAgent()
    busy._turn_active = True
    idle = _RecordingCommitAgent()
    cache = _cache_with([("busy", busy), ("idle", idle)])
    _pending_session("busy", busy)
    idle_captured = _pending_session("idle", idle)

    g = _governor(cache, memory_high_mb=100, protect_recent=0)
    assert g.sweep_pressure(rss_mb=500) == 1  # the idle peer only
    _wait_release(g)

    assert cache["busy"][0] is busy and busy._session_messages
    assert busy.commits == [], "the mid-turn session was committed for a shed"
    assert idle._session_messages == []
    assert idle.commits == [idle_captured]





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
    """The hook raises (owner scope refused / arity mismatch) `fail_times` times."""

    def __init__(self, fail_times=1, **kw):
        super().__init__(**kw)
        self.attempts = 0
        self.commits = []
        self._fail_left = fail_times

    def commit_memory_session(self, messages=None):
        self.attempts += 1
        if self._fail_left > 0:
            self._fail_left -= 1
            raise RuntimeError("commit hook unavailable")
        self.commits.append(None if messages is None else list(messages))


class _FatalCommitAgent(_FakeAgent):
    """The hook raises a BaseException (a lazy import's RelaunchExit)."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.attempts = 0

    def commit_memory_session(self, messages=None):
        self.attempts += 1
        raise SystemExit("relaunch")


class _GatedCommitAgent(_FakeAgent):
    """Commit blocks until the test opens the gate (a slow provider)."""

    def __init__(self, gate, started, **kw):
        super().__init__(**kw)
        self._gate = gate
        self._started = started
        self._caller_ident = threading.get_ident()
        self.commits = []

    def commit_memory_session(self, messages=None):
        assert threading.get_ident() != self._caller_ident, (
            "the pre-release commit ran on the pass thread: provider I/O is "
            "back on the pass instead of the release worker"
        )
        self._started.set()
        assert self._gate.wait(10), "the test never opened the commit gate"
        self.commits.append(None if messages is None else list(messages))


class _ReleasingAgent(_RecordingCommitAgent):
    """Records release_clients() and the state it saw when it ran."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.releases = 0
        self.commits_at_release = None
        self.transcript_len_at_release = None

    def release_clients(self):
        self.releases += 1
        self.commits_at_release = len(self.commits)
        self.transcript_len_at_release = len(self._session_messages)


class _LegacyAgent(_FakeAgent):
    """An agent shape with no release_clients() (legacy full-close path)."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.shutdowns = []
        self.closes = 0

    def shutdown_memory_provider(self, messages=None):
        self.shutdowns.append(None if messages is None else list(messages))

    def close(self):
        self.closes += 1


class _FatalOnceCommitAgent(_ReleasingAgent):
    """Raises a BaseException ONCE (a lazy relaunch import), then commits."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.attempts = 0
        self._fatal_left = 1

    def commit_memory_session(self, messages=None):
        self.attempts += 1
        if self._fatal_left > 0:
            self._fatal_left -= 1
            raise SystemExit("relaunch")
        self.commits.append(None if messages is None else list(messages))


def _pending_session(session_id, agent, text="remember this", flushed=True):
    """Register a session with one completed (therefore committable) turn."""
    from api.session_lifecycle import mark_turn_completed, register_agent

    messages = [{"role": "user", "content": text}]
    agent._session_messages = list(messages)
    agent._last_flushed_db_idx = len(messages) if flushed else 0
    register_agent(session_id, agent)
    mark_turn_completed(session_id, agent=agent)
    return messages


def test_pressure_release_delivers_the_owner_transcript_and_ignores_the_hook_result():
    """The pre-shed commit hands the provider the REAL transcript, once.

    Two contracts at once, both at the point where they matter (the release):
    the payload is the owner's captured transcript — not ``None``/``[]``, which
    a batch-extraction provider's empty-input guard would skip — and the hook's
    return value (the installed Agent returns ``None`` even when extraction
    worked) neither blocks nor repeats the release.  Fails on the debt-era code,
    which required an acknowledgement the installed Agent can never give and so
    never shed at all.
    """
    from api.session_lifecycle import _reset_for_tests, commit_session_memory

    _reset_for_tests()
    agent = _RecordingCommitAgent(acknowledged=None)
    captured = _pending_session("s1", agent)
    cache = _cache_with([("s1", agent)])
    g = _governor(cache, memory_high_mb=100, protect_recent=0,
                  running_check=lambda k: False)

    assert g.sweep_pressure(rss_mb=500) == 1
    _wait_release(g)

    assert agent.commits == [captured], "the provider did not receive the transcript"
    assert agent._session_messages == []
    # The ledger advanced on that delivery, so the next boundary is a no-op.
    assert commit_session_memory("s1", agent=agent) is False
    assert agent.commits == [captured], "the boundary re-delivered a finished commit"


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


def test_mid_turn_session_is_never_shed_and_is_shed_once_the_turn_ends(caplog):
    """(c) Mid-turn protection is TEMPORARY: both liveness spellings.

    While the turn is live neither spelling may be touched, and the gateway
    warning counts them (mid-turn, not "blocked on un-flushed persistence").
    The moment the lease clears / the registry empties, the next pass sheds the
    same agents — a guard that never releases would wedge the valve, and both
    sessions carry pending work, which is what the debt-era code refused to shed
    at all (it waited for an acknowledgement the installed Agent cannot give).
    """
    from api.session_lifecycle import _reset_for_tests

    _reset_for_tests()
    busy = _RecordingCommitAgent()          # no lease: registry snapshot decides
    leased = _RecordingCommitAgent()        # lease decides
    leased._turn_active = True
    cache = _cache_with([("busy", busy), ("leased", leased)])
    _pending_session("busy", busy)
    _pending_session("leased", leased)

    running = {"busy": True}
    g = _governor(
        cache,
        memory_high_mb=100,
        protect_recent=0,
        running_check=lambda k: running.get(k, False),
    )
    with caplog.at_level(logging.WARNING, logger="api.agent_cache_governance"):
        assert g.sweep_pressure(rss_mb=500) == 0
        _wait_release(g)

    assert cache["busy"][0] is busy and busy._session_messages
    assert cache["leased"][0] is leased and leased._session_messages
    assert busy.commits == [] and leased.commits == []
    assert "(2 cached, 2 mid-turn, 0 blocked on un-flushed persistence)" in caplog.text

    # The turns finish: the same two agents must now be shed on the next pass.
    running["busy"] = False
    leased._turn_active = False
    assert g.sweep_pressure(rss_mb=500) == 2
    _wait_release(g)
    assert busy._session_messages == [] and leased._session_messages == []


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


def test_a_raising_commit_hook_keeps_the_transcript_instead_of_shedding_it(caplog):
    """(d, failure half) A REFUSED commit must not clear the transcript.

    The hook raises (owner scope refused / arity mismatch), so the lifecycle
    ledger stays dirty: that pending extraction has exactly one payload, the
    live transcript.  Clearing it here made the NEXT boundary commit
    ``payload=None`` and mark the extraction done — the memories were silently
    lost.  The entry still leaves the cache (no pass re-delivers to a hook that
    is refusing), the transcript stays resident, and the next boundary delivers
    it for real.
    """
    from api.session_lifecycle import _reset_for_tests, commit_session_memory, has_uncommitted_work

    _reset_for_tests()
    agent = _RaisingCommitAgent()
    captured = _pending_session("k0", agent)
    cache = _cache_with([("k0", agent)])

    g = _governor(cache, memory_high_mb=100, protect_recent=0,
                  running_check=lambda k: False)
    with caplog.at_level(logging.WARNING, logger="api.agent_cache_governance"):
        assert g.sweep_pressure(rss_mb=500) == 1
        _wait_release(g)

    assert agent.attempts == 1, "the batch retried a refusing hook"
    assert agent._session_messages == captured, "a refused commit cleared the transcript"
    assert has_uncommitted_work("k0") is True, "the ledger was advanced by a refused commit"
    assert cache == {}, "the entry must leave the cache (no per-pass re-delivery)"
    assert "still has a pending extraction" in caplog.text

    # A later pass cannot reach the handle, so there is no retry loop...
    assert g.sweep_pressure(rss_mb=500) == 0
    _wait_release(g)
    assert agent.attempts == 1

    # ...and the next boundary now delivers the REAL payload.
    assert commit_session_memory("k0", agent=agent) is True
    assert agent.attempts == 2
    assert agent.commits == [captured], "the boundary commit lost the payload"


def test_base_exception_in_a_commit_does_not_kill_the_worker_or_strand_the_batch():
    """A BaseException must not disable the valve or strand the whole batch.

    Measured on the previous revision: a ``SystemExit`` raised inside a commit
    (a lazy import reaching ``hermes_bootstrap``'s relaunch) killed the single
    long-lived worker and left 4 of 4 claimed transcripts resident and
    unreachable — the gateway's thread-per-batch shape loses only its own
    batch, this one loses the valve for the process's lifetime.
    """
    from api.session_lifecycle import _reset_for_tests

    _reset_for_tests()
    fatal = _FatalCommitAgent()
    others = [_RecordingCommitAgent() for _ in range(3)]
    pairs = [("k0", fatal)] + [(f"k{i}", a) for i, a in enumerate(others, start=1)]
    cache = _cache_with(pairs)
    for key, agent in pairs:
        _pending_session(key, agent)

    g = _governor(cache, memory_high_mb=100, protect_recent=0,
                  running_check=lambda k: False)
    assert g.sweep_pressure(rss_mb=500) == 4
    _wait_release(g)

    worker = g._release_worker._thread
    assert worker is not None and worker.is_alive(), (
        "the single release worker died on a BaseException — the valve is off"
    )
    # The aborted entry keeps its transcript (its payload is still owed)...
    assert fatal.attempts == 1
    assert fatal._session_messages, "the aborted entry lost a transcript it still owes"
    # ...but the rest of the batch is released, not stranded.
    for key, agent in pairs[1:]:
        assert agent._session_messages == [], f"{key} was stranded by another entry's abort"

    # The valve still works on the next pass.
    fresh = _RecordingCommitAgent()
    cache["fresh"] = (fresh, "sig")
    _pending_session("fresh", fresh)
    assert g.sweep_pressure(rss_mb=500) == 1, (
        "the valve stopped shedding after a BaseException"
    )
    _wait_release(g)
    assert fresh._session_messages == []


def test_owner_scope_refusal_keeps_the_transcript_and_no_empty_payload_is_committed(monkeypatch):
    """A REFUSED pre-release commit must not clear the transcript (reviewer repro).

    Measured on the previous revision: the owner scope refused the pre-release
    delivery, the shed still cleared ``_session_messages``, and the NEXT boundary
    then committed ``payload=None`` and advanced the ledger — the extraction the
    transcript existed for was silently marked done with nothing to extract.
    """
    import api.session_lifecycle as lifecycle
    from api.session_lifecycle import _reset_for_tests, commit_session_memory, has_uncommitted_work

    _reset_for_tests()
    agent = _RecordingCommitAgent()
    captured = _pending_session("k0", agent)
    cache = _cache_with([("k0", agent)])

    real_owner_call = lifecycle.call_as_session_owner
    refuse = {"on": True}

    def _maybe_refuse(*args, **kwargs):
        if refuse["on"]:
            return False  # the owner scope refused: the hook never ran
        return real_owner_call(*args, **kwargs)

    monkeypatch.setattr(lifecycle, "call_as_session_owner", _maybe_refuse)

    g = _governor(cache, memory_high_mb=100, protect_recent=0,
                  running_check=lambda k: False)
    assert g.sweep_pressure(rss_mb=500) == 1
    _wait_release(g)

    assert agent.commits == [], "a refused delivery reached the provider"
    assert agent._session_messages == captured, (
        "the refused commit cleared the transcript — its pending extraction had "
        "no payload left"
    )
    assert has_uncommitted_work("k0") is True
    assert cache == {}, "the entry still leaves the cache (no per-pass re-delivery)"

    # The refusal was transient: the next boundary commits the REAL payload.
    refuse["on"] = False
    assert commit_session_memory("k0", agent=agent) is True
    assert agent.commits == [captured], "the boundary commit lost the payload"
    assert None not in agent.commits, "an empty-payload commit was issued"
    assert has_uncommitted_work("k0") is False


def test_dirty_owners_each_receive_their_own_transcript():
    """Owner, generation and payload are selected together (restored from base).

    A rebuild leaves the older handle owning its earlier dirty generation; the
    older owner must receive ITS transcript, not the cached replacement's (the
    re-gate measured both hooks receiving the NEW owner's memory).  The first
    call also proves the post-commit fence: the delivery advanced the ledger, but
    the replacement's own generation was still pending, so its transcript must
    stay.
    """
    from api.session_lifecycle import _reset_for_tests, register_agent

    _reset_for_tests()
    old = _RecordingCommitAgent(flushed=True)
    new = _RecordingCommitAgent(flushed=True)
    old_msgs = _pending_session("s1", old, text="OLD owner memory")
    register_agent("s1", new)
    new_msgs = _pending_session("s1", new, text="NEW owner memory")

    # First call commits the earliest dirty segment (the OLD owner) with the OLD
    # owner's own payload; the newer generation is still pending, so the
    # transcript is kept.
    assert _commit_before_soft_release("s1", new) is False
    assert old.commits == [old_msgs], "the old owner received the wrong payload"
    assert new.commits == []

    # Second call commits the NEW segment with the NEW owner's payload.
    assert _commit_before_soft_release("s1", new) is True
    assert new.commits == [new_msgs]
    assert old.commits == [old_msgs], "the old owner must not be re-committed"


def test_pressure_keeps_a_successor_transcript_while_its_generation_is_pending(caplog):
    """Reviewer repro: an older owner's commit must not free a NEWER transcript.

    STEP1 A's boundary commit is refused, so its transcript is kept and the
    entry leaves the cache with the ledger still owned by A.  STEP2 the next
    turn rebuilds agent B, so the session has segments [(1,1,A), (2,2,B)].
    STEP3 pressure claims B and commits the EARLIEST segment (A's): returning
    "the hook ran" from there cleared B's transcript although generation 2 was
    still pending, and STEP4 the next boundary committed ``payload=None`` and
    marked the ledger clean — B's memories silently marked done with nothing to
    extract.
    """
    from api.session_lifecycle import _reset_for_tests, commit_session_memory, has_uncommitted_work

    _reset_for_tests()
    a = _RaisingCommitAgent()
    a_msgs = _pending_session("s1", a, text="A memory")
    g = _governor(_cache_with([("s1", a)]), memory_high_mb=100, protect_recent=0,
                  running_check=lambda k: False)
    with caplog.at_level(logging.WARNING, logger="api.agent_cache_governance"):
        assert g.sweep_pressure(rss_mb=500) == 1
        _wait_release(g)

    # STEP1: refused, so the payload stays and the ledger is still A's.
    assert a.attempts == 1
    assert a._session_messages == a_msgs
    assert has_uncommitted_work("s1") is True

    # STEP2: the next turn rebuilds the agent; generation 2 belongs to B.
    b = _RecordingCommitAgent()
    b_msgs = _pending_session("s1", b, text="B memory")
    cache = _cache_with([("s1", b)])
    g2 = _governor(cache, memory_high_mb=100, protect_recent=0,
                   running_check=lambda k: False)

    # STEP3: the shed commits ONLY A's earliest segment.
    with caplog.at_level(logging.WARNING, logger="api.agent_cache_governance"):
        assert g2.sweep_pressure(rss_mb=500) == 1
        _wait_release(g2)

    assert a.commits == [a_msgs], "the older owner did not receive its own payload"
    assert b._session_messages == b_msgs, (
        "an older owner's commit freed a transcript whose own generation is "
        "still pending — the next boundary would commit payload=None"
    )
    assert has_uncommitted_work("s1") is True
    assert cache == {}, "the entry still leaves the cache (no per-pass re-delivery)"
    assert "still has a pending extraction" in caplog.text

    # STEP4: the next boundary delivers B's REAL payload, never an empty one.
    assert commit_session_memory("s1", agent=b) is True
    assert b.commits == [b_msgs]
    assert None not in b.commits, "an empty-payload commit was issued"
    assert has_uncommitted_work("s1") is False


def test_a_base_exception_releases_the_in_flight_claim_and_the_next_commit_works(caplog):
    """An abandoned commit must not wedge the session's in-flight claim.

    ``commit_session_memory`` clears ``in_flight`` on its normal and refused
    paths only; a ``SystemExit`` from a lazy relaunch import unwinds past both,
    so the claim stayed set for the life of the process: every later
    ``commit_session_memory(wait=True)`` blocked on it, ``discard_session``
    refused, and the provider client stayed open (measured by the reviewer).
    """
    from api.session_lifecycle import _reset_for_tests, commit_session_memory, has_uncommitted_work

    _reset_for_tests()
    agent = _FatalOnceCommitAgent()
    captured = _pending_session("k0", agent)
    cache = _cache_with([("k0", agent)])
    g = _governor(cache, memory_high_mb=100, protect_recent=0,
                  running_check=lambda k: False)

    with caplog.at_level(logging.WARNING, logger="api.agent_cache_governance"):
        assert g.sweep_pressure(rss_mb=500) == 1
        _wait_release(g)

    # The abandonment is logged, with the session and the reason.
    assert "session=k0 aborted by SystemExit" in caplog.text
    assert "transcript KEPT" in caplog.text
    # …and it actually happened: clients released, transcript kept for the
    # extraction that is still pending.
    assert agent.releases == 1, "the abandoned entry kept its provider client open"
    assert agent._session_messages == captured, (
        "the aborted entry lost the payload its pending extraction needs"
    )
    assert has_uncommitted_work("k0") is True

    # The claim was handed back: the next commit does not block and delivers.
    started = time.monotonic()
    assert commit_session_memory("k0", agent=agent, wait=True, timeout=5.0) is True
    assert time.monotonic() - started < 2.0, (
        "the in-flight claim was never released: the commit waited for a "
        "delivery that had already been abandoned"
    )
    assert agent.commits == [captured]
    assert has_uncommitted_work("k0") is False


class _FakeMemoryProvider:
    """The provider object ``AIAgent.release_clients()`` deliberately KEEPS.

    ``release_clients`` frees LLM clients and leaves session tool state — the
    memory provider included — in place so the session can resume.  A bytearray
    makes the retention measurable and lets a weakref prove collectability.
    """

    def __init__(self, mb=32):
        self.buffer = bytearray(mb << 20)


class _ProviderHoldingAgent(_ReleasingAgent):
    """A releasing agent that also carries a provider (as AIAgent does)."""

    def __init__(self, mb=32, **kw):
        super().__init__(**kw)
        self.memory_provider = _FakeMemoryProvider(mb)


def test_a_shed_agent_is_not_retained_by_the_lifecycle_ledger():
    """The ledger must not keep the shed agent (and its provider) alive.

    ``release_clients()`` keeps the memory provider on purpose, and the claim
    has already popped the entry out of the cache — so the lifecycle entry's
    ``agent`` slot was the LAST strong reference.  Nothing could ever reach it
    again (the idle sweep only walks the cache, the next turn builds a different
    agent), so the provider lived for the rest of the process: measured 32.0 MB
    retained before this fix (32.0 MB -> 32.1 MB across the shed, i.e. nothing
    freed) and 32.0 MB -> 0.1 MB after it (31.9 MB freed).
    """
    import gc
    import tracemalloc
    import weakref

    # A running server always has api.streaming imported (server.py, every turn
    # path).  Import it BEFORE the sweep so this asserts the ledger reference and
    # not the one-off cost of the worker's first import of that module.
    import api.streaming  # noqa: F401

    from api.session_lifecycle import _reset_for_tests, _sessions, commit_session_memory

    _reset_for_tests()
    tracemalloc.start()
    try:
        agent = _ProviderHoldingAgent()
        captured = _pending_session("s1", agent)
        agent_ref = weakref.ref(agent)
        provider_ref = weakref.ref(agent.memory_provider)
        cache = _cache_with([("s1", agent)])

        g = _governor(cache, memory_high_mb=100, protect_recent=0,
                      running_check=lambda k: False)
        gc.collect()
        before = tracemalloc.get_traced_memory()[0]
        assert g.sweep_pressure(rss_mb=500) == 1
        _wait_release(g)
        assert agent.commits == [captured], "the pre-release commit did not run"
        del agent
        gc.collect()
        after = tracemalloc.get_traced_memory()[0]
    finally:
        tracemalloc.stop()

    assert agent_ref() is None, (
        "the lifecycle ledger still holds the shed agent: nothing can reach it "
        "again, so its provider is pinned for the life of the process"
    )
    assert provider_ref() is None, (
        "the memory provider release_clients() keeps was never collectable"
    )
    assert after <= before - (24 << 20), (
        f"the 32 MB provider was not freed: before={before} after={after}"
    )

    # The ENTRY and its ledger stay: the next turn re-attaches instead of
    # starting a fresh generation, and the committed work is not re-delivered.
    entry = _sessions["s1"]
    assert entry["agent"] is None
    assert entry["generation"] == entry["committed_generation"] == 1
    assert entry["segments"] == [], "the committed segment kept its owner"
    assert commit_session_memory("s1") is False, "the ledger re-opened finished work"

    # Next turn: a DIFFERENT agent on the same entry, rebuilt transcript, and
    # the commit/shed path still works end to end.
    rebuilt = _ProviderHoldingAgent()
    rebuilt_messages = _pending_session("s1", rebuilt, text="next turn")
    assert _sessions["s1"]["agent"] is rebuilt
    assert _sessions["s1"]["generation"] == 2
    cache["s1"] = (rebuilt, "sig")
    assert g.sweep_pressure(rss_mb=500) == 1
    _wait_release(g)

    assert rebuilt.commits == [rebuilt_messages], "the rebuilt agent's commit path broke"
    assert rebuilt._session_messages == []
    assert _sessions["s1"]["committed_generation"] == 2
    assert _sessions["s1"]["agent"] is None


def test_an_uncommitted_segment_owner_is_never_forgotten():
    """Only MEMORY is dropped: a dirty owner is the retry payload's only handle.

    The release path refuses to shed while work is pending, so this cannot be
    reached from the valve — it is the assertion that the helper does not clear
    a reference the ledger still needs.
    """
    from api.session_lifecycle import (
        _reset_for_tests,
        _sessions,
        forget_released_agent,
        mark_turn_completed,
        register_agent,
    )

    _reset_for_tests()
    agent = _FakeAgent()
    register_agent("s2", agent)
    mark_turn_completed("s2", agent=agent)  # commit is pending from here on

    assert forget_released_agent("s2", agent) is False
    assert _sessions["s2"]["agent"] is agent
    assert _sessions["s2"]["segments"][0]["agent"] is agent


def test_soft_release_releases_clients_before_dropping_the_transcript():
    """(gateway/run_agent_cache.py:911) release_clients() before the transcript.

    Every shed session used to keep its provider client open for the life of the
    process: the entry is gone from the cache, so the idle-TTL teardown can never
    reach the handle (measured 0 client releases / 0 provider shutdowns for shed
    agents).  Order matters too: the memory commit needs the live manager, and
    the client release needs the transcript still present for the legacy
    fallback.
    """
    from api.session_lifecycle import _reset_for_tests

    _reset_for_tests()
    agent = _ReleasingAgent()
    captured = _pending_session("k0", agent)
    cache = _cache_with([("k0", agent)])

    g = _governor(cache, memory_high_mb=100, protect_recent=0,
                  running_check=lambda k: False)
    assert g.sweep_pressure(rss_mb=500) == 1
    _wait_release(g)

    assert agent.releases == 1, "a shed agent kept its LLM clients open"
    assert agent.commits_at_release == 1, "clients were released before the commit"
    assert agent.transcript_len_at_release == len(captured), (
        "clients must be released while the transcript is still present"
    )
    assert agent._session_messages == []
    assert agent.commits == [captured], "no commit after the release"

    # No second release, and the transcript is rebuilt by the next turn.
    assert g.sweep_pressure(rss_mb=500) == 0
    _wait_release(g)
    assert agent.releases == 1, "the released handle was released again"


def test_soft_release_falls_back_to_the_legacy_close():
    """No release_clients(): the gateway's full-close fallback, transcript first."""
    agent = _LegacyAgent()
    assert not hasattr(agent, "release_clients")
    soft_release_transcript(agent)
    assert agent.shutdowns == [[{"role": "user", "content": "x" * 1000}]], (
        "the legacy fallback did not drain the memory provider with the transcript"
    )
    assert agent.closes == 1
    assert agent._session_messages == []


def test_released_handle_reinserted_in_the_cache_is_not_reclaimed():
    """The dropped ``_agent_has_resident_transcript`` guard, restored.

    Streaming re-inserts the agent it fetched, so a released handle comes back
    into the cache with an empty transcript — which is trivially "caught up"
    (``0 >= 0``).  Without the guard every later pass re-claims it (measured
    5 of 5), wasting an eviction slot on entries that hold no memory and issuing
    a no-payload ``commit_memory_session()`` for a transcript that no longer
    exists.
    """
    from api.session_lifecycle import _reset_for_tests

    _reset_for_tests()
    agent = _RecordingCommitAgent()
    captured = _pending_session("k0", agent)
    cache = _cache_with([("k0", agent)])

    g = _governor(cache, memory_high_mb=100, protect_recent=0,
                  running_check=lambda k: False)
    assert g.sweep_pressure(rss_mb=500) == 1
    _wait_release(g)

    cache["k0"] = (agent, "sig")  # the next turn re-inserts the fetched handle
    for _ in range(5):
        assert g.sweep_pressure(rss_mb=500) == 0, "a released handle was re-claimed"
        _wait_release(g)

    assert agent.commits == [captured], (
        f"a released handle issued {len(agent.commits) - 1} extra commit(s)"
    )
    assert agent._session_messages == [], "the next turn has not rebuilt the transcript yet"


def test_twelve_over_budget_passes_touch_only_the_flushed_session(caplog):
    """(e) 12 over-budget passes: 0 provider calls for the un-flushed session.

    The mixed cache is the point.  The un-flushed session must cost the
    provider nothing at all over 12 passes (that transcript is the only
    complete copy: it is the gate, not the commit, that keeps it) and it must be
    counted in the warning each time.  Its flushed neighbour, in the SAME cache,
    must be shed exactly once and then never delivered again — the debt-era code
    re-delivered a commit on every single pass (12 calls) because it waited for
    an acknowledgement the installed Agent cannot give.
    """
    from api.session_lifecycle import _reset_for_tests

    _reset_for_tests()
    dirty = _RecordingCommitAgent()
    clean = _RecordingCommitAgent()
    cache = _cache_with([("dirty", dirty), ("clean", clean)])
    _pending_session("dirty", dirty, flushed=False)
    captured = _pending_session("clean", clean, flushed=True)

    g = _governor(cache, memory_high_mb=100, protect_recent=0,
                  running_check=lambda k: False)
    claims = []
    with caplog.at_level(logging.WARNING, logger="api.agent_cache_governance"):
        for _ in range(12):
            claims.append(g.sweep_pressure(rss_mb=500))
            _wait_release(g)

    assert claims == [1] + [0] * 11, f"unexpected claims across 12 passes: {claims}"
    assert dirty.commits == [], "an un-flushed session must never reach the provider"
    assert dirty._session_messages, "the un-flushed transcript was dropped"
    assert cache["dirty"][0] is dirty
    assert "1 blocked on un-flushed persistence" in caplog.text
    assert clean.commits == [captured], (
        f"the flushed session was delivered {len(clean.commits)} times, expected 1"
    )
    assert clean._session_messages == []
    assert "clean" not in cache


def test_pressure_keeps_one_batch_in_flight_and_logs_the_backlog(caplog):
    """B1: a slow drain must not let the valve queue pinned transcripts.

    The pass claims by popping entries OUT of the cache, so every queued batch
    is a transcript still resident while the log says it was shed.  Measured on
    the previous revision with a slow drain: depth 1 -> 10, then 4 -> 41 pinned
    transcripts over 12 over-budget passes.  Now: one batch in flight, the skip
    logged with the observed backlog, and every claimed transcript released
    exactly once when the drain finishes.
    """
    from api.session_lifecycle import _reset_for_tests

    _reset_for_tests()
    gate, started = threading.Event(), threading.Event()
    agents = [(f"k{i}", _GatedCommitAgent(gate, started)) for i in range(20)]
    cache = _cache_with(agents)
    for key, agent in agents:
        _pending_session(key, agent)

    g = _governor(cache, memory_high_mb=100, protect_recent=0,
                  running_check=lambda k: False)

    claims = []
    with caplog.at_level(logging.INFO, logger="api.agent_cache_governance"):
        claims.append(g.sweep_pressure(rss_mb=500))
        assert started.wait(10), "the release worker never started the batch"
        for _ in range(11):
            claims.append(g.sweep_pressure(rss_mb=500))
        backlog = g._release_worker.backlog()
        assert backlog["batches"] == 1, f"batches in flight: {backlog}"
        assert backlog["transcripts"] <= 16, f"transcripts pinned by the queue: {backlog}"
        assert backlog["depth"] <= 1, f"queue depth grew past one batch: {backlog}"
        gate.set()
        _wait_release(g)

    assert claims == [16] + [0] * 11, f"claims across 12 passes: {claims}"
    assert "still pinned by the release worker" in caplog.text
    assert "queue depth" in caplog.text
    # Nothing was lost and nothing was duplicated: 16 released once each, the
    # 4 beyond the per-pass cap still resident and still cached.
    for key, agent in agents[:16]:
        assert agent._session_messages == [], f"{key} was claimed but never released"
        assert len(agent.commits) == 1, f"{key} was committed {len(agent.commits)} times"
    assert set(cache) == {f"k{i}" for i in range(16, 20)}, (
        f"only the per-pass cap may be claimed, still cached: {sorted(cache)}"
    )
    for key, agent in agents[16:]:
        assert agent._session_messages, f"{key} was claimed past the per-pass cap"


def test_pressure_sheds_on_the_pass_after_a_busy_one():
    """B1: skipping a pass must not disable the valve — the next one sheds."""
    from api.session_lifecycle import _reset_for_tests

    _reset_for_tests()
    gate, started = threading.Event(), threading.Event()
    slow = _GatedCommitAgent(gate, started)
    cache = _cache_with([("slow", slow)])
    _pending_session("slow", slow)

    g = _governor(cache, memory_high_mb=100, protect_recent=0,
                  running_check=lambda k: False)

    assert g.sweep_pressure(rss_mb=500) == 1
    assert started.wait(10)
    # A second entry appears while the first batch is still draining.
    later = _RecordingCommitAgent()
    cache["later"] = (later, "sig")
    _pending_session("later", later)
    assert g.sweep_pressure(rss_mb=500) == 0, "a second batch was queued"
    assert later._session_messages, "the skipped pass claimed the new entry anyway"

    gate.set()
    _wait_release(g)
    assert slow._session_messages == []

    # The backlog is gone: the very next pass sheds the entry it skipped.
    assert g.sweep_pressure(rss_mb=500) == 1
    _wait_release(g)
    assert later._session_messages == []


def test_warning_counts_mid_turn_sessions_once(caplog):
    """B4: the gateway's buckets are exclusive (mid-turn is NOT also un-flushed).

    Same state in the gateway prints "0 blocked on un-flushed persistence"; this
    port printed 1 because a mid-turn session was counted in both buckets, which
    sends an operator hunting a session-persistence failure when the memory is
    simply pinned by a running turn.
    """
    busy = _FakeAgent(flushed=False, turn_active=True)  # mid-turn AND lagging
    stale = _FakeAgent(flushed=False)                   # idle and lagging
    cache = _cache_with([("busy", busy), ("stale", stale)])

    g = _governor(cache, memory_high_mb=100, protect_recent=0)
    with caplog.at_level(logging.WARNING, logger="api.agent_cache_governance"):
        assert g.sweep_pressure(rss_mb=500) == 0
        _wait_release(g)

    assert "(2 cached, 1 mid-turn, 1 blocked on un-flushed persistence)" in caplog.text


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
