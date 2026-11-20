"""Equivalence tests for the read_run_events(after_seq=...) offset index."""

import builtins
import gzip
import io
import json
import os
import threading
import time

import pytest

from api import run_journal as rj

SID, RID = "sess_idx", "run_idx"


@pytest.fixture(autouse=True)
def _clear_index():
    rj._after_seq_index.clear()
    yield
    rj._after_seq_index.clear()


def _oracle(path, after_seq):
    events, malformed = rj._read_jsonl(path)
    events = [e for e in events if int(e.get("seq") or 0) > int(after_seq)]
    return {"session_id": SID, "run_id": RID, "events": events, "malformed": malformed}


def _row(seq, **extra):
    return json.dumps(
        {"seq": seq, "event": "token", "payload": {"t": "x" * 20, **extra}},
        ensure_ascii=False,
    ) + "\n"


def _write(path, text, mode="w"):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, mode, encoding="utf-8", newline="") as fh:
        fh.write(text)


def _check(tmp_path, seqs):
    path = rj._run_path(SID, RID, session_dir=tmp_path)
    for after in seqs:
        got = rj.read_run_events(SID, RID, after_seq=after, session_dir=tmp_path)
        assert got == _oracle(path, after), after
    return path


def _seqs(n):
    return [0, -1, 1, 255, 256, 257, n // 2, n - 20, n - 1, n, n + 5]


def test_plain_and_repeated(tmp_path):
    path = rj._run_path(SID, RID, session_dir=tmp_path)
    _write(path, "".join(_row(i) for i in range(1, 2001)))
    _check(tmp_path, _seqs(2000))
    _check(tmp_path, _seqs(2000))
    assert len(rj._after_seq_index[str(path)]["offs"]) > 2


def test_append_between_calls_and_partial_tail(tmp_path):
    path = rj._run_path(SID, RID, session_dir=tmp_path)
    _write(path, "".join(_row(i) for i in range(1, 1001)))
    _check(tmp_path, _seqs(1000))
    _write(path, "".join(_row(i) for i in range(1001, 1600)) + '{"seq": 1600, "par', "a")
    _check(tmp_path, _seqs(1600))
    _write(path, 'tial": 1}\n' + _row(1601), "a")
    _check(tmp_path, _seqs(1601))


def test_malformed_lines_keep_absolute_line_numbers(tmp_path):
    path = rj._run_path(SID, RID, session_dir=tmp_path)
    rows = []
    for i in range(1, 1500):
        rows.append(_row(i))
        if i % 300 == 0:
            rows.append("not json\n\n[1,2]\n   \n")
    rows.append("garbage tail")
    _write(path, "".join(rows))
    _check(tmp_path, _seqs(1500))
    _write(path, "\nmore junk\n" + _row(2000), "a")
    _check(tmp_path, _seqs(2000))


def test_truncation_and_inode_swap(tmp_path):
    path = rj._run_path(SID, RID, session_dir=tmp_path)
    _write(path, "".join(_row(i) for i in range(1, 1200)))
    _check(tmp_path, _seqs(1200))
    _write(path, "".join(_row(i) for i in range(1, 300)))  # shrink, same inode
    _check(tmp_path, _seqs(300))
    tmp = path.with_suffix(".tmp")
    _write(tmp, "".join(_row(i, v=2) for i in range(1, 900)) + "bad\n")
    os.replace(tmp, path)  # new inode
    _check(tmp_path, _seqs(900))


def test_non_monotonic_and_crlf_and_utf8(tmp_path):
    path = rj._run_path(SID, RID, session_dir=tmp_path)
    seqs = list(range(1, 700)) + [5, 3] + list(range(700, 1300))
    raw = "".join(_row(s, u="é\u2028") for s in seqs).encode("utf-8") + b"\xff\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    assert b"\xe2\x80\xa8" in raw and raw.endswith(b"\xff\n")
    _check(tmp_path, [0, 4, 5, 6, 600, 1299, 2000])
    _check(tmp_path, [0, 4, 5, 6, 600, 1299, 2000])
    _write(path, "\r\n".join(_row(s).strip() for s in range(1, 600)) + "\r\n")
    _check(tmp_path, [0, 10, 598, 700])


def test_missing_bad_seq_and_gz(tmp_path):
    _check(tmp_path, [0, 5])
    path = rj._run_path(SID, RID, session_dir=tmp_path)
    _write(path, _row(1) + json.dumps({"seq": "abc"}) + "\n")
    with pytest.raises(ValueError):
        rj.read_run_events(SID, RID, after_seq=0, session_dir=tmp_path)
    _write(path, "".join(_row(i) for i in range(1, 400)))
    archived = rj._archive_path_for(path)
    archived.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(archived, "wt", encoding="utf-8") as gz:
        gz.write("".join(_row(i) for i in range(1, 200)))
    assert rj._read_jsonl_after_seq_indexed(path, 0) is None
    _check(tmp_path, [0, 150, 399])


def test_warm_tail_does_not_require_pread(tmp_path, monkeypatch):
    path = rj._run_path(SID, RID, session_dir=tmp_path)
    _write(path, "".join(_row(i) for i in range(1, 521)))
    _check(tmp_path, [0])
    monkeypatch.delattr(os, "pread", raising=False)
    got = rj.read_run_events(SID, RID, after_seq=510, session_dir=tmp_path)
    assert [event["seq"] for event in got["events"]] == list(range(511, 521))
    assert got == _oracle(path, 510)


def _event_line_len(seq, text, padding=""):
    event = {
        "version": 2,
        "event_id": f"{RID}:{seq}",
        "seq": seq,
        "run_id": RID,
        "session_id": SID,
        "event": "token",
        "type": "token",
        "created_at": 0.0,
        "terminal": False,
        "terminal_state": None,
        "payload": {"text": text, "pad": padding},
    }
    return len((json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n").encode())


@pytest.mark.parametrize("interleaved_replay", [False, True])
@pytest.mark.parametrize("append_after_regrowth", [False, True])
def test_failed_append_replay_cannot_skip_committed_regrowth(
    tmp_path, monkeypatch, interleaved_replay, append_after_regrowth,
):
    """A speculative indexed row loses all authority when its append rolls back."""
    monkeypatch.setenv(rj._FSYNC_MODE_ENV, rj._FSYNC_MODE_EAGER)
    path = rj._run_path(SID, RID, session_dir=tmp_path)
    for seq in range(1, 256):
        rj.append_run_event(
            SID, RID, "token", {"text": f"prefix {seq}", "pad": "x" * 20},
            session_dir=tmp_path, created_at=0.0,
        )
    committed_prefix = path.read_bytes()
    observed = {}
    real_fsync = os.fsync

    def fail_fsync_after_optional_replay(fd):
        if "size" not in observed and os.fstat(fd).st_ino == path.stat().st_ino:
            if interleaved_replay:
                speculative = rj.read_run_events(SID, RID, after_seq=0, session_dir=tmp_path)
                assert speculative["events"][-1]["seq"] == 256
                assert str(path) in rj._after_seq_index
            observed["size"] = path.stat().st_size
            observed["inode"] = path.stat().st_ino
            raise OSError("injected append failure after complete row became visible")
        return real_fsync(fd)

    monkeypatch.setattr(os, "fsync", fail_fsync_after_optional_replay)
    with pytest.raises(OSError, match="injected append failure"):
        rj.append_run_event(
            SID, RID, "token", {"text": "rolled back", "pad": "F" * 20000},
            session_dir=tmp_path, created_at=0.0,
        )
    assert path.read_bytes() == committed_prefix

    rj.append_run_event(
        SID, RID, "token", {"text": "committed 256", "pad": ""},
        session_dir=tmp_path, created_at=0.0,
    )
    final_without_padding = (
        len(committed_prefix)
        + _event_line_len(256, "committed 256")
        + _event_line_len(257, "committed 257")
    )
    padding = observed["size"] - final_without_padding
    assert padding > 0
    rj.append_run_event(
        SID, RID, "token", {"text": "committed 257", "pad": "P" * padding},
        session_dir=tmp_path, created_at=0.0,
    )
    assert path.stat().st_ino == observed["inode"]
    assert path.stat().st_size == observed["size"]
    if append_after_regrowth:
        rj.append_run_event(
            SID, RID, "token", {"text": "committed 258", "pad": ""},
            session_dir=tmp_path, created_at=0.0,
        )
        assert path.stat().st_size > observed["size"]
    assert [event["seq"] for event in rj._read_jsonl(path)[0]][-2:] == (
        [257, 258] if append_after_regrowth else [256, 257]
    )

    got = rj.read_run_events(SID, RID, after_seq=256, max_seq=257, session_dir=tmp_path)
    assert [event["seq"] for event in got["events"]] == [257]
    assert got["events"][0]["payload"]["text"] == "committed 257"


@pytest.mark.parametrize("hold_suffix_read", [False, True], ids=["control", "rollback_during_read"])
def test_checkpoint_and_suffix_come_from_one_coherent_read(
    tmp_path, monkeypatch, hold_suffix_read,
):
    """Checkpoint validation and replay must not straddle producer generations."""
    monkeypatch.setenv(rj._FSYNC_MODE_ENV, rj._FSYNC_MODE_EAGER)
    path = rj._run_path(SID, RID, session_dir=tmp_path)
    for seq in range(1, 256):
        rj.append_run_event(
            SID, RID, "token", {"text": f"prefix {seq}", "pad": "x" * 20},
            session_dir=tmp_path, created_at=0.0,
        )
    committed_prefix = path.read_bytes()

    observed = {}
    suffix_read_waiting = threading.Event()
    allow_suffix_read = threading.Event()
    failed_append_parked = threading.Event()
    release_failed_fsync = threading.Event()
    replay_result = []
    thread_errors = []
    real_fsync = os.fsync
    real_open = builtins.open

    def replay_after_speculative_checkpoint():
        try:
            replay_result.append(
                rj.read_run_events(SID, RID, after_seq=256, session_dir=tmp_path)
            )
        except BaseException as exc:  # surface thread failures in the test body
            thread_errors.append(exc)

    def fail_fsync_with_reader_at_suffix(fd):
        if "size" not in observed and os.fstat(fd).st_ino == path.stat().st_ino:
            speculative = rj.read_run_events(SID, RID, after_seq=0, session_dir=tmp_path)
            assert speculative["events"][-1]["seq"] == 256
            observed["size"] = path.stat().st_size
            assert rj._after_seq_index[str(path)]["offs"][-1] == observed["size"]
            if hold_suffix_read:
                observed["replay_armed"] = True
                reader = threading.Thread(target=replay_after_speculative_checkpoint)
                reader.start()
                observed["reader"] = reader
                assert suffix_read_waiting.wait(20), "reader never reached its suffix read"
                failed_append_parked.set()
                assert release_failed_fsync.wait(20), "test did not release failed fsync"
            raise OSError("injected append failure with a replay at the suffix read")
        return real_fsync(fd)

    def open_with_suffix_barrier(file, *args, **kwargs):
        is_journal = os.fspath(file) == os.fspath(path)
        if not hold_suffix_read or not is_journal or not args or args[0] != "rb":
            return real_open(file, *args, **kwargs)
        kwargs = dict(kwargs)
        kwargs["buffering"] = 0
        raw = real_open(file, *args, **kwargs)

        class RawSuffixBarrier:
            def __getattr__(self, name):
                return getattr(raw, name)

            def _wait_at_old_eof(self):
                # BufferedReader.read() may issue several raw reads. Let the
                # coherent-snapshot candidate consume the old segment, then
                # pause the next raw read at the old EOF. On the old split
                # reader this is the first raw read of its separate suffix read.
                if (observed.get("replay_armed")
                        and not observed.get("raw_barrier_hit")
                        and raw.tell() == observed.get("size")):
                    observed["raw_barrier_hit"] = True
                    suffix_read_waiting.set()
                    assert allow_suffix_read.wait(20), "test did not release suffix read"

            def read(self, size=-1):
                self._wait_at_old_eof()
                return raw.read(size)

            def readall(self):
                chunks = []
                while True:
                    chunk = self.read(1024 * 1024)
                    if not chunk:
                        return b"".join(chunks)
                    chunks.append(chunk)

            def readinto(self, buffer):
                self._wait_at_old_eof()
                return raw.readinto(buffer)

        return io.BufferedReader(RawSuffixBarrier())

    monkeypatch.setattr(os, "fsync", fail_fsync_with_reader_at_suffix)
    monkeypatch.setattr(builtins, "open", open_with_suffix_barrier)

    def failed_append():
        try:
            with pytest.raises(OSError, match="injected append failure"):
                rj.append_run_event(
                    SID, RID, "token", {"text": "rolled back", "pad": "F" * 20000},
                    session_dir=tmp_path, created_at=0.0,
                )
        except BaseException as exc:
            thread_errors.append(exc)

    if hold_suffix_read:
        writer = threading.Thread(target=failed_append)
        writer.start()
        assert failed_append_parked.wait(20), (thread_errors, suffix_read_waiting.is_set())
        release_failed_fsync.set()
        writer.join(20)
        assert not writer.is_alive()
    else:
        failed_append()
    assert not thread_errors
    assert path.read_bytes() == committed_prefix

    rj.append_run_event(
        SID, RID, "token", {"text": "committed 256", "pad": ""},
        session_dir=tmp_path, created_at=0.0,
    )
    final_without_padding = (
        len(committed_prefix)
        + _event_line_len(256, "committed 256")
        + _event_line_len(257, "committed 257")
    )
    padding = observed["size"] - final_without_padding
    assert padding > 0
    rj.append_run_event(
        SID, RID, "token", {"text": "committed 257", "pad": "P" * padding},
        session_dir=tmp_path, created_at=0.0,
    )
    assert path.stat().st_size == observed["size"]
    rj.append_run_event(
        SID, RID, "token", {"text": "committed 258", "pad": ""},
        session_dir=tmp_path, created_at=0.0,
    )
    assert [event["seq"] for event in rj._read_jsonl(path)[0]][-3:] == [256, 257, 258]

    if hold_suffix_read:
        allow_suffix_read.set()
        observed["reader"].join(20)
        assert not observed["reader"].is_alive()
        assert not thread_errors
        assert len(replay_result) == 1
        got = replay_result[0]
    else:
        got = rj.read_run_events(SID, RID, after_seq=256, session_dir=tmp_path)
    assert [event["seq"] for event in got["events"]] == [257, 258]


def test_oversized_malformed_rows_are_not_cached(tmp_path, monkeypatch):
    monkeypatch.setattr(rj, "_AFTER_SEQ_INDEX_MAX_MALFORMED_BYTES", 64, raising=False)
    path = rj._run_path(SID, RID, session_dir=tmp_path)
    damaged = "x" * 100 + "é"
    _write(path, "".join(_row(i) for i in range(1, 301)) + damaged + "\n" + _row(301))
    first = rj.read_run_events(SID, RID, after_seq=250, session_dir=tmp_path)
    assert first == _oracle(path, 250)
    assert first["malformed"] == [{"line": 301, "raw": damaged}]
    assert str(path) not in rj._after_seq_index
    second = rj.read_run_events(SID, RID, after_seq=250, session_dir=tmp_path)
    assert second == first == _oracle(path, 250)
    assert str(path) not in rj._after_seq_index


def _append_completed_run(tmp_path, run_id):
    for seq, event in enumerate(("token", "done", "stream_end"), start=1):
        rj.append_run_event(SID, run_id, event, {"seq": seq}, session_dir=tmp_path)
    path = rj._run_path(SID, run_id, session_dir=tmp_path)
    aged = time.time() - 7200
    os.utime(path, (aged, aged))
    return path


def test_live_open_retention_race_falls_back_to_archive(tmp_path, monkeypatch):
    if not rj._DIR_FD_OK:
        pytest.skip("journal archive replay requires directory file descriptors")
    path = _append_completed_run(tmp_path, RID)
    real_open = builtins.open
    swept = []

    def open_with_sweep(file, *args, **kwargs):
        if os.fspath(file) == os.fspath(path) and args and args[0] == "rb" and not swept:
            counters = rj.sweep_run_journal(
                session_dir=tmp_path, ttl_days=0.001,
                max_runs_per_session=0, max_bytes_per_session=0,
            )
            assert counters["archived_files"] == 1
            assert counters["errors"] == 0
            swept.append(counters)
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", open_with_sweep)
    got = rj.read_run_events(SID, RID, after_seq=1, session_dir=tmp_path)
    assert swept and not path.exists()
    assert [event["seq"] for event in got["events"]] == [2, 3]


def test_already_archived_after_seq_control(tmp_path):
    if not rj._DIR_FD_OK:
        pytest.skip("journal archive replay requires directory file descriptors")
    path = _append_completed_run(tmp_path, RID)
    counters = rj.sweep_run_journal(
        session_dir=tmp_path, ttl_days=0.001,
        max_runs_per_session=0, max_bytes_per_session=0,
    )
    assert counters["archived_files"] == 1 and counters["errors"] == 0
    assert not path.exists()
    got = rj.read_run_events(SID, RID, after_seq=1, session_dir=tmp_path)
    assert [event["seq"] for event in got["events"]] == [2, 3]


def test_microbenchmark_tail_read(tmp_path):
    path = rj._run_path(SID, RID, session_dir=tmp_path)
    n = 50_000
    _write(path, "".join(_row(i, pad="y" * 600) for i in range(1, n + 1)))
    t0 = time.perf_counter()
    for _ in range(5):
        _oracle(path, n - 20)
    before = (time.perf_counter() - t0) / 5
    rj.read_run_events(SID, RID, after_seq=0, session_dir=tmp_path)  # builds index
    t0 = time.perf_counter()
    for _ in range(5):
        got = rj.read_run_events(SID, RID, after_seq=n - 20, session_dir=tmp_path)
    after = (time.perf_counter() - t0) / 5
    assert len(got["events"]) == 20
    print(f"\n[after_seq bench] {path.stat().st_size / 1e6:.1f} MB, {n} events: "
          f"full={before * 1000:.1f} ms indexed={after * 1000:.2f} ms")
    assert after < before
