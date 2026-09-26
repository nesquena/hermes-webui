"""Run-journal archival retention (#7613).

The feature ARCHIVES runs past the age / count / size caps: eligible
``{rid}.jsonl`` files are compressed into ``_run_journal_archive/<sid>/`` and
the live file is only dropped once the compressed copy is proven to decompress
back to the exact bytes. A wrong classification therefore costs one compressed
copy instead of the data, and every read path falls back to the archive.

What must hold:
  * non-terminal runs are never archived (a live or crashed run stays readable
    in place);
  * the compressed copy is byte-exact before the live file is dropped;
  * when anything fails mid-way the live file survives (a missed archive is
    safe, a wrong archive is recoverable);
  * reads (summaries, event reads, session replay, run lookup) transparently
    see archived runs;
  * the sweep is off the request path and gated: env kill switch, hourly
    schedule with a boot delay, one sweep at a time;
  * the only destructive step — archive pruning — is disabled by default.
"""
import gzip
import json
import os
import shutil
import threading
import time
from pathlib import Path

import pytest

from api import run_journal as rj


# ── helpers ────────────────────────────────────────────────────────────────


def _write_run(root: Path, sid: str, rid: str, *, events: list[tuple[str, dict]] | None = None,
               terminal: bool = True, mtime_age_days: float = 0.0,
               terminal_state: str = "completed", truncate_terminal: bool = False) -> Path:
    """Create one run file shaped exactly like ``append_run_event`` writes it."""
    session_dir = root / rj.RUN_JOURNAL_DIR_NAME / sid
    session_dir.mkdir(parents=True, exist_ok=True)
    path = session_dir / f"{rid}.jsonl"
    rows = []
    seq = 1
    for name, payload in (events or [("token", {"text": "hello"}), ("token", {"text": " world"})]):
        rows.append({
            "version": 1,
            "event_id": f"{rid}:{seq}",
            "seq": seq,
            "run_id": rid,
            "session_id": sid,
            "event": name,
            "type": name,
            "created_at": time.time() - 3600,
            "terminal": False,
            "terminal_state": None,
            "payload": payload,
        })
        seq += 1
    if terminal:
        rows.append({
            "version": 1,
            "event_id": f"{rid}:{seq}",
            "seq": seq,
            "run_id": rid,
            "session_id": sid,
            "event": "done",
            "type": "done",
            "created_at": time.time() - 3600,
            "terminal": True,
            "terminal_state": terminal_state,
            "payload": {"terminal_state": terminal_state},
        })
    body = "".join(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n" for row in rows)
    if truncate_terminal:
        # Simulate a journal whose last row was cut mid-write: the bytes still
        # contain the ``"terminal":true`` marker but the row has no terminating
        # newline and does not parse as a complete JSON document.
        body = "".join(
            json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n" for row in rows[:-1]
        )
        last = json.dumps(rows[-1], ensure_ascii=False, separators=(",", ":"))
        body += last[: last.index('"terminal":true') + len('"terminal":true')]
    path.write_text(body, encoding="utf-8")
    if mtime_age_days:
        old = time.time() - mtime_age_days * 86400.0
        os.utime(path, (old, old))
    return path


def _archive_path(root: Path, sid: str, rid: str) -> Path:
    return root / rj.RUN_JOURNAL_ARCHIVE_DIR_NAME / sid / f"{rid}.jsonl.gz"


def _sweep(root: Path, **caps):
    return rj.sweep_run_journal(session_dir=root, **caps)


# ── classification: only provably-terminal runs are ever archived ──────────


def test_terminal_run_is_archived(tmp_path):
    _write_run(tmp_path, "s1", "r1", mtime_age_days=30)
    counters = _sweep(tmp_path, ttl_days=14, max_runs_per_session=0, max_bytes_per_session=0)
    assert counters["archived_files"] == 1
    assert counters["errors"] == 0
    assert not (tmp_path / rj.RUN_JOURNAL_DIR_NAME / "s1" / "r1.jsonl").exists()
    assert _archive_path(tmp_path, "s1", "r1").exists()


def test_non_terminal_run_is_never_archived(tmp_path):
    _write_run(tmp_path, "s1", "r1", terminal=False, mtime_age_days=365)
    counters = _sweep(tmp_path, ttl_days=14, max_runs_per_session=0, max_bytes_per_session=0)
    assert counters["archived_files"] == 0
    assert counters["retained_open"] == 1
    live = tmp_path / rj.RUN_JOURNAL_DIR_NAME / "s1" / "r1.jsonl"
    assert live.exists()


def test_truncated_terminal_row_is_not_terminal(tmp_path):
    """A journal cut mid-row (the only-copy shape) is NOT classified as terminal."""
    _write_run(tmp_path, "s1", "r1", mtime_age_days=365, truncate_terminal=True)
    counters = _sweep(tmp_path, ttl_days=14, max_runs_per_session=0, max_bytes_per_session=0)
    assert counters["archived_files"] == 0
    assert (tmp_path / rj.RUN_JOURNAL_DIR_NAME / "s1" / "r1.jsonl").exists()


def test_terminal_string_inside_payload_does_not_classify(tmp_path):
    """A non-terminal row whose PAYLOAD contains the terminal marker is not terminal."""
    _write_run(
        tmp_path,
        "s1",
        "r1",
        events=[("tool", {"blob": 'x "terminal":true y'})],
        terminal=False,
        mtime_age_days=365,
    )
    counters = _sweep(tmp_path, ttl_days=14, max_runs_per_session=0, max_bytes_per_session=0)
    assert counters["archived_files"] == 0
    assert (tmp_path / rj.RUN_JOURNAL_DIR_NAME / "s1" / "r1.jsonl").exists()


def test_terminal_row_for_another_run_does_not_classify(tmp_path):
    """Terminal row whose run_id/session_id do not match the file is foreign."""
    session_dir = tmp_path / rj.RUN_JOURNAL_DIR_NAME / "s1"
    session_dir.mkdir(parents=True)
    row = {
        "version": 1, "event_id": "other:3", "seq": 3, "run_id": "other",
        "session_id": "s1", "event": "done", "type": "done",
        "created_at": time.time() - 3600, "terminal": True,
        "terminal_state": "completed", "payload": {},
    }
    line = json.dumps(row, separators=(",", ":")) + "\n"
    (session_dir / "r1.jsonl").write_text(line, encoding="utf-8")
    old = time.time() - 365 * 86400
    os.utime(session_dir / "r1.jsonl", (old, old))
    counters = _sweep(tmp_path, ttl_days=14, max_runs_per_session=0, max_bytes_per_session=0)
    assert counters["archived_files"] == 0


def test_settlement_window_defers_recent_runs(tmp_path):
    """A just-settled run is never archived: the writer may still append."""
    _write_run(tmp_path, "s1", "r1", mtime_age_days=0.0)
    counters = _sweep(tmp_path, ttl_days=0.00001, max_runs_per_session=1, max_bytes_per_session=0)
    assert counters["archived_files"] == 0


# ── the archive is byte-exact before the live file is dropped ──────────────


def test_archived_copy_decompresses_to_exact_original_bytes(tmp_path):
    path = _write_run(tmp_path, "s1", "r1", mtime_age_days=30)
    original = path.read_bytes()
    _sweep(tmp_path, ttl_days=14, max_runs_per_session=0, max_bytes_per_session=0)
    with gzip.open(_archive_path(tmp_path, "s1", "r1"), "rb") as fh:
        assert fh.read() == original


def test_archive_written_through_verified_temp_before_live_unlink(tmp_path, monkeypatch):
    """The live file survives when the verify step fails."""
    path = _write_run(tmp_path, "s1", "r1", mtime_age_days=30)
    monkeypatch.setattr(rj, "_archive_reproduces_source", lambda *a, **k: False)
    counters = _sweep(tmp_path, ttl_days=14, max_runs_per_session=0, max_bytes_per_session=0)
    assert counters["archived_files"] == 0
    assert path.exists()
    assert not _archive_path(tmp_path, "s1", "r1").exists()


def test_append_during_classification_aborts_archive(tmp_path, monkeypatch):
    """A file that changes identity after classification is skipped, not archived."""
    path = _write_run(tmp_path, "s1", "r1", mtime_age_days=30)
    real = rj._journal_file_is_terminal

    def append_then_classify(*args, **kwargs):
        result = real(*args, **kwargs)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "version": 1, "event_id": "r1:99", "seq": 99, "run_id": "r1",
                "session_id": "s1", "event": "metering", "type": "metering",
                "created_at": time.time(), "terminal": False, "terminal_state": None,
                "payload": {},
            }, separators=(",", ":")) + "\n")
        return result

    monkeypatch.setattr(rj, "_journal_file_is_terminal", append_then_classify)
    counters = _sweep(tmp_path, ttl_days=14, max_runs_per_session=0, max_bytes_per_session=0)
    assert counters["archived_files"] == 0
    assert counters["skipped_files"] == 1
    assert path.exists()


