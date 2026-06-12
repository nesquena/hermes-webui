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
BOOT_JS = (REPO_ROOT / "static" / "boot.js").read_text(encoding="utf-8")
PANELS_JS = (REPO_ROOT / "static" / "panels.js").read_text(encoding="utf-8")
STYLE_CSS = (REPO_ROOT / "static" / "style.css").read_text(encoding="utf-8")


def _function_body(source, name):
    marker = f"function {name}("
    start = source.find(marker)
    assert start != -1, f"{name} function not found"
    next_function = source.find("\nfunction ", start + len(marker))
    next_async_function = source.find("\nasync function ", start + len(marker))
    ends = [pos for pos in (next_function, next_async_function) if pos != -1]
    end = min(ends) if ends else len(source)
    return source[start:end]


def test_skill_commands_are_loaded_from_api_skills_for_autocomplete():
    assert "loadSkillCommands" in COMMANDS_JS
    assert "api('/api/skills')" in COMMANDS_JS
    assert "source:'skill'" in COMMANDS_JS


def test_use_command_declares_skills_subargs():
    assert "{name:'use'" in COMMANDS_JS
    assert "subArgs:'skills'" in COMMANDS_JS


def test_skills_subargs_route_through_dedicated_loader():
    assert "async function _loadSlashSkillSubArgs(force=false)" in COMMANDS_JS
    assert "if(spec==='skills') return _loadSlashSkillSubArgs();" in COMMANDS_JS
    assert "if(spec==='skills') return loadSkillCommands();" not in COMMANDS_JS


def test_skill_mutations_invalidate_slash_skill_caches():
    assert "function invalidateSlashSkillCaches()" in COMMANDS_JS
    assert "window.invalidateSlashSkillCaches=invalidateSlashSkillCaches;" in COMMANDS_JS
    for function_name in ("saveSkillForm", "deleteCurrentSkill", "toggleSkill"):
        assert "window.invalidateSlashSkillCaches()" in _function_body(PANELS_JS, function_name)


def test_builtin_commands_take_precedence_over_skill_slug_collisions():
    assert "_getReservedSlashCommandSlugs" in COMMANDS_JS
    assert "if(_getReservedSlashCommandSlugs().has(slug)) return null;" in COMMANDS_JS
    assert "if(!skill.name.startsWith(q)||seen.has(skill.name)||reserved.has(skill.name))continue;" in COMMANDS_JS


def test_typing_slash_primes_async_skill_command_loading():
    assert "ensureSkillCommandsLoadedForAutocomplete" in BOOT_JS
    assert "ensureSkillCommandsLoadedForAutocomplete();" in BOOT_JS


def test_dropdown_has_visual_badge_for_skill_backed_entries():
    assert "cmd-item-badge-skill" in STYLE_CSS
    assert "slash_skill_badge" in COMMANDS_JS
