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
            focus() {{}}, setSelectionRange() {{}}, dispatchEvent() {{}},
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
