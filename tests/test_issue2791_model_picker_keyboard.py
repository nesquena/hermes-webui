"""Regression tests for issue #2791: keyboard navigation in the model picker."""
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def test_model_picker_has_arrow_and_enter_handlers():
    src = (REPO / "static/ui.js").read_text(encoding="utf-8")
    # The keydown handler must cover ArrowDown, ArrowUp, Enter, and Escape.
    # Locate the search-input handler block by anchor and assert each branch lives there.
    anchor = src.index("_si.addEventListener('input',()=>_filterModels(_si.value));")
    block = src[anchor:anchor + 2200]
    assert "'ArrowDown'" in block, "missing ArrowDown branch"
    assert "'ArrowUp'" in block, "missing ArrowUp branch"
    assert "'Enter'" in block, "missing Enter branch"
    assert "'Escape'" in block, "missing Escape branch"
    assert "is-highlighted" in block
    # Enter should invoke the highlighted row's onclick.
    assert "row.onclick" in block
    # And the handler must scroll the highlighted row into view.
    assert "scrollIntoView" in block


def test_highlighted_style_present():
    src = (REPO / "static/style.css").read_text(encoding="utf-8")
    assert ".model-opt.is-highlighted" in src, (
        "expected a CSS rule for the keyboard-highlighted model row"
    )
