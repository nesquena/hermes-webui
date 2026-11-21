"""Launchability policy for the real-browser gate engines.

The browser gates parametrize engines from BROWSERS (default: chromium,webkit).
A host without one engine's system libraries must still run plain
``pytest tests/`` and the standalone gates, so the DEFAULT set skips an engine
that cannot launch. An explicit BROWSERS value stays strict: a deliberate
engine request fails loudly at launch instead of being silently dropped. CI
pins BROWSERS=chromium to match the engines its runner installs.
"""
import os
from functools import lru_cache

DEFAULT_ENGINES = ("chromium", "webkit")


@lru_cache(maxsize=None)
def engine_launchable(engine: str) -> bool:
    """True when Playwright can launch `engine` headless on this host."""
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        return False
    try:
        with sync_playwright() as pw:
            browser = getattr(pw, engine).launch(headless=True)
            browser.close()
    except Exception:
        return False
    return True


def requested_engines():
    """The explicit BROWSERS request as a list, or None when unset/blank."""
    raw = os.environ.get("BROWSERS", "").strip()
    if not raw:
        return None
    return [engine.strip() for engine in raw.split(",") if engine.strip()]


def resolved_engines():
    """Engines to run: the explicit request untouched, else launchable defaults."""
    requested = requested_engines()
    if requested is not None:
        return requested
    return [engine for engine in DEFAULT_ENGINES if engine_launchable(engine)]
