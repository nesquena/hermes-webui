"""Equivalence tests for the read_run_events(after_seq=...) offset index."""

import builtins
import gzip
import json
import os
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
    monkeypatch.delattr(os, "pread")
    got = rj.read_run_events(SID, RID, after_seq=510, session_dir=tmp_path)
    assert [event["seq"] for event in got["events"]] == list(range(511, 521))
    assert got == _oracle(path, 510)


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
