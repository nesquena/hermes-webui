"""Regression tests for #7613 — run-journal retention sweep (terminal runs only).

`~/.hermes/webui/sessions/_run_journal/` had no retention policy of any kind:
`delete_run_journal()` has exactly one call site (session deletion), and there
was no TTL, no size cap, and no age sweep anywhere. A long-lived or pinned
session therefore accumulated one `{run_id}.jsonl` per run forever — measured
at 916 MB of completed-run logs on one real install (98.6% of the footprint
was `terminal: true` runs).

These tests pin the sweep's contract:

* terminal (`completed` / `interrupted-by-user` / `errored`) runs past the
  age / count / size caps ARE reclaimed,
* non-terminal runs are NEVER touched (they are the crashed-run payloads the
  journal exists to recover),
* an unrelated session's journal is untouched (isolation),
* the age signal is the FILE's own mtime, never the directory's,
* a run that changed after classification is not unlinked,
* reclaimed runs leave no stale seq/summary cache entries behind.

`sweep_run_journal` accepts `session_dir` so tests never touch real state.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import api.run_journal as run_journal
from api.run_journal import (
    RunJournalWriter,
    latest_run_summary,
    sweep_run_journal,
)

DAY = 86400.0
REPO = Path(__file__).resolve().parent.parent


def _write_terminal_run(session_dir: Path, sid: str, run_id: str, *, age_days: float = 0.0, events: int = 3) -> Path:
    """Create a settled run journal (ends in a terminal row) with a chosen age."""
    writer = RunJournalWriter(sid, run_id, session_dir=session_dir)
    for i in range(events - 1):
        writer.append_sse_event("token", {"text": f"chunk-{i}"})
    writer.append_sse_event("done", {"session": {"session_id": sid}})
    path = session_dir / run_journal.RUN_JOURNAL_DIR_NAME / sid / f"{run_id}.jsonl"
    if age_days:
        old = time.time() - age_days * DAY
        os.utime(path, (old, old))
    return path


def _write_open_run(session_dir: Path, sid: str, run_id: str, *, age_days: float = 0.0, events: int = 2) -> Path:
    """Create a crashed (non-terminal) run journal with a chosen age."""
    writer = RunJournalWriter(sid, run_id, session_dir=session_dir)
    for i in range(events):
        writer.append_sse_event("token", {"text": f"open-{i}"})
    path = session_dir / run_journal.RUN_JOURNAL_DIR_NAME / sid / f"{run_id}.jsonl"
    if age_days:
        old = time.time() - age_days * DAY
        os.utime(path, (old, old))
    return path


def _journal_dir(session_dir: Path, sid: str) -> Path:
    return session_dir / run_journal.RUN_JOURNAL_DIR_NAME / sid


def test_sweep_reclaims_terminal_runs_past_the_age_ttl(tmp_path):
    """A settled run older than the TTL is retired; a fresh settled run survives."""
    stale = _write_terminal_run(tmp_path, "sid-1", "run-old", age_days=30)
    stale_size = stale.stat().st_size
    fresh = _write_terminal_run(tmp_path, "sid-1", "run-new", age_days=0)

    result = sweep_run_journal(session_dir=tmp_path, ttl_days=14, max_runs_per_session=0, max_bytes_per_session=0)

    assert not stale.exists(), "terminal run past the TTL must be reclaimed"
    assert fresh.exists(), "recent terminal run must be retained"
    assert result["removed_files"] == 1
    assert result["removed_bytes"] == stale_size
    # The session dir itself remains (runs can still be written).
    assert _journal_dir(tmp_path, "sid-1").exists()


def test_sweep_never_touches_non_terminal_runs(tmp_path):
    """Non-terminal runs (crashed-run recovery payloads) are load-bearing: never reclaim them."""
    open_old = _write_open_run(tmp_path, "sid-1", "run-open-old", age_days=90)
    open_new = _write_open_run(tmp_path, "sid-1", "run-open-new", age_days=0)
    stale_term = _write_terminal_run(tmp_path, "sid-1", "run-term-old", age_days=90)

    result = sweep_run_journal(session_dir=tmp_path, ttl_days=1, max_runs_per_session=1, max_bytes_per_session=0)

    assert open_old.exists(), "old non-terminal run must NEVER be reclaimed"
    assert open_new.exists(), "recent non-terminal run must never be reclaimed"
    assert not stale_term.exists(), "terminal run past the caps is reclaimed"
    assert result["removed_files"] == 1


def test_sweep_respects_per_session_run_count_cap(tmp_path):
    """Keep at most N terminal runs per session; the newest terminal runs win."""
    for i in range(5):
        _write_terminal_run(tmp_path, "sid-1", f"run-{i}", age_days=float(10 - i))  # run-4 newest

    # TTL effectively disabled (huge): only the count cap can reclaim.
    result = sweep_run_journal(session_dir=tmp_path, ttl_days=3650, max_runs_per_session=2, max_bytes_per_session=0)

    survivors = sorted(p.name for p in _journal_dir(tmp_path, "sid-1").glob("*.jsonl"))
    assert survivors == ["run-3.jsonl", "run-4.jsonl"], survivors
    assert result["removed_files"] == 3


def test_sweep_respects_per_session_size_cap(tmp_path):
    """Total retained terminal bytes per session are bound; oldest terminal runs go first."""
    for i in range(4):
        path = _write_terminal_run(tmp_path, "sid-1", f"run-{i}", age_days=float(10 - i))
        pad = "x" * 2048
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"seq": 99, "event": "metering", "payload": {"pad": pad}}) + "\n")
        old = time.time() - float(10 - i) * DAY  # re-age after the padding append
        os.utime(path, (old, old))

    sizes = {p.name: p.stat().st_size for p in _journal_dir(tmp_path, "sid-1").glob("*.jsonl")}
    # Budget: room for exactly the newest two runs (run-2 and run-3).
    cap = sizes["run-2.jsonl"] + sizes["run-3.jsonl"]

    result = sweep_run_journal(session_dir=tmp_path, ttl_days=3650, max_runs_per_session=0, max_bytes_per_session=cap)

    survivors = sorted(p.name for p in _journal_dir(tmp_path, "sid-1").glob("*.jsonl"))
    assert survivors == ["run-2.jsonl", "run-3.jsonl"], survivors
    assert result["removed_files"] == 2


def test_sweep_size_cap_retirement_is_a_sticky_newest_first_prefix(tmp_path):
    """Once the byte budget is exceeded, everything older is reclaimed too.

    A smaller file further back in the ordering must not survive merely because
    it would individually fit inside the budget.
    """
    big_new = _write_terminal_run(tmp_path, "sid-1", "run-big-new", age_days=1)
    with open(big_new, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"seq": 99, "event": "metering", "payload": {"pad": "x" * 4096}}) + "\n")
    os.utime(big_new, (time.time() - DAY, time.time() - DAY))

    small_mid = _write_terminal_run(tmp_path, "sid-1", "run-small-mid", age_days=3)
    big_old = _write_terminal_run(tmp_path, "sid-1", "run-big-old", age_days=5)
    with open(big_old, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"seq": 99, "event": "metering", "payload": {"pad": "x" * 8192}}) + "\n")
    os.utime(big_old, (time.time() - 5 * DAY, time.time() - 5 * DAY))
    small_oldest = _write_terminal_run(tmp_path, "sid-1", "run-small-oldest", age_days=9)

    # Budget fits only the newest run: everything older retires, even the small
    # run that would individually fit.
    cap = big_new.stat().st_size

    result = sweep_run_journal(session_dir=tmp_path, ttl_days=3650, max_runs_per_session=0, max_bytes_per_session=cap)

    survivors = sorted(p.name for p in _journal_dir(tmp_path, "sid-1").glob("*.jsonl"))
    assert survivors == ["run-big-new.jsonl"], survivors
    assert not small_mid.exists(), "a mid-size run after the overflow point must retire too"
    assert not small_oldest.exists(), "an old small run must retire with the overflow prefix"
    assert result["removed_files"] == 3


def test_sweep_is_isolated_per_session(tmp_path):
    """One session's over-cap journal must not touch a neighbouring session's journal."""
    for i in range(4):
        _write_terminal_run(tmp_path, "sid-busy", f"run-{i}", age_days=float(10 - i))
    keep_term = _write_terminal_run(tmp_path, "sid-quiet", "run-a", age_days=1)
    keep_open = _write_open_run(tmp_path, "sid-quiet", "run-b", age_days=0)

    sweep_run_journal(session_dir=tmp_path, ttl_days=3650, max_runs_per_session=2, max_bytes_per_session=0)

    busy = sorted(p.name for p in _journal_dir(tmp_path, "sid-busy").glob("*.jsonl"))
    assert busy == ["run-2.jsonl", "run-3.jsonl"]
    assert keep_term.exists(), "unrelated session's terminal run must survive"
    assert keep_open.exists(), "unrelated session's open run must survive"


def test_sweep_is_a_noop_when_nothing_exceeds_the_caps(tmp_path):
    """Below every cap: the sweep reports zero removals and deletes nothing."""
    a = _write_terminal_run(tmp_path, "sid-1", "run-a", age_days=1)
    b = _write_open_run(tmp_path, "sid-1", "run-b", age_days=1)

    result = sweep_run_journal(session_dir=tmp_path, ttl_days=14, max_runs_per_session=10, max_bytes_per_session=10 * 1024 * 1024)

    assert a.exists() and b.exists()
    assert result["removed_files"] == 0
    assert result["removed_bytes"] == 0


def test_sweep_does_not_use_directory_mtime_as_an_age_signal(tmp_path):
    """A live writer's session must not be reaped because the dir looks old.

    The sweep's predicate is the run's own terminal event + the FILE's mtime.
    Touch the directory mtime far into the past; the fresh run must survive.
    """
    fresh = _write_terminal_run(tmp_path, "sid-1", "run-live", age_days=0)
    d = _journal_dir(tmp_path, "sid-1")
    old = time.time() - 90 * DAY
    os.utime(d, (old, old))  # directory mtime is old, file is fresh

    sweep_run_journal(session_dir=tmp_path, ttl_days=14, max_runs_per_session=0, max_bytes_per_session=0)

    assert fresh.exists(), "fresh terminal run must survive an old dir mtime"


def test_sweep_treats_age_by_file_mtime_after_a_touch(tmp_path):
    """The file's own mtime is the age signal: an old file is reclaimed even in a fresh dir."""
    stale = _write_terminal_run(tmp_path, "sid-1", "run-stale", age_days=40)
    # Make the directory look brand-new.
    os.utime(_journal_dir(tmp_path, "sid-1"), None)

    sweep_run_journal(session_dir=tmp_path, ttl_days=14, max_runs_per_session=0, max_bytes_per_session=0)

    assert not stale.exists()


