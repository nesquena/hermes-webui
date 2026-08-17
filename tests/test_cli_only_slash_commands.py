"""Regression tests for WebUI handling of Hermes CLI-only slash commands."""

import json
from pathlib import Path
import re
import subprocess
import tempfile
import textwrap
from types import SimpleNamespace

from api.commands import list_commands, _ALLOWED_AGENT_COMMANDS, _AGENT_COMMAND_ALIASES

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
COMMANDS_JS = (REPO_ROOT / "static" / "commands.js").read_text(encoding="utf-8")
MESSAGES_JS = (REPO_ROOT / "static" / "messages.js").read_text(encoding="utf-8")


def _extract_js_set_members(source: str, varname: str) -> set[str]:
    """Return the string members of a `const NAME=new Set([...])` literal."""
    m = re.search(rf"const\s+{re.escape(varname)}\s*=\s*new Set\(\[(.*?)\]\)", source, re.S)
    assert m, f"could not find const {varname}=new Set([...]) in source"
    return set(re.findall(r"'([^']*)'", m.group(1)))


def _canonical_agent_names(names: set[str]) -> set[str]:
    """Map underscore alias forms to canonical names via api/commands.py's map."""
    return {_AGENT_COMMAND_ALIASES.get(n, n) for n in names}


def _extract_busy_intercept_block() -> str:
    """Extract the busy-path slash intercept `if` block verbatim from send()."""
    marker = "Busy-control slash commands must be intercepted"
    marker_idx = MESSAGES_JS.find(marker)
    assert marker_idx != -1
    start = MESSAGES_JS.find("if(text.startsWith('/')&&!literalSlash){", marker_idx)
    assert start != -1
    depth = 0
    for i in range(start, len(MESSAGES_JS)):
        if MESSAGES_JS[i] == "{":
            depth += 1
        elif MESSAGES_JS[i] == "}":
            depth -= 1
            if depth == 0:
                return MESSAGES_JS[start : i + 1]
    raise AssertionError("unbalanced braces while extracting busy intercept block")


def test_api_commands_exposes_cli_only_metadata_for_webui_intercept():
    """CLI-only commands must remain visible so the frontend can explain them."""
    registry = [
        SimpleNamespace(
            name="browser",
            description="Attach browser tools",
            category="tools",
            aliases=["browse"],
            args_hint="connect",
            subcommands=["connect"],
            cli_only=True,
            gateway_only=False,
        )
    ]

    body = list_commands(registry)

    assert body == [
        {
            "name": "browser",
            "description": "Attach browser tools",
            "category": "tools",
            "aliases": ["browse"],
            "args_hint": "connect",
            "subcommands": ["connect"],
            "cli_only": True,
            "gateway_only": False,
        }
    ]


def test_frontend_fetches_agent_command_metadata_lazily():
    assert "async function loadAgentCommandMetadata" in COMMANDS_JS
    assert "api('/api/commands')" in COMMANDS_JS
    assert "_agentCommandCache" in COMMANDS_JS


def test_frontend_fetches_bundle_command_metadata_lazily():
    assert "async function loadBundleCommands" in COMMANDS_JS
    assert "async function getBundleCommandMetadata" in COMMANDS_JS
    assert "api('/api/commands/bundles')" in COMMANDS_JS
    assert "_bundleCommandCache" in COMMANDS_JS


def test_frontend_matches_agent_command_aliases():
    helper_idx = COMMANDS_JS.find("async function getAgentCommandMetadata")
    assert helper_idx != -1
    helper = COMMANDS_JS[helper_idx : helper_idx + 700]
    assert "cmd.aliases" in helper
    assert "some(a=>String(a||'').toLowerCase()===needle)" in helper


def test_frontend_can_execute_agent_commands_via_api_endpoint():
    assert "async function executeAgentCommand" in COMMANDS_JS
    assert "async function executeAgentPluginCommand" in COMMANDS_JS
    assert "async function _runAgentCommandTransport" in COMMANDS_JS
    assert "api('/api/commands/exec'" in COMMANDS_JS
    assert COMMANDS_JS.count("api('/api/commands/exec'") == 1


def test_cli_only_response_mentions_webui_and_cli_scope():
    assert "function cliOnlyCommandResponse" in COMMANDS_JS
    assert "Hermes CLI-only command" in COMMANDS_JS
    assert "cannot run inside the WebUI" in COMMANDS_JS


