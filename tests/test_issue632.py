"""
Issue #632: slash autocomplete should suggest second-level arguments.

Covers:
- commands.js exposes a dedicated slash autocomplete parser/loader
- /model sub-args hydrate from /api/models
- /personality sub-args hydrate from /api/personalities
- /reasoning provides static low/medium/high suggestions without becoming a
  locally executed built-in command
- boot.js uses the async slash autocomplete helper while typing
"""
import pathlib


REPO_ROOT = pathlib.Path(__file__).parent.parent
COMMANDS_JS = (REPO_ROOT / "static" / "commands.js").read_text(encoding="utf-8")
BOOT_JS = (REPO_ROOT / "static" / "boot.js").read_text(encoding="utf-8")
STYLE_CSS = (REPO_ROOT / "static" / "style.css").read_text(encoding="utf-8")


def test_subarg_registry_exists_without_promoting_reasoning_to_builtin():
    assert "const SLASH_SUBARG_SOURCES=" in COMMANDS_JS
    assert "reasoning:{desc:'Set reasoning effort', subArgs:['low','medium','high']}" in COMMANDS_JS
    assert "{name:'reasoning'" not in COMMANDS_JS, \
        "/reasoning suggestions must not register as a local built-in command"


def test_model_and_personality_subargs_load_from_existing_apis():
    assert "_loadSlashModelSubArgs" in COMMANDS_JS
    assert "api('/api/models')" in COMMANDS_JS
    assert "_loadSlashPersonalitySubArgs" in COMMANDS_JS
    assert "api('/api/personalities')" in COMMANDS_JS


def test_slash_autocomplete_parses_second_level_arguments():
    assert "function _parseSlashAutocomplete" in COMMANDS_JS
    assert "return {kind:'subargs'" in COMMANDS_JS
    assert "getSlashAutocompleteMatches" in COMMANDS_JS


def test_boot_uses_async_slash_autocomplete_helper():
    assert "getSlashAutocompleteMatches(text).then(matches=>" in BOOT_JS


def test_subarg_dropdown_has_distinct_parent_and_argument_styling():
    assert ".cmd-item-parent" in STYLE_CSS
    assert ".cmd-item-subarg" in STYLE_CSS
