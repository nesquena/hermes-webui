"""Tests for #1100 - Prism.js asset loading after vendoring.

Originally tested SRI integrity removal from CDN-loaded Prism assets.
Updated to verify vendored local assets are correctly configured.
"""
import re


def test_prism_theme_link_has_no_integrity():
    """The prism-tomorrow.min.css link must not have an integrity attribute."""
    with open("static/index.html") as f:
        src = f.read()
    m = re.search(
        r'<link[^>]*id="prism-theme"[^>]*>',
        src
    )
    assert m, "prism-theme link must exist"
    link_tag = m.group(0)
    assert "integrity=" not in link_tag, \
        "prism-theme link must not have integrity attribute"


def test_prism_theme_link_is_same_origin():
    """Vendored prism-theme link should not have crossorigin (same-origin)."""
    with open("static/index.html") as f:
        src = f.read()
    m = re.search(
        r'<link[^>]*id="prism-theme"[^>]*>',
        src
    )
    assert m, "prism-theme link must exist"
    link_tag = m.group(0)
    assert "crossorigin" not in link_tag, \
        "prism-theme link must not have crossorigin when vendored locally"


def test_prism_theme_version_pinned():
    """The prism CSS URL must pin the version to prevent breaking changes."""
    with open("static/index.html") as f:
        src = f.read()
    m = re.search(
        r'<link[^>]*id="prism-theme"[^>]*href="([^"]*)"[^>]*>',
        src
    )
    assert m, "prism-theme link must have href"
    href = m.group(1)
    assert "1.29.0" in href, \
        f"Prism CSS version must be pinned, found href: {href}"


def test_prism_js_vendored_locally():
    """Prism JS files should be loaded from local vendor paths."""
    with open("static/index.html") as f:
        src = f.read()
    assert re.search(r'src="static/vendor/prismjs/1\.29\.0/prism-core\.min\.js"', src), \
        "prism-core.min.js should be loaded from local vendor path"
    assert re.search(r'src="static/vendor/prismjs/1\.29\.0/prism-autoloader\.min\.js"', src), \
        "prism-autoloader.min.js should be loaded from local vendor path"


def test_boot_js_set_resolved_theme_no_integrity():
    """_setResolvedTheme in boot.js must not re-apply integrity on theme switch."""
    with open("static/boot.js") as f:
        src = f.read()
    assert "_setResolvedTheme" in src, "_setResolvedTheme function must exist"
    assert not re.search(r'link\.integrity\s*=\s*["\']sha', src), \
        "_setResolvedTheme must not set link.integrity to an SRI hash"
    assert "wantIntegrity" not in src, \
        "wantIntegrity variable should be removed from _setResolvedTheme"
    assert re.search(r"link\.integrity\s*=\s*['\"]", src), \
        "_setResolvedTheme should clear link.integrity on theme switch"
