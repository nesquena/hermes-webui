"""Regression coverage for large MCP tool inventories in Settings → System."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX_HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
PANELS_JS = (ROOT / "static" / "panels.js").read_text(encoding="utf-8")
STYLE_CSS = (ROOT / "static" / "style.css").read_text(encoding="utf-8")
CHANGELOG = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
I18N_JS = (ROOT / "static" / "i18n.js").read_text(encoding="utf-8")


def test_mcp_tool_list_has_summary_list_and_pager_mounts():
    assert 'id="mcpToolToolbar"' in INDEX_HTML
    assert 'aria-live="polite"' in INDEX_HTML
    assert 'id="mcpToolList" class="mcp-tool-list"' in INDEX_HTML
    assert 'id="mcpToolPager"' in INDEX_HTML
    assert 'aria-label="MCP tools pagination"' in INDEX_HTML
    assert 'data-i18n-aria-label="mcp_tools_pagination_label"' in INDEX_HTML


def test_mcp_tool_rendering_is_paginated_not_full_list_rendered():
    assert "let _mcpToolsPageSize=5" in PANELS_JS
    assert "const MCP_TOOLS_PAGE_SIZE_OPTIONS=[5,10,20,40]" in PANELS_JS
    assert "filtered.slice((_mcpToolsPage-1)*_mcpToolsPageSize,_mcpToolsPage*_mcpToolsPageSize)" in PANELS_JS
    assert "list.innerHTML=visible.map(tool=>" in PANELS_JS
    assert "list.innerHTML=filtered.map(tool=>" not in PANELS_JS


def test_mcp_tool_page_size_selector_persists_and_resets_to_first_page():
    assert "function setMcpToolsPageSize(size){" in PANELS_JS
    assert "const MCP_TOOLS_PAGE_SIZE_STORAGE_KEY='hermes-webui-mcp-tools-page-size'" in PANELS_JS
    assert "if(!MCP_TOOLS_PAGE_SIZE_OPTIONS.includes(next)) return;" in PANELS_JS
    assert "_mcpToolsPageSize=next;\n  _persistMcpToolsPageSize(next);\n  _mcpToolsPage=1;" in PANELS_JS
    assert "function _restoreMcpToolsPageSize(){" in PANELS_JS
    assert "_restoreMcpToolsPageSize();\n  const list=$('mcpToolList');" in PANELS_JS
    assert "mcp_tools_per_page_aria" in PANELS_JS


def test_mcp_tool_page_setter_clamps_public_onclick_input():
    assert "function setMcpToolsPage(page){" in PANELS_JS
    assert "const next=Number(page);\n  if(!Number.isFinite(next)) return;" in PANELS_JS
    assert "_mcpToolsPage=Math.max(1,Math.floor(next));" in PANELS_JS


def test_mcp_tool_search_respects_selected_page_size():
    assert "const filtered=_filterMcpToolsForSearch(tools, query);" in PANELS_JS
    assert "const pages=Math.max(1,Math.ceil(filtered.length/_mcpToolsPageSize));" in PANELS_JS
    assert "mcp_tools_summary_showing" in PANELS_JS
    assert "t('mcp_tools_summary_showing',start,end,filtered,searchNote,totalNote,page,pages)" in PANELS_JS
    assert "mcp_tools_summary_no_matches" in PANELS_JS


def test_mcp_tool_search_resets_to_first_page_and_page_changes_scroll_top():
    assert "function setMcpToolsPage(page){" in PANELS_JS
    assert "function filterMcpTools(){\n  _mcpToolsPage=1;" in PANELS_JS
    search_block = PANELS_JS.split("function filterMcpTools(){", 1)[1].split("function loadMcpTools(){", 1)[0]
    assert "if(list) list.scrollTop=0;" in search_block


def test_mcp_tool_empty_state_mentions_inactive_configured_servers():
    assert "let _mcpToolsMeta={}" in PANELS_JS
    assert "mcp_tools_inactive_configured_servers" in PANELS_JS
    assert "_mcpToolsMeta=r||{};" in PANELS_JS


def test_mcp_tool_list_is_bounded_scroll_region_with_pager_chrome():
    assert ".mcp-tool-list{max-height:min(52vh,560px);overflow:auto" in STYLE_CSS
    assert "scrollbar-gutter:stable" in STYLE_CSS
    assert ".mcp-tool-pager{display:flex" in STYLE_CSS
    assert ".mcp-tool-page-btn" in STYLE_CSS
    assert ".mcp-tool-page-size" in STYLE_CSS


def test_mcp_tool_pagination_strings_are_i18n_backed():
    for key in [
        "mcp_tools_summary_no_matches",
        "mcp_tools_summary_none",
        "mcp_tools_summary_matching",
        "mcp_tools_summary_total_note",
        "mcp_tools_summary_showing",
        "mcp_tools_page_size_prefix",
        "mcp_tools_page_size_suffix",
        "mcp_tools_per_page_aria",
        "mcp_tools_inactive_configured_servers",
        "mcp_tools_pagination_label",
        "mcp_tools_previous_page",
        "mcp_tools_previous_page_aria",
        "mcp_tools_next_page",
        "mcp_tools_next_page_aria",
    ]:
        assert f"{key}:" in I18N_JS


def test_changelog_mentions_large_mcp_tool_inventory_fix():
    assert "large MCP tool inventories" in CHANGELOG
    assert "5-item default pages" in CHANGELOG
    assert "remembered per-page selector up to 40 tools" in CHANGELOG
