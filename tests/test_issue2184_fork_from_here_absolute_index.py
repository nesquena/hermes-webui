"""Regression test for issue #2184 — Fork from here absolute-index fix.

When a long session is loaded with ``msg_limit=30`` (tail window), the
``Fork from here`` button passes ``rawIdx+1`` as ``keep_count`` to
``/api/session/branch``.  ``rawIdx`` is the 0-based index within the
loaded window, but the backend expects an absolute message count from
the beginning of the full transcript.  In a truncated session where
``_oldestIdx > 0``, the local index alone is wrong.

The fix:

1. ``forkFromMessage`` captures ``absoluteKeepCount = _oldestIdx + msgIdx``
   *before* any async work (``_ensureAllMessagesLoaded`` resets
   ``_oldestIdx`` to 0 after its wholesale replace).
2. It calls ``_ensureAllMessagesLoaded()`` so the full transcript is
   available for the forked session.
3. It sends ``keep_count: absoluteKeepCount`` instead of the raw
   ``msgIdx``.

When the full transcript is already loaded (short sessions or sessions
where all older messages have been scrolled in), ``_oldestIdx`` is 0
and ``absoluteKeepCount`` equals ``msgIdx``, preserving existing
behaviour.
"""

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
COMMANDS_JS = (REPO / "static" / "commands.js").read_text(encoding="utf-8")
SESSIONS_JS = (REPO / "static" / "sessions.js").read_text(encoding="utf-8")


