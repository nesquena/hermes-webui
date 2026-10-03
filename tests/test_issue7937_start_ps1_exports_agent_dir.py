"""start.ps1 must hand its discovered hermes-agent dir to the child process.

server.py calls activate_managed_agent() (managed_agent_startup.py) as its
first Agent-related import, and that hook reads HERMES_WEBUI_AGENT_DIR only.
start.ps1 bypasses bootstrap.py - the component that normally performs this
export - so when the launcher keeps the discovered directory in a PowerShell
variable, dependency activation silently no-ops and the next Agent import
crashes on a module the managed environment already provides.
"""
from __future__ import annotations

import re
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
START_PS1 = REPO_ROOT / "start.ps1"


def _start_ps1_source() -> str:
    return START_PS1.read_text(encoding="utf-8")


def _export_statement_line(source: str) -> int:
    """1-based line of the HERMES_WEBUI_AGENT_DIR export, or -1 when absent."""
    pattern = re.compile(
        r"^\s*(?:\$env:HERMES_WEBUI_AGENT_DIR\s*=|"
        r"\[Environment\]::SetEnvironmentVariable\(\s*'HERMES_WEBUI_AGENT_DIR')",
        re.MULTILINE,
    )
    match = pattern.search(source)
    if match is None:
        return -1
    return source[: match.start()].count("\n") + 1


def test_start_ps1_exports_the_discovered_agent_dir():
    assert _export_statement_line(_start_ps1_source()) != -1, (
        "start.ps1 must export HERMES_WEBUI_AGENT_DIR before launching server.py; "
        "without it managed_agent_startup.activate_managed_agent() no-ops"
    )


def test_agent_dir_export_precedes_the_server_launch():
    source = _start_ps1_source()
    export_line = _export_statement_line(source)
    launch_line = source[: source.index("& $Python $serverPath")].count("\n") + 1
    assert export_line != -1
    assert export_line < launch_line


def test_agent_dir_export_precedes_the_agent_venv_python_override():
    """The exported value must be the discovered dir, not the venv-derived one.

    $Python is re-pointed at <agent>/venv/Scripts/python.exe further down; if
    the export trailed that block it could pick up the venv path instead of the
    Agent root that activate_managed_agent() validates.
    """
    source = _start_ps1_source()
    export_line = _export_statement_line(source)
    venv_line = source[: source.index("$agentVenvPython = Join-Path")].count("\n") + 1
    assert export_line != -1
    assert export_line < venv_line


def test_agent_dir_export_precedes_the_startup_banner():
    source = _start_ps1_source()
    export_line = _export_statement_line(source)
    banner_line = source[: source.index('Write-Host "[start.ps1] Hermes WebUI')].count(
        "\n"
    ) + 1
    assert export_line != -1
    assert export_line < banner_line


def test_bootstrap_owns_the_same_export_for_the_posix_launcher():
    """Guards the pairing this fix restores: bootstrap.py exports it for start.sh."""
    bootstrap = (REPO_ROOT / "bootstrap.py").read_text(encoding="utf-8")
    assert 'os.environ["HERMES_WEBUI_AGENT_DIR"]' in bootstrap



def test_hermes_home_default_precedes_agent_discovery():
    """HERMES_HOME must be defaulted before the candidate list is built.

    Exporting the discovery result makes start.ps1's order win over the
    server's _discover_agent_dir. Resolving HERMES_HOME first (matching
    api/config.py) keeps %LOCALAPPDATA%\\hermes ahead of a stale
    %USERPROFILE%\\.hermes install.
    """
    source = _start_ps1_source()
    home_default = source.index("if ($hermesHomeIsDefault)")
    discovery = source.index("$AgentDir = $env:HERMES_WEBUI_AGENT_DIR")
    assert home_default < discovery, (
        "HERMES_HOME default must be resolved before agent discovery so the "
        "exported HERMES_WEBUI_AGENT_DIR matches api/config.py"
    )


