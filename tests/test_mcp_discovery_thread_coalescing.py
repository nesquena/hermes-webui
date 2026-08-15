"""Runtime regression tests for the WebUI's profile-scoped MCP readiness.

Exercises the REAL production path — `api.streaming._ensure_mcp_discovery` /
`_mcp_wait_readiness` / `_mcp_retry_discovery` / `_wait_and_surface_mcp_readiness`
— with fake discovery payloads.  These map 1:1 onto the readiness contract the
maintainer review demanded:

1. A discovery that finishes AFTER any fixed-timeout guess (e.g. 4s) must
   still be present for the first tool-bearing turn — the turn waits on the
   shared readiness event, not a timed join.
2. Several same-profile turns while one discovery is pending must share ONE
   owner thread and resolve on the SAME event, not each pay a fresh wait.
3. Different profiles keep independent readiness/tool registries.
4. Failure has an explicit surfaced outcome: a thrown run becomes 'failed',
   a PRODUCTION-style closure returning False becomes 'failed' (closures no
   longer swallow failures into a fake 'completed'), and the outcome is
   surfaced at the stream boundary before the agent snapshot is built.
5. Readiness is generation-based and single-flight: a timed-out wait RETIRES
   the run so a late-finishing owner can never flip state (or register tools
   into an in-flight turn), and a retry while the prior owner is still
   pending never leaves two live owners for one profile.
"""
import sys
import threading
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from api import streaming  # noqa: E402


def _make_discover(duration: float, fail: bool = False):
    """Return (discover_fn, started_flag).  Raises on failure."""
    started = threading.Event()

    def _discover():
        started.set()
        if fail:
            time.sleep(duration or 0)
            raise RuntimeError("simulated connect failure")
        if duration:
            time.sleep(duration)
        return True

    return _discover, started


def _make_production_discover(duration: float, fail: bool = False):
    """Return (discover_fn, started_flag) shaped like the PRODUCTION closures.

    `_discover_mcp_background` / `_discover_default` catch their own
    exceptions and return an explicit bool — they never raise.  The
    readiness runner must record a False return as 'failed'; this is the
    exact path the maintainer review found broken when closures swallowed
    failures into a fake 'completed'.
    """
    started = threading.Event()

    def _discover():
        try:
            started.set()
            if duration:
                time.sleep(duration)
            if fail:
                return False
            return True
        except Exception:
            return False

    return _discover, started


class TestFirstTurnCompleteness:
    def test_turn_waits_for_discovery_past_old_4s_boundary(self):
        """A server connecting at 4.5s must not be omitted from the first turn.

        The old bounded-join design cut off at a fixed 4.0s; the readiness
        contract waits on the shared event until discovery actually
        completes, so the agent snapshot that follows sees the tools.
        """
        discover, started = _make_discover(4.5)
        readiness = streaming._ensure_mcp_discovery(
            "profile-a", discover, "t-slow"
        )
        t0 = time.monotonic()
        status = streaming._mcp_wait_readiness(readiness)
        elapsed = time.monotonic() - t0
        assert started.is_set()
        assert status == "completed"
        assert elapsed >= 4.5, (
            f"readiness resolved before discovery finished: {elapsed:.2f}s"
        )

    def test_completed_readiness_does_not_wait(self):
        """A resolved profile returns immediately — no per-turn wait."""
        discover, started = _make_discover(0.05)
        readiness = streaming._ensure_mcp_discovery(
            "profile-a2", discover, "t-fast"
        )
        assert streaming._mcp_wait_readiness(readiness) == "completed"
        t0 = time.monotonic()
        assert streaming._mcp_wait_readiness(readiness) == "completed"
        assert time.monotonic() - t0 < 0.2, (
            "a completed profile must never make the turn wait again"
        )


class TestSharedReadiness:
    def test_same_profile_turns_share_one_thread_and_one_wait(self):
        """Concurrent same-profile turns: ONE thread, ONE shared wait."""
        discover, started = _make_discover(1.5)
        r1 = streaming._ensure_mcp_discovery("profile-b", discover, "t1")
        r2 = streaming._ensure_mcp_discovery("profile-b", discover, "t2")
        assert r1.thread is r2.thread, "both turns must share the owner thread"
        t0 = time.monotonic()
        s1 = streaming._mcp_wait_readiness(r1)
        s2 = streaming._mcp_wait_readiness(r2)
        elapsed = time.monotonic() - t0
        assert s1 == s2 == "completed"
        assert elapsed < 3.0, (
            f"two turns paid ~2x the discovery wait: {elapsed:.2f}s"
        )

    def test_completed_profile_is_not_re_discovered(self):
        """A completed profile must not be re-run by a later turn."""
        runs = []

        def _discover():
            runs.append(1)
            return True

        r = streaming._ensure_mcp_discovery("profile-b2", _discover, "t")
        assert streaming._mcp_wait_readiness(r) == "completed"
        streaming._ensure_mcp_discovery("profile-b2", _discover, "t2")
        assert len(runs) == 1, "completed profile must not re-run discovery"


