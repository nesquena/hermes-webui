"""The durable ACK retries a briefly locked ledger on every Agent core.

Older cores raise ``sqlite3.OperationalError("database is locked")`` from the
delegation ledger. Newer cores reach the ledger through the storage interface,
which raises its ``StoreContention`` with the SQLite error chained as the cause.
``_is_transient_ledger_error`` must treat both as transient: otherwise a ledger
locked past the core's own wait turns an accepted completion into a requeue
(one turn late, one extra delivery attempt spent).
"""

from __future__ import annotations

import sqlite3

import pytest

from api import process_event_utils as peu


class StoreContention(Exception):
    """Agent-free stand-in carrying the storage interface's class name."""


class _NarrowerContention(StoreContention):
    pass


def _raised(outer, *, cause=None, context=None, suppress=False):
    """*outer* as raised with an explicit cause, an implicit context, or ``from None``."""
    try:
        if context is not None:
            try:
                raise context
            except BaseException:
                if suppress:
                    raise outer from None
                raise outer  # noqa: B904 - an implicit __context__ is the case under test
        if cause is not None:
            raise outer from cause
        raise outer
    except BaseException as exc:
        return exc


def _locked():
    return sqlite3.OperationalError("database is locked")


@pytest.mark.parametrize(
    "build, transient",
    [
        (lambda: _locked(), True),
        (lambda: sqlite3.OperationalError("database table is busy"), True),
        (lambda: sqlite3.OperationalError("attempt to write a readonly database"), False),
        (lambda: sqlite3.DatabaseError("database disk image is malformed"), False),
        (lambda: _raised(StoreContention("database is locked (held too long)"), cause=_locked()), True),
        (lambda: StoreContention("locked"), True),
        (lambda: _NarrowerContention("locked"), True),
        (lambda: _raised(RuntimeError("store failed"), cause=_locked()), True),
        (lambda: _raised(RuntimeError("store failed"), context=_locked()), True),
        (lambda: _raised(RuntimeError("store failed"), context=_locked(), suppress=True), False),
        (lambda: _raised(ValueError("bad"), cause=sqlite3.DatabaseError("malformed")), False),
        (lambda: RuntimeError("database is locked"), False),
    ],
    ids=[
        "sqlite-locked", "sqlite-busy", "sqlite-readonly", "sqlite-malformed",
        "store-contention-chained", "store-contention-bare", "store-contention-subclass",
        "cause-locked", "context-locked", "context-suppressed", "cause-malformed", "message-only",
    ],
)
def test_transient_ledger_errors(build, transient):
    assert peu._is_transient_ledger_error(build()) is transient


def test_cyclic_exception_chain_terminates():
    first, second = RuntimeError("a"), RuntimeError("b")
    first.__cause__, second.__cause__ = second, first
    assert peu._is_transient_ledger_error(first) is False


def _real_translated():
    try:
        from persistence.sqlite._backend import translated
    except ImportError:
        translated = None
    if translated is None:
        pytest.skip("Agent core predates the storage-interface ledger (no StoreContention)")
    return translated


def test_real_store_contention_is_transient_and_retried(monkeypatch):
    """The installed core's own translation of a lock error, through the ACK retry."""
    translated = _real_translated()

    def raise_locked():
        with translated():
            raise _locked()

    with pytest.raises(Exception) as info:
        raise_locked()
    assert any(cls.__name__ == "StoreContention" for cls in type(info.value).__mro__)
    assert peu._is_transient_ledger_error(info.value)

    monkeypatch.setattr(peu, "ASYNC_DELIVERY_ACK_RETRY_DELAYS", (0.0, 0.0))
    calls = []

    def complete(evt, claim_id):
        calls.append(claim_id)
        if len(calls) == 1:
            raise_locked()

    peu._complete_event_delivery_with_retry(
        complete, {"type": "async_delegation", "delegation_id": "deleg_contention"}, "claim-1"
    )
    assert calls == ["claim-1", "claim-1"]
