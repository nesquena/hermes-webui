"""Regression tests for #7613 — run-journal retention sweep.

``delete_run_journal`` only fires on full session delete, so a long-lived
session accumulates one ``{run_id}.jsonl`` per turn forever. #7613 reported
916 MB across 104 sessions on one install, 98.6% from completed runs. The
sweep must reclaim ONLY terminal run files (``terminal: true``); the
non-terminal remainder is the live-recovery payload and silently deleting
it would destroy the user's only recoverable output.

These tests pin:
- ``should_purge_run`` rejects non-terminal regardless of age/count.
- ``purge_session_terminal_journals`` evicts the three in-memory caches
  (``_WRITER_LOCKS`` / ``_SEQ_CACHE`` / ``_SUMMARY_CACHE``) the same way
  ``delete_run_journal`` does, with the same per-cache mutexes.
- The age + count caps compose OR, and aggressive mode halves the age cap.
- Directory mtime is NOT a usable age signal (#7613 warning).
- One bad session does not abort a whole-journal sweep.
- ``maybe_sweep_run_journals`` throttles by ``min_interval_secs`` and
  rejects host

ile paths outside the journal dir.
"""
import json
import os
import time
from pathlib import Path

import pytest

import api.run_journal as run_journal
from api.run_journal import (
    RUN_JOURNAL_DIR_NAME,
    _purge_run_file,
    maybe_sweep_run_journals,
    purge_all_terminal_journals,
    purge_session_terminal_journals,
    should_purge_run,
)


# ── Helpers ──────────────────────────────────────────────────────────────


def _write_terminal_journal(
    session_dir: Path,
    sid: str,
    rid: str,
    *,
    mtime_age_secs: float,
    terminal: bool = True,
    terminal_state: str = "completed",
    pad_bytes: int = 0,
) -> Path:
    """Write a minimal run journal file with controlled mtime.

    Single-row journal; ``terminal`` flag decides whether ``_summary_from_events``
    derives ``terminal: True``. ``mtime_age_secs`` is subtracted from now
    before ``os.utime`` so the sweep sees the file at that age.
    """
    path = Path(session_dir) / RUN_JOURNAL_DIR_NAME / sid / f"{rid}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    event = {
        "version": 1,
        "event_id": f"{rid}:1",
        "seq": 1,
        "run_id": rid,
        "session_id": sid,
        "event": "done" if terminal else "assistant_token",
        "type": "done" if terminal else "assistant_token",
        "created_at": time.time(),
        "terminal": terminal,
        "terminal_state": terminal_state if terminal else None,
    }
    body = json.dumps(event) + "\n"
    if pad_bytes > 0:
        body = body + " " * max(0, pad_bytes - len(body))
    path.write_text(body)
    target_mtime = time.time() - mtime_age_secs
    os.utime(path, (target_mtime, target_mtime))
    return path


def _retention(
    *,
    enabled: bool = True,
    max_age_secs: int = 14 * 86400,
    max_runs_per_session: int = 100,
    aggressive_total_bytes: int = 500 * 1024 * 1024,
    min_interval_secs: int = 300,
    aggressive: bool = False,
) -> dict:
    return {
        "enabled": enabled,
        "max_age_secs": max_age_secs,
        "max_runs_per_session": max_runs_per_session,
        "aggressive_total_bytes": aggressive_total_bytes,
        "min_interval_secs": min_interval_secs,
        "aggressive": aggressive,
    }


@pytest.fixture(autouse=True)
def _reset_module_caches():
    """Drop module-level caches between tests so retention state never leaks."""
    with run_journal._WRITER_LOCKS_GUARD:
        run_journal._WRITER_LOCKS.clear()
    with run_journal._SEQ_CACHE_LOCK:
        run_journal._SEQ_CACHE.clear()
    with run_journal._SUMMARY_CACHE_LOCK:
        run_journal._SUMMARY_CACHE.clear()
    run_journal._LAST_SWEEP_AT = 0.0
    yield


# ── should_purge_run unit tests ──────────────────────────────────────────


def test_should_purge_run_terminal_past_age_purges():
    spec = _retention(max_age_secs=100)
    assert should_purge_run(
        {"terminal": True}, file_mtime=0.0, retention=spec, now=200.0
    ) is True


def test_should_purge_run_nontemrinal_never_purges():
    """#7613 core invariant: non-terminal recovery payload must NEVER be reclaimed."""
    spec = _retention(max_age_secs=100, max_runs_per_session=1)
    # Age well past cap AND over-cap flag set — still no.
    assert should_purge_run(
        {"terminal": False}, file_mtime=0.0, retention=spec, now=10_000.0,
        is_over_count_cap=True,
    ) is False


