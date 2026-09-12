"""Regression tests for the in-memory run-journal seq cache.

Repeat ``append_run_event`` calls used to re-read (and re-parse) the entire
journal file on every append via ``_next_seq``, which is O(n) per append and
O(n^2) over a run. The cache seeds once per path and then increments in memory
while staying consistent with ``RunJournalWriter`` (both share one cache under
the same per-path lock).
"""
import threading

import pytest  # noqa: F401  # top-level import keeps pytest collection unambiguous

from api import run_journal


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
        run_journal.append_run_event(
            "sess_cache", "run_cache", "token", {"text": str(i)}, session_dir=tmp_path
        )["seq"]
        for i in range(n)
    ]

    assert seqs == list(range(1, n + 1))
    # Seeded from the file exactly once; every later append is in-memory only.
    assert calls["next_seq"] == 1
    assert calls["read_jsonl"] <= 1


def test_writer_and_free_function_share_one_gapless_sequence(tmp_path):
    writer = run_journal.RunJournalWriter("sess_shared", "run_shared", session_dir=tmp_path)
    a = writer.append_sse_event("token", {"text": "a"})
    b = run_journal.append_run_event(
        "sess_shared", "run_shared", "token", {"text": "b"}, session_dir=tmp_path
    )
    c = writer.append_sse_event("token", {"text": "c"})
    d = run_journal.append_run_event(
        "sess_shared", "run_shared", "done", {"session": {}}, session_dir=tmp_path
    )

    assert [a["seq"], b["seq"], c["seq"], d["seq"]] == [1, 2, 3, 4]

    journal = run_journal.read_run_events("sess_shared", "run_shared", session_dir=tmp_path)
    file_seqs = sorted(event["seq"] for event in journal["events"])
    assert file_seqs == [1, 2, 3, 4]


def test_writer_and_free_append_keep_physical_sequence_order(tmp_path, monkeypatch):
    """A direct append cannot overtake a writer after it reserves its seq."""
    writer = run_journal.RunJournalWriter(
        "sess_order", "run_order", session_dir=tmp_path
    )
    real_append = run_journal.append_run_event
    writer_at_append = threading.Event()
    release_writer = threading.Event()
    errors: list[BaseException] = []

    def controlled_append(*args, **kwargs):
        if threading.current_thread().name == "journal-writer":
            writer_at_append.set()
            if not release_writer.wait(timeout=10):
                raise TimeoutError("writer interleave was not released")
        return real_append(*args, **kwargs)

    monkeypatch.setattr(run_journal, "append_run_event", controlled_append)

    def write_from_writer():
        try:
            writer.append_sse_event("token", {"text": "writer"})
        except BaseException as exc:  # noqa: BLE001 - asserted below
            errors.append(exc)

    thread = threading.Thread(target=write_from_writer, name="journal-writer")
    thread.start()
    assert writer_at_append.wait(timeout=10), "writer never reached append"
    run_journal.append_run_event(
        "sess_order",
        "run_order",
        "steer_delivered",
        {"text": "direct"},
        session_dir=tmp_path,
    )
    release_writer.set()
    thread.join(timeout=10)

    assert not thread.is_alive()
    assert errors == []
    journal = run_journal.read_run_events(
        "sess_order", "run_order", session_dir=tmp_path
    )
    assert [event["seq"] for event in journal["events"]] == [1, 2]


def test_explicit_seq_keeps_cache_from_reissuing(tmp_path):
    # A caller-supplied seq must push the cache forward so a later cache append
    # does not collide with it.
    run_journal.append_run_event(
        "sess_expl", "run_expl", "token", {"text": "x"}, session_dir=tmp_path, seq=5
    )
    nxt = run_journal.append_run_event(
        "sess_expl", "run_expl", "token", {"text": "y"}, session_dir=tmp_path
    )
    assert nxt["seq"] == 6