def test_browser_cli_only_response_explains_server_side_browser_tools():
    response_idx = COMMANDS_JS.find("function cliOnlyCommandResponse")
    response = COMMANDS_JS[response_idx : response_idx + 900]
    assert "if(name==='browser')" in response
    assert "configured server-side" in response
    assert "`/browser` itself only works in `hermes chat`" in response


def _run_commands_js(script_body: str) -> dict:
    script = textwrap.dedent(
        f"""
        const vm = require('vm');
        const ctx = {{
          console,
          localStorage: {{ getItem(){{return null;}}, setItem(){{}}, removeItem(){{}} }},
          t: (key) => key,
          api: async (path) => {{
            if (path === '/api/commands') return {{
              commands: [
                {{
                  name: 'pet',
                  description: 'Desktop Companion command',
                  category: 'Tools',
                  aliases: [],
                  cli_only: true,
                  gateway_only: false
                }},
                {{
                  name: 'browser',
                  description: 'Attach browser tools',
                  category: 'Tools',
                  aliases: ['browse'],
                  cli_only: true,
                  gateway_only: false
                }},
                {{
                  name: 'handoff',
                  description: 'Hand work to another agent',
                  category: 'Tools',
                  aliases: ['delegate_work'],
                  cli_only: true,
                  gateway_only: false
                }},
                {{
                  name: 'model',
                  description: 'Change model',
                  category: 'Tools',
                  aliases: [],
                  cli_only: false,
                  gateway_only: false
                }},
                {{
                  name: 'codex-runtime',
                  description: 'Toggle Codex app-server runtime',
                  category: 'Tools',
                  aliases: ['codex_runtime'],
                  cli_only: false,
                  gateway_only: false
                }},
                {{
                  name: 'reload-skills',
                  description: 'Re-scan installed skills',
                  category: 'Tools',
                  aliases: ['reload_skills'],
                  cli_only: false,
                  gateway_only: false
                }},
                {{
                  name: 'triage-review',
                  description: 'Run runtime triage review',
                  category: 'Tools',
                  aliases: ['triage_review'],
                  cli_only: false,
                  gateway_only: false
                }},
                {{
                  name: 'plugin-review',
                  description: 'Run plugin review',
                  category: 'Plugin',
                  aliases: ['plugin_review'],
                  cli_only: false,
                  gateway_only: false
                }},
                {{
                  name: 'agents',
                  description: 'Manage background agents',
                  category: 'Session',
                  aliases: ['tasks'],
                  cli_only: false,
                  gateway_only: false
                }},
                {{
                  name: 'sessions',
                  description: 'List sessions',
                  category: 'Session',
                  aliases: [],
                  cli_only: false,
                  gateway_only: false
                }},
                {{
                  name: 'resume',
                  description: 'Resume a session',
                  category: 'Session',
                  aliases: [],
                  cli_only: false,
                  gateway_only: false
                }}
              ]
            }};
            if (path === '/api/commands/bundles') return {{
              bundles: [
                {{
                  name: 'handoff',
                  description: 'Bundle collision should stay hidden behind reserved slash names',
                  skill_count: 2,
                  source: 'bundle'
                }},
                {{
                  name: 'incident-review',
                  description: 'Bundle should beat a same-slug plain skill',
                  skill_count: 3,
                  source: 'bundle'
                }},
                {{
                  name: 'triage-review',
                  description: 'Bundle collision should stay hidden behind runtime slash names',
                  skill_count: 4,
                  source: 'bundle'
                }},
                {{
                  name: 'plugin-review',
                  description: 'Bundle collision should stay hidden behind plugin slash names',
                  skill_count: 5,
                  source: 'bundle'
                }}
              ]
            }};
            if (path === '/api/skills') return {{
              skills: [
                {{
                  name: 'handoff',
                  description: 'Skill shortcut that should stay reachable via /use'
                }},
                {{
                  name: 'delegate work',
                  description: 'Alias collision should also be hidden from slash autocomplete'
                }},
                {{
                  name: 'incident review',
                  description: 'Non-colliding skills should still autocomplete'
                }},
                {{
                  name: 'triage review',
                  description: 'Runtime collisions should stay hidden from slash autocomplete'
                }},
                {{
                  name: 'plugin review',
                  description: 'Plugin collisions should stay hidden from slash autocomplete'
                }},
                {{
                  name: 'hermes-upgrade',
                  description: 'Safely upgrade and verify a Hermes installation'
                }},
                {{
                  name: 'maintenance-guide',
                  description: 'Plan a safe upgrade and rollback path'
                }},
                {{
                  name: 'reinstall-helper',
                  description: 'Recover from a failed reinstall'
                }}
              ]
            }};
            throw new Error('unexpected api path: ' + path);
          }}
        }};
        vm.createContext(ctx);
        vm.runInContext({json.dumps(COMMANDS_JS)}, ctx);
        (async () => {{
          const result = await vm.runInContext(`(async () => {{ {script_body} }})()`, ctx);
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
        proc = subprocess.run(["node", str(script_path)], check=True, capture_output=True, text=True)
    finally:
        script_path.unlink(missing_ok=True)
    return json.loads(proc.stdout)


def test_agent_command_metadata_helper_resolves_name_and_alias():
    result = _run_commands_js(
        """
        const byName = await getAgentCommandMetadata('browser');
        const byAlias = await getAgentCommandMetadata('browse');
        const unknown = await getAgentCommandMetadata('does-not-exist');
        return {
          by_name: byName && byName.name,
          by_alias: byAlias && byAlias.name,
          cli_only: byAlias && byAlias.cli_only === true,
          unknown: unknown === null
        };
        """
    )

    assert result == {
        "by_name": "browser",
        "by_alias": "browser",
        "cli_only": True,
        "unknown": True,
    }


def test_cli_only_response_helper_uses_canonical_command_name():
    result = _run_commands_js(
        """
        const meta = await getAgentCommandMetadata('browse');
        return {
          response: cliOnlyCommandResponse('browse', meta)
        };
        """
    )

    assert "`/browser` is a Hermes CLI-only command" in result["response"]
    assert "Attach browser tools" in result["response"]
    assert "configured server-side" in result["response"]


def test_bundle_command_metadata_helper_resolves_known_bundle():
    result = _run_commands_js(
        """
        const bundle = await getBundleCommandMetadata('incident-review');
        const missing = await getBundleCommandMetadata('does-not-exist');
        return {
          by_name: bundle && bundle.name,
          source: bundle && bundle.source,
          skill_count: bundle && bundle.skillCount,
          missing: missing === null
        };
        """
    )

    assert result == {
        "by_name": "incident-review",
        "source": "bundle",
        "skill_count": 3,
        "missing": True,
    }


def test_cli_only_slugs_reserve_skill_autocomplete_namespace():
    result = _run_commands_js(
        """
        await loadAgentCommandMetadata(true);
        await loadBundleCommands(true);
        await loadSkillCommands(true);
        const pet = await getSlashAutocompleteMatches('/pet');
        const browser = await getSlashAutocompleteMatches('/bro');
        const handoff = await getSlashAutocompleteMatches('/handoff');
        const delegate = await getSlashAutocompleteMatches('/delegate');
        const incident = await getSlashAutocompleteMatches('/incident');
        const triage = await getSlashAutocompleteMatches('/triage');
        const plugin = await getSlashAutocompleteMatches('/plugin');
        const skills = await getSlashAutocompleteMatches('/skills');
        const use = await getSlashAutocompleteMatches('/use');
        return {
          pet_names: pet.map(item => item.name),
          pet_sources: pet.map(item => item.source),
          pet_descs: pet.map(item => item.desc),
          browser_names: browser.map(item => item.name),
          handoff_names: handoff.map(item => item.name),
          delegate_names: delegate.map(item => item.name),
          incident_names: incident.map(item => item.name),
          incident_sources: incident.map(item => item.source),
          triage_names: triage.map(item => item.name),
          triage_sources: triage.map(item => item.source),
          plugin_names: plugin.map(item => item.name),
          plugin_sources: plugin.map(item => item.source),
          skills_names: skills.map(item => item.name),
          use_names: use.map(item => item.name)
        };
        """
    )

    assert result["pet_names"] == ["pet"]
    assert result["pet_sources"] == ["agent"]
    assert result["pet_descs"] == ["Desktop Companion command"]
    assert result["browser_names"] == []
    assert result["handoff_names"] == []
    assert result["delegate_names"] == []
    assert result["incident_names"] == ["incident-review"]
    assert result["incident_sources"] == ["bundle"]
    assert result["triage_names"] == []
    assert result["triage_sources"] == []
    assert result["plugin_names"] == ["plugin-review"]
    assert result["plugin_sources"] == ["plugin"]
    assert "skills" in result["skills_names"]
    assert "use" in result["use_names"]


def test_skill_autocomplete_matches_keyword_in_name_or_description():
    result = _run_commands_js(
        """
        await loadBundleCommands(true);
        await loadSkillCommands(true);
        const upgrade = await getSlashAutocompleteMatches('/upgrade');
        const reinstall = await getSlashAutocompleteMatches('/reinstall');
        return {
          upgrade_names: upgrade.map(item => item.name),
          upgrade_sources: upgrade.map(item => item.source),
          reinstall_names: reinstall.map(item => item.name),
          reinstall_sources: reinstall.map(item => item.source)
        };
        """
    )

    assert result["upgrade_names"] == ["hermes-upgrade", "maintenance-guide"]
    assert result["upgrade_sources"] == ["skill", "skill"]
    assert result["reinstall_names"] == ["reinstall-helper"]
    assert result["reinstall_sources"] == ["skill"]


def test_skill_autocomplete_hides_keyword_match_shadowed_by_bundle():
    result = _run_commands_js(
        """
        await loadAgentCommandMetadata(true);
        await loadBundleCommands(true);
        await loadSkillCommands(true);
        const matches = await getSlashAutocompleteMatches('/non-colliding');
        return matches.map(item => ({ name: item.name, source: item.source }));
        """
    )

    assert result == []


def test_skill_autocomplete_waits_for_bundle_metadata_before_showing_colliding_keyword_match():
    script = textwrap.dedent(
        f"""
        const vm = require('vm');
        let releaseBundles;
        const bundlesReady = new Promise(resolve => {{ releaseBundles = resolve; }});
        const ctx = {{
          console,
          localStorage: {{ getItem(){{return null;}}, setItem(){{}}, removeItem(){{}} }},
          t: (key) => key,
          api: async (path) => {{
            if (path === '/api/commands') return {{ commands: [] }};
            if (path === '/api/commands/bundles') return bundlesReady;
            if (path === '/api/skills') return {{ skills: [
              {{
                name: 'incident-review',
                description: 'Handle a non-colliding keyword',
                category: 'ops'
              }}
            ] }};
            throw new Error('unexpected api path: ' + path);
          }}
        }};
        vm.createContext(ctx);
        vm.runInContext({json.dumps(COMMANDS_JS)}, ctx);
        (async () => {{
          await vm.runInContext('loadSkillCommands(true)', ctx);
          const bundleLoad = vm.runInContext('loadBundleCommands(true)', ctx);
          const before = await vm.runInContext("getSlashAutocompleteMatches('/non-colliding')", ctx);
          releaseBundles({{ bundles: [
            {{
              name: 'incident-review',
              description: 'Incident review bundle',
              skill_count: 3,
              source: 'bundle'
            }}
          ] }});
          await bundleLoad;
          const after = await vm.runInContext("getSlashAutocompleteMatches('/non-colliding')", ctx);
          process.stdout.write(JSON.stringify({{
            before: before.map(item => ({{ name: item.name, source: item.source }})),
            after: after.map(item => ({{ name: item.name, source: item.source }}))
          }}));
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
        proc = subprocess.run(["node", str(script_path)], check=True, capture_output=True, text=True)
    finally:
        script_path.unlink(missing_ok=True)

    assert json.loads(proc.stdout) == {"before": [], "after": []}


def test_bundle_collisions_stay_hidden_until_agent_metadata_is_ready():
    script = textwrap.dedent(
        f"""
        const vm = require('vm');
        let releaseCommands;
        const commandsReady = new Promise(resolve => {{ releaseCommands = resolve; }});
        const ctx = {{
          console,
          localStorage: {{ getItem(){{return null;}}, setItem(){{}}, removeItem(){{}} }},
          t: (key) => key,
          api: async (path) => {{
            if (path === '/api/commands') return commandsReady;
            if (path === '/api/commands/bundles') return {{
              bundles: [
                {{
                  name: 'plugin-review',
                  description: 'Bundle collision should stay hidden until plugin metadata lands',
                  skill_count: 5,
                  source: 'bundle'
                }}
              ]
            }};
            if (path === '/api/skills') return {{ skills: [] }};
            throw new Error('unexpected api path: ' + path);
          }}
        }};
        vm.createContext(ctx);
        vm.runInContext({json.dumps(COMMANDS_JS)}, ctx);
        (async () => {{
          const bundleLoad = vm.runInContext('loadBundleCommands(true)', ctx);
          const before = await vm.runInContext("getSlashAutocompleteMatches('/plugin')", ctx);
          releaseCommands({{
            commands: [
              {{
                name: 'plugin-review',
                description: 'Run plugin review',
                category: 'Plugin',
                aliases: ['plugin_review'],
                cli_only: false,
                gateway_only: false
              }}
            ]
          }});
          await bundleLoad;
          const after = await vm.runInContext("getSlashAutocompleteMatches('/plugin')", ctx);
          process.stdout.write(JSON.stringify({{
            before_names: before.map(item => item.name),
            before_sources: before.map(item => item.source),
            after_names: after.map(item => item.name),
            after_sources: after.map(item => item.source)
          }}));
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
        proc = subprocess.run(["node", str(script_path)], check=True, capture_output=True, text=True)
    finally:
        script_path.unlink(missing_ok=True)

    result = json.loads(proc.stdout)
    assert result["before_names"] == []
    assert result["before_sources"] == []
    assert result["after_names"] == ["plugin-review"]
    assert result["after_sources"] == ["plugin"]


def test_send_intercepts_cli_only_commands_before_agent_round_trip():
    intercept_idx = MESSAGES_JS.find("Slash command intercept")
    assert intercept_idx != -1
    normal_send_idx = MESSAGES_JS.find("const activeSid=S.session.session_id", intercept_idx)
    assert normal_send_idx != -1
    intercept = MESSAGES_JS[intercept_idx:normal_send_idx]

    assert "await getAgentCommandMetadata(_parsedCmd.name)" in intercept
    assert "if(_agentCmd&&_agentCmd.cli_only)" in intercept
    assert "cliOnlyCommandResponse(_parsedCmd.name,_agentCmd)" in intercept
    assert "return;" in intercept


def test_send_intercepts_bundle_commands_before_agent_round_trip():
    intercept_idx = MESSAGES_JS.find("Slash command intercept")
    normal_send_idx = MESSAGES_JS.find("const activeSid=S.session.session_id", intercept_idx)
    assert normal_send_idx != -1
    intercept = MESSAGES_JS[intercept_idx:normal_send_idx]

    assert "const _bundleCmd=!_agentCmd&&typeof getBundleCommandMetadata==='function'" in intercept
    assert "await resolveBundleCommand(text,_bundleCmd)" in intercept
    assert "_slashDisplayTextOverride=text;" in intercept
    assert "text=_bundleMessage;" in intercept


def test_send_consults_agent_metadata_before_bundle_resolution():
    intercept_idx = MESSAGES_JS.find("Slash command intercept")
    normal_send_idx = MESSAGES_JS.find("const activeSid=S.session.session_id", intercept_idx)
    assert normal_send_idx != -1
    intercept = MESSAGES_JS[intercept_idx:normal_send_idx]

    agent_idx = intercept.find("await getAgentCommandMetadata(_parsedCmd.name)")
    bundle_idx = intercept.find("await getBundleCommandMetadata(_parsedCmd.name)")
    assert agent_idx != -1
    assert bundle_idx != -1
    assert agent_idx < bundle_idx


def test_send_intercepts_reload_mcp_agent_command_before_agent_round_trip():
    intercept_idx = MESSAGES_JS.find("Slash command intercept")
    normal_send_idx = MESSAGES_JS.find("const activeSid=S.session.session_id", intercept_idx)
    assert normal_send_idx != -1
    intercept = MESSAGES_JS[intercept_idx:normal_send_idx]

    assert "const _agentCmdName=String(_agentCmd&&_agentCmd.name||_parsedCmd&&_parsedCmd.name||'')" in intercept
    assert "if(_AGENT_COMMANDS_RUN_ON_WEBUI.has(_agentCmdName))" in intercept
    assert "executeAgentCommand(text,_agentCmd||{name:_agentCmdName})" in intercept


def test_reload_mcp_reload_skills_and_codex_runtime_webui_intercept_aliases_are_defined_in_js_whitelist():
    assert "'reload-mcp'" in MESSAGES_JS
    assert "'reload_mcp'" in MESSAGES_JS
    assert "'reload-skills'" in MESSAGES_JS
    assert "'reload_skills'" in MESSAGES_JS
    assert "'codex-runtime'" in MESSAGES_JS
    assert "'codex_runtime'" in MESSAGES_JS
    assert "'credits'" in MESSAGES_JS
    assert "if(_agentCmd&&_AGENT_COMMANDS_RUN_ON_WEBUI.has(_agentCmdName))" not in MESSAGES_JS


def test_reload_skills_agent_command_metadata_resolves_alias():
    result = _run_commands_js(
        """
        const byName = await getAgentCommandMetadata('reload-skills');
        const byAlias = await getAgentCommandMetadata('reload_skills');
        return {
          by_name: byName && byName.name,
          by_alias: byAlias && byAlias.name,
          cli_only: byAlias && byAlias.cli_only === true
        };
        """
    )

    assert result == {
        "by_name": "reload-skills",
        "by_alias": "reload-skills",
        "cli_only": False,
    }


def test_codex_runtime_agent_command_metadata_resolves_alias():
    result = _run_commands_js(
        """
        const byName = await getAgentCommandMetadata('codex-runtime');
        const byAlias = await getAgentCommandMetadata('codex_runtime');
        return {
          by_name: byName && byName.name,
          by_alias: byAlias && byAlias.name,
          cli_only: byAlias && byAlias.cli_only === true
        };
        """
    )

    assert result == {
        "by_name": "codex-runtime",
        "by_alias": "codex-runtime",
        "cli_only": False,
    }


def test_unknown_slash_commands_still_fall_through_to_agent():
    """Only explicitly supported metadata-backed commands should be intercepted."""
    intercept_idx = MESSAGES_JS.find("Slash command intercept")
    normal_send_idx = MESSAGES_JS.find("const activeSid=S.session.session_id", intercept_idx)
    intercept = MESSAGES_JS[intercept_idx:normal_send_idx]

    assert "if(_bundleCmd){" in intercept
    assert "if(_agentCmd&&_agentCmd.cli_only)" in intercept
    assert "if(_AGENT_COMMANDS_RUN_ON_WEBUI.has(_agentCmdName))" in intercept
    assert "if(_agentCmd&&_agentCmd.category==='Plugin')" in intercept
    assert "if(_parsedCmd&&!_cmd)" in intercept
    assert "if(!_agentCmd" not in intercept
    assert "if(_agentCmd){" not in intercept
    assert "else" not in intercept[intercept.find("if(_agentCmd&&_agentCmd.cli_only)") :]


def test_builtin_command_opt_outs_do_not_hit_agent_metadata_lookup():
    """Built-in fall-through commands like /reasoning high keep their old path."""
    intercept_idx = MESSAGES_JS.find("Slash command intercept")
    normal_send_idx = MESSAGES_JS.find("const activeSid=S.session.session_id", intercept_idx)
    intercept = MESSAGES_JS[intercept_idx:normal_send_idx]
    optout_idx = intercept.find("if(_cmd.fn(_parsedCmd.args)===false)")
    metadata_idx = intercept.find("await getAgentCommandMetadata(_parsedCmd.name)")

    assert optout_idx != -1
    assert metadata_idx != -1
    assert "if(_parsedCmd&&!_cmd)" in intercept[optout_idx:metadata_idx + 120]


# ── #6951: autocomplete must only announce commands the WebUI dispatches ─────


def test_non_dispatchable_agent_command_hidden_from_autocomplete():
    """#6951: registry commands the WebUI does not dispatch (e.g. /agents) must
    not be advertised, since submitting them would fall through to plain chat."""
    result = _run_commands_js(
        """
        await loadAgentCommandMetadata(true);
        const agents = await getSlashAutocompleteMatches('/agents');
        const ag = await getSlashAutocompleteMatches('/ag');
        return {
          agents_names: agents.map(item => item.name),
          ag_names: ag.map(item => item.name)
        };
        """
    )
    assert result["agents_names"] == []
    assert result["ag_names"] == []


def test_dispatchable_agent_commands_stay_in_autocomplete():
    """#6951: commands the WebUI does dispatch -- backend exec allowlist and
    native WebUI behaviors -- must keep appearing in autocomplete."""
    result = _run_commands_js(
        """
        await loadAgentCommandMetadata(true);
        const reload = await getSlashAutocompleteMatches('/reload');
        const sessions = await getSlashAutocompleteMatches('/sessions');
        const resume = await getSlashAutocompleteMatches('/resume');
        const plugin = await getSlashAutocompleteMatches('/plugin');
        return {
          reload_names: reload.map(item => item.name),
          reload_sources: reload.map(item => item.source),
          sessions_names: sessions.map(item => item.name),
          resume_names: resume.map(item => item.name),
          plugin_names: plugin.map(item => item.name)
        };
        """
    )
    assert result["reload_names"] == ["reload-skills"]
    assert result["reload_sources"] == ["agent"]
    assert result["sessions_names"] == ["sessions"]
    assert result["resume_names"] == ["resume"]
    assert result["plugin_names"] == ["plugin-review"]


def test_autocomplete_allowlist_is_exact_parity_with_dispatchers():
    """#6951 (re-gate): the announced allowlist must be in exact agreement with
    BOTH real dispatch authorities -- api/commands.py's _ALLOWED_AGENT_COMMANDS
    (the /api/commands/exec allowlist) and messages.js's
    _AGENT_COMMANDS_RUN_ON_WEBUI (the send() dispatch set). This compares the
    actual parsed sets, so drift in ANY authority fails the test: a command
    added to one allowlist but not the others, a renamed alias, or a removed
    entry all break parity here."""
    announced = _extract_js_set_members(COMMANDS_JS, "_WEBUI_DISPATCHABLE_AGENT_COMMANDS")
    dispatched = _extract_js_set_members(MESSAGES_JS, "_AGENT_COMMANDS_RUN_ON_WEBUI")

    # The backend-exec family must agree exactly across all three authorities
    # (canonical names after alias normalization).
    backend_allowed = set(_ALLOWED_AGENT_COMMANDS)
    assert _canonical_agent_names(dispatched) == backend_allowed
    assert _canonical_agent_names(announced) & backend_allowed == backend_allowed

    # Announced backend-exec commands are exactly the backend-exec allowlist
    # (no announced exec command outside /api/commands/exec, none missing).
    announced_backend = _canonical_agent_names(announced) & backend_allowed
    assert announced_backend == backend_allowed

    # Every remaining announced command must be a native WebUI behavior that
    # send() handles without an agent round-trip (moa/sessions/resume/pet).
    native = announced - announced_backend
    assert native == {"moa", "sessions", "resume", "pet"}

    # The plugin transport is parity by rule, not by list: the filter accepts
    # category==='Plugin' and the dispatcher routes the exact same value.
    assert "category==='Plugin'" in COMMANDS_JS
    assert "_agentCmd.category==='Plugin'" in MESSAGES_JS


def test_busy_path_intercepts_stop_before_mode_routing():
    """#6951: while busy, /stop must cancel the active run immediately instead
    of being steered/queued as the literal text '/stop'."""
    busy_idx = MESSAGES_JS.find("Busy-control slash commands must be intercepted")
    assert busy_idx != -1
    mode_idx = MESSAGES_JS.find("const defaultMessageMode=", busy_idx)
    assert mode_idx != -1
    busy_block = MESSAGES_JS[busy_idx:mode_idx]
    assert "['steer','interrupt','queue','terminal','goal','yolo','stop']" in busy_block
    assert "cmdStop" in busy_block or "COMMANDS.find(c=>c.name===_pc.name)" in busy_block


def _run_production_autocomplete_js(commands_payload: list[dict], script_body: str) -> dict:
    """Run the REAL getMatchingCommands/autocomplete pipeline from commands.js
    against a /api/commands payload (defaults to the real hermes_cli registry
    serialized exactly as list_commands() produces it)."""
    script = textwrap.dedent(
        f"""
        const vm = require('vm');
        const ctx = {{
          console,
          localStorage: {{ getItem(){{return null;}}, setItem(){{}}, removeItem(){{}} }},
          t: (key) => key,
          api: async (path) => {{
            if (path === '/api/commands') return {{ commands: {json.dumps(commands_payload)} }};
            if (path === '/api/commands/bundles') return {{ bundles: [] }};
            if (path === '/api/skills') return {{ skills: [] }};
            throw new Error('unexpected api path: ' + path);
          }}
        }};
        vm.createContext(ctx);
        vm.runInContext({json.dumps(COMMANDS_JS)}, ctx);
        (async () => {{
          const result = await vm.runInContext(`(async () => {{
            {script_body}
          }})()`, ctx);
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
        proc = subprocess.run(["node", str(script_path)], check=True, capture_output=True, text=True)
    finally:
        script_path.unlink(missing_ok=True)
    return json.loads(proc.stdout)


def _real_registry_payload() -> list[dict] | None:
    """Serialize the real hermes_cli COMMAND_REGISTRY via the production
    list_commands() path. Returns None when hermes_cli is unavailable."""
    try:
        return list_commands()
    except Exception:
        return None


def test_real_registry_announced_commands_are_all_dispatchable():
    """#6951 (re-gate): with the REAL hermes_cli registry as /api/commands
    payload, every command the production autocomplete announces must be
    dispatchable -- backend-exec via /api/commands/exec, a native WebUI
    behavior, or a plugin command. /agents (in the registry, NOT dispatched)
    must not be announced. This is the announced <= dispatched invariant
    exercised against the real production data path."""
    payload = _real_registry_payload()
    if payload is None:
        pytest.skip("hermes_cli registry unavailable")
    announced_by_prefix = {}
    for cmd in payload:
        name = str(cmd.get("name") or "")
        if not name:
            continue
        result = _run_production_autocomplete_js(
            payload,
            f"""
            await loadAgentCommandMetadata(true);
            const matches = await getSlashAutocompleteMatches('/{name}');
            return matches.map(item => ({{ name: item.name, source: item.source }}));
            """,
        )
        announced_by_prefix[name] = result

    backend_allowed = set(_ALLOWED_AGENT_COMMANDS)
    for name, rows in announced_by_prefix.items():
        for row in rows:
            if row["source"] not in ("agent", "plugin"):
                continue  # builtin/subarg/skill rows are out of scope
            assert row["name"] in backend_allowed or row["name"] in (
                "moa",
                "sessions",
                "resume",
                "pet",
            ), f"{row['name']} announced but not dispatchable"

    # /agents is in the real registry but must not be announced (it is neither
    # backend-exec nor a native WebUI behavior nor a plugin).
    assert announced_by_prefix.get("agents", []) == [], announced_by_prefix.get("agents")


def test_alias_typed_text_reaches_exec_dispatch():
    """#6951 (re-gate): typing an underscore alias (/reload_skills) must
    resolve through getAgentCommandMetadata to the canonical name, which is
    what the dispatcher allowlist tests -- so alias forms dispatch without
    needing their own autocomplete row."""
    result = _run_commands_js(
        """
        const byAlias = await getAgentCommandMetadata('reload_skills');
        const canonical = byAlias && byAlias.name;
        return { canonical, dispatched: canonical === 'reload-skills' };
        """
    )
    assert result["canonical"] == "reload-skills"
    assert result["dispatched"] is True


def _run_busy_intercept_js(script_body: str) -> dict:
    """Execute the REAL busy-path slash intercept block from messages.js with
    the REAL COMMANDS table + parseCommand from commands.js. This exercises the
    production routing branch -- not just its source text.

    The harness defines `async function busyIntercept(text, literalSlash)` in
    the vm context (so it closes over the real COMMANDS/parseCommand) and the
    script_body then calls it. The block's inner `return;` exits run() early
    when the command is intercepted, so `intercepted: true` is returned."""
    block = _extract_busy_intercept_block()
    script = textwrap.dedent(
        f"""
        const vm = require('vm');
        const calls = [];
        const ctx = {{
          console,
          localStorage: {{ getItem(){{return null;}}, setItem(){{}}, removeItem(){{}} }},
          t: (key) => key,
          S: {{
            busy: true,
            activeStreamId: 'stream-1',
            session: {{ session_id: 'sess-1' }},
            pendingFiles: []
          }},
          cancelStream: async (reason) => {{ calls.push('cancelStream:' + reason); return true; }},
          showToast: () => {{}},
          $: () => ({{ value: '' }}),
          autoResize: () => {{}},
          api: async () => ({{}}),
          _trySteer: async () => {{ calls.push('steer'); }},
          queueSessionMessage: () => {{ calls.push('queue'); }}
        }};
        vm.createContext(ctx);
        vm.runInContext({json.dumps(COMMANDS_JS)}, ctx);
        vm.runInContext(`async function busyIntercept(text, literalSlash){{
          let reachedModeRouting = false;
          const run = async () => {{
            {block}
            reachedModeRouting = true;
          }};
          await run();
          return {{ intercepted: !reachedModeRouting }};
        }}`, ctx);
        (async () => {{
          const result = await vm.runInContext(`(async () => {{
            {script_body}
          }})()`, ctx);
          process.stdout.write(JSON.stringify({{ result, calls }}));
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
        proc = subprocess.run(["node", str(script_path)], check=True, capture_output=True, text=True)
    finally:
        script_path.unlink(missing_ok=True)
    return json.loads(proc.stdout)


def test_busy_stop_executes_real_cancel_branch():
    """#6951 (re-gate): executing the real busy-intercept branch from messages.js
    with the real COMMANDS table must route /stop to cmdStop -> cancelStream,
    and must NOT fall through to steer/queue routing."""
    out = _run_busy_intercept_js(
        """
        const r1 = await busyIntercept('/stop', false);
        const r2 = await busyIntercept('/agents', false);
        return { stop: r1, agents: r2 };
        """
    )
    # /stop is intercepted by the busy branch (cmdStop -> cancelStream ran,
    # mode routing never reached), /agents is NOT a busy-control command so it
    # falls through to mode routing (documented: it is no longer announced).
    assert out["result"]["stop"] == {"intercepted": True}
    assert out["result"]["agents"] == {"intercepted": False}
    assert any(c.startswith("cancelStream:slash-stop") for c in out["calls"]), out["calls"]
    assert not any(c.startswith("steer") for c in out["calls"]), out["calls"]
