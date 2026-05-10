"""Test that the SSE subscribe + snapshot are taken atomically under _lock.

Regression test for the snapshot/subscribe race condition: if subscribe
happens AFTER the snapshot, a submit_pending() that fires in the gap is
both appended to _pending (after our snapshot) AND notified to subscribers
(before we joined) — the client never learns about it until the next event.

The fix in v0.50.248 takes the lock once, registers the subscriber queue,
THEN reads the snapshot — all under the same lock acquisition.

This test verifies the source-level invariant rather than the runtime
behavior: the subscriber-registration line MUST appear inside the same
`with _lock:` block as the snapshot read, and BEFORE the snapshot read.
"""

import pathlib
import sys

REPO_ROOT = pathlib.Path(__file__).parent.parent.resolve()
sys.path.insert(0, str(REPO_ROOT))

ROUTES_SRC = (REPO_ROOT / "api" / "routes.py").read_text(encoding="utf-8")


def _extract_lock_block(body: str) -> str:
    """Extract the body of the first `with _lock:` block from the handler.

    Lines are part of the block as long as they are blank or start with the
    block's indent (>= 8 spaces, since the handler body itself is at 4 spaces
    and the lock block indents one level deeper).
    """
    lines = body.split("\n")
    out: list[str] = []
    in_block = False
    block_indent = None
    for line in lines:
        if not in_block:
            if line.strip() == "with _lock:":
                in_block = True
            continue
        # Determine block indent from the first non-empty line we see inside.
        if block_indent is None:
            stripped = line.lstrip(" ")
            if stripped == "":
                continue  # blank lines don't set indent
            block_indent = len(line) - len(stripped)
            out.append(line)
            continue
        # Continuation: blank lines OK, otherwise must be at >= block_indent.
        if line.strip() == "":
            out.append(line)
            continue
        line_indent = len(line) - len(line.lstrip(" "))
        if line_indent >= block_indent:
            out.append(line)
        else:
            break
    return "\n".join(out)


def _handler_body() -> str:
    start = ROUTES_SRC.find("def _handle_approval_sse_stream(")
    assert start != -1, "_handle_approval_sse_stream must exist"
    end = ROUTES_SRC.find("\ndef ", start + 1)
    return ROUTES_SRC[start : end if end != -1 else len(ROUTES_SRC)]


def test_snapshot_taken_under_lock():
    """The initial _pending snapshot must be guarded by `with _lock:`."""
    lock_body = _extract_lock_block(_handler_body())
    assert lock_body, "_handle_approval_sse_stream must contain a `with _lock:` block"
    assert "_pending.get(sid)" in lock_body, (
        "Initial snapshot of _pending must be read inside the `with _lock:` block"
    )


def test_subscriber_registered_inside_lock():
    """The subscriber queue must be registered inside the same `with _lock:` block."""
    lock_body = _extract_lock_block(_handler_body())
    assert lock_body, "Handler must contain a `with _lock:` block"
    assert "_approval_sse_subscribers" in lock_body and "append(q)" in lock_body, (
        "Subscriber registration (`_approval_sse_subscribers.setdefault(sid, []).append(q)`) "
        "must happen inside the same `with _lock:` block as the snapshot. "
        "Otherwise a submit_pending() between snapshot-and-subscribe is lost."
    )


def test_subscribe_before_snapshot_in_lock():
    """Inside the lock, the subscriber must be registered BEFORE reading the snapshot."""
    lock_body = _extract_lock_block(_handler_body())
    assert lock_body, "Handler must contain a `with _lock:` block"

    sub_idx = lock_body.find("_approval_sse_subscribers")
    snap_idx = lock_body.find("_pending.get(sid)")

    assert sub_idx != -1, "Subscriber registration must be inside the lock"
    assert snap_idx != -1, "Snapshot read must be inside the lock"
    assert sub_idx < snap_idx, (
        "Subscriber registration must come BEFORE the snapshot read inside the lock. "
        "Otherwise an approval arriving between subscribe and snapshot is silently dropped."
    )


def test_no_double_subscribe_outside_lock():
    """The handler must not also call `_approval_sse_subscribe()` (legacy code path)."""
    body = _handler_body()
    assert "= _approval_sse_subscribe(sid)" not in body, (
        "_handle_approval_sse_stream must not call _approval_sse_subscribe() — "
        "the atomic version inlines subscribe inside the snapshot lock block."
    )
