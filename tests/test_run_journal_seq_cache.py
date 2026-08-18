"""Regression tests for the in-memory run-journal seq cache.

Repeat ``append_run_event`` calls used to re-read (and re-parse) the entire
journal file on every append via ``_next_seq``, which is O(n) per append and
O(n^2) over a run. The cache seeds once per path and then increments in memory
while staying consistent with ``RunJournalWriter`` (both share one cache under
the same per-path lock).
"""
import subprocess
import sys
import threading
from pathlib import Path

import pytest  # noqa: F401  # top-level import keeps pytest collection unambiguous

from api import run_journal


_REPO_ROOT = Path(__file__).resolve().parents[1]


def _capability(session_id, session_dir, *, reactivate_retired=False):
    return run_journal.activate_run_journal_session(
        session_id,
        session_dir=session_dir,
        reactivate_retired=reactivate_retired,
    )


def _append_run_event(session_id, run_id, event_name, payload=None, **kwargs):
    return run_journal.append_run_event(
        session_id,
        run_id,
        event_name,
        payload,
        **kwargs,
        _incarnation=_capability(session_id, kwargs.get("session_dir")),
    )


def _writer(session_id, run_id, *, session_dir=None):
    return run_journal.RunJournalWriter(
        session_id,
        run_id,
        session_dir=session_dir,
        incarnation=_capability(session_id, session_dir),
    )


def test_append_run_event_seeds_seq_once_and_stays_gapless(tmp_path, monkeypatch):
    calls = {"next_seq": 0, "read_jsonl": 0}
    real_next_seq = run_journal._next_seq
    real_read_jsonl = run_journal._read_jsonl

    def counting_next_seq(path):
        calls["next_seq"] += 1
        return real_next_seq(path)

    def counting_read_jsonl(path):
        calls["read_jsonl"] += 1
        return real_read_jsonl(path)

    monkeypatch.setattr(run_journal, "_next_seq", counting_next_seq)
    monkeypatch.setattr(run_journal, "_read_jsonl", counting_read_jsonl)

    n = 25
    seqs = [
        _append_run_event(
            "sess_cache", "run_cache", "token", {"text": str(i)}, session_dir=tmp_path
        )["seq"]
        for i in range(n)
    ]

    assert seqs == list(range(1, n + 1))
    # Seeded from the file exactly once; every later append is in-memory only.
    assert calls["next_seq"] == 1
    assert calls["read_jsonl"] <= 1
    path = tmp_path / "_run_journal" / "sess_cache" / "run_cache.jsonl"
    generation_record = run_journal._read_run_generation_record(path)
    assert generation_record is not None
    with run_journal._SEQ_CACHE_LOCK:
        assert run_journal._SEQ_CACHE[str(path)] == (
            generation_record[0],
            path.stat().st_size,
            n + 1,
        )


def test_writer_and_free_function_share_one_gapless_sequence(tmp_path):
    writer = _writer("sess_shared", "run_shared", session_dir=tmp_path)
    a = writer.append_sse_event("token", {"text": "a"})
    b = _append_run_event(
        "sess_shared", "run_shared", "token", {"text": "b"}, session_dir=tmp_path
    )
    c = writer.append_sse_event("token", {"text": "c"})
    d = _append_run_event(
        "sess_shared", "run_shared", "done", {"session": {}}, session_dir=tmp_path
    )

    assert [a["seq"], b["seq"], c["seq"], d["seq"]] == [1, 2, 3, 4]

    journal = run_journal.read_run_events("sess_shared", "run_shared", session_dir=tmp_path)
    file_seqs = sorted(event["seq"] for event in journal["events"])
    assert file_seqs == [1, 2, 3, 4]


