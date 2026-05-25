"""Tests for message skill-chip highlighting in rendered messages."""


def test_postprocess_invokes_skill_highlighter():
    """postProcessRenderedMessages should invoke skill highlighting."""
    with open("static/ui.js", "r", encoding="utf-8") as f:
        content = f.read()
    assert "highlightSkillsInMessages(container);" in content, (
        "postProcessRenderedMessages must call highlightSkillsInMessages"
    )


def test_skill_highlighter_uses_api_skills_cache():
    """Skill chip source should be loaded from /api/skills with caching fields."""
    with open("static/ui.js", "r", encoding="utf-8") as f:
        content = f.read()

    assert "async function _loadSkillMentions()" in content
    assert "await api('/api/skills')" in content
    assert "_skillMentionCacheReady" in content
    assert "_skillMentionCacheLoadPromise" in content
    assert "if(skill && skill.disabled) continue;" in content
    assert "_SKILL_MENTION_TOKEN_RE" in content
    assert "root.textContent && root.textContent.includes('/')" not in content


def test_skill_highlighter_supports_requested_token_forms():
    """Matcher should support slash, bare, and inline-code skill mentions."""
    with open("static/ui.js", "r", encoding="utf-8") as f:
        content = f.read()

    assert "`\\/?([A-Za-z0-9][A-Za-z0-9_-]*)`" in content
    assert "\\/?([A-Za-z0-9][A-Za-z0-9_-]*)" in content
    assert "const matchedText = m[2] || '';" in content
    assert "const skillName = m[3] || m[4] || '';" in content


def test_skill_chip_truncates_slash_and_code_markers():
    """Chip label should strip presentation markers from slash/code forms."""
    with open("static/ui.js", "r", encoding="utf-8") as f:
        content = f.read()

    assert "chip.textContent = skillName;" in content
    assert "chip.textContent = matchedText;" not in content


def test_inline_code_wrapper_is_replaced_by_skill_chip():
    """Inline code skill mentions should not leave a <code> wrapper around the chip."""
    with open("static/ui.js", "r", encoding="utf-8") as f:
        content = f.read()

    assert "const codeParent = _nearestInlineSkillMentionCodeParent(node);" in content
    assert "codeParent.parentNode.replaceChild(chip, codeParent);" in content


def test_skill_highlighter_allows_inline_code_but_skips_blocks_and_links():
    """Inline backtick-rendered code can be chipped, but blocks/links remain protected."""
    with open("static/ui.js", "r", encoding="utf-8") as f:
        content = f.read()

    skip_line = "const _SKILL_MENTION_SKIP_TAGS = new Set(['PRE', 'A', 'SCRIPT', 'STYLE', 'TEXTAREA', 'INPUT', 'BUTTON']);"
    assert skip_line in content
    assert "'CODE'" not in skip_line


def test_skill_chip_styles_present():
    """CSS must provide visual chip style for highlighted skills."""
    with open("static/style.css", "r", encoding="utf-8") as f:
        css = f.read()

    assert ".skill-chip{" in css, "Missing .skill-chip rule"
    assert ".skill-chip:hover{" in css, "Missing .skill-chip:hover rule"


def test_composer_skill_chip_editor_is_wired_to_autocomplete():
    """The chat composer should render skill chips in-place while preserving #msg.value."""
    with open("static/index.html", "r", encoding="utf-8") as f:
        html = f.read()
    with open("static/ui.js", "r", encoding="utf-8") as f:
        ui = f.read()
    with open("static/commands.js", "r", encoding="utf-8") as f:
        commands = f.read()
    with open("static/style.css", "r", encoding="utf-8") as f:
        css = f.read()

    assert 'id="msg" class="composer-editor"' in html
    assert "Object.defineProperty(el,'value'" in ui
    assert "function _renderComposerSkillChips(opts={})" in ui
    assert "chip.dataset.raw=raw;" in ui
    assert "raw.slice(0,-1)" in ui
    assert "function renderComposerSkillChips(opts={})" in commands
    assert "renderComposerSkillChips();" in commands
    assert "#msg.composer-editor" in css


def test_composer_skill_chips_only_render_after_whitespace_boundary():
    """Composer skill chips should render only after whitespace confirms token completion."""
    with open("static/ui.js", "r", encoding="utf-8") as f:
        ui = f.read()
    with open("static/commands.js", "r", encoding="utf-8") as f:
        commands = f.read()

    assert "_composerSkillTokenHasWhitespaceBoundary(text,end)" in ui
    assert "if(!opts.allowPrefixExact&&!_composerSkillTokenHasWhitespaceBoundary(text,end)) continue;" in ui
    assert "String(skill.name||'').startsWith(slug)" not in ui
    assert "renderComposerSkillChips(c.source==='skill'?{allowPrefixExact:true}:{})" in commands


def test_composer_backspace_chip_reverts_before_browser_deletes_node():
    """Backspace around a chip should convert it to shortened plain text."""
    with open("static/ui.js", "r", encoding="utf-8") as f:
        ui = f.read()

    assert "const revertChipForBackspace=e=>" in ui
    assert "deleteContentBackward" in ui
    assert "chip.parentNode.replaceChild(textNode,chip);" in ui
