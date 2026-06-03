"""Regression test for #1669 follow-up — sidebar scroll jump fix.

The original PR #1669 added DOM virtualization to renderSessionListFromCache,
which:

1. Attached an unconditional scroll listener to the session list
2. The scroll listener triggers renderSessionListFromCache() on every rAF
3. The render rebuilds the list DOM via list.innerHTML='' / appendChild loop
4. After the rebuild, scrollTop was only restored when virtualWindow.virtualized
   was true (i.e. total > 80 rows)
5. For lists ≤ 80 rows, the scrollTop reset to 0 on every scroll event,
   producing a "scroll keeps jumping back" feel.

This test pins:
- The non-virtualized branch always restores scrollTop after a rebuild
- The scroll handler short-circuits when total <= threshold (prevents the
  rebuild churn entirely on small lists)
"""
from pathlib import Path

SESSIONS_JS = Path(__file__).parent.parent / "static" / "sessions.js"


def _read_source():
    return SESSIONS_JS.read_text()


def test_render_restores_scroll_top_for_non_virtualized_lists():
    """The bug: virtualWindow.virtualized=false skipped the scrollTop restore.

    The fix: restore scrollTop whenever listScrollTopBeforeRender > 0,
    regardless of virtualized flag. Otherwise small lists (≤80 rows) reset
    to scrollTop=0 on every render.
    """
    src = _read_source()
    # The new branch must include listScrollTopBeforeRender>0 as the guard
    # rather than virtualWindow.virtualized
    assert "}else if(listScrollTopBeforeRender>0){" in src, (
        "Expected the scrollTop-restore guard to use listScrollTopBeforeRender>0, "
        "not virtualWindow.virtualized — without this fix, small lists drop "
        "scrollTop to 0 on every scroll event."
    )


def test_scroll_handler_short_circuits_below_virtualization_threshold():
    """The bug: the rAF re-render fired on every scroll event regardless of
    whether virtualization was actually needed. For ≤80-row lists this caused
    full DOM rebuild on every scroll tick.

    The fix: _scheduleSessionVirtualizedRender skips the rebuild when
    total <= SESSION_VIRTUAL_THRESHOLD_ROWS — there's no virtual window to
    recompute on small lists, and the rebuild was wasteful (and bug-prone).
    """
    src = _read_source()
    # Locate the function body
    start = src.find("function _scheduleSessionVirtualizedRender()")
    end = src.find("function _ensureSessionVirtualScrollHandler", start)
    body = src[start:end]
    # The fix introduces an early-return when total <= SESSION_VIRTUAL_THRESHOLD_ROWS
    assert "SESSION_VIRTUAL_THRESHOLD_ROWS" in body, (
        "Expected _scheduleSessionVirtualizedRender to read the threshold; "
        "without this guard, the rAF re-render fires on every scroll event "
        "even when there's nothing to virtualize."
    )
    assert "total<=SESSION_VIRTUAL_THRESHOLD_ROWS" in body or "total <= SESSION_VIRTUAL_THRESHOLD_ROWS" in body, (
        "Expected explicit total<=THRESHOLD comparison to short-circuit the re-render."
    )
    # The early return must be BEFORE the rAF schedule (else it's dead code)
    early_return_idx = body.find("return")
    raf_idx = body.find("requestAnimationFrame")
    assert early_return_idx > 0 and early_return_idx < raf_idx, (
        "The total<=THRESHOLD short-circuit must return BEFORE scheduling the rAF."
    )


def test_virtualization_still_active_for_large_lists():
    """Regression: ensure the threshold + virtualWindow logic is still in place
    for large lists. The fix must not break the original virtualization path.
    """
    src = _read_source()
    assert "SESSION_VIRTUAL_THRESHOLD_ROWS = 80" in src, (
        "Threshold constant must remain at 80 rows."
    )
    # _sessionVirtualWindow function still defined
    assert "function _sessionVirtualWindow" in src
    # virtualWindow.virtualized branch still drives spacer rendering
    assert "virtualWindow.virtualized" in src