def test_hermes_home_agent_candidate_is_listed_first():
    """First auto-discovery candidate must be $HERMES_HOME\\hermes-agent."""
    source = _start_ps1_source()
    # Narrow to the auto-discovery block (between empty-AgentDir check and export).
    block_start = source.index("if (-not $AgentDir)")
    block_end = source.index(
        "[Environment]::SetEnvironmentVariable('HERMES_WEBUI_AGENT_DIR'"
    )
    block = source[block_start:block_end]
    first_append = re.search(
        r"\$serverCandidates\s*\+=\s*\(Join-Path\s+\$env:HERMES_HOME\s+'hermes-agent'\)",
        block,
    )
    assert first_append is not None, (
        "auto-discovery must Join-Path $env:HERMES_HOME 'hermes-agent' as a server candidate"
    )
    earlier = re.search(r"\$serverCandidates\s*\+=", block)
    assert earlier is not None
    assert earlier.start() == first_append.start(), (
        "Join-Path $env:HERMES_HOME 'hermes-agent' must be the first candidate append; "
        f"found earlier append at offset {earlier.start()} vs HERMES_HOME at "
        f"{first_append.start()}"
    )


def _discovery_block(source: str) -> str:
    """Auto-discovery block between empty-AgentDir check and the export."""
    block_start = source.index("if (-not $AgentDir)")
    block_end = source.index(
        "[Environment]::SetEnvironmentVariable('HERMES_WEBUI_AGENT_DIR'"
    )
    return source[block_start:block_end]


def test_discovery_candidate_order_matches_server():
    """Server-equivalent appends must follow api/config.py through HOME/hermes-agent."""
    block = _discovery_block(_start_ps1_source())
    appends = [
        m.group(0)
        for m in re.finditer(r"\$serverCandidates\s*\+=\s*[^\n]+", block)
    ]
    assert len(appends) >= 4, f"expected >=4 server candidate appends, got {appends!r}"
    assert "Join-Path $env:HERMES_HOME 'hermes-agent'" in appends[0]
    assert "Join-Path $repoParent 'hermes-agent'" in appends[1]
    # After sibling/parent: the home the SERVER would default to, then the
    # platform home itself, then HOME\hermes-agent.
    platform_idx = next(
        i
        for i, a in enumerate(appends)
        if "Join-Path $serverPlatformDefaultHome 'hermes-agent'" in a
    )
    new_home_idx = next(
        i for i, a in enumerate(appends) if "Join-Path $newHermesHome 'hermes-agent'" in a
    )
    home_idx = next(
        i
        for i, a in enumerate(appends)
        if re.search(
            r"Join-Path\s+\$env:USERPROFILE\s+'hermes-agent'",
            a,
        )
    )
    sibling_idx = next(
        i for i, a in enumerate(appends) if "Join-Path $repoParent 'hermes-agent'" in a
    )
    assert sibling_idx < platform_idx < new_home_idx < home_idx, (
        f"order must be sibling → server-default home → platform home → "
        f"HOME/hermes-agent (sibling@{sibling_idx}, platform@{platform_idx}, "
        f"new_home@{new_home_idx}, home@{home_idx})"
    )
    # Must not append the launcher's own launcher-only legacy root directly.
    # It is reachable as the server-default home when #2905 selects it, and as
    # a launcher-only rescue below.
    unconditional_legacy = [
        a
        for a in appends
        if "USERPROFILE" in a and ".hermes" in a and "hermes-agent" in a
    ]
    assert not unconditional_legacy, (
        "USERPROFILE/.hermes/hermes-agent must not be appended unconditionally; "
        f"Found: {unconditional_legacy!r}"
    )
    # Program Files must not be mixed into the server-equivalent list
    assert not any("ProgramW6432" in a or "ProgramFiles" in a for a in appends), (
        "Program Files roots must not be appended to $serverCandidates"
    )