def test_should_purge_run_count_cap_purges_when_over():
    spec = _retention(max_age_secs=10**9, max_runs_per_session=10)
    assert should_purge_run(
        {"terminal": True}, file_mtime=time.time(), retention=spec, now=time.time(),
        is_over_count_cap=True,
    ) is True


def test_should_purge_run_under_count_cap_does_not_purge():
    spec = _retention(max_age_secs=10**9, max_runs_per_session=10)
    assert should_purge_run(
        {"terminal": True}, file_mtime=time.time(), retention=spec, now=time.time(),
        is_over_count_cap=False,
    ) is False


def test_should_purge_run_zero_count_cap_disables_cap():
    spec = _retention(max_age_secs=10**9, max_runs_per_session=0)
    # Even with the over-cap flag set, cap=0 disables the trigger.
    assert should_purge_run(
        {"terminal": True}, file_mtime=time.time(), retention=spec, now=time.time(),
        is_over_count_cap=True,
    ) is False


def test_should_purge_run_aggressive_halves_age_cap():
    spec = _retention(max_age_secs=100, aggressive=True)
    # At 60s old, under default 100s but over 50s (halved) → purge.
    assert should_purge_run(
        {"terminal": True}, file_mtime=0.0, retention=spec, now=60.0,
    ) is True


def test_should_purge_run_aggressive_below_half_age_does_not_purge():
    spec = _retention(max_age_secs=100, aggressive=True)
    # At 40s old, below 50s halved cap → keep.
    assert should_purge_run(
        {"terminal": True}, file_mtime=0.0, retention=spec, now=40.0,
    ) is False


# ── _purge_run_file unit tests ───────────────────────────────────────────


def test_purge_run_file_evicts_three_caches(tmp_path: Path):
    """`_purge_run_file` must drop _WRITER_LOCKS / _SEQ_CACHE / _SUMMARY_CACHE
    entries for the same path, under each cache's own mutex — mirroring
    `delete_run_journal`'s directory-level sweep (#5784, #5799)."""
    path = _write_terminal_journal(tmp_path, "sid", "rid", mtime_age_secs=10**9)
    # Seed the three caches for this path.
    dir_key = str(path.parent)
    name = path.name
    with run_journal._WRITER_LOCKS_GUARD:
        run_journal._WRITER_LOCKS[(dir_key, name, str(os.getpid()))] = __import__("threading").Lock()
    with run_journal._SEQ_CACHE_LOCK:
        run_journal._SEQ_CACHE[str(path)] = 7
    with run_journal._SUMMARY_CACHE_LOCK:
        run_journal._SUMMARY_CACHE[str(path)] = ((0, 0, 0, 0, 0), {"terminal": True})

    assert _purge_run_file(path) is True
    assert not path.exists()

    with run_journal._WRITER_LOCKS_GUARD:
        assert not [k for k in run_journal._WRITER_LOCKS if k[0] == dir_key and k[1] == name]
    with run_journal._SEQ_CACHE_LOCK:
        assert not [k for k in run_journal._SEQ_CACHE if str(Path(k).parent) == dir_key and Path(k).name == name]
    with run_journal._SUMMARY_CACHE_LOCK:
        assert not [k for k in run_journal._SUMMARY_CACHE if str(Path(k).parent) == dir_key and Path(k).name == name]


def test_purge_run_file_rejects_dot_and_dotdot_stems():
    """`.` and `..` are matched by ``_SAFE_ID_RE`` (it allows dot chars) but
    resolve to the journal root / its parent — reject them explicitly, the
    same way ``delete_run_journal`` does. pathlib happily keeps the literal
    ``..`` in ``.stem`` / ``.parent.name`` as long as we don't `resolve()`."""
    # stem = "..": `Path("/synthetic/...jsonl")` splits as stem="..", suffix=".jsonl"
    stem_dotdot = Path("/synthetic/...jsonl")
    assert stem_dotdot.suffix == ".jsonl" and stem_dotdot.stem == ".."
    assert _purge_run_file(stem_dotdot) is False

    # parent.name = "..": `Path("..") / "rid.jsonl"` keeps the literal ".." in parent.name
    parent_dotdot = Path("..") / "rid.jsonl"
    assert parent_dotdot.suffix == ".jsonl" and parent_dotdot.parent.name == ".."
    assert _purge_run_file(parent_dotdot) is False


