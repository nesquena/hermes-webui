"""Regression tests for WebUI handling of Hermes CLI-only slash commands."""

import json
from pathlib import Path
import subprocess
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


def test_frontend_matches_agent_command_aliases():
    helper_idx = COMMANDS_JS.find("async function getAgentCommandMetadata")
    assert helper_idx != -1
    helper = COMMANDS_JS[helper_idx : helper_idx + 700]
    assert "cmd.aliases" in helper
    assert "some(a=>String(a||'').toLowerCase()===needle)" in helper


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
            if (path !== '/api/commands') throw new Error('unexpected api path: ' + path);
            return {{
              commands: [
                {{
                  name: 'browser',
                  description: 'Attach browser tools',
                  aliases: ['browse'],
                  cli_only: true,
                  gateway_only: false
                }},
                {{
                  name: 'model',
                  description: 'Change model',
                  aliases: [],
                  cli_only: false,
                  gateway_only: false
                }}
              ]
            }};
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
    proc = subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)
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


def test_unknown_slash_commands_still_fall_through_to_agent():
    """Only explicitly supported metadata-backed commands should be intercepted."""
    intercept_idx = MESSAGES_JS.find("Slash command intercept")
    normal_send_idx = MESSAGES_JS.find("const activeSid=S.session.session_id", intercept_idx)
    intercept = MESSAGES_JS[intercept_idx:normal_send_idx]

    assert "if(_agentCmd&&_agentCmd.cli_only)" in intercept
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