class TestProfileIsolation:
    def test_different_profiles_independent(self):
        """Different profiles keep independent readiness and threads."""
        d1, s1 = _make_discover(0.3)
        d2, s2 = _make_discover(0.6)
        ra = streaming._ensure_mcp_discovery("profile-x", d1, "tx")
        rb = streaming._ensure_mcp_discovery("profile-y", d2, "ty")
        assert ra is not rb
        assert ra.thread is not rb.thread
        assert streaming._mcp_wait_readiness(ra) == "completed"
        assert streaming._mcp_wait_readiness(rb) == "completed"


class TestFailureAndRetry:
    def test_thrown_discovery_becomes_failed(self):
        """A discovery that RAISES must surface status='failed'."""
        discover, started = _make_discover(0.1, fail=True)
        readiness = streaming._ensure_mcp_discovery("profile-z", discover, "tz")
        assert streaming._mcp_wait_readiness(readiness) == "failed"

    def test_closure_returning_false_becomes_failed(self):
        """A PRODUCTION-style closure returning False must surface 'failed'.

        Regression (maintainer review): the production closures used to
        swallow exceptions and return None, so the runner recorded every
        failed run as 'completed'.  The closures now return an explicit
        bool and the runner must honor a False outcome.
        """
        discover, started = _make_production_discover(0.05, fail=True)
        readiness = streaming._ensure_mcp_discovery(
            "profile-z2", discover, "tz2"
        )
        assert streaming._mcp_wait_readiness(readiness) == "failed"

    def test_closure_returning_true_completes(self):
        """A production-style closure returning True completes."""
        discover, started = _make_production_discover(0.05)
        readiness = streaming._ensure_mcp_discovery(
            "profile-z3", discover, "tz3"
        )
        assert streaming._mcp_wait_readiness(readiness) == "completed"

    def test_explicit_retry_after_failure_completes(self):
        """An explicit retry re-runs discovery and completes."""
        fail_disc, _ = _make_discover(0.05, fail=True)
        r = streaming._ensure_mcp_discovery("profile-w", fail_disc, "tw1")
        assert streaming._mcp_wait_readiness(r) == "failed"
        old_thread = r.thread
        ok_disc, ok_started = _make_discover(0.05)
        r2 = streaming._mcp_retry_discovery("profile-w", ok_disc, "tw2")
        assert r2.thread is not old_thread, "retry must run a fresh owner thread"
        assert streaming._mcp_wait_readiness(r2) == "completed"
        assert ok_started.is_set()


