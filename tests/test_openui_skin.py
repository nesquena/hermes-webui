"""Tests for OpenUI skin — minimal light/dark palette."""
import re

def test_openui_skin_css_block():
    """The :root[data-skin=\"openui\"] block must be present with expected tokens."""
    with open("static/style.css", encoding="utf-8") as f:
        css = f.read()

    m = re.search(r':root\[data-skin="openui"\]\s*\{([^}]+)\}', css, re.DOTALL)
    assert m, "Missing :root[data-skin=\"openui\"] block"

    block = m.group(1)
    assert "--bg:#ffffff" in block
    assert "--sidebar:#f3f4f6" in block
    assert "--accent:#10a37f" in block
    assert "--accent-text:#047857" in block
    assert "--text:#111827" in block
    assert "--font-ui:\"Segoe UI\"" in block


def test_openui_skin_dark_block():
    """The :root.dark[data-skin=\"openui\"] block must be present."""
    with open("static/style.css", encoding="utf-8") as f:
        css = f.read()

    m = re.search(r':root\.dark\[data-skin="openui"\]\s*\{([^}]+)\}', css, re.DOTALL)
    assert m, "Missing :root.dark[data-skin=\"openui\"] block"

    block = m.group(1)
    assert "--bg:#212121" in block
    assert "--sidebar:#171717" in block
    assert "--accent:#10a37f" in block
    assert "--accent-text:#19c37d" in block
    assert "color-scheme:dark" in block


def test_openui_skin_in_boot_js():
    """The _SKINS array in boot.js must include the OpenUI entry."""
    with open("static/boot.js", encoding="utf-8") as f:
        js = f.read()

    assert "'OpenUI'" in js or '"OpenUI"' in js, "Missing OpenUI entry in _SKINS"
    assert "#10a37f" in js or "#10A37F" in js, "Expected OpenUI accent color #10a37f"
    assert "#059669" in js, "Expected OpenUI hover color #059669"
    assert "#19c37d" in js or "#19C37D" in js, "Expected OpenUI dark accent #19c37d"


def test_openui_in_config_settings():
    """The _SETTINGS_SKIN_VALUES set must include 'openui'."""
    with open("api/config.py", encoding="utf-8") as f:
        py = f.read()
    assert '"openui"' in py, "Missing 'openui' in _SETTINGS_SKIN_VALUES"


def test_openui_in_index_html_inline_skins():
    """The early-init skin allowlist in index.html must include 'openui:1'."""
    with open("static/index.html", encoding="utf-8") as f:
        html = f.read()
    assert 'openui:1' in html, "Missing 'openui:1' in inline skin whitelist"