def test_discovery_uses_two_pass_run_agent_then_hermes_cli():
    """First pass prefers run_agent.py; only then accept hermes_cli (pip-style)."""
    block = _discovery_block(_start_ps1_source())
    run_agent = block.index("run_agent.py")
    hermes_cli_pass = block.index(
        "Test-Path (Join-Path $c 'hermes_cli') -PathType Container"
    )
    assert run_agent < hermes_cli_pass, (
        "run_agent.py pass must precede the hermes_cli pass so source checkouts win"
    )
    after_run = block[run_agent:]
    assert "if (-not $AgentDir)" in after_run, (
        "second pass must be gated on AgentDir still being empty after run_agent.py"
    )


def test_platform_default_home_uses_localappdata_when_established():
    """Case 1: custom HERMES_HOME empty + Agents in both legacy and LOCALAPPDATA.

    After sibling/parent, the candidate must be the home the SERVER would
    default to per api.paths._platform_default_hermes_home, not an
    unconditional USERPROFILE\\.hermes first among fallbacks. Legacy is only
    chosen when it still holds WebUI state and the new location does not — and
    LOCALAPPDATA stays in the list unconditionally behind it.
    """
    source = _start_ps1_source()
    assert "$newHermesHome = Join-Path $env:LOCALAPPDATA 'hermes'" in source
    assert "$legacyHermesHome = Join-Path $env:USERPROFILE '.hermes'" in source
    assert (
        "-not (Test-HermesWebuiState $newHermesHome) -and\n"
        "    (Test-HermesWebuiState $legacyHermesHome)"
    ) in source
    assert "$serverPlatformDefaultHome = $legacyHermesHome" in source
    block = _discovery_block(source)
    assert "Join-Path $serverPlatformDefaultHome 'hermes-agent'" in block
    assert "Join-Path $newHermesHome 'hermes-agent'" in block
    # Legacy USERPROFILE\.hermes must NOT be a server-equivalent candidate in
    # its own right; it is reachable via $serverPlatformDefaultHome when #2905
    # selects it, and as a launcher-only rescue after both server passes.
    server_appends = [
        m.group(0)
        for m in re.finditer(r"\$serverCandidates\s*\+=\s*[^\n]+", block)
    ]
    assert not any(
        "USERPROFILE" in a and ".hermes" in a for a in server_appends
    ), f"legacy .hermes must not be in $serverCandidates: {server_appends!r}"
    assert "Join-Path $env:USERPROFILE '.hermes\\hermes-agent'" in block
    assert "$launcherOnlyCandidates" in block


def test_home_hermes_agent_precedes_program_files_roots():
    """Case 2: HOME/hermes-agent must beat launcher-only Program Files roots.

    Server candidate #6 is HOME/hermes-agent; Program Files is launcher-only
    and must be searched last so it cannot preempt a HOME install the server
    would have used.
    """
    block = _discovery_block(_start_ps1_source())
    home_pos = block.index("Join-Path $env:USERPROFILE 'hermes-agent'")
    # ${env:ProgramFiles(x86)} nests parens, so match on ProgramW6432 marker.
    pf_pos = block.index("${env:ProgramW6432}")
    assert home_pos < pf_pos, (
        "HOME/hermes-agent must be appended before Program Files roots "
        f"(home@{home_pos}, pf@{pf_pos})"
    )
    # LOCALAPPDATA must not ride along in the Program Files loop anymore.
    # Scope this to the server-candidate list: the narrow repair below rebuilds
    # master's OWN candidate order on purpose, and master's order does fold
    # LOCALAPPDATA into the Program Files loop. Asserting it repo-wide would
    # forbid re-deriving master's list at all, which is the point of the repair.
    server_block = block[block.index("$serverCandidates = @()") :]
    server_block = server_block[: server_block.index("Select-Object -Unique")]
    assert "@($env:LOCALAPPDATA," not in server_block
    assert "$env:LOCALAPPDATA, ${env:ProgramW6432}" not in server_block


