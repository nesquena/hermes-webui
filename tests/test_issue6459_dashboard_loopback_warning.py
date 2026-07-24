"""Tests for Dashboard loopback warning suppression (Issue #6459).

When a public browser_url is configured (status.browser_url), the WebUI
correctly opens that URL but should NOT show the "Dashboard is loopback-only"
warning, even when the browser itself is on a non-loopback origin.

This follows the repo's established pattern of asserting on JS source structure
(see test_issue4756, test_issue467, test_todo_live_frontend_static).
"""
import pathlib


def _read_static(name: str) -> str:
    return (pathlib.Path(__file__).resolve().parents[1] / "static" / name).read_text(
        encoding="utf-8"
    )


def _extract_function_body(src: str, signature: str) -> str:
    idx = src.find(signature)
    assert idx >= 0, f"{signature!r} not found in static/ui.js"
    header_end = src.find("){", idx)
    assert header_end >= 0, f"function body start for {signature!r} not found"
    open_idx = header_end + 1
    depth = 0
    i = open_idx
    while i < len(src):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[idx:i + 1]
        i += 1
    raise AssertionError(f"unbalanced braces in {signature!r}")


def test_apply_dashboard_status_suppresses_warning_when_browser_url_set():
    """AC-1: When status.browser_url is set, the loopback warning condition
    must account for it and suppress the warning.

    Before the fix, the warning was derived solely from _dashboardIsBrowserLoopback().
    After the fix, the condition also checks status.browser_url and skips the
    warning when a public browser URL is configured.
    """
    body = _extract_function_body(_read_static("ui.js"), "function _applyDashboardStatus(")

    # The warning derivation must reference the explicit browser_url field —
    # the server's status.url is also populated for loopback auto-probes and
    # must not suppress the warning.
    assert "status.browser_url" in body, (
        "_applyDashboardStatus warning logic must check status.browser_url "
        "to suppress the false loopback-only warning (#6459)"
    )

    # The warning ternary must NOT be the old unconditional form that only
    # checks browser loopback. We verify the guard variable is present AND used
    # in the warning derivation (not just declared).
    assert "hasBrowserUrl" in body, (
        "_applyDashboardStatus must derive a hasBrowserUrl guard from "
        "status.browser_url before deciding the warning (#6459)"
    )

    # Critical: the guard must appear in the WARNING derivation line itself,
    # not just be declared and ignored. Extract the warning ternary.
    warning_idx = body.index("const warning=")
    warning_line_end = body.index("\n", warning_idx)
    warning_line = body[warning_idx:warning_line_end]
    assert "hasBrowserUrl" in warning_line, (
        "hasBrowserUrl guard must be used in the warning derivation ternary, "
        "not merely declared elsewhere in the function (#6459)"
    )

    guard_idx = body.index("const hasBrowserUrl=")
    guard_line_end = body.index("\n", guard_idx)
    guard_line = body[guard_idx:guard_line_end]
    assert "status.browser_url" in guard_line, (
        "hasBrowserUrl must be based on the explicit browser_url field (#6459)"
    )
    assert "status.url" not in guard_line, (
        "the auto-probed loopback status.url must not suppress the warning (#6459)"
    )


def test_apply_dashboard_status_preserves_warning_when_no_browser_url():
    """AC-2: The existing loopback-warning behavior must be preserved when no
    browser_url is configured. The _dashboardIsBrowserLoopback() call must
    still be present in the function body.
    """
    body = _extract_function_body(_read_static("ui.js"), "function _applyDashboardStatus(")

    # The loopback check must still be present — it remains the fallback
    # when no browser_url is configured.
    assert "_dashboardIsBrowserLoopback" in body, (
        "_applyDashboardStatus must still call _dashboardIsBrowserLoopback() "
        "to produce the warning when no public browser_url is configured"
    )
    assert "dashboard_loopback_warning" in body, (
        "_applyDashboardStatus must still reference the dashboard_loopback_warning "
        "translation key for the no-browser_url case"
    )
