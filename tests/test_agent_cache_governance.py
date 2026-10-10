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

    unregister_active_run() pops the row under ACTIVE_RUNS_LOCK and then
    writes the lease after releasing the lock.  If a successor stream
    registers in that gap, the finishing stream's late ``False`` overwrites
    the successor's ``True`` — the governor then reads a live turn as idle
    and can evict the agent mid-turn.

    Deterministic interleaving: hook the ACTIVE_RUNS_LOCK release so that the
    successor registers exactly between the finishing stream's row pop and
    its lease write.
    """
    import api.config as config
    import threading
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


def test_compression_rotation_updates_run_row_session_id(monkeypatch):
    """When compression rotates the session id, the ACTIVE_RUNS row must
    follow so the final unregister clears the lease under the NEW id.

    The cache key moves from old_sid to new_sid mid-turn (streaming.py
    moves the entry).  If the registry row keeps old_sid, the final
    unregister_active_run clears the lease under old_sid — which no longer
    holds a cache entry — and the agent under new_sid keeps ``_turn_active``
    True forever, so both governor passes skip it permanently.

    Two halves:
    1. config-layer contract: update_active_run rewrites the row's session
       id (the row is keyed by stream id, so the update is unconditional).
    2. wiring contract: the rotation site in streaming.py carries the
       ``update_active_run(stream_id, session_id=new_sid)`` call (static
       AST check — the codebase has no runtime harness for this private
       worker block, so the assertion pins the exact wiring to prevent it
       being dropped in a future refactor).
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

    # Rotation: update the row's session id to the new cache key.
    config.update_active_run("stream-1", session_id="new-sid")

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
                    if _kw.get("session_id") == "new_sid":
                        _rotation_wired = True
    assert _rotation_wired, (
        "streaming.py rotation block must call "
        "update_active_run(stream_id, session_id=new_sid)"
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
    work → True (release proceeds); pending work → delegates to the streaming
    lifecycle commit with the captured agent."""
    import api.agent_cache_governance as gmod
    import api.streaming as streaming

    agent = _FakeAgent(flushed=True)
    seen = {}

    monkeypatch.setattr(streaming, "_lifecycle_has_uncommitted_work", lambda sid: True)

    def _fake_commit(session_id, agent=None, wait=False):
        seen["sid"] = session_id
        seen["agent"] = agent
        seen["wait"] = wait
        return True

    monkeypatch.setattr(streaming, "_lifecycle_commit_session_memory", _fake_commit)

    assert gmod._commit_pending_memory("s1", agent) is True
    assert seen == {"sid": "s1", "agent": agent, "wait": False}

    monkeypatch.setattr(streaming, "_lifecycle_has_uncommitted_work", lambda sid: False)
    seen.clear()
    assert gmod._commit_pending_memory("s1", agent) is True
    assert seen == {}  # nothing pending — no commit call needed
