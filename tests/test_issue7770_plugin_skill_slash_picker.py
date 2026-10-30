"""#7770: plugin-qualified skill names must round-trip through the slash picker.

``static/commands.js`` builds one slash-command entry per ``/api/skills`` row.
``_skillCommandSlug()`` used to strip every character outside ``[a-z0-9-]``,
which collapsed ``superpowers:brainstorming`` into ``superpowersbrainstorming``.
The dropdown then offered an entry whose label could never be resolved back to
the qualified ``name`` the backend returns, so picking it silently failed.

This suite runs the real ``static/commands.js`` in a ``vm`` context with a
mocked ``api()`` and asserts on ``getSlashAutocompleteMatches()`` -- the entry
point the composer calls -- for both picker surfaces:

* skill suggestions (``/superpower`` and ``/brain``)
* ``/use`` sub-args (``/use superpowers``)
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
COMMANDS_JS = (ROOT / "static" / "commands.js").read_text(encoding="utf-8")

# The harness below drives static/commands.js through `node`, so skip the module
# rather than failing with FileNotFoundError on a Python-only checkout.
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")

# `window` is intentionally absent, so commands.js keeps its module scope local.
_PRELUDE = """
const skillNames = async (text) => (await getSlashAutocompleteMatches(text))
  .filter((m) => m && m.source === 'skill').map((m) => m.name);
const skillEntries = async (text) => (await getSlashAutocompleteMatches(text))
  .filter((m) => m && m.source === 'skill')
  .map((m) => ({name: m.name, skillName: m.skillName, plugin: m.plugin || ''}));
const subArgValues = async (text) => (await getSlashAutocompleteMatches(text))
  .map((m) => String(m && m.value || ''));
