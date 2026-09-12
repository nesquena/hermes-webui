"""Shared Playwright helpers for browser regression tests.

These tests need a real Chromium binary. In constrained environments — CI images
or sandboxes where ``playwright install chromium`` was never run — the Python
package imports fine but ``chromium.launch()`` fails because the browser
*executable* is absent (``Executable doesn't exist at .../chrome-headless-shell``).

That is a missing optional dependency, not a product regression, so such tests
should ``pytest.skip`` rather than FAIL. Any OTHER launch/assertion error is
re-raised unchanged so real bugs still surface.
"""
from __future__ import annotations

import pytest

# Substrings (matched case-insensitively) that identify a "browser executable is
# missing / not installed" launch failure, as opposed to a real assertion/logic
# error we must not hide. These are emitted by Playwright only when the browser
# binary itself is absent.
_BROWSER_MISSING_MARKERS = (
    "executable doesn't exist",
    "please run the following command to download new browsers",
    "playwright install",
    "looks like playwright was just installed",
)


def _is_browser_missing_error(exc: BaseException) -> bool:
    message = str(exc).lower()
    return any(marker in message for marker in _BROWSER_MISSING_MARKERS)


def skip_if_browser_unavailable(exc: BaseException) -> None:
    """``pytest.skip`` when *exc* is a missing-browser-binary launch failure.

    Returns normally (does nothing) for any other exception so the caller can
    re-raise it — real assertion errors and unexpected failures must still fail
    the test.
    """
    if _is_browser_missing_error(exc):
        pytest.skip(
            "playwright browser not installed; run `playwright install chromium`"
        )


def launch_chromium_or_skip(playwright, **launch_kwargs):
    """Launch Chromium, or ``pytest.skip`` if the browser binary is missing.

    All keyword arguments are forwarded verbatim to
    ``playwright.chromium.launch`` so each caller keeps its exact launch flags.
    """
    try:
        return playwright.chromium.launch(**launch_kwargs)
    except Exception as exc:  # noqa: BLE001 - narrowed by skip_if_browser_unavailable
        skip_if_browser_unavailable(exc)
        raise
