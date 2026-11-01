"""Regression: edit/regenerate use absolute keep_count (#2184 pattern)."""

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
UI_JS = (REPO / "static" / "ui.js").read_text(encoding="utf-8")


def _function_body(src: str, name: str) -> str:
    needle_async = f"async function {name}"
    start = src.index(needle_async)
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


def test_submit_edit_uses_absolute_keep_count():
    body = _function_body(UI_JS, "submitEdit")
    assert re.search(r"absoluteKeepCount\s*=\s*_oldestIdx\s*\+\s*msgIdx", body)
    assert "keep_count: absoluteKeepCount" in body


def test_regenerate_delegates_to_atomic_start_without_client_truncation():
    body = _function_body(UI_JS, "regenerateResponse")
    assert "await startRegeneration(initialSid, S.session.regeneration_revision)" in body
    assert "/api/session/truncate" not in body
    assert "await send()" not in body


def test_submit_edit_captures_absolute_before_await():
    body = _function_body(UI_JS, "submitEdit")
    cap = re.search(r"absoluteKeepCount\s*=\s*_oldestIdx\s*\+\s*msgIdx", body)
    assert cap
    first_await = re.search(r"\bawait\b", body)
    assert first_await and cap.start() < first_await.start()


def test_submit_edit_is_guarded_against_reentry():
    """A second submitEdit while the first is in flight must be refused.

    submitEdit is destructive (it POSTs /api/session/truncate) and its only guard
    was S.busy, which send() does not set until the last line — after
    _ensureAllMessagesLoaded() and the truncate round-trip, both of which take
    seconds on a long session. Observed in the wild: one user clicking "Send edit"
    repeatedly because the UI had not acknowledged the first click produced SEVEN
    concurrent truncate POSTs (5.2-8.5s each).

    The hazard is not just load. absoluteKeepCount is deliberately captured before
    the awaits (test_submit_edit_captures_absolute_before_await) because at click
    time _oldestIdx is the window offset and msgIdx is window-relative. But the
    first call's _ensureAllMessagesLoaded() sets _oldestIdx = 0, so a second call
    entering after that computes `0 + msgIdx` from a still-window-relative msgIdx —
    a much smaller keep_count that would truncate away most of the transcript.
    """
    body = _function_body(UI_JS, "submitEdit")

    assert "let _submitEditInFlight = false;" in UI_JS, (
        "submitEdit needs a module-scope in-flight flag; a local cannot survive "
        "across invocations"
    )
    assert "if(!S.session || S.busy || _submitEditInFlight) return;" in body, (
        "submitEdit must refuse re-entry while a previous call is still in flight — "
        "S.busy alone is set too late (only by send(), on the last line)"
    )

    # The flag must be raised before the first await, or the window it closes is
    # exactly the window that was open before.
    raise_pos = body.index("_submitEditInFlight = true;")
    first_await = body.index("await ")
    assert raise_pos < first_await

    # ...and cleared in a finally, so an early return or a throw cannot wedge
    # editing off permanently for the rest of the page's life.
    assert "} finally {" in body
    finally_pos = body.index("} finally {")
    clear_pos = body.index("_submitEditInFlight = false;", finally_pos)
    assert clear_pos > finally_pos
    assert clear_pos > body.index("await send();"), (
        "the flag must be cleared after the send completes, not partway through"
    )

    # Guard at the function, not the click handler: the edit is also submittable
    # via Enter (the keydown handler clicks the button), so a handler-only guard
    # would leave that path open.
    assert "await submitEdit(msgIdx, newText);" in UI_JS
