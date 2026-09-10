"""Issue #7509: disabled skills must not be offered by the slash-command picker.

``skills.disabled`` entries are already excluded from the backend skill-command
map (``scan_skill_commands`` in the hermes-agent runtime), so the WebUI picker is
the surface that leaks them. This suite runs the real ``static/commands.js``
inside a ``vm`` context with a mocked ``api()`` and asserts on
``getSlashAutocompleteMatches()`` -- the entry point the composer calls -- for
both surfaces reported in the issue:

* skill suggestions (``/partial``)
* ``/use`` sub-args (``/use partial``)

State space covered: 0 / 1 / many skills, enabled / disabled / flag-absent
entries, both surfaces, and a cache-refresh cycle after a skill is disabled.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
COMMANDS_JS = (ROOT / "static" / "commands.js").read_text(encoding="utf-8")

# `window` is intentionally absent, so commands.js keeps its module scope local.
_PRELUDE = """
const skillsOffered = (matches) => matches.filter((m) => m && m.source === 'skill').map((m) => m.name).sort();
const subArgsOffered = (matches) => matches.map((m) => String(m && m.value || '')).sort();
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
          // Test-only knob, same idiom as the other commands.js harnesses: lets the
          // in-context script swap the mocked /api/skills payload mid-run.
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


MIXED_SKILLS = [
    {"name": "gamma-live", "description": "Enabled skill", "disabled": False},
    {"name": "beta-gone", "description": "Disabled skill", "disabled": True},
    {"name": "delta-gone", "description": "Disabled skill", "disabled": True},
    {"name": "alpha-legacy", "description": "Entry without a disabled flag"},
]

ALL_DISABLED_SKILLS = [
    {"name": "solo-one", "description": "Disabled skill", "disabled": True},
    {"name": "solo-two", "description": "Disabled skill", "disabled": True},
]


def test_disabled_skills_are_not_offered_as_skill_commands():
    result = _run_commands_js(
        """
        await loadPicker();
        return {
          enabled: skillsOffered(await getSlashAutocompleteMatches('/gam')),
          disabled_beta: skillsOffered(await getSlashAutocompleteMatches('/bet')),
          disabled_delta: skillsOffered(await getSlashAutocompleteMatches('/del')),
          flag_absent: skillsOffered(await getSlashAutocompleteMatches('/alp')),
        };
        """,
        MIXED_SKILLS,
    )

    assert result["enabled"] == ["gamma-live"], result
    assert result["disabled_beta"] == [], result
    assert result["disabled_delta"] == [], result
    assert result["flag_absent"] == ["alpha-legacy"], result


def test_disabled_skills_are_not_offered_as_use_sub_args():
    result = _run_commands_js(
        """
        await loadPicker();
        return {
          all: subArgsOffered(await getSlashAutocompleteMatches('/use ')),
          disabled_prefix: subArgsOffered(await getSlashAutocompleteMatches('/use bet')),
          enabled_prefix: subArgsOffered(await getSlashAutocompleteMatches('/use gam')),
        };
        """,
        MIXED_SKILLS,
    )

    assert result["all"] == ["alpha-legacy", "gamma-live"], result
    assert result["disabled_prefix"] == [], result
    assert result["enabled_prefix"] == ["gamma-live"], result


def test_picker_stays_empty_when_every_skill_is_disabled():
    result = _run_commands_js(
        """
        await loadPicker();
        return {
          commands: skillsOffered(await getSlashAutocompleteMatches('/so')),
          sub_args: subArgsOffered(await getSlashAutocompleteMatches('/use ')),
        };
        """,
        ALL_DISABLED_SKILLS,
    )

    assert result == {"commands": [], "sub_args": []}, result


def test_picker_handles_an_empty_skill_list():
    result = _run_commands_js(
        """
        await loadPicker();
        return {
          commands: skillsOffered(await getSlashAutocompleteMatches('/an')),
          sub_args: subArgsOffered(await getSlashAutocompleteMatches('/use ')),
        };
        """,
        [],
    )

    assert result == {"commands": [], "sub_args": []}, result


def test_newly_disabled_skill_disappears_after_cache_refresh():
    result = _run_commands_js(
        """
        await loadPicker();
        const before = {
          commands: skillsOffered(await getSlashAutocompleteMatches('/ze')),
          sub_args: subArgsOffered(await getSlashAutocompleteMatches('/use ')),
        };
        __setSkills([{ name: 'zeta-live', description: 'Disabled later', disabled: true }]);
        invalidateSlashSkillCaches();
        await loadPicker();
        const after = {
          commands: skillsOffered(await getSlashAutocompleteMatches('/ze')),
          sub_args: subArgsOffered(await getSlashAutocompleteMatches('/use ')),
        };
        return { before, after };
        """,
        [{"name": "zeta-live", "description": "Enabled skill"}],
    )

    assert result["before"] == {"commands": ["zeta-live"], "sub_args": ["zeta-live"]}, result
    assert result["after"] == {"commands": [], "sub_args": []}, result
