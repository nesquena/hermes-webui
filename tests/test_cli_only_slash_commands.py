"""Regression tests for WebUI handling of Hermes CLI-only slash commands."""

import json
from pathlib import Path
import subprocess
import tempfile
import textwrap
from types import SimpleNamespace

from api.commands import list_commands


REPO_ROOT = Path(__file__).resolve().parents[1]
COMMANDS_JS = (REPO_ROOT / "static" / "commands.js").read_text(encoding="utf-8")
MESSAGES_JS = (REPO_ROOT / "static" / "messages.js").read_text(encoding="utf-8")


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
    assert result["triage_names"] == ["triage-review"]
    assert result["triage_sources"] == ["agent"]
    assert result["plugin_names"] == ["plugin-review"]
    assert result["plugin_sources"] == ["plugin"]
    assert "skills" in result["skills_names"]
    assert "use" in result["use_names"]


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