def test_accept_transaction_does_not_call_runtime_after_terminal(tmp_path):
    run_journal.append_run_event(
        "sess_terminal", "run_terminal", "done", {"session": {}}, session_dir=tmp_path
    )
    writer = run_journal.RunJournalWriter(
        "sess_terminal", "run_terminal", session_dir=tmp_path
    )
    called = []

    accepted, event, reason, error = writer.accept_and_append_if_nonterminal(
        "steer_delivered",
        {"text": "too late"},
        lambda: called.append(True) or True,
    )

    assert (accepted, event, reason, error) == (False, None, "terminal", None)
    assert called == []
    journal = run_journal.read_run_events(
        "sess_terminal", "run_terminal", session_dir=tmp_path
    )
    assert [item["event"] for item in journal["events"]] == ["done"]


def test_accept_transaction_orders_delivery_before_concurrent_terminal(tmp_path):
    writer = run_journal.RunJournalWriter(
        "sess_accept", "run_accept", session_dir=tmp_path
    )
    terminal_started = threading.Event()
    terminal_finished = threading.Event()

    def append_terminal():
        terminal_started.set()
        run_journal.append_run_event(
            "sess_accept",
            "run_accept",
            "done",
            {"session": {}},
            session_dir=tmp_path,
        )
        terminal_finished.set()

    def accept():
        thread = threading.Thread(target=append_terminal)
        thread.start()
        assert terminal_started.wait(timeout=10)
        assert not terminal_finished.wait(timeout=0.1), "terminal bypassed transaction lock"
        return True

    accepted, event, reason, error = writer.accept_and_append_if_nonterminal(
        "steer_delivered",
        {"text": "in time"},
        accept,
    )
    assert accepted is True
    assert event is not None
    assert reason is None
    assert error is None
    assert terminal_finished.wait(timeout=10)

    journal = run_journal.read_run_events(
        "sess_accept", "run_accept", session_dir=tmp_path
    )
    assert [item["event"] for item in journal["events"]] == [
        "steer_delivered",
        "done",
    ]


def test_accept_transaction_publishes_before_concurrent_terminal(tmp_path):
    writer = run_journal.RunJournalWriter(
        "sess_publish", "run_publish", session_dir=tmp_path
    )
    terminal_started = threading.Event()
    terminal_finished = threading.Event()
    published = []

    def append_terminal():
        terminal_started.set()
        run_journal.append_run_event(
            "sess_publish",
            "run_publish",
            "done",
            {"session": {}},
            session_dir=tmp_path,
        )
        terminal_finished.set()

    def accept():
        threading.Thread(target=append_terminal).start()
        assert terminal_started.wait(timeout=10)
        assert not terminal_finished.wait(timeout=0.1)
        return True

    def publish(event):
        # Publication is in the same ordering domain as append: the terminal
        # writer cannot commit (and then enqueue) before this callback runs.
        assert not terminal_finished.is_set()
        published.append(event["event_id"])

    accepted, event, reason, error = writer.accept_and_append_if_nonterminal(
        "steer_delivered",
        {"text": "publish first"},
        accept,
        publish=publish,
    )
    assert accepted is True and event is not None and reason is None and error is None
    assert published == [event["event_id"]]
    assert terminal_finished.wait(timeout=10)


