"""Regression tests for #8143: `_` and `-` are equivalent for slash-command slugs.

The agent-side resolver (`resolve_bundle_command_key`) folds the spelling, so the
WebUI must fold it at both seams or `/knowledge_loop` neither suggests nor
resolves the canonical `knowledge-loop` command and falls through to chat.
"""

import json
from pathlib import Path
import subprocess
import tempfile
import textwrap

REPO_ROOT = Path(__file__).resolve().parents[1]
COMMANDS_JS = (REPO_ROOT / "static" / "commands.js").read_text(encoding="utf-8")


def _run_commands_js(script_body: str) -> dict:
    """Drive the real static/commands.js in a node vm with stubbed IO."""
    script = textwrap.dedent(
        f"""
        const vm = require('vm');
        const calls = [];
        const ctx = {{
          console,
          localStorage: {{ getItem(){{return null;}}, setItem(){{}}, removeItem(){{}} }},
          t: (key) => key,
          api: async (path, opts) => {{
            calls.push({{path, body: opts && opts.body || null}});
            if (path === '/api/commands') return {{
              commands: [
                {{
                  name: 'incident-report',
                  description: 'Report an incident',
                  category: 'Tools',
                  // No underscore alias declared: the registry does not
                  // guarantee one, so the client cannot rely on `aliases`.
                  aliases: [],
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
                  name: 'dual-spell',
                  description: 'Registered with hyphens',
                  category: 'Plugin',
                  aliases: [],
                  cli_only: false,
                  gateway_only: false
                }},
                {{
                  name: 'dual_spell',
                  description: 'Registered with underscores',
                  category: 'Plugin',
                  aliases: [],
                  cli_only: false,
                  gateway_only: false
                }}
              ]
            }};
            if (path === '/api/commands/bundles') return {{
              bundles: [
                {{
                  name: 'knowledge-loop',
                  description: 'Bundle should be found by either separator',
                  skill_count: 3,
                  source: 'bundle'
                }},
                {{
                  name: 'retention-loop',
                  description: 'Neighbouring bundle used for prefix boundaries',
                  skill_count: 1,
                  source: 'bundle'
                }}
              ]
            }};
            if (path === '/api/skills') return {{
              skills: [
                {{ name: 'reinstall-helper', description: 'Recover from a failed reinstall' }},
                {{ name: 'knowledgebase', description: 'Neighbouring skill used for prefix boundaries' }},
                {{ name: 'incident-notes', description: 'Raw text with knowledge_loop inside' }},
                {{ name: 'hyphen-desc-only', description: 'Mentions knowledge-loop with a hyphen' }}
              ]
            }};
            if (path === '/api/commands/bundles/resolve') return {{
              message: 'bundle ran: ' + JSON.parse(opts.body).command
            }};
            throw new Error('unexpected api path: ' + path);
          }}
        }};
        vm.createContext(ctx);
        vm.runInContext({json.dumps(COMMANDS_JS)}, ctx);
        (async () => {{
          const result = await vm.runInContext(`(async () => {{ {script_body} }})()`, ctx);
          process.stdout.write(JSON.stringify({{result, calls}}));
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
            env={"PATH": "/usr/bin:/bin:/usr/local/bin", "NODE_CHANNEL_FD": ""},
        )
    finally:
        script_path.unlink(missing_ok=True)
    payload = json.loads(proc.stdout)
    return {"result": payload["result"], "calls": payload["calls"]}


# ── Seam 1: submit-time bundle metadata lookup ────────────────────────────────


def test_bundle_metadata_resolves_either_separator_spelling():
    """getBundleCommandMetadata must fold `_`/`-`; the literal `===` missed."""
    out = _run_commands_js(
        """
        await loadBundleCommands(true);
        return {
          hyphen: (await getBundleCommandMetadata('knowledge-loop') || {}).name || null,
          underscore: (await getBundleCommandMetadata('knowledge_loop') || {}).name || null,
          unknown: (await getBundleCommandMetadata('does-not-exist') || {}).name || null,
          empty: (await getBundleCommandMetadata('') || {}).name || null
        };
        """
    )

    assert out["result"] == {
        "hyphen": "knowledge-loop",
        "underscore": "knowledge-loop",
        "unknown": None,
        "empty": None,
    }


def test_bundle_metadata_folds_separator_for_underscore_named_bundle():
    """The alias works in both directions, not just hyphen-canonical bundles."""
    out = _run_commands_js(
        """
        _bundleCommandCache = [
          {name: 'incident_loop', desc: 'underscore-named bundle', source: 'bundle', skillCount: 2},
        ];
        _bundleCommandCacheReady = true;
        return {
          hyphen: (await getBundleCommandMetadata('incident-loop') || {}).name || null,
          underscore: (await getBundleCommandMetadata('incident_loop') || {}).name || null
        };
        """
    )

    assert out["result"] == {"hyphen": "incident_loop", "underscore": "incident_loop"}


def test_bundle_metadata_does_not_fold_unrelated_names():
    """Folding must be `_` <-> `-` only: `knowledgebase` is not `knowledge-loop`."""
    out = _run_commands_js(
        """
        _bundleCommandCache = [{name: 'knowledgebase', desc: 'x', source: 'bundle', skillCount: 0}];
        _bundleCommandCacheReady = true;
        return {
          hyphen: (await getBundleCommandMetadata('knowledge-loop') || {}).name || null,
          underscore: (await getBundleCommandMetadata('knowledge_loop') || {}).name || null
        };
        """
    )

    assert out["result"] == {"hyphen": None, "underscore": None}


# ── Seam 2: submit-time agent/plugin metadata lookup (Greptile P1) ────────────


def test_agent_metadata_resolves_underscore_spelling_without_declared_alias():
    """`incident-report` declares no underscore alias; the lookup must still fold.

    Without this the autocomplete suggests `/incident_report` but submit misses
    the plugin/agent intercept and the text falls through to the chat path.
    """
    out = _run_commands_js(
        """
        await loadAgentCommandMetadata(true);
        return {
          hyphen: (await getAgentCommandMetadata('incident-report') || {}).name || null,
          underscore: (await getAgentCommandMetadata('incident_report') || {}).name || null,
          unknown: (await getAgentCommandMetadata('does-not-exist') || {}).name || null
        };
        """
    )

    assert out["result"] == {
        "hyphen": "incident-report",
        "underscore": "incident-report",
        "unknown": None,
    }


def test_agent_metadata_prefers_the_literal_spelling_when_both_are_registered():
    """When both spellings exist the exact typed one wins -- no silent rewrite."""
    out = _run_commands_js(
        """
        await loadAgentCommandMetadata(true);
        return {
          typed_underscore: (await getAgentCommandMetadata('dual_spell') || {}).name || null,
          typed_hyphen: (await getAgentCommandMetadata('dual-spell') || {}).name || null
        };
        """
    )

    assert out["result"] == {"typed_underscore": "dual_spell", "typed_hyphen": "dual-spell"}


# ── Seam 3: autocomplete ─────────────────────────────────────────────────────


def test_autocomplete_finds_bundle_by_either_separator_prefix():
    out = _run_commands_js(
        """
        await loadAgentCommandMetadata(true);
        await loadBundleCommands(true);
        await loadSkillCommands(true);
        const names = async (q) => (await getSlashAutocompleteMatches(q)).map(m => m.name);
        return {
          hyphen: await names('/knowledge-'),
          underscore: await names('/knowledge_')
        };
        """
    )

    assert "knowledge-loop" in out["result"]["hyphen"]
    assert "knowledge-loop" in out["result"]["underscore"]


def test_autocomplete_alias_keeps_prefix_boundaries():
    """`/knowledge_` must not match `knowledgebase`, nor collapse to an empty prefix."""
    out = _run_commands_js(
        """
        await loadAgentCommandMetadata(true);
        await loadBundleCommands(true);
        await loadSkillCommands(true);
        const names = async (q) => (await getSlashAutocompleteMatches(q)).map(m => m.name);
        return {
          underscore: await names('/knowledge_'),
          single: await names('/k')
        };
        """
    )

    assert "knowledgebase" not in out["result"]["underscore"]
    # A one-character prefix must still be a real prefix, not a global match.
    assert "knowledge-loop" in out["result"]["single"]
    assert "retention-loop" not in out["result"]["single"]


def test_autocomplete_skill_name_matches_either_separator():
    """Skills are substring-matched, so the alias has to apply to the name half."""
    out = _run_commands_js(
        """
        await loadAgentCommandMetadata(true);
        await loadBundleCommands(true);
        await loadSkillCommands(true);
        const names = async (q) => (await getSlashAutocompleteMatches(q)).map(m => m.name);
        return {
          hyphen: await names('/reinstall-'),
          underscore: await names('/reinstall_')
        };
        """
    )

    assert "reinstall-helper" in out["result"]["hyphen"]
    assert "reinstall-helper" in out["result"]["underscore"]


def test_autocomplete_description_matching_stays_on_the_raw_query():
    """The alias applies to command names only; description search stays literal."""
    out = _run_commands_js(
        """
        await loadAgentCommandMetadata(true);
        await loadBundleCommands(true);
        await loadSkillCommands(true);
        const names = async (q) => (await getSlashAutocompleteMatches(q)).map(m => m.name);
        return {
          underscore: await names('/knowledge_loop'),
          hyphen: await names('/knowledge-loop')
        };
        """
    )

    # Raw description text still matches its own spelling...
    assert "incident-notes" in out["result"]["underscore"]
    # ...and a hyphen-only description is NOT conjured by the underscore query.
    assert "hyphen-desc-only" not in out["result"]["underscore"]
    # The canonical spelling keeps its own description match.
    assert "hyphen-desc-only" in out["result"]["hyphen"]
    # The bundle itself is found by either separator — that is the #8143 fix.
    assert "knowledge-loop" in out["result"]["underscore"]
    assert "knowledge-loop" in out["result"]["hyphen"]


def test_autocomplete_ranks_the_literal_spelling_first():
    """Enter dispatches row zero, so the typed spelling must rank first."""
    out = _run_commands_js(
        """
        await loadAgentCommandMetadata(true);
        await loadBundleCommands(true);
        await loadSkillCommands(true);
        const first = async (q) => (await getSlashAutocompleteMatches(q))[0].name;
        return {
          typed_underscore: await first('/dual_'),
          typed_hyphen: await first('/dual-')
        };
        """
    )

    assert out["result"] == {"typed_underscore": "dual_spell", "typed_hyphen": "dual-spell"}


# ── Acceptance: both spellings reach the same submit-time resolver ────────────


def test_both_spellings_reach_the_bundle_resolver_via_the_send_sequence():
    """The send path: parseCommand -> agent metadata -> bundle -> resolver.

    Reproduces the real ordering in static/messages.js so a green helper test
    alone cannot hide a fall-through to chat.
    """
    out = _run_commands_js(
        """
        await loadAgentCommandMetadata(true);
        await loadBundleCommands(true);
        const dispatched = [];
        for (const text of ['/knowledge-loop do X', '/knowledge_loop do X']) {
          const parsed = parseCommand(text);
          const agentCmd = await getAgentCommandMetadata(parsed.name);
          const bundleCmd = !agentCmd ? await getBundleCommandMetadata(parsed.name) : null;
          if (!bundleCmd) { dispatched.push(null); continue; }
          const resolved = await resolveBundleCommand(text, bundleCmd);
          dispatched.push(String(resolved.message));
        }
        // An unknown slug must NOT be intercepted: no resolver call, no rewrite.
        const unknown = parseCommand('/totally-unknown do X');
        const unknownBundle = await getBundleCommandMetadata(unknown.name);
        return { dispatched, unknownIntercepted: unknownBundle !== null };
        """
    )

    assert out["result"]["dispatched"] == [
        "bundle ran: /knowledge-loop do X",
        "bundle ran: /knowledge_loop do X",
    ]
    assert out["result"]["unknownIntercepted"] is False
    resolve_calls = [c for c in out["calls"] if c["path"] == "/api/commands/bundles/resolve"]
    assert len(resolve_calls) == 2