"""Regression tests for Markdown table cell spacing."""
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
STYLE_CSS = (REPO_ROOT / "static" / "style.css").read_text(encoding="utf-8")


def test_table_cell_paragraph_margins_are_reset():
    """Paragraphs inserted inside Markdown table cells should not add extra row height."""
    assert ".msg-body td p,.msg-body th p{margin:0;}" in STYLE_CSS


def test_table_cell_paragraph_reset_follows_global_message_paragraph_rule():
    """The table-specific reset must override the generic message paragraph spacing rule."""
    generic_rule = ".msg-body p{margin-bottom:10px;}"
    table_reset = ".msg-body td p,.msg-body th p{margin:0;}"

    assert generic_rule in STYLE_CSS
    assert STYLE_CSS.index(generic_rule) < STYLE_CSS.index(table_reset)