def test_sweep_removed_bytes_are_reported(tmp_path):
    """The sweep result reports reclaimed file count and bytes for diagnostics."""
    a = _write_terminal_run(tmp_path, "sid-1", "run-a", age_days=30)
    b = _write_terminal_run(tmp_path, "sid-1", "run-b", age_days=30)
    expected = a.stat().st_size + b.stat().st_size

    result = sweep_run_journal(session_dir=tmp_path, ttl_days=14, max_runs_per_session=0, max_bytes_per_session=0)

    assert result["removed_files"] == 2
    assert result["removed_bytes"] == expected


def test_sweep_missing_journal_root_is_a_clean_noop(tmp_path):
    """No journal root yet: no exception, zero removals."""
    result = sweep_run_journal(session_dir=tmp_path, ttl_days=14, max_runs_per_session=1, max_bytes_per_session=1)
    assert result["removed_files"] == 0
    assert result["removed_bytes"] == 0


def test_sweep_evicts_seq_and_summary_caches_for_removed_runs(tmp_path):
    """Reclaimed runs leave no stale seq/summary cache entries behind.

    Mirrors `delete_run_journal`'s eviction contract from #5784/#5799: a later
    re-creation of the same path must restart at seq 1, not resume a stale
    cached seq from the removed file.
    """
    path = _write_terminal_run(tmp_path, "sid-1", "run-1", age_days=30)
    latest_run_summary("sid-1", "run-1", session_dir=tmp_path)  # seed the summary cache
    assert str(path) in run_journal._SEQ_CACHE, "writer append must have seeded the seq cache"

    sweep_run_journal(session_dir=tmp_path, ttl_days=14, max_runs_per_session=0, max_bytes_per_session=0)

    assert not path.exists()
    assert str(path) not in run_journal._SEQ_CACHE
    assert str(path) not in run_journal._SUMMARY_CACHE