def test_program_files_only_after_both_server_passes():
    """CORE: stale Program Files source must not beat LOCALAPPDATA pip Agent.

    Both run_agent.py and hermes_cli passes over $serverCandidates must complete
    before any launcher-only (legacy .hermes + Program Files) fallback pass.
    Otherwise an all-source first pass over a combined list picks Program Files
    over a working LOCALAPPDATA pip root.
    """
    block = _discovery_block(_start_ps1_source())
    assert "$serverCandidates = @()" in block
    assert "$launcherOnlyCandidates = @()" in block
    server_run = block.index("foreach ($c in $serverCandidates)")
    server_pip = block.index(
        "Test-Path (Join-Path $c 'hermes_cli') -PathType Container"
    )
    legacy_marker = block.index(
        "Join-Path $env:USERPROFILE '.hermes\\hermes-agent'"
    )
    pf_marker = block.index("${env:ProgramW6432}")
    launcher_run = block.index("foreach ($c in $launcherOnlyCandidates)")
    assert server_run < server_pip < legacy_marker < pf_marker < launcher_run, (
        "server source+pip passes must both precede launcher-only legacy+PF "
        f"(server_run@{server_run}, server_pip@{server_pip}, "
        f"legacy@{legacy_marker}, pf_list@{pf_marker}, launcher_run@{launcher_run})"
    )
    between = block[server_pip:launcher_run]
    assert "if (-not $AgentDir)" in between, (
        "launcher-only source pass must be gated on AgentDir still empty "
        "after both server-equivalent passes"
    )
    before_pf = block[:pf_marker]
    assert "$serverCandidates += (Join-Path $root" not in before_pf
    assert "$launcherOnlyCandidates += (Join-Path $root" in block[pf_marker:]


def test_explicit_webui_state_dir_skips_legacy_home_migration():
    """CORE: the #2905 legacy preference must move only the STATE_DIR default.

    api/config.py reads providers and models from HERMES_HOME, so a user whose
    config.yaml is in %LOCALAPPDATA%\\hermes must keep reading it from there
    even while their WebUI sessions are still at the legacy
    %USERPROFILE%\\.hermes. Only webui/ state location is affected by the
    migration, and only while this script chose HERMES_HOME itself.
    """
    source = _start_ps1_source()
    assert "$platformDefaultHermesHome = $newHermesHome" in source
    assert (
        "$hermesHomeIsDefault = -not $env:HERMES_HOME\n"
        "if ($hermesHomeIsDefault) {\n"
        "    $env:HERMES_HOME = $platformDefaultHermesHome\n"
        "}"
    ) in source, (
        "HERMES_HOME must take the platform default unconditionally; only the "
        "STATE_DIR default may follow the #2905 legacy preference"
    )
    # The legacy preference must not be able to reach HERMES_HOME any more.
    assert "$platformDefaultHermesHome = $serverPlatformDefaultHome" not in source
    # The redirect is applied to the STATE_DIR default, gated on this script
    # having picked HERMES_HOME so an explicit value keeps master's behaviour.
    assert (
        "if ($hermesHomeIsDefault -and $serverPlatformDefaultHome -ne "
        "$platformDefaultHermesHome) {"
    ) in source, (
        "the #2905 legacy fallback must gate on $hermesHomeIsDefault so an "
        "explicit HERMES_HOME still gets webui/ beneath it"
    )
    assert (
        "$env:HERMES_WEBUI_STATE_DIR = Join-Path $serverPlatformDefaultHome 'webui'"
    ) in source
    # Comment documents why the two answers are separated.
    assert "reads providers and models from it" in source
    assert "STATE_DIR default below" in source


