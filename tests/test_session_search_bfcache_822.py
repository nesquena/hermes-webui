"""Tests for #822 — session list empty after browser reload / version update.

Root cause (from Opus analysis): Chrome's bfcache restores a prior search query
into `#sessionSearch` on page restore; `renderSessionListFromCache()` reads that
field and applies it as a title filter — hiding every session.

Fix:
- ``autocomplete="off"`` on the input hints to browsers not to restore the value
- Boot-time explicit `sessionSearch.value = ''` before the first render covers
  fresh loads and hard reloads
- ``pageshow`` listener that checks ``event.persisted`` covers the true bfcache
  restore case (where the async boot IIFE does NOT re-run)
"""

import pathlib
import re

REPO = pathlib.Path(__file__).parent.parent


def read(rel):
    return (REPO / rel).read_text(encoding="utf-8")


class TestSessionSearchAutocompleteAttribute:
    """index.html must opt the session search input out of browser autocomplete/restore."""

    def test_session_search_has_autocomplete_off(self):
        src = read("static/index.html")
        m = re.search(r'<input[^>]*id="sessionSearch"[^>]*>', src)
        assert m, "#sessionSearch input tag not found in index.html"
        tag = m.group(0)
        assert 'autocomplete="off"' in tag or "autocomplete='off'" in tag, (
            '#sessionSearch must have autocomplete="off" so browsers do not '
            "restore a prior search query across reloads or bfcache restores (#822)"
        )


class TestBootClearsSessionSearch:
    """Boot sequence must clear any browser-restored search value before
    the first render, so the initial `renderSessionListFromCache` call sees
    an empty filter and shows all sessions."""

    def test_boot_clears_session_search_value_before_first_render(self):
        src = read("static/boot.js")
        # Must find a line that sets sessionSearch.value = '' at boot
        assert re.search(
            r"getElementById\(['\"]sessionSearch['\"]\)\s*;\s*if\s*\([^)]+\)\s*[^=]+\.value\s*=\s*['\"]{2}"
            r"|sessionSearch[^=]+\.value\s*=\s*['\"]{2}",
            src,
        ), (
            "boot.js must clear #sessionSearch.value to '' before the first render "
            "(before renderSessionList / loadSession) to avoid stale filter from "
            "browser form restoration (#822)"
        )

    def test_boot_clear_is_before_first_render_call(self):
        """The clear must precede the first renderSessionList call path so the
        initial render shows an unfiltered list."""
        src = read("static/boot.js")
        clear_pos = None
        m = re.search(r"getElementById\(['\"]sessionSearch['\"]\)", src)
        if m:
            clear_pos = m.start()
        assert clear_pos is not None, "session search clear not found in boot.js"
        first_render_pos = src.find("renderSessionList()", clear_pos)
        assert first_render_pos != -1, "renderSessionList() call not found after clear"
        assert clear_pos < first_render_pos, (
            "sessionSearch clear must appear before the first renderSessionList() call"
        )


class TestPageShowBfcacheHandler:
    """bfcache restore path: the async boot IIFE does NOT re-run when the
    browser restores the page from back-forward cache, but the DOM — including
    any stale value in #sessionSearch — IS restored. A `pageshow` listener
    with `event.persisted` check is the only reliable way to clear on bfcache."""

    def test_pageshow_listener_registered(self):
        src = read("static/boot.js")
        assert re.search(
            r"addEventListener\(\s*['\"]pageshow['\"]",
            src,
        ), (
            "boot.js must register a `pageshow` event listener to handle "
            "bfcache-restored page views (#822)"
        )

    def test_pageshow_handler_checks_event_persisted(self):
        """Only bfcache restores set event.persisted=true; fresh loads have
        it false and are already handled by the boot IIFE. Guarding prevents
        clearing the search on every page show (which would wipe an in-progress
        user filter if any other pageshow triggers happen)."""
        src = read("static/boot.js")
        m = re.search(
            r"addEventListener\(\s*['\"]pageshow['\"].*?\}\s*\)",
            src,
            re.DOTALL,
        )
        assert m, "pageshow listener body not found"
        body = m.group(0)
        assert "persisted" in body, (
            "pageshow handler must guard on event.persisted so fresh loads "
            "don't double-clear the field"
        )

    def test_pageshow_handler_clears_session_search(self):
        src = read("static/boot.js")
        m = re.search(
            r"addEventListener\(\s*['\"]pageshow['\"].*?\}\s*\)",
            src,
            re.DOTALL,
        )
        assert m
        body = m.group(0)
        assert "sessionSearch" in body, (
            "pageshow handler must target #sessionSearch specifically"
        )
        assert re.search(r"\.value\s*=\s*['\"]{2}", body), (
            "pageshow handler must set sessionSearch.value = ''"
        )

    def test_pageshow_handler_triggers_rerender(self):
        """After clearing on bfcache restore, the cached DOM still shows the
        filtered view. Re-rendering from cache with the now-empty filter
        repopulates the list."""
        src = read("static/boot.js")
        m = re.search(
            r"addEventListener\(\s*['\"]pageshow['\"].*?\}\s*\)",
            src,
            re.DOTALL,
        )
        assert m
        body = m.group(0)
        assert "renderSessionListFromCache" in body or "renderSessionList" in body, (
            "pageshow handler must re-render the list after clearing the filter "
            "so the stale filtered DOM is replaced with the full list"
        )