def test_sweep_skips_a_run_file_that_changed_after_classification(tmp_path, monkeypatch):
    """A trailing append between classification and reclaim must abort the unlink.

    The reclaim step re-checks the complete stat identity captured at
    classification time (device / inode / size / mtime / ctime); if anything
    moved, the file was not quiescent and must be left in place. The wrapper
    forwards every kwarg the sweep passes (a stale signature would TypeError
    into the error counter and pass vacuously), and the no-errors assertion
    makes that failure mode impossible to miss.
    """
    path = _write_terminal_run(tmp_path, "sid-1", "run-1", age_days=30)

    real_reclaim = run_journal._reclaim_run_file

    def _racing_reclaim(candidate, expected_signature, **kwargs):
        # A writer lands one more row after classification, before the unlink.
        with open(candidate, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"seq": 99, "event": "metering", "payload": {}}) + "\n")
        return real_reclaim(candidate, expected_signature, **kwargs)

    monkeypatch.setattr(run_journal, "_reclaim_run_file", _racing_reclaim)

    result = sweep_run_journal(session_dir=tmp_path, ttl_days=14, max_runs_per_session=0, max_bytes_per_session=0)

    assert path.exists(), "stat-identity mismatch must abort the unlink"
    assert result["removed_files"] == 0
    assert result["errors"] == 0, "the injected race must not surface as a sweep error"