def test_purge_run_file_rejects_non_jsonl(tmp_path: Path):
    """Even if the path is inside the journal dir, a non-`.jsonl` suffix is
    refused so the unlink can never reach an unrelated file (e.g. the
    session's `_run_journal` directory itself or a sidecar)."""
    stray = tmp_path / RUN_JOURNAL_DIR_NAME / "sid" / "rid"
    stray.parent.mkdir(parents=True, exist_ok=True)
    stray.write_text("x")
    assert _purge_run_file(stray) is False
    assert stray.exists()


# ── purge_session_terminal_journals integration tests ───────────────────


def test_purge_session_mixed_terminal_and_nontemrinal_keeps_nontemrinal(tmp_path: Path):
    """A session with one terminal and one non-terminal file, both well past
    age — only the terminal one is reclaimed. This is the single most
    important regression #7613 needs to pin."""
    terminal = _write_terminal_journal(tmp_path, "s", "rt", mtime_age_secs=10**9, terminal=True)
    live = _write_terminal_journal(tmp_path, "s", "rl", mtime_age_secs=10**9, terminal=False)

    deleted = purge_session_terminal_journals(
        "s", session_dir=tmp_path,
        retention=_retention(max_age_secs=100), now=time.time(),
    )

    assert deleted == 1
    assert not terminal.exists()
    assert live.exists(), "non-terminal recovery payload must survive"


def test_purge_session_old_dir_mtime_does_not_force_nontemrinal_purge(tmp_path: Path):
    """#7613 explicit warning: directory mtime is not a usable age signal.
    Even if the parent dir's mtime is ancient, a non-terminal child file
    must NOT be considered aged-out."""
    live = _write_terminal_journal(tmp_path, "s", "rl", mtime_age_secs=0, terminal=False)
    # Force the parent dir to look old.
    old = time.time() - 10**9
    os.utime(live.parent, (old, old))

    deleted = purge_session_terminal_journals(
        "s", session_dir=tmp_path,
        retention=_retention(max_age_secs=100), now=time.time(),
    )

    assert deleted == 0
    assert live.exists()


def test_purge_session_count_cap_purges_oldest_terminal(tmp_path: Path):
    """5 terminal files, cap=2 → 3 oldest purged, 2 newest kept."""
    paths = []
    for i in range(5):
        # Older index → larger mtime_age_secs → older file.
        p = _write_terminal_journal(tmp_path, "s", f"r{i}", mtime_age_secs=10**9 - i)
        paths.append(p)

    deleted = purge_session_terminal_journals(
        "s", session_dir=tmp_path,
        retention=_retention(max_age_secs=10**12, max_runs_per_session=2),
        now=time.time(),
    )

    assert deleted == 3
    # r0, r1, r2 are the oldest three by mtime (largest mtime_age_secs).
    assert not paths[0].exists()
    assert not paths[1].exists()
    assert not paths[2].exists()
    assert paths[3].exists()
    assert paths[4].exists()


def test_purge_session_disabled_does_nothing(tmp_path: Path):
    path = _write_terminal_journal(tmp_path, "s", "r", mtime_age_secs=10**9)
    deleted = purge_session_terminal_journals(
        "s", session_dir=tmp_path,
        retention=_retention(enabled=False, max_age_secs=100),
        now=time.time(),
    )
    assert deleted == 0
    assert path.exists()


def test_purge_session_isolation_keeps_other_sessions(tmp_path: Path):
    """A bad / unrelated session must never have its journal touched when
    a different session is being swept."""
    target = _write_terminal_journal(tmp_path, "target", "rt", mtime_age_secs=10**9)
    other = _write_terminal_journal(tmp_path, "other", "ro", mtime_age_secs=10**9)

    deleted = purge_session_terminal_journals(
        "target", session_dir=tmp_path,
        retention=_retention(max_age_secs=100), now=time.time(),
    )

    assert deleted == 1
    assert not target.exists()
    assert other.exists(), "unrelated session must remain intact"


def test_purge_session_idempotent_on_second_sweep(tmp_path: Path):
    _write_terminal_journal(tmp_path, "s", "r", mtime_age_secs=10**9)
    spec = _retention(max_age_secs=100)

    first = purge_session_terminal_journals("s", session_dir=tmp_path, retention=spec, now=time.time())
    second = purge_session_terminal_journals("s", session_dir=tmp_path, retention=spec, now=time.time())

    assert first == 1
    assert second == 0


def test_purge_session_age_cap_keeps_recent_terminal(tmp_path: Path):
    fresh = _write_terminal_journal(tmp_path, "s", "rf", mtime_age_secs=10, terminal=True)
    old = _write_terminal_journal(tmp_path, "s", "ro", mtime_age_secs=10**9, terminal=True)

    deleted = purge_session_terminal_journals(
        "s", session_dir=tmp_path,
        retention=_retention(max_age_secs=100), now=time.time(),
    )

    assert deleted == 1
    assert not old.exists()
    assert fresh.exists()


