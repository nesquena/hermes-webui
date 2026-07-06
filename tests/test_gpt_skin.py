"""Tests for GPT skin — ChatGPT 2025 redesign palette."""
import re

def test_gpt_skin_css_block():
    """The :root[data-skin=\"gpt\"] block must be present with expected tokens."""
    with open("static/style.css", encoding="utf-8") as f:
        css = f.read()

    m = re.search(r':root\[data-skin="gpt"\]\s*\{([^}]+)\}', css, re.DOTALL)
    assert m, "Missing :root[data-skin=\"gpt\"] block"

    block = m.group(1)
    assert "--bg:#ffffff" in block
    assert "--sidebar:#f3f4f6" in block
    assert "--accent:#10a37f" in block
    assert "--accent-text:#047857" in block
    assert "--text:#111827" in block
    assert "--font-ui:\"Segoe UI\"" in block


def test_gpt_skin_dark_block():
    """The :root.dark[data-skin=\"gpt\"] block must be present."""
    with open("static/style.css", encoding="utf-8") as f:
        css = f.read()

    m = re.search(r':root\.dark\[data-skin="gpt"\]\s*\{([^}]+)\}', css, re.DOTALL)
    assert m, "Missing :root.dark[data-skin=\"gpt\"] block"

    block = m.group(1)
    assert "--bg:#212121" in block
    assert "--sidebar:#171717" in block
    assert "--accent:#10a37f" in block
    assert "--accent-text:#19c37d" in block
    assert "color-scheme:dark" in block


def test_gpt_skin_in_boot_js():
    """The _SKINS array in boot.js must include the GPT entry."""
    with open("static/boot.js", encoding="utf-8") as f:
        js = f.read()

    assert "'GPT'" in js or '"GPT"' in js, "Missing GPT entry in _SKINS"
    assert "#10a37f" in js, "Expected GPT accent color #10a37f"
    assert "#059669" in js, "Expected GPT hover color #059669"
    assert "#19c37d" in js, "Expected GPT dark accent #19c37d"