def test_crash_window_archive_is_completed_not_clobbered(tmp_path):
    """Archive published but live unlink never ran (crash): next pass completes it.

    The live journal is append-only, so an existing archive is a PREFIX of the
    current live bytes. A complete-for-the-current-bytes archive is left as-is
    (and the live file dropped); one that no longer reproduces the live bytes
    (the run grew after the crash) is replaced with a freshly verified copy —
    never left as a truncated copy, which would silently lose the tail.
    """
    path = _write_run(tmp_path, "s1", "r1", mtime_age_days=30)
    original = path.read_bytes()
    existing = _archive_path(tmp_path, "s1", "r1")
    existing.parent.mkdir(parents=True)
    # Prefix-only archive: compresses just the first row, so it does NOT
    # reproduce the current live bytes and must be replaced.
    first_row = original.split(b"\n", 1)[0] + b"\n"
    with gzip.open(existing, "wb") as fh:
        fh.write(first_row)

    counters = _sweep(tmp_path, ttl_days=14, max_runs_per_session=0, max_bytes_per_session=0)

    assert counters["archived_files"] == 1
    assert counters["errors"] == 0
    assert not path.exists()  # live file dropped once the archive is complete
    with gzip.open(existing, "rb") as fh:
        assert fh.read() == original  # the FULL bytes, not the truncated prefix


def test_complete_existing_archive_is_kept_and_live_file_dropped(tmp_path):
    """A crash-window archive that already reproduces the live bytes is kept."""
    path = _write_run(tmp_path, "s1", "r1", mtime_age_days=30)
    existing = _archive_path(tmp_path, "s1", "r1")
    existing.parent.mkdir(parents=True)
    with gzip.open(existing, "wb") as fh:
        fh.write(path.read_bytes())
    before = existing.read_bytes()

    counters = _sweep(tmp_path, ttl_days=14, max_runs_per_session=0, max_bytes_per_session=0)

    assert counters["archived_files"] == 1
    assert not path.exists()
    assert existing.read_bytes() == before


# ── reads fall back to the archive transparently ───────────────────────────


def test_latest_run_summary_reads_archived_run(tmp_path):
    _write_run(tmp_path, "s1", "r1", mtime_age_days=30)
    _sweep(tmp_path, ttl_days=14, max_runs_per_session=0, max_bytes_per_session=0)
    summary = rj.latest_run_summary("s1", "r1", session_dir=tmp_path)
    assert summary["terminal"] is True
    assert summary["terminal_state"] == "completed"
    assert summary["event_count"] == 3


def test_read_run_events_reads_archived_run(tmp_path):
    _write_run(tmp_path, "s1", "r1", mtime_age_days=30)
    _sweep(tmp_path, ttl_days=14, max_runs_per_session=0, max_bytes_per_session=0)
    journal = rj.read_run_events("s1", "r1", session_dir=tmp_path)
    assert len(journal["events"]) == 3
    assert journal["events"][-1]["terminal"] is True


def test_find_run_summary_and_find_run_file_see_archived_run(tmp_path):
    _write_run(tmp_path, "s1", "r1", mtime_age_days=30)
    _sweep(tmp_path, ttl_days=14, max_runs_per_session=0, max_bytes_per_session=0)
    summary = rj.find_run_summary("r1", session_dir=tmp_path)
    assert summary is not None and summary["session_id"] == "s1"
    located = rj.find_run_file("r1", session_dir=tmp_path)
    assert located is not None and located[0] == "s1"


def test_session_journal_replay_includes_archived_runs(tmp_path):
    """:func:`read_session_run_events` replays archived + live runs together.

    The cursor points into the ARCHIVED run; its rows must replay alongside the
    live run's rows rather than the archived run silently vanishing from the
    replay window.
    """
    _write_run(tmp_path, "s1", "r1", mtime_age_days=30)
    _write_run(tmp_path, "s1", "r2", mtime_age_days=30 - 1)
    # Count cap only (ttl disabled): r1 is the older run, r2 stays live.
    _sweep(tmp_path, ttl_days=0, max_runs_per_session=1, max_bytes_per_session=0)
    archived = list((tmp_path / rj.RUN_JOURNAL_ARCHIVE_DIR_NAME / "s1").glob("*.jsonl.gz"))
    assert len(archived) == 1
    archived_rid = archived[0].name[: -len(".jsonl.gz")]
    result = rj.read_session_run_events("s1", after_event_id=f"{archived_rid}:1", session_dir=tmp_path)
    assert result["status"] == "ok"
    # The cursor's own run is resolvable even though its live file is gone, and
    # its post-cursor rows replay.
    assert any(
        event["run_id"] == archived_rid and event["seq"] > 1 for event in result["events"]
    )


def test_session_replay_cursor_resolves_via_archive_not_missing(tmp_path):
    """A cursor into an ARCHIVED run must not report ``cursor_run_missing``.

    This is the archive-visibility of the replay path: on the unarchived code
    the run id is unknown (its live file is gone), so the status degrades to
    ``cursor_run_missing``; with archive fallback the cursor resolves.
    """
    _write_run(tmp_path, "s1", "r1", mtime_age_days=30)
    _sweep(tmp_path, ttl_days=0, max_runs_per_session=1, max_bytes_per_session=0)
    _write_run(tmp_path, "s1", "r2", mtime_age_days=1)
    _write_run(tmp_path, "s1", "r3", mtime_age_days=1)
    _sweep(tmp_path, ttl_days=0, max_runs_per_session=1, max_bytes_per_session=0)
    result = rj.read_session_run_events("s1", after_event_id="r1:1", session_dir=tmp_path)
    assert result["status"] == "ok"
    assert result["cursor_run_id"] == "r1"


def test_session_replay_cursor_missing_when_run_fully_absent(tmp_path):
    _write_run(tmp_path, "s1", "r1", mtime_age_days=30)
    result = rj.read_session_run_events("s1", after_event_id="ghost:1", session_dir=tmp_path)
    assert result["status"] == "cursor_run_missing"


def test_live_and_archive_union_when_both_exist(tmp_path):
    """Both copies on disk (crash window / re-created run) read as the union."""
    path = _write_run(tmp_path, "s1", "r1", mtime_age_days=30)
    _sweep(tmp_path, ttl_days=14, max_runs_per_session=0, max_bytes_per_session=0)
    assert not path.exists()
    # Re-create the live path with strictly newer rows.
    with path.open("w", encoding="utf-8") as fh:
        fh.write(json.dumps({
            "version": 1, "event_id": "r1:4", "seq": 4, "run_id": "r1",
            "session_id": "s1", "event": "done", "type": "done",
            "created_at": time.time(), "terminal": True,
            "terminal_state": "completed", "payload": {},
        }, separators=(",", ":")) + "\n")
    journal = rj.read_run_events("s1", "r1", session_dir=tmp_path)
    seqs = [event["seq"] for event in journal["events"]]
    assert seqs == [1, 2, 3, 4]


# ── caps: age, count, and size ─────────────────────────────────────────────


def test_ttl_cap_only_archives_old_runs(tmp_path):
    _write_run(tmp_path, "s1", "old", mtime_age_days=30)
    _write_run(tmp_path, "s1", "recent", mtime_age_days=1)
    counters = _sweep(tmp_path, ttl_days=14, max_runs_per_session=0, max_bytes_per_session=0)
    assert counters["archived_files"] == 1
    assert (tmp_path / rj.RUN_JOURNAL_DIR_NAME / "s1" / "recent.jsonl").exists()
    assert _archive_path(tmp_path, "s1", "old").exists()


def test_count_cap_keeps_newest_runs(tmp_path):
    for index, rid in enumerate(["r1", "r2", "r3"]):
        _write_run(tmp_path, "s1", rid, mtime_age_days=30 - index)
    counters = _sweep(tmp_path, ttl_days=0, max_runs_per_session=1, max_bytes_per_session=0)
    assert counters["archived_files"] == 2
    assert (tmp_path / rj.RUN_JOURNAL_DIR_NAME / "s1" / "r3.jsonl").exists()


def test_size_cap_archives_oldest_first_and_keeps_newest(tmp_path):
    for index, rid in enumerate(["r1", "r2", "r3"]):
        _write_run(tmp_path, "s1", rid, mtime_age_days=30 - index)
    one_size = (tmp_path / rj.RUN_JOURNAL_DIR_NAME / "s1" / "r1.jsonl").stat().st_size
    counters = _sweep(
        tmp_path, ttl_days=0, max_runs_per_session=0, max_bytes_per_session=one_size + 10
    )
    assert counters["archived_files"] == 2
    assert (tmp_path / rj.RUN_JOURNAL_DIR_NAME / "s1" / "r3.jsonl").exists()


