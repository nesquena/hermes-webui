"""Regression check for #3845 — keyboard session navigation with J/K bindings.

The session list supports keyboard navigation using J (next) and K (previous)
to move focus through sessions without mouse interaction. Verified at the source
level so this stays fast.
"""
from pathlib import Path

REPO = Path(__file__).parent.parent
SESSIONS_JS = (REPO / "static" / "sessions.js").read_text(encoding="utf-8")


def test_navigateSession_function_exists():
    """navigateSession function is defined and exported to global scope."""
    assert "function navigateSession(dir)" in SESSIONS_JS


def test_navigateSession_calls_loadSession():
    """navigateSession calls loadSession(next) when switching sessions."""
    # Extract the navigateSession function definition
    start = SESSIONS_JS.index("function navigateSession(dir)")
    end = SESSIONS_JS.index("}", start) + 1
    func_body = SESSIONS_JS[start:end]
    assert "loadSession(next)" in func_body


def test_navigateSession_queries_session_items():
    """navigateSession queries .session-item[data-sid] elements."""
    start = SESSIONS_JS.index("function navigateSession(dir)")
    end = SESSIONS_JS.index("}", start) + 1
    func_body = SESSIONS_JS[start:end]
    assert ".session-item[data-sid]" in func_body


def test_j_k_keydown_listener_exists():
    """J/K keyboard listener is registered on document."""
    # Find the keyboard listener that checks for j and k keys
    assert "if(e.key!=='j'&&e.key!=='k')" in SESSIONS_JS


def test_j_k_listener_appears_after_navigateSession():
    """J/K listener appears after navigateSession function definition."""
    nav_start = SESSIONS_JS.index("function navigateSession(dir)")
    jk_start = SESSIONS_JS.index("if(e.key!=='j'&&e.key!=='k')", nav_start)
    assert jk_start > nav_start


def test_j_k_listener_prevents_default():
    """e.preventDefault() is called before navigateSession in J/K listener."""
    jk_start = SESSIONS_JS.index("if(e.key!=='j'&&e.key!=='k')")
    # Extract the J/K listener code block
    jk_block_end = SESSIONS_JS.index("});", jk_start)
    jk_block = SESSIONS_JS[jk_start:jk_block_end]

    assert "e.preventDefault()" in jk_block
    # Verify preventDefault comes before navigateSession call
    prevent_idx = jk_block.index("e.preventDefault()")
    nav_idx = jk_block.index("navigateSession")
    assert prevent_idx < nav_idx


def test_j_k_listener_checks_modifiers():
    """J/K listener ignores when Ctrl, Meta, or Alt modifiers are active."""
    jk_start = SESSIONS_JS.index("if(e.key!=='j'&&e.key!=='k')")
    jk_block_end = SESSIONS_JS.index("});", jk_start)
    jk_block = SESSIONS_JS[jk_start:jk_block_end]

    assert "e.ctrlKey" in jk_block
    assert "e.metaKey" in jk_block
    assert "e.altKey" in jk_block


def test_j_k_listener_checks_interactive_swipe_target():
    """J/K listener checks _isInteractiveSwipeTarget to avoid input fields."""
    jk_start = SESSIONS_JS.index("if(e.key!=='j'&&e.key!=='k')")
    jk_block_end = SESSIONS_JS.index("});", jk_start)
    jk_block = SESSIONS_JS[jk_start:jk_block_end]

    assert "_isInteractiveSwipeTarget" in jk_block
    assert "typeof _isInteractiveSwipeTarget===" in jk_block


def test_j_navigates_forward():
    """Pressing J calls navigateSession with dir=1 (forward)."""
    jk_start = SESSIONS_JS.index("if(e.key!=='j'&&e.key!=='k')")
    jk_block_end = SESSIONS_JS.index("});", jk_start)
    jk_block = SESSIONS_JS[jk_start:jk_block_end]

    assert "navigateSession(e.key==='j'?1:-1)" in jk_block


def test_k_navigates_backward():
    """Pressing K calls navigateSession with dir=-1 (backward)."""
    jk_start = SESSIONS_JS.index("if(e.key!=='j'&&e.key!=='k')")
    jk_block_end = SESSIONS_JS.index("});", jk_start)
    jk_block = SESSIONS_JS[jk_start:jk_block_end]

    # The ternary "e.key==='j'?1:-1" encodes both J and K behavior
    assert "navigateSession(e.key==='j'?1:-1)" in jk_block