def test_reclaim_pins_the_session_dir_against_a_swap_race(tmp_path, monkeypatch):
    """A dir swap in the check-then-use window must not reclaim outside the root.

    Check-then-use race (Greptile review on PR #7642, round 2): containment is
    validated, then the session directory is replaced with a symlink to an
    external directory holding a same-name HARD LINK to the classified inode
    (identical dev/ino/size/mtime/ctime, so a signature re-check cannot tell
    them apart). A pathname-resolving unlink would follow the swapped path and
    remove that external entry. The reclaim pins the session directory as an
    open handle, so the unlink acts on the directory's inode and the external
    entry survives.

    The swap fires at ``_lock_for`` — the exact mid-reclaim moment, after the
    containment check and first stat, before the re-check and unlink.
    """
    session_dir = tmp_path / "sessions"
    sid = "swap-sid"
    real = _write_terminal_run(session_dir, sid, "swap-run", age_days=90)

    outside = tmp_path / "outside"
    outside.mkdir()
    external_entry = outside / "swap-run.jsonl"
    os.link(real, external_entry)  # same inode, separate directory entry

    journal_dir = _journal_dir(session_dir, sid)
    moved_away = tmp_path / "moved-away"
    real_lock_for = run_journal._lock_for
    state = {"swapped": False}

    def _swapping_lock_for(path):
        if not state["swapped"]:
            # The race: swap the session dir for a symlink to the outside dir.
            os.rename(journal_dir, moved_away)
            os.symlink(outside, journal_dir)
            state["swapped"] = True
        return real_lock_for(path)

    monkeypatch.setattr(run_journal, "_lock_for", _swapping_lock_for)

    result = sweep_run_journal(
        session_dir=session_dir, ttl_days=14, max_runs_per_session=0, max_bytes_per_session=0
    )

    assert external_entry.exists(), (
        "the unlink must act on the pinned session directory, never on whatever "
        "the swapped pathname resolves to"
    )
    assert not (moved_away / "swap-run.jsonl").exists(), (
        "the classified entry is still reclaimed (from the pinned directory itself)"
    )
    assert result["removed_files"] == 1
    assert result["errors"] == 0


