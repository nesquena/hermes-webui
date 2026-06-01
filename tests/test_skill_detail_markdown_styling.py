"""Skills detail markdown must use the shared preview-md styling pipeline."""

from pathlib import Path


PANELS_JS = Path("static/panels.js").read_text(encoding="utf-8")


def _function_block(name: str) -> str:
    marker = f"function {name}("
    start = PANELS_JS.find(marker)
    assert start != -1, f"{name}() not found"
    params_end = PANELS_JS.find("){", start)
    assert params_end != -1, f"{name}() body not found"
    brace = params_end + 1
    depth = 0
    for idx in range(brace, len(PANELS_JS)):
        ch = PANELS_JS[idx]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return PANELS_JS[start : idx + 1]
    raise AssertionError(f"{name}() body did not close")


def test_skill_detail_wraps_markdown_in_preview_md():
    block = _function_block("_renderSkillDetail")
    assert "_skillMarkdownHtml(" in block, "Skill detail must render through the shared markdown wrapper"
    assert "renderMd(markdownBody" not in block.replace(" ", ""), (
        "Skill detail must not inject raw renderMd() output without preview-md styling"
    )


def test_skill_markdown_helper_uses_preview_md():
    block = _function_block("_skillMarkdownHtml")
    assert 'class="preview-md"' in block.replace("'", '"')
    assert "renderMd(" in block


def test_skill_detail_enhances_markdown_after_render():
    detail_block = _function_block("_renderSkillDetail")
    assert "_enhanceSkillMarkdown(body)" in detail_block

    enhance_block = _function_block("_enhanceSkillMarkdown")
    assert "highlightCode" in enhance_block
    assert "renderKatexBlocks" in enhance_block


def test_open_skill_file_markdown_uses_preview_md():
    block = _function_block("openSkillFile")
    assert "_skillMarkdownHtml(" in block
    assert "if (isMd) _enhanceSkillMarkdown(body)" in block