def test_explicit_seq_discards_cache_and_rebuilds(tmp_path):
    # A caller-supplied seq conservatively discards the cache; a later automatic
    # append must rebuild from the physical journal rather than trusting a
    # possibly stale next-seq candidate.
    _append_run_event(
        "sess_expl", "run_expl", "token", {"text": "x"}, session_dir=tmp_path, seq=5
    )
    path = tmp_path / "_run_journal" / "sess_expl" / "run_expl.jsonl"
    with run_journal._SEQ_CACHE_LOCK:
        assert str(path) not in run_journal._SEQ_CACHE
    nxt = _append_run_event(
        "sess_expl", "run_expl", "token", {"text": "y"}, session_dir=tmp_path
    )
    assert nxt["seq"] == 6


def test_delete_evicts_seq_cache_so_recreated_run_restarts(tmp_path):
    _append_run_event(
        "sess_del", "run_del", "token", {"text": "one"}, session_dir=tmp_path
    )
    _append_run_event(
        "sess_del", "run_del", "token", {"text": "two"}, session_dir=tmp_path
    )

    assert run_journal.delete_run_journal("sess_del", session_dir=tmp_path) is True

    incarnation = _capability("sess_del", tmp_path, reactivate_retired=True)
    restarted = run_journal.append_run_event(
        "sess_del",
        "run_del",
        "token",
        {"text": "fresh"},
        session_dir=tmp_path,
        _incarnation=incarnation,
    )
    assert restarted["seq"] == 1


def test_delete_evicts_seq_cache_concurrently_without_crash(tmp_path):
    """delete_run_journal must evict _SEQ_CACHE under a shared lock.

    The eviction iterates the whole ``_SEQ_CACHE`` to drop the deleted session's
    keys. It ran outside any mutex the append path holds, so a concurrent append
    on ANOTHER session — which inserts a fresh key — mutated the dict mid-iteration
    and raised ``RuntimeError: dictionary changed size during iteration``. Both
    paths now take ``_SEQ_CACHE_LOCK``, so the eviction and inserts serialize.
    """
    # Seed the cache with many keys so an eviction sweep iterates a wide dict,
    # widening the window for a concurrent insert to collide.
    for s in range(60):
        _append_run_event(
            f"sess_seed{s}", "run", "token", {"text": "x"}, session_dir=tmp_path
        )

    errors: list[BaseException] = []
    errors_lock = threading.Lock()
    stop = threading.Event()

    def deleter():
        i = 0
        while not stop.is_set():
            sid = f"sess_del{i}"
            try:
                _append_run_event(
                    sid, "run", "token", {"text": "d"}, session_dir=tmp_path
                )
                run_journal.delete_run_journal(sid, session_dir=tmp_path)
            except BaseException as exc:  # noqa: BLE001 - recorded for the assert
                with errors_lock:
                    errors.append(exc)
            i += 1

    def inserter(base):
        i = 0
        while not stop.is_set():
            try:
                # Each append to a brand-new session inserts a fresh cache key,
                # racing the deleter's eviction comprehension.
                _append_run_event(
                    f"sess_ins{base}_{i}", "run", "token", {"text": "i"},
                    session_dir=tmp_path,
                )
            except BaseException as exc:  # noqa: BLE001 - recorded for the assert
                with errors_lock:
                    errors.append(exc)
            i += 1

    workers = [threading.Thread(target=deleter)]
    workers += [threading.Thread(target=inserter, args=(b,)) for b in range(4)]
    for w in workers:
        w.start()
    # Let them contend briefly, then wind down.
    for _ in range(200):
        run_journal.delete_run_journal("sess_seed0", session_dir=tmp_path)
    stop.set()
    for w in workers:
        w.join(timeout=10.0)

    assert not any(w.is_alive() for w in workers), "worker threads did not finish"
    assert not errors, f"eviction raced an insert: {errors[:3]}"