def test_reclaim_leaves_a_dir_swapped_before_the_reclaim_untouched(tmp_path, monkeypatch):
    """A dir swapped to a symlink before the reclaim runs is refused (fail closed).

    Companion to the race test above: when the swap is already in place as the
    reclaim starts, the containment gate resolves the path outside the journal
    root and refuses — nothing is removed, inside or outside the root.
    """
    session_dir = tmp_path / "sessions"
    sid = "preswap-sid"
    real = _write_terminal_run(session_dir, sid, "preswap-run", age_days=90)

    outside = tmp_path / "preswap-outside"
    outside.mkdir()
    external_entry = outside / "preswap-run.jsonl"
    os.link(real, external_entry)

    journal_dir = _journal_dir(session_dir, sid)
    moved_away = tmp_path / "preswap-moved"
    real_reclaim = run_journal._reclaim_run_file

    def _preswapped_reclaim(candidate, expected_signature, **kwargs):
        os.rename(journal_dir, moved_away)
        os.symlink(outside, journal_dir)
        return real_reclaim(candidate, expected_signature, **kwargs)

    monkeypatch.setattr(run_journal, "_reclaim_run_file", _preswapped_reclaim)

    result = sweep_run_journal(
        session_dir=session_dir, ttl_days=14, max_runs_per_session=0, max_bytes_per_session=0
    )

    assert external_entry.exists(), "nothing outside the root is ever touched"
    assert (moved_away / "preswap-run.jsonl").exists(), "a refused reclaim removes nothing"
    assert result["removed_files"] == 0
    assert result["errors"] == 0


def test_sweep_keeps_the_newest_terminal_run_even_when_it_exceeds_the_size_cap(tmp_path):
    """A session always keeps its most recent settled run; the size cap never empties it.

    The TTL is what retires an old oversized anchor — not an immediate eviction
    that could destroy the only settled run in a session.
    """
    only_run = _write_terminal_run(tmp_path, "sid-1", "run-only", age_days=1)
    pad = "x" * 512 * 1024
    with open(only_run, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"seq": 99, "event": "token", "payload": {"text": pad}}) + "\n")

    result = sweep_run_journal(session_dir=tmp_path, ttl_days=3650, max_runs_per_session=0, max_bytes_per_session=1)

    assert only_run.exists(), "the newest terminal run is exempt from the size cap"
    assert result["removed_files"] == 0


def test_sweep_leaves_huge_unverifiable_files_untouched(tmp_path, monkeypatch):
    """A file whose terminal row cannot be proven within the scan budget is left alone.

    Fail closed: a missed reclaim is safe, a wrong one is not.
    """
    monkeypatch.setattr(run_journal, "_RETENTION_VERIFY_MAX_BYTES", 64)
    path = _write_terminal_run(tmp_path, "sid-1", "run-1", age_days=30, events=6)

    result = sweep_run_journal(session_dir=tmp_path, ttl_days=0, max_runs_per_session=1, max_bytes_per_session=0)

    assert path.exists(), "unprovable terminality must never reclaim"
    assert result["removed_files"] == 0


def test_cap_resolution_precedence_env_over_settings_over_default(monkeypatch):
    """Caps resolve env var > settings.json > default, and 0 disables a cap."""
    import api.run_journal as rj

    monkeypatch.delenv(rj._RETENTION_MAX_RUNS_ENV, raising=False)
    settings = {rj._RETENTION_MAX_RUNS_SETTING: 12}
    assert rj._resolve_retention_cap(
        rj._RETENTION_MAX_RUNS_ENV,
        rj._RETENTION_MAX_RUNS_SETTING,
        settings,
        kind=int,
        default=40,
        minimum=0,
        maximum=100_000,
    ) == 12
    monkeypatch.setenv(rj._RETENTION_MAX_RUNS_ENV, "7")
    assert rj._resolve_retention_cap(
        rj._RETENTION_MAX_RUNS_ENV,
        rj._RETENTION_MAX_RUNS_SETTING,
        settings,
        kind=int,
        default=40,
        minimum=0,
        maximum=100_000,
    ) == 7
    # Invalid values fall through to the next source, never raise.
    monkeypatch.setenv(rj._RETENTION_MAX_RUNS_ENV, "not-a-number")
    assert rj._resolve_retention_cap(
        rj._RETENTION_MAX_RUNS_ENV,
        rj._RETENTION_MAX_RUNS_SETTING,
        {},
        kind=int,
        default=40,
        minimum=0,
        maximum=100_000,
    ) == 40
    # 0 is a valid explicit "disabled".
    assert rj._resolve_retention_cap(
        rj._RETENTION_MAX_RUNS_ENV,
        rj._RETENTION_MAX_RUNS_SETTING,
        {rj._RETENTION_MAX_RUNS_SETTING: 0},
        kind=int,
        default=40,
        minimum=0,
        maximum=100_000,
    ) == 0


