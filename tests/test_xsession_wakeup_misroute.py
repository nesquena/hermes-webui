"""Regression: cross-session notify_on_complete wakeup misroute (Option 1 + Option 3).

ROOT CAUSE (RCA t_f62ff1e8, verified line-by-line):
  WebUI's per-turn session identity was bound ONLY to the process-global
  ``os.environ['HERMES_SESSION_KEY']`` (streaming.py turn-start), and the env
  lock was released BEFORE the agent ran. WebUI NEVER called
  ``gateway.session_context.set_session_vars`` so the ``_SESSION_KEY``
  contextvar stayed ``_UNSET`` and ``tools.approval.get_current_session_key``
  fell back to the racy process-global env. Two concurrent WebUI turns
  therefore raced on one slot: session A's ``terminal(notify_on_complete=True)``
  spawn could capture session B's id, and at completion the server-side wakeup
  turn started for the WRONG session.

This module proves BOTH fix layers with REAL modules (no mocks of the code
under test), so each test is RED before the fix and GREEN after — never a
tautology:

  Option 1 (root fix, streaming.py) — ``_bind_turn_session_identity`` binds the
    REAL ``_SESSION_KEY`` contextvar for the turn's worker thread/context and
    clears it on exit. Under a simulated env race across two concurrent turns,
    the REAL ``get_current_session_key`` must return each turn's OWN id.

  Option 3 (defense-in-depth, background_process.py) —
    ``_resolve_wakeup_target`` cross-checks the (possibly env-contaminated)
    session_key-resolved session against the spawn-time owner persisted in the
    process registry's ``ProcessSession.spawn_session_id`` (an env-immune
    field). On a positively-detected mismatch it re-routes to the true owner
    instead of waking the wrong session.

Precedent for this no-live-server style: tests/test_wakeup_defer_race.py,
tests/test_session_channel_option_x.py.
"""
from __future__ import annotations

import importlib
import threading

import pytest


# ---------------------------------------------------------------------------
# Option 1 — contextvar binding makes per-turn session identity race-immune
# ---------------------------------------------------------------------------

def test_streaming_exposes_turn_session_identity_binder():
    """streaming.py must expose the helper that binds the REAL _SESSION_KEY
    contextvar for the agent worker thread (root fix, not env-only)."""
    streaming = importlib.import_module("api.streaming")
    assert hasattr(streaming, "_bind_turn_session_identity"), (
        "Option 1 missing: streaming.py must expose _bind_turn_session_identity "
        "to bind gateway.session_context._SESSION_KEY for the turn"
    )


def test_concurrent_turns_capture_their_own_session_under_env_race():
    """THE invariant. Two concurrent WebUI turns, A and B. Each binds its own
    session identity via the REAL streaming helper, then — while the OTHER
    turn has just stamped the shared process-global env (the documented race:
    lock released, agent still running) — performs the EXACT capture call a
    notify_on_complete spawn makes: ``tools.approval.get_current_session_key``.

    Pre-fix: WebUI never set the contextvar, so the helper does not exist
    (ImportError above) / the contextvar stays _UNSET and the capture falls
    back to the env → A captures B's id → MISROUTE (RED).

    Post-fix: the helper binds _SESSION_KEY in each worker context, so the
    contextvar (task-local) wins over the racy env → each turn captures its
    OWN id (GREEN).
    """
    import os

    streaming = importlib.import_module("api.streaming")
    pytest.importorskip("tools.approval", reason="hermes-agent not installed")
    pytest.importorskip("gateway.session_context")
    from tools.approval import get_current_session_key
    from gateway import session_context as sc

    bind = getattr(streaming, "_bind_turn_session_identity", None)
    if bind is None:
        pytest.fail("Option 1 not implemented: _bind_turn_session_identity missing")

    # The two turn() threads below stamp os.environ["HERMES_SESSION_KEY"]
    # without owning it. Save/restore the prior value (sentinel for "was
    # unset") so this test does not leak state into sibling tests when
    # collection order interleaves it with another consumer.
    _prev_env_sentinel = object()
    _prev_env = os.environ.get("HERMES_SESSION_KEY", _prev_env_sentinel)

    SESS_A = "20260518_161627_60b5f4"   # board claude-code-import
    SESS_B = "47ec28f66dff"             # board mcp-optimize

    captured: dict[str, str] = {}
    barrier = threading.Barrier(2)
    # Force a deterministic interleave: A captures only AFTER B has stamped
    # the process-global env (reproduces "lock released, agent still running,
    # other turn stamps the global slot").
    b_stamped_env = threading.Event()

    def turn(my_sid: str, label: str) -> None:
        # streaming.py turn-start still writes the process-global env as a
        # fallback for non-contextvar consumers; the fix is that session-key
        # ROUTING now binds the contextvar so it no longer races.
        with bind(my_sid):
            os.environ["HERMES_SESSION_KEY"] = my_sid
            barrier.wait()
            if label == "B":
                # B stamps env last while A's "agent" is still mid-turn.
                os.environ["HERMES_SESSION_KEY"] = my_sid
                b_stamped_env.set()
            else:
                assert b_stamped_env.wait(timeout=5), "B never stamped env"
            # The EXACT call terminal_tool.py makes for a bg spawn:
            captured[label] = get_current_session_key(default="")

    try:
        ta = threading.Thread(target=turn, args=(SESS_A, "A"))
        tb = threading.Thread(target=turn, args=(SESS_B, "B"))
        ta.start()
        tb.start()
        ta.join(timeout=10)
        tb.join(timeout=10)

        # Assert the worker threads actually terminated. If a thread deadlocks the
        # join() above returns silently — surface that as a clear test failure
        # instead of letting downstream asserts mask the hang or leak threads into
        # the rest of the run.
        assert not ta.is_alive(), "worker thread A did not terminate within join timeout"
        assert not tb.is_alive(), "worker thread B did not terminate within join timeout"

        # Contextvar must be restored after the turn context exits (no thread-pool
        # residue → no new race for a reused worker).
        assert sc._SESSION_KEY.get() is sc._UNSET

        assert captured.get("A") == SESS_A, (
            f"MISROUTE: session A captured {captured.get('A')!r}, expected "
            f"{SESS_A!r} — per-turn identity still races on process-global env"
        )
        assert captured.get("B") == SESS_B, (
            f"MISROUTE: session B captured {captured.get('B')!r}, expected {SESS_B!r}"
        )
    finally:
        # Restore HERMES_SESSION_KEY to its pre-test value (or unset if it was
        # never set), independent of which assertion above might have failed —
        # see save/restore note at the top of the test.
        if _prev_env is _prev_env_sentinel:
            os.environ.pop("HERMES_SESSION_KEY", None)
        else:
            assert isinstance(_prev_env, str)
            os.environ["HERMES_SESSION_KEY"] = _prev_env


