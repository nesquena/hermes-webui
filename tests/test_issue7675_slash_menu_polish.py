"""Regression tests for #7675: slash-command menu polish.

Four independent polish items filed off the Fable UX gate on #6962. The PR
keeps the four small changes independent so any one of them can revert
without touching the others.

1. ``/pet`` must render its argument hint on the dropdown row
   (previously: pet special-case skipped the arg).
2. Argument notation must be consistent across builtin and registry rows
   (``<required>`` / ``[optional]`` -- never bare).
3. Typing a hidden registry command (e.g. ``/agents``) must route through
   the existing CLI-only explainer instead of leaking as plain text.
4. ``_AGENT_COMMAND_ALIASES`` in ``static/messages.js`` is gone -- its
   alias membership is now set members in
   ``_AGENT_COMMANDS_RUN_ON_WEBUI``.
"""
from __future__ import annotations

import json
import re
import textwrap
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
COMMANDS_JS = (REPO_ROOT / "static" / "commands.js").read_text(encoding="utf-8")
MESSAGES_JS = (REPO_ROOT / "static" / "messages.js").read_text(encoding="utf-8")


# ──────────────────────────────────────────────────────────────────────────
# Static-source assertions (cheap, no Node boot)
# ──────────────────────────────────────────────────────────────────────────


# Fix #1: /pet must surface its arg hint on the dropdown row.
def test_pet_arg_hint_branch_reads_args_hint_from_metadata():
    """The pet special-case branch in getMatchingCommands() must pass
    args_hint through normalizeArgHint so /pet renders the same shape
    as every other registry row.
    """
    # The branch is the explicit pet special-case (still in place to
    # override cli_only gating); it must set arg from the metadata.
    pet_branch_idx = COMMANDS_JS.find("if('pet'.startsWith(q)&&!seen.has('pet'))")
    assert pet_branch_idx != -1, "pet special-case branch is gone -- regression on #7675 follow-up #1"
    end_idx = COMMANDS_JS.find("\n  }", pet_branch_idx)
    branch = COMMANDS_JS[pet_branch_idx:end_idx]
    assert "args_hint" in branch, (
        "pet special-case must read args_hint from the agent metadata so the "
        "row can render the arg hint (#7675 follow-up #1)."
    )
    assert "normalizeArgHint" in branch, (
        "pet special-case must wrap args_hint through normalizeArgHint so the "
        "row uses the same <required>/[optional] convention as the rest of "
        "the dropdown (#7675 follow-ups #1 + #2)."
    )


def test_pet_arg_hint_is_not_lost_in_registry_loop_path():
    """The /pet registry-loop branch also covers pet, but pet is dispatched
    by handlePetSlashCommand not by the generic agent exec path. The
    registry loop must still allow pet to fall through (cli_only check
    exempts pet) so a future /api/commands metadata that supplies
    args_hint populates the row even if the special-case is removed.
    """
    assert "if(cmd.cli_only&&name!=='pet')continue;" in COMMANDS_JS, (
        "cli_only gating in the registry loop must keep exempting pet so "
        "args_hint from the metadata is still surfaced."
    )


# Fix #2: argument notation consistency.
def test_no_bare_builtin_arg_hints_remain():
    """Builtin COMMANDS rows must use '<required>' or '[optional]' -- never
    bare identifiers like 'name', 'message', 'prompt'. Such bare strings
    read as inconsistent alongside the registry rows that already use
    angle brackets.
    """
    # Strip COMMANDS=[...] array contents only. The array is bounded by
    # the first '];' after the opening '[', not by anything later in the
    # file. Walk brace depth to be safe.
    array_start = COMMANDS_JS.find("const COMMANDS=[")
    assert array_start != -1
    body_start = array_start + len("const COMMANDS=[")
    depth = 0
    end = body_start
    for i, ch in enumerate(COMMANDS_JS[body_start:], start=body_start):
        if ch == "[":
            depth += 1
        elif ch == "]":
            if depth == 0:
                end = i
                break
            depth -= 1
    body = COMMANDS_JS[body_start:end]

    # Find every `arg: 'X'` literal in the body. None should be bare.
    bare_hints = re.findall(r"arg:'([^']+)'", body)
    for hint in bare_hints:
        if hint.startswith("[") and hint.endswith("]"):
            continue  # [optional] is fine
        if hint.startswith("<") and hint.endswith(">"):
            continue  # <required> is fine
        raise AssertionError(
            f"Builtin arg hint {hint!r} is not wrapped in <required> or "
            f"[optional] brackets (#7675 follow-up #2). Allowed: <...> / [...]."
        )


