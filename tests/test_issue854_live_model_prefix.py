"""Regression tests for #854 — live-fetched models must route through the
configured portal provider, not OpenRouter."""
import os
import re


_SRC = os.path.join(os.path.dirname(__file__), "..")


def _read(name):
    return open(os.path.join(_SRC, name), encoding="utf-8").read()


class TestLiveModelPrefix:
    """_fetchLiveModels() must apply @provider: prefix to live-fetched model
    IDs when the fetch is for the active portal provider (Nous, OpenCode,
    etc.) — including IDs that already contain a slash (the upstream vendor
    namespace), since those would otherwise be mis-routed via OpenRouter."""

    def test_apply_prefix_to_any_non_at_id(self):
        """The prefix check must not gate on `!mid.includes('/')`.  The bug
        scenario in #854 is precisely about slash-prefixed IDs like
        `minimax/minimax-m2.7` from Nous's live catalog — excluding them
        leaves the bug unfixed."""
        js = _read("static/ui.js")
        # Live model prefix logic was extracted to _addLiveModelsToSelect (#872)
        m = re.search(r'function _addLiveModelsToSelect\(.*?\n\}', js, re.DOTALL)
        if not m:
            m = re.search(r'async function _fetchLiveModels\(.*?\n\}', js, re.DOTALL)
        assert m, "_addLiveModelsToSelect or _fetchLiveModels not found"
        fn = m.group(0)
        # The prefix application block must NOT have `!mid.includes('/')`
        # as a guard — slash-prefixed IDs from portal providers also need
        # the prefix.
        prefix_block = re.search(
            r"if\s*\(\s*[^)]*!mid\.startsWith\(['\"]@['\"]\)[^)]*\)\s*\{\s*mid\s*=\s*`@",
            fn,
        )
        assert prefix_block, "@provider: prefix application not found"
        # The block must prefix when portal-fetch is true and not already @-prefixed.
        # It must NOT check for slash presence — that's the bug.
        assert "!mid.includes('/')" not in prefix_block.group(0), (
            "The prefix application must NOT exclude slash-prefixed IDs — "
            "portal catalogs return `minimax/minimax-m2.7` and similar that "
            "need `@nous:` prefix to route through the configured portal (#854)"
        )

    def test_portal_fetch_flag_semantics(self):
        """The flag controlling prefix application should be named/structured
        so the prefix is ADDED when the flag is true (portal fetch), not when
        false.  Earlier revision used `!_needsPrefix` (inverted)."""
        js = _read("static/ui.js")
        # Live model prefix logic was extracted to _addLiveModelsToSelect (#872)
        m = re.search(r'function _addLiveModelsToSelect\(.*?\n\}', js, re.DOTALL)
        if not m:
            m = re.search(r'async function _fetchLiveModels\(.*?\n\}', js, re.DOTALL)
        assert m
        fn = m.group(0)
        # New flag: _isPortalFetch (positive semantics)
        assert "_isPortalFetch" in fn, (
            "flag should be named _isPortalFetch to reflect positive semantics "
            "(prefix ADDED when true, not when false)"
        )
        # And the prefix application should be guarded BY the flag (not by its negation)
        gate = re.search(
            r"if\s*\(\s*_isPortalFetch\s*&&\s*!mid\.startsWith",
            fn,
        )
        assert gate, "prefix application must be guarded by _isPortalFetch (true ⇒ prefix)"

    def test_portal_fetch_excludes_openrouter_and_custom(self):
        """OpenRouter IDs are cross-namespace by design, and `custom` providers
        use user-defined bare names — neither should get a `@provider:` prefix."""
        js = _read("static/ui.js")
        # Live model prefix logic was extracted to _addLiveModelsToSelect (#872)
        m = re.search(r'function _addLiveModelsToSelect\(.*?\n\}', js, re.DOTALL)
        if not m:
            m = re.search(r'async function _fetchLiveModels\(.*?\n\}', js, re.DOTALL)
        assert m
        fn = m.group(0)
        assert "_ap!=='openrouter'" in fn or "_ap !== 'openrouter'" in fn, (
            "portal flag must exclude openrouter"
        )
        assert "_ap!=='custom'" in fn or "_ap !== 'custom'" in fn, (
            "portal flag must exclude custom"
        )


class TestCheckProviderMismatchAtPrefix:
    """_checkProviderMismatch() must not warn on `@provider:`-prefixed IDs —
    the prefix itself is an explicit provider hint, so there's no mismatch."""

    def test_returns_null_for_at_prefix_ids(self):
        js = _read("static/ui.js")
        m = re.search(r'function _checkProviderMismatch\(.*?\n\}', js, re.DOTALL)
        assert m, "_checkProviderMismatch not found"
        fn = m.group(0)
        assert "modelId.startsWith('@')" in fn or 'modelId.startsWith("@")' in fn, (
            "_checkProviderMismatch must return null early for @provider: prefixed IDs"
        )
