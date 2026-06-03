"""Regression coverage for #2823 large Markdown workspace previews."""

from pathlib import Path


WORKSPACE_JS = Path("static/workspace.js").read_text(encoding="utf-8")


def _open_file_block() -> str:
    marker = "async function openFile(path){"
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
    assert "MD_PREVIEW_RICH_RENDER_MAX_BYTES = 64 * 1024" in WORKSPACE_JS
    assert "MD_PREVIEW_RICH_RENDER_MAX_LINES = 1500" in WORKSPACE_JS
    assert "function shouldRenderMarkdownPreviewAsPlainText(content)" in WORKSPACE_JS


def test_large_markdown_fallback_sets_raw_content_before_size_gate():
    branch = _markdown_branch()
    raw_pos = branch.find("_previewRawContent = data.content")
    gate_pos = branch.find("shouldRenderMarkdownPreviewAsPlainText(data.content)")
    fallback_pos = branch.find("showPreview('code')")
    rich_pos = branch.find("showPreview('md')")

    assert raw_pos != -1, "Markdown preview must retain raw text for Edit mode"
    assert gate_pos != -1, "Markdown preview must guard rich rendering by size/line count"
    assert fallback_pos != -1, "Large Markdown preview must fall back to plain text"
    assert rich_pos != -1, "Small Markdown preview must still use rich Markdown mode"
    assert raw_pos < gate_pos < fallback_pos < rich_pos


def test_large_markdown_fallback_uses_code_view_without_rich_render_or_katex():
    branch = _markdown_branch()
    gate_pos = branch.find("if(shouldRenderMarkdownPreviewAsPlainText(data.content)){")
    fallback_end = branch.find("return;", gate_pos)
    assert gate_pos != -1 and fallback_end != -1, "Large Markdown fallback block not found"

    fallback = branch[gate_pos:fallback_end]
    compact = fallback.replace(" ", "")
    assert "$('previewCode').textContent=data.content" in compact
    assert "setStatus(" in fallback
    assert "renderMd(" not in fallback
    assert "renderKatexBlocks" not in fallback


def test_small_markdown_still_renders_and_runs_katex_after_render():
    branch = _markdown_branch()
    fallback_end = branch.find("return;")
    assert fallback_end != -1, "Large Markdown fallback must return before rich rendering"

    rich = branch[fallback_end:]
    render_pos = rich.find("$('previewMd').innerHTML=renderMd(data.content)")
    katex_pos = rich.rfind("renderKatexBlocks")
    assert render_pos != -1, "Small Markdown files must still rich-render with renderMd()"
    assert katex_pos != -1, "Small Markdown file previews must still trigger KaTeX rendering"
    assert katex_pos > render_pos