def test_retention_caps_read_from_settings_json(monkeypatch):
    """The `run_journal_retention_*` settings.json keys are honoured."""
    import api.config as cfg
    import api.run_journal as rj

    for env_name in (
        rj._RETENTION_TTL_ENV,
        rj._RETENTION_MAX_RUNS_ENV,
        rj._RETENTION_MAX_BYTES_ENV,
    ):
        monkeypatch.delenv(env_name, raising=False)
    monkeypatch.setattr(
        cfg,
        "load_settings",
        lambda: {
            rj._RETENTION_TTL_SETTING: 3,
            rj._RETENTION_MAX_RUNS_SETTING: 5,
            rj._RETENTION_MAX_BYTES_SETTING: 1024,
        },
    )

    caps = rj.resolve_run_journal_retention_caps()

    assert caps == {"ttl_days": 3.0, "max_runs_per_session": 5, "max_bytes_per_session": 1024}


def test_sweep_is_due_only_after_the_interval(monkeypatch):
    """`maybe_sweep_run_journal` gates on the interval and delays the first pass.

    The maintenance tick calls this every 60 s; only a due check may start a
    sweep. Fresh process → first call arms a boot delay so the first pass is due
    after ~RETENTION_FIRST_SWEEP_DELAY_SECS; then one sweep per interval.
    """
    import api.run_journal as rj

    monkeypatch.delenv(rj.RUN_JOURNAL_SWEEP_ENV, raising=False)
    calls = []
    monkeypatch.setattr(rj, "sweep_run_journal", lambda: (calls.append(1), {"removed_files": 0})[1])
    try:
        rj._reset_run_journal_sweep_schedule()
        base = 1_000_000.0
        assert rj.maybe_sweep_run_journal(now=base) is None, "first call must arm the boot delay"
        assert calls == []
        assert rj.maybe_sweep_run_journal(now=base + 30) is None, "not due before the boot delay"
        assert calls == []
        result = rj.maybe_sweep_run_journal(now=base + rj.RETENTION_FIRST_SWEEP_DELAY_SECS + 1)
        assert result == {"removed_files": 0}, "first pass must be due after the boot delay"
        assert calls == [1]
        # Next pass is one full interval later, not before.
        assert rj.maybe_sweep_run_journal(now=base + rj.RETENTION_SWEEP_INTERVAL_SECS) is None
        assert calls == [1], "a second tick inside the interval must not re-sweep"
        assert rj.maybe_sweep_run_journal(
            now=base + rj.RETENTION_FIRST_SWEEP_DELAY_SECS + 1 + rj.RETENTION_SWEEP_INTERVAL_SECS
        ) == {"removed_files": 0}
        assert calls == [1, 1]
    finally:
        rj._reset_run_journal_sweep_schedule()


def test_sweep_is_disabled_by_env(monkeypatch):
    """HERMES_WEBUI_RUN_JOURNAL_SWEEP=0 stops the tick from ever sweeping."""
    import api.run_journal as rj

    monkeypatch.setenv(rj.RUN_JOURNAL_SWEEP_ENV, "0")
    calls = []
    monkeypatch.setattr(rj, "sweep_run_journal", lambda: calls.append(1) or {"removed_files": 0})
    try:
        rj._reset_run_journal_sweep_schedule()
        assert rj.run_journal_sweep_enabled() is False
        assert rj.maybe_sweep_run_journal(now=1_000_000.0) is None
        assert rj.maybe_sweep_run_journal(now=1_000_000.0 + rj.RETENTION_SWEEP_INTERVAL_SECS) is None
        assert calls == [], "disabled sweep must never run"
    finally:
        rj._reset_run_journal_sweep_schedule()


def test_maintenance_tick_drives_the_sweep():
    """The shared maintenance tick must call the due-check, not a private thread.

    Without this wiring the sweep would be dead code and the journal would keep
    growing unbounded (#7613). Riding the existing SessionChannel reaper tick is
    deliberate: `server.py` has a hard <750-line guard and the repo's convention
    keeps server logic in `api/`.
    """
    src = (REPO / "api" / "background_process.py").read_text(encoding="utf-8")
    assert "maybe_sweep_run_journal" in src, "the maintenance tick must drive the retention sweep"
    assert "_reaper_loop" in src


