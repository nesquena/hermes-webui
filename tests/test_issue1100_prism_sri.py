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
