"""Behavioural test for #9035 — exact skill-name matches must rank first.

The issue: `/api/skills` rows are cached alphabetically
(``static/commands.js`` sorts them with ``localeCompare``), and
``getMatchingCommands()`` appended every skill that matched on NAME **or**
DESCRIPTION in that same raw order. Row zero is what the dropdown preselects
(``showCmdDropdown()`` sets ``_cmdSelectedIdx=0``) and what Enter accepts
(``acceptCmdSelection()`` dispatches ``items[0]``), so typing a full skill
name could run an alphabetically-earlier neighbour whose *description* merely
mentions the typed word:

    /next  ->  context7-mcp  ("Look up Next.js docs ...")   <- wrong skill

These tests EXECUTE the real ``static/commands.js`` in a Node VM (same
shared-realm pattern as test_issue7683_slash_menu_review_fixes.py), populate
the skill cache through the real ``loadSkillCommands()`` loader — so the
production alphabetical sort is part of the fixture — and assert the whole
match order, not just the first row:

    (0) exact name, (1) name prefix, (2) name substring, (3) description only
    — ``localeCompare`` inside each band.

The inclusion criterion is deliberately NOT under test here: description
matches must stay in the list, they only drop to the last band.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
COMMANDS_JS = (REPO_ROOT / "static" / "commands.js").read_text(encoding="utf-8")

pytestmark = pytest.mark.skipif(
    subprocess.run(["which", "node"], capture_output=True).returncode != 0,
    reason="node not on PATH",
)


# The issue's probe: one skill whose NAME is exactly the typed word, one that
# extends the name, one that contains the word mid-name, and two that must
# match on their DESCRIPTION only ("Next.js", "next N patients").
_SKILLS = [
    {"name": "next", "description": "Plan the next iteration of a feature"},
    {"name": "nextgen-board", "description": "Kanban board for the next generation"},
    {"name": "csv-to-next-batch", "description": "Convert CSV rows into the next batch"},
    {"name": "context7-mcp", "description": "Look up Next.js and React docs via Context7"},
    {"name": "load-synthea-data", "description": "Load the next N synthetic patients"},
]


def _run(script_body: str) -> dict:
    """Run the REAL static/commands.js in a Node VM and return the JSON the
    body produces. ``script_body`` runs inside an async IIFE with every
    commands.js binding in scope."""
    script = textwrap.dedent(
        """
        const vm = require('vm');
        const makeEl = () => ({
          innerHTML: '', textContent: '', value: '',
          dataset: {}, style: {},
          classList: { add() {}, remove() {}, contains() { return false; } },
          appendChild() {}, addEventListener() {}, removeEventListener() {},
          setAttribute() {}, remove() {}, focus() {}, blur() {},
          setSelectionRange() {}, scrollIntoView() {},
          dispatchEvent() {},
        });
        const ctx = {
          console, Math, JSON, Object, Array, String, Number, Boolean,
          RegExp, Error, Promise, Set, Map, Date,
          setTimeout: () => 0, clearTimeout: () => 0,
          setInterval: () => 0, clearInterval: () => 0,
          t: key => key,
          localStorage: { getItem() { return null; }, setItem() {}, removeItem() {} },
          navigator: { userAgent: 'node' },
          window: { addEventListener() {}, requestAnimationFrame(cb) { return 1; } },
          document: {
            addEventListener() {}, removeEventListener() {},
            getElementById() { return null; },
            querySelector() { return null; },
            querySelectorAll() { return []; },
            createElement() { return makeEl(); },
            body: { appendChild() {}, classList: { add() {}, remove() {} } },
          },
        };
        ctx.globalThis = ctx;
        ctx.window.window = ctx.window;
        // page-level globals commands.js borrows from ui.js/index.html
        ctx.$ = (id) => ctx.document.getElementById(id);
        ctx.esc = (s) => String(s ?? '').replace(/[&<>"']/g,
          c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
        ctx.Event = function (type, opts) { this.type = type; Object.assign(this, opts); };
        ctx.api = async (path) => {
          if (path === '/api/skills') return { skills: __SKILLS__ };
          if (path === '/api/commands') return { commands: [] };
          if (path === '/api/commands/bundles') return { bundles: [] };
          throw new Error('unexpected api path: ' + path);
        };
        vm.createContext(ctx);
        vm.runInContext(__COMMANDS_JS__, ctx);
        (async () => {
          const result = await vm.runInContext(`(async () => {
            __BODY__
          })()`, ctx);
          process.stdout.write(JSON.stringify(result));
        })().catch(err => {
          console.error(err && err.stack || err);
          process.exit(1);
        });
        """
    )
    script = (
        script.replace("__SKILLS__", json.dumps(_SKILLS))
        .replace("__COMMANDS_JS__", json.dumps(COMMANDS_JS))
        .replace("__BODY__", textwrap.dedent(script_body))
    )
    with tempfile.NamedTemporaryFile("w", suffix=".js", encoding="utf-8", delete=False) as fh:
        fh.write(script)
        path = Path(fh.name)
    try:
        proc = subprocess.run(["node", str(path)], capture_output=True, text=True, timeout=60)
    finally:
        path.unlink(missing_ok=True)
    if proc.returncode != 0:
        raise RuntimeError(f"node VM failed: {proc.stderr}")
    return json.loads(proc.stdout)


# Loads the cache through the REAL loader, so the alphabetical sort at
# static/commands.js:2766 is part of the fixture rather than test setup, and
# marks the bundle gate settled the way a successful /api/commands/bundles
# fetch does. Everything that follows is the unmodified production pipeline.
_PRELUDE = """
await loadSkillCommands(true);
_bundleCommandCacheReady = true;
_bundleCommandCache = [];
_agentCommandCache = null;
"""


def test_exact_skill_name_is_row_zero_and_bands_are_ordered():
    """#9035 (CORE): `/next` must rank `next` first, then the name-prefix row,
    then the name-substring row, and only then the description-only rows —
    alphabetically inside each band.

    Before the fix the list was plain cache order, so row zero (what Enter
    runs) was `context7-mcp`, a skill whose *description* mentions Next.js.
    """
    out = _run(
        _PRELUDE
        + """
        return { names: getMatchingCommands('next').map(m => m.name) };
        """
    )
    assert out["names"] == [
        "next",              # 0: exact name
        "nextgen-board",     # 1: name prefix
        "csv-to-next-batch", # 2: name substring
        "context7-mcp",      # 3: description only ("Next.js ..."), alphabetical
        "load-synthea-data", # 3: description only ("... next N ..."), alphabetical
    ], (
        "the dropdown ranked matches by nothing but /api/skills alphabetical "
        f"order; got {out['names']!r} (#9035)"
    )


def test_description_matches_are_still_included_but_last():
    """#9035: the fix is ranking only — the inclusion criterion stays put.

    Both description-only skills must still be offered (a user searching for
    "Next.js" should find context7-mcp), they just cannot outrank a name hit.
    """
    out = _run(
        _PRELUDE
        + """
        return { names: getMatchingCommands('next').map(m => m.name) };
        """
    )
    for name in ("context7-mcp", "load-synthea-data"):
        assert name in out["names"], (
            f"{name} matches on description and must stay in the list (#9035); "
            f"got {out['names']!r}"
        )
    assert out["names"].index("context7-mcp") > out["names"].index("csv-to-next-batch")
    assert out["names"].index("load-synthea-data") > out["names"].index("csv-to-next-batch")


def test_enter_accepts_the_typed_skill_not_a_description_neighbour():
    """#9035: row zero is what Enter runs. Drives the real dropdown selection
    (``showCmdDropdown`` preselects index 0, ``selectCmdDropdownItem`` — the
    Enter handler — dispatches the selected row) and asserts the composer
    receives `/next`.
    """
    out = _run(
        _PRELUDE
        + """
        const ddItems = [];
        const dd = { innerHTML: '', classList: { add() {}, remove() {} },
                     appendChild(el) { ddItems.push(el); },
                     querySelectorAll() { return ddItems; },
                     scrollTop: 0, style: {} };
        const ta = { value: '/next', selectionStart: 5, selectionEnd: 5,
                     focus() {}, setSelectionRange() {}, dispatchEvent() {} };
        document.getElementById = (id) => (id === 'cmdDropdown' ? dd : ta);
        const matches = getMatchingCommands('next');
        showCmdDropdown(matches);
        const selectedIdx = _cmdSelectedIdx;
        selectCmdDropdownItem();
        return { selectedIdx, composer: ta.value, first: matches[0] && matches[0].name };
        """
    )
    assert out["first"] == "next", (
        f"row zero must be the exact name the user typed, got {out['first']!r} (#9035)"
    )
    assert out["selectedIdx"] == 0, (
        f"the dropdown must preselect row zero, got {out['selectedIdx']!r}"
    )
    assert out["composer"] == "/next", (
        "Enter/accept must insert the typed skill, not a description neighbour; "
        f"composer got {out['composer']!r} (#9035)"
    )


def test_band_order_also_applies_to_bundle_and_agent_rows():
    """#9035: the ranking is applied to the assembled list, so exact/prefix
    bands hold for bundle rows too — not just for plain skills."""
    out = _run(
        """
        _agentCommandCache = null;
        _skillCommandCache = [];
        _bundleCommandCacheReady = true;
        _bundleCommandCache = [
          { name: 'graph-explorer-bundle', desc: 'Explore graphs', source: 'bundle' },
          { name: 'graph', desc: 'Graph toolkit bundle', source: 'bundle' },
        ];
        return { names: getMatchingCommands('graph').map(m => m.name) };
        """
    )
    assert out["names"] == ["graph", "graph-explorer-bundle"], out["names"]


def test_separator_alias_literal_spelling_still_owns_row_zero():
    """#9035 must not regress #8203: with both spellings registered, the
    literal as-typed spelling still wins row zero under the band sort."""
    out = _run(
        """
        _skillCommandCache = [];
        _bundleCommandCacheReady = true;
        _bundleCommandCache = [];
        _agentCommandCache = [
          { name: 'plugin-review', description: 'Review A', cli_only: false, category: 'Plugin' },
          { name: 'plugin_review', description: 'Review B', cli_only: false, category: 'Plugin' },
        ];
        return {
          underscore_typed: getMatchingCommands('plugin_review').map(m => m.name),
          hyphen_typed: getMatchingCommands('plugin-review').map(m => m.name),
        };
        """
    )
    assert out["underscore_typed"] == ["plugin_review", "plugin-review"]
    assert out["hyphen_typed"] == ["plugin-review", "plugin_review"]