def test_legacy_hermes_is_launcher_only_before_program_files():
    """BRICK: legacy-only Agent must still be found after server-equivalent passes.

    %USERPROFILE%\\.hermes\\hermes-agent is not a server candidate (HOME is
    %USERPROFILE%\\hermes-agent), but master always searched it. Keep it as a
    launcher-only rescue ahead of Program Files so a legacy-only install is not
    a hard exit.
    """
    block = _discovery_block(_start_ps1_source())
    server_appends = [
        m.group(0)
        for m in re.finditer(r"\$serverCandidates\s*\+=\s*[^\n]+", block)
    ]
    assert not any(
        ".hermes" in a and "hermes-agent" in a for a in server_appends
    ), f"legacy .hermes must not be appended to $serverCandidates: {server_appends!r}"
    legacy_pos = block.index("Join-Path $env:USERPROFILE '.hermes\\hermes-agent'")
    pf_pos = block.index("${env:ProgramW6432}")
    assert "$launcherOnlyCandidates" in block
    assert legacy_pos < pf_pos, (
        "legacy .hermes/hermes-agent must precede Program Files among "
        f"launcher-only roots (legacy@{legacy_pos}, pf@{pf_pos})"
    )
    server_pip = block.index(
        "Test-Path (Join-Path $c 'hermes_cli') -PathType Container"
    )
    assert server_pip < legacy_pos


def test_launcher_only_roots_are_ranked_by_path_not_by_kind():
    """BRICK: a stale Program Files source must not outrank a legacy pip Agent.

    %USERPROFILE%\\.hermes\\hermes-agent is a pip install (hermes_cli) and comes
    first in $launcherOnlyCandidates; %ProgramFiles%\\hermes\\hermes-agent is a
    stale source checkout (run_agent.py). Splitting the launcher-only roots into
    a run_agent.py pass and then a hermes_cli pass let the stale Program Files
    source win purely by kind, and its hermes_bootstrap.py can SystemExit before
    the server binds. These roots are not in api/config.py's candidate list, so
    there is no server pass order to mirror here: take them in path order and
    accept either kind.
    """
    block = _discovery_block(_start_ps1_source())
    launcher = block[block.index("$launcherOnlyCandidates = @()") :]
    legacy_pos = launcher.index("Join-Path $env:USERPROFILE '.hermes\\hermes-agent'")
    pf_pos = launcher.index("${env:ProgramW6432}")
    assert legacy_pos < pf_pos, (
        "legacy .hermes/hermes-agent must stay ahead of Program Files so the "
        "interleaved pass can reach it first"
    )
    # The two kinds must be tested in ONE condition, not as two separate loops.
    interleaved = launcher.index(
        "if ((Test-Path (Join-Path $c 'hermes_cli') -PathType Container) -or"
    )
    assert interleaved > pf_pos, (
        "the launcher-only pass must come after the roots are built"
    )
    single_pass = launcher[interleaved:]
    # No kind-splitting loop may remain over $launcherOnlyCandidates.
    run_agent_kind_loop = single_pass.index(
        "Test-Path (Join-Path $c 'run_agent.py') -PathType Leaf"
    )
    assert single_pass[run_agent_kind_loop - 40 : run_agent_kind_loop].count(
        "foreach"
    ) == 0, (
        "run_agent.py must be part of the same condition as hermes_cli in the "
        "launcher-only phase, not a separate preceding loop"
    )


