"""Tests for registered skill-chip highlighting and composer preview contracts."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_skill_registry_is_single_api_skills_source():
    """Skill identity should come from one /api/skills registry, not autocomplete cache ownership."""
    skills = read("static/skills.js")
    commands = read("static/commands.js")
    ui = read("static/ui.js")

    assert "async function loadSkillRegistry" in skills
    assert "await api('/api/skills')" in skills
    assert "function getSkillByMentionToken" in skills
    assert "function getSkillAutocompleteEntries" in skills
    assert "window.loadSkillRegistry" in skills
    assert "getSkillAutocompleteEntries()" in commands
    assert "await api('/api/skills')" not in commands
    assert "_skillCommandCache" not in ui


def test_postprocess_invokes_skill_highlighter():
    """postProcessRenderedMessages should invoke registered skill highlighting."""
    ui = read("static/ui.js")
    assert "highlightSkillsInMessages(container);" in ui


def test_skill_highlighter_supports_requested_token_forms():
    """Matcher should support slash, bare, and inline-code skill mentions."""
    skills = read("static/skills.js")

    assert "`\\/?([A-Za-z0-9][A-Za-z0-9_-]*)`" in skills
    assert "\\/?([A-Za-z0-9][A-Za-z0-9_-]*)" in skills
    assert "const matchedText = m[2] || '';" in skills
    assert "const skillName = m[3] || m[4] || '';" in skills


def test_skill_chip_truncates_slash_and_code_markers():
    """Chip label should strip presentation markers from slash/code forms."""
    skills = read("static/skills.js")
    assert "chip.textContent = skill.name;" in skills
    assert "chip.textContent = matchedText;" not in skills


def test_inline_code_wrapper_is_replaced_by_skill_chip():
    """Inline code skill mentions should not leave a <code> wrapper around the chip."""
    skills = read("static/skills.js")
    assert "const codeParent = nearestInlineSkillMentionCodeParent(node);" in skills
    assert "codeParent.parentNode.replaceChild(chip, codeParent);" in skills


def test_skill_highlighter_allows_inline_code_but_skips_blocks_and_links():
    """Inline backtick-rendered code can be chipped, but blocks/links remain protected."""
    skills = read("static/skills.js")
    skip_line = "const SKILL_MENTION_SKIP_TAGS = new Set(['PRE', 'A', 'SCRIPT', 'STYLE', 'TEXTAREA', 'INPUT', 'BUTTON']);"
    assert skip_line in skills
    assert "'CODE'" not in skip_line


def test_skill_chip_styles_present():
    """CSS must provide visual chip style and composer preview style."""
    css = read("static/style.css")
    assert ".skill-chip{" in css, "Missing .skill-chip rule"
    assert ".skill-chip:hover{" in css, "Missing .skill-chip:hover rule"
    assert ".composer-skill-overlay{" in css, "Missing composer skill overlay rule"


def test_composer_remains_native_textarea_contract():
    """The chat composer must remain a native textarea with no contenteditable shim."""
    html = read("static/index.html")
    ui = read("static/ui.js")
    css = read("static/style.css")

    assert '<textarea id="msg"' in html
    assert 'contenteditable="true"' not in html
    assert 'id="msg" class="composer-editor"' not in html
    assert "Object.defineProperty(el,'value'" not in ui
    assert "Object.defineProperty(el, 'value'" not in ui
    assert "setSelectionRange=function" not in ui
    assert "composer-editor" not in ui
    assert "#msg.composer-editor" not in css
    assert "textarea#msg" in css


def test_workspace_drop_can_use_textarea_selection_api():
    """Workspace path drops should still target native textarea selection APIs safely."""
    panels = read("static/panels.js")
    assert "msgEl.selectionStart" in panels
    assert "msgEl.selectionEnd" in panels
    assert "msgEl.selectionStart=msgEl.selectionEnd" in panels


def test_composer_skill_overlay_uses_whitespace_completed_mentions():
    """Composer chips are inline overlay rendering for completed mentions, not editor replacements."""
    html = read("static/index.html")
    skills = read("static/skills.js")
    boot = read("static/boot.js")
    commands = read("static/commands.js")
    css = read("static/style.css")

    assert 'id="composerSkillOverlay"' in html
    assert "function updateComposerSkillPreview" in skills
    assert "function renderComposerSkillOverlay" in skills
    assert "composer-overlay-token" in skills
    assert "composer-overlay-token-raw" in skills
    assert "position:absolute" in css
    assert "visibility:hidden" in css
    assert "function findCompletedComposerSkillMentions" in skills
    assert "COMPOSER_SKILL_TOKEN_RE" in skills
    assert "(?=\\s)" in skills
    assert "opts && opts.force" in skills
    assert "updateComposerSkillPreview();" in boot
    assert "updateComposerSkillPreview({force:true});" in commands
    assert "replaceChild(textNode,chip)" not in skills


def test_composer_preview_avoids_prefix_overlap_by_waiting_for_whitespace():
    """Prefix-overlapping skills should not mismatch because unfinished tokens are ignored."""
    skills = read("static/skills.js")
    assert "COMPOSER_SKILL_TOKEN_RE" in skills
    assert "(?=\\s)" in skills
    assert "startsWith(slug)" not in skills
    assert "startsWith(slug)" not in read("static/ui.js")
