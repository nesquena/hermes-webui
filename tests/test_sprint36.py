"""
Sprint 36 Tests: cancelStream cleanup no longer depends on SSE event (PR #309 / issue #299).

The old cancelStream() set "Cancelling..." status and then relied on the SSE cancel
event to clear it. If the SSE connection was already closed, the event never arrived
and "Cancelling..." lingered indefinitely.

The fix: cancelStream() now clears status, busy state, activeStreamId, and the cancel
button directly after the cancel API request completes — regardless of whether the SSE
cancel event fires. The SSE handler still runs if it arrives (all operations idempotent).

Covers:
  1. cancelStream() clears activeStreamId unconditionally after the fetch
  2. cancelStream() calls setBusy(false) unconditionally
  3. cancelStream() calls setStatus('') unconditionally
  4. cancelStream() hides the cancel button unconditionally
  5. The catch block no longer calls setStatus(cancel_failed) — cleanup runs even on error
  6. The SSE cancel handler is still present (idempotent path)
  7. cancel_failed i18n key is still defined in all locales (key exists, just not used in
     the catch-path anymore — kept for potential future use)
"""

import pathlib
import re

REPO = pathlib.Path(__file__).parent.parent


def read(path):
    return (REPO / path).read_text(encoding="utf-8")


def _locale_count(src: str) -> int:
    pattern = re.compile(
        r"^\s{2}(?:'(?P<quoted>[A-Za-z0-9-]+)'|(?P<plain>[A-Za-z0-9-]+))\s*:\s*\{",
        re.MULTILINE,
    )
    return sum(1 for _ in pattern.finditer(src))


# ── 1–4. cancelStream() cleanup is unconditional ─────────────────────────────

class TestCancelStreamCleanup:
    """cancelStream() must clear all busy state regardless of SSE connection state."""

    def _get_cancel_block(self):
        """Extract the cancelStream function body from boot.js."""
        src = read("static/boot.js")
        idx = src.find("async function cancelStream()")
        assert idx != -1, "cancelStream not found in boot.js"
        # Find the closing brace — scan for the matching }
        depth = 0
        end = idx
        for i, ch in enumerate(src[idx:]):
            if ch == '{':
                depth += 1
            elif ch == '}':
                depth -= 1
                if depth == 0:
                    end = idx + i + 1
                    break
        return src[idx:end]

    def test_clears_active_stream_id(self):
        """cancelStream() must null out S.activeStreamId after the request."""
        block = self._get_cancel_block()
        assert "S.activeStreamId=null" in block or "S.activeStreamId = null" in block, (
            "cancelStream() does not clear S.activeStreamId — "
            "subsequent calls could re-cancel an already-finished stream"
        )

    def test_calls_set_busy_false(self):
        """cancelStream() must call setBusy(false) directly."""
        block = self._get_cancel_block()
        assert "setBusy(false)" in block, (
            "cancelStream() does not call setBusy(false) — "
            "spinner may linger if SSE connection is already closed"
        )

    def test_calls_set_status_empty(self):
        """cancelStream() must call setStatus('') to clear 'Cancelling...' text."""
        block = self._get_cancel_block()
        assert "setStatus('')" in block or 'setStatus("")' in block, (
            "cancelStream() does not clear status text — "
            "'Cancelling...' can linger if SSE cancel event never arrives"
        )

    def test_hides_cancel_button(self):
        """cancelStream() must hide the cancel button unconditionally."""
        block = self._get_cancel_block()
        assert "btnCancel" in block, (
            "cancelStream() does not reference btnCancel — cancel button may stay visible"
        )

    def test_cleanup_not_inside_try_block(self):
        """Cleanup must happen outside the try block so it runs even if fetch fails."""
        block = self._get_cancel_block()
        # The S.activeStreamId=null and setBusy(false) must appear after the try/catch
        # Verify they are NOT only inside the try block by checking position relative to catch
        try_idx = block.find("try{")
        catch_idx = block.find("}catch(")
        cleanup_idx = block.find("S.activeStreamId=null")
        if cleanup_idx == -1:
            cleanup_idx = block.find("S.activeStreamId = null")
        assert cleanup_idx > catch_idx, (
            "S.activeStreamId cleanup appears to be inside the try block — "
            "it won't run if the fetch throws"
        )


# ── 5. Error path behavior ────────────────────────────────────────────────────

class TestCancelStreamErrorPath:
    """The catch block should not prevent cleanup from running."""

    def test_catch_block_does_not_call_set_status_cancel_failed(self):
        """The catch block must not call setStatus(cancel_failed) on its own.

        Previously: catch(e){setStatus(t('cancel_failed')+e.message)}
        After fix: catch swallows the error; cleanup runs in the outer scope.
        The status is cleared by setStatus('') unconditionally.
        """
        src = read("static/boot.js")
        idx = src.find("async function cancelStream()")
        block = src[idx:idx + 400]
        # The old pattern was setStatus inside catch; new pattern has it outside
        # Look for the catch block specifically
        catch_idx = block.find("}catch(")
        if catch_idx == -1:
            catch_idx = block.find("} catch (")
        assert catch_idx != -1, "No catch block found in cancelStream"
        # Get just the catch body
        brace_open = block.find("{", catch_idx)
        brace_close = block.find("}", brace_open)
        catch_body = block[brace_open:brace_close + 1]
        assert "cancel_failed" not in catch_body, (
            "catch block still calls setStatus(cancel_failed) — "
            "this means a failed cancel shows an error instead of cleaning up silently"
        )


# ── 6. SSE cancel handler still present ──────────────────────────────────────

def test_sse_cancel_handler_still_present():
    """The SSE 'cancel' event handler must still exist in messages.js.

    The new cancelStream() cleanup is not a replacement — the SSE handler
    provides additional cleanup (removes 'Task cancelled.' message, clears
    tool cards, etc.) when the connection is still alive.
    """
    src = read("static/messages.js")
    assert "addEventListener('cancel'" in src or 'addEventListener("cancel"' in src, (
        "SSE cancel event handler missing from messages.js — "
        "live cancellation cleanup path is broken"
    )


def test_sse_cancel_handler_calls_set_busy():
    """The SSE cancel handler must still call setBusy(false)."""
    src = read("static/messages.js")
    idx = src.find("addEventListener('cancel'")
    if idx == -1:
        idx = src.find('addEventListener("cancel"')
    assert idx != -1
    # Find the closing of this handler block (next top-level addEventListener)
    next_handler = src.find("source.addEventListener(", idx + 50)
    block = src[idx:next_handler] if next_handler != -1 else src[idx:idx + 3000]
    assert "setBusy(false)" in block, (
        "SSE cancel handler no longer calls setBusy(false)"
    )


# ── 7. i18n key preserved ─────────────────────────────────────────────────────

def test_cancel_failed_i18n_key_exists_in_all_locales():
    """cancel_failed key must still exist in i18n.js for all locales."""
    src = read("static/i18n.js")
    # Should appear once per locale (en, es, de, ru, zh, zh-Hant)
    locale_count = _locale_count(src)
    count = src.count("cancel_failed:")
    assert count >= locale_count, (
        f"cancel_failed key only found {count} times in i18n.js — "
        f"expected at least {locale_count} (one per locale)"
    )
