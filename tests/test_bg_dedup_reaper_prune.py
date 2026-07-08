"""Regression test for the ``BG_TASK_COMPLETE_EVENTS_SEEN`` memory leak (#4633).

The per-session completion-dedup map gained one ``session_id -> set[process_id]``
entry the first time a background task completed for a session and was never
removed anywhere in the repo, so it grew unbounded for the server lifetime.

The dedup entry is created in ``_process_one`` for EVERY completion, whether or
not any SSE channel/tab exists — so coupling the prune to channel collection
would miss the dominant headless case (task fires, tab closed or never opened).
Instead the reaper sweeps the map by DELIVERY lifecycle: once a completion has
been drained (its ``session_id`` removed from ``PENDING_BG_TASK_COMPLETIONS``),
the short dedup window is closed and the entry is swept. Session deletion prunes
it too, covering a session deleted while a completion is still pending.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_reaper_source_sweeps_dedup_map_by_delivery():
    """The reaper cleanup must sweep BG_TASK_COMPLETE_EVENTS_SEEN under its lock,
    gated on PENDING_BG_TASK_COMPLETIONS (delivery), not channel collection."""
    src = (REPO_ROOT / "api" / "background_process.py").read_text(encoding="utf-8")
    reaper = src[src.index("def _reaper_loop("):]
    reaper = reaper[: reaper.index("\ndef ", 1)]
    assert "BG_TASK_COMPLETE_EVENTS_SEEN_LOCK" in reaper, (
        "reaper must take the dedup-map lock"
    )
    assert "BG_TASK_COMPLETE_EVENTS_SEEN.pop(" in reaper, (
        "reaper must pop dedup entries"
    )
    assert "PENDING_BG_TASK_COMPLETIONS" in reaper, (
        "the sweep must be gated on delivery (PENDING_BG_TASK_COMPLETIONS), "
        "not on channel collection — otherwise headless completions leak"
    )


def _run_reaper_once(bp, cfg):
    """Start the real reaper, wait for one sweep pass, stop it."""
    bp.start_session_channel_reaper()
    time.sleep(0.4)  # first pass runs immediately (interval sleep is at loop tail)
    bp.stop_session_channel_reaper()


def test_reaper_sweeps_delivered_dedup_entry_without_any_channel():
    """The dominant headless case: a completed+drained task with NO SSE channel
    ever — its dedup entry must still be swept (this is what the channel-coupled
    version missed)."""
    from api import background_process as bp
    from api import config as cfg

    sid = "sess-headless-4633"
    with cfg.BG_TASK_COMPLETE_EVENTS_SEEN_LOCK:
        cfg.BG_TASK_COMPLETE_EVENTS_SEEN[sid] = {"proc-1"}
    cfg.PENDING_BG_TASK_COMPLETIONS.discard(sid)  # delivered/drained
    # No SessionChannel for sid — nothing for channel collection to reap.
    try:
        with bp.SESSION_CHANNELS_LOCK:
            assert sid not in bp.SESSION_CHANNELS
        _run_reaper_once(bp, cfg)
        with cfg.BG_TASK_COMPLETE_EVENTS_SEEN_LOCK:
            assert sid not in cfg.BG_TASK_COMPLETE_EVENTS_SEEN, (
                "reaper did not sweep the delivered headless dedup entry"
            )
    finally:
        bp.stop_session_channel_reaper()
        with cfg.BG_TASK_COMPLETE_EVENTS_SEEN_LOCK:
            cfg.BG_TASK_COMPLETE_EVENTS_SEEN.pop(sid, None)


def test_reaper_retains_dedup_entry_while_completion_pending():
    """An UNDELIVERED completion (still in PENDING) must keep its dedup entry —
    the _move_to_finished dedup window is still open."""
    from api import background_process as bp
    from api import config as cfg

    sid = "sess-pending-4633"
    with cfg.BG_TASK_COMPLETE_EVENTS_SEEN_LOCK:
        cfg.BG_TASK_COMPLETE_EVENTS_SEEN[sid] = {"proc-2"}
    cfg.PENDING_BG_TASK_COMPLETIONS.add(sid)  # undelivered
    try:
        _run_reaper_once(bp, cfg)
        with cfg.BG_TASK_COMPLETE_EVENTS_SEEN_LOCK:
            assert sid in cfg.BG_TASK_COMPLETE_EVENTS_SEEN, (
                "reaper swept a still-pending (undelivered) dedup entry"
            )
    finally:
        bp.stop_session_channel_reaper()
        cfg.PENDING_BG_TASK_COMPLETIONS.discard(sid)
        with cfg.BG_TASK_COMPLETE_EVENTS_SEEN_LOCK:
            cfg.BG_TASK_COMPLETE_EVENTS_SEEN.pop(sid, None)


def test_session_delete_prunes_dedup_entry():
    """Deleting a session prunes its dedup entry even if the completion never
    delivered (so a delete-while-pending session can't leak forever)."""
    from api import background_process as bp
    from api import config as cfg

    sid = "sess-delete-4633"
    with cfg.BG_TASK_COMPLETE_EVENTS_SEEN_LOCK:
        cfg.BG_TASK_COMPLETE_EVENTS_SEEN[sid] = {"proc-3"}
    cfg.PENDING_BG_TASK_COMPLETIONS.add(sid)  # undelivered, but being deleted
    try:
        bp.forget_bg_task_completion_dedup(sid)
        with cfg.BG_TASK_COMPLETE_EVENTS_SEEN_LOCK:
            assert sid not in cfg.BG_TASK_COMPLETE_EVENTS_SEEN
    finally:
        cfg.PENDING_BG_TASK_COMPLETIONS.discard(sid)
        with cfg.BG_TASK_COMPLETE_EVENTS_SEEN_LOCK:
            cfg.BG_TASK_COMPLETE_EVENTS_SEEN.pop(sid, None)


if __name__ == "__main__":  # pragma: no cover - manual invocation
    raise SystemExit(pytest.main([__file__, "-v"]))
