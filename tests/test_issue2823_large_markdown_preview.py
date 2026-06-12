"""Regression coverage for #2823 large Markdown workspace previews."""

from pathlib import Path


WORKSPACE_JS = Path("static/workspace.js").read_text(encoding="utf-8")
CONFIG_PY = Path("api/config.py").read_text(encoding="utf-8")


def _open_file_block() -> str:
    marker = "async function openFile(path, opts={}){"
    start = WORKSPACE_JS.find(marker)
    assert start != -1, "openFile() not found in workspace.js"
    end = WORKSPACE_JS.find("\nfunction downloadFile", start)
    assert end != -1, "downloadFile() marker not found after openFile()"
    return WORKSPACE_JS[start:end]


def _markdown_branch() -> str:
    block = _open_file_block()
    start = block.find("} else if(MD_EXTS.has(ext)){")
    assert start != -1, "Markdown preview branch not found in openFile()"
    end = block.find("} else if(HTML_EXTS.has(ext)){", start)
    assert end != -1, "HTML preview branch marker not found after Markdown branch"
    return block[start:end]


def test_large_markdown_preview_limits_are_source_controlled():
    assert "MD_PREVIEW_RICH_RENDER_MAX_BYTES = 256 * 1024" in WORKSPACE_JS
    assert "MD_PREVIEW_RICH_RENDER_MAX_LINES = 5000" in WORKSPACE_JS
    assert "function shouldRenderMarkdownPreviewAsPlainText(content)" in WORKSPACE_JS


def test_backend_file_read_limit_allows_plain_text_markdown_fallback():
    assert "MAX_FILE_BYTES = 400_000" in CONFIG_PY


def test_large_markdown_force_render_affordance_exists():
    index_html = Path("static/index.html").read_text(encoding="utf-8")

    assert "btnRenderMarkdownAnyway" in index_html
    assert "Render as markdown anyway" in index_html
    assert "onclick=\"forceRenderMarkdownPreview()\"" in index_html
    assert "function forceRenderMarkdownPreview()" in WORKSPACE_JS
    assert "function setLargeMarkdownForceRenderVisible(visible)" in WORKSPACE_JS
    assert "openFile(_previewCurrentPath,{forceRichMarkdown:true})" in WORKSPACE_JS


def test_markdown_render_helper_runs_render_md_and_katex():
    marker = "function renderMarkdownPreviewContent(data){"
    start = WORKSPACE_JS.find(marker)
    assert start != -1, "renderMarkdownPreviewContent() helper not found"
    end = WORKSPACE_JS.find("\n}", start)
    assert end != -1, "renderMarkdownPreviewContent() helper end not found"
    helper = WORKSPACE_JS[start:end]

    render_pos = helper.find("$('previewMd').innerHTML=renderMd(data.content)")
    katex_pos = helper.rfind("renderKatexBlocks")
    assert "showPreview('md')" in helper
    assert render_pos != -1, "Helper must rich-render markdown"
    assert katex_pos != -1, "Helper must preserve KaTeX enhancement"
    assert katex_pos > render_pos


def test_large_markdown_fallback_sets_raw_content_before_size_gate():
    branch = _markdown_branch()
    force_pos = branch.find("forceRichMarkdown")
    raw_pos = branch.find("_previewRawContent = data.content")
    gate_pos = branch.find("!forceRichMarkdown && shouldRenderMarkdownPreviewAsPlainText(data.content)")
    fallback_pos = branch.find("showPreview('code')")
    rich_pos = branch.find("renderMarkdownPreviewContent(data)")

    assert force_pos != -1, "Markdown preview branch must support forceRichMarkdown"
    assert raw_pos != -1, "Markdown preview must retain raw text for Edit mode"
    assert gate_pos != -1, "Markdown preview gate must be bypassable by forceRichMarkdown"
    assert fallback_pos != -1, "Large Markdown preview must fall back to plain text"
    assert rich_pos != -1, "Small Markdown preview must still use rich Markdown mode"
    assert force_pos < raw_pos < gate_pos < fallback_pos < rich_pos


def test_large_markdown_fallback_uses_code_view_without_rich_render_or_katex():
    branch = _markdown_branch()
    gate_pos = branch.find("if(!forceRichMarkdown && shouldRenderMarkdownPreviewAsPlainText(data.content)){")
    fallback_end = branch.find("return;", gate_pos)
    assert gate_pos != -1 and fallback_end != -1, "Large Markdown fallback block not found"

    fallback = branch[gate_pos:fallback_end]
    compact = fallback.replace(" ", "")
    assert "$('previewCode').textContent=data.content" in compact
    assert "setLargeMarkdownForceRenderVisible(true)" in fallback
    assert "setStatus(" in fallback
    assert "renderMd(" not in fallback
    assert "renderKatexBlocks" not in fallback


def test_small_markdown_uses_shared_rich_render_helper():
    branch = _markdown_branch()
    fallback_end = branch.find("return;")
    assert fallback_end != -1, "Large Markdown fallback must return before rich rendering"

    rich = branch[fallback_end:]
    assert "renderMarkdownPreviewContent(data)" in rich


def test_force_rich_markdown_reuses_preview_raw_content_without_refetch():
    branch = _markdown_branch()
    # #3378 review (Codex): the cache reuse is guarded on _previewRawContentPath
    # (the path the cached content belongs to), NOT the tautological
    # path===_previewCurrentPath (that var was just assigned above).
    assert "forceRichMarkdown&&path===_previewRawContentPath&&_previewRawContent" in branch
    assert "? {content:_previewRawContent}" in branch
    assert "_previewRawContentPath = path" in branch
    # The old tautological guard must be gone.
    assert "path===_previewCurrentPath&&_previewRawContent" not in branch


def test_save_updates_cached_raw_content_for_force_render():
    """#3378 review (Codex): saving a markdown file from the plain-text fallback
    must refresh _previewRawContent (and its path) so a later force-render shows the
    saved text, not the stale pre-edit fetch."""
    save_idx = WORKSPACE_JS.find("await api('/api/file/save'")
    assert save_idx != -1, "file save call not found"
    save_block = WORKSPACE_JS[save_idx:save_idx + 600]
    assert "_previewRawContent = content" in save_block
    assert "_previewRawContentPath = _previewCurrentPath" in save_block


def test_force_render_blocked_while_editor_dirty_or_open():
    """#3378 review (Codex): force-render must not fire from a dirty/open editor
    (cached raw content wouldn't reflect the unsaved edit) and must require cached
    content belonging to the current file."""
    start = WORKSPACE_JS.find("function forceRenderMarkdownPreview()")
    assert start != -1, "forceRenderMarkdownPreview() not found"
    body = WORKSPACE_JS[start:start + 700]
    assert "_previewDirty" in body
    assert "previewEditArea" in body
    assert "_previewRawContentPath!==_previewCurrentPath" in body


def test_preview_mode_resets_force_render_button():
    show_marker = "function showPreview(mode){"
    start = WORKSPACE_JS.find(show_marker)
    assert start != -1, "showPreview() not found"
    end = WORKSPACE_JS.find("\nfunction updateEditBtn", start)
    assert end != -1, "updateEditBtn() marker not found after showPreview()"
    show_preview = WORKSPACE_JS[start:end]

    assert "setLargeMarkdownForceRenderVisible(false)" in show_preview