def test_normal_sse_publish_cannot_be_overtaken_by_steer(tmp_path, monkeypatch):
    writer = run_journal.RunJournalWriter(
        "sess_total_order", "run_total_order", session_dir=tmp_path
    )
    normal_publish_started = threading.Event()
    release_normal_publish = threading.Event()
    steer_finished = threading.Event()
    published = []
    errors: list[BaseException] = []

    def publish_normal(event):
        published.append(event["event_id"])
        normal_publish_started.set()
        if not release_normal_publish.wait(timeout=10):
            raise TimeoutError("normal SSE publication was not released")

    def write_normal():
        try:
            writer.append_and_publish_sse_event(
                "token",
                {"text": "before steer"},
                publish_normal,
            )
        except BaseException as exc:  # noqa: BLE001 - asserted below
            errors.append(exc)

    def write_steer():
        try:
            accepted, event, reason, error = writer.accept_and_append_if_nonterminal(
                "steer_delivered",
                {"text": "correct course"},
                lambda: True,
                publish=lambda item: published.append(item["event_id"]),
            )
            assert accepted is True and event is not None and reason is None and error is None
        except BaseException as exc:  # noqa: BLE001 - asserted below
            errors.append(exc)
        finally:
            steer_finished.set()

    normal_thread = threading.Thread(target=write_normal)
    normal_thread.start()
    assert normal_publish_started.wait(timeout=10)

    steer_thread = threading.Thread(target=write_steer)
    steer_thread.start()
    assert not steer_finished.wait(timeout=0.1), "Steer overtook normal SSE publication"

    release_normal_publish.set()
    normal_thread.join(timeout=10)
    steer_thread.join(timeout=10)

    assert not normal_thread.is_alive() and not steer_thread.is_alive()
    assert errors == []
    journal = run_journal.read_run_events(
        "sess_total_order", "run_total_order", session_dir=tmp_path
    )
    journal_ids = [event["event_id"] for event in journal["events"]]
    assert published == journal_ids == ["run_total_order:1", "run_total_order:2"]

    # The replay projection must expose the same order the live queue observed.
    from api import routes

    monkeypatch.setattr(
        routes,
        "find_run_summary",
        lambda _stream_id: {
            "session_id": "sess_total_order",
            "run_id": "run_total_order",
            "last_seq": 2,
            "last_event_id": "run_total_order:2",
        },
    )
    monkeypatch.setattr(routes, "read_run_events", lambda _sid, _run: journal)
    snapshot = routes._run_journal_live_snapshot("run_total_order")
    projected = [
        (row.get("source_event_type"), row.get("seq"))
        for row in snapshot["anchor_activity_scene"]["activity_rows"]
    ]
    assert projected == [
        ("token", 1),
        ("steer_delivered", 2),
    ]


def test_steer_delivery_fsyncs_before_durable_success(tmp_path, monkeypatch):
    writer = run_journal.RunJournalWriter(
        "sess_fsync", "run_fsync", session_dir=tmp_path
    )
    fsync_calls = []
    monkeypatch.setattr(run_journal.os, "fsync", lambda fd: fsync_calls.append(fd))
    monkeypatch.setattr(run_journal, "_fsync_parent_dir", lambda _path: None)

    accepted, event, reason, error = writer.accept_and_append_if_nonterminal(
        "steer_delivered",
        {"text": "persist me"},
        lambda: True,
    )

    assert accepted is True and event is not None and reason is None and error is None
    assert len(fsync_calls) == 1, "durable success requires the steer row fsynced before return"


def test_steer_fsync_failure_reports_accepted_but_not_durable(tmp_path, monkeypatch):
    writer = run_journal.RunJournalWriter(
        "sess_fsync_fail", "run_fsync_fail", session_dir=tmp_path
    )
    monkeypatch.setattr(run_journal.os, "fsync", lambda _fd: (_ for _ in ()).throw(OSError("fsync failed")))
    monkeypatch.setattr(run_journal, "_fsync_parent_dir", lambda _path: None)

    accepted, event, reason, error = writer.accept_and_append_if_nonterminal(
        "steer_delivered",
        {"text": "accepted runtime text"},
        lambda: True,
    )

    assert accepted is True
    assert event is None
    assert reason == "persistence_error"
    assert isinstance(error, OSError)


def test_delete_evicts_seq_cache_so_recreated_run_restarts(tmp_path):
    run_journal.append_run_event(
        "sess_del", "run_del", "token", {"text": "one"}, session_dir=tmp_path
    )
    run_journal.append_run_event(
        "sess_del", "run_del", "token", {"text": "two"}, session_dir=tmp_path
    )

    assert run_journal.delete_run_journal("sess_del", session_dir=tmp_path) is True

    restarted = run_journal.append_run_event(
        "sess_del", "run_del", "token", {"text": "fresh"}, session_dir=tmp_path
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
        run_journal.append_run_event(
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
                run_journal.append_run_event(
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
                run_journal.append_run_event(
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


def test_concurrent_appends_produce_unique_gapless_seqs(tmp_path):
    threads = []
    results: list[int] = []
    results_lock = threading.Lock()

    def worker(i):
        event = run_journal.append_run_event(
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
