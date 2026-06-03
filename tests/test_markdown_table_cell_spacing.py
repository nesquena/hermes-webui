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


def test_markdown_tables_use_scroll_wrapper_and_wide_message_body():
    """Markdown tables should scroll horizontally instead of crushing narrow columns."""
    assert ".md-table-wrap{max-width:100%;overflow-x:auto;" in STYLE_CSS
    assert ".msg-row[data-role=\"assistant\"] .msg-body:has(.md-table-wrap){max-width:100%;}" in STYLE_CSS


def test_markdown_table_first_column_resists_vertical_word_wrapping():
    """Short label columns such as Priority should not collapse into stacked letters."""
    assert ".md-table th{white-space:nowrap;}" in STYLE_CSS
    assert ".md-table th:first-child,.md-table td:first-child{white-space:nowrap;min-width:72px;width:1%;}" in STYLE_CSS
    assert ".md-table td code,.md-table th code{white-space:normal;overflow-wrap:anywhere;}" in STYLE_CSS