def test_zero_caps_disable_their_cap(tmp_path):
    _write_run(tmp_path, "s1", "r1", mtime_age_days=365)
    counters = _sweep(tmp_path, ttl_days=0, max_runs_per_session=0, max_bytes_per_session=0)
    assert counters["archived_files"] == 0


def test_caps_resolve_from_env_over_settings(tmp_path, monkeypatch):
    monkeypatch.setenv(rj._RETENTION_TTL_ENV, "3")
    _write_run(tmp_path, "s1", "r1", mtime_age_days=10)
    counters = _sweep(tmp_path, max_runs_per_session=0, max_bytes_per_session=0)
    assert counters["caps"]["ttl_days"] == 3.0
    assert counters["archived_files"] == 1


# ── gating: kill switch, schedule, single-flight ───────────────────────────


def test_sweep_disabled_by_env(monkeypatch):
    monkeypatch.setenv(rj.RUN_JOURNAL_SWEEP_ENV, "0")
    assert rj.run_journal_sweep_enabled() is False
    assert rj.maybe_sweep_run_journal() is None
    monkeypatch.setenv(rj.RUN_JOURNAL_SWEEP_ENV, "1")
    assert rj.run_journal_sweep_enabled() is True


def test_maybe_sweep_delays_first_pass_and_then_honours_interval(tmp_path, monkeypatch):
    rj._reset_run_journal_sweep_schedule()
    calls: list[float] = []
    monkeypatch.setattr(rj, "sweep_run_journal", lambda **kwargs: calls.append(1) or {"ok": True})
    base = 1_000_000.0
    assert rj.maybe_sweep_run_journal(now=base) is None  # arms the boot delay
    assert rj.maybe_sweep_run_journal(now=base + rj.RETENTION_FIRST_SWEEP_DELAY_SECS - 1) is None
    result = rj.maybe_sweep_run_journal(now=base + rj.RETENTION_FIRST_SWEEP_DELAY_SECS + 1)
    assert result == {"ok": True}
    assert rj.maybe_sweep_run_journal(now=base + rj.RETENTION_FIRST_SWEEP_DELAY_SECS + 2) is None
    result = rj.maybe_sweep_run_journal(
        now=base + rj.RETENTION_FIRST_SWEEP_DELAY_SECS + rj.RETENTION_SWEEP_INTERVAL_SECS + 2
    )
    assert result == {"ok": True}
    assert len(calls) == 2


def test_sweep_skipped_entirely_without_dir_fd_support(tmp_path, monkeypatch):
    _write_run(tmp_path, "s1", "r1", mtime_age_days=30)
    monkeypatch.setattr(rj, "_DIR_FD_OK", False)
    counters = _sweep(tmp_path, ttl_days=14, max_runs_per_session=0, max_bytes_per_session=0)
    assert counters["archived_files"] == 0
    assert (tmp_path / rj.RUN_JOURNAL_DIR_NAME / "s1" / "r1.jsonl").exists()


# ── hygiene: symlinks, ids, temps, archive pruning ─────────────────────────