def test_cross_process_delete_recreate_refreshes_parent_seq_cache(tmp_path):
    session_id = "sess_cross_process_recreate"
    run_id = "run_cross_process_recreate"
    for i in range(5):
        event = _append_run_event(
            session_id,
            run_id,
            "token",
            {"text": str(i)},
            session_dir=tmp_path,
        )
        assert event["seq"] == i + 1

    child = subprocess.run(
        [
            sys.executable,
            "-c",
            """
from pathlib import Path
import sys
from api import run_journal

root = Path(sys.argv[1])
sid = sys.argv[2]
rid = sys.argv[3]
run_journal.delete_run_journal(sid, session_dir=root)
incarnation = run_journal.activate_run_journal_session(
    sid, session_dir=root, reactivate_retired=True
)
event = run_journal.append_run_event(
    sid, rid, "token", {"text": "child"}, session_dir=root,
    _incarnation=incarnation,
)
print(event["seq"], flush=True)
""",
            str(tmp_path),
            session_id,
            run_id,
        ],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    assert child.stdout.strip() == "1"

    parent_event = _append_run_event(
        session_id,
        run_id,
        "token",
        {"text": "parent"},
        session_dir=tmp_path,
    )
    assert parent_event["seq"] == 2
    events = run_journal.read_run_events(
        session_id, run_id, session_dir=tmp_path
    )["events"]
    assert [event["seq"] for event in events] == [1, 2]


def test_long_lived_processes_alternate_with_unique_contiguous_seqs(tmp_path):
    session_id = "sess_cross_process_alternate"
    run_id = "run_cross_process_alternate"
    coordination = tmp_path / "coordination"
    coordination.mkdir()
    rounds = 4
    worker = """
from pathlib import Path
import sys
import time
from api import run_journal

root = Path(sys.argv[1])
coordination = Path(sys.argv[2])
sid = sys.argv[3]
rid = sys.argv[4]
label = sys.argv[5]
rounds = int(sys.argv[6])
other = "b" if label == "a" else "a"
incarnation = run_journal.activate_run_journal_session(sid, session_dir=root)

def wait_for(path):
    deadline = time.monotonic() + 20
    while not path.exists():
        if time.monotonic() >= deadline:
            raise TimeoutError(path)
        time.sleep(0.01)

for index in range(rounds):
    wait_for(coordination / f"go-{label}-{index}")
    event = run_journal.append_run_event(
        sid, rid, "token", {"worker": label, "index": index}, session_dir=root,
        _incarnation=incarnation,
    )
    (coordination / f"result-{label}-{index}").write_text(
        str(event["seq"]), encoding="ascii"
    )
    next_index = index if label == "a" else index + 1
    (coordination / f"go-{other}-{next_index}").touch()
"""
    processes = [
        subprocess.Popen(
            [
                sys.executable,
                "-c",
                worker,
                str(tmp_path),
                str(coordination),
                session_id,
                run_id,
                label,
                str(rounds),
            ],
            cwd=_REPO_ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for label in ("a", "b")
    ]
    (coordination / "go-a-0").touch()
    outputs = [process.communicate(timeout=30) for process in processes]
    assert all(process.returncode == 0 for process in processes), outputs

    seqs = [
        int((coordination / f"result-{label}-{index}").read_text(encoding="ascii"))
        for index in range(rounds)
        for label in ("a", "b")
    ]
    assert sorted(seqs) == list(range(1, rounds * 2 + 1))
    events = run_journal.read_run_events(
        session_id, run_id, session_dir=tmp_path
    )["events"]
    assert sorted(event["seq"] for event in events) == list(range(1, rounds * 2 + 1))


def test_concurrent_appends_produce_unique_gapless_seqs(tmp_path):
    threads = []
    results: list[int] = []
    results_lock = threading.Lock()

    def worker(i):
        event = _append_run_event(
            "sess_conc", "run_conc", "token", {"text": str(i)}, session_dir=tmp_path
        )
        with results_lock:
            results.append(event["seq"])

    for i in range(40):
        threads.append(threading.Thread(target=worker, args=(i,)))
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(results) == list(range(1, 41))