def test_normalize_arg_hint_helper_handles_three_shapes():
    """normalizeArgHint must preserve '[optional]' and '<required>' literals
    and wrap any bare hint in angle brackets.
    """
    assert "function normalizeArgHint" in COMMANDS_JS

    # Extract the helper and execute it in a minimal Node VM. The helper
    # is pure so it has no dependencies.
    helper_idx = COMMANDS_JS.find("function normalizeArgHint")
    brace_open = COMMANDS_JS.find("{", helper_idx)
    depth = 0
    end = brace_open
    for i, ch in enumerate(COMMANDS_JS[brace_open:], start=brace_open):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    helper_src = COMMANDS_JS[helper_idx:end]

    cases = [
        ("", None),
        ("   ", None),
        ("[focus topic]", "[focus topic]"),
        ("<model_name>", "<model_name>"),
        ("name", "<name>"),
        ("message", "<message>"),
        ("show|hide|... ", "<show|hide|...>"),
    ]
    # Run the helper once with a battery of inputs and assert every
    # output matches the expected shape. This is the pure-function
    # test the maintainer asked for (#7649 review): execute the helper,
    # don't grep the source.
    script = textwrap.dedent(
        f"""
        const vm = require('vm');
        const ctx = {{ console }};
        vm.createContext(ctx);
        vm.runInContext({json.dumps(helper_src)}, ctx);
        const fn = ctx.normalizeArgHint;
        const cases = {json.dumps(cases)};
        const out = cases.map(([h, _]) => fn(h));
        console.log(JSON.stringify(out));
        """
    )
    import subprocess

    result = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 0, f"Node VM failed: {result.stderr}"
    outputs = json.loads(result.stdout.strip().splitlines()[-1])
    assert outputs == [c[1] for c in cases], (
        f"normalizeArgHint output mismatch: got {outputs!r}, "
        f"expected {[c[1] for c in cases]!r}"
    )


# Fix #3: hidden commands route through the explainer.
def test_hidden_command_path_routes_through_cli_only_response():
    """A non-dispatchable agent command (e.g. /agents) must not fall through
    to plain text. The new branch in send() must call
    cliOnlyCommandResponse() just like the existing cli_only branch.
    """
    # The new branch is right after the existing cli_only check.
    cli_only_idx = MESSAGES_JS.find("if(_agentCmd&&_agentCmd.cli_only){")
    assert cli_only_idx != -1
    rest = MESSAGES_JS[cli_only_idx:]
    # The hidden-command branch follows the cli_only branch and uses the
    # same explainer helper.
    assert "category!=='Plugin'" in rest, (
        "Non-dispatchable registry commands must be routed through the "
        "explainer, except for the Plugin category which has its own "
        "exec transport below."
    )
    assert "_AGENT_COMMANDS_RUN_ON_WEBUI.has(" in rest, (
        "Hidden command check must consult the dispatch allowlist to "
        "decide whether to fall through to plain text."
    )
    # The explainer call must be reused, not duplicated.
    explainer_calls = rest.count("cliOnlyCommandResponse(")
    assert explainer_calls >= 2, (
        f"Expected at least 2 cliOnlyCommandResponse() call sites in the "
        f"slash intercept block (cli_only + non-dispatchable), found "
        f"{explainer_calls}."
    )