def test_symlinked_session_dir_is_skipped(tmp_path):
    _write_run(tmp_path, "s1", "r1", mtime_age_days=30)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "evil.jsonl").write_text(
        json.dumps({
            "version": 1, "event_id": "evil:1", "seq": 1, "run_id": "evil",
            "session_id": "s2", "event": "done", "type": "done",
            "created_at": time.time(), "terminal": True,
            "terminal_state": "completed", "payload": {},
        }, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    journal_root = tmp_path / rj.RUN_JOURNAL_DIR_NAME
    (journal_root / "s2").symlink_to(outside)
    counters = _sweep(tmp_path, ttl_days=14, max_runs_per_session=0, max_bytes_per_session=0)
    assert counters["archived_files"] == 1  # only the real s1 run
    assert (outside / "evil.jsonl").exists()
    assert not (outside / "evil.jsonl.gz").exists()


def test_weird_session_and_run_names_are_skipped(tmp_path):
    journal_root = tmp_path / rj.RUN_JOURNAL_DIR_NAME
    weird_dir = journal_root / "bad name"
    weird_dir.mkdir(parents=True)
    _write_run(tmp_path, "s1", "r1", mtime_age_days=30)
    (weird_dir / "x.jsonl").write_text("", encoding="utf-8")
    counters = _sweep(tmp_path, ttl_days=14, max_runs_per_session=0, max_bytes_per_session=0)
    assert counters["errors"] == 0
    assert counters["archived_files"] == 1


def test_stray_temp_files_are_cleaned(tmp_path):
    _write_run(tmp_path, "s1", "r1", mtime_age_days=30)
    archive_session = tmp_path / rj.RUN_JOURNAL_ARCHIVE_DIR_NAME / "s1"
    archive_session.mkdir(parents=True)
    stray = archive_session / ".r1.jsonl.gz.tmp.999999"
    stray.write_bytes(b"partial")
    _sweep(tmp_path, ttl_days=14, max_runs_per_session=0, max_bytes_per_session=0)
    assert not stray.exists()
    assert _archive_path(tmp_path, "s1", "r1").exists()


def test_archive_pruning_disabled_by_default(tmp_path):
    _write_run(tmp_path, "s1", "r1", mtime_age_days=800)
    _sweep(tmp_path, ttl_days=14, max_runs_per_session=0, max_bytes_per_session=0)
    assert _archive_path(tmp_path, "s1", "r1").exists()
    counters = _sweep(tmp_path, ttl_days=0, max_runs_per_session=0, max_bytes_per_session=0)
    assert counters["pruned_archives"] == 0
    assert _archive_path(tmp_path, "s1", "r1").exists()


def test_archive_pruning_deletes_only_past_archive_ttl(tmp_path, monkeypatch):
    _write_run(tmp_path, "s1", "r1", mtime_age_days=800)
    _sweep(tmp_path, ttl_days=14, max_runs_per_session=0, max_bytes_per_session=0)
    archive = _archive_path(tmp_path, "s1", "r1")
    old = time.time() - 400 * 86400
    os.utime(archive, (old, old))
    monkeypatch.setenv(rj._RETENTION_ARCHIVE_TTL_ENV, "90")
    _write_run(tmp_path, "s1", "r2", mtime_age_days=800)
    # r2's archive is fresh; r1's is 400 days old and must be pruned.
    rj.sweep_run_journal(session_dir=tmp_path, ttl_days=14, max_runs_per_session=0, max_bytes_per_session=0)
    assert not archive.exists()
    assert _archive_path(tmp_path, "s1", "r2").exists()


def test_delete_run_journal_removes_archives_too(tmp_path):
    """Deleting a session must not leave recoverable payloads in the archive."""
    _write_run(tmp_path, "s1", "r1", mtime_age_days=30)
    _write_run(tmp_path, "s1", "r2", mtime_age_days=30)
    _sweep(tmp_path, ttl_days=14, max_runs_per_session=0, max_bytes_per_session=0)
    assert _archive_path(tmp_path, "s1", "r1").exists()
    _write_run(tmp_path, "s2", "keep", mtime_age_days=30)
    _sweep(tmp_path, ttl_days=14, max_runs_per_session=0, max_bytes_per_session=0)
    assert _archive_path(tmp_path, "s2", "keep").exists()

    rj.delete_run_journal("s1", session_dir=tmp_path)

    assert not (tmp_path / rj.RUN_JOURNAL_DIR_NAME / "s1").exists()
    assert not (tmp_path / rj.RUN_JOURNAL_ARCHIVE_DIR_NAME / "s1").exists()
    # A sibling session is untouched.
    assert _archive_path(tmp_path, "s2", "keep").exists()


def test_sweep_on_missing_root_is_a_noop(tmp_path):
    counters = _sweep(tmp_path / "nope", ttl_days=14, max_runs_per_session=0, max_bytes_per_session=0)
    assert counters["archived_files"] == 0
    assert counters["errors"] == 0


# ── containment: archived reads must never escape the archive root ─────────


def _symlinked_archive_dir_with_external_run(root: Path, sid: str, rid: str) -> Path:
    """Point ``_run_journal_archive/<sid>`` at an EXTERNAL dir holding ``<rid>.jsonl.gz``.

    Mirrors the escape reported on the PR: archive discovery used to follow the
    symlinked session directory, so the external gzip was read as journal data.
    """
    outside = root.parent / f"{root.name}-outside-{sid}"
    outside.mkdir(parents=True, exist_ok=True)
    body = (
        json.dumps(
            {
                "version": 1,
                "event_id": f"{rid}:1",
                "seq": 1,
                "run_id": rid,
                "session_id": sid,
                "event": "token",
                "type": "token",
                "created_at": time.time() - 3600,
                "terminal": True,
                "terminal_state": "completed",
                "payload": {"text": "EXTERNAL"},
            },
            separators=(",", ":"),
        )
        + "\n"
    )
    with gzip.open(outside / f"{rid}.jsonl.gz", "wb") as fh:
        fh.write(body.encode("utf-8"))
    archive_root = root / rj.RUN_JOURNAL_ARCHIVE_DIR_NAME
    archive_root.mkdir(parents=True, exist_ok=True)
    (archive_root / sid).symlink_to(outside, target_is_directory=True)
    return outside


def test_symlinked_archive_session_dir_is_not_read(tmp_path):
    """A symlinked archive session dir must never serve journal data (fail closed)."""
    _symlinked_archive_dir_with_external_run(tmp_path, "s1", "evil")

    assert rj.find_run_summary("evil", session_dir=tmp_path) is None
    assert rj.find_run_file("evil", session_dir=tmp_path) is None
    # `read_run_events`/`latest_run_summary` return an empty shape (not the
    # external rows) when the only copy is behind an untrusted symlink.
    result = rj.read_run_events("s1", "evil", session_dir=tmp_path)
    assert result["events"] == []
    summary = rj.latest_run_summary("s1", "evil", session_dir=tmp_path)
    assert not (summary and summary.get("event_count"))
    assert rj._read_jsonl(tmp_path / rj.RUN_JOURNAL_DIR_NAME / "s1" / "evil.jsonl")[0] == []


def test_symlinked_archive_session_dir_not_in_replay(tmp_path):
    """Session replay must not include a run reachable only through a symlinked archive dir."""
    _symlinked_archive_dir_with_external_run(tmp_path, "s1", "evil")
    _write_run(tmp_path, "s1", "ok", mtime_age_days=30)

    replay = rj.read_session_run_events("s1", after_event_id="ok:1", session_dir=tmp_path)
    run_ids = {event.get("run_id") for event in replay.get("events", [])}
    assert "evil" not in run_ids


def test_archive_read_falls_back_when_live_path_is_symlinked(tmp_path):
    """A legitimate archive still reads when the ARCHIVE dir is a real directory."""
    _write_run(tmp_path, "s1", "r1", mtime_age_days=30)
    _sweep(tmp_path, ttl_days=14, max_runs_per_session=0, max_bytes_per_session=0)
    assert _archive_path(tmp_path, "s1", "r1").exists()

    summary = rj.latest_run_summary("s1", "r1", session_dir=tmp_path)
    assert summary is not None and summary.get("run_id") == "r1"


def test_archive_read_race_does_not_leak_external_file(tmp_path, monkeypatch):
    """A swap between the containment check and the open must not leak data.

    Reproduces the review finding: `_archive_read_allowed` validated the path,
    then `gzip.open()` re-resolved that mutable pathname — a symlink planted in
    between made the read follow it out of the archive tree. Reads now open
    through pinned directory handles (`O_NOFOLLOW` + `dir_fd`), so the swap
    cannot redirect the read.
    """
    _write_run(tmp_path, "s1", "r1", mtime_age_days=30)
    _sweep(tmp_path, ttl_days=14, max_runs_per_session=0, max_bytes_per_session=0)
    archive = _archive_path(tmp_path, "s1", "r1")
    assert archive.exists()

    # External gzip with a sentinel payload, then the attacker's swap.
    external = tmp_path.parent / "external-evil.jsonl.gz"
    with gzip.open(external, "wb") as fh:
        fh.write(_external_row_bytes("s1", "r1"))

    real_open = rj._open_archive_entry
    swap = {"done": False}

    def open_with_swap(path):
        if not swap["done"] and Path(path) == archive:
            swap["done"] = True
            archive.unlink()
            archive.symlink_to(external)
        return real_open(path)

    monkeypatch.setattr(rj, "_open_archive_entry", open_with_swap)
    text = rj._read_gz_text(archive)
    monkeypatch.undo()

    assert text is None or "EXTERNAL" not in text, "external file was served as archive data"


def test_archive_read_race_via_run_events_does_not_leak(tmp_path, monkeypatch):
    """The same swap must not leak through the read_run_events path either."""
    _write_run(tmp_path, "s1", "r1", mtime_age_days=30)
    _sweep(tmp_path, ttl_days=14, max_runs_per_session=0, max_bytes_per_session=0)
    archive = _archive_path(tmp_path, "s1", "r1")

    external = tmp_path.parent / "external-evil2.jsonl.gz"
    with gzip.open(external, "wb") as fh:
        fh.write(_external_row_bytes("s1", "r1"))

    real_open = rj._open_archive_entry
    swap = {"done": False}

    def open_with_swap(path):
        if not swap["done"] and Path(path) == archive:
            swap["done"] = True
            archive.unlink()
            archive.symlink_to(external)
        return real_open(path)

    monkeypatch.setattr(rj, "_open_archive_entry", open_with_swap)
    result = rj.read_run_events("s1", "r1", session_dir=tmp_path)
    monkeypatch.undo()

    payloads = [json.dumps(event.get("payload", {})) for event in result["events"]]
    assert not any("EXTERNAL" in p for p in payloads)


def _external_row_bytes(sid: str, rid: str) -> bytes:
    return (
        json.dumps(
            {
                "version": 1,
                "event_id": f"{rid}:99",
                "seq": 99,
                "run_id": rid,
                "session_id": sid,
                "event": "token",
                "type": "token",
                "created_at": time.time(),
                "terminal": True,
                "terminal_state": "completed",
                "payload": {"text": "EXTERNAL"},
            },
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


# ── archive root must never be followed (sweep / delete / prune) ────────────


def _symlink_archive_root(root: Path, external: Path) -> None:
    """Point ``_run_journal_archive`` at an EXTERNAL directory (symlink root)."""
    external.mkdir(parents=True, exist_ok=True)
    (root / rj.RUN_JOURNAL_ARCHIVE_DIR_NAME).symlink_to(external, target_is_directory=True)


def test_sweep_skips_archival_when_archive_root_is_symlinked(tmp_path):
    """A symlinked archive ROOT must not receive archives or lose the live file.

    Following it would move the run outside the journal tree — where the
    (correctly) containment-checked readers refuse it — so recovery would
    silently see zero events for a run whose live file was removed.
    """
    path = _write_run(tmp_path, "s1", "r1", mtime_age_days=30)
    external = tmp_path.parent / f"{tmp_path.name}-external-root"
    _symlink_archive_root(tmp_path, external)

    counters = _sweep(tmp_path, ttl_days=14, max_runs_per_session=0, max_bytes_per_session=0)

    assert counters["archived_files"] == 0, "archived into a symlinked root"
    assert path.exists(), "live file removed although the archive root was untrusted"
    assert list(external.rglob("*.jsonl.gz")) == [], "files written into the external dir"
    # Recovery still works: the run is live and fully readable.
    events = rj.read_run_events("s1", "r1", session_dir=tmp_path)
    assert len(events["events"]) > 0


def test_delete_run_journal_does_not_follow_symlinked_archive_root(tmp_path):
    """Session deletion must not remove a foreign directory via a symlinked root."""
    _write_run(tmp_path, "s1", "r1", mtime_age_days=30)
    external = tmp_path.parent / f"{tmp_path.name}-external-del"
    (external / "s1").mkdir(parents=True)
    precious = external / "s1" / "PRECIOUS.txt"
    precious.write_text("must survive", encoding="utf-8")
    _symlink_archive_root(tmp_path, external)

    rj.delete_run_journal("s1", session_dir=tmp_path)

    assert precious.exists(), "external file deleted through a symlinked archive root"
    assert (external / "s1").is_dir()


def test_prune_does_not_follow_symlinked_archive_root(tmp_path, monkeypatch):
    """Archive pruning must not delete files outside the journal tree."""
    _write_run(tmp_path, "s1", "keep", mtime_age_days=0)
    external = tmp_path.parent / f"{tmp_path.name}-external-prune"
    (external / "s1").mkdir(parents=True)
    victim = external / "s1" / "victim.jsonl.gz"
    with gzip.open(victim, "wb") as fh:
        fh.write(b'{"version":1}\n')
    old = time.time() - 400 * 86400
    os.utime(victim, (old, old))
    _symlink_archive_root(tmp_path, external)

    monkeypatch.setenv(rj._RETENTION_ARCHIVE_TTL_ENV, "90")
    counters = _sweep(tmp_path, ttl_days=0, max_runs_per_session=0, max_bytes_per_session=0)
    monkeypatch.undo()

    assert counters["pruned_archives"] == 0
    assert victim.exists(), "external archive pruned through a symlinked root"


def test_prune_skips_symlinked_session_dir_inside_real_root(tmp_path, monkeypatch):
    """A symlinked SESSION dir inside a real archive root is not pruned through."""
    _write_run(tmp_path, "s1", "keep", mtime_age_days=0)
    external = tmp_path.parent / f"{tmp_path.name}-external-session"
    external.mkdir(parents=True, exist_ok=True)
    victim = external / "victim.jsonl.gz"
    with gzip.open(victim, "wb") as fh:
        fh.write(b'{"version":1}\n')
    old = time.time() - 400 * 86400
    os.utime(victim, (old, old))
    archive_root = tmp_path / rj.RUN_JOURNAL_ARCHIVE_DIR_NAME
    archive_root.mkdir(parents=True, exist_ok=True)
    (archive_root / "s2").symlink_to(external, target_is_directory=True)

    monkeypatch.setenv(rj._RETENTION_ARCHIVE_TTL_ENV, "90")
    counters = _sweep(tmp_path, ttl_days=0, max_runs_per_session=0, max_bytes_per_session=0)
    monkeypatch.undo()

    assert counters["pruned_archives"] == 0
    assert victim.exists(), "pruned through a symlinked session dir"


# ── durability: a failed fsync must keep the live file ─────────────────────


def test_failed_archive_dir_fsync_keeps_live_file(tmp_path, monkeypatch):
    """The live file is the only copy until the archive is durably synced.

    Reproduces the gate finding: a failed archive-directory fsync was ignored
    and the live file was then removed, so a crash at that point could lose the
    run's only durable copy.
    """
    path = _write_run(tmp_path, "s1", "r1", mtime_age_days=30)
    # Pre-create the archive dir so the dir-chain sync is not the thing tested.
    (tmp_path / rj.RUN_JOURNAL_ARCHIVE_DIR_NAME / "s1").mkdir(parents=True)

    real_fsync = os.fsync

    def failing_dir_fsync(fd):
        import stat as _stat

        if _stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError("injected dir fsync failure")
        return real_fsync(fd)

    monkeypatch.setattr(rj.os, "fsync", failing_dir_fsync)
    counters = _sweep(tmp_path, ttl_days=14, max_runs_per_session=0, max_bytes_per_session=0)
    monkeypatch.undo()

    assert path.exists(), "live file removed although the archive fsync failed"
    assert counters["archived_files"] == 0
    # The run is still fully readable from the live copy.
    events = rj.read_run_events("s1", "r1", session_dir=tmp_path)
    assert len(events["events"]) > 0


# ── late publication / un-synced root must not lose or strand data ──────────


def test_delete_removes_archive_published_during_deletion(tmp_path, monkeypatch):
    """A publication racing the deletion must not survive it.

    Reproduces the finding: deletion listed the archive directory once, so an
    entry published after that snapshot was not seen, the final rmdir failed on
    the non-empty directory, and the error was suppressed — leaving a recoverable
    transcript behind after the session was deleted.
    """
    _write_run(tmp_path, "s1", "r1", mtime_age_days=30)
    _sweep(tmp_path, ttl_days=14, max_runs_per_session=0, max_bytes_per_session=0)
    arch_dir = tmp_path / rj.RUN_JOURNAL_ARCHIVE_DIR_NAME / "s1"
    assert arch_dir.is_dir()

    real_listdir = os.listdir
    state = {"fired": False}

    def listdir_publishing_late(fd):
        names = real_listdir(fd)
        if not state["fired"]:
            state["fired"] = True
            with gzip.open(arch_dir / "late.jsonl.gz", "wb") as fh:
                fh.write(b"late publication")
        return names

    monkeypatch.setattr(rj.os, "listdir", listdir_publishing_late)
    rj.delete_run_journal("s1", session_dir=tmp_path)
    monkeypatch.undo()

    assert state["fired"], "the race was never injected"
    survivors = [p for p in arch_dir.rglob("*")] if arch_dir.exists() else []
    assert survivors == [], f"recoverable transcript left behind: {survivors}"


def test_un_synced_new_archive_root_keeps_live_file(tmp_path, monkeypatch):
    """A freshly created archive root whose PARENT cannot be synced keeps the live file.

    Reproduces the finding: the parent fsync that makes a new
    ``_run_journal_archive`` name durable was ignored, so a crash after the live
    unlink could lose the root's directory entry and its whole subtree —
    including the run's only remaining copy.
    """
    path = _write_run(tmp_path, "s1", "r1", mtime_age_days=30)
    # tmp_path itself plays the parent of a root that does not exist yet.
    parent_ino = os.stat(tmp_path).st_ino

    real_fsync = os.fsync

    def fail_parent_fsync(fd):
        st = os.fstat(fd)
        import stat as _stat

        if _stat.S_ISDIR(st.st_mode) and st.st_ino == parent_ino:
            raise OSError("injected parent-of-root fsync failure")
        return real_fsync(fd)

    monkeypatch.setattr(rj.os, "fsync", fail_parent_fsync)
    counters = _sweep(tmp_path, ttl_days=14, max_runs_per_session=0, max_bytes_per_session=0)
    monkeypatch.undo()

    assert path.exists(), "live file removed although the new root's name was never synced"
    assert counters["archived_files"] == 0
    events = rj.read_run_events("s1", "r1", session_dir=tmp_path)
    assert len(events["events"]) > 0


def test_delete_does_not_wait_on_the_global_sweep_pass(tmp_path):
    """Deletion coordinates per-session, not with the whole (long) sweep pass.

    The sweep holds the global lock across every session — up to 512 MiB of
    compression and pruning — and deletion runs synchronously on the request
    path, so taking the global lock here could stall a delete request behind
    unrelated sessions' work. On the pre-fix code this does not just stall: the
    deletion BLOCKS on the held global lock, so the delete is asserted from a
    worker thread with a join timeout (a hang is the failure symptom, but a
    hanging test is a poor signal).
    """
    _write_run(tmp_path, "s1", "r1", mtime_age_days=30)

    # Simulate a sweep mid-pass over unrelated sessions.
    rj._SWEEP_RUN_LOCK.acquire()
    try:
        result: list = []

        def _delete():
            result.append(rj.delete_run_journal("s1", session_dir=tmp_path))

        worker = threading.Thread(target=_delete, daemon=True)
        worker.start()
        worker.join(timeout=5.0)
        blocked = worker.is_alive()
    finally:
        rj._SWEEP_RUN_LOCK.release()

    assert not blocked, "deletion blocked on the global sweep pass (would stall the request path)"
    assert result == [True]
    assert not (tmp_path / rj.RUN_JOURNAL_DIR_NAME / "s1").exists()


def test_sweep_skips_session_whose_deletion_is_in_flight(tmp_path):
    """A session being deleted is skipped by the pass instead of raced."""
    _write_run(tmp_path, "s1", "r1", mtime_age_days=30)
    _write_run(tmp_path, "s2", "r2", mtime_age_days=30)

    session_lock = rj._session_lock_for(tmp_path, "s1")
    session_lock.acquire()
    try:
        counters = _sweep(tmp_path, ttl_days=14, max_runs_per_session=0, max_bytes_per_session=0)
    finally:
        session_lock.release()

    # s1 was skipped (nothing archived for it); s2 was still swept.
    assert not _archive_path(tmp_path, "s1", "r1").exists()
    assert _archive_path(tmp_path, "s2", "r2").exists()
    assert counters["archived_files"] == 1


# ── live deletion containment + lock-registry lifecycle ─────────────────────


def test_live_deletion_does_not_follow_symlinked_journal_root(tmp_path):
    """Deletion must not follow a symlinked ``_run_journal`` out of the tree.

    Reproduces the finding: deletion checked the mutable live path and then
    handed it to ``shutil.rmtree``, so with ``_run_journal`` replaced by a
    symlink the recursive delete resolved the pathname again and destroyed a
    foreign tree (in the probe: an external ``s1/PRECIOUS.txt`` was deleted).
    The pinned, no-follow implementation refuses the symlinked root and leaves
    the external tree alone.
    """
    _write_run(tmp_path, "s1", "r1", mtime_age_days=0)
    # Replace the journal root with a symlink to an external tree that contains
    # a decoy "s1" directory holding data that must survive.
    external = tmp_path.parent / f"{tmp_path.name}-external-jroot"
    (external / "s1").mkdir(parents=True)
    precious = external / "s1" / "PRECIOUS.txt"
    precious.write_text("must survive", encoding="utf-8")
    journal_root = tmp_path / rj.RUN_JOURNAL_DIR_NAME
    shutil.rmtree(journal_root, ignore_errors=True)
    journal_root.symlink_to(external, target_is_directory=True)

    result = rj.delete_run_journal("s1", session_dir=tmp_path)

    assert result is False, "deletion claimed success through a symlinked journal root"
    assert precious.exists(), "external file deleted through a symlinked journal root"
    assert (external / "s1").is_dir(), "external directory deleted"


def test_session_lock_registry_does_not_grow_without_bound(tmp_path):
    """The per-session lock registry must not retain entries forever.

    Reproduces the finding: `_session_lock_for` stored a strong reference for
    every session the sweep or a deletion ever touched, so ongoing session churn
    grew the registry without bound. The registry now holds weak references, so
    an entry with no live user is dropped automatically.
    """
    import gc

    for i in range(50):
        lock = rj._session_lock_for(tmp_path, f"s{i}")
        assert isinstance(lock, type(__import__("threading").Lock()))
        del lock
    gc.collect()

    remaining = len(rj._SESSION_LOCKS)
    assert remaining == 0, f"registry retained {remaining} dead locks"


def test_session_lock_registry_keeps_live_lock_alive(tmp_path):
    """A lock still in use must not be collected (it must stay the same object)."""
    held = rj._session_lock_for(tmp_path, "s1")
    again = rj._session_lock_for(tmp_path, "s1")
    assert held is again, "an in-use lock was replaced — mutual exclusion would break"


def test_deletion_still_removes_transcripts_without_pinned_handles(tmp_path, monkeypatch):
    """On no-pin platforms (Windows) deletion must still remove the transcripts.

    Reproduces the finding: the pinned implementation returns False when
    ``dir_fd``/``O_NOFOLLOW`` are unavailable, and the delete route discards that
    result — so a deleted session kept its recoverable run transcripts on disk.
    Privacy must fail CLOSED here (remove the tree), not open (leave it).

    The platform is simulated the way the module itself distinguishes it: no
    pinned directory opens are available (on Windows, ``os.open`` cannot open a
    directory at all, so ``_open_dir_no_follow`` yields nothing), and the
    ``_DIR_FD_OK`` capability flag is off. NOTE: the module's ``O_*`` constants
    are NOT zeroed — on a POSIX host that still opens successfully and would
    test the pinned path, not the fallback.
    """
    path = _write_run(tmp_path, "s1", "r1", mtime_age_days=0)
    session_dir = path.parent

    monkeypatch.setattr(rj, "_DIR_FD_OK", False)
    monkeypatch.setattr(rj, "_open_dir_no_follow", lambda _path: None)

    result = rj.delete_run_journal("s1", session_dir=tmp_path)

    assert result is True, "deletion reported failure on a no-pin platform"
    assert not session_dir.exists(), "recoverable transcripts left on disk"


def test_deletion_refuses_symlinked_root_without_pinning_available(tmp_path, monkeypatch):
    """The no-pin fallback still refuses a symlinked journal root."""
    _write_run(tmp_path, "s1", "r1", mtime_age_days=0)
    external = tmp_path.parent / f"{tmp_path.name}-external-fallback"
    (external / "s1").mkdir(parents=True)
    precious = external / "s1" / "PRECIOUS.txt"
    precious.write_text("must survive", encoding="utf-8")
    journal_root = tmp_path / rj.RUN_JOURNAL_DIR_NAME
    shutil.rmtree(journal_root, ignore_errors=True)
    journal_root.symlink_to(external, target_is_directory=True)

    monkeypatch.setattr(rj, "_DIR_FD_OK", False)
    monkeypatch.setattr(rj, "_open_dir_no_follow", lambda _path: None)

    result = rj.delete_run_journal("s1", session_dir=tmp_path)

    assert result is False
    assert precious.exists(), "fallback followed a symlinked root"


def test_fallback_deletion_never_gives_a_checked_path_to_rmtree(tmp_path, monkeypatch):
    """The no-pin fallback must not hand a checked pathname to a recursive delete.

    Reproduces the finding: the fallback checked ``journal_root``/``target`` for
    symlinks and then passed the same mutable pathname to ``shutil.rmtree``, so
    a swap landing between the two (journal root re-pointed at an external tree
    holding a decoy ``s1/``) redirected the recursive delete outside the journal
    tree and destroyed the external data. The claim-first implementation never
    calls ``shutil.rmtree`` on a checked path at all.
    """
    path = _write_run(tmp_path, "s1", "r1", mtime_age_days=0)
    session_dir = path.parent
    external = tmp_path.parent / f"{tmp_path.name}-external-claim"
    (external / "s1").mkdir(parents=True)
    precious = external / "s1" / "PRECIOUS.txt"
    precious.write_text("must survive", encoding="utf-8")
    journal_root = tmp_path / rj.RUN_JOURNAL_DIR_NAME

    rmtree_calls: list[str] = []
    real_rmtree = shutil.rmtree

    def swap_then_rmtree(target, *args, **kwargs):
        # The swap that used to land between the containment checks and the
        # recursive delete.
        rmtree_calls.append(str(target))
        if not journal_root.is_symlink():
            os.rename(journal_root, tmp_path / "_run_journal-moved")
            journal_root.symlink_to(external, target_is_directory=True)
        return real_rmtree(target, *args, **kwargs)

    monkeypatch.setattr(rj, "_DIR_FD_OK", False)
    monkeypatch.setattr(rj, "_open_dir_no_follow", lambda _p: None)
    monkeypatch.setattr(shutil, "rmtree", swap_then_rmtree)

    result = rj.delete_run_journal("s1", session_dir=tmp_path)

    assert precious.exists(), "deletion escaped the journal root"
    assert rmtree_calls == [], f"a checked pathname reached shutil.rmtree: {rmtree_calls}"
    assert result is True, "deletion did not complete for a normal session"
    assert not session_dir.exists(), "transcripts left behind"


def test_fallback_deletion_removes_link_entries_without_following_them(tmp_path, monkeypatch):
    """Links inside the tree are removed as entries; their targets are untouched.

    Guard for the no-pin fallback's entry-level semantics: a link entry is
    removed as the ENTRY itself (its target is never entered) and the rest of
    the tree is still cleared. Passes on the pre-fix implementation too
    (``rmtree`` also unlinks links as entries) — kept so a future rewrite that
    recurses through link targets fails here.
    """
    path = _write_run(tmp_path, "s1", "r1", mtime_age_days=0)
    session_dir = path.parent
    external = tmp_path.parent / f"{tmp_path.name}-external-inner"
    external.mkdir(exist_ok=True)
    precious = external / "PRECIOUS.txt"
    precious.write_text("must survive", encoding="utf-8")
    (session_dir / "linked").symlink_to(external, target_is_directory=True)

    monkeypatch.setattr(rj, "_DIR_FD_OK", False)
    monkeypatch.setattr(rj, "_open_dir_no_follow", lambda _p: None)

    result = rj.delete_run_journal("s1", session_dir=tmp_path)

    assert result is True, "deletion left the session tree behind"
    assert not session_dir.exists()
    assert precious.exists(), "deletion followed a link out of the tree"
    assert external.is_dir()


def test_fallback_deletion_finishes_a_claim_left_by_a_crash(tmp_path, monkeypatch):
    """A claim left by an interrupted deletion is cleared on the next attempt.

    The claim-first fallback renames the session entry before clearing it; an
    interruption between those steps leaves the tree under the private claim
    name. The next deletion for the same session must finish the job (the claim
    is, by construction, this module's own debris) rather than leaving
    recoverable transcripts behind.
    """
    path = _write_run(tmp_path, "s1", "r1", mtime_age_days=0)
    session_dir = path.parent
    stale = session_dir.parent / ".s1.delete-claim.999.deadbeef"
    os.rename(session_dir, stale)

    monkeypatch.setattr(rj, "_DIR_FD_OK", False)
    monkeypatch.setattr(rj, "_open_dir_no_follow", lambda _p: None)

    result = rj.delete_run_journal("s1", session_dir=tmp_path)

    assert result is True
    assert not stale.exists(), "claim debris still holds the transcripts"
    assert not session_dir.exists()


# ── sweep root containment (CORE 1) + no-pin root swap (CORE 2) ─────────────


def test_sweep_does_not_follow_symlinked_journal_root(tmp_path):
    """A symlinked ``_run_journal`` root must not be swept into another tree.

    Reproduces the finding: the sweep resolved the root with ``realpath`` (which
    hides the symlink) and enumerated through the pathname, so a root pointing
    at another tree's journal had THAT tree's runs archived into the local
    archive - the foreign original was removed and its owner could read zero
    events. The root is now opened with ``O_NOFOLLOW`` and everything enumerates
    through the pinned handle; a root that is not a real directory is refused.
    """
    victim_root = tmp_path / "victim" / "sessions"
    victim = _write_run(victim_root, "sess-vic", "runvic", mtime_age_days=40)
    sweeper = tmp_path / "sweeper" / "sessions"
    sweeper.mkdir(parents=True)
    (sweeper / rj.RUN_JOURNAL_DIR_NAME).symlink_to(
        victim_root / rj.RUN_JOURNAL_DIR_NAME, target_is_directory=True
    )

    counters = _sweep(sweeper, ttl_days=7, max_runs_per_session=0, max_bytes_per_session=0)

    assert victim.exists(), "the sweep archived a foreign tree's run"
    assert counters["archived_files"] == 0
    assert not _archive_path(sweeper, "sess-vic", "runvic").exists()


def test_sweep_root_swap_mid_pass_stays_handle_relative(tmp_path, monkeypatch):
    """A root pathname swapped mid-pass cannot redirect the sweep.

    The root is pinned BEFORE enumeration; after the swap the pass must still
    operate on the original directory (its inode) - archiving ITS runs - and
    never touch the tree the pathname now points at. On the pre-fix code the
    per-session open went through the pathname, so the swapped-in tree's run was
    archived and its original removed.
    """
    path = _write_run(tmp_path, "s1", "r1", mtime_age_days=30)
    original_rows = path.read_text(encoding="utf-8")
    journal_root = tmp_path / rj.RUN_JOURNAL_DIR_NAME

    foreign_journal = tmp_path.parent / f"{tmp_path.name}-foreign-journal"
    (foreign_journal / "s1").mkdir(parents=True)
    foreign_run = foreign_journal / "s1" / "r2.jsonl"
    foreign_rows = original_rows.replace('"r1"', '"r2"').replace("r1:", "r2:")
    foreign_run.write_text(foreign_rows, encoding="utf-8")
    old = time.time() - 40 * 86400
    os.utime(foreign_run, (old, old))

    real_sweep_session = rj._sweep_run_journal_session
    state = {"swapped": False}

    def swap_then_sweep(*args, **kwargs):
        if not state["swapped"]:
            state["swapped"] = True
            os.rename(journal_root, tmp_path / "_run_journal-real-saved")
            journal_root.symlink_to(foreign_journal, target_is_directory=True)
        return real_sweep_session(*args, **kwargs)

    monkeypatch.setattr(rj, "_sweep_run_journal_session", swap_then_sweep)

    _sweep(tmp_path, ttl_days=14, max_runs_per_session=0, max_bytes_per_session=0)

    assert state["swapped"], "test did not trigger the swap"
    assert foreign_run.exists(), "the sweep followed the swapped-in root"
    assert not _archive_path(tmp_path, "s1", "r2").exists()
    assert _archive_path(tmp_path, "s1", "r1").exists(), (
        "the pass did not operate on the pinned original directory"
    )


def test_fallback_deletion_root_swap_restores_entry_and_fails_closed(tmp_path, monkeypatch):
    """A root swapped mid-fallback must not destroy foreign files.

    Reproduces the finding: the no-pin fallback lstat-checked the root and then
    the debris scan walked the pathname again; when a root was swapped in
    between, its debris-shaped entries were deleted while the deletion still
    reported success. Every claim is now identity-verified against the root
    captured before it, and a mismatch restores the entry and fails closed.
    """
    _write_run(tmp_path, "s1", "r1", mtime_age_days=0)
    journal_root = tmp_path / rj.RUN_JOURNAL_DIR_NAME

    foreign = tmp_path.parent / f"{tmp_path.name}-foreign-delete"
    debris = foreign / ".s1.delete-claim.999.cafebabe"
    debris.mkdir(parents=True)
    precious = debris / "PRECIOUS.txt"
    precious.write_text("must survive", encoding="utf-8")
    (foreign / "s1").mkdir()

    real_scandir = os.scandir
    state = {"swapped": False}

    def swapping_scandir(path, *args, **kwargs):
        if not state["swapped"] and str(path) == str(journal_root):
            state["swapped"] = True
            os.rename(journal_root, tmp_path / "_run_journal-real-saved")
            journal_root.symlink_to(foreign, target_is_directory=True)
        return real_scandir(path, *args, **kwargs)

    monkeypatch.setattr(rj, "_DIR_FD_OK", False)
    monkeypatch.setattr(rj, "_open_dir_no_follow", lambda _p: None)
    monkeypatch.setattr(os, "scandir", swapping_scandir)

    result = rj.delete_run_journal("s1", session_dir=tmp_path)

    assert state["swapped"], "test did not trigger the swap"
    assert precious.exists(), "the fallback destroyed a foreign entry"
    assert result is False, "deletion falsely reported success after a root swap"


# ── TTL pruning must not orphan a run's live suffix (CORE 3) ────────────────


def test_prune_preserves_archives_when_live_session_is_uninspectable(tmp_path, monkeypatch):
    """An un-openable live session must keep its archives (fail closed).

    Reproduces the finding: when the live session entry exists but cannot be
    opened (swapped to a symlink, or unreadable), the prune treated it like an
    ABSENT directory, bypassed the only live-counterpart check, and unlinked an
    aged archive whose run may still have a live suffix. An unknown live state
    must preserve the archive instead.
    """
    sid, rid = "s1", "r1"
    archive_dir = tmp_path / rj.RUN_JOURNAL_ARCHIVE_DIR_NAME / sid
    archive_dir.mkdir(parents=True)
    archive = archive_dir / f"{rid}.jsonl.gz"
    with gzip.open(archive, "wb") as gz:
        gz.write(b'{"version":1}\n')
    old = time.time() - 400 * 86400
    os.utime(archive, (old, old))

    # Live session replaced by a symlink: exists, but not openable.
    live_root = tmp_path / rj.RUN_JOURNAL_DIR_NAME
    live_root.mkdir(parents=True)
    elsewhere = tmp_path.parent / f"{tmp_path.name}-elsewhere"
    elsewhere.mkdir()
    (live_root / sid).symlink_to(elsewhere, target_is_directory=True)

    monkeypatch.setenv(rj._RETENTION_ARCHIVE_TTL_ENV, "30")
    counters = _sweep(tmp_path, ttl_days=0, max_runs_per_session=0, max_bytes_per_session=0)

    assert archive.exists(), "an un-inspectable live session let the prune delete the archive"
    assert counters["pruned_archives"] == 0


def test_prune_live_check_is_not_redirected_by_swapped_live_root(tmp_path, monkeypatch):
    """The live-counterpart check must not resolve the mutable live-root pathname.

    Reproduces the finding: the prune opened ``_run_journal/<sid>`` through the
    live-root pathname per session, so a root swapped for a symlink between
    checks made the prune inspect the substitute tree, conclude the run had no
    live suffix, and delete the only stored prefix (reads then lost seq 1 and
    replay reported ``replay_noncontiguous``). The live root is now pinned once
    and sessions open relative to that handle; a live root that is present but
    not pinnable keeps every archive (fail closed).
    """
    sid, rid = "s1", "r1"
    archive_dir = tmp_path / rj.RUN_JOURNAL_ARCHIVE_DIR_NAME / sid
    archive_dir.mkdir(parents=True)

    def _row(seq, name, terminal=False):
        return {
            "version": 1, "event_id": f"{rid}:{seq}", "seq": seq, "run_id": rid,
            "session_id": sid, "event": name, "type": name,
            "created_at": time.time() - 3600, "terminal": terminal,
            "terminal_state": "completed" if terminal else None,
            "payload": {"terminal_state": "completed"} if terminal else {"text": "x"},
        }

    # Aged archived prefix (seq 1) + fresh live suffix (seq 2, terminal).
    archive = archive_dir / f"{rid}.jsonl.gz"
    with gzip.open(archive, "wb") as gz:
        gz.write((json.dumps(_row(1, "token"), separators=(",", ":")) + "\n").encode())
    old = time.time() - 400 * 86400
    os.utime(archive, (old, old))
    live_dir = tmp_path / rj.RUN_JOURNAL_DIR_NAME / sid
    live_dir.mkdir(parents=True)
    (live_dir / f"{rid}.jsonl").write_text(
        json.dumps(_row(2, "done", True), separators=(",", ":")) + "\n", encoding="utf-8"
    )

    journal_root = tmp_path / rj.RUN_JOURNAL_DIR_NAME
    foreign = tmp_path.parent / f"{tmp_path.name}-foreign-live"
    foreign.mkdir()

    # Swap the live-root pathname at the moment the prune starts: the sweep has
    # already pinned its root handle, so the pin is unaffected while every
    # pathname-based check inside the prune is redirected.
    real_prune = rj._prune_run_journal_archive
    state = {"swapped": False}

    def swap_then_prune(*args, **kwargs):
        if not state["swapped"]:
            state["swapped"] = True
            os.rename(journal_root, tmp_path / "_run_journal-real-saved")
            journal_root.symlink_to(foreign, target_is_directory=True)
        return real_prune(*args, **kwargs)

    monkeypatch.setattr(rj, "_prune_run_journal_archive", swap_then_prune)
    monkeypatch.setenv(rj._RETENTION_ARCHIVE_TTL_ENV, "30")
    counters = _sweep(tmp_path, ttl_days=0, max_runs_per_session=0, max_bytes_per_session=0)

    assert state["swapped"], "test did not trigger the swap"
    assert archive.exists(), "the swapped live-root redirected the prune"
    assert counters["pruned_archives"] == 0

    # Restore the real tree; reads must be complete.
    journal_root.unlink()
    os.rename(tmp_path / "_run_journal-real-saved", journal_root)
    read = rj.read_run_events(sid, rid, session_dir=tmp_path)
    assert [int(e["seq"]) for e in read["events"]] == [1, 2]
    replay = rj.read_session_run_events(sid, after_event_id=f"{rid}:1", session_dir=tmp_path)
    assert replay["status"] == "ok", replay["status"]


def test_archive_prune_keeps_archive_while_live_suffix_exists(tmp_path, monkeypatch):
    """TTL must not prune a prefix while the same run still has live rows.

    Reproduces the finding: with archive TTL enabled, an aged ``.jsonl.gz`` was
    pruned by age alone even though a NEWER live suffix existed for the same run
    id; event reads silently lost the prefix and session replay went
    ``replay_noncontiguous``. The prune now keeps any archive whose live
    counterpart exists (checked under the run's writer lock).
    """
    sid, rid = "s1", "r1"
    archive_dir = tmp_path / rj.RUN_JOURNAL_ARCHIVE_DIR_NAME / sid
    archive_dir.mkdir(parents=True)

    def _row(seq, name, terminal=False):
        return {
            "version": 1, "event_id": f"{rid}:{seq}", "seq": seq, "run_id": rid,
            "session_id": sid, "event": name, "type": name,
            "created_at": time.time() - 3600, "terminal": terminal,
            "terminal_state": "completed" if terminal else None,
            "payload": {"terminal_state": "completed"} if terminal else {"text": "x"},
        }

    # Archived prefix: seq 1 only, well past the 30-day archive TTL.
    archive = archive_dir / f"{rid}.jsonl.gz"
    with gzip.open(archive, "wb") as gz:
        gz.write((json.dumps(_row(1, "token"), separators=(",", ":")) + "\n").encode())
    old = time.time() - 400 * 86400
    os.utime(archive, (old, old))

    # Live suffix: seqs 2 + 3, freshly written (below every archival cap).
    live_dir = tmp_path / rj.RUN_JOURNAL_DIR_NAME / sid
    live_dir.mkdir(parents=True)
    live = live_dir / f"{rid}.jsonl"
    live.write_text(
        json.dumps(_row(2, "token"), separators=(",", ":")) + "\n"
        + json.dumps(_row(3, "done", True), separators=(",", ":")) + "\n",
        encoding="utf-8",
    )

    monkeypatch.setenv(rj._RETENTION_ARCHIVE_TTL_ENV, "30")
    counters = _sweep(tmp_path, ttl_days=0, max_runs_per_session=0, max_bytes_per_session=0)

    assert archive.exists(), "TTL pruned the prefix while the live suffix was present"
    assert counters["pruned_archives"] == 0
    # Reads stay complete across the archive + live union.
    read = rj.read_run_events(sid, rid, session_dir=tmp_path)
    assert [int(e["seq"]) for e in read["events"]] == [1, 2, 3]
    # SSE replay from the archived prefix stays contiguous.
    replay = rj.read_session_run_events(sid, after_event_id=f"{rid}:1", session_dir=tmp_path)
    assert replay["status"] == "ok", replay["status"]
    assert [int(e["seq"]) for e in replay["events"]] == [2, 3]


# ── ownership: the pinned raw handle must close on every exit path ──────────


def _archived_run_with_held_raw(tmp_path, monkeypatch):
    """Archive one run and return (live_style_path, raws_list, real_open).

    ``raws_list`` receives a STRONG reference to every raw handle opened by
    ``_open_archive_entry``. Keeping the reference alive is the point: it stops
    CPython refcount finalization from masking a missing explicit close.
    """
    _write_run(tmp_path, "s1", "r1", mtime_age_days=30,
               events=[("token", {"text": f"line-{i}"}) for i in range(12)])
    _sweep(tmp_path, ttl_days=14, max_runs_per_session=0, max_bytes_per_session=0)
    assert _archive_path(tmp_path, "s1", "r1").exists()

    raws: list = []
    real_open = rj._open_archive_entry

    def spy_open(path):
        fh = real_open(path)
        if fh is not None:
            raws.append(fh)
        return fh

    monkeypatch.setattr(rj, "_open_archive_entry", spy_open)
    return tmp_path / rj.RUN_JOURNAL_DIR_NAME / "s1" / "r1.jsonl", raws


def test_streaming_archive_read_closes_pinned_handle_on_completion(tmp_path, monkeypatch):
    """Full consumption of the bounded iterator must close the pinned raw handle.

    GzipFile.close() does not close a caller-supplied fileobj, so wrapping a
    pinned descriptor without owning it leaves closure to refcount finalization.
    The holder list keeps the raw handle strongly referenced, so only an
    explicit close can satisfy this.
    """
    path, raws = _archived_run_with_held_raw(tmp_path, monkeypatch)
    lines = list(rj._iter_bounded_raw_jsonl_lines(path, max_bytes=10_000_000))
    monkeypatch.undo()

    assert lines, "iterator yielded nothing"
    assert raws, "_open_archive_entry was not used"
    assert all(raw.closed for raw in raws), "pinned raw handle left open after full consumption"


def test_streaming_archive_read_closes_pinned_handle_on_limit_exception(tmp_path, monkeypatch):
    """A replay-limit ValueError mid-iteration must still close the raw handle."""
    path, raws = _archived_run_with_held_raw(tmp_path, monkeypatch)
    with pytest.raises(ValueError):
        list(rj._iter_bounded_raw_jsonl_lines(path, max_bytes=16))
    monkeypatch.undo()

    assert raws, "_open_archive_entry was not used"
    assert all(raw.closed for raw in raws), "pinned raw handle left open after replay-limit raise"


def test_streaming_archive_read_closes_pinned_handle_on_generator_close(tmp_path, monkeypatch):
    """Abandoning the generator (close() without exhaustion) must close the raw handle."""
    path, raws = _archived_run_with_held_raw(tmp_path, monkeypatch)
    iterator = rj._iter_bounded_raw_jsonl_lines(path, max_bytes=10_000_000)
    next(iterator)
    iterator.close()
    monkeypatch.undo()

    assert raws, "_open_archive_entry was not used"
    assert all(raw.closed for raw in raws), "pinned raw handle left open after generator close"


# ── pruning: a replacement archive must never be pruned ─────────────────────


def test_archive_pruning_does_not_delete_replacement_archive(tmp_path, monkeypatch):
    """Pruning claims the entry: a replacement published mid-prune survives.

    Reproduces the PR finding: the prune stat()ed an entry's age, then unlinked
    the mutable NAME later. A writer that republished that name between the two
    steps lost its newly written archive (the only retained copy). The fix
    claims the entry by rename, verifies the claim, and on a mismatch restores
    it without clobbering the canonical name.
    """
    _write_run(tmp_path, "s1", "r1", mtime_age_days=800)
    _sweep(tmp_path, ttl_days=14, max_runs_per_session=0, max_bytes_per_session=0)
    archive = _archive_path(tmp_path, "s1", "r1")
    assert archive.exists()
    old = time.time() - 400 * 86400
    os.utime(archive, (old, old))

    # Simulate the race in the widest window: replace the entry at the moment
    # the prune CLAIMS it (after the age check, before the identity verify).
    fresh_body = b"FRESH REPLACEMENT ARCHIVE"
    real_rename = os.rename
    swapped = {"done": False}

    def swap_then_claim(src, dst, **kwargs):
        if not swapped["done"] and isinstance(src, str) and src.endswith(".jsonl.gz"):
            swapped["done"] = True
            archive.unlink()  # the aged entry the checker saw
            archive.write_bytes(fresh_body)  # a NEW archive published at that name
        return real_rename(src, dst, **kwargs)

    monkeypatch.setattr(rj.os, "rename", swap_then_claim)
    monkeypatch.setenv(rj._RETENTION_ARCHIVE_TTL_ENV, "90")
    counters = rj.sweep_run_journal(
        session_dir=tmp_path, ttl_days=0, max_runs_per_session=0, max_bytes_per_session=0
    )
    monkeypatch.undo()

    assert archive.exists(), "replacement archive was deleted"
    assert archive.read_bytes() == fresh_body
    assert counters["pruned_archives"] == 0
    # No claim debris left behind.
    claims = list(archive.parent.glob("*.prune-claim.*"))
    assert claims == []