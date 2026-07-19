"""The streaming session-list caches must outlive one streaming poll interval.

While a turn is actively streaming, the frontend re-polls ``/api/sessions`` on a
fixed cadence (``static/sessions.js`` ``_streamingPollMs``). Both streaming-cache
hold-downs — the route-level ``_SESSIONS_CACHE_STREAMING_TTL_SECONDS`` (#4808) and
the CLI/cron ``_CLI_SESSIONS_CACHE_STREAMING_TTL_SECONDS`` (#4842) — exist so that
those repeated polls read a held entry instead of forcing a full, LOCK-contending
rebuild on the streaming hot path (#4672).

That only holds if each streaming TTL is strictly longer than the poll interval.
If a TTL is <= the poll interval, the entry expires between polls, every poll can
find it stale, and the hold-down collapses back into per-poll rebuilds — the exact
regression these constants were added to prevent.

This test derives the poll interval from the single source of truth (the JS
constant) so the invariant can never silently drift when either side is retuned.
"""

import re
from pathlib import Path

import pytest

from api import models
from api import route_session_list_cache

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SESSIONS_JS = _REPO_ROOT / "static" / "sessions.js"


def _streaming_poll_seconds() -> float:
    """Parse the real ``const _streamingPollMs = <int>;`` from static/sessions.js.

    Kept as the single source of the poll magic number so the test never encodes
    a second, drift-prone copy of it.
    """
    source = _SESSIONS_JS.read_text(encoding="utf-8")
    match = re.search(r"\bconst\s+_streamingPollMs\s*=\s*(\d+)\s*;", source)
    assert match, "could not find `const _streamingPollMs = <int>;` in static/sessions.js"
    poll_ms = int(match.group(1))
    assert poll_ms > 0, f"_streamingPollMs must be a positive integer, got {poll_ms!r}"
    return poll_ms / 1000.0


@pytest.mark.parametrize(
    "ttl_seconds, label",
    [
        (
            route_session_list_cache._SESSIONS_CACHE_STREAMING_TTL_SECONDS,
            "route_session_list_cache._SESSIONS_CACHE_STREAMING_TTL_SECONDS",
        ),
        (
            models._CLI_SESSIONS_CACHE_STREAMING_TTL_SECONDS,
            "models._CLI_SESSIONS_CACHE_STREAMING_TTL_SECONDS",
        ),
    ],
)
def test_streaming_ttl_strictly_exceeds_poll_interval(ttl_seconds, label):
    poll_seconds = _streaming_poll_seconds()
    assert ttl_seconds > poll_seconds, (
        f"{label} ({ttl_seconds}s) must be strictly greater than the streaming "
        f"poll interval ({poll_seconds}s from static/sessions.js `_streamingPollMs`), "
        "otherwise the streaming hold-down expires between polls and every poll "
        "forces a full rebuild on the streaming hot path (#4672/#4808/#4842)."
    )