def test_hidden_command_predicate_is_the_dispatchability_check():
    """#7683 (review finding 1): the hidden-command predicate must ask the
    dispatchability question -- "does send() dispatch this command?" -- not
    "is it absent from _AGENT_COMMANDS_RUN_ON_WEBUI?".

    That set holds only the backend-exec family, so WebUI-native commands
    (moa/sessions/resume/pet) are intentionally absent from it while still
    being dispatched by their own native branches further down send().
    Treating "absent" as "hidden" swallowed /moa before its native handler
    and answered with the CLI-only explainer instead.
    """
    cli_only_idx = MESSAGES_JS.find("if(_agentCmd&&_agentCmd.cli_only){")
    assert cli_only_idx != -1
    rest = MESSAGES_JS[cli_only_idx:]
    hidden_idx = rest.find("_isWebuiDispatchableAgentCommand(_agentCmd)")
    assert hidden_idx != -1, (
        "The hidden-command branch must gate on "
        "_isWebuiDispatchableAgentCommand(_agentCmd) -- the same predicate "
        "commands.js uses to decide what autocomplete announces -- so a "
        "WebUI-native command (e.g. /moa) is not misrouted to the CLI-only "
        "explainer (#7683)."
    )
    # And it must not have been "fixed" by adding moa to the generic
    # backend-exec set, which would send /moa down the generic exec path
    # instead of its native handler (a different regression).
    m = re.search(
        r"const\s+_AGENT_COMMANDS_RUN_ON_WEBUI\s*=\s*new Set\(\[([^\]]+)\]\)",
        MESSAGES_JS,
    )
    assert m, "_AGENT_COMMANDS_RUN_ON_WEBUI not found in messages.js"
    members = set(re.findall(r"'([^']*)'", m.group(1)))
    assert "moa" not in members, (
        "moa must stay out of the generic backend-exec dispatch set -- it "
        "has its own native handler in send() (#7683)."
    )


# Fix #4: dead code in messages.js removed.
def test_dead_agent_command_aliases_dict_is_gone():
    """The formerly dead _AGENT_COMMAND_ALIASES dict at the top of
    messages.js is removed. Its alias membership now lives in
    _AGENT_COMMANDS_RUN_ON_WEBUI as set members (so typing /reload_mcp
    still matches when the metadata cache is empty).
    """
    assert "_AGENT_COMMAND_ALIASES" not in MESSAGES_JS, (
        "_AGENT_COMMAND_ALIASES is dead JS code -- its alias membership "
        "must move into _AGENT_COMMANDS_RUN_ON_WEBUI as set members."
    )


def test_dispatch_set_includes_underscore_alias_forms():
    """The dispatch set must keep the underscore alias forms as members
    (not as a separate dict) so the existing fallthrough path still
    matches /reload_mcp and friends when getAgentCommandMetadata() is
    not available.
    """
    m = re.search(
        r"const\s+_AGENT_COMMANDS_RUN_ON_WEBUI\s*=\s*new Set\(\[([^\]]+)\]\)",
        MESSAGES_JS,
    )
    assert m, "_AGENT_COMMANDS_RUN_ON_WEBUI not found in messages.js"
    members = set(re.findall(r"'([^']*)'", m.group(1)))
    assert "reload-mcp" in members
    assert "reload_mcp" in members, (
        "Underscore alias form /reload_mcp must be in the dispatch set "
        "as a member (#7675 follow-up #4 wire-up)."
    )
    assert "reload-skills" in members
    assert "reload_skills" in members
    assert "codex-runtime" in members
    assert "codex_runtime" in members
    assert "credits" in members
    # WebUI-native commands are dispatched in their own branches in
    # send(); they don't belong in the backend-exec dispatch set.
    for native in ("moa", "sessions", "resume", "pet"):
        assert native not in members, (
            f"{native!r} is a WebUI-native command and is dispatched in "
            f"its own branch, not via the backend-exec set."
        )


# ──────────────────────────────────────────────────────────────────────────
# Behavioural assertions (Node VM driver)
# ──────────────────────────────────────────────────────────────────────────


