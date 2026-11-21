"""Simulate a native Windows interpreter: ``import fcntl`` raises ImportError.

#7862 round 21 (P1, "Windows tests cannot load"): the round-14 and round-15
files used a module-scope ``import fcntl``. On native Windows that raises
``ModuleNotFoundError`` during COLLECTION, which stops the whole suite
before any test can be skipped.

This plugin blocks the real ``fcntl`` module so the collection behaviour can
be exercised on a POSIX host. It answers the same question the Windows
runner does: does the suite still COLLECT, and are only the genuinely
POSIX-dependent cases skipped?

Usage:
    pytest -p no_cacheprovider -p win_fcntl_sim tests/test_issue7862_round14_findings.py ...
"""

from __future__ import annotations

import sys


class _BlockFcntl:
    def find_module(self, fullname, path=None):
        return self if fullname == "fcntl" else None

    def find_spec(self, fullname, path=None, target=None):
        if fullname != "fcntl":
            return None
        raise ImportError("No module named 'fcntl' (simulated Windows)")


def pytest_configure(config):
    for name in list(sys.modules):
        if name == "fcntl":
            del sys.modules[name]
    sys.meta_path.insert(0, _BlockFcntl())
