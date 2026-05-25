"""
Sprint 47 tests: skill-backed slash commands appear in the Web UI autocomplete.

Covers:
- commands.js lazily loads /api/skills for slash autocomplete
- built-in commands still win over skill name collisions
- boot.js primes the async skill load when typing '/'
- the dropdown marks skill-backed entries visually
"""
import pathlib


REPO_ROOT = pathlib.Path(__file__).parent.parent
COMMANDS_JS = (REPO_ROOT / "static" / "commands.js").read_text(encoding="utf-8")
SKILLS_JS = (REPO_ROOT / "static" / "skills.js").read_text(encoding="utf-8")
BOOT_JS = (REPO_ROOT / "static" / "boot.js").read_text(encoding="utf-8")
STYLE_CSS = (REPO_ROOT / "static" / "style.css").read_text(encoding="utf-8")


def test_skill_commands_are_loaded_from_shared_registry_for_autocomplete():
    assert "loadSkillCommands" in COMMANDS_JS
    assert "loadSkillRegistry" in COMMANDS_JS
    assert "getSkillAutocompleteEntries()" in COMMANDS_JS
    assert "source: 'skill'" in SKILLS_JS
    assert "api('/api/skills')" not in COMMANDS_JS


def test_builtin_commands_take_precedence_over_skill_slug_collisions():
    assert "!COMMANDS.some(c=>c.name===entry.name)" in COMMANDS_JS, \
        "Built-in commands must block skill slug collisions"


def test_typing_slash_primes_async_skill_command_loading():
    assert "ensureSkillCommandsLoadedForAutocomplete" in BOOT_JS
    assert "ensureSkillCommandsLoadedForAutocomplete();" in BOOT_JS


def test_dropdown_has_visual_badge_for_skill_backed_entries():
    assert "cmd-item-badge-skill" in STYLE_CSS
    assert "slash_skill_badge" in COMMANDS_JS
