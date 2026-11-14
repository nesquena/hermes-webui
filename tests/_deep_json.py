"""Build JSON that is too deeply nested for the running interpreter's decoder.

Up to Python 3.13 the C JSON decoder stops at the recursion limit, so a fixed
12,000-deep payload always raised RecursionError. Python 3.14's decoder guards
on the real C stack instead, and parses far deeper (a 12,000-deep list loads
fine; ~200,000 still raises). Tests that need the decoder's RecursionError path
probe the depth at runtime rather than hard-coding one.
"""
from __future__ import annotations

import json
from collections.abc import Callable


def recursion_overflowing_json(
    build: Callable[[int], str], start: int = 12_000, limit: int = 2_000_000
) -> str:
    """Return ``build(depth)`` for the first doubling depth that ``json.loads`` refuses."""
    depth = start
    while depth <= limit:
        payload = build(depth)
        try:
            json.loads(payload)
        except RecursionError:
            return payload
        depth *= 2
    raise AssertionError(f"json.loads never raised RecursionError up to depth {limit}")