def test_sweep_never_loses_rows_written_while_it_runs(tmp_path):
    """A run still receiving rows must never lose them to a concurrent sweep.

    Runs the real sweep repeatedly while a writer appends to a (settle-window
    fresh) run, then asserts every written row is present and ordered — the
    end-to-end form of the stat-identity race guard.
    """
    import threading

    sid, run_id = "sid-live", "run-live"
    writer = RunJournalWriter(sid, run_id, session_dir=tmp_path)
    path = tmp_path / run_journal.RUN_JOURNAL_DIR_NAME / sid / f"{run_id}.jsonl"
    stop = threading.Event()
    written = []

    def _append_loop():
        i = 0
        while not stop.is_set() and i < 60:
            try:
                writer.append_sse_event("token", {"text": f"live-{i}"})
                written.append(i)
            except Exception:
                pass
            i += 1
            time.sleep(0.005)

    thread = threading.Thread(target=_append_loop)
    thread.start()
    try:
        for _ in range(12):
            sweep_run_journal(session_dir=tmp_path, ttl_days=0, max_runs_per_session=1, max_bytes_per_session=0)
            time.sleep(0.02)
    finally:
        stop.set()
        thread.join(timeout=10)
    writer.append_sse_event("done", {"session": {"session_id": sid}})

    assert path.exists(), "a live run must never be reclaimed out from under its writer"
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    seqs = [row["seq"] for row in rows]
    assert seqs == list(range(1, len(rows) + 1)), "rows must stay contiguous — no lost append"
    texts = [row["payload"].get("text") for row in rows if row.get("event") == "token"]
    assert texts == [f"live-{i}" for i in written], "every written row must survive the sweep"


def test_periodic_loop_sweeps_the_default_session_dir(tmp_path, monkeypatch):
    """The sweep must target the DEFAULT session dir, not just accept one.

    Proves the due-check wiring end-to-end on the isolated test state dir: a
    stale terminal run planted under the resolved default session dir is
    reclaimed once a due tick runs, and a non-terminal sibling survives.
    """
    import api.run_journal as rj
    from api.run_journal import RunJournalWriter

    default_dir = rj._default_session_dir()
    sid = "loop-sweep-test"
    journal_dir = default_dir / rj.RUN_JOURNAL_DIR_NAME / sid

    def _plant(run_id, terminal, age_days):
        w = RunJournalWriter(sid, run_id, session_dir=default_dir)
        w.append_sse_event("token", {"text": "a"})
        if terminal:
            w.append_sse_event("done", {"session": {"session_id": sid}})
        p = journal_dir / f"{run_id}.jsonl"
        old = time.time() - age_days * DAY
        os.utime(p, (old, old))
        return p

    stale_terminal = _plant("loop-run-term", True, 30)
    open_run = _plant("loop-run-open", False, 30)

    monkeypatch.delenv(rj.RUN_JOURNAL_SWEEP_ENV, raising=False)
    try:
        rj._reset_run_journal_sweep_schedule()
        base = 4_000_000.0
        assert rj.maybe_sweep_run_journal(now=base) is None  # arms the boot delay
        rj.maybe_sweep_run_journal(now=base + rj.RETENTION_SWEEP_INTERVAL_SECS)  # due tick
    finally:
        rj._reset_run_journal_sweep_schedule()

    assert not stale_terminal.exists(), "a due tick must sweep the default session dir"
    assert open_run.exists(), "the sweep must never touch non-terminal runs"
    # Clean up the planted files so the shared test state dir stays tidy.
    try:
        open_run.unlink()
    except OSError:
        pass



# ── Symlink containment (Greptile review on PR #7642) ──────────────────────


def _raw_terminal_file(dir_path: Path, sid: str, run_id: str, *, age_days: float = 90.0) -> Path:
    """Write a terminal run file BY HAND with ids matching its parent dir/stem.

    Mirrors the real attack shape: `_run_journal/<sid>/<run_id>.jsonl` where the
    rows carry `run_id == stem` and `session_id == parent.name`. Written as raw
    JSONL (not via RunJournalWriter) so the file can be planted at a path the
    writer would never choose — e.g. inside a symlink target directory.
    """
    rows = [
        {
            "version": 1,
            "event_id": f"{run_id}:1",
            "seq": 1,
            "run_id": run_id,
            "session_id": sid,
            "event": "token",
            "type": "token",
            "created_at": 1.0,
            "terminal": False,
            "terminal_state": None,
            "payload": {"text": "x"},
        },
        {
            "version": 1,
            "event_id": f"{run_id}:2",
            "seq": 2,
            "run_id": run_id,
            "session_id": sid,
            "event": "done",
            "type": "done",
            "created_at": 2.0,
            "terminal": True,
            "terminal_state": "completed",
            "payload": {"session": {"session_id": sid}},
        },
    ]
    dir_path.mkdir(parents=True, exist_ok=True)
    path = dir_path / f"{run_id}.jsonl"
    path.write_text(
        "".join(json.dumps(row, separators=(",", ":")) + "\n" for row in rows),
        encoding="utf-8",
    )
    old = time.time() - age_days * DAY
    os.utime(path, (old, old))
    return path


