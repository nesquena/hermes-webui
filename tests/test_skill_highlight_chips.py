"""Tests for registered skill-chip highlighting in conversation messages."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_skill_highlighter_loads_current_skills_from_api():
    """Conversation chips should only match skills returned by /api/skills."""
    skills = read("static/skills.js")

    assert "async function loadSkillRegistry" in skills
    assert "await api('/api/skills')" in skills
    assert "function getSkillByMentionToken" in skills
    assert "window.highlightSkillsInMessages" in skills


def test_postprocess_invokes_skill_highlighter():
    """postProcessRenderedMessages should invoke registered skill highlighting."""
    ui = read("static/ui.js")
    assert "highlightSkillsInMessages(container);" in ui


def test_skill_highlighter_requires_explicit_slash_or_inline_code_mentions():
    """Conversation matcher should not chip bare prose words, case variants, or slash/path substrings."""
    skills = read("static/skills.js")

    assert "const SKILL_MENTION_TOKEN_RE = /(^|\\s)(\\/?([A-Za-z0-9][A-Za-z0-9_-]*))(?=$|\\s)/g;" in skills
    assert "const isSlashMention = matchedText.startsWith('/');" in skills
    assert "if(!isSlashMention && !codeParent) continue;" in skills
    assert "function getSkillByMentionToken(token)" in skills
    assert "const raw = String(token || '').trim().replace(/^\\//, '');" in skills
    assert "return _skillRegistry.get(raw) || null;" in skills
    assert "return getSkillBySlug(token);" not in skills


def test_skill_chip_truncates_slash_and_code_markers():
    """Conversation chip label should strip presentation markers from slash/code forms."""
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


def test_skill_chip_styles_present_for_conversation_view():
    """CSS must provide conversation chip style."""
    css = read("static/style.css")
    assert ".skill-chip{" in css, "Missing .skill-chip rule"
    assert ".skill-chip:hover{" in css, "Missing .skill-chip:hover rule"
