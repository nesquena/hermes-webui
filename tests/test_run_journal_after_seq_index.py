"""Equivalence tests for the read_run_events(after_seq=...) offset index."""

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
    return json.dumps({"seq": seq, "event": "token", "payload": {"t": "x" * 20, **extra}}) + "\n"


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
    _write(path, "".join(_row(s, u="é\u2028") for s in seqs) + "\xff\n")
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
