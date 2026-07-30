"""Regression tests for #3433: chat composer path completion for ~/ tokens."""

import json
import pathlib
import shutil
import subprocess
import textwrap

import pytest


REPO_ROOT = pathlib.Path(__file__).parent.parent
COMMANDS_JS = (REPO_ROOT / "static" / "commands.js").read_text(encoding="utf-8")
BOOT_JS = (REPO_ROOT / "static" / "boot.js").read_text(encoding="utf-8")
STYLE_CSS = (REPO_ROOT / "static" / "style.css").read_text(encoding="utf-8")
NODE = shutil.which("node")


def _run_commands_js(script_body: str) -> dict:
    script = textwrap.dedent(
        f"""
        const vm = require('vm');
        const ctx = {{
          console,
          URL,
          URLSearchParams,
          localStorage: {{ getItem(){{return null;}}, setItem(){{}}, removeItem(){{}} }},
          t: (key) => key,
          api: async (path) => {{
            const url = new URL('http://hermes.local' + path);
            if (url.pathname !== '/api/workspaces/suggest') {{
              throw new Error('unexpected api path: ' + path);
            }}
            const prefix = url.searchParams.get('prefix');
            return {{
              suggestions: prefix === '~/' ? ['~', '~/Documents', '~/Projects'] : []
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
    proc = subprocess.run(
        [NODE, "-e", script],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(proc.stdout)


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_composer_path_token_matches_tilde_path_inside_message():
    result = _run_commands_js(
        """
        return {
          token: _findComposerPathToken('please inspect ~/Doc', 20),
          slash: _findComposerPathToken('/model gpt', 10),
          bareTilde: _findComposerPathToken('please inspect ~', 16)
        };
        """
    )

    assert result["token"] == {"start": 15, "end": 20, "prefix": "~/Doc"}
    assert result["slash"] is None
    assert result["bareTilde"] is None


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_composer_path_autocomplete_uses_workspace_suggest_endpoint():
    result = _run_commands_js(
        """
        const matches = await getComposerPathAutocompleteMatches('please inspect ~/', 17);
        return {
          count: matches.length,
          first: matches[0],
          second: matches[1]
        };
        """
    )

    assert result["count"] == 2
    assert result["first"]["source"] == "path"
    assert result["first"]["value"] == "~/Documents"
    assert result["first"]["tokenStart"] == 15
    assert result["first"]["tokenEnd"] == 17
    assert result["second"]["value"] == "~/Projects"


def test_composer_input_uses_path_autocomplete_after_slash_branch():
    assert "const cursor=$('msg').selectionStart;" in BOOT_JS
    assert "getComposerPathAutocompleteMatches(text,cursor).then(matches=>" in BOOT_JS
    assert "ta.value!==text||ta.selectionStart!==cursor" in BOOT_JS


def test_dropdown_selection_replaces_only_path_token():
    assert "const isPath=c.source==='path';" in COMMANDS_JS
    assert "tokenStart:token.start" in COMMANDS_JS
    assert "tokenEnd:token.end" in COMMANDS_JS
    assert "current.slice(0,safeStart)+nextPath+current.slice(safeEnd)" in COMMANDS_JS
    assert "ta.setSelectionRange(pos,pos);" in COMMANDS_JS
    assert "ta.dispatchEvent(new Event('input',{bubbles:true}));" in COMMANDS_JS


def test_path_suggestions_have_distinct_dropdown_style():
    assert ".cmd-item-path-value" in STYLE_CSS
