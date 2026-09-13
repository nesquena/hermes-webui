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

Profile-switch coverage (#7509 follow-up): a skill that is disabled in the outgoing
profile and enabled in the incoming one must become visible on the next picker pass,
including when the outgoing profile's ``/api/skills`` reply is still in flight while
the switch lands -- that stale reply must not repopulate the caches.
"""

from __future__ import annotations

import json
import re
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
        // In-flight control: __holdSkills(true) parks every /api/skills reply until
        // __releaseHeldSkills(), so a test can land a profile switch mid-request.
        let skillRequestCount = 0;
        let holdSkills = false;
        const heldSkills = [];
        const ctx = {{
          console,
          localStorage: {{ getItem(){{return null;}}, setItem(){{}}, removeItem(){{}} }},
          t: (key) => key,
          api: async (path) => {{
            if (path === '/api/skills') {{
              skillRequestCount++;
              // Snapshot at request time: a reply produced for the outgoing profile
              // must not be able to read a payload that only exists after the switch.
              const snapshot = skillsPayload;
              if (holdSkills) {{
                await new Promise((resolve) => {{ heldSkills.push(resolve); }});
              }}
              return {{ skills: snapshot }};
            }}
            if (path === '/api/commands') return {{ commands: [] }};
            if (path === '/api/commands/bundles') return {{ bundles: [] }};
            throw new Error('unexpected api path: ' + path);
          }},
          // Test-only knob, same idiom as the other commands.js harnesses: lets the
          // in-context script swap the mocked /api/skills payload mid-run.
          __setSkills: (next) => {{ skillsPayload = next; }},
          __holdSkills: (on) => {{ holdSkills = !!on; }},
          __releaseHeldSkills: () => {{ heldSkills.splice(0).forEach((resolve) => resolve()); }},
          __skillRequests: () => skillRequestCount
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

# A payload shaped like the one the outgoing profile served: the skill the incoming
# profile enables is present here, flagged disabled, so a stale cache entry (or a
# stale in-flight reply) keeps it hidden.
_DISABLED_SHARED_SKILL = [
    {"name": "shared-skill", "description": "Disabled in the outgoing profile", "disabled": True},
]
_ENABLED_SHARED_SKILL = [
    {"name": "shared-skill", "description": "Enabled in the incoming profile"},
]


def test_skill_enabled_in_new_profile_appears_in_both_surfaces_after_switch():
    result = _run_commands_js(
        """
        await loadPicker();
        const before = {
          commands: skillsOffered(await getSlashAutocompleteMatches('/sh')),
          sub_args: subArgsOffered(await getSlashAutocompleteMatches('/use ')),
        };
        // The profile switch drops the caches (invalidateSlashSkillCaches) and
        // nothing forces a reload afterwards, so the picker's own non-forced load
        // is what has to serve the incoming profile's payload.
        __setSkills([{ name: 'shared-skill', description: 'Enabled in the incoming profile' }]);
        invalidateSlashSkillCaches();
        await loadSkillCommands();
        const after = {
          commands: skillsOffered(await getSlashAutocompleteMatches('/sh')),
          sub_args: subArgsOffered(await getSlashAutocompleteMatches('/use ')),
        };
        return { before, after };
        """,
        _DISABLED_SHARED_SKILL,
    )

    assert result["before"] == {"commands": [], "sub_args": []}, result
    assert result["after"] == {"commands": ["shared-skill"], "sub_args": ["shared-skill"]}, result


def test_inflight_skills_reply_cannot_repopulate_caches_after_switch():
    result = _run_commands_js(
        """
        await loadPicker();
        const before = {
          commands: skillsOffered(await getSlashAutocompleteMatches('/sh')),
          sub_args: subArgsOffered(await getSlashAutocompleteMatches('/use ')),
        };
        // The caches are empty (as they are after the picker's own refresh), so the
        // next picker pass issues one /api/skills request per surface for the
        // outgoing profile. Park both of them, then land the switch.
        invalidateSlashSkillCaches();
        __holdSkills(true);
        const inflightCommands = loadSkillCommands();
        const inflightSubArgs = getSlashAutocompleteMatches('/use ');
        __setSkills([{ name: 'shared-skill', description: 'Enabled in the incoming profile' }]);
        invalidateSlashSkillCaches();
        __holdSkills(false);
        __releaseHeldSkills();
        await inflightCommands;
        await inflightSubArgs;
        await loadSkillCommands();
        const after = {
          commands: skillsOffered(await getSlashAutocompleteMatches('/sh')),
          sub_args: subArgsOffered(await getSlashAutocompleteMatches('/use ')),
          requests: __skillRequests(),
        };
        return { before, after };
        """,
        _DISABLED_SHARED_SKILL,
    )

    assert result["before"] == {"commands": [], "sub_args": []}, result
    assert result["after"]["commands"] == ["shared-skill"], result
    assert result["after"]["sub_args"] == ["shared-skill"], result
    # 1 from loadPicker(), 1 held reply that must be discarded, 1 fresh fetch: a
    # cache that simply stayed empty would be wrong, the picker has to reload.
    assert result["after"]["requests"] >= 3, result


PANELS_JS = (ROOT / "static" / "panels.js").read_text(encoding="utf-8")
SESSIONS_JS = (ROOT / "static" / "sessions.js").read_text(encoding="utf-8")


def _top_level_function_body(source: str, name: str) -> str:
    """Return the source of a column-0 `function name(...)` declaration."""
    match = re.search(r"^(?:async\s+)?function\s+" + re.escape(name) + r"\s*\(", source, re.MULTILINE)
    assert match, f"{name} not found"
    end = source.find("\n}\n", match.start())
    assert end != -1, f"{name}: closing brace not found"
    return source[match.start():end + 3]


def test_both_profile_switch_paths_drop_the_slash_skill_caches():
    """The switch handlers must call the invalidation once the POST has succeeded.

    The handlers cannot be executed in isolation here (they touch most of the app
    shell), so this is a structural check: the invalidation call has to live inside
    the switch function, after the /api/profile/switch request -- dropping the
    caches before the request would let a reply from the outgoing profile commit
    once the switch is done.
    """
    for label, source, function_name in (
        ("panels.switchToProfile", PANELS_JS, "switchToProfile"),
        ("sessions._switchProfileForSessionLoad", SESSIONS_JS, "_switchProfileForSessionLoad"),
    ):
        body = _top_level_function_body(source, function_name)
        switch_call = body.find("/api/profile/switch")
        invalidate_call = body.find("invalidateSlashSkillCaches()")
        assert switch_call != -1, f"{label}: no /api/profile/switch call in {function_name}"
        assert invalidate_call != -1, (
            f"{label}: {function_name} does not drop the slash-skill caches, so the "
            "previous profile's /api/skills payload keeps hiding the new profile's skills"
        )
        assert invalidate_call > switch_call, f"{label}: caches dropped before the switch request"
