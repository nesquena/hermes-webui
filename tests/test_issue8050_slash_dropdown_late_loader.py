"""Issue #8050: a slash-command loader that lands after the user picked a command must not re-open the list.

On an agent-free server ``/api/skills`` fails, so ``loadSkillCommands()`` leaves the cache not-ready (by design, so it
retries, #7509) and ``ensureSkillCommandsLoadedForAutocomplete()`` re-issues the load on every keystroke. Each load
ends in ``refreshSlashCommandDropdown()``; on master that refresh re-opened the dropdown after ``/new`` had been taken,
so the next Enter picked ``/new`` again instead of sending it. That is the browser-smoke ``/new`` flake (#8050), and a
real user typing ``/new`` Enter Enter quickly hits the same thing.

The fix records the text the user closed the list on (a pick or Escape); a refresh that the user's typing did not
cause skips that text, and any edit clears it. Closing because nothing matched is not a dismissal, so a loader landing
later can still show its matches.

These tests run the real ``static/commands.js`` in a ``vm`` context with a minimal DOM stub and a mocked ``api()``
whose ``/api/skills`` reply can be held and failed on demand.
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
BOOT_JS = (ROOT / "static" / "boot.js").read_text(encoding="utf-8")


def _run(script_body: str) -> dict:
    script = textwrap.dedent(
        f"""
        const vm = require('vm');
        const mkEl = () => {{
          const set = new Set();
          let html = '';
          return {{
            className: '', dataset: {{}}, style: {{}}, children: [], value: '',
            // Like the DOM: assigning innerHTML replaces the element's children.
            get innerHTML() {{ return html; }},
            set innerHTML(v) {{ html = v; this.children = []; }},
            classList: {{ add: (c) => set.add(c), remove: (c) => set.delete(c), contains: (c) => set.has(c) }},
            appendChild(c) {{ this.children.push(c); }},
            querySelectorAll() {{ return this.children; }},
            focus() {{}}, setSelectionRange() {{}}, dispatchEvent() {{}}, scrollIntoView() {{}},
          }};
        }};
        const msg = mkEl();
        const dd = mkEl();
        const held = [];
        let holdSkills = false;
        let failSkills = false;
        const ctx = {{
          console,
          setTimeout,
          clearTimeout,
          localStorage: {{ getItem() {{ return null; }}, setItem() {{}}, removeItem() {{}} }},
          t: (key) => key,
          esc: (s) => String(s == null ? '' : s),
          document: {{ createElement: mkEl }},
          $: (id) => (id === 'msg' ? msg : id === 'cmdDropdown' ? dd : null),
          api: async (path) => {{
            if (path === '/api/skills') {{
              if (holdSkills) await new Promise((resolve) => held.push(resolve));
              if (failSkills) throw new Error('agent-free server: no skills');
              return {{ skills: [
                {{ name: 'gamma-live', description: 'A skill' }},
                {{ name: 'newish-skill', description: 'A skill that shares the /ne prefix with built-ins' }},
                {{ name: 'comp', description: 'Focused composition' }},
              ] }};
            }}
            if (path === '/api/commands') return {{ commands: [] }};
            if (path === '/api/commands/bundles') return {{ bundles: [] }};
            throw new Error('unexpected api path: ' + path);
          }},
          __msg: msg,
          __dd: dd,
          __hold: (on) => {{ holdSkills = !!on; }},
          __fail: (on) => {{ failSkills = !!on; }},
          __release: () => held.splice(0).forEach((resolve) => resolve()),
        }};
        vm.createContext(ctx);
        vm.runInContext({json.dumps(COMMANDS_JS)}, ctx);
        (async () => {{
          const result = await vm.runInContext(`(async () => {{ {script_body} }})()`, ctx);
          process.stdout.write(JSON.stringify(result));
        }})().catch((err) => {{ console.error(err && err.stack || err); process.exit(1); }});
        """
    )
    with tempfile.NamedTemporaryFile("w", suffix=".js", encoding="utf-8", delete=False) as handle:
        handle.write(script)
        path = Path(handle.name)
    try:
        proc = subprocess.run(["node", str(path)], capture_output=True, text=True)
    finally:
        path.unlink(missing_ok=True)
    if proc.returncode != 0:
        raise RuntimeError(f"node harness failed (exit {proc.returncode}):\n{proc.stderr}")
    return json.loads(proc.stdout)


_TICK = "await new Promise((r) => setTimeout(r, 20));"


def test_late_failing_skill_load_does_not_reopen_after_a_pick():
    """Red on master: the held /api/skills retry lands after `/new` was picked and re-opens the list."""
    result = _run(
        f"""
        await loadBundleCommands(true);
        await loadAgentCommandMetadata(true);
        __fail(true);
        __hold(true);
        __msg.value = '/new';
        // The composer's input handler: show matches, then kick the (failing) skill loader.
        showCmdDropdown(await getSlashAutocompleteMatches('/new'));
        ensureSkillCommandsLoadedForAutocomplete();
        const openBeforePick = __dd.classList.contains('open');
        // Enter: take the highlighted /new; the list closes and the composer holds '/new'.
        selectCmdDropdownItem();
        const afterPick = {{ open: __dd.classList.contains('open'), value: __msg.value }};
        // The failing skills request now lands.
        __release();
        {_TICK}
        return {{ openBeforePick, afterPick, openAfterLateLoad: __dd.classList.contains('open') }};
        """
    )
    assert result["openBeforePick"] is True
    assert result["afterPick"] == {"open": False, "value": "/new"}
    assert result["openAfterLateLoad"] is False, (
        "a loader that finishes after the pick re-opened the slash dropdown, so the next Enter would pick "
        "again instead of sending /new (#8050)"
    )


def test_late_failing_skill_load_does_not_reopen_after_escape():
    result = _run(
        f"""
        await loadBundleCommands(true);
        await loadAgentCommandMetadata(true);
        __fail(true);
        __hold(true);
        __msg.value = '/ne';
        showCmdDropdown(await getSlashAutocompleteMatches('/ne'));
        ensureSkillCommandsLoadedForAutocomplete();
        // boot.js's Escape branch (pinned below): close, then record the dismissal.
        hideCmdDropdown();
        if (typeof markSlashDropdownDismissed === 'function') markSlashDropdownDismissed();
        __release();
        {_TICK}
        return {{ open: __dd.classList.contains('open') }};
        """
    )
    assert result["open"] is False


def test_loader_landing_while_the_list_is_open_still_refreshes_it():
    """An open list of built-ins (`/ne` -> /new ...) still picks up a skill that loads late."""
    result = _run(
        f"""
        await loadBundleCommands(true);
        await loadAgentCommandMetadata(true);
        __hold(true);
        __msg.value = '/ne';
        showCmdDropdown(await getSlashAutocompleteMatches('/ne'));
        const before = __dd.children.length;
        ensureSkillCommandsLoadedForAutocomplete();
        __release();
        {_TICK}
        return {{ open: __dd.classList.contains('open'), before, after: __dd.children.length }};
        """
    )
    assert result["before"] >= 1, "the /ne list must already show built-ins before the skill load lands"
    assert result["open"] is True
    assert result["after"] == result["before"] + 1, "the late skill load must add newish-skill to the open list"


def test_no_match_close_is_not_a_dismissal_so_late_skills_still_show():
    """'/gam' matches no built-in, so the list closes; the skill load that lands afterwards must still show it."""
    result = _run(
        f"""
        await loadBundleCommands(true);
        await loadAgentCommandMetadata(true);
        __hold(true);
        __msg.value = '/gam';
        const first = await getSlashAutocompleteMatches('/gam');
        if (first.length) showCmdDropdown(first); else hideCmdDropdown();
        ensureSkillCommandsLoadedForAutocomplete();
        const openBefore = __dd.classList.contains('open');
        __release();
        {_TICK}
        return {{ openBefore, open: __dd.classList.contains('open'), items: __dd.children.length }};
        """
    )
    assert result["openBefore"] is False
    assert result["open"] is True and result["items"] >= 1


def test_an_edit_after_a_pick_lets_late_loads_refresh_again():
    result = _run(
        f"""
        await loadBundleCommands(true);
        await loadAgentCommandMetadata(true);
        __hold(true);
        __msg.value = '/new';
        showCmdDropdown(await getSlashAutocompleteMatches('/new'));
        ensureSkillCommandsLoadedForAutocomplete();
        selectCmdDropdownItem();
        // The user keeps typing: boot.js's input handler clears the dismissal first (pinned below).
        if (typeof clearSlashDropdownDismissed === 'function') clearSlashDropdownDismissed();
        __msg.value = '/gam';
        __release();
        {_TICK}
        return {{ open: __dd.classList.contains('open') }};
        """
    )
    assert result["open"] is True


def test_boot_input_and_escape_handlers_use_the_dismissal():
    # The composer keydown's dropdown branch: the single-line Escape handler that closes the slash list.
    escape = re.search(r"if\(e\.key==='Escape'\)\{[^\n]*hideCmdDropdown\(\)[^\n]*\}", BOOT_JS)
    assert escape and "markSlashDropdownDismissed()" in escape.group(0), "Escape must record the dismissal"
    handler = BOOT_JS[BOOT_JS.index("$('msg').addEventListener('input'"):]
    handler = handler[: handler.index("\n});")]
    assert "clearSlashDropdownDismissed()" in handler, "a user edit must clear the dismissal"
    assert handler.index("clearSlashDropdownDismissed()") < handler.index("getSlashAutocompleteMatches(text)"), (
        "the input handler must clear the dismissal before its own lookup"
    )
    assert "slashDropdownDismissedFor(text)" in handler, (
        "the input handler's in-flight lookup must not re-open a list the user closed meanwhile"
    )


def test_late_skill_load_keeps_an_arrowed_highlight():
    """#9110: a skill that loads while the list is open must not pull an arrowed highlight back to row zero."""
    result = _run(
        f"""
        await loadBundleCommands(true);
        await loadAgentCommandMetadata(true);
        __hold(true);
        __msg.value = '/comp';
        showCmdDropdown(await getSlashAutocompleteMatches('/comp'));
        navigateCmdDropdown(1);
        const before = __dd.children[_cmdSelectedIdx] && __dd.children[_cmdSelectedIdx].dataset.cmdName;
        ensureSkillCommandsLoadedForAutocomplete();
        __release();
        {_TICK}
        const names = __dd.children.map(el => el.dataset.cmdName);
        const after = __dd.children[_cmdSelectedIdx] && __dd.children[_cmdSelectedIdx].dataset.cmdName;
        return {{ before, after, names, open: __dd.classList.contains('open') }};
        """
    )
    assert result["before"] == "compact", result
    assert result["open"] is True
    assert "comp" in result["names"], result
    assert result["after"] == "compact", result


