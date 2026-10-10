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
  * the only destructive step — archive pruning — is disabled by default;
  * a writer in ANOTHER process is never raced: the sweep serializes on the
    same cross-process journal lock and keeps the file when the writer commits,
    and a platform without a lock backend sweeps nothing;
  * appends after archival continue from the durable maximum (also across a
    restart), so archive -> append -> restart -> prune keeps every row;
  * an explicitly invalid retention value DISABLES its cap instead of falling
    through, and terminal classification requires the writer's own metadata.
"""
import gzip
import json
import multiprocessing
import os
import shutil
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor as _ThreadPoolExecutor
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from conftest import requires_fork

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


def test_second_sweep_keeps_archive_prefix_and_live_suffix(tmp_path):
    """A separate live suffix must never replace the stored archive prefix.

    Reproduces the finding: the publish step assumed any existing archive was a
    byte prefix of the live bytes, so after a run was archived and then appended
    to again at the same path, a second sweep replaced the archive with the
    compressed SUFFIX — events [1, 2, 3] read back as [3] and session replay
    returned `replay_noncontiguous`. The publish step now replaces the archive
    only with a VERIFIED superset (a byte-exact prefix relation); a separate
    suffix keeps BOTH copies, and the readers union them.
    """
    sid, rid = "s1", "r1"

    def _row(seq, name, terminal=False):
        return {
            "version": 1, "event_id": f"{rid}:{seq}", "seq": seq, "run_id": rid,
            "session_id": sid, "event": name, "type": name,
            "created_at": time.time() - 3600, "terminal": terminal,
            "terminal_state": "completed" if terminal else None,
            "payload": {"terminal_state": "completed"} if terminal else {"text": "x"},
        }

    # Archived prefix: seqs 1 + 2.
    archive_dir = tmp_path / rj.RUN_JOURNAL_ARCHIVE_DIR_NAME / sid
    archive_dir.mkdir(parents=True)
    archive = archive_dir / f"{rid}.jsonl.gz"
    with gzip.open(archive, "wb") as gz:
        gz.write(
            (json.dumps(_row(1, "token"), separators=(",", ":")) + "\n").encode()
            + (json.dumps(_row(2, "token"), separators=(",", ":")) + "\n").encode()
        )

    # Live file re-created with the new SUFFIX only (seq 3) and old enough to
    # be archival-eligible.
    live_dir = tmp_path / rj.RUN_JOURNAL_DIR_NAME / sid
    live_dir.mkdir(parents=True)
    live = live_dir / f"{rid}.jsonl"
    live.write_text(
        json.dumps(_row(3, "done", True), separators=(",", ":")) + "\n", encoding="utf-8"
    )
    old = time.time() - 30 * 86400
    os.utime(live, (old, old))

    counters = _sweep(tmp_path, ttl_days=14, max_runs_per_session=0, max_bytes_per_session=0)

    assert counters["archived_files"] == 0, "the live suffix replaced the archive's prefix"
    with gzip.open(archive, "rb") as fh:
        kept = fh.read().decode("utf-8")
    assert f"{rid}:1" in kept and f"{rid}:2" in kept, "the archived prefix was clobbered"
    assert live.exists(), "the live suffix was dropped"
    read = rj.read_run_events(sid, rid, session_dir=tmp_path)
    assert [int(e["seq"]) for e in read["events"]] == [1, 2, 3]
    replay = rj.read_session_run_events(sid, after_event_id=f"{rid}:1", session_dir=tmp_path)
    assert replay["status"] == "ok", replay["status"]
    assert [int(e["seq"]) for e in replay["events"]] == [2, 3]


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


def test_completed_archive_with_tmp_like_run_id_is_not_cleaned(tmp_path):
    """Temp cleanup must match the FULL temp shape, not a ``.gz.tmp.`` substring.

    Reproduces the finding: cleanup tested ``".gz.tmp." in name``, which also
    matched a COMPLETED archive whose run id itself contains ``.gz.tmp.``
    (e.g. run ``r.gz.tmp.x`` -> ``r.gz.tmp.x.jsonl.gz``); the next sweep
    deleted its only copy. Generated ids are UUID hex so this mostly bites
    imported/persisted dotted ids, but the cleanup now matches the exact temp
    shape (leading dot + run segment + ``.jsonl.gz.tmp.`` + digits).
    """
    _write_run(tmp_path, "s1", "r1", mtime_age_days=30)
    archive_session = tmp_path / rj.RUN_JOURNAL_ARCHIVE_DIR_NAME / "s1"
    archive_session.mkdir(parents=True)

    # A COMPLETED archive whose run id merely contains ".gz.tmp.".
    completed = archive_session / "r.gz.tmp.x.jsonl.gz"
    with gzip.open(completed, "wb") as gz:
        gz.write(b"PRECIOUS\n")
    # A genuine stray temp (the exact shape _archive_run_file writes).
    stray = archive_session / ".r1.jsonl.gz.tmp.999999"
    stray.write_bytes(b"partial")

    _sweep(tmp_path, ttl_days=14, max_runs_per_session=0, max_bytes_per_session=0)

    assert completed.exists(), "a completed archive was deleted as temp debris"
    with gzip.open(completed, "rb") as fh:
        assert fh.read() == b"PRECIOUS\n"
    assert not stray.exists(), "a genuine stray temp was not cleaned"


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


def test_prune_keeps_archive_when_live_counterpart_stat_fails(tmp_path, monkeypatch):
    """An unreadable live counterpart must keep its archive (fail closed).

    Reproduces the finding: the live-counterpart check folded EVERY OSError
    into "absent", so an injected PermissionError on the stat read as "no live
    file" and the archive TTL pruned archived event 1 while live event 2 was
    still present. Only a confirmed FileNotFoundError may read as absent; any
    other error is an unknown live state and preserves the archive.
    """
    sid, rid = "s1", "r1"

    def _row(seq, name, terminal=False):
        return {
            "version": 1, "event_id": f"{rid}:{seq}", "seq": seq, "run_id": rid,
            "session_id": sid, "event": name, "type": name,
            "created_at": time.time() - 3600, "terminal": terminal,
            "terminal_state": "completed" if terminal else None,
            "payload": {"terminal_state": "completed"} if terminal else {"text": "x"},
        }

    archive_dir = tmp_path / rj.RUN_JOURNAL_ARCHIVE_DIR_NAME / sid
    archive_dir.mkdir(parents=True)
    archive = archive_dir / f"{rid}.jsonl.gz"
    with gzip.open(archive, "wb") as gz:
        gz.write((json.dumps(_row(1, "token"), separators=(",", ":")) + "\n").encode())
    old = time.time() - 400 * 86400
    os.utime(archive, (old, old))

    # Control: a second aged archive with NO live counterpart is still pruned.
    control = archive_dir / "r2.jsonl.gz"
    with gzip.open(control, "wb") as gz:
        gz.write(b'{"version":1}\n')
    os.utime(control, (old, old))

    # Live counterpart for r1 (event 2), freshly written (below every cap).
    live_dir = tmp_path / rj.RUN_JOURNAL_DIR_NAME / sid
    live_dir.mkdir(parents=True)
    live = live_dir / f"{rid}.jsonl"
    live.write_text(
        json.dumps(_row(2, "done", True), separators=(",", ":")) + "\n", encoding="utf-8"
    )

    real_stat = os.stat

    def flaky_stat(path="", *args, **kwargs):
        if path == f"{rid}.jsonl" and kwargs.get("dir_fd") is not None:
            raise PermissionError("injected: live counterpart unreadable")
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(rj.os, "stat", flaky_stat)
    monkeypatch.setenv(rj._RETENTION_ARCHIVE_TTL_ENV, "30")
    counters = rj.sweep_run_journal(
        session_dir=tmp_path, ttl_days=0, max_runs_per_session=0, max_bytes_per_session=0
    )

    assert archive.exists(), "a PermissionError on the live counterpart let the prune delete the archive"
    assert counters["pruned_archives"] == 1, "the control archive without a live counterpart must still prune"
    assert not control.exists()
    assert live.exists()
    read = rj.read_run_events(sid, rid, session_dir=tmp_path)
    assert [int(e["seq"]) for e in read["events"]] == [1, 2]


def test_prune_sees_live_suffix_created_after_the_session_probe(tmp_path, monkeypatch):
    """A live session created mid-pass must block the prune (fail closed).

    Reproduces the finding: the prune captured each session's live-directory
    handle ONCE per session (outside the writer lock). When the session had no
    live directory at that moment, a writer that created it — and appended a
    suffix — before the per-run check was invisible, and the archived prefix
    was pruned (`replay_noncontiguous`). The state is now read INSIDE the
    writer lock, where the writer's mkdir+append is visible.
    """
    sid, rid = "s1", "r1"

    def _row(seq, name, terminal=False):
        return {
            "version": 1, "event_id": f"{rid}:{seq}", "seq": seq, "run_id": rid,
            "session_id": sid, "event": name, "type": name,
            "created_at": time.time() - 3600, "terminal": terminal,
            "terminal_state": "completed" if terminal else None,
            "payload": {"terminal_state": "completed"} if terminal else {"text": "x"},
        }

    # Aged archived prefix: seqs 1 + 2.
    archive_dir = tmp_path / rj.RUN_JOURNAL_ARCHIVE_DIR_NAME / sid
    archive_dir.mkdir(parents=True)
    archive = archive_dir / f"{rid}.jsonl.gz"
    with gzip.open(archive, "wb") as gz:
        gz.write((json.dumps(_row(1, "token"), separators=(",", ":")) + "\n").encode()
                 + (json.dumps(_row(2, "token"), separators=(",", ":")) + "\n").encode())
    old = time.time() - 400 * 86400
    os.utime(archive, (old, old))

    # The live root exists but has NO s1 directory yet: the writer has not
    # created it. That is the state the old per-session probe froze.
    (tmp_path / rj.RUN_JOURNAL_DIR_NAME).mkdir(parents=True)

    # The writer creates the session dir + appends its suffix at the moment
    # the prune takes the run's writer lock — i.e. after any pre-lock probe.
    live_path = tmp_path / rj.RUN_JOURNAL_DIR_NAME / sid / f"{rid}.jsonl"
    real_lock_for = rj._lock_for
    state = {"done": False}

    def lock_for_then_write(path):
        lock = real_lock_for(path)
        if not state["done"] and str(path) == str(live_path):
            state["done"] = True
            rj.append_run_event(
                sid, rid, "done", {"terminal_state": "completed"},
                session_dir=tmp_path, seq=3,
            )
        return lock

    monkeypatch.setattr(rj, "_lock_for", lock_for_then_write)
    monkeypatch.setenv(rj._RETENTION_ARCHIVE_TTL_ENV, "30")
    counters = rj.sweep_run_journal(
        session_dir=tmp_path, ttl_days=0, max_runs_per_session=0, max_bytes_per_session=0
    )

    assert state["done"], "test did not fire the mid-pass writer"
    assert archive.exists(), "prune deleted the prefix although a live suffix was created under its lock"
    assert counters["pruned_archives"] == 0
    read = rj.read_run_events(sid, rid, session_dir=tmp_path)
    assert [int(e["seq"]) for e in read["events"]] == [1, 2, 3]
    replay = rj.read_session_run_events(sid, after_event_id=f"{rid}:1", session_dir=tmp_path)
    assert replay["status"] == "ok", replay["status"]


def _claim_in_archive_only_session(tmp_path, sid, rid, live_root_state):
    """Helper: a stale prune claim for a session that has no live directory."""
    archive_dir = tmp_path / rj.RUN_JOURNAL_ARCHIVE_DIR_NAME / sid
    archive_dir.mkdir(parents=True)
    claim = archive_dir / f".{rid}.jsonl.gz.prune-claim.999"
    with gzip.open(claim, "wb") as gz:
        gz.write(
            (json.dumps(
                {"version": 1, "event_id": f"{rid}:1", "seq": 1, "run_id": rid,
                 "session_id": sid, "event": "done", "type": "done",
                 "created_at": time.time() - 3600, "terminal": True,
                 "terminal_state": "completed", "payload": {"terminal_state": "completed"}},
                separators=(",", ":"),
            ) + "\n").encode()
        )
    old = time.time() - 7200  # past the quiescence window
    os.utime(claim, (old, old))
    if live_root_state == "empty":
        (tmp_path / rj.RUN_JOURNAL_DIR_NAME).mkdir(parents=True)
    return archive_dir / f"{rid}.jsonl.gz", claim


def test_archive_only_session_claim_recovered_without_live_root(tmp_path, monkeypatch):
    """A crash-left claim in an archive-only session is restored even when the
    live journal root does not exist at all.

    Reproduces the finding: claim recovery ran only from the per-session sweep,
    which is only reached for LIVE-enumerated sessions, and the sweep returned
    early when the live root was absent — so a claim left by an interrupted
    prune stayed hidden forever and ``find_run_summary`` returned None for the
    run's only copy. Recovery now walks the archive root independently.
    """
    sid, rid = "s1", "r1"
    canonical, claim = _claim_in_archive_only_session(tmp_path, sid, rid, "absent")

    monkeypatch.setenv(rj._RETENTION_ARCHIVE_TTL_ENV, "30")
    counters = rj.sweep_run_journal(
        session_dir=tmp_path, ttl_days=0, max_runs_per_session=0, max_bytes_per_session=0
    )

    assert counters["errors"] == 0
    assert canonical.exists(), "the claim was not restored (only copy stayed hidden)"
    assert not claim.exists(), "claim debris left behind"
    summary = rj.find_run_summary(rid, session_dir=tmp_path)
    assert summary is not None, "the run's only copy is unreachable"


def test_archive_only_session_claim_recovered_with_empty_live_root(tmp_path, monkeypatch):
    """Same as above, with the live root present but this session absent from it.

    This is the case the maintainer reproduced through ``sweep_run_journal()``:
    the live root exists (other sessions are live), the claim's session exists
    ONLY in the archive, and the per-session sweep never visits it.
    """
    sid, rid = "s1", "r1"
    canonical, claim = _claim_in_archive_only_session(tmp_path, sid, rid, "empty")
    # A second, LIVE session, so the sweep does not early-return.
    other = tmp_path / rj.RUN_JOURNAL_DIR_NAME / "s2"
    other.mkdir(parents=True)

    monkeypatch.setenv(rj._RETENTION_ARCHIVE_TTL_ENV, "30")
    counters = rj.sweep_run_journal(
        session_dir=tmp_path, ttl_days=0, max_runs_per_session=0, max_bytes_per_session=0
    )

    assert counters["errors"] == 0
    assert canonical.exists(), "the claim was not restored (only copy stayed hidden)"
    assert not claim.exists(), "claim debris left behind"
    summary = rj.find_run_summary(rid, session_dir=tmp_path)
    assert summary is not None, "the run's only copy is unreachable"


# ── claim grammar: canonical names can never parse as claims ────────────────


def _write_claimlike_archive(tmp_path, sid, rid):
    """An aged, readable archive for a run id that CONTAINS the claim marker."""
    assert rj._validate_id(rid, "run_id") == rid, "test id must be writer-accepted"
    archive_dir = tmp_path / rj.RUN_JOURNAL_ARCHIVE_DIR_NAME / sid
    archive_dir.mkdir(parents=True)
    canonical = archive_dir / f"{rid}.jsonl.gz"
    row = {
        "version": 1, "event_id": f"{rid}:1", "seq": 1, "run_id": rid,
        "session_id": sid, "event": "done", "type": "done",
        "created_at": time.time() - 3600, "terminal": True,
        "terminal_state": "completed", "payload": {"terminal_state": "completed"},
    }
    body = (json.dumps(row, separators=(",", ":")) + "\n").encode()
    with gzip.open(canonical, "wb") as gz:
        gz.write(body)
    old = time.time() - 400 * 86400
    os.utime(canonical, (old, old))
    return canonical, body


def test_canonical_archive_for_claimlike_run_id_not_restored_as_claim(tmp_path):
    """A canonical archive whose run id contains '.prune-claim.' survives recovery.

    Reproduces the finding: ``_validate_id`` accepts ``.r.jsonl.gz.prune-claim.999``
    whose canonical archive is ``.r.jsonl.gz.prune-claim.999.jsonl.gz``. The
    recovery pass found the marker with an unanchored ``find()``, treated the
    CANONICAL archive as a claim for run ``r``, hard-linked it under
    ``r.jsonl.gz`` and unlinked its real name — the run's only copy became
    undiscoverable (``find_run_summary`` -> None). The claim grammar is now
    anchored end-to-end, so a canonical name can never parse as a claim.
    """
    sid, rid = "s1", ".r.jsonl.gz.prune-claim.999"
    canonical, body = _write_claimlike_archive(tmp_path, sid, rid)
    # No live root at all: archive-only recovery runs despite the early return.

    counters = rj.sweep_run_journal(
        session_dir=tmp_path, ttl_days=0, max_runs_per_session=0, max_bytes_per_session=0
    )

    assert counters["errors"] == 0
    assert canonical.exists(), "the canonical archive was renamed away as claim debris"
    with gzip.open(canonical, "rb") as fh:
        assert fh.read() == body, "the canonical archive's bytes changed"
    misattributed = canonical.parent / "r.jsonl.gz"
    assert not misattributed.exists(), "the archive was misattributed to another run id"
    read = rj.read_run_events(sid, rid, session_dir=tmp_path)
    assert [int(e["seq"]) for e in read["events"]] == [1]
    summary = rj.find_run_summary(rid, session_dir=tmp_path)
    assert summary is not None, "the run's only copy is undiscoverable"


def test_canonical_archive_for_claimlike_run_id_survives_with_live_root(tmp_path):
    """Same collision, with a live root present (the normal sweep path)."""
    sid, rid = "s1", ".r.jsonl.gz.prune-claim.999"
    canonical, body = _write_claimlike_archive(tmp_path, sid, rid)
    # A live root, so the sweep takes the normal path (not the early return).
    (tmp_path / rj.RUN_JOURNAL_DIR_NAME / "s2").mkdir(parents=True)

    counters = rj.sweep_run_journal(
        session_dir=tmp_path, ttl_days=0, max_runs_per_session=0, max_bytes_per_session=0
    )

    assert counters["errors"] == 0
    assert canonical.exists(), "the canonical archive was renamed away as claim debris"
    with gzip.open(canonical, "rb") as fh:
        assert fh.read() == body
    assert not (canonical.parent / "r.jsonl.gz").exists()
    summary = rj.find_run_summary(rid, session_dir=tmp_path)
    assert summary is not None


def test_genuine_claim_for_claimlike_run_id_is_still_restored(tmp_path):
    """The anchored grammar still recovers a REAL claim for such a run id.

    The claim for run ``.r.jsonl.gz.prune-claim.999`` is
    ``..r.jsonl.gz.prune-claim.999.jsonl.gz.prune-claim.<pid>`` — the fix must
    not swing so far that genuine recovery stops working for these ids.
    """
    sid, rid = "s1", ".r.jsonl.gz.prune-claim.999"
    canonical, body = _write_claimlike_archive(tmp_path, sid, rid)
    # Simulate the crash: the canonical entry was claimed (renamed), never unlinked.
    claim = canonical.parent / f".{rid}.jsonl.gz.prune-claim.999"
    os.rename(canonical, claim)
    old = time.time() - 7200  # past the quiescence window
    os.utime(claim, (old, old))

    counters = rj.sweep_run_journal(
        session_dir=tmp_path, ttl_days=0, max_runs_per_session=0, max_bytes_per_session=0
    )

    assert counters["errors"] == 0
    assert canonical.exists(), "a genuine claim was not restored"
    with gzip.open(canonical, "rb") as fh:
        assert fh.read() == body
    assert not claim.exists(), "claim debris left behind"


# ── the Oct-8 re-gate findings (cross-process writer, v2 rows, recovery) ────


def _real_run_file(tmp_path: Path, sid: str, rid: str, *, age_days: float = 30.0,
                   shape: list[tuple[str, dict]] | None = None) -> Path:
    """Write a run with the REAL production writer (``append_run_event``)."""
    for name, payload in (shape or [("token", {"text": "hello"}),
                                    ("done", {"terminal_state": "completed"})]):
        rj.append_run_event(sid, rid, name, payload, session_dir=tmp_path)
    path = tmp_path / rj.RUN_JOURNAL_DIR_NAME / sid / f"{rid}.jsonl"
    old = time.time() - age_days * 86400.0
    os.utime(path, (old, old))
    return path


def _simulate_restart() -> None:
    """Drop every in-process cache the way a fresh process starts (#7613).

    A restart after archival must not change what an append is assigned: the
    seed comes from disk (archived prefix + live suffix), never from memory.
    """
    with rj._SEQ_CACHE_LOCK:
        rj._SEQ_CACHE.clear()
        rj._SEQ_CACHE_SIGNATURES.clear()
    with rj._SUMMARY_CACHE_LOCK:
        rj._SUMMARY_CACHE.clear()


def test_real_writer_creates_version_two_rows(tmp_path):
    """Pin the premise: the production writer emits ``"version":2`` rows."""
    path = _real_run_file(tmp_path, "s1", "r1", age_days=0.0)
    assert '"version":2' in path.read_text(encoding="utf-8")


def test_real_writer_v2_journal_is_archived(tmp_path):
    """The classification must recognize the rows production actually writes.

    Reproduces the finding: both terminal matchers required ``"version":1``
    while ``append_run_event`` writes version 2, so a real completed run gave
    ``archived_files=0, retained_open=1`` forever — retention never ran on any
    production journal. Built with the real writer, not the v1 test helper.
    """
    path = _real_run_file(tmp_path, "s1", "r1")
    counters = _sweep(tmp_path, ttl_days=14, max_runs_per_session=0, max_bytes_per_session=0)
    assert counters["archived_files"] == 1
    assert not path.exists()
    assert _archive_path(tmp_path, "s1", "r1").exists()
    # Every read path still resolves the archived run.
    read = rj.read_run_events("s1", "r1", session_dir=tmp_path)
    assert [int(e["seq"]) for e in read["events"]] == [1, 2]
    summary = rj.find_run_summary("r1", session_dir=tmp_path)
    assert summary is not None and summary["terminal"] is True
    assert summary["terminal_state"] == "completed"


def test_validated_recovery_reads_archived_run_after_archival(tmp_path):
    """Cold-load recovery must validate through the archive, not the live file.

    Reproduces the finding: validated recovery returned two events before
    archival and zero after (ordinary reads still returned both), so a restart
    recovered the answer from neither the transcript nor the model context.
    """
    _write_run(tmp_path, "s1", "r1", mtime_age_days=30)
    before = rj.read_run_events("s1", "r1", session_dir=tmp_path, validated_recovery=True)
    assert len(before["events"]) == 3
    counters = _sweep(tmp_path, ttl_days=14, max_runs_per_session=0, max_bytes_per_session=0)
    assert counters["archived_files"] == 1
    after = rj.read_run_events("s1", "r1", session_dir=tmp_path, validated_recovery=True)
    assert [int(e["seq"]) for e in after["events"]] == [1, 2, 3]
    assert after["malformed"] == []


def test_archive_append_restart_prune_chain_keeps_every_row(tmp_path, monkeypatch):
    """The full archive -> append -> restart -> prune chain (repeatable).

    Reproduces the finding: an append after archival was assigned sequence 1
    (the recreated live file reset the seed) and was dropped from reads, so the
    run silently lost its tail; the later prune then deleted the stored prefix.
    With the fix each append continues at the durable maximum + 1 — including
    after a restart — and the prune keeps the archive while a live suffix
    exists. Every checkpoint asserts the full row set is still readable.
    """
    path = _write_run(tmp_path, "s1", "r1", mtime_age_days=30)
    counters = _sweep(tmp_path, ttl_days=14, max_runs_per_session=0, max_bytes_per_session=0)
    assert counters["archived_files"] == 1
    archive = _archive_path(tmp_path, "s1", "r1")
    assert archive.exists() and not path.exists()

    # Append after archival: the seed must come from the archived prefix.
    row = rj.append_run_event("s1", "r1", "token", {"text": "post"}, session_dir=tmp_path)
    assert row["seq"] == 4
    read = rj.read_run_events("s1", "r1", session_dir=tmp_path)
    assert [int(e["seq"]) for e in read["events"]] == [1, 2, 3, 4]

    # Restart: empty caches, reseed from disk only.
    _simulate_restart()
    row2 = rj.append_run_event("s1", "r1", "token", {"text": "post-2"}, session_dir=tmp_path)
    assert row2["seq"] == 5
    replay = rj.read_session_run_events("s1", after_event_id="r1:1", session_dir=tmp_path)
    assert replay["status"] == "ok", replay["status"]
    assert [int(e["seq"]) for e in replay["events"]] == [2, 3, 4, 5]

    # Prune with the archive TTL enabled: the stored prefix must survive while
    # its live suffix exists, or the next replay fails ``replay_noncontiguous``.
    old = time.time() - 400 * 86400.0
    os.utime(archive, (old, old))
    monkeypatch.setenv(rj._RETENTION_ARCHIVE_TTL_ENV, "30")
    counters2 = _sweep(tmp_path, ttl_days=0, max_runs_per_session=0, max_bytes_per_session=0)
    assert counters2["pruned_archives"] == 0
    assert archive.exists()
    read2 = rj.read_run_events("s1", "r1", session_dir=tmp_path)
    assert [int(e["seq"]) for e in read2["events"]] == [1, 2, 3, 4, 5]


def test_unreadable_archive_does_not_restart_sequences(tmp_path):
    """A corrupt archive must block appends, not restart them at seq 1.

    Reproduces the Greptile finding: after archival the live file is gone; if
    the archive cannot be read, ``_archived_next_seq`` returned 1 (treated like
    "no archive") and a resumed writer created a NEW live journal starting at
    seq 1 beside the real stored history. Readers union archive + live by seq
    and drop live rows at or below the archived maximum, so the resumed rows
    were written but never read. The seed now refuses with a clear error when
    the archive exists but its last sequence cannot be established.
    """
    _write_run(tmp_path, "s1", "r1", mtime_age_days=30)
    counters = _sweep(tmp_path, ttl_days=14, max_runs_per_session=0, max_bytes_per_session=0)
    assert counters["archived_files"] == 1
    archive = _archive_path(tmp_path, "s1", "r1")
    assert archive.exists()

    # Corrupt the archive in place: truncated gzip member -> unreadable.
    raw = archive.read_bytes()
    archive.write_bytes(raw[: len(raw) // 3])
    assert rj._read_gz_text(archive) is None

    with pytest.raises(ValueError, match="archive_sequence_unavailable"):
        rj.append_run_event("s1", "r1", "token", {"text": "after"}, session_dir=tmp_path)

    # Fail closed: no row was written, so nothing can shadow the stored history,
    # and the archive is untouched. (The open() creates an empty live file; what
    # matters is that it stays empty rather than restarting at seq 1.)
    live = tmp_path / rj.RUN_JOURNAL_DIR_NAME / "s1" / "r1.jsonl"
    assert not live.exists() or live.read_text() == ""
    assert archive.exists()


def test_append_without_any_archive_still_starts_at_one(tmp_path):
    """The refusal must not fire when there is genuinely no archive."""
    rj.append_run_event("s1", "r1", "token", {"text": "first"}, session_dir=tmp_path)
    read = rj.read_run_events("s1", "r1", session_dir=tmp_path)
    assert [int(e["seq"]) for e in read["events"]] == [1]


def test_readable_but_invalid_archive_does_not_restart_sequences(tmp_path):
    """A readable archive with no valid seq must block appends, not restart them.

    Reproduces the Greptile follow-up: the archive DEcompresses fine but every
    row is malformed JSON, a non-object, or missing ``seq`` — the max-seq scan
    skipped them all and returned 1, so a resumed writer restarted at seq 1
    beside the stored bytes. Refused now, same fail-closed rule as the
    unreadable case.
    """
    _write_run(tmp_path, "s1", "r1", mtime_age_days=30)
    counters = _sweep(tmp_path, ttl_days=14, max_runs_per_session=0, max_bytes_per_session=0)
    assert counters["archived_files"] == 1
    archive = _archive_path(tmp_path, "s1", "r1")
    assert archive.exists()

    # Valid gzip, but no row yields a valid positive seq.
    payload = (b"not json at all\n"
               + json.dumps({"nope": 1}).encode() + b"\n"
               + json.dumps([1, 2, 3]).encode() + b"\n")
    with gzip.open(archive, "wb") as gz:
        gz.write(payload)
    assert rj._read_gz_text(archive) is not None  # readable...

    with pytest.raises(ValueError, match="archive_sequence_unavailable"):
        rj.append_run_event("s1", "r1", "token", {"text": "after"}, session_dir=tmp_path)

    live = tmp_path / rj.RUN_JOURNAL_DIR_NAME / "s1" / "r1.jsonl"
    assert not live.exists() or live.read_text() == ""
    assert archive.exists()


def test_archive_with_uncommitted_torn_tail_still_seeds_from_valid_prefix(tmp_path):
    """A tolerated uncommitted torn tail does not invalidate the valid prefix.

    Seeding reads THROUGH the scanner's canonical contract: an unterminated
    crash fragment at archive EOF is the one tolerated stop, so the prefix
    that validated still supplies the seed. (A complete malformed row does
    not — see the refusal tests below.)
    """
    _write_run(tmp_path, "s1", "r1", mtime_age_days=30)
    counters = _sweep(tmp_path, ttl_days=14, max_runs_per_session=0, max_bytes_per_session=0)
    assert counters["archived_files"] == 1
    archive = _archive_path(tmp_path, "s1", "r1")
    raw = gzip.decompress(archive.read_bytes())
    with gzip.open(archive, "wb") as gz:
        gz.write(raw + b'{"version":2,"event_id":"r1:4","seq":4,')
    row = rj.append_run_event("s1", "r1", "token", {"text": "after"}, session_dir=tmp_path)
    assert row["seq"] == 4  # prefix max is 3; the torn fragment is not a row
    read = rj.read_run_events("s1", "r1", session_dir=tmp_path)
    assert [int(e["seq"]) for e in read["events"]] == [1, 2, 3, 4]


def test_readable_archive_still_seeds_appends(tmp_path):
    """A readable archive keeps the continue-past-maximum behavior."""
    _write_run(tmp_path, "s1", "r1", mtime_age_days=30)
    counters = _sweep(tmp_path, ttl_days=14, max_runs_per_session=0, max_bytes_per_session=0)
    assert counters["archived_files"] == 1
    row = rj.append_run_event("s1", "r1", "token", {"text": "after"}, session_dir=tmp_path)
    assert row["seq"] == 4
    read = rj.read_run_events("s1", "r1", session_dir=tmp_path)
    assert [int(e["seq"]) for e in read["events"]] == [1, 2, 3, 4]



def _hold_and_commit(path_str: str, ready_str: str, go_str: str) -> None:
    """Child-process worker: hold the journal lock, then commit seq 4.

    The hold spans the parent's whole sweep attempt, so the sweep either waits
    (fixed: re-checks identity, sees the new row, keeps the file) or archives
    and unlinks under the writer (bug: seq 4 lands on a detached inode and is
    lost from every read).
    """
    import fcntl as child_fcntl
    import json as child_json
    import os as child_os
    import time as child_time
    from pathlib import Path as ChildPath

    fd = child_os.open(path_str, child_os.O_RDWR | child_os.O_APPEND)
    try:
        child_fcntl.flock(fd, child_fcntl.LOCK_EX)
        ChildPath(ready_str).write_text("ready", encoding="utf-8")
        deadline = child_time.time() + 30.0
        while child_time.time() < deadline and not ChildPath(go_str).exists():
            child_time.sleep(0.02)
        row = {
            "version": 2, "event_id": "r1:4", "seq": 4, "run_id": "r1",
            "session_id": "s1", "event": "token", "type": "token",
            "created_at": child_time.time(), "terminal": False,
            "terminal_state": None, "payload": {"text": "cross-process"},
        }
        child_os.write(fd, (child_json.dumps(row, separators=(",", ":")) + "\n").encode())
        child_os.fsync(fd)
    finally:
        child_fcntl.flock(fd, child_fcntl.LOCK_UN)
        child_os.close(fd)


@requires_fork
def test_sweep_waits_for_cross_process_writer_commit(tmp_path):
    """A writer in ANOTHER process must never lose its committed row.

    Reproduces the finding: the sweep archived and unlinked the journal while a
    child process held the cross-process lock; the child then committed r1:4 to
    the detached inode and replay returned only r1:1..r1:3. The sweep now takes
    the same inode lock for the whole archive, so it either serializes behind
    the writer (identity changed -> keep the file) or the writer re-opens.
    """
    ctx = multiprocessing.get_context("fork")
    path = _write_run(tmp_path, "s1", "r1", mtime_age_days=30)
    ready = tmp_path / "child-ready"
    go = tmp_path / "child-go"
    child = ctx.Process(target=_hold_and_commit, args=(str(path), str(ready), str(go)))
    child.start()
    try:
        deadline = time.time() + 15.0
        while not ready.exists() and time.time() < deadline:
            time.sleep(0.02)
        assert ready.exists(), "child never acquired the journal lock"
        with _ThreadPoolExecutor(max_workers=1) as pool:
            sweep_future = pool.submit(
                _sweep, tmp_path, ttl_days=14, max_runs_per_session=0, max_bytes_per_session=0
            )
            time.sleep(0.3)  # let the sweep reach the lock and block on it
            go.write_text("go", encoding="utf-8")
            counters = sweep_future.result(timeout=60)
    finally:
        go.write_text("go", encoding="utf-8")
        child.join(timeout=30)
        if child.is_alive():  # pragma: no cover - defensive
            child.terminate()
            child.join(timeout=10)
    assert child.exitcode == 0
    assert counters["archived_files"] == 0, "the sweep archived under a live cross-process writer"
    assert path.exists()
    read = rj.read_run_events("s1", "r1", session_dir=tmp_path)
    assert [int(e["seq"]) for e in read["events"]] == [1, 2, 3, 4]
    assert read["events"][-1]["payload"]["text"] == "cross-process"


def test_sweep_skips_entirely_without_process_lock_backend(tmp_path, monkeypatch):
    """No lock backend -> no sweep at all (fail closed, keep every run).

    Reproduces the finding: the unsupported-backend probe archived a run even
    though appends could not coordinate with it; a writer committing during
    that window would lose its row. The sweep now returns before scanning.
    """
    path = _write_run(tmp_path, "s1", "r1", mtime_age_days=30)
    monkeypatch.setattr(rj, "_fcntl", None)
    monkeypatch.setattr(rj, "_msvcrt", None)
    counters = _sweep(tmp_path, ttl_days=14, max_runs_per_session=0, max_bytes_per_session=0)
    assert counters["archived_files"] == 0
    assert counters["files_scanned"] == 0, "the sweep scanned although it cannot lock"
    assert path.exists()
    assert not _archive_path(tmp_path, "s1", "r1").exists()


# ── fail-closed config: invalid values disable, they never fall through ─────


def test_invalid_archive_ttl_disables_prune_instead_of_falling_through(tmp_path, monkeypatch):
    """``-1`` must disable pruning, not fall through to a finite persisted TTL.

    Reproduces the finding: env ``-1`` fell through to a 30-day settings value
    and deleted the run's only archived copy. An explicitly invalid supplied
    value disables the affected operation under the approved fail-closed policy.
    """
    import api.config as config_module

    sid = "s1"
    archive_dir = tmp_path / rj.RUN_JOURNAL_ARCHIVE_DIR_NAME / sid
    archive_dir.mkdir(parents=True)
    archive = archive_dir / "r1.jsonl.gz"
    row = {
        "version": 2, "event_id": "r1:1", "seq": 1, "run_id": "r1", "session_id": sid,
        "event": "done", "type": "done", "created_at": time.time() - 3600,
        "terminal": True, "terminal_state": "completed",
        "payload": {"terminal_state": "completed"},
    }
    with gzip.open(archive, "wb") as gz:
        gz.write((json.dumps(row, separators=(",", ":")) + "\n").encode())
    old = time.time() - 400 * 86400.0
    os.utime(archive, (old, old))
    (tmp_path / rj.RUN_JOURNAL_DIR_NAME / "s2").mkdir(parents=True)

    monkeypatch.setenv(rj._RETENTION_ARCHIVE_TTL_ENV, "-1")
    monkeypatch.setattr(
        config_module, "load_settings", lambda: {"run_journal_archive_ttl_days": 30}
    )
    assert rj.resolve_run_journal_retention_caps()["archive_ttl_days"] == 0.0

    counters = _sweep(tmp_path, ttl_days=0, max_runs_per_session=0, max_bytes_per_session=0)
    assert counters["pruned_archives"] == 0
    assert archive.exists(), "an invalid TTL fell through and deleted the only archive"


def test_invalid_or_negative_caps_disable_instead_of_enabled_defaults(monkeypatch):
    """Garbage / negative / out-of-range supplied values resolve to disabled (0)."""
    import api.config as config_module

    monkeypatch.setattr(config_module, "load_settings", lambda: {})
    monkeypatch.setenv(rj._RETENTION_TTL_ENV, "-5")
    monkeypatch.setenv(rj._RETENTION_MAX_RUNS_ENV, "banana")
    monkeypatch.setenv(rj._RETENTION_MAX_BYTES_ENV, "999999999999999")
    caps = rj.resolve_run_journal_retention_caps()
    assert caps["ttl_days"] == 0.0
    assert caps["max_runs_per_session"] == 0
    assert caps["max_bytes_per_session"] == 0


def test_valid_caps_still_resolve_and_empty_env_is_unset(monkeypatch):
    """Valid explicit values win; an empty env passthrough is not "supplied"."""
    import api.config as config_module

    monkeypatch.setattr(config_module, "load_settings", lambda: {})
    monkeypatch.setenv(rj._RETENTION_TTL_ENV, "")
    caps = rj.resolve_run_journal_retention_caps()
    assert caps["ttl_days"] == rj.DEFAULT_RUN_JOURNAL_RETENTION_TTL_DAYS
    monkeypatch.setenv(rj._RETENTION_TTL_ENV, "3")
    assert rj.resolve_run_journal_retention_caps()["ttl_days"] == 3.0


# ── terminal metadata must match the writer's own shapes ────────────────────


def test_unknown_terminal_state_is_not_archived(tmp_path):
    """A state the writer never produces (e.g. approval-pending) fails closed.

    Reproduces the finding: classification accepted any non-empty
    ``terminal_state``, so a row claiming an unknown state was archived even
    though the run's real state was in-flight.
    """
    _write_run(tmp_path, "s1", "r1", mtime_age_days=30, terminal_state="approval-pending")
    counters = _sweep(tmp_path, ttl_days=14, max_runs_per_session=0, max_bytes_per_session=0)
    assert counters["archived_files"] == 0
    assert counters["retained_open"] == 1
    assert (tmp_path / rj.RUN_JOURNAL_DIR_NAME / "s1" / "r1.jsonl").exists()


def test_inconsistent_terminal_metadata_is_not_archived(tmp_path):
    """A terminal row whose metadata contradicts its event must not classify."""
    session_dir = tmp_path / rj.RUN_JOURNAL_DIR_NAME / "s1"
    session_dir.mkdir(parents=True)
    shapes = [
        # done's state is completed; claiming "errored" contradicts the event.
        ("done", "errored", {"terminal_state": "errored"}),
        # cancel's state is interrupted-by-user; claiming "completed" is a lie.
        ("cancel", "completed", {"terminal_state": "completed"}),
    ]
    lines = []
    for seq, (event, state, payload) in enumerate(shapes, start=1):
        lines.append(json.dumps({
            "version": 1, "event_id": f"r1:{seq}", "seq": seq, "run_id": "r1",
            "session_id": "s1", "event": event, "type": event,
            "created_at": time.time() - 3600, "terminal": True,
            "terminal_state": state, "payload": payload,
        }, separators=(",", ":")) + "\n")
    path = session_dir / "r1.jsonl"
    path.write_text("".join(lines), encoding="utf-8")
    old = time.time() - 30 * 86400.0
    os.utime(path, (old, old))
    counters = _sweep(tmp_path, ttl_days=14, max_runs_per_session=0, max_bytes_per_session=0)
    assert counters["archived_files"] == 0
    assert path.exists()


@pytest.mark.parametrize("shape", [
    [("token", {"text": "x"}), ("done", {"terminal_state": "completed"})],
    [("token", {"text": "x"}), ("stream_end", {"terminal_state": "completed"})],
    [("token", {"text": "x"}), ("cancel", {})],
    [("token", {"text": "x"}), ("apperror", {"type": "interrupted"})],
    [("token", {"text": "x"}), ("apperror", {"type": "tool_limit_reached"})],
    [("token", {"text": "x"}), ("done", {"terminal_state": "tool_limit_reached"})],
])
def test_real_writer_terminal_shapes_still_classify(tmp_path, shape):
    """The strict metadata check must accept every shape the writer produces."""
    _real_run_file(tmp_path, "s1", "r1", shape=shape)
    counters = _sweep(tmp_path, ttl_days=14, max_runs_per_session=0, max_bytes_per_session=0)
    assert counters["archived_files"] == 1


def test_real_writer_active_run_is_never_archived(tmp_path):
    """A run with no terminal row (in-flight / never settled) stays live."""
    path = _real_run_file(tmp_path, "s1", "r1", shape=[("token", {"text": "x"})])
    counters = _sweep(tmp_path, ttl_days=14, max_runs_per_session=0, max_bytes_per_session=0)
    assert counters["archived_files"] == 0
    assert counters["retained_open"] == 1
    assert path.exists()

# ── re-gate 22:30Z: composed regressions ─────────────────────────────────────


def test_crash_tail_survives_archive_append_and_cold_recovery(tmp_path):
    """Crash-tail -> archive -> append -> cold recovery must keep every row.

    Reproduces the re-gate finding: a tolerated uncommitted EOF tail was
    archived while still un-terminated; the post-archive append then joined the
    new suffix onto those bytes, giving them a false newline and promoting them
    into a malformed COMPLETE row. Cold validated recovery returned ZERO events
    (``recovery_malformed_row``) where pinned master recovers [1,2,3]. The union
    now drops the archive's uncommitted torn tail before joining: appends
    continue at the durable maximum and the whole run validates.
    """
    sid, rid = "s1", "r1"
    rj.append_run_event(sid, rid, "token", {"text": "hello"}, session_dir=tmp_path)
    rj.append_run_event(sid, rid, "done", {"terminal_state": "completed"}, session_dir=tmp_path)
    path = tmp_path / rj.RUN_JOURNAL_DIR_NAME / sid / f"{rid}.jsonl"

    # Crash mid-append: unterminated bytes at EOF (rollback never ran).
    with open(path, "ab") as fh:
        fh.write(b'{"version":2,"event_id":"' + rid.encode() + b':3","seq":3,')
        fh.flush()
        os.fsync(fh.fileno())

    torn = rj.read_run_events(sid, rid, session_dir=tmp_path, validated_recovery=True)
    assert [int(e["seq"]) for e in torn["events"]] == [1, 2]
    assert torn["malformed"] == [{"line": 3, "reason": "recovery_torn_tail"}]

    # Age + sweep with the ordinary live TTL: the run is archived WITH the tail.
    old = time.time() - 30 * 86400.0
    os.utime(path, (old, old))
    counters = _sweep(tmp_path, ttl_days=14, max_runs_per_session=0, max_bytes_per_session=0)
    assert counters["archived_files"] == 1

    # The post-archive append must continue the durable maximum.
    row = rj.append_run_event(sid, rid, "stream_end", {}, session_dir=tmp_path)
    assert row["seq"] == 3

    # Cold recovery (fresh process view) validates the union whole.
    _simulate_restart()
    recovered = rj.read_run_events(sid, rid, session_dir=tmp_path, validated_recovery=True)
    assert [int(e["seq"]) for e in recovered["events"]] == [1, 2, 3], recovered
    assert recovered["malformed"] == []
    replay = rj.read_session_run_events(sid, after_event_id=f"{rid}:1", session_dir=tmp_path)
    assert replay["status"] == "ok", replay["status"]
    assert [int(e["seq"]) for e in replay["events"]] == [2, 3]


def test_merge_drops_torn_archive_tail_and_ignores_invalid_sequence_claims(tmp_path):
    """Sequence authority: complete-invalid and wrong-owner rows never steer.

    Reproduces the re-gate finding: the archive/live merge accepted ANY row
    with a parseable ``seq`` as sequence authority — a wrong-owner row
    (``event_id``/``run_id`` naming another run) or a wrong-protocol row could
    out-rank committed rows and SUPPRESS the live suffix from the union. The
    archive's uncommitted torn tail was also re-joined as if committed,
    promoting an uncommitted crash row into a malformed complete row. Only rows
    that pass the scanner's own ownership/protocol contract carry authority
    now, and the uncommitted tail is dropped before the join.
    """
    sid, rid = "s1", "r1"
    live_dir = tmp_path / rj.RUN_JOURNAL_DIR_NAME / sid
    live_dir.mkdir(parents=True)
    live = live_dir / f"{rid}.jsonl"
    archive = _archive_path(tmp_path, sid, rid)
    archive.parent.mkdir(parents=True, exist_ok=True)

    def _row(seq, *, run="r1", session="s1", version=2, event_id=None):
        return json.dumps({
            "version": version,
            "event_id": event_id if event_id is not None else f"{run}:{seq}",
            "seq": seq, "run_id": run, "session_id": session,
            "event": "token", "type": "token", "created_at": time.time(),
            "terminal": False, "terminal_state": None, "payload": {"text": "x"},
        }, separators=(",", ":"))

    def _seqs(text):
        out = []
        for line in text.splitlines():
            if not line.strip():
                continue
            try:
                out.append(json.loads(line)["seq"])
            except Exception:  # noqa: BLE001 - surface a malformed row in the list
                out.append("MALFORMED")
        return out

    def _compose(archive_text, live_text):
        with gzip.open(archive, "wb") as gz:
            gz.write(archive_text.encode("utf-8"))
        live.write_text(live_text, encoding="utf-8")
        return rj._read_run_file_text(live)

    # Archive: committed rows 1-2, then a TORN tail (no newline) claiming seq 99.
    # The live suffix must survive and the torn bytes must NOT become a
    # malformed complete row.
    merged = _compose(_row(1) + "\n" + _row(2) + "\n" + '{"version":2,"seq":99,', _row(3) + "\n")
    assert _seqs(merged) == [1, 2, 3], merged

    # A wrong-owner row may not claim the maximum: the live suffix survives.
    merged = _compose(_row(1) + "\n" + _row(7, run="other", event_id="other:7") + "\n", _row(3) + "\n")
    assert _seqs(merged) == [1, 7, 3], merged

    # A wrong-protocol (downgrade) row with a huge seq must not suppress either.
    merged = _compose(_row(1) + "\n" + _row(50, version=3) + "\n", _row(3) + "\n")
    assert _seqs(merged) == [1, 50, 3], merged

    # Real writer chain end to end: the committed suffix + archived prefix
    # validate as one run after a restart.
    archive.unlink()
    live.unlink()
    rj.append_run_event(sid, rid, "token", {"text": "one"}, session_dir=tmp_path)
    rj.append_run_event(sid, rid, "done", {"terminal_state": "completed"}, session_dir=tmp_path)
    old = time.time() - 30 * 86400.0
    os.utime(live, (old, old))
    assert _sweep(tmp_path, ttl_days=14, max_runs_per_session=0, max_bytes_per_session=0)["archived_files"] == 1
    row = rj.append_run_event(sid, rid, "token", {"text": "two"}, session_dir=tmp_path)
    assert row["seq"] == 3
    _simulate_restart()
    recovered = rj.read_run_events(sid, rid, session_dir=tmp_path, validated_recovery=True)
    assert [int(e["seq"]) for e in recovered["events"]] == [1, 2, 3], recovered
    assert recovered["malformed"] == []


def _prune_race_child(root_str: str, ready_str: str, go_str: str, result_str: str) -> None:
    """Child process: wait for go, then append one row (a REAL writer)."""
    import time as child_time
    from pathlib import Path as ChildPath

    from api import run_journal as child_rj  # inherited through fork

    ChildPath(ready_str).write_text("ready", encoding="utf-8")
    deadline = child_time.time() + 30.0
    while child_time.time() < deadline and not ChildPath(go_str).exists():
        child_time.sleep(0.01)
    try:
        event = child_rj.append_run_event(
            "s1", "r1", "token", {"text": "child"}, session_dir=ChildPath(root_str),
        )
        ChildPath(result_str).write_text(f"ok {event['seq']}", encoding="utf-8")
    except Exception as exc:  # noqa: BLE001 - report any failure to the parent
        ChildPath(result_str).write_text(f"err {type(exc).__name__}: {exc}", encoding="utf-8")


@requires_fork
def test_prune_never_splits_committed_suffix_from_archived_prefix(tmp_path, monkeypatch):
    """The finite-TTL prune must not orphan a suffix a writer commits meanwhile.

    Reproduces the re-gate finding: the prune's live-absence check and the
    unlink held only the pid-scoped writer lock, so a child PROCESS could seed
    from the aged archive (committing seq 3) between them; the parent then
    deleted the prefix. Reads returned [3] and validated recovery returned
    nothing (``recovery_identity_or_sequence``) — a committed row split from its
    stored history. The child is now scheduled at the prune boundary itself,
    deterministically: with the fix the prune holds the cross-process archive
    authority across [re-check -> delete], so the child either seeds before the
    re-check (its live file exists -> prefix kept -> both recoverable) or after
    the delete (fresh journal, no split). Both outcomes are asserted consistent.
    """
    sid, rid = "s1", "r1"

    # Aged archived prefix through the real writers.
    rj.append_run_event(sid, rid, "token", {"text": "one"}, session_dir=tmp_path)
    rj.append_run_event(sid, rid, "done", {"terminal_state": "completed"}, session_dir=tmp_path)
    live = tmp_path / rj.RUN_JOURNAL_DIR_NAME / sid / f"{rid}.jsonl"
    old = time.time() - 30 * 86400.0
    os.utime(live, (old, old))
    counters = _sweep(tmp_path, ttl_days=14, max_runs_per_session=0, max_bytes_per_session=0)
    assert counters["archived_files"] == 1
    archive = _archive_path(tmp_path, sid, rid)
    assert archive.exists() and not live.exists()
    aold = time.time() - 400 * 86400.0
    os.utime(archive, (aold, aold))
    monkeypatch.setenv(rj._RETENTION_ARCHIVE_TTL_ENV, "30")

    ctx = multiprocessing.get_context("fork")
    ready = tmp_path / "child-ready"
    go = tmp_path / "child-go"
    result_file = tmp_path / "child-result"
    child = ctx.Process(
        target=_prune_race_child,
        args=(str(tmp_path), str(ready), str(go), str(result_file)),
    )
    child.start()
    try:
        deadline = time.time() + 15.0
        while not ready.exists() and time.time() < deadline:
            time.sleep(0.02)
        assert ready.exists(), "child never started"

        # Instrument the PRUNE BOUNDARY exactly as the finding describes: fire
        # the child after the live-absence re-check, just before the real
        # unlink. A bounded wait lets a buggy tree's child commit (it is not
        # coordinated) while a fixed tree's child blocks on the authority the
        # prune already holds — either way the sweep then proceeds unmodified.
        real_prune_entry = rj._prune_archive_entry
        state = {"fired": False}

        def fire_child_then_prune(*args, **kwargs):
            if not state["fired"]:
                state["fired"] = True
                go.write_text("go", encoding="utf-8")
                # Bounded: a buggy tree's child completes here; a fixed tree's
                # child is blocked on the authority this sweep already holds, so
                # this simply expires and the sweep proceeds unmodified.
                wait_deadline = time.time() + 3.0
                while not result_file.exists() and time.time() < wait_deadline:
                    time.sleep(0.02)
            return real_prune_entry(*args, **kwargs)

        monkeypatch.setattr(rj, "_prune_archive_entry", fire_child_then_prune)
        rj.sweep_run_journal(
            session_dir=tmp_path, ttl_days=0, max_runs_per_session=0, max_bytes_per_session=0
        )
        monkeypatch.undo()
    finally:
        go.write_text("go", encoding="utf-8")
        child.join(timeout=60)
        if child.is_alive():  # pragma: no cover - defensive
            child.terminate()
            child.join(timeout=10)

    assert state["fired"], "test did not fire the child at the prune boundary"
    assert child.exitcode == 0, f"child writer failed: {child.exitcode}"
    result = result_file.read_text(encoding="utf-8").strip()
    assert result.startswith("ok "), f"child append failed: {result}"
    child_seq = int(result.split()[1])

    ordinary = rj.read_run_events(sid, rid, session_dir=tmp_path)
    validated = rj.read_run_events(sid, rid, session_dir=tmp_path, validated_recovery=True)
    ordinary_seqs = [int(e["seq"]) for e in ordinary["events"]]
    validated_seqs = [int(e["seq"]) for e in validated["events"]]

    if archive.exists():
        # Prefix kept: the child must have committed the verified continuation.
        assert child_seq == 3, f"archive kept but child restarted at {child_seq}"
        assert ordinary_seqs == [1, 2, 3], ordinary_seqs
        assert validated_seqs == [1, 2, 3], validated
    else:
        # Prefix pruned (the documented TTL behavior): the child must have
        # restarted a fresh journal — never a seq-3 suffix beside no prefix.
        assert child_seq == 1, f"prefix pruned but the child committed seq {child_seq}"
        assert ordinary_seqs == [1], ordinary_seqs
        assert validated_seqs == [1], validated
    assert validated["malformed"] == [], validated["malformed"]


# ── re-gate 06:38Z Oct 9: live EOF boundary + canonical archive authority ────


def test_torn_live_suffix_survives_cold_recovery_and_repaired_append(tmp_path):
    """Archive -> live seq3 -> torn live seq4 -> cold recovery -> repaired append.

    Reproduces the reviewer's scenario: the merge gave every retained live line
    a fresh terminating newline, so an uncommitted crash fragment at live EOF
    became a malformed COMPLETE row — after a cache-clearing restart, cold
    validated recovery returned NO events (``recovery_malformed_row`` at the
    fragment's line) and the whole run was unrecoverable. The live EOF boundary
    is preserved now: cold recovery retains [1,2,3] with the tolerated
    ``recovery_torn_tail``, and the next real append repairs the tail and
    commits seq 4 so the same run reads back complete.
    """
    sid, rid = "s1", "r1"
    rj.append_run_event(sid, rid, "token", {"text": "one"}, session_dir=tmp_path)
    rj.append_run_event(sid, rid, "done", {"terminal_state": "completed"}, session_dir=tmp_path)
    live = tmp_path / rj.RUN_JOURNAL_DIR_NAME / sid / f"{rid}.jsonl"
    old = time.time() - 30 * 86400.0
    os.utime(live, (old, old))
    counters = _sweep(tmp_path, ttl_days=14, max_runs_per_session=0, max_bytes_per_session=0)
    assert counters["archived_files"] == 1
    assert not live.exists()

    _simulate_restart()
    assert rj.append_run_event(sid, rid, "token", {"text": "three"}, session_dir=tmp_path)["seq"] == 3

    # Crash mid-append: an unterminated seq4 fragment at live EOF.
    with open(live, "ab") as fh:
        fh.write(b'{"version":2,"event_id":"r1:4","seq":4,')
        fh.flush()
        os.fsync(fh.fileno())

    _simulate_restart()
    recovered = rj.read_run_events(sid, rid, session_dir=tmp_path, validated_recovery=True)
    assert [int(e["seq"]) for e in recovered["events"]] == [1, 2, 3], recovered
    assert recovered["malformed"] == [{"line": 4, "reason": "recovery_torn_tail"}]

    # The repaired append truncates the uncommitted fragment and commits seq 4.
    row = rj.append_run_event(sid, rid, "token", {"text": "four"}, session_dir=tmp_path)
    assert row["seq"] == 4
    _simulate_restart()
    repaired = rj.read_run_events(sid, rid, session_dir=tmp_path, validated_recovery=True)
    assert [int(e["seq"]) for e in repaired["events"]] == [1, 2, 3, 4], repaired
    assert repaired["malformed"] == []


def _archive_row(rid: str, sid: str, seq: int, **over):
    base = {
        "version": 2, "event_id": f"{rid}:{seq}", "seq": seq,
        "run_id": rid, "session_id": sid, "event": "token", "type": "token",
        "created_at": time.time(), "terminal": False, "terminal_state": None,
        "payload": {"text": "x"},
    }
    base.update(over)
    return json.dumps(base, separators=(",", ":"))


def test_damaged_archive_rows_never_become_sequence_authority(tmp_path):
    """The scanner's canonical contract gates append authority over archives.

    Reproduces the reviewer's scenarios: with contiguous archived 1,2 plus an
    invalid seq3 (event/type disagreement) the reader rejects the prefix, but
    the old per-row skim acknowledged seq4 — an acknowledged row into a union
    no reader can recover. Complete garbage after valid rows behaved the same.
    Authority now comes from the scanner's own validation, so both refuse
    while a reader still surfaces the damaged rows. An uncommitted torn tail
    does not refuse (see the prefix-seeding test).
    """
    sid, rid = "s1", "r1"
    archive = _archive_path(tmp_path, sid, rid)
    archive.parent.mkdir(parents=True, exist_ok=True)
    live = tmp_path / rj.RUN_JOURNAL_DIR_NAME / sid / f"{rid}.jsonl"

    # Contiguous 1,2 + an invalid seq3 (event/type disagreement).
    with gzip.open(archive, "wb") as gz:
        gz.write((_archive_row(rid, sid, 1) + "\n" + _archive_row(rid, sid, 2) + "\n"
                  + _archive_row(rid, sid, 3, type="other") + "\n").encode())
    with pytest.raises(ValueError, match="archive_sequence_unavailable"):
        rj.append_run_event(sid, rid, "token", {"text": "four"}, session_dir=tmp_path)
    assert not live.exists() or live.read_text() == ""
    read = rj.read_run_events(sid, rid, session_dir=tmp_path, validated_recovery=True)
    assert read["events"] == []
    assert read["malformed"][0]["reason"] == "recovery_event_type"

    # Complete garbage after valid rows refuses the same way.
    with gzip.open(archive, "wb") as gz:
        gz.write((_archive_row(rid, sid, 1) + "\n" + _archive_row(rid, sid, 2)
                  + "\ngarbage tail\n").encode())
    with pytest.raises(ValueError, match="archive_sequence_unavailable"):
        rj.append_run_event(sid, rid, "token", {"text": "four"}, session_dir=tmp_path)
    assert not live.exists() or live.read_text() == ""


def test_real_v2_to_v1_downgrade_refuses_seed(tmp_path):
    """A real version2 -> version1 downgrade refuses append authority.

    The scanner's stateful rule: once a version2 row starts gapless
    publication, a later version1 row downgrades the run's validation
    contract and invalidates it. (version3 is an unsupported version, not
    that downgrade — this pins the real stateful case.) The writer must not
    acknowledge an append beside rows no reader can recover.
    """
    sid, rid = "s1", "r1"
    archive = _archive_path(tmp_path, sid, rid)
    archive.parent.mkdir(parents=True, exist_ok=True)
    live = tmp_path / rj.RUN_JOURNAL_DIR_NAME / sid / f"{rid}.jsonl"

    with gzip.open(archive, "wb") as gz:
        gz.write((_archive_row(rid, sid, 1, version=2) + "\n"
                  + _archive_row(rid, sid, 2, version=2) + "\n"
                  + _archive_row(rid, sid, 3, version=1) + "\n").encode())
    # The reader rejects from the downgrade row onward.
    read = rj.read_run_events(sid, rid, session_dir=tmp_path, validated_recovery=True)
    assert read["events"] == []
    assert read["malformed"][0]["reason"] == "recovery_protocol_version"
    # The writer refuses too: the prefix cannot supply append authority.
    with pytest.raises(ValueError, match="archive_sequence_unavailable"):
        rj.append_run_event(sid, rid, "token", {"text": "four"}, session_dir=tmp_path)
    assert not live.exists() or live.read_text() == ""


def test_invalid_high_seq_archive_row_does_not_suppress_live_suffix(tmp_path):
    """Damaged archive metadata never suppresses a genuine live suffix.

    The old skim let ANY parseable seq — including an invalid row's seq 99 —
    set the archived maximum, so a genuine live seq3 was dropped from the
    union. Canonical authority bounds the maximum at the last row the scanner
    accepts (2); the damaged row's bytes remain in the union (readers surface
    them) but cannot rank.
    """
    sid, rid = "s1", "r1"
    live_dir = tmp_path / rj.RUN_JOURNAL_DIR_NAME / sid
    live_dir.mkdir(parents=True)
    live = live_dir / f"{rid}.jsonl"
    archive = _archive_path(tmp_path, sid, rid)
    archive.parent.mkdir(parents=True, exist_ok=True)

    with gzip.open(archive, "wb") as gz:
        gz.write((_archive_row(rid, sid, 1) + "\n" + _archive_row(rid, sid, 2) + "\n"
                  + _archive_row(rid, sid, 99, type="other") + "\n").encode())
    live.write_text(_archive_row(rid, sid, 3) + "\n", encoding="utf-8")

    merged = rj._read_run_file_text(live)
    seqs = [json.loads(line)["seq"] for line in merged.splitlines() if line.strip()]
    assert seqs == [1, 2, 99, 3], merged


# ── 14:32Z re-gate: UTF-8 archive tails + archive refusal beside a suffix ──


def test_partial_utf8_archive_tail_survives_cold_recovery_and_repaired_append(tmp_path):
    """A crash tail cut mid-UTF-8 must survive archival as a tolerated torn tail.

    Reproduces the re-gate finding: the real writer stopped mid-append after a
    partial UTF-8 sequence (``e2 82``) at EOF; cold recovery tolerated it while
    the file was live, but the real sweep archived those exact bytes and the
    archive reader strict-decoded them — discarding the WHOLE run (zero events)
    and refusing every later append with ``archive_sequence_unavailable``. The
    archive decode/encode round-trip is byte-reversible now (surrogateescape,
    not replacement decoding): the byte scanner sees the ORIGINAL bytes, so
    cold recovery keeps [1,2] + recovery_torn_tail and the repaired append
    commits seq 3; the union then reads back complete.
    """
    sid, rid = "s1", "r1"
    rj.append_run_event(sid, rid, "token", {"text": "one"}, session_dir=tmp_path)
    rj.append_run_event(sid, rid, "done", {"terminal_state": "completed"}, session_dir=tmp_path)
    live = tmp_path / rj.RUN_JOURNAL_DIR_NAME / sid / f"{rid}.jsonl"

    # Crash mid-append: unterminated fragment ending in a partial UTF-8
    # sequence (two bytes of a three-byte character) — strictly undecodable.
    with open(live, "ab") as fh:
        fh.write(b'{"version":2,"event_id":"' + rid.encode()
                 + b':3","seq":3,"payload":{"text":"\xe2\x82')
        fh.flush()
        os.fsync(fh.fileno())

    _simulate_restart()
    torn = rj.read_run_events(sid, rid, session_dir=tmp_path, validated_recovery=True)
    assert [int(e["seq"]) for e in torn["events"]] == [1, 2], torn
    assert torn["malformed"] == [{"line": 3, "reason": "recovery_torn_tail"}]

    # Age + real sweep: the run is archived WITH the tail bytes.
    old = time.time() - 30 * 86400.0
    os.utime(live, (old, old))
    counters = _sweep(tmp_path, ttl_days=14, max_runs_per_session=0, max_bytes_per_session=0)
    assert counters["archived_files"] == 1
    assert not live.exists()

    # Cold recovery over the archived copy: the tolerated-torn-tail rule must
    # apply to the original bytes, not be discarded by a strict decode.
    _simulate_restart()
    recovered = rj.read_run_events(sid, rid, session_dir=tmp_path, validated_recovery=True)
    assert [int(e["seq"]) for e in recovered["events"]] == [1, 2], recovered
    assert recovered["malformed"] == [{"line": 3, "reason": "recovery_torn_tail"}]

    # The repaired append continues the durable maximum and commits seq 3.
    row = rj.append_run_event(sid, rid, "token", {"text": "three"}, session_dir=tmp_path)
    assert row["seq"] == 3
    _simulate_restart()
    repaired = rj.read_run_events(sid, rid, session_dir=tmp_path, validated_recovery=True)
    assert [int(e["seq"]) for e in repaired["events"]] == [1, 2, 3], repaired
    assert repaired["malformed"] == []


def test_ordinary_read_tolerates_partial_utf8_live_tail(tmp_path):
    """The ordinary read path must decode live journals reversibly (no raise).

    Same root cause as the archive-tail loss: a crash-truncated UTF-8 sequence
    at live EOF is the tolerated uncommitted torn tail, but the plain decode
    raised ``UnicodeDecodeError`` out of every ordinary read of the run. The
    merge decode is reversible now: the valid prefix survives and the fragment
    surfaces as one malformed row.
    """
    sid, rid = "s1", "r1"
    rj.append_run_event(sid, rid, "token", {"text": "one"}, session_dir=tmp_path)
    live = tmp_path / rj.RUN_JOURNAL_DIR_NAME / sid / f"{rid}.jsonl"
    with open(live, "ab") as fh:
        fh.write(b'{"version":2,"event_id":"' + rid.encode()
                 + b':2","seq":2,"payload":{"text":"\xe2\x82')
        fh.flush()
        os.fsync(fh.fileno())

    read = rj.read_run_events(sid, rid, session_dir=tmp_path)
    assert [int(e["seq"]) for e in read["events"]] == [1], read
    assert len(read["malformed"]) == 1
    assert read["malformed"][0]["line"] == 2


_DAMAGED_SEQ2_VARIANTS = [
    ("event", {"event": "cancel"}, "recovery_event_type"),
    ("type", {"type": "cancel"}, "recovery_event_type"),
    ("terminal-flag", {"terminal": False}, "recovery_terminal_identity"),
    ("terminal-state", {"terminal_state": "errored"}, "recovery_terminal_identity"),
    ("version", {"version": 1}, "recovery_protocol_version"),
    ("event-id", {"event_id": "r1:9"}, "recovery_identity_or_sequence"),
]


def _setup_archived_run_with_live_suffix(root: Path, sid: str, rid: str) -> Path:
    """Real writer + real sweep + real append: archived seq 1,2 and live seq 3."""
    rj.append_run_event(sid, rid, "token", {"text": "one"}, session_dir=root)
    rj.append_run_event(sid, rid, "done", {"terminal_state": "completed"}, session_dir=root)
    live = root / rj.RUN_JOURNAL_DIR_NAME / sid / f"{rid}.jsonl"
    old = time.time() - 30 * 86400.0
    os.utime(live, (old, old))
    counters = _sweep(root, ttl_days=14, max_runs_per_session=0, max_bytes_per_session=0)
    assert counters["archived_files"] == 1
    assert not live.exists()
    # Fresh process view: the re-created suffix seeds from the archived prefix.
    _simulate_restart()
    assert rj.append_run_event(sid, rid, "token", {"text": "three"}, session_dir=root)["seq"] == 3
    return live


def _archive_with_damaged_seq2(root: Path, sid: str, rid: str, mutation: dict) -> None:
    """Rewrite the archived copy's seq2 row with a metadata/writer-state change.

    Written to a temp entry and atomically replaced, so the damage is a fresh
    inode: the hot-path cache must miss on identity alone even if a rewrite
    happened to preserve size and timestamps.
    """
    archive = _archive_path(root, sid, rid)
    raw = gzip.decompress(archive.read_bytes())
    rows = [json.loads(line) for line in raw.decode("utf-8").splitlines()]
    assert len(rows) == 2
    rows[-1].update(mutation)
    body = "".join(json.dumps(row, separators=(",", ":")) + "\n" for row in rows)
    tmp = archive.with_name(archive.name + ".dmg")
    with gzip.open(tmp, "wb") as gz:
        gz.write(body.encode("utf-8"))
    os.replace(tmp, archive)


@pytest.mark.parametrize(
    "mutation,reason",
    [(mutation, reason) for _label, mutation, reason in _DAMAGED_SEQ2_VARIANTS],
    ids=[label for label, _mutation, _reason in _DAMAGED_SEQ2_VARIANTS],
)
def test_damaged_archive_beside_live_suffix_refuses_cold_append(tmp_path, mutation, reason):
    """A damaged archive must refuse COLD appends even with a live suffix.

    Reproduces the re-gate finding: with archived seq1/2 and live seq3, the
    cold writer consulted the archive only when the live scan was EMPTY, so
    damaged seq2 metadata (event/type, terminal metadata, version downgrade,
    identity) was never seen — the writer acknowledged seq4 into a union no
    reader can recover (validated recovery stays empty). The canonical
    archive-prefix authority now runs on EVERY append that can see an archive
    and refuses before any mutate/acknowledge; the live suffix stays untouched.
    """
    sid, rid = "s1", "r1"
    live = _setup_archived_run_with_live_suffix(tmp_path, sid, rid)

    _simulate_restart()
    _archive_with_damaged_seq2(tmp_path, sid, rid, mutation)
    before = live.read_bytes()

    # The reader rejects the union (the failure the writer must respect)...
    read = rj.read_run_events(sid, rid, session_dir=tmp_path, validated_recovery=True)
    assert read["events"] == []
    assert read["malformed"][0]["reason"] == reason

    # ...and the cold writer refuses instead of acknowledging seq 4.
    with pytest.raises(ValueError, match="archive_sequence_unavailable"):
        rj.append_run_event(sid, rid, "token", {"text": "four"}, session_dir=tmp_path)
    assert live.read_bytes() == before
    read_again = rj.read_run_events(sid, rid, session_dir=tmp_path, validated_recovery=True)
    assert read_again["events"] == []


@pytest.mark.parametrize(
    "mutation",
    [mutation for _label, mutation, _reason in _DAMAGED_SEQ2_VARIANTS],
    ids=[label for label, _mutation, _reason in _DAMAGED_SEQ2_VARIANTS],
)
def test_damaged_archive_beside_live_suffix_refuses_hot_append(tmp_path, mutation):
    """The append cache must miss when the ARCHIVE changes, not just the live file.

    Reproduces the re-gate finding: the cached next seq was keyed on the live
    inode only, so damaging the archived seq2 (live untouched) left the cache
    warm — the writer acknowledged seq4 straight off the stale validation while
    recovery stayed empty. The cache key now folds the archive's
    identity/generation in, so the same append goes cold, re-consults the
    canonical authority, and refuses.
    """
    sid, rid = "s1", "r1"
    live = _setup_archived_run_with_live_suffix(tmp_path, sid, rid)

    # Warm-cache precondition: the just-completed seq3 append published a
    # signature for this path (no restart below — this is the hot path).
    with rj._SEQ_CACHE_LOCK:
        assert str(live) in rj._SEQ_CACHE_SIGNATURES

    _archive_with_damaged_seq2(tmp_path, sid, rid, mutation)
    before = live.read_bytes()
    with pytest.raises(ValueError, match="archive_sequence_unavailable"):
        rj.append_run_event(sid, rid, "token", {"text": "four"}, session_dir=tmp_path)
    assert live.read_bytes() == before


def test_live_suffix_beside_healthy_archive_continues_cold_and_hot(tmp_path):
    """Control: a healthy archive must keep seeding appends cold AND hot.

    Guards the always-consulted authority against over-refusal: after archival
    plus a re-created live suffix, appends continue at the durable maximum with
    fresh caches (cold) and with the warm cache (hot; the archive is unchanged),
    and the union reads back complete.
    """
    sid, rid = "s1", "r1"
    _setup_archived_run_with_live_suffix(tmp_path, sid, rid)  # live seq 3
    # Hot: warm cache, archive unchanged -> continues.
    assert rj.append_run_event(sid, rid, "token", {"text": "four"}, session_dir=tmp_path)["seq"] == 4
    _simulate_restart()
    # Cold: fresh caches -> reseeds from the healthy archive + live pair.
    assert rj.append_run_event(sid, rid, "token", {"text": "five"}, session_dir=tmp_path)["seq"] == 5
    _simulate_restart()
    read = rj.read_run_events(sid, rid, session_dir=tmp_path, validated_recovery=True)
    assert [int(e["seq"]) for e in read["events"]] == [1, 2, 3, 4, 5], read
    assert read["malformed"] == []


def test_tolerated_torn_archive_tail_does_not_refuse_live_suffix_appends(tmp_path):
    """Control: a torn (uncommitted) archive tail must NOT refuse appends.

    The always-consulted archive authority tolerates the one uncommitted crash
    tail: with archived 1,2 + an unterminated fragment and a re-created live
    seq3, the next append continues at 4 and the union validates whole. This is
    the boundary the refusal must not cross.
    """
    sid, rid = "s1", "r1"
    rj.append_run_event(sid, rid, "token", {"text": "one"}, session_dir=tmp_path)
    rj.append_run_event(sid, rid, "done", {"terminal_state": "completed"}, session_dir=tmp_path)
    live = tmp_path / rj.RUN_JOURNAL_DIR_NAME / sid / f"{rid}.jsonl"
    with open(live, "ab") as fh:
        fh.write(b'{"version":2,"event_id":"' + rid.encode() + b':3","seq":3,')
        fh.flush()
        os.fsync(fh.fileno())
    old = time.time() - 30 * 86400.0
    os.utime(live, (old, old))
    counters = _sweep(tmp_path, ttl_days=14, max_runs_per_session=0, max_bytes_per_session=0)
    assert counters["archived_files"] == 1

    _simulate_restart()
    assert rj.append_run_event(sid, rid, "token", {"text": "three"}, session_dir=tmp_path)["seq"] == 3
    _simulate_restart()
    assert rj.append_run_event(sid, rid, "token", {"text": "four"}, session_dir=tmp_path)["seq"] == 4
    _simulate_restart()
    read = rj.read_run_events(sid, rid, session_dir=tmp_path, validated_recovery=True)
    assert [int(e["seq"]) for e in read["events"]] == [1, 2, 3, 4], read
    assert read["malformed"] == []
