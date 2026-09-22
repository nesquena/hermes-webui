import errno
import os
from pathlib import Path

import pytest

from api.worktree_gc_inventory import scan_process_cwds


@pytest.fixture(scope="session", autouse=True)
def test_server():
    """These unit tests do not need the repository's HTTP server fixture."""


def _proc_cwd(proc_root: Path, pid: int, cwd: Path) -> None:
    pid_dir = proc_root / str(pid)
    pid_dir.mkdir(parents=True)
    os.symlink(cwd, pid_dir / "cwd")
    # A live process always has an fd table; an empty readable one here.
    (pid_dir / "fd").mkdir()


def _proc_fd(proc_root: Path, pid: int, target: Path) -> None:
    fd_dir = proc_root / str(pid) / "fd"
    fd_dir.mkdir(parents=True, exist_ok=True)
    os.symlink(target, fd_dir / "3")


def test_process_scan_blocks_exact_and_descendant_cwds_but_not_prefix_lookalike(
    tmp_path,
):
    proc_root = tmp_path / "proc"
    worktree = tmp_path / "worktrees" / "feature"
    child = worktree / "nested"
    lookalike = tmp_path / "worktrees" / "feature-copy"
    child.mkdir(parents=True)
    lookalike.mkdir(parents=True)
    _proc_cwd(proc_root, 101, worktree)
    _proc_cwd(proc_root, 102, child)
    _proc_cwd(proc_root, 103, lookalike)

    scan = scan_process_cwds(proc_root)

    assert scan.available is True
    assert scan.complete is True
    assert scan.process_count == 3
    assert scan.blocking_process_count(worktree) == 2
    assert scan.blocking_process_count(child) == 1
    assert scan.blocking_process_count(lookalike) == 1


def test_process_with_open_fd_inside_worktree_but_foreign_cwd_is_active(tmp_path):
    """A consumer holding an open FD in the worktree is active even elsewhere."""
    proc_root = tmp_path / "proc"
    worktree = tmp_path / "worktrees" / "feature"
    foreign_cwd = tmp_path / "elsewhere"
    open_file = worktree / "notes.txt"
    worktree.mkdir(parents=True)
    foreign_cwd.mkdir()
    open_file.write_text("held open\n", encoding="utf-8")
    _proc_cwd(proc_root, 401, foreign_cwd)
    _proc_fd(proc_root, 401, open_file)

    scan = scan_process_cwds(proc_root)

    assert scan.available is True
    assert scan.complete is True
    assert scan.process_count == 1
    assert scan.blocking_process_count(worktree) == 1
    counts = scan.blocking_process_counts((worktree,))
    assert counts == {str(worktree.resolve()): 1}


def test_process_fds_pointing_outside_or_to_sockets_are_ignored(tmp_path):
    proc_root = tmp_path / "proc"
    worktree = tmp_path / "worktrees" / "feature"
    elsewhere = tmp_path / "elsewhere"
    worktree.mkdir(parents=True)
    elsewhere.mkdir()
    _proc_cwd(proc_root, 501, elsewhere)
    _proc_fd(proc_root, 501, elsewhere / "file.txt")
    (proc_root / "501" / "fd" / "4").symlink_to("socket:[12345]")

    scan = scan_process_cwds(proc_root)

    assert scan.blocking_process_count(worktree) == 0


def test_process_that_disappears_during_scan_is_not_an_error(tmp_path, monkeypatch):
    proc_root = tmp_path / "proc"
    cwd = tmp_path / "worktree"
    cwd.mkdir()
    _proc_cwd(proc_root, 201, cwd)
    (proc_root / "202").mkdir()
    real_readlink = os.readlink

    def disappearing_readlink(path):
        if Path(path).parent.name == "202":
            raise FileNotFoundError(errno.ENOENT, "gone", str(path))
        return real_readlink(path)

    monkeypatch.setattr(os, "readlink", disappearing_readlink)

    scan = scan_process_cwds(proc_root)

    assert scan.available is True
    assert scan.complete is True
    assert scan.process_count == 1
    assert scan.unreadable_count == 0


def test_globally_inaccessible_proc_scan_is_uncertain(tmp_path):
    proc_root = tmp_path / "not-a-directory"
    proc_root.write_text("not proc", encoding="utf-8")

    scan = scan_process_cwds(proc_root)

    assert scan.available is False
    assert scan.complete is False
    assert scan.process_count == 0
    assert scan.blocking_process_count(tmp_path) == 0


def test_unreadable_pid_cwd_makes_scan_incomplete(tmp_path, monkeypatch):
    proc_root = tmp_path / "proc"
    (proc_root / "301").mkdir(parents=True)

    def denied_readlink(path):
        raise PermissionError(errno.EACCES, "denied", str(path))

    monkeypatch.setattr(os, "readlink", denied_readlink)

    scan = scan_process_cwds(proc_root)

    assert scan.available is True
    assert scan.complete is False
    assert scan.unreadable_count == 1


def test_unreadable_fd_table_makes_scan_incomplete_but_keeps_cwd_evidence(
    tmp_path,
    monkeypatch,
):
    """Fail closed: an unreadable fd table is not proof of no open files."""
    proc_root = tmp_path / "proc"
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    _proc_cwd(proc_root, 601, worktree)
    real_scandir = os.scandir

    def denied_scandir(path):
        if Path(path).name == "fd":
            raise PermissionError(errno.EACCES, "denied", str(path))
        return real_scandir(path)

    monkeypatch.setattr(os, "scandir", denied_scandir)

    scan = scan_process_cwds(proc_root)

    assert scan.available is True
    assert scan.complete is False
    assert scan.unreadable_count == 1
    # cwd evidence is retained: the process still blocks the worktree.
    assert scan.blocking_process_count(worktree) == 1
