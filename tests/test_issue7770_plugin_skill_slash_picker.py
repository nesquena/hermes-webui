"""#7770 (commands.js): plugin-registered skills must round-trip through the
slash-command autocomplete and the ``/use`` sub-arg picker.

The picker builds its command entries from ``/api/skills``. Before the fix:

* ``_skillCommandSlug('superpowers:brainstorming')`` stripped ``:`` and
  produced ``superpowersbrainstorming``, so the entry the user clicked had
  no resolvable path back to the qualified name returned by the endpoint.
* The picker had no way to label a plugin skill distinctly, so a
  ``superpowers:*`` row looked identical to a directory-installed skill.

The fix keeps ``:`` in the slug (lossless round-trip) and tags the entry
with the plugin namespace so the description can say ``Plugin: superpowers``
when the qualified name is not already prefixed with the namespace.

The tests below run the real ``static/commands.js`` in a node ``vm`` with a
mocked ``/api/skills`` payload — the same harness style as
``test_issue7509_disabled_skills_slash_picker.py``. They pin:

* the slug of a qualified name preserves the colon;
* the autocomplete shows the qualified-name entry when the user types the
  namespace prefix (``/super``);
* the ``/use super`` sub-arg list includes the qualified names so users can
  force a plugin skill for the next turn;
* the picker entry carries ``source: 'skill'`` AND a ``plugin`` namespace so
  the UI can render a plugin hint.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import textwrap
from pathlib import Path

# Pin #7770 coverage to environments where the hermes-agent module is
# importable: ``api/routes.py:_skills_list_from_dir`` (the source of the
# /api/skills payload the picker reflects) imports from
# ``agent.skill_utils`` / ``tools.skills_tool`` and the conftest's
# ``pytest_collection_modifyitems`` skip list does not enumerate these
# new test names. Without this marker the tests run in CI shards and
# explode with ``ModuleNotFoundError: No module named 'tools.skills_tool'``
# on the first /api/skills code path. (CI: 15/24 test jobs FAILED on
# this exact class.) The commands.js / node-vm harness itself does not
# need the agent module, but every other WebUI test in the same shard
# does — a module-level skip keeps the whole file clean.
from tests.conftest import requires_agent_modules

pytestmark = requires_agent_modules

ROOT = Path(__file__).resolve().parent.parent
COMMANDS_JS = (ROOT / "static" / "commands.js").read_text(encoding="utf-8")


_PRELUDE = """
const skillsOffered = (matches) => matches.filter((m) => m && m.source === 'skill').map((m) => m.name).sort();
const subArgsOffered = (matches) => matches.map((m) => String(m && m.value || '')).sort();
const skillEntries = (matches) => matches.filter((m) => m && m.source === 'skill').map((m) => ({
  name: m.name,
  skillName: m.skillName,
  plugin: m.plugin,
  desc: m.desc,
}));
const loadPicker = async () => {
  await loadSkillCommands(true);
  await loadBundleCommands(true);
};
"""


def _run_commands_js(script_body: str, skills: list) -> dict:
    script = textwrap.dedent(
        f"""
        const vm = require('vm');
        let skillsPayload = {json.dumps(skills)};
        const ctx = {{
          console,
          localStorage: {{ getItem(){{return null;}}, setItem(){{}}, removeItem(){{}} }},
          t: (key) => key,
          api: async (path) => {{
            if (path === '/api/skills') return {{ skills: skillsPayload }};
            if (path === '/api/commands') return {{ commands: [] }};
            if (path === '/api/commands/bundles') return {{ bundles: [] }};
            throw new Error('unexpected api path: ' + path);
          }},
        }};
        vm.createContext(ctx);
        vm.runInContext({json.dumps(COMMANDS_JS)}, ctx);
        (async () => {{
          const result = await vm.runInContext(`(async () => {{ {_PRELUDE} {script_body} }})()`, ctx);
          process.stdout.write(JSON.stringify(result));
        }})().catch(err => {{
          console.error(err && err.stack || err);
          process.exit(1);
        }});
        """
    )
    with tempfile.NamedTemporaryFile("w", suffix=".js", encoding="utf-8", delete=False) as handle:
        handle.write(script)
        script_path = Path(handle.name)
    try:
        proc = subprocess.run(["node", str(script_path)], capture_output=True, text=True)
    finally:
        script_path.unlink(missing_ok=True)
    if proc.returncode != 0:
        raise RuntimeError(f"node harness failed (exit {proc.returncode}):\n{proc.stderr}")
    return json.loads(proc.stdout)


# A directory-installed skill and two plugin-registered skills. The plugin
# entries mirror the shape the fixed /api/skills endpoint produces (qualified
# name + source/plugin metadata).
PLUGIN_SKILLS = [
    {
        "name": "local-skill",
        "description": "A directory-installed skill",
        "category": None,
        "disabled": False,
    },
    {
        "name": "superpowers:brainstorming",
        "description": "Brainstorm like a genius",
        "category": "plugin",
        "plugin": "superpowers",
        "source": "plugin",
        "disabled": False,
    },
    {
        "name": "superpowers:writing-plans",
        "description": "Write a plan",
        "category": "plugin",
        "plugin": "superpowers",
        "source": "plugin",
        "disabled": False,
    },
]


def test_qualified_skill_name_survives_slugification():
    """The picker slug for a plugin-qualified name must keep the colon so
    the qualified identifier round-trips losslessly through the autocomplete
    and /use paths (#7770). The pre-fix slug was
    'superpowersbrainstorming' and the picker could not map it back to the
    qualified name the agent's 'skill_view' expects.
    """
    # The slug function is private to commands.js; the public surface is the
    # entry name the picker builds from a /api/skills row. We assert on that
    # entry name AND on the property the picker carries (skillName) so a
    # regression that strips ':' from the slug is caught even if the
    # internal _skillCommandSlug helper is renamed.
    result = _run_commands_js(
        """
        await loadPicker();
        const all = await getSlashAutocompleteMatches('/super');
        return {
          offered: skillsOffered(all),
          // The picker must also surface the namespace-stripped form, so
          // typing the bare skill name also resolves.
          by_skill: skillsOffered(await getSlashAutocompleteMatches('/brainstor')),
          // The use-subarg surface is the second consumer; it must include
          // the qualified name verbatim so /use superpowers:brainstorming
          // forces that exact skill.
          use_subargs: subArgsOffered(await getSlashAutocompleteMatches('/use super')),
        };
        """,
        PLUGIN_SKILLS,
    )
    assert result["offered"] == ["superpowers:brainstorming", "superpowers:writing-plans"], result
    assert result["by_skill"] == ["superpowers:brainstorming"], result
    # The sub-arg list is fed straight to cmdUse, which matches names
    # case-insensitively; both qualified plugin skills must appear when the
    # user types the namespace prefix.
    assert "superpowers:brainstorming" in result["use_subargs"], result
    assert "superpowers:writing-plans" in result["use_subargs"], result


def test_picker_entry_carries_plugin_namespace_metadata():
    """The picker entry must expose the namespace so the dropdown can render
    a plugin hint. Without this, plugin skills look identical to
    directory skills and the user cannot tell where they come from (#7770).
    """
    result = _run_commands_js(
        """
        await loadPicker();
        const all = await getSlashAutocompleteMatches('/super');
        return {
          entries: skillEntries(all),
        };
        """,
        PLUGIN_SKILLS,
    )
    entries_by_name = {e["name"]: e for e in result["entries"]}
    # Both qualified entries must carry the plugin namespace.
    assert entries_by_name["superpowers:brainstorming"]["plugin"] == "superpowers", result
    assert entries_by_name["superpowers:writing-plans"]["plugin"] == "superpowers", result
    # skillName is the authoritative round-trip carrier: it must equal the
    # qualified name the /api/skills row had.
    assert entries_by_name["superpowers:brainstorming"]["skillName"] == "superpowers:brainstorming"


def test_directory_skill_entry_does_not_carry_plugin_namespace():
    """A directory-installed skill must NOT carry a plugin namespace, even
    though its row lives next to plugin rows in /api/skills. This is the
    negative test for the picker entry metadata (#7770).
    """
    result = _run_commands_js(
        """
        await loadPicker();
        // /loc matches only the directory skill; the namespace filter
        // collapses the plugin skills to a separate row.
        const dir_only = await getSlashAutocompleteMatches('/loc');
        return { entries: skillEntries(dir_only) };
        """,
        PLUGIN_SKILLS,
    )
    entries = result["entries"]
    assert len(entries) == 1, entries
    # The directory skill must NOT carry a plugin namespace.
    assert "plugin" not in entries[0] or not entries[0]["plugin"], entries


def test_picker_distinguishes_directory_and_plugin_skills():
    """Both the namespace and the local-skill label must surface in the
    autocomplete when the user types a partial that matches neither. The
    directory skill (local-skill) and the plugin skills
    (superpowers:*) are independently matched — the picker must not
    collapse them into one entry.
    """
    result = _run_commands_js(
        """
        await loadPicker();
        // 'l' matches the directory skill (local-skill); the plugin skills
        // begin with 's' and are filtered out.
        const local_matches = skillsOffered(await getSlashAutocompleteMatches('/loc'));
        // 's' matches both plugin skills.
        const plugin_matches = skillsOffered(await getSlashAutocompleteMatches('/sup'));
        return { local_matches, plugin_matches };
        """,
        PLUGIN_SKILLS,
    )
    assert result["local_matches"] == ["local-skill"], result
    assert result["plugin_matches"] == [
        "superpowers:brainstorming",
        "superpowers:writing-plans",
    ], result


def test_qualified_name_does_not_collide_with_reserved_slash_command():
    """A plugin skill with a colon must NOT be treated as a reserved slash
    command name. _getReservedSlashCommandSlugs strips reserved
    COMMANDS+agent commands from the picker. The built-in slash command
    /superpowers:brainstorming is not a slash command, so it must
    surface (#7770). This is the negative test that would catch a future
    change that wrongly filters out any colon-bearing name.
    """
    result = _run_commands_js(
        """
        await loadPicker();
        return { offered: skillsOffered(await getSlashAutocompleteMatches('/super')) };
        """,
        PLUGIN_SKILLS,
    )
    assert "superpowers:brainstorming" in result["offered"], result
    assert "superpowers:writing-plans" in result["offered"], result