def test_turn_identity_binder_restores_previous_value():
    """Restore uses contextvars reset-token semantics (the canonical idiom),
    NOT a blanket clear_session_vars: it composes correctly under nesting and
    restores _UNSET for the top-level turn so CLI/cron env-fallback compat is
    preserved, and it must NOT touch the platform/chat_id/user session vars
    (those keep their env fallback so the notify_on_complete watcher
    registration that reads HERMES_SESSION_PLATFORM still works)."""
    streaming = importlib.import_module("api.streaming")
    pytest.importorskip("tools.approval", reason="hermes-agent not installed")
    from tools.approval import get_current_session_key
    from gateway import session_context as sc

    bind = streaming._bind_turn_session_identity
    assert sc._SESSION_KEY.get() is sc._UNSET
    # Platform var starts unset → env fallback path intact.
    assert sc._SESSION_PLATFORM.get() is sc._UNSET
    with bind("sid-outer"):
        assert get_current_session_key(default="") == "sid-outer"
        with bind("sid-inner"):
            assert get_current_session_key(default="") == "sid-inner"
        # Reset-token restores the OUTER value (composes under nesting),
        # it does NOT clear to "".
        assert get_current_session_key(default="") == "sid-outer"
        # The binder must never disturb the other session vars.
        assert sc._SESSION_PLATFORM.get() is sc._UNSET
    # Full exit restores _UNSET → env fallback resumes (CLI/cron compat).
    assert sc._SESSION_KEY.get() is sc._UNSET
    assert sc._SESSION_PLATFORM.get() is sc._UNSET


# ---------------------------------------------------------------------------
# Option 3 — completion-time second-check against the env-immune spawn owner
# ---------------------------------------------------------------------------

def test_background_process_exposes_wakeup_target_resolver():
    bp = importlib.import_module("api.background_process")
    assert hasattr(bp, "_resolve_wakeup_target"), (
        "Option 3 missing: background_process.py must expose _resolve_wakeup_target "
        "to cross-check the session_key-resolved target against the env-immune "
        "spawn-time owner"
    )


def _fake_ps(session_key: str, spawn_session_id: str):
    import types

    return types.SimpleNamespace(
        session_key=session_key, spawn_session_id=spawn_session_id
    )


def test_resolve_wakeup_target_reroutes_on_positive_mismatch(monkeypatch):
    """The RCA scenario: env race made the process's ``session_key`` resolve to
    session B, but the env-immune ``spawn_session_id`` says session A truly
    spawned it. ``_resolve_wakeup_target`` must detect the positive mismatch
    and return A (the true owner), NOT B.
    """
    bp = importlib.import_module("api.background_process")

    SESS_A = "20260518_161627_60b5f4"
    SESS_B = "47ec28f66dff"

    # session_key (env-contaminated) → resolves to B; spawn owner is A.
    ps = _fake_ps(session_key=SESS_B, spawn_session_id=SESS_A)

    resolved = bp._resolve_wakeup_target(
        process_id="proc_f377e2f552cd",
        session_key_resolved_sid=SESS_B,
        proc_session=ps,
    )
    assert resolved == SESS_A, (
        f"Option 3 failed to re-route: woke {resolved!r}, the env-immune spawn "
        f"owner is {SESS_A!r} — this is the exact agent.log:6632 misroute"
    )


def test_resolve_wakeup_target_passthrough_when_consistent():
    """No mismatch (the normal case, and the post-Option 1 case): the resolver is
    a pure pass-through and never suppresses a legitimate Option Z wakeup."""
    bp = importlib.import_module("api.background_process")

    sid = "session-normal"
    ps = _fake_ps(session_key=sid, spawn_session_id=sid)
    assert bp._resolve_wakeup_target(
        process_id="proc_ok",
        session_key_resolved_sid=sid,
        proc_session=ps,
    ) == sid


def test_resolve_wakeup_target_passthrough_when_owner_unknown():
    """If the spawn owner is indeterminate (no env-immune field, e.g. a
    cron/CLI process sharing the registry, or a pre-Option 1 spawn), the resolver
    must fall through to the session_key-resolved sid UNCHANGED — never
    suppress a wakeup on uncertainty (Option Z must keep working)."""
    bp = importlib.import_module("api.background_process")

    sid = "session-unknown-owner"
    assert bp._resolve_wakeup_target(
        process_id="proc_cron",
        session_key_resolved_sid=sid,
        proc_session=_fake_ps(session_key=sid, spawn_session_id=""),
    ) == sid
    assert bp._resolve_wakeup_target(
        process_id="proc_cron",
        session_key_resolved_sid=sid,
        proc_session=None,
    ) == sid
