"""Regression tests for the <meta name="theme-color"> bridge.

Covers:
- index.html declares the static prefers-color-scheme media variants (light + dark).
- index.html declares a single `id="hermes-theme-color"` meta tag for runtime updates.
- Inline pre-paint script reads localStorage `hermes-theme` and seeds the meta tag
  before any external JS loads (no flash of wrong colour for native chrome).
- boot.js defines `_syncThemeColorMeta()` and calls it from `_setResolvedTheme()`
  (covering both prism-loaded and prism-absent paths) and from `_applySkin()`.
- The helper reads `getComputedStyle(html).getPropertyValue('--bg')`, which means
  every skin (Default, Sienna, Sisyphus, Charizard, etc.) reaches the meta tag.

This bridge is the source of truth that native WKWebView wrappers
(hermes-webui/hermes-swift-mac) read instead of pixel-sampling the page —
overlay-resistant (modals/lightboxes don't poison it) and IPC-free.
"""
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
INDEX = ROOT / "static" / "index.html"
BOOT = ROOT / "static" / "boot.js"
STYLE = ROOT / "static" / "style.css"


class TestIndexHtmlMetaTags:
    def test_static_prefers_color_scheme_variants_present(self):
        """Two static <meta name="theme-color"> tags with media queries cover the
        pre-load case for browsers that use the OS color scheme as a hint.
        """
        src = INDEX.read_text(encoding="utf-8")
        assert 'name="theme-color"' in src
        assert 'media="(prefers-color-scheme: light)"' in src
        assert 'media="(prefers-color-scheme: dark)"' in src

    def test_runtime_theme_color_meta_has_stable_id(self):
        """A third theme-color meta tag (no media query) carries id="hermes-theme-color"
        so boot.js can update it on theme/skin change. The id is the contract the
        Mac Swift app reads via `document.getElementById('hermes-theme-color')`.
        """
        src = INDEX.read_text(encoding="utf-8")
        assert 'id="hermes-theme-color"' in src
        # Must be on a meta tag (not some other element)
        assert '<meta name="theme-color" id="hermes-theme-color"' in src

    def test_inline_pre_paint_script_seeds_meta(self):
        """An inline script in <head> seeds the runtime meta tag from localStorage
        before any external JS loads. This prevents a single-frame flash of the
        OS-default theme-color when the user has explicitly chosen the opposite.
        """
        src = INDEX.read_text(encoding="utf-8")
        assert "hermes-theme-color" in src
        # The seeder must read from the same localStorage key the theme bootstrap uses.
        assert "localStorage.getItem('hermes-theme')" in src
        # And must call setAttribute('content', ...) on the meta tag.
        assert "setAttribute('content'" in src or 'setAttribute("content"' in src


class TestBootJsThemeColorSync:
    def test_sync_helper_defined(self):
        src = BOOT.read_text(encoding="utf-8")
        assert "function _syncThemeColorMeta()" in src

    def test_sync_helper_reads_computed_bg_var(self):
        """The helper must read the computed --bg CSS custom property so each skin's
        background reaches the meta tag (Sienna gets terracotta, Sisyphus gets purple,
        etc.).
        """
        src = BOOT.read_text(encoding="utf-8")
        # The helper reads getComputedStyle on documentElement and extracts --bg.
        assert "getComputedStyle(document.documentElement).getPropertyValue('--bg')" in src

    def test_sync_helper_targets_known_meta_id(self):
        """The helper must target the same id declared in index.html. Drift here
        is the most common way a one-line frontend change silently breaks the
        Swift app's theme-color reader.
        """
        src = BOOT.read_text(encoding="utf-8")
        assert "getElementById('hermes-theme-color')" in src

    def test_set_resolved_theme_calls_sync_in_both_branches(self):
        """_setResolvedTheme has two exit paths:
            1. Early return when the Prism stylesheet is absent (onboarding pages,
               error pages, etc.).
            2. Normal completion after possibly toggling the Prism stylesheet href.
        Both paths must update the meta tag — otherwise the Mac chrome would lag
        the page on those paths.
        """
        src = BOOT.read_text(encoding="utf-8")
        # Path 1 — the early return must call the sync first.
        assert "if(!link){ _syncThemeColorMeta(); return; }" in src
        # Path 2 — the trailing call must follow the link-href update.
        assert (
            "if(link.href!==want){ link.integrity=''; link.href=want; }\n"
            "  _syncThemeColorMeta();"
        ) in src

    def test_apply_skin_calls_sync(self):
        """Switching skin (Default → Sienna → Sisyphus, etc.) recomputes --bg and
        must update the meta tag so the Mac chrome flips with the page.
        """
        src = BOOT.read_text(encoding="utf-8")
        # The end of _applySkin must call the sync.
        # We assert the literal anchor block from the recent edit so any drift
        # in surrounding code triggers a clear test failure.
        anchor = (
            "function _applySkin(name){\n"
            "  const key=(name||'default').toLowerCase();\n"
            "  if(key==='default') delete document.documentElement.dataset.skin;\n"
            "  else document.documentElement.dataset.skin=key;\n"
            "  _syncThemeColorMeta();\n"
            "}"
        )
        assert anchor in src


class TestStyleCssBgVarPresent:
    """The bridge depends on every theme/skin defining the --bg CSS variable.
    These are the canonical locations as of v0.51.x — if any are missing or
    renamed the meta-tag reader returns empty and the Mac chrome reverts to the
    static prefers-color-scheme defaults.
    """

    def test_root_light_defines_bg(self):
        src = STYLE.read_text(encoding="utf-8")
        # :root (light default) at the top of the file defines --bg.
        assert "--bg:#FEFCF7" in src or "--bg: #FEFCF7" in src

    def test_root_dark_defines_bg(self):
        src = STYLE.read_text(encoding="utf-8")
        assert "--bg:#0D0D1A" in src or "--bg: #0D0D1A" in src