def _drive_get_matching_commands(*, pet_meta):
    """Run getMatchingCommands('') in a Node VM with a synthetic agent
    metadata cache and return the resulting rows.

    Note: ``static/commands.js`` declares ``_agentCommandCache`` (and
    several other top-level lets) at module scope, so the ctx property
    is shadowed by the module-scope binding unless we strip those
    declarations first.
    """
    declarations = (
        r"\s*(let|var)\s+_agentCommandCache\s*=\s*null\s*;",
        r"\s*(let|var)\s+_agentCommandCachePromise\s*=\s*null\s*;",
        r"\s*(let|var)\s+_agentCommandCacheReady\s*=\s*false\s*;",
        r"\s*(let|var)\s+_agentCommandCachePrimed\s*=\s*false\s*;",
        r"\s*(let|var)\s+_agentCommandCachePrimingPromise\s*=\s*null\s*;",
        r"\s*(let|var)\s+_bundleCommandCache\s*=\s*\[\s*\]\s*;",
        r"\s*(let|var)\s+_bundleCommandCacheReady\s*=\s*false\s*;",
        r"\s*(let|var)\s+_skillCommandCache\s*=\s*\[\s*\]\s*;",
    )
    src = COMMANDS_JS
    for pattern in declarations:
        src = re.sub(pattern, "", src)
    script = textwrap.dedent(
        f"""
        const vm = require('vm');
        const ctx = {{
          console,
          localStorage: {{ getItem(){{return null;}}, setItem(){{}}, removeItem(){{}} }},
          t: (key) => key,
          _bundleCommandCache: [],
          _bundleCommandCacheReady: true,
          _skillCommandCache: [],
          _agentCommandCache: {json.dumps([pet_meta] if pet_meta else [])},
          _agentCommandCacheReady: true,
          _getReservedSlashCommandSlugs: () => new Set(),
        }};
        vm.createContext(ctx);
        vm.runInContext({json.dumps(src)}, ctx);
        const all = ctx.getMatchingCommands('');
        const petRows = all.filter(r => r.name === 'pet');
        console.log(JSON.stringify(petRows));
        """
    )
    import subprocess

    result = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 0, f"Node VM failed: {result.stderr}"
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_get_matching_commands_pet_row_includes_arg_hint():
    """End-to-end: when the agent metadata supplies an args_hint for pet,
    the row returned by getMatchingCommands() must include an arg field
    with the hint wrapped in the normalised convention.
    """
    rows = _drive_get_matching_commands(
        pet_meta={
            "name": "pet",
            "description": "Desktop Companion command",
            "category": "Tools",
            "aliases": [],
            "cli_only": True,
            "args_hint": "toggle|list|scale <n>|<slug>",
        }
    )
    assert len(rows) == 1, f"Expected one pet row, got {rows!r}"
    assert rows[0].get("arg") == "<toggle|list|scale <n>|<slug>>", (
        f"Pet row arg hint is missing or not normalised: {rows[0]!r} "
        f"(regression on #7675 follow-up #1)"
    )


def test_get_matching_commands_builtin_uses_angle_brackets():
    """End-to-end: every builtin command that takes a required argument
    must render it inside angle brackets in the dropdown row, not as a
    bare string.
    """
    # Strip top-level lets that would shadow ctx-supplied caches.
    declarations = (
        r"\s*(let|var)\s+_agentCommandCache\s*=\s*null\s*;",
        r"\s*(let|var)\s+_agentCommandCachePromise\s*=\s*null\s*;",
        r"\s*(let|var)\s+_agentCommandCacheReady\s*=\s*false\s*;",
        r"\s*(let|var)\s+_agentCommandCachePrimed\s*=\s*false\s*;",
        r"\s*(let|var)\s+_agentCommandCachePrimingPromise\s*=\s*null\s*;",
        r"\s*(let|var)\s+_bundleCommandCache\s*=\s*\[\s*\]\s*;",
        r"\s*(let|var)\s+_bundleCommandCacheReady\s*=\s*false\s*;",
        r"\s*(let|var)\s+_skillCommandCache\s*=\s*\[\s*\]\s*;",
    )
    src = COMMANDS_JS
    for pattern in declarations:
        src = re.sub(pattern, "", src)
    script = textwrap.dedent(
        f"""
        const vm = require('vm');
        const ctx = {{
          console,
          localStorage: {{ getItem(){{return null;}}, setItem(){{}}, removeItem(){{}} }},
          t: (key) => key,
          _bundleCommandCache: [],
          _bundleCommandCacheReady: true,
          _skillCommandCache: [],
          _agentCommandCache: [],
          _agentCommandCacheReady: true,
          _getReservedSlashCommandSlugs: () => new Set(),
        }};
        vm.createContext(ctx);
        vm.runInContext({json.dumps(src)}, ctx);
        const rows = ctx.getMatchingCommands('');
        const bad = rows
          .filter(r => r.source === 'builtin' && r.arg && !/^[<\\[]/.test(r.arg));
        console.log(JSON.stringify(bad.map(r => ({{ name: r.name, arg: r.arg }}))));
        """
    )
    import subprocess

    result = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 0, f"Node VM failed: {result.stderr}"
    bad = json.loads(result.stdout.strip().splitlines()[-1])
    assert not bad, (
        f"Builtin rows still render bare arg hints: {bad!r} "
        f"(regression on #7675 follow-up #2)"
    )
