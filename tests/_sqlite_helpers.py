"""Shared SQLite helpers for tests that pin best-effort self-heal branches.

Several call sites (the sidebar projection's index primes) open a read-only
``mode=ro`` URI connection for the actual read and a separate, short-lived
plain writable connection for a best-effort ``CREATE INDEX IF NOT EXISTS``.
That write is wrapped in ``except sqlite3.Error: pass`` so a read-only, locked,
or older-schema db still serves the listing without the perf benefit.

Testing that branch by making the *file* read-only does not work: root ignores
POSIX mode bits, so ``chmod(0o444)`` lets the prime succeed and the test
validates the writable path while claiming to cover the failure path. It
passes either way and can never catch a regression. The repo previously
skipped such a test under root, which traded a false pass for no coverage.

``writes_blocked()`` expresses the intent directly and deterministically for
every user, root included.
"""

import contextlib
import sqlite3


@contextlib.contextmanager
def writes_blocked():
    """Fail every writable SQLite connect; let read-only URI opens through.

    Swaps ``sqlite3.connect`` for the duration of the block so that:

    * ``uri=True`` opens (the ``mode=ro`` read handle) delegate to the real
      connector and behave normally, and
    * plain writable opens raise ``sqlite3.OperationalError``, exactly as a
      genuinely read-only or locked database does.

    This isolates the best-effort index prime as the only thing that fails,
    independent of file permissions or the running user's privileges.
    """
    real_connect = sqlite3.connect

    def guarded(target, *args, **kwargs):
        if kwargs.get("uri"):
            return real_connect(target, *args, **kwargs)
        raise sqlite3.OperationalError("attempt to write a readonly database")

    sqlite3.connect = guarded
    try:
        yield
    finally:
        sqlite3.connect = real_connect
