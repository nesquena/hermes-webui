"""Regression tests for the run-journal path-resolution cache.

``find_run_file`` and ``find_run_summary`` both located a run by walking
``glob("*/{run_id}.jsonl")`` across every session directory under the journal
root -- on every call, from per-poll call sites (SSE reconnect probes, stream
ownership checks). With hundreds of session directories that walk measured ~7 ms
per call while the resolved path itself is stable for the journal's lifetime.

``_RUN_PATH_CACHE`` remembers ``(session_id, path)`` and revalidates with a single
``stat``. These tests pin the behavioural contract: resolve once, share the
resolution between both entry points, never cache a miss, rediscover after the
file disappears, and drop entries when ``delete_run_journal`` removes a session.
"""
import pathlib

import pytest  # noqa: F401  # top-level import keeps pytest collection unambiguous

from api import run_journal


@pytest.fixture(autouse=True)
def _clear_path_cache():
    """Keep tests independent: the cache is module-global."""
    with run_journal._RUN_PATH_CACHE_LOCK:
        run_journal._RUN_PATH_CACHE.clear()
    yield
    with run_journal._RUN_PATH_CACHE_LOCK:
        run_journal._RUN_PATH_CACHE.clear()


def _seed(session_id, run_id, session_dir):
    run_journal.append_run_event(
        session_id, run_id, "token", {"text": "a"}, session_dir=session_dir
    )


def _count_run_globs(monkeypatch):
    """Count only the ``*/<run>.jsonl`` walks, not unrelated glob calls."""
    calls = {"glob": 0}
    real_glob = pathlib.Path.glob

    def counting_glob(self, pattern, *args, **kwargs):
        if isinstance(pattern, str) and pattern.startswith("*/") and pattern.endswith(".jsonl"):
            calls["glob"] += 1
        return real_glob(self, pattern, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "glob", counting_glob)
    return calls


def test_find_run_file_globs_once(tmp_path, monkeypatch):
    _seed("sess_hit", "run_hit", tmp_path)
    calls = _count_run_globs(monkeypatch)

    for _ in range(10):
        resolved = run_journal.find_run_file("run_hit", session_dir=tmp_path)
        assert resolved is not None
        session_id, path = resolved
        assert session_id == "sess_hit"
        assert path.name == "run_hit.jsonl"

    assert calls["glob"] == 1


def test_find_run_summary_reuses_the_path_resolution(tmp_path, monkeypatch):
    _seed("sess_sum", "run_sum", tmp_path)
    calls = _count_run_globs(monkeypatch)

    for _ in range(10):
        summary = run_journal.find_run_summary("run_sum", session_dir=tmp_path)
        assert summary is not None
        assert summary["run_id"] == "run_sum"

    # Summary caching covers the parse; the path cache covers the directory walk.
    assert calls["glob"] == 1


def test_find_run_file_and_summary_share_one_resolution(tmp_path, monkeypatch):
    _seed("sess_share", "run_share", tmp_path)
    calls = _count_run_globs(monkeypatch)

    assert run_journal.find_run_file("run_share", session_dir=tmp_path) is not None
    assert run_journal.find_run_summary("run_share", session_dir=tmp_path) is not None
    assert run_journal.find_run_file("run_share", session_dir=tmp_path) is not None

    assert calls["glob"] == 1


def test_missing_run_is_not_cached(tmp_path):
    assert run_journal.find_run_file("run_absent", session_dir=tmp_path) is None
    with run_journal._RUN_PATH_CACHE_LOCK:
        assert run_journal._RUN_PATH_CACHE == {}


def test_resolution_rediscovered_after_file_disappears(tmp_path):
    _seed("sess_gone", "run_gone", tmp_path)
    resolved = run_journal.find_run_file("run_gone", session_dir=tmp_path)
    assert resolved is not None

    # Simulate an out-of-band removal (not via delete_run_journal).
    resolved[1].unlink()
    assert run_journal.find_run_file("run_gone", session_dir=tmp_path) is None

    # A run recreated under the same id must be found again.
    _seed("sess_gone", "run_gone", tmp_path)
    again = run_journal.find_run_file("run_gone", session_dir=tmp_path)
    assert again is not None
    assert again[0] == "sess_gone"


def test_delete_run_journal_evicts_path_cache(tmp_path):
    _seed("sess_del", "run_del", tmp_path)
    assert run_journal.find_run_file("run_del", session_dir=tmp_path) is not None
    with run_journal._RUN_PATH_CACHE_LOCK:
        assert run_journal._RUN_PATH_CACHE

    assert run_journal.delete_run_journal("sess_del", session_dir=tmp_path) is True

    with run_journal._RUN_PATH_CACHE_LOCK:
        assert run_journal._RUN_PATH_CACHE == {}
    assert run_journal.find_run_file("run_del", session_dir=tmp_path) is None


def test_resolution_is_scoped_per_journal_root(tmp_path):
    """Two roots may both hold a journal for the same run id."""
    root_a = tmp_path / "a"
    root_b = tmp_path / "b"
    for root in (root_a, root_b):
        root.mkdir()
    _seed("sess_a", "run_dup", root_a)
    _seed("sess_b", "run_dup", root_b)

    resolved_a = run_journal.find_run_file("run_dup", session_dir=root_a)
    resolved_b = run_journal.find_run_file("run_dup", session_dir=root_b)

    assert resolved_a is not None and resolved_b is not None
    assert resolved_a[0] == "sess_a"
    assert resolved_b[0] == "sess_b"