def test_sweep_ignores_a_symlinked_session_dir_pointing_outside(tmp_path):
    """A symlinked session dir must not let the sweep reclaim outside the root.

    Regression for the Greptile finding on PR #7642: if `_run_journal/<sid>` is
    a symlink to an external directory, a naive `is_dir()` walk traverses it and
    unlinks matching files OUTSIDE the journal root. The sweep must skip any
    symlinked session dir (the journal only ever creates real directories).
    """
    outside = tmp_path / "outside"
    outside.mkdir()
    # The attack: files whose run_id/session_id match the LINK NAME, so they
    # would classify as terminal if the sweep followed the symlink.
    victim_a = _raw_terminal_file(outside, "evil-sid", "outside-run-a")
    victim_b = _raw_terminal_file(outside, "evil-sid", "outside-run-b")

    session_dir = tmp_path / "sessions"
    journal_root = session_dir / run_journal.RUN_JOURNAL_DIR_NAME
    journal_root.mkdir(parents=True)
    os.symlink(outside, journal_root / "evil-sid")

    result = sweep_run_journal(
        session_dir=session_dir, ttl_days=14, max_runs_per_session=0, max_bytes_per_session=0
    )

    assert victim_a.exists(), "sweep must not unlink files outside the journal root"
    assert victim_b.exists(), "sweep must not unlink files outside the journal root"
    assert result["removed_files"] == 0
    assert result["files_scanned"] == 0, "a symlinked session dir must not be traversed"


def test_sweep_ignores_a_symlinked_run_file_pointing_outside(tmp_path):
    """A symlinked run file must not be reclaimed, even inside a real session dir.

    Second half of the containment contract: the session dir is legitimate, but
    one `{run_id}.jsonl` entry is a symlink to an external file. The symlink is
    skipped (the journal only ever writes plain files), so the target survives.
    """
    session_dir = tmp_path / "sessions"
    sid = "symlink-file-sid"
    journal_dir = _journal_dir(session_dir, sid)
    journal_dir.mkdir(parents=True)

    outside = tmp_path / "outside-file"
    outside.mkdir()
    victim = _raw_terminal_file(outside, sid, "linked-run")
    os.symlink(victim, journal_dir / "linked-run.jsonl")

    result = sweep_run_journal(
        session_dir=session_dir, ttl_days=14, max_runs_per_session=0, max_bytes_per_session=0
    )

    assert victim.exists(), "a symlinked run file's target must never be unlinked"
    assert result["removed_files"] == 0


def test_sweep_still_reclaims_normal_files_alongside_a_symlink(tmp_path):
    """The containment guard must not break ordinary reclamation.

    Same shape as the attack test, but the qualifying run is a REAL file in a
    REAL session dir alongside a symlinked sibling: the real stale terminal run
    is reclaimed, the symlink target is not touched.
    """
    session_dir = tmp_path / "sessions"
    sid = "mixed-sid"
    real = _write_terminal_run(session_dir, sid, "real-run", age_days=90)

    outside = tmp_path / "mixed-outside"
    outside.mkdir()
    victim = _raw_terminal_file(outside, sid, "linked-run")
    os.symlink(victim, _journal_dir(session_dir, sid) / "linked-run.jsonl")

    result = sweep_run_journal(
        session_dir=session_dir, ttl_days=14, max_runs_per_session=0, max_bytes_per_session=0
    )

    assert result["removed_files"] == 1, "the real stale terminal run must be reclaimed"
    assert result["removed_bytes"] > 0
    assert not real.exists()
    assert victim.exists(), "the symlinked target must survive"
