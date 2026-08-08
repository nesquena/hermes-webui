"""Pichot-local regression for the /validation persistent-goal shortcut."""

import json
from pathlib import Path
import subprocess
import tempfile
import textwrap


REPO_ROOT = Path(__file__).resolve().parents[1]
COMMANDS_JS = (REPO_ROOT / "static" / "commands.js").read_text(encoding="utf-8")


def _run_validation_command() -> dict:
    script = textwrap.dedent(
        f"""
        const vm = require('vm');
        const calls = [];
        const ctx = {{
          console,
          calls,
          localStorage: {{ getItem(){{return null;}}, setItem(){{}}, removeItem(){{}} }},
          t: (key) => key,
          api: async (path, options={{}}) => {{
            calls.push({{path, options}});
            if (path === '/api/goal') return {{message: 'goal accepted'}};
            throw new Error('unexpected api path: ' + path);
          }},
          S: {{
            session: {{
              session_id: 'sid-validation',
              workspace: '/workspace',
              model: 'model-id',
              model_provider: 'provider-id'
            }},
            activeProfile: 'irmella-agent',
            messages: []
          }},
          $: () => null,
          renderMessages: () => {{}},
          showToast: () => {{}},
          newSession: async () => {{}},
          renderSessionList: async () => {{}},
        }};
        vm.createContext(ctx);
        vm.runInContext({json.dumps(COMMANDS_JS)}, ctx);
        (async () => {{
          const result = await vm.runInContext(`(async () => {{
            const command = COMMANDS.find(item => item.name === 'validation');
            if (!command) return {{found: false}};
            await command.fn('');
            return {{
              found: true,
              no_echo: command.noEcho === true,
              calls,
            }};
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
        proc = subprocess.run(
            ["node", str(script_path)],
            check=True,
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
        )
    finally:
        script_path.unlink(missing_ok=True)
    return json.loads(proc.stdout)


def test_validation_command_starts_one_persistent_goal_for_any_active_profile():
    result = _run_validation_command()

    assert result["found"] is True
    assert result["no_echo"] is False
    assert len(result["calls"]) == 1
    call = result["calls"][0]
    assert call["path"] == "/api/goal"
    assert call["options"]["method"] == "POST"

    payload = json.loads(call["options"]["body"])
    assert payload["session_id"] == "sid-validation"
    assert payload["profile"] == "irmella-agent"
    assert payload["workspace"] == "/workspace"
    assert payload["model"] == "model-id"
    assert payload["model_provider"] == "provider-id"
    assert payload["args"].startswith("/validation")
    assert "livraison live vérifiée" in payload["args"]
    assert "aucune tâche, délégation, revue, intégration, publication" in payload["args"]
    assert "stop when:" in payload["args"]