def _function_body(src: str, name: str) -> str:
    """Slice the body of ``async function <name>`` (or ``function <name>``)."""
    needle_async = f"async function {name}"
    needle_sync = f"function {name}"
    if needle_async in src:
        start = src.index(needle_async)
    else:
        start = src.index(needle_sync)
    brace = src.index("{", start)
    depth = 0
    for i in range(brace, len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[start : i + 1]
    raise AssertionError(f"function {name!r} body not found")


# ---------------------------------------------------------------------------
# forkFromMessage computes absolute keep_count using _oldestIdx
# ---------------------------------------------------------------------------

def test_fork_uses_absolute_keep_count():
    """``forkFromMessage`` must add ``_oldestIdx`` to ``msgIdx`` before sending."""
    body = _function_body(COMMANDS_JS, "forkFromMessage")
    # The function must compute an absolute count that incorporates _oldestIdx.
    assert "_oldestIdx" in body, (
        "forkFromMessage must reference _oldestIdx to compute the absolute "
        "keep_count for truncated sessions. See #2184."
    )
    # The absolute count expression must combine _oldestIdx + msgIdx.
    assert re.search(r"_oldestIdx\s*\+\s*msgIdx", body), (
        "forkFromMessage must compute absoluteKeepCount as _oldestIdx + msgIdx. "
        "See #2184."
    )


def test_fork_captures_absolute_count_before_await():
    """The absolute keep_count must be captured BEFORE any ``await`` call."""
    body = _function_body(COMMANDS_JS, "forkFromMessage")
    capture_match = re.search(r"absoluteKeepCount\s*=\s*_oldestIdx\s*\+\s*msgIdx", body)
    assert capture_match, "Missing absoluteKeepCount = _oldestIdx + msgIdx assignment"
    capture_idx = capture_match.start()
    # Find the first await in the function body.
    await_match = re.search(r"\bawait\b", body)
    assert await_match, "forkFromMessage should contain at least one await"
    await_idx = await_match.start()
    assert capture_idx < await_idx, (
        "forkFromMessage must capture absoluteKeepCount BEFORE the first "
        "await, because _ensureAllMessagesLoaded resets _oldestIdx to 0 "
        "after its wholesale replace. See #2184."
    )


def test_fork_sends_absolute_keep_count_not_raw_msgIdx():
    """The request body must use ``absoluteKeepCount``, not ``msgIdx``."""
    body = _function_body(COMMANDS_JS, "forkFromMessage")
    assert "keep_count:absoluteKeepCount" in body, (
        "forkFromMessage must send keep_count:absoluteKeepCount (not "
        "keep_count:msgIdx) so the backend receives the absolute message "
        "count. See #2184."
    )
    # Ensure the old raw form is gone.
    assert "keep_count:msgIdx" not in body, (
        "forkFromMessage must NOT send keep_count:msgIdx — that is the "
        "pre-fix bug where a tail-window local index was sent as an "
        "absolute count. See #2184."
    )


def test_fork_calls_ensure_all_messages_loaded():
    """``forkFromMessage`` must call ``_ensureAllMessagesLoaded`` for truncated sessions."""
    body = _function_body(COMMANDS_JS, "forkFromMessage")
    assert "_ensureAllMessagesLoaded" in body, (
        "forkFromMessage must call _ensureAllMessagesLoaded so the full "
        "transcript is loaded before the fork is created. See #2184."
    )


def test_fork_ensure_all_called_before_branch_api():
    """``_ensureAllMessagesLoaded`` must be called BEFORE the ``/api/session/branch`` request."""
    body = _function_body(COMMANDS_JS, "forkFromMessage")
    ensure_idx = body.index("_ensureAllMessagesLoaded")
    branch_idx = body.index("'/api/session/branch'")
    assert ensure_idx < branch_idx, (
        "forkFromMessage must call _ensureAllMessagesLoaded BEFORE sending "
        "the branch API request, so the full transcript is available. "
        "See #2184."
    )


# ---------------------------------------------------------------------------
# _oldestIdx and _messagesTruncated are declared at module scope
# ---------------------------------------------------------------------------

def test_oldest_idx_declared_at_module_scope():
    """``_oldestIdx`` must be a module-scoped variable accessible to forkFromMessage."""
    assert "let _oldestIdx = 0;" in SESSIONS_JS, (
        "static/sessions.js must declare `let _oldestIdx = 0;` at module "
        "scope so forkFromMessage can read it for absolute-index "
        "computation. See #2184."
    )


def test_messages_truncated_declared_at_module_scope():
    """``_messagesTruncated`` must be a module-scoped variable."""
    assert "let _messagesTruncated = false;" in SESSIONS_JS, (
        "static/sessions.js must declare `let _messagesTruncated = false;` "
        "at module scope. See #2184."
    )


# ---------------------------------------------------------------------------
# _ensureAllMessagesLoaded resets _oldestIdx (existing #1937 guard)
# ---------------------------------------------------------------------------

def test_ensure_all_resets_oldest_idx_to_zero():
    """After loading all messages, ``_oldestIdx`` must be 0 — this is why we capture early."""
    body = _function_body(SESSIONS_JS, "_ensureAllMessagesLoaded")
    assert "_oldestIdx = 0;" in body, (
        "_ensureAllMessagesLoaded must reset _oldestIdx to 0 after the "
        "wholesale replace. This is why forkFromMessage must capture the "
        "absolute count BEFORE awaiting _ensureAllMessagesLoaded. "
        "See #2184 and #1937."
    )


# ---------------------------------------------------------------------------
# Short-session / full-transcript behaviour preserved
# ---------------------------------------------------------------------------

def test_fork_absolute_count_reduces_to_msgIdx_when_oldestIdx_zero():
    """When ``_oldestIdx`` is 0, ``absoluteKeepCount`` equals ``msgIdx`` (no behaviour change)."""
    body = _function_body(COMMANDS_JS, "forkFromMessage")
    # The expression _oldestIdx + msgIdx evaluates to msgIdx when _oldestIdx==0.
    # Verify the expression exists (already checked above) and that there
    # is no conditional that would skip the computation for non-truncated sessions.
    assert re.search(r"absoluteKeepCount\s*=\s*_oldestIdx\s*\+\s*msgIdx", body), (
        "The absolute count must always be computed as _oldestIdx + msgIdx. "
        "When _oldestIdx is 0 (full transcript loaded), this equals msgIdx, "
        "preserving short-session behaviour. See #2184."
    )
    # There should NOT be a conditional that only computes the offset for
    # truncated sessions — that would be fragile if the condition and the
    # _oldestIdx read got out of sync.
    assert "if(" not in body.split("absoluteKeepCount")[0].split("\n")[-1], (
        "The absoluteKeepCount computation must not be inside a conditional "
        "that gates on _messagesTruncated — always computing _oldestIdx + "
        "msgIdx is simpler and correct for both truncated and non-truncated "
        "sessions. See #2184."
    )