def test_agent_discovery_uses_the_servers_default_home_plus_the_platform_home():
    """Candidate 5 is the SERVER's default home, and LOCALAPPDATA must survive.

    api/config.py candidate 5 is `_DEFAULT_HERMES_HOME / "hermes-agent"`, and
    `_DEFAULT_HERMES_HOME` is `_platform_default_hermes_home()` — which still
    prefers the legacy %USERPROFILE%\\.hermes while the WebUI state has not
    migrated off it. Reading only the exported HERMES_HOME here got that wrong
    in both directions:

    - reaching %USERPROFILE%\\hermes-agent first exports a flat checkout the
      server never searches, in the very layout where the legacy install is the
      one the server would pick;
    - dropping %LOCALAPPDATA%\\hermes\\hermes-agent when the #2905 preference
      applies makes a LOCALAPPDATA-only Agent unreachable and startup dies with
      "hermes-agent not found".

    So candidate 5 is the server's own default, candidate 6 is the platform
    home unconditionally, and candidate 7 is HOME\\hermes-agent.
    """
    source = _start_ps1_source()
    block = _discovery_block(source)

    assert "$serverPlatformDefaultHome = $newHermesHome" in source
    assert (
        "if ($hermesHomeIsDefault) {\n"
        "    $env:HERMES_HOME = $platformDefaultHermesHome\n"
        "}"
    ) in source, (
        "HERMES_HOME must not be moved to the legacy home by the #2905 "
        "preference; that would hide the working config.yaml"
    )

    assert (
        "Join-Path $serverPlatformDefaultHome 'hermes-agent'" in block
    ), "candidate 5 must be the home the server itself would default to"
    assert (
        "Join-Path $newHermesHome 'hermes-agent'" in block
    ), (
        "%LOCALAPPDATA\\hermes\\hermes-agent must stay a candidate even when "
        "the #2905 legacy preference selects a different home for candidate 5, "
        "or a LOCALAPPDATA-only Agent becomes unreachable"
    )
    # The launcher-only legacy root must not reappear as the server's answer.
    assert "$platformDefaultAgentHome" not in source
    assert (
        "$serverCandidates += (Join-Path $platformDefaultHermesHome 'hermes-agent')"
        not in source
    )

    # The legacy install must still be findable, as a launcher-only candidate.
    assert (
        "$launcherOnlyCandidates += (Join-Path $env:USERPROFILE '.hermes\\hermes-agent')"
        in source
    ), (
        "the legacy Agent root has to stay in the launcher-only candidates, "
        "otherwise moving candidate 5 to the new home loses legacy-only installs"
    )


def test_state_helper_parameter_is_not_named_home():
    """`$Home` as a parameter name collides with the read-only automatic `$HOME`.

    PowerShell variable names are case-insensitive, so `param([string]$Home)`
    makes every assignment a write to the read-only automatic variable. Under
    this script's `$ErrorActionPreference = 'Stop'` the first call aborts the
    script, so no layout gets as far as Agent discovery.
    """
    source = _start_ps1_source()
    helper = source[source.index("function Test-HermesWebuiState") :]
    helper = helper[: helper.index("\n}")]
    assert "param([string]$Home)" not in helper, (
        "Test-HermesWebuiState must not bind $Home; PowerShell treats that as "
        "the read-only automatic $HOME and the script dies on the first call"
    )
    assert "param([string]$BaseHome)" in helper


def test_missing_venv_falls_back_to_a_candidate_venv():
    """A source checkout with no venv and no bootstrap cannot supply its deps.

    The source-first pass reaches a sibling checkout before the platform
    default, and activate_managed_agent() no-ops for an Agent without
    hermes_bootstrap.py, so the dependency import has to come from elsewhere.
    """
    source = _start_ps1_source()
    python_block = source[source.index("# === Prefer the agent's venv Python") :]
    python_block = python_block[: python_block.index("# === Resolve bind")]
    code = "\n".join(
        ln for ln in python_block.split("\n") if not ln.lstrip().startswith("#")
    )
    assert "hermes_bootstrap.py" in code, (
        "a checkout with no venv but a hermes_bootstrap.py activates its own "
        "dependencies, so the venv fallback must not fire for it"
    )
    assert "$candidates" in code, (
        "the fallback has to walk the discovered candidates; that list is the "
        "only place a working venv can be found"
    )
    assert "falling back to the venv" in python_block, (
        "silently swapping interpreters is worse than a bare source checkout; "
        "the fallback must say which Agent it is covering"
    )

