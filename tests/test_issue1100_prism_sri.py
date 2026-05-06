"""Tests for #1100 — Prism.js SRI integrity check no longer blocks theme CSS."""
import re


def test_prism_theme_link_has_no_integrity():
    """The prism-tomorrow.min.css link must not have an integrity attribute."""
    with open("static/index.html") as f:
        src = f.read()
    # Find the prism-theme link tag
    m = re.search(
        r'<link[^>]*id="prism-theme"[^>]*>',
        src
    )
    assert m, "prism-theme link must exist"
    link_tag = m.group(0)
    assert "integrity=" not in link_tag, \
        "prism-theme link must not have integrity attribute (causes intermittent failures)"


def test_prism_theme_self_hosted():
    """Prism theme CSS must come from the self-hosted vendor folder, not a CDN.

    The Neo fork bundles Prism under static/vendor/prismjs@1.29.0/ to remove
    the cdn.jsdelivr.net dependency on first paint (SRI mismatches were the
    original cause of #1100, and the cold CDN fetch was a measurable latency
    contributor on the VPS deploy)."""
    with open("static/index.html") as f:
        src = f.read()
    m = re.search(
        r'<link[^>]*id="prism-theme"[^>]*href="([^"]*)"[^>]*>',
        src
    )
    assert m, "prism-theme link must exist"
    href = m.group(1)
    assert href.startswith("static/vendor/prismjs@1.29.0/"), \
        f"prism-theme href must point at static/vendor/prismjs@1.29.0/, got: {href}"


def test_prism_theme_version_pinned():
    """The prism CSS path must pin the version to prevent breaking changes."""
    with open("static/index.html") as f:
        src = f.read()
    m = re.search(
        r'<link[^>]*id="prism-theme"[^>]*href="([^"]*)"[^>]*>',
        src
    )
    assert m, "prism-theme link must have href"
    href = m.group(1)
    assert "@1.29.0" in href, \
        f"Prism CSS version must be pinned, found href: {href}"


def test_prism_js_self_hosted():
    """Prism core + autoloader must be loaded from the self-hosted vendor folder."""
    with open("static/index.html") as f:
        src = f.read()
    assert re.search(
        r'src="static/vendor/prismjs@1\.29\.0/components/prism-core\.min\.js"',
        src,
    ), "prism-core.min.js must be loaded from static/vendor/"
    assert re.search(
        r'src="static/vendor/prismjs@1\.29\.0/plugins/autoloader/prism-autoloader\.min\.js"',
        src,
    ), "prism-autoloader.min.js must be loaded from static/vendor/"


def test_prism_autoloader_languages_path_self_hosted():
    """Autoloader must fetch language components from the local vendor folder
    so missing-language requests don't fall back to cdn.jsdelivr.net."""
    with open("static/index.html") as f:
        src = f.read()
    assert "Prism.plugins.autoloader.languages_path" in src, \
        "index.html must configure Prism.plugins.autoloader.languages_path"
    assert "static/vendor/prismjs@1.29.0/components/" in src, \
        "languages_path must point at static/vendor/prismjs@1.29.0/components/"


def test_boot_js_set_resolved_theme_no_integrity():
    """_setResolvedTheme in boot.js must not re-apply integrity on theme switch."""
    with open("static/boot.js") as f:
        src = f.read()
    # _setResolvedTheme function must exist
    assert "_setResolvedTheme" in src, "_setResolvedTheme function must exist"
    # Must NOT assign link.integrity with a hash value
    assert not re.search(r'link\.integrity\s*=\s*["\']sha', src), \
        "_setResolvedTheme must not set link.integrity to an SRI hash"
    # Must NOT have a wantIntegrity variable
    assert "wantIntegrity" not in src, \
        "wantIntegrity variable should be removed from _setResolvedTheme"
    # Should clear integrity (set to empty) when switching theme
    assert re.search(r"link\.integrity\s*=\s*['\"]", src), \
        "_setResolvedTheme should clear link.integrity on theme switch"
