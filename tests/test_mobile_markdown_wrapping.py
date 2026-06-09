from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CSS = (ROOT / "static" / "style.css").read_text(encoding="utf-8")


def _mobile_code_wrap_block() -> str:
    start = CSS.index("@media(max-width:700px){")
    end = CSS.index("  .pre-header", start)
    return CSS[start:end]


def test_mobile_markdown_code_blocks_wrap_instead_of_horizontal_scroll():
    block = _mobile_code_wrap_block()

    assert ".msg-body pre,.preview-md pre{white-space:pre-wrap !important;overflow-x:hidden !important;overflow-wrap:anywhere !important;}" in block
    assert ".msg-body pre code,.preview-md pre code{white-space:inherit !important;overflow-wrap:anywhere !important;word-break:break-word !important;}" in block


def test_mobile_prism_tokens_do_not_force_horizontal_scroll():
    block = _mobile_code_wrap_block()

    assert ".msg-body pre code .token,.preview-md pre code .token{white-space:inherit !important;overflow-wrap:anywhere !important;word-break:inherit !important;}" in block


def test_mobile_diff_lines_wrap_instead_of_forcing_scroll():
    block = _mobile_code_wrap_block()

    assert ".diff-block .diff-line{white-space:pre-wrap !important;overflow-wrap:anywhere !important;word-break:break-word !important;}" in block
