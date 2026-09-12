"""Bounded, process-local freshness for mutable static-tree content identity.

The owner is one instance shared by the shell and worker token helper. This
cache does not authorize immutable responses or replace per-response byte ETags.
Filesystem work is supplied by the existing strict inventory function and runs
outside the coordination lock. There are no background threads or exporters.
"""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
import math
from pathlib import Path
import threading
import time
from typing import Callable


@dataclass
class _Flight:
    key: str
    done: bool = False
    value: str | None = None


class AssetIdentityCache:
    """Share success and failure outcomes with one global in-flight scan.

    Limits are per instance/process, not per root or per HTTP route. A slow
    scanner cannot spawn replacement scanners: excess or timed-out followers
    receive None (identity unavailable), never a stale successful identity.
    """

    def __init__(
        self,
        *,
        freshness_seconds: float = 1.0,
        failure_seconds: float = 0.25,
        max_entries: int = 4,
        max_waiters: int = 32,
        wait_seconds: float = 2.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        for value in (freshness_seconds, failure_seconds, wait_seconds):
            if not math.isfinite(value) or value <= 0:
                raise ValueError("cache durations must be finite and positive")
        if not isinstance(max_entries, int) or max_entries < 1:
            raise ValueError("max_entries must be a positive integer")
        if not isinstance(max_waiters, int) or max_waiters < 1:
            raise ValueError("max_waiters must be a positive integer")
        self._freshness_seconds = freshness_seconds
        self._failure_seconds = failure_seconds
        self._max_entries = max_entries
        self._max_waiters = max_waiters
        self._wait_seconds = wait_seconds
        self._clock = clock
        self._condition = threading.Condition(threading.Lock())
        self._entries: OrderedDict[str, tuple[str | None, float]] = OrderedDict()
        self._flight: _Flight | None = None
        self._waiters = 0
        self._stats: dict[str, int | float] = {
            "requests": 0,
            "cache_hits": 0,
            "negative_hits": 0,
            "refreshes": 0,
            "refresh_successes": 0,
            "refresh_failures": 0,
            "shared_results": 0,
            "wait_timeouts": 0,
            "wait_rejections": 0,
            "peak_waiters": 0,
            "evictions": 0,
            "last_refresh_seconds": 0.0,
            "total_refresh_seconds": 0.0,
            "max_refresh_seconds": 0.0,
        }

    def get(self, static_root: Path, compute: Callable[[Path], str]) -> str | None:
        # Lexical absolute identity only: resolve()/stat() here would let warm
        # requests repeat filesystem work before reaching the shared flight.
        # Resolve symlinks inside compute, where the original inventory does it.
        key = str(Path(static_root).absolute())
        # Waiting has its own real monotonic deadline, independent of a test's
        # injected freshness clock, and spans waits for *different* root flights.
        deadline = time.monotonic() + self._wait_seconds
        with self._condition:
            self._stats["requests"] += 1
            while True:
                cached = self._entries.get(key)
                if cached is not None and self._clock() < cached[1]:
                    self._entries.move_to_end(key)
                    self._stats["cache_hits"] += 1
                    if cached[0] is None:
                        self._stats["negative_hits"] += 1
                    return cached[0]
                flight = self._flight
                if flight is None:
                    flight = _Flight(key)
                    self._flight = flight
                    self._stats["refreshes"] += 1
                    break
                if self._waiters >= self._max_waiters:
                    self._stats["wait_rejections"] += 1
                    return None
                self._waiters += 1
                self._stats["peak_waiters"] = max(
                    self._stats["peak_waiters"], self._waiters
                )
                try:
                    completed = self._condition.wait_for(
                        lambda flight=flight: flight.done,
                        timeout=max(0.0, deadline - time.monotonic()),
                    )
                finally:
                    self._waiters -= 1
                if not completed:
                    self._stats["wait_timeouts"] += 1
                    return None
                if flight.key == key:
                    # Consume this generation's result even if a later flight
                    # has already begun. Never turn awakened followers into a
                    # second burst of scans after a slow refresh or failure.
                    self._stats["shared_results"] += 1
                    return flight.value
                # A changed server-selected root may wait behind another root,
                # but must never consume the other root's content identity.

        value = None
        started = time.monotonic()
        try:
            try:
                candidate = compute(Path(key))
                if isinstance(candidate, str) and candidate:
                    value = candidate
            except Exception:
                # Do not retain exception objects, paths or tracebacks. Unknown
                # identity is a short-lived result, not a successful fallback.
                pass
            return value
        finally:
            # Also release/notify after BaseException (which still propagates
            # to the leader). The failed generation is briefly shared as None.
            elapsed = time.monotonic() - started
            with self._condition:
                try:
                    lifetime = (
                        self._freshness_seconds if value is not None
                        else self._failure_seconds
                    )
                    self._entries[key] = (value, self._clock() + lifetime)
                    self._entries.move_to_end(key)
                    while len(self._entries) > self._max_entries:
                        self._entries.popitem(last=False)
                        self._stats["evictions"] += 1
                    self._stats[
                        "refresh_successes" if value is not None else "refresh_failures"
                    ] += 1
                    self._stats["last_refresh_seconds"] = elapsed
                    self._stats["total_refresh_seconds"] += elapsed
                    self._stats["max_refresh_seconds"] = max(
                        self._stats["max_refresh_seconds"], elapsed
                    )
                    flight.value = value
                except BaseException:
                    self._entries.pop(key, None)
                    raise
                finally:
                    flight.done = True
                    self._flight = None
                    self._condition.notify_all()

    def snapshot(self) -> dict[str, int | float]:
        """Return fixed-cardinality diagnostics, with no keys or user data.

        This is an in-process accessor, not a public HTTP metrics endpoint.
        Taking a snapshot must remain possible while filesystem I/O is blocked.
        """
        with self._condition:
            return {
                **self._stats,
                "current_waiters": self._waiters,
                "in_flight": int(self._flight is not None),
                "entries": len(self._entries),
            }


# One owner for all shell/worker callers, including server-selected root changes.
ASSET_IDENTITY_CACHE = AssetIdentityCache()
