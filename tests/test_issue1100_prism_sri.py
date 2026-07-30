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


def test_prism_theme_link_has_crossorigin():
    """The prism-theme link should still have crossorigin for CORS."""
    with open("static/index.html") as f:
        src = f.read()
    m = re.search(
        r'<link[^>]*id="prism-theme"[^>]*>',
        src
    )
    assert m, "prism-theme link must exist"
    link_tag = m.group(0)
    assert "crossorigin" in link_tag, \
        "prism-theme link should still have crossorigin attribute"


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
    assert "@1.29.0" in href, \
        f"Prism CSS version must be pinned, found href: {href}"


def test_prism_js_still_has_integrity():
    """Prism JS files should keep SRI — they are less affected by CDN edge issues."""
    with open("static/index.html") as f:
        src = f.read()
    # prism-core.min.js
    assert re.search(r'prism-core\.min\.js[^>]*integrity=', src), \
        "prism-core.min.js should still have integrity attribute"
    # prism-autoloader.min.js
    assert re.search(r'prism-autoloader\.min\.js[^>]*integrity=', src), \
        "prism-autoloader.min.js should still have integrity attribute"


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
