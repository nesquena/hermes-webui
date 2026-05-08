"""Regression tests for #1539 — removing a provider in Settings must invalidate
every dropdown surface that caches /api/models, so the removed provider
disappears immediately without a server restart or page reload.

The bug
-------
Pre-fix, ``_removeProviderKey()`` in ``static/panels.js`` only called
``loadProvidersPanel()`` after deletion. That refreshed the providers card
list but left these JS-side caches stale:

  * ``_slashModelCache`` / ``_slashModelCachePromise`` (``static/commands.js``) —
    cache for the ``/model`` slash-command suggestions.
  * ``_dynamicModelLabels`` / ``window._configuredModelBadges`` (``static/ui.js``) —
    populated by ``populateModelDropdown()`` on boot and on profile switch.

Layered server-side cache via ``api/config.invalidate_models_cache`` was
already flushed (``set_provider_key`` calls it on both add + remove), so the
next ``/api/models`` request would return the correct list — but no consumer
was triggering one.

The fix
-------
``static/commands.js`` exposes an ``_invalidateSlashModelCache()`` helper on
``window``. ``static/panels.js`` calls it from a shared
``_refreshModelDropdownsAfterProviderChange()`` helper after both the save
and the remove paths, plus invokes ``populateModelDropdown()`` to rebuild
the composer / Settings dropdowns and ``_configuredModelBadges`` map.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parent.parent


def _read_static(name: str) -> str:
    return (REPO / "static" / name).read_text(encoding="utf-8")


def _extract_function_body(src: str, signature: str) -> str:
    """Return the source of a top-level ``async function NAME(...)`` /
    ``function NAME(...)`` declaration via brace-balance — robust to nested
    blocks (try/catch/await) and not dependent on indentation.
    """
    idx = src.find(signature)
    if idx == -1:
        raise AssertionError(f"signature {signature!r} not found in source")
    open_idx = src.find("{", idx)
    if open_idx == -1:
        raise AssertionError(f"could not find opening brace after {signature!r}")
    depth = 0
    for i in range(open_idx, len(src)):
        c = src[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return src[idx : i + 1]
    raise AssertionError(f"unbalanced braces in {signature!r}")


class TestSlashModelCacheInvalidator:
    """``static/commands.js`` must export the helper to ``window`` so
    ``static/panels.js`` can drop the slash-command cache without poking
    module-local lets across module boundaries."""

    def test_invalidator_helper_defined(self):
        src = _read_static("commands.js")
        assert "function _invalidateSlashModelCache(" in src, (
            "_invalidateSlashModelCache helper missing from static/commands.js. "
            "Without it static/panels.js cannot drop the /model slash-command "
            "cache when a provider is added/removed (#1539)."
        )

    def test_invalidator_clears_both_cache_slots(self):
        src = _read_static("commands.js")
        body = _extract_function_body(src, "function _invalidateSlashModelCache(")
        # Cache slots from static/commands.js:84-85 — keep both null'd.
        assert "_slashModelCache=null" in body, (
            "_invalidateSlashModelCache must null _slashModelCache so the next "
            "/model autocomplete refetches /api/models."
        )
        assert "_slashModelCachePromise=null" in body, (
            "_invalidateSlashModelCache must null _slashModelCachePromise so an "
            "in-flight load doesn't resolve into the stale cache slot after "
            "invalidation."
        )

    def test_invalidator_exposed_on_window(self):
        src = _read_static("commands.js")
        # Exposed on window via a typeof-guarded assignment so the module is
        # also importable in headless test contexts (vm.runInContext) that
        # don't define a window global.
        assert "window._invalidateSlashModelCache=_invalidateSlashModelCache" in src, (
            "_invalidateSlashModelCache must be exposed on window so static/panels.js "
            "can invoke it across module boundaries."
        )
        assert "typeof window!=='undefined'" in src, (
            "The window-export assignment must be guarded by `typeof window!=='undefined'` "
            "so static/commands.js stays importable in headless vm contexts (the "
            "tests/test_cli_only_slash_commands.py harness has no window global)."
        )


class TestProviderRemoveInvalidatesDropdowns:
    """The remove path in ``static/panels.js`` must trigger the dropdown-cache
    flush and rebuild — otherwise the dropped provider lingers in every
    /model dropdown until the page reloads (#1539)."""

    def test_remove_path_invokes_dropdown_flush(self):
        src = _read_static("panels.js")
        body = _extract_function_body(src, "async function _removeProviderKey(")
        assert "_refreshModelDropdownsAfterProviderChange()" in body, (
            "_removeProviderKey must call _refreshModelDropdownsAfterProviderChange() "
            "after a successful delete. Without this, the JS-side caches "
            "(_slashModelCache, _dynamicModelLabels, _configuredModelBadges) "
            "still offer the deleted provider's models until reload (#1539)."
        )

    def test_save_path_invokes_dropdown_flush(self):
        """Defense-in-depth: adding a key has the same staleness shape — the
        new provider's models won't show up until reload without this call.
        Bundled in #1539."""
        src = _read_static("panels.js")
        body = _extract_function_body(src, "async function _saveProviderKey(")
        assert "_refreshModelDropdownsAfterProviderChange()" in body, (
            "_saveProviderKey must also call _refreshModelDropdownsAfterProviderChange() "
            "so a newly-configured provider's models appear in every dropdown "
            "without a reload. Same staleness shape as the remove path (#1539)."
        )

    def test_dropdown_flush_helper_defined(self):
        src = _read_static("panels.js")
        assert "function _refreshModelDropdownsAfterProviderChange(" in src, (
            "_refreshModelDropdownsAfterProviderChange must be defined in "
            "static/panels.js (single helper used by both save + remove paths)."
        )

    def test_dropdown_flush_calls_slash_cache_invalidator(self):
        src = _read_static("panels.js")
        body = _extract_function_body(src, "function _refreshModelDropdownsAfterProviderChange(")
        # Must invoke the commands.js helper — directly poking module-local
        # lets across module boundaries is brittle.
        assert "_invalidateSlashModelCache" in body, (
            "_refreshModelDropdownsAfterProviderChange must call "
            "window._invalidateSlashModelCache() so the /model slash-command "
            "cache is dropped (covers the slash-command surface from #1539)."
        )

    def test_dropdown_flush_calls_populate_model_dropdown(self):
        src = _read_static("panels.js")
        body = _extract_function_body(src, "function _refreshModelDropdownsAfterProviderChange(")
        assert "populateModelDropdown" in body, (
            "_refreshModelDropdownsAfterProviderChange must call "
            "populateModelDropdown() so the composer model picker, Settings → "
            "Default Model dropdown, _dynamicModelLabels, and "
            "_configuredModelBadges all rebuild from a fresh /api/models "
            "response (covers the dropdown + badge surfaces from #1539)."
        )

    def test_dropdown_flush_is_resilient_to_missing_modules(self):
        """If commands.js or ui.js failed to load, the providers panel must
        still update — the dropdown flush is best-effort (#1539)."""
        src = _read_static("panels.js")
        body = _extract_function_body(src, "function _refreshModelDropdownsAfterProviderChange(")
        # Outer try/catch wraps the whole helper so a runtime error inside
        # populateModelDropdown / cache flush cannot surface as an unhandled
        # rejection that breaks the surrounding save/remove flow.
        assert re.search(r"\btry\s*\{", body), (
            "_refreshModelDropdownsAfterProviderChange must wrap its work in "
            "try/catch — if commands.js or ui.js failed to load, a missing "
            "function should not break the providers panel update (#1539)."
        )
        # And the populateModelDropdown call must be guarded by typeof — the
        # dropdown rebuild is best-effort.
        assert "typeof populateModelDropdown" in body, (
            "populateModelDropdown lookup must use typeof so it gracefully "
            "skips when ui.js hasn't loaded yet."
        )

    def test_dropdown_flush_does_not_block_panel_refresh(self):
        """populateModelDropdown is async; its result must not be awaited
        synchronously inside the helper — otherwise a slow /api/models would
        delay the providers panel re-render (#1539)."""
        src = _read_static("panels.js")
        body = _extract_function_body(src, "function _refreshModelDropdownsAfterProviderChange(")
        # The helper itself is non-async (signature checked indirectly: the
        # source begins with 'function _refresh...', not 'async function').
        # Anything async is fired with Promise.resolve(...).catch(...) so the
        # provider panel re-render is not blocked.
        assert body.startswith("function _refreshModelDropdownsAfterProviderChange"), (
            "_refreshModelDropdownsAfterProviderChange should be a sync helper "
            "that fires-and-forgets populateModelDropdown — not an async one "
            "the save/remove paths await."
        )


class TestServerSideInvariantPreserved:
    """Server-side ``invalidate_models_cache()`` is the load-bearing invariant
    that lets the next /api/models request return correct data; #1539 was a
    pure frontend bug, but pin the server-side wiring so a refactor of
    ``set_provider_key`` cannot silently regress it."""

    def test_set_provider_key_invalidates_cache(self):
        src = (REPO / "api" / "providers.py").read_text(encoding="utf-8")
        # set_provider_key is the canonical write path — both add and remove
        # flow through it (remove_provider_key calls set_provider_key(pid, None)).
        m = re.search(
            r"def set_provider_key\([^)]*\).*?(?=\ndef |\Z)",
            src,
            re.DOTALL,
        )
        assert m, "set_provider_key not found in api/providers.py"
        body = m.group(0)
        assert "invalidate_models_cache()" in body, (
            "set_provider_key must call invalidate_models_cache() so the "
            "server-side TTL cache is flushed on every add/remove. Without "
            "this, even a perfectly-cached frontend would receive stale data."
        )
