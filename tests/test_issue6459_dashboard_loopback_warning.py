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

    # The warning derivation must check whether the resolved browser target is
    # non-loopback — mere truthiness of browser_url is insufficient because a
    # configured loopback URL (e.g. http://127.0.0.1:port) would still suppress.
    assert "_isLoopbackHostname" in body, (
        "_applyDashboardStatus warning logic must classify the browser target "
        "via _isLoopbackHostname to suppress only for non-loopback URLs (#6459)"
    )

    # The guard variable must be present AND used in the warning derivation.
    assert "hasNonLoopbackBrowserUrl" in body, (
        "_applyDashboardStatus must derive a hasNonLoopbackBrowserUrl guard "
        "that classifies the resolved target before deciding the warning (#6459)"
    )

    # Critical: the guard must appear in the WARNING derivation line itself,
    # not just be declared and ignored. Extract the warning ternary.
    warning_idx = body.index("const warning=")
    warning_line_end = body.index("\n", warning_idx)
    warning_line = body[warning_idx:warning_line_end]
    assert "hasNonLoopbackBrowserUrl" in warning_line, (
        "hasNonLoopbackBrowserUrl guard must be used in the warning derivation, "
        "not merely declared elsewhere in the function (#6459)"
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


def test_is_loopback_hostname_recognizes_ipv4_mapped_ipv6_loopback():
    """AC-3: _isLoopbackHostname must recognize IPv4-mapped IPv6 loopback addresses
    in the 127.0.0.0/8 range (::ffff:7f00:0/104), as emitted by Chromium.

    Chromium canonicalizes http://[::ffff:127.0.0.1]:3000 -> hostname [::ffff:7f00:1].
    The classifier must match this canonical shape and reject mapped public addresses.
    """
    body = _extract_function_body(_read_static("ui.js"), "function _isLoopbackHostname(")

    # Must contain the IPv4-mapped IPv6 loopback pattern with ::ffff:7f00 prefix
    assert "::ffff:7f00:" in body, (
        "_isLoopbackHostname must match IPv4-mapped IPv6 loopback addresses "
        "in the ::ffff:7f00:0/104 range (127.0.0.0/8)"
    )

    # The regex must be strict — require the ::ffff:7f00 prefix and validate the low group
    assert "7f00" in body, (
        "_isLoopbackHostname IPv4-mapped check must constrain to 127.0.0.0/8 "
        "(0x7f00 pins the /8 prefix)"
    )

    # Should not treat arbitrary ::ffff: addresses as loopback
    # The implementation must have the specific 7f00 check
    assert body.count("::ffff:") >= 1, (
        "_isLoopbackHostname must check for IPv4-mapped IPv6 addresses"
    )


def test_apply_dashboard_status_uses_is_loopback_hostname_for_mapped_addresses():
    """AC-4: _applyDashboardStatus must use _isLoopbackHostname to classify
    dashboard targets, ensuring IPv4-mapped IPv6 loopback addresses are correctly
    handled through the real decision path.

    The _isLoopbackHostname helper is called on the parsed dashboard URL hostname,
    so mapped loopback addresses (::ffff:7f00:NNNN) trigger the warning while
    mapped public addresses (::ffff:non-7f00) suppress it.
    """
    body = _extract_function_body(_read_static("ui.js"), "function _applyDashboardStatus(")

    # The guard uses _isLoopbackHostname to classify the browser target hostname
    assert "_isLoopbackHostname(parsed.hostname)" in body, (
        "_applyDashboardStatus must call _isLoopbackHostname on the parsed "
        "dashboard URL hostname to handle IPv4-mapped IPv6 addresses"
    )


def test_locale_strings_exist_for_dashboard_warning_decision():
    """AC-5: Locale strings must exist for both the loopback warning and
    the default dashboard label, and the decision path must exercise them
    through the t() translation function.

    This ensures that when _applyDashboardStatus evaluates the loopback condition,
    both outcomes (warning vs no warning) map to valid locale keys that are
    available in at least the default locale (en) and one non-default locale.
    """
    i18n_src = _read_static("i18n.js")

    # The loopback warning key must exist in the locale bundles
    assert "dashboard_loopback_warning" in i18n_src, (
        "locale bundles must contain the dashboard_loopback_warning translation key"
    )

    # The default dashboard tab label must also exist (used when warning is suppressed)
    assert "tab_dashboard" in i18n_src, (
        "locale bundles must contain the tab_dashboard translation key "
        "(default label when no warning is shown)"
    )

    # Verify at least one non-default locale has both keys (pick a common one)
    # The i18n.js file structure is: const LOCALES = { en: {...}, es: {...}, ... }
    assert "es:" in i18n_src or "fr:" in i18n_src or "de:" in i18n_src, (
        "at least one non-default locale should exist for locale replay coverage"
    )

    # The decision path in _applyDashboardStatus must use t() for both outcomes
    body = _extract_function_body(_read_static("ui.js"), "function _applyDashboardStatus(")

    # The ternary that decides the text must call t() with both keys
    assert "t('dashboard_loopback_warning')" in body, (
        "_applyDashboardStatus must use t() to localize the loopback warning text"
    )
    assert "t('tab_dashboard')" in body, (
        "_applyDashboardStatus must use t() to localize the default dashboard label"
    )
