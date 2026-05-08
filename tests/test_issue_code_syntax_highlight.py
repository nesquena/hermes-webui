"""Regression tests for fenced code block syntax highlighting."""
from pathlib import Path

UI_JS = Path(__file__).resolve().parent.parent / "static" / "ui.js"


def _read_ui_js() -> str:
    return UI_JS.read_text()


def test_fenced_code_blocks_add_prism_language_class():
    js = _read_ui_js()
    assert 'class="language-${esc(lang)}"' in js, (
        "Fenced code blocks should add Prism language-* classes so syntax highlighting works"
    )

def test_fenced_code_blocks_keep_existing_pre_header_layout():
    js = _read_ui_js()
    # The fenced code rendering was moved into the stash callback (#1154 fix).
    # The template string now uses `lang` instead of `normalizedLang`.
    assert '${h}<pre><code${langAttr}>${esc(code.replace(/\\n$/,' in js, (
        "The syntax-highlight fix should preserve the existing fenced code block layout"
    )
    assert '<div class="code-block">' not in js, (
        "This fix should not introduce a new wrapper around fenced code blocks"
    )
