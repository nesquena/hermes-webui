"""Tests for #1103 — reasoning chip visible on page load."""
import re


def test_boot_calls_fetchReasoningChip():
    """boot.js must call fetchReasoningChip() during boot initialization."""
    with open("static/boot.js") as f:
        src = f.read()
    assert "fetchReasoningChip" in src, "fetchReasoningChip must be referenced in boot.js"
    # Must be called (not just defined)
    assert re.search(r"fetchReasoningChip\s*\(\s*\)", src), \
        "fetchReasoningChip() must be called in boot.js"


def test_boot_call_before_session_load():
    """fetchReasoningChip() should be called before session load in boot sequence."""
    with open("static/boot.js") as f:
        src = f.read()
    # Find the boot session load: "const saved=localStorage.getItem('hermes-webui-session')"
    boot_marker = "const saved=localStorage.getItem('hermes-webui-session')"
    boot_pos = src.index(boot_marker)
    fetch_pos = src.index("fetchReasoningChip()")
    # fetchReasoningChip must be called just before the saved session load
    assert fetch_pos < boot_pos, \
        "fetchReasoningChip() should be called before saved session load in boot.js"


def test_boot_call_has_typeof_guard():
    """fetchReasoningChip() call in boot.js should have a typeof guard."""
    with open("static/boot.js") as f:
        src = f.read()
    assert "typeof fetchReasoningChip" in src, \
        "fetchReasoningChip call should be guarded with typeof check"


def test_reasoning_chip_html_starts_hidden():
    """The reasoning wrap must start hidden (display:none) in HTML."""
    with open("static/index.html") as f:
        src = f.read()
    assert 'id="composerReasoningWrap"' in src, "composerReasoningWrap must exist in HTML"
    # Extract the element and check for display:none
    m = re.search(
        r'<div[^>]*id="composerReasoningWrap"[^>]*style="display:none"[^>]*>',
        src
    )
    assert m, "composerReasoningWrap must start with style='display:none'"


def test_applyReasoningChip_shows_wrap():
    """_applyReasoningChip must set wrap display to empty string (visible)."""
    with open("static/ui.js") as f:
        src = f.read()
    assert "wrap.style.display=''" in src or "wrap.style.display =''" in src, \
        "_applyReasoningChip must set wrap.style.display='' to make chip visible"


def test_fetchReasoningChip_calls_apply():
    """fetchReasoningChip must call _applyReasoningChip on success."""
    with open("static/ui.js") as f:
        src = f.read()
    # Find fetchReasoningChip function
    func_match = re.search(r"function fetchReasoningChip\(\)\{(.+?)\}", src, re.DOTALL)
    assert func_match, "fetchReasoningChip function must exist"
    func_body = func_match.group(1)
    assert "_applyReasoningChip" in func_body, \
        "fetchReasoningChip must call _applyReasoningChip"


def test_syncReasoningChip_called_on_session_load():
    """syncReasoningChip must be called when a session is rendered."""
    with open("static/ui.js") as f:
        src = f.read()
    # Should be called in the session render flow
    assert "syncReasoningChip()" in src, \
        "syncReasoningChip() must be called somewhere in ui.js"
