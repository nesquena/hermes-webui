"""#5896 Greptile P2 (PR comment 4654016032): the slash picker must refresh when the
bundle request SETTLLES, including on failure, so skills that already loaded are not
left hidden.

When the skill list loads before the bundle request finishes, the skill-side refresh
runs while ``_bundleCommandLoadPromise`` is still in flight, and ``getMatchingCommands``
holds plain skills back in that window. If the bundle request then fails,
``_bundleCommandCacheReady`` stays false, so the bundle-side ``.then`` callback used to
skip ``refreshSlashCommandDropdown()`` and the picker stayed empty until the next
keystroke. The fix keys that callback on ``_bundleCommandCacheSettled`` (true on success
AND failure, current generation only).

This suite drives the real ``static/commands.js`` in a ``vm`` context with a mocked
``api()``: skills load fine, ``/api/commands/bundles`` rejects once, and we assert the
settle callback still refreshes the picker.
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


def _run(script_body: str) -> dict:
    """Run `script_body` against the real static/commands.js with a mocked api()."""
    script = textwrap.dedent(
        f"""
        const vm = require('vm');
        let failBundlesOnce = false;
        const ctx = {{
          console,
          localStorage: {{ getItem(){{return null;}}, setItem(){{}}, removeItem(){{}} }},
          t: (k) => k,
          api: async (path) => {{
            if (path === '/api/skills') return {{ skills: [{{ name: 'alpha-skill', description: 'Alpha' }}] }};
            if (path === '/api/commands') return {{ commands: [] }};
            if (path === '/api/commands/bundles') {{
              if (failBundlesOnce) {{ failBundlesOnce = false; throw new Error('simulated bundle failure'); }}
              return {{ bundles: [] }};
            }}
            throw new Error('unexpected api path: ' + path);
          }},
          __failBundlesOnce: () => {{ failBundlesOnce = true; }},
        }};
        vm.createContext(ctx);
        vm.runInContext({json.dumps(COMMANDS_JS)}, ctx);
        (async () => {{
          const result = await vm.runInContext(`(async () => {{
            const tick = async () => {{ for (let i = 0; i < 50; i++) await Promise.resolve(); }};
            {script_body}
          }})()`, ctx);
          process.stdout.write(JSON.stringify(result));
        }})().catch(err => {{ console.error(err && err.stack || err); process.exit(1); }});
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


def test_bundle_failure_still_triggers_picker_refresh():
    """A failed bundle load must still refresh the picker once it settles.

    Preload skills + agent metadata so only the bundle load runs inside
    ensureSkillCommandsLoadedForAutocomplete(); then fail the bundle request once.
    After it settles, _bundleCommandCacheReady is false but _bundleCommandCacheSettled
    is true, and refreshSlashCommandDropdown() must have been called.
    """
    result = _run(
        """
        await loadSkillCommands(true);
        await loadAgentCommandMetadata();
        let refreshCount = 0;
        refreshSlashCommandDropdown = function () { refreshCount++; };
        __failBundlesOnce();
        ensureSkillCommandsLoadedForAutocomplete();
        await tick();
        await tick();
        return {
          refreshCount,
          bundleReady: _bundleCommandCacheReady,
          bundleSettled: _bundleCommandCacheSettled,
        };
        """
    )
    assert result["bundleReady"] is False, result
    assert result["bundleSettled"] is True, result
    assert result["refreshCount"] >= 1, result


def test_bundle_settle_callback_keys_on_settled_flag():
    """Source-level guard: the loadBundleCommands().then refresh must key on
    _bundleCommandCacheSettled (true on success AND failure), not on
    _bundleCommandCacheReady (false on failure)."""
    m = re.search(r"loadBundleCommands\(\)\.then\(\(\)=>\{(.*?)\}\);", COMMANDS_JS, re.S)
    assert m, "loadBundleCommands().then(...) block not found in commands.js"
    block = m.group(1)
    assert "_bundleCommandCacheSettled" in block, block
    assert "_bundleCommandCacheReady)refreshSlashCommandDropdown" not in block, block
