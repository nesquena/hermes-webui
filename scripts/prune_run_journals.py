#!/usr/bin/env python3
"""One-off: enforce the run-journal retention cap on every existing session journal
dir (and on the journal root), freeing disk immediately without waiting for the
next Nth-append prune to fire. Idempotent; safe to run anytime; uses the
same prune logic the WebUI uses at append time.

Usage::
    HERMES_WEBUI_RUN_JOURNAL_MAX_BYTES=67108864 python3 scripts/prune_run_journals.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _dir_size(p: Path) -> int:
    total = 0
    try:
        for e in os.scandir(p):
            if e.is_file(follow_symlinks=False):
                total += e.stat().st_size
    except OSError:
        pass
    return total


def _fmt(n: int) -> str:
    n = max(n, 0)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f}{unit}"
        n = n / 1024.0
    return f"{n:.0f}TB"


def main() -> int:
    from api.run_journal import (
        RUN_JOURNAL_DIR_NAME,
        _default_session_dir,
        _run_journal_max_bytes,
        prune_run_journal_dir,
    )

    max_bytes = _run_journal_max_bytes()
    if max_bytes <= 0:
        print("pruning disabled (HERMES_WEBUI_RUN_JOURNAL_MAX_BYTES<=0); nothing to do")
        return 0

    root = Path(os.environ.get("HERMES_WEBUI_SESSION_DIR", "")) or _default_session_dir()
    journal_root = root / RUN_JOURNAL_DIR_NAME
    total_start = 0
    n_dirs = 0
    for e in os.scandir(journal_root):
        if e.is_dir(follow_symlinks=False):
            total_start += _dir_size(Path(e.path))
            n_dirs += 1

    n_pruned = 0
    for e in os.scandir(journal_root):
        if e.is_dir(follow_symlinks=False):
            if prune_run_journal_dir(Path(e.path), max_bytes):
                n_pruned += 1

    total_end = 0
    for e in os.scandir(journal_root):
        if e.is_dir(follow_symlinks=False):
            total_end += _dir_size(Path(e.path))

    print(
        f"pruned {n_pruned}/{n_dirs} session journal dirs; "
        f"{_fmt(total_start)} -> {_fmt(total_end)} "
        f"({_fmt(total_start - total_end)} freed); cap={_fmt(max_bytes)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