class TestGenerations:
    def test_timeout_retires_generation_and_cannot_flip_later(self):
        """A timed-out wait retires the run; a late finish cannot flip state.

        Regression (maintainer review): `_mcp_wait_readiness` used to set
        'failed' after the cap without retiring the owner, so the thread
        could later finish and overwrite the terminal outcome to
        'completed' after the caller had already proceeded.  Round 9:
        the thread POINTER is retained (execution continues) but the
        generation is fenced so a late finish can never publish.
        """
        _old_cap = streaming._MCP_READINESS_WAIT_CAP_S
        streaming._MCP_READINESS_WAIT_CAP_S = 0.15
        try:
            discover, started = _make_discover(0.6)  # finishes AFTER the cap
            readiness = streaming._ensure_mcp_discovery(
                "profile-g", discover, "tg"
            )
            assert streaming._mcp_wait_readiness(readiness) == "failed"
            assert readiness.thread is not None, (
                "the thread pointer must not be cleared while execution "
                "continues (maintainer review round 9)"
            )
            time.sleep(0.7)  # let the retired thread actually finish
            assert readiness.status == "failed", (
                "a late-finishing retired generation must never publish "
                "terminal state over a timed-out wait"
            )
        finally:
            streaming._MCP_READINESS_WAIT_CAP_S = _old_cap

    def test_runner_skips_discovery_when_cancelled(self):
        """A cancelled runner performs NO discovery side effects.

        Regression (maintainer review round 9): generation-fencing only
        protected the final label — a retired body could still call
        discover_mcp_tools() and mutate the process-global MCP registry.
        The runner now checks the cancel token BEFORE discovery, so a run
        retired by timeout or superseded by retry that has not started
        does no work.
        """
        calls = []

        def discover():
            calls.append("discover")
            return True

        readiness = streaming._McpReadiness()
        readiness.cancel.set()
        event = threading.Event()
        streaming._discovery_runner(
            discover, readiness, event, readiness.gen, readiness.cancel
        )
        assert calls == [], "cancelled runner must not call discover_fn"
        assert readiness.status == "pending"
        assert event.is_set()

    def test_retry_joins_old_body_before_replacement(self):
        """Retry while the prior owner runs must JOIN it first — never overlap.

        Regression (maintainer review round 9): retry used to bump the
        generation and start a replacement immediately, leaving BOTH
        discovery bodies alive and mutating the global registry.  Retry
        now cancels and joins the old body (bounded) before starting the
        new one — at most one discovery body is ever alive per profile.
        """
        timeline = []
        lock = threading.Lock()

        def make_logging_discover(duration, label):
            def _discover():
                with lock:
                    timeline.append((label + "-start", time.monotonic()))
                time.sleep(duration)
                with lock:
                    timeline.append((label + "-end", time.monotonic()))
                return True

            return _discover

        slow = make_logging_discover(0.3, "old")
        r = streaming._ensure_mcp_discovery("profile-p", slow, "tp1")
        time.sleep(0.1)  # old body is running
        fast = make_logging_discover(0.05, "new")
        r2 = streaming._mcp_retry_discovery("profile-p", fast, "tp2")
        assert streaming._mcp_wait_readiness(r2) == "completed"
        with lock:
            old_end = max(t for ev, t in timeline if ev == "old-end")
            new_start = min(t for ev, t in timeline if ev == "new-start")
        assert new_start >= old_end - 0.01, (
            "the replacement body started before the old body finished — "
            "two discovery bodies were alive for one profile"
        )

    def test_canonical_key_startup_and_default_turn_share_entry(self, monkeypatch):
        """Startup and a default-profile turn must share ONE readiness entry.

        Regression (maintainer review round 9): startup keyed readiness
        as '' while real default turns keyed by the resolved home path,
        so the logical default profile held two entries and a global
        reload refreshed only one.
        """
        import sys
        from types import ModuleType

        from api import profiles

        class _FakeOverride:
            def set_hermes_home_override(self, home):
                return "tok"

            def reset_hermes_home_override(self, token):
                pass

            def get_default_hermes_root(self):
                return "/default/hermes/home"

        monkeypatch.setattr(
            profiles, "_resolve_hermes_home_override", lambda: _FakeOverride()
        )

        # Fake tools.mcp_tool so _startup_mcp_discovery can import it.
        tools_pkg = ModuleType("tools")
        tools_pkg.__path__ = []
        mcp_tool = ModuleType("tools.mcp_tool")
        mcp_tool.discover_mcp_tools = lambda: []
        monkeypatch.setitem(sys.modules, "tools", tools_pkg)
        monkeypatch.setitem(sys.modules, "tools.mcp_tool", mcp_tool)

        streaming._startup_mcp_discovery()
        r_startup = streaming._MCP_READINESS.get("/default/hermes/home")
        assert r_startup is not None, (
            "startup must key readiness by the canonical resolved default "
            "home, not ''"
        )
        assert streaming._MCP_READINESS.get("") is None, (
            "the '' key must never coexist with the canonical default key"
        )
        # A default-profile turn ('') resolves to the same canonical key.
        turn_disc, turn_started = _make_discover(0.05)
        r_turn = streaming._ensure_mcp_discovery("", turn_disc, "t-turn")
        assert r_turn is r_startup, (
            "startup and a default turn must share ONE readiness entry"
        )
        assert streaming._mcp_wait_readiness(r_turn) == "completed"

    def test_owner_creation_waits_for_reload_teardown(self):
        """A concurrent stream cannot create an owner during reload teardown.

        Regression (Greptile review round 11): the reload validated only
        the owners it SNAPSHOTTED.  A stream worker creating a new owner
        during the join phase could register servers into (or after) the
        registry shutdown — overlapping teardown even though every
        snapshot owner terminated.  Owner creation now blocks on
        `_MCP_RELOAD_FENCE` for the reload's teardown window, so the
        reload's snapshot is complete and a new body can only start
        against the fresh registry.
        """
        timeline = []
        lock = threading.Lock()

        def make_logging_discover(duration, label):
            def _discover():
                with lock:
                    timeline.append((label + "-start", time.monotonic()))
                time.sleep(duration)
                with lock:
                    timeline.append((label + "-end", time.monotonic()))
                return True

            return _discover

        # A live owner that blocks until released (simulates a discovery
        # body mid-connect during the reload's join phase).
        zombie_readiness = streaming._McpReadiness()
        release = threading.Event()

        def zombie_discover():
            with lock:
                timeline.append(("zombie-start", time.monotonic()))
            release.wait(5.0)
            with lock:
                timeline.append(("zombie-end", time.monotonic()))
            return True

        zt = threading.Thread(
            target=streaming._discovery_runner,
            args=(
                zombie_discover,
                zombie_readiness,
                zombie_readiness.event,
                zombie_readiness.gen,
                zombie_readiness.cancel,
            ),
            name="mcp-fence-test-zombie",
            daemon=True,
        )
        zombie_readiness.thread = zt
        zt.start()
        streaming._MCP_READINESS["/profiles/zombie"] = zombie_readiness

        reload_done = threading.Event()

        def _do_prepare():
            streaming._prepare_global_reload()
            reload_done.set()

        # Hold the reload fence exactly like /reload-mcp does across
        # snapshot + join + shutdown.
        with streaming._MCP_RELOAD_FENCE:
            rt = threading.Thread(target=_do_prepare, daemon=True)
            rt.start()
            # Prepare has set the zombie's cancel token → it is inside the
            # bounded join, still waiting on the zombie.
            assert zombie_readiness.cancel.wait(2.0), "prepare never started join"
            time.sleep(0.1)

            # A stream worker for a NEW profile tries to create an owner
            # mid-teardown.  It must BLOCK on the fence, not create a
            # discovery body that overlaps the shutdown.
            ensure_done = threading.Event()
            ensure_holder = {}

            def _do_ensure():
                ensure_holder["readiness"] = streaming._ensure_mcp_discovery(
                    "profile-new", make_logging_discover(0.05, "new"), "t-new"
                )
                ensure_done.set()

            et = threading.Thread(target=_do_ensure, daemon=True)
            et.start()
            time.sleep(0.3)
            assert "profile-new" not in streaming._MCP_READINESS, (
                "owner creation must be fenced during the reload teardown"
            )
            assert not ensure_done.is_set(), (
                "_ensure_mcp_discovery returned while the reload fence was held"
            )

            # Let the zombie finish → prepare's join returns (owner
            # terminated) → teardown completes.
            release.set()
            rt.join(timeout=2.0)
            assert reload_done.is_set(), "prepare did not finish"

        # Fence released: the fenced creator may now create its owner.
        et.join(timeout=2.0)
        new_readiness = ensure_holder.get("readiness")
        assert new_readiness is not None, "fenced owner creation never ran"
        assert streaming._mcp_wait_readiness(new_readiness) == "completed"

        # No overlap: the new body started only AFTER the zombie ended.
        with lock:
            zombie_end = max(t for ev, t in timeline if ev == "zombie-end")
            new_start = min(t for ev, t in timeline if ev == "new-start")
        assert new_start >= zombie_end - 0.01, (
            "the fenced owner started discovery while the reload teardown "
            "was still in progress"
        )

    def test_retry_while_pending_never_creates_two_owners(self):
        """A retry while the prior owner is still running must be single-flight.

        Regression (maintainer review): `_mcp_retry_discovery` used to
        spawn a new daemon unconditionally, so a retry while the previous
        owner was still pending left TWO live discovery threads for one
        profile.  Retry now retires the old generation first.
        """
        slow_disc, slow_started = _make_discover(0.4)
        r = streaming._ensure_mcp_discovery("profile-r", slow_disc, "tr1")
        assert r.status == "pending"
        old_thread = r.thread
        assert old_thread is not None and old_thread.is_alive()
        ok_disc, ok_started = _make_discover(0.05)
        r2 = streaming._mcp_retry_discovery("profile-r", ok_disc, "tr2")
        assert r2.thread is not old_thread, "retry must run a fresh owner"
        assert r2.thread is not None and r2.thread.is_alive()
        assert streaming._mcp_wait_readiness(r2) == "completed"
        # The retired slow owner must finish WITHOUT flipping the outcome.
        time.sleep(0.5)
        assert r2.status == "completed", "retired generation must not publish"
        # The registry still holds exactly ONE current owner for the profile.
        assert streaming._MCP_READINESS["profile-r"].thread is r2.thread


class TestStreamBoundarySurfacing:
    def test_failed_status_surfaced_at_stream_boundary(self, caplog):
        """A failed readiness must be surfaced before the agent snapshot."""
        discover, started = _make_discover(0.05, fail=True)
        readiness = streaming._ensure_mcp_discovery("profile-s", discover, "ts")
        with caplog.at_level("WARNING", logger="api.streaming"):
            status = streaming._wait_and_surface_mcp_readiness(
                readiness, "profile-s", "sess-1"
            )
        assert status == "failed"
        assert any(
            "MCP discovery failed" in rec.getMessage() for rec in caplog.records
        )

    def test_completed_status_not_surfaced_as_failure(self, caplog):
        """A completed readiness proceeds without a failure log."""
        discover, started = _make_discover(0.05)
        readiness = streaming._ensure_mcp_discovery(
            "profile-s2", discover, "ts2"
        )
        with caplog.at_level("WARNING", logger="api.streaming"):
            status = streaming._wait_and_surface_mcp_readiness(
                readiness, "profile-s2", "sess-2"
            )
        assert status == "completed"
        assert not any(
            "MCP discovery failed" in rec.getMessage() for rec in caplog.records
        )