const loadPicker = async () => {
  await loadSkillCommands(true);
  await loadBundleCommands(true);
};
"""


def _run_commands_js(script_body: str, skills: list) -> dict:
    """Run `script_body` against static/commands.js with /api/skills mocked."""
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
          __setSkills: (next) => {{ skillsPayload = next; }}
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


PLUGIN_SKILLS = [
    {"name": "superpowers:brainstorming", "description": "Brainstorm first", "category": "plugin",
     "plugin": "superpowers", "source": "plugin", "disabled": False},
    {"name": "superpowers:writing-plans", "description": "Plan the work", "category": "plugin",
     "plugin": "superpowers", "source": "plugin", "disabled": False},
]

MIXED_SKILLS = PLUGIN_SKILLS + [
    {"name": "docker-deploy", "description": "Deploy it", "disabled": False},
]


def test_plugin_qualified_skill_keeps_colon_in_suggestion():
    """The slug must keep ':' so the dropdown label matches the qualified name."""
    result = _run_commands_js(
        """
        await loadPicker();
        return { names: await skillNames('/superpower') };
        """,
        PLUGIN_SKILLS,
    )

    assert "superpowers:brainstorming" in result["names"], result
    # The bare concatenation (old behaviour) must not be offered.
    assert "superpowersbrainstorming" not in result["names"], result


def test_namespace_prefix_is_reachable():
    """Typing the plugin namespace alone must offer its skills."""
    result = _run_commands_js(
        """
        await loadPicker();
        return { names: await skillNames('/superpowers:') };
        """,
        PLUGIN_SKILLS,
    )

    assert "superpowers:brainstorming" in result["names"], result
    assert "superpowers:writing-plans" in result["names"], result


def test_bare_skill_fragment_still_matches():
    """Matching on the bare fragment (no namespace) must keep working, so a
    user who types '/brainstorm' still finds the plugin skill."""
    result = _run_commands_js(
        """
        await loadPicker();
        return { names: await skillNames('/brainstorm') };
        """,
        PLUGIN_SKILLS,
    )

    assert "superpowers:brainstorming" in result["names"], result


def test_plugin_skill_appears_in_use_subargs():
    """`/use` must list plugin skills with the qualified identifier intact."""
    result = _run_commands_js(
        """
        await loadPicker();
        return { values: await subArgValues('/use superpowers') };
        """,
        PLUGIN_SKILLS,
    )

    assert "superpowers:brainstorming" in result["values"], result


def test_directory_skills_unaffected_by_colon_handling():
    """Plain skills must keep their previous slug behaviour (no ':' invented)."""
    result = _run_commands_js(
        """
        await loadPicker();
        return { names: await skillNames('/docker') };
        """,
        MIXED_SKILLS,
    )

    assert result["names"] == ["docker-deploy"], result


def test_plugin_entry_carries_namespace_metadata():
    """The picker entry must expose the namespace so the row can be labelled."""
    result = _run_commands_js(
        """
        await loadPicker();
        const entries = await skillEntries('/superpowers:brain');
        return { entry: entries[0] || null };
        """,
        PLUGIN_SKILLS,
    )

    entry = result["entry"]
    assert entry is not None, result
    assert entry["name"] == "superpowers:brainstorming", result
    assert entry["skillName"] == "superpowers:brainstorming", result
    assert entry["plugin"] == "superpowers", result


def test_derived_namespace_when_marker_absent():
    """A plugin skill that lacks an explicit `plugin` field (e.g. an older
    backend) still gets its namespace derived from the qualified name."""
    skills = [{"name": "other:helper", "description": "Help", "source": "plugin", "disabled": False}]
    result = _run_commands_js(
        """
        await loadPicker();
        const entries = await skillEntries('/other:');
        return { entry: entries[0] || null };
        """,
        skills,
    )

    entry = result["entry"]
    assert entry is not None, result
    assert entry["name"] == "other:helper", result
    assert entry["skillName"] == "other:helper", result
    assert entry["plugin"] == "other", result


def test_non_plugin_skill_with_colon_gets_no_namespace_label():
    """A directory skill whose name merely contains a colon is not a plugin
    skill, so it must not be mislabelled with a namespace."""
    skills = [{"name": "weird:name", "description": "Local oddity", "disabled": False}]
    result = _run_commands_js(
        """
        await loadPicker();
        const entries = await skillEntries('/weird:');
        return { entry: entries[0] || null };
        """,
        skills,
    )

    entry = result["entry"]
    assert entry is not None, result
    assert entry["name"] == "weird:name", result
    assert entry["plugin"] == "", result


def test_plugin_underscore_id_is_offered_verbatim():
    """A plugin id containing underscores must be offered with the underscores
    intact, and the hyphenated form must not be offered.

    The Agent keys plugin skills as ``/<namespace>:<skill>`` with the
    qualified name preserved (``_scan_plugin_skill_commands``), and
    ``resolve_skill_command_key`` tries that exact spelling before the
    underscore/hyphen fallback. A slugified ``my-plugin:my-skill`` therefore
    resolves to nothing and the Agent's ``skill_view`` reports it as not
    found (#7770).
    """
    skills = [
        {
            "name": "my_plugin:my_skill",
            "description": "Underscore id",
            "source": "plugin",
            "plugin": "my_plugin",
            "disabled": False,
        }
    ]
    result = _run_commands_js(
        """
        await loadPicker();
        return { names: await skillNames('/my_plugin') };
        """,
        skills,
    )

    assert "my_plugin:my_skill" in result["names"], result
    assert "my-plugin:my-skill" not in result["names"], result


def test_plugin_underscore_id_entry_carries_the_verbatim_name():
    """The offered entry (and therefore the inserted command) keeps the
    qualified spelling the Agent resolves."""
    skills = [
        {
            "name": "my_plugin:my_skill",
            "description": "Underscore id",
            "source": "plugin",
            "plugin": "my_plugin",
            "disabled": False,
        }
    ]
    result = _run_commands_js(
        """
        await loadPicker();
        const entries = await skillEntries('/my_plugin:my_skill');
        return { entry: entries[0] || null };
        """,
        skills,
    )

    entry = result["entry"]
    assert entry is not None, result
    assert entry["name"] == "my_plugin:my_skill", result
    assert entry["skillName"] == "my_plugin:my_skill", result
    assert entry["plugin"] == "my_plugin", result


def test_reserved_slash_command_still_wins():
    """Preserving ':' must not break reserved-command filtering.

    The reserved check is an exact slug match against built-in commands, so a
    skill whose slug IS a command name is dropped. ``help`` is unaffected by the
    ':' change (it never had one), which is exactly the regression this guards:
    if ':' handling broke the reserved set, this entry would start appearing.
    """
    skills = [
        {"name": "help", "description": "Squat a built-in", "disabled": False},
        {"name": "superpowers:help", "description": "Plugin's own help", "source": "plugin",
         "plugin": "superpowers", "disabled": False},
    ]
    result = _run_commands_js(
        """
        await loadPicker();
        return { names: await skillNames('/help') };
        """,
        skills,
    )

    # A skill named exactly like the built-in command is reserved and dropped.
    assert "help" not in result["names"], result
    # A namespaced plugin skill is a distinct identifier and stays available.
    assert "superpowers:help" in result["names"], result