def test_late_skill_load_keeps_unarrowed_row_zero_until_next_keystroke():
    """#9110: without an arrow, a newly loaded exact match must not become the row Enter accepts.

    The next showCmdDropdown() with no preserve flag is the keystroke path and ranks that exact match at index 0.
    """
    result = _run(
        f"""
        await loadBundleCommands(true);
        await loadAgentCommandMetadata(true);
        __hold(true);
        __msg.value = '/comp';
        showCmdDropdown(await getSlashAutocompleteMatches('/comp'));
        ensureSkillCommandsLoadedForAutocomplete();
        __release();
        {_TICK}
        const preserved = __dd.children.map(el => el.dataset.cmdName);
        const ranked = await getSlashAutocompleteMatches('/comp');
        selectCmdDropdownItem();
        const composer = __msg.value;
        showCmdDropdown(ranked);
        const keystroke = __dd.children.map(el => el.dataset.cmdName);
        return {{ preserved, composer, keystroke, ranked: ranked.map(m => m.name) }};
        """
    )
    assert result["preserved"][0] == "compress", result
    assert "comp" in result["preserved"] and result["preserved"].index("comp") > 0, result
    assert result["composer"].split()[0] == "/compress", result
    assert result["ranked"][0] == "comp", result
    assert result["keystroke"][0] == "comp", result


def test_late_refresh_keeps_an_arrowed_argument_row():
    """#9110: argument rows share their parent as `name`, so a late refresh must re-select by parent and value."""
    result = _run(
        f"""
        await loadBundleCommands(true);
        await loadAgentCommandMetadata(true);
        __msg.value = '/reasoning ';
        const matches = await getSlashAutocompleteMatches('/reasoning ');
        showCmdDropdown(matches);
        const target = matches.findIndex(m => m.value === 'high');
        for (let i = 0; i < target; i++) navigateCmdDropdown(1);
        const label = () => {{
          const el = __dd.children[_cmdSelectedIdx];
          const m = el && /cmd-item-subarg">([^<]*)</.exec(el.innerHTML);
          return m && m[1];
        }};
        const before = label();
        refreshSlashCommandDropdown();
        {_TICK}
        return {{ before, after: label(), target, open: __dd.classList.contains('open') }};
        """
    )
    assert result["target"] > 0, result
    assert result["before"] == "high", result
    assert result["open"] is True
    assert result["after"] == "high", result


def test_late_skill_load_scrolls_a_kept_arrowed_row_into_view():
    """#9110: rows inserted above an arrowed row can push it below the fold, so the refresh must scroll it back."""
    result = _run(
        f"""
        const make = document.createElement;
        document.createElement = (...args) => {{
          const el = make(...args);
          el.scrollIntoView = (opts) => {{ el.__scrolled = opts; }};
          return el;
        }};
        await loadBundleCommands(true);
        await loadAgentCommandMetadata(true);
        __hold(true);
        __msg.value = '/comp';
        showCmdDropdown(await getSlashAutocompleteMatches('/comp'));
        navigateCmdDropdown(1);
        const beforeIdx = _cmdSelectedIdx;
        ensureSkillCommandsLoadedForAutocomplete();
        __release();
        {_TICK}
        const kept = __dd.children[_cmdSelectedIdx];
        return {{
          beforeIdx,
          afterIdx: _cmdSelectedIdx,
          after: kept && kept.dataset.cmdName,
          scrolled: kept && kept.__scrolled || null,
          others: __dd.children.filter(el => el !== kept && el.__scrolled).length,
        }};
        """
    )
    assert result["after"] == "compact", result
    assert result["afterIdx"] > result["beforeIdx"], "the late skill must land above the arrowed row"
    assert result["scrolled"] == {"block": "nearest"}, "the kept row must be scrolled into view after the refresh"
    assert result["others"] == 0, result