# ── purge_all_terminal_journals integration tests ──────────────────────


def test_purge_all_aggressive_when_total_bytes_exceed_threshold(tmp_path: Path):
    """When total on-disk bytes exceed ``aggressive_total_bytes``, the per-session
    sweep must run with ``aggressive=True`` (age cap halved). Use a file whose
    age is between half-age and full-age to prove aggressive kicked in."""
    # 100-day-old terminal, half-age=50d, full-age=100d, threshold set so
    # the file size alone trips aggressive.
    big = _write_terminal_journal(
        tmp_path, "s", "r", mtime_age_secs=75 * 86400, pad_bytes=2048,
    )
    spec = _retention(
        max_age_secs=100 * 86400,
        aggressive_total_bytes=1024,  # 1 KiB — easily exceeded by 2 KiB file
    )

    stats = purge_all_terminal_journals(session_dir=tmp_path, retention=spec, now=time.time())

    assert stats["aggressive"] is True
    assert stats["deleted"] == 1
    assert not big.exists()


def test_purge_all_below_threshold_does_not_set_aggressive(tmp_path: Path):
    _write_terminal_journal(
        tmp_path, "s", "r", mtime_age_secs=75 * 86400, pad_bytes=100,
    )
    spec = _retention(
        max_age_secs=100 * 86400,
        aggressive_total_bytes=10 * 1024 * 1024,  # 10 MiB — far above 100 B
    )

    stats = purge_all_terminal_journals(session_dir=tmp_path, retention=spec, now=time.time())

    assert stats["aggressive"] is False
    assert stats["deleted"] == 0


def test_purge_all_one_bad_session_does_not_abort_sweep(tmp_path: Path):
    """A session whose path fails the safe-id regex, or whose glob raises,
    must not abort the whole sweep — the all-sessions caller catches
    per-session exceptions and continues."""
    _write_terminal_journal(tmp_path, "good", "r", mtime_age_secs=10**9)
    # A bogus directory name that fails `_SAFE_ID_RE` (contains '/' via
    # ``..``-style traversal — we simulate by creating a sibling path
    # with a name the regex rejects). The simplest way to break a session
    # sweep without breaking iterdir itself is to plant a non-directory
    # entry at the session level.
    journal_root = tmp_path / RUN_JOURNAL_DIR_NAME
    (journal_root / "not-a-dir").write_text("x")

    stats = purge_all_terminal_journals(
        session_dir=tmp_path,
        retention=_retention(max_age_secs=100),
        now=time.time(),
    )

    assert stats["deleted"] == 1
    assert stats["scanned"] >= 1


# ── maybe_sweep_run_journals integration tests ─────────────────────────


def test_maybe_sweep_throttles_within_interval(monkeypatch, tmp_path: Path):
    """Two calls within ``min_interval_secs`` must result in only one sweep."""
    _write_terminal_journal(tmp_path, "s", "r", mtime_age_secs=10**9)
    monkeypatch.setattr(run_journal, "_LAST_SWEEP_AT", 0.0)

    # Patch the config so the helper reads a 1-hour interval.
    monkeypatch.setattr(run_journal, "_resolve_retention", lambda r=None: _retention(min_interval_secs=3600))

    first = maybe_sweep_run_journals(session_dir=tmp_path)
    second = maybe_sweep_run_journals(session_dir=tmp_path)

    assert first is not None and first["deleted"] == 1
    assert second is None, "second call within min_interval_secs must be throttled"


def test_maybe_sweep_force_bypasses_throttle(monkeypatch, tmp_path: Path):
    _write_terminal_journal(tmp_path, "s", "r1", mtime_age_secs=10**9)
    monkeypatch.setattr(run_journal, "_LAST_SWEEP_AT", 0.0)
    monkeypatch.setattr(run_journal, "_resolve_retention", lambda r=None: _retention(min_interval_secs=3600))

    first = maybe_sweep_run_journals(session_dir=tmp_path)
    second = maybe_sweep_run_journals(session_dir=tmp_path, force=True)

    assert first is not None and first["deleted"] == 1
    assert second is not None and second["deleted"] == 0  # already swept


def test_maybe_sweep_disabled_returns_none(monkeypatch, tmp_path: Path):
    _write_terminal_journal(tmp_path, "s", "r", mtime_age_secs=10**9)
    monkeypatch.setattr(run_journal, "_resolve_retention", lambda r=None: _retention(enabled=False))
    assert maybe_sweep_run_journals(session_dir=tmp_path) is None
