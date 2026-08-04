"""Real-process exclusion tests for stable lineage turn permits."""
from __future__ import annotations

import importlib
import multiprocessing
import os
from pathlib import Path

import pytest


def _lineage_module():
    try:
        return importlib.import_module("api.session_lineage")
    except ModuleNotFoundError:
        pytest.fail(
            "lineage permit unavailable: cross-process same-root exclusion "
            "is not implemented"
        )


def _try_permit_in_child(root_session_id: str, lock_dir: str, result_queue) -> None:
    lineage = importlib.import_module("api.session_lineage")
    try:
        permit = lineage.acquire_lineage_turn_permit(
            root_session_id,
            lock_dir=Path(lock_dir),
            backend="fcntl",
        )
    except lineage.LineageTurnBusyError:
        result_queue.put("busy")
        return
    except Exception as exc:  # pragma: no cover - diagnostic path for child failures
        result_queue.put(f"error:{type(exc).__name__}:{exc}")
        return
    try:
        result_queue.put("acquired")
    finally:
        permit.release()


def _child_permit_result(root_session_id: str, lock_dir: Path) -> str:
    ctx = multiprocessing.get_context("spawn")
    result_queue = ctx.Queue()
    process = ctx.Process(
        target=_try_permit_in_child,
        args=(root_session_id, str(lock_dir), result_queue),
    )
    process.start()
    process.join(timeout=10)
    assert process.exitcode == 0
    return result_queue.get(timeout=2)


def test_same_root_is_excluded_across_real_processes(tmp_path):
    lineage = _lineage_module()
    lock_dir = tmp_path / "permits"
    permit = lineage.acquire_lineage_turn_permit(
        "sharedroot", lock_dir=lock_dir, backend="fcntl"
    )
    try:
        assert _child_permit_result("sharedroot", lock_dir) == "busy"
    finally:
        permit.release()

    lock_files = list(lock_dir.glob("*.lock"))
    assert len(lock_files) == 1
    assert lock_files[0].is_file()


def test_different_roots_are_independent_across_real_processes(tmp_path):
    lineage = _lineage_module()
    lock_dir = tmp_path / "permits"
    permit = lineage.acquire_lineage_turn_permit(
        "roota", lock_dir=lock_dir, backend="fcntl"
    )
    try:
        assert _child_permit_result("rootb", lock_dir) == "acquired"
    finally:
        permit.release()


class _FakeMsvcrt:
    LK_NBLCK = 1
    LK_UNLCK = 2

    def __init__(self):
        self.held: set[tuple[int, int]] = set()

    @staticmethod
    def _key(fd: int) -> tuple[int, int]:
        stat_result = os.fstat(fd)
        return stat_result.st_dev, stat_result.st_ino

    def locking(self, fd: int, mode: int, count: int) -> None:
        assert count == 1
        key = self._key(fd)
        if mode == self.LK_NBLCK:
            if key in self.held:
                raise OSError(13, "permission denied")
            self.held.add(key)
            return
        assert mode == self.LK_UNLCK
        self.held.discard(key)


def test_windows_backend_is_nonblocking_and_never_unlinks(tmp_path, monkeypatch):
    lineage = _lineage_module()
    fake_msvcrt = _FakeMsvcrt()
    monkeypatch.setattr(lineage, "_load_msvcrt", lambda: fake_msvcrt)
    lock_dir = tmp_path / "windows-permits"

    first = lineage.acquire_lineage_turn_permit(
        "windowsroot", lock_dir=lock_dir, backend="msvcrt"
    )
    with pytest.raises(lineage.LineageTurnBusyError):
        lineage.acquire_lineage_turn_permit(
            "windowsroot", lock_dir=lock_dir, backend="msvcrt"
        )
    lock_path = first.path
    first.release()

    assert lock_path.exists()
    again = lineage.acquire_lineage_turn_permit(
        "windowsroot", lock_dir=lock_dir, backend="msvcrt"
    )
    again.release()
    assert lock_path.exists()


def test_unsupported_backend_refuses_before_filesystem_mutation(tmp_path):
    lineage = _lineage_module()
    lock_dir = tmp_path / "unsupported"

    with pytest.raises(lineage.LineagePermitUnsupportedError):
        lineage.acquire_lineage_turn_permit(
            "unsupportedroot",
            lock_dir=lock_dir,
            backend="unsupported",
        )

    assert not lock_dir.exists()
