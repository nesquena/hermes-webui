import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _read_static(name):
    return (ROOT / "static" / name).read_text(encoding="utf-8")


def _locale_blocks():
    text = _read_static("i18n.js")
    matches = list(re.finditer(r"^  '?([A-Za-z]{2}(?:-[A-Za-z]+)?)'?: \{", text, re.M))
    assert matches, "could not find locale blocks"
    blocks = {}
    for idx, match in enumerate(matches):
        end = matches[idx + 1].start() if idx + 1 < len(matches) else text.index("\n};", match.end())
        blocks[match.group(1)] = text[match.end():end]
    return blocks


def test_markdown_table_enhancer_is_registered_and_invoked_after_render_paths():
    messages = _read_static("messages.js")

    assert "function enhanceMarkdownTables(root)" in messages
    assert "window.enhanceMarkdownTables=enhanceMarkdownTables" in messages
    assert "function _wireMarkdownTableEnhancer()" in messages
    assert "window.renderMessages=function(...args)" in messages
    assert "enhanceMarkdownTables(inner)" in messages

    smd_end = messages[messages.index("function _smdEndParser()"):messages.index("function _scheduleStreamingKatex()")]
    assert "_sanitizeSmdLinks(assistantBody);enhanceMarkdownTables(assistantBody);" in smd_end


def test_markdown_table_enhancement_is_idempotent_and_message_scoped():
    messages = _read_static("messages.js")
    helper = messages[messages.index("function enhanceMarkdownTables(root)"):messages.index("function _markdownTableText")]

    assert ".msg-body table:not([data-markdown-table-enhanced])" in helper
    assert "data-markdown-table-enhanced" in helper
    assert "table.setAttribute('data-markdown-table-enhanced','1')" in helper
    assert ".csv-table-wrap" in helper


def test_markdown_table_sorting_uses_accessible_buttons_and_stable_rows():
    messages = _read_static("messages.js")
    helper = messages[messages.index("function enhanceMarkdownTables(root)"):messages.index("function _markdownTableText")]

    assert "document.createElement('button')" in helper
    assert "button.type='button'" in helper
    assert "const columnName=_markdownTableText(cell.textContent)||String(colIdx+1)" in helper
    assert "const columnSortLabel=`${sortLabel}: ${columnName}`" in helper
    assert "button.setAttribute('aria-label',columnSortLabel)" in helper
    assert "button.title=columnSortLabel" in helper
    assert "cell.setAttribute('aria-sort','none')" in helper
    assert "other.setAttribute('aria-sort','none')" in helper
    assert "cell.setAttribute('aria-sort',nextDir==='asc'?'ascending':'descending')" in helper
    assert "row.dataset.markdownTableOriginalIndex=String(idx)" in helper
    assert "localeCompare(bv,undefined,{numeric:true,sensitivity:'base'})" in helper
    assert "return ai-bi" in helper


def test_markdown_table_filter_is_gated_to_multi_row_tables_and_preserves_rows():
    messages = _read_static("messages.js")
    helper = messages[messages.index("function enhanceMarkdownTables(root)"):messages.index("function _markdownTableText")]

    assert "if(bodyRows.length>=4&&table.parentElement)" in helper
    assert "filter.type='search'" in helper
    assert "filter.placeholder=filterLabel" in helper
    assert "filter.setAttribute('aria-label',filterLabel)" in helper
    assert "row.hidden=!!query" in helper
    assert "body.appendChild(row)" in helper


def test_markdown_table_styles_keep_controls_compact():
    style = _read_static("style.css")

    assert ".markdown-table-filter" in style
    assert "width:min(260px,100%)" in style
    assert ".markdown-table-sort{display:flex" in style
    assert "min-height:20px" in style
    assert ".msg-body th[aria-sort=\"ascending\"]" in style
    assert ".msg-body th[aria-sort=\"descending\"]" in style


def test_markdown_table_i18n_keys_are_present_in_every_locale():
    assert "zh-Hant" in _locale_blocks()
    for locale, block in _locale_blocks().items():
        keys = set(re.findall(r"^\s*([A-Za-z0-9_]+):", block, re.M))
        assert "markdown_table_filter" in keys, f"{locale} missing markdown_table_filter"
        assert "markdown_table_sort_column" in keys, f"{locale} missing markdown_table_sort_column"
