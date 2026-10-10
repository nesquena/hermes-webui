<#
.SYNOPSIS
    Native Windows launcher for Hermes WebUI - PowerShell equivalent
    of start.sh, bypassing bootstrap.py's platform refusal.

.DESCRIPTION
    Mirrors start.sh's discovery: load optional .env, find Python,
    locate the hermes-agent install, set sensible env defaults, then
    invoke server.py directly. The bootstrap.py path is skipped
    because it currently raises on platform.system() == 'Windows';
    server.py itself runs cleanly on native Windows.

    Assumes Python + hermes-agent + the WebUI Python deps are already
    installed natively on Windows - same assumption start.sh makes
    when invoked outside a fresh bootstrap. For first-time setup, the
    native Windows path is to install Python 3.11+, then create a
    Windows venv (`python -m venv venv`) and `pip install -r
    requirements.txt` from the hermes-agent root in PowerShell - this
    script then finds `venv\Scripts\python.exe` automatically. A venv
    created inside WSL2 is a Linux virtual environment (`venv/bin/python`)
    and cannot be used by native Windows Python, so the bootstrap.py-
    inside-WSL2 path produces a venv `start.ps1` can't invoke.

.PARAMETER Port
    TCP port the WebUI binds to. Overrides HERMES_WEBUI_PORT env.
    Default: 8787.

.PARAMETER BindHost
    Bind address. Overrides HERMES_WEBUI_HOST env.
    Default: 127.0.0.1.

.EXAMPLE
    .\start.ps1
    # Bind to 127.0.0.1:8787, foreground.

.EXAMPLE
    .\start.ps1 -Port 9000
    # Bind to 127.0.0.1:9000.

.EXAMPLE
    $env:HERMES_WEBUI_HOST = '0.0.0.0'
    .\start.ps1
    # Bind to all interfaces (set a password first via env or Settings).

.LINK
    https://github.com/nesquena/hermes-webui/issues/1952
#>

[CmdletBinding()]
param(
    [int]$Port = 0,
    [string]$BindHost = ''
)

$ErrorActionPreference = 'Stop'
$RepoRoot = Split-Path -Parent $PSCommandPath

# === Load .env (mirroring start.sh's filtering) ========================
$envFile = Join-Path $RepoRoot '.env'
if (Test-Path $envFile) {
    foreach ($line in Get-Content $envFile -Encoding UTF8) {
        $trimmed = $line.Trim()
        if (-not $trimmed -or $trimmed.StartsWith('#') -or -not $trimmed.Contains('=')) { continue }
        $kv = $trimmed -split '=', 2
        $key = ($kv[0].Trim() -replace '^export\s+', '')
        # Filter out shell-readonly vars (UID, GID, EUID, EGID, PPID) per start.sh
        if ($key -in @('UID', 'GID', 'EUID', 'EGID', 'PPID')) { continue }
        if ($key -notmatch '^[A-Za-z_][A-Za-z0-9_]*$') { continue }
        # Explicit $null check — an env var explicitly set to '' should still
        # be considered "set" and NOT overridden by .env (empty string is
        # falsey in PowerShell, so a plain truthy check would mis-skip).
        if ($null -ne [Environment]::GetEnvironmentVariable($key)) { continue }
        $val = $kv[1]
        if ($val -match '^"(.*)"$') { $val = $Matches[1] }
        elseif ($val -match "^'(.*)'$") { $val = $Matches[1] }
        [Environment]::SetEnvironmentVariable($key, $val)
    }
}

# === Find Python (matches start.sh order) ==============================
$Python = $env:HERMES_WEBUI_PYTHON
if (-not $Python) {
    foreach ($candidate in @('python3', 'python', 'py')) {
        $cmd = Get-Command $candidate -ErrorAction SilentlyContinue
        if ($cmd) { $Python = $cmd.Source; break }
    }
}
if (-not $Python) {
    Write-Error 'Python 3 is required to run server.py (set HERMES_WEBUI_PYTHON or add python to PATH).'
    exit 1
}

# === Resolve platform-default Hermes home before agent discovery =======
# api/config.py's _discover_agent_dir prefers $HERMES_HOME\hermes-agent after
# the explicit HERMES_WEBUI_AGENT_DIR override, then later falls back to
# `_DEFAULT_HERMES_HOME\hermes-agent` from
# api/paths._platform_default_hermes_home(). Mirror that #2905 rule here:
# %LOCALAPPDATA%\hermes once established; %USERPROFILE%\.hermes only when the
# legacy home still holds WebUI state and the new location does not. Resolving
# before the candidate list keeps the exported discovery result aligned with
# the server. Leave an already-set HERMES_HOME alone.

function Test-HermesWebuiState {
    # $BaseHome, not $Home: PowerShell variable names are case-insensitive, so
    # a $Home parameter collides with the read-only automatic $HOME and the
    # assignment fails under $ErrorActionPreference = 'Stop' — which killed the
    # script on the first call, before any Agent discovery.
    param([string]$BaseHome)
    foreach ($rel in @('webui\sessions', 'webui\settings.json', 'webui')) {
        # SilentlyContinue: an unreadable legacy .hermes (access denied) must
        # not abort startup under $ErrorActionPreference = 'Stop'. Matches the
        # server's non-throwing state check — treat unreadable as "no state".
        if (Test-Path (Join-Path $BaseHome $rel) -ErrorAction SilentlyContinue) { return $true }
    }
    return $false
}

if ($env:LOCALAPPDATA) {
    $newHermesHome = Join-Path $env:LOCALAPPDATA 'hermes'
    $legacyHermesHome = Join-Path $env:USERPROFILE '.hermes'
} else {
    $newHermesHome = Join-Path $env:USERPROFILE '.hermes'
    $legacyHermesHome = $newHermesHome
}

# What the server itself would use. api/paths.py gates the #2905 legacy
# fallback on where the WebUI STATE lives and nothing else, so this is
# computed with no STATE_DIR gate.
$serverPlatformDefaultHome = $newHermesHome
if ($legacyHermesHome -ne $newHermesHome -and
    -not (Test-HermesWebuiState $newHermesHome) -and
    (Test-HermesWebuiState $legacyHermesHome)) {
    $serverPlatformDefaultHome = $legacyHermesHome
}

# The exported HERMES_HOME stays on master's unconditional platform default.
# api/config.py reads providers and models from it, so yanking it to the legacy
# home because only the WebUI STATE has not migrated yet hides a working
# config.yaml sitting in %LOCALAPPDATA%\hermes. The #2905 legacy preference
# belongs to the STATE_DIR default below, which is the only place it changes
# what the user actually sees.
$platformDefaultHermesHome = $newHermesHome

$hermesHomeIsDefault = -not $env:HERMES_HOME
if ($hermesHomeIsDefault) {
    $env:HERMES_HOME = $platformDefaultHermesHome
}

# === Find Hermes Agent dir (server.py imports from it) =================
# When HERMES_WEBUI_AGENT_DIR is set we still validate it on disk —
# an explicit override pointing at a missing dir should fail FAST
# with a clear message, not silently progress into a python3 launch
# that's about to crash on missing imports. Smoke-test feedback on
# PR #2783: nesquena/hermes-webui requested this guard.
$AgentDir = $env:HERMES_WEBUI_AGENT_DIR
# Set by discovery below. Stays $false for an explicit override, which is the
# caller's choice and is never second-guessed.
$selectedIsBareSourceCheckout = $false
if ($AgentDir -and -not (Test-Path (Join-Path $AgentDir 'hermes_cli') -PathType Container)) {
    Write-Error "HERMES_WEBUI_AGENT_DIR is set to '$AgentDir' but no hermes_cli/ folder exists there. Unset the variable to fall back to auto-discovery, or fix the path."
    exit 1
}
if (-not $AgentDir) {
    # Mirror api/config.py `_discover_agent_dir` for server-equivalent candidates:
    # HERMES_HOME\hermes-agent → repo sibling → parent (when it looks like an
    # agent root) → _DEFAULT_HERMES_HOME\hermes-agent → HOME\hermes-agent.
    # Complete BOTH source (run_agent.py) and pip-style (hermes_cli) passes over
    # that list BEFORE trying launcher-only roots (legacy .hermes + Program Files).
    # Folding those into the same candidate list lets a stale Program Files source
    # outrank a working LOCALAPPDATA pip Agent on the all-source first pass — the
    # server never searches Program Files (or USERPROFILE\.hermes) at all. Build
    # Program Files incrementally — ${env:ProgramFiles(x86)} is null on 32-bit
    # Windows and in some constrained environments, and Join-Path throws on a
    # null Path. Skip any system-wide root that isn't set so the launcher stays
    # robust across Windows variants.
    $serverCandidates = @()
    $serverCandidates += (Join-Path $env:HERMES_HOME 'hermes-agent')
    $repoParent = Split-Path -Parent $RepoRoot
    $serverCandidates += (Join-Path $repoParent 'hermes-agent')
    # Parent-is-agent: repo cloned inside hermes-agent/ (same gate as
    # api/config.py's `_looks_like_agent_source_root` on REPO_ROOT.parent).
    if ((Test-Path (Join-Path $repoParent 'run_agent.py') -PathType Leaf) -or
        (Test-Path (Join-Path $repoParent 'hermes_cli') -PathType Container)) {
        $serverCandidates += $repoParent
    }
    # 5. _DEFAULT_HERMES_HOME\hermes-agent, i.e. what the SERVER would default
    #    to — which is NOT the same as where this script points HERMES_HOME.
    #    _platform_default_hermes_home() still prefers %USERPROFILE%\.hermes
    #    while the WebUI state has not migrated off it, so this slot resolves to
    #    the legacy home in that layout and to %LOCALAPPDATA%\hermes otherwise.
    #    Reach HOME\hermes-agent first and this exports a stale flat checkout the
    #    server itself would never pick.
    $serverCandidates += (Join-Path $serverPlatformDefaultHome 'hermes-agent')
    # 6. LOCALAPPDATA\hermes-agent unconditionally. A legacy-state layout can
    #    have the new home's Agent as the only install on the machine, and
    #    dropping this when the #2905 preference applies leaves startup dying
    #    with "hermes-agent not found".
    $serverCandidates += (Join-Path $newHermesHome 'hermes-agent')
    # 7. HOME\hermes-agent (Path.home() → %USERPROFILE% on Windows)
    $serverCandidates += (Join-Path $env:USERPROFILE 'hermes-agent')
    # De-dup server-equivalent list (HERMES_HOME may coincide with platform default).
    $serverCandidates = $serverCandidates | Select-Object -Unique
    # Two-pass over server-equivalent candidates first (matches api/config.py).
    foreach ($c in $serverCandidates) {
        if (Test-Path (Join-Path $c 'run_agent.py') -PathType Leaf -ErrorAction SilentlyContinue) { $AgentDir = $c; break }
    }
    if (-not $AgentDir) {
        foreach ($c in $serverCandidates) {
            if (Test-Path (Join-Path $c 'hermes_cli') -PathType Container -ErrorAction SilentlyContinue) { $AgentDir = $c; break }
        }
    }
    # Launcher-only fallbacks — only after both server-equivalent passes.
    # Master always searched %USERPROFILE%\.hermes\hermes-agent first; the server
    # never does (its HOME candidate is %USERPROFILE%\hermes-agent). Keep that
    # legacy path as a launcher-only rescue AFTER the server-equivalent passes so
    # a legacy-only Agent is still found, then Program Files (POSIX XDG/opt
    # equivalents have no Windows server twin). Legacy ahead of Program Files
    # matches master's precedence among these launcher-only roots.
    $launcherOnlyCandidates = @()
    $launcherOnlyCandidates += (Join-Path $env:USERPROFILE '.hermes\hermes-agent')
    foreach ($root in @(${env:ProgramW6432}, ${env:ProgramFiles}, ${env:ProgramFiles(x86)})) {
        if ($root) { $launcherOnlyCandidates += (Join-Path $root 'hermes\hermes-agent') }
    }
    # De-dup: WOW64 can make ProgramFiles == ProgramFiles(x86); legacy may equal
    # a prior server candidate when HERMES_HOME already points there.
    $launcherOnlyCandidates = $launcherOnlyCandidates | Select-Object -Unique
    # One interleaved pass, in path order, accepting either kind. The
    # server-equivalent passes above stay source-first-then-pip because that is
    # api/config.py's order and the server genuinely searches that list twice.
    # These roots are not in the server's list at all, so there is no server
    # order to mirror and splitting them by kind lets a stale Program Files
    # SOURCE checkout outrank a working legacy pip install that comes first in
    # the list — and that checkout's hermes_bootstrap.py can SystemExit before
    # the server binds.
    if (-not $AgentDir) {
        foreach ($c in $launcherOnlyCandidates) {
            if ((Test-Path (Join-Path $c 'hermes_cli') -PathType Container -ErrorAction SilentlyContinue) -or
                (Test-Path (Join-Path $c 'run_agent.py') -PathType Leaf -ErrorAction SilentlyContinue)) {
                $AgentDir = $c
                break
            }
        }
    }
    # Combined list for the not-found error message.
    $candidates = @($serverCandidates) + @($launcherOnlyCandidates)

    # Narrow repair, and narrow on purpose: it fires ONLY when the source-first
    # pass displaced an install that master's own hermes_cli-only pass would have
    # selected. Both halves matter, and each closes a real regression:
    #
    #   * Selected root has no hermes_cli -> it is a bare source checkout, the
    #     one shape master's pass never accepted. Master would have gone on to
    #     some install further down its list, so this genuinely is a displaced
    #     install. When the selected root DOES have hermes_cli AND is not the
    #     repo sibling (a pip-style Agent under HERMES_HOME / LOCALAPPDATA, or
    #     an editable tree the server itself would also pick first), master's
    #     pass would have accepted that same root, nothing was displaced, and
    #     the selected install stays authoritative together with its
    #     interpreter.
    #
    #   * The repo sibling is special: serverCandidates list it before the
    #     LOCALAPPDATA install, while master's own order lists LOCALAPPDATA
    #     before the sibling. A complete Agent checkout (run_agent.py +
    #     hermes_cli/ + hermes_bootstrap.py together, no venv) therefore wins
    #     the source-first pass here, then fails a hermes_cli-only "bare?"
    #     guard and keeps the sibling — even though master would have kept the
    #     installed Agent. Treat the sibling the same as a bare source for the
    #     repair so that shape is covered too.
    #
    #   * Replacement is master's pick over master's own candidate order, not
    #     "any later root that happens to have a venv". Treating venv presence
    #     as universal authority was what let a root the launcher never used to
    #     search - %USERPROFILE%\hermes-agent is the launcher's LAST server
    #     candidate and is not in master's list at all - displace a working
    #     selected Agent.
    #
    # Why the case is worth repairing at all: the export below runs
    # hermes_bootstrap.py from $AgentDir through activate_managed_agent(), and
    # that hook lets SystemExit propagate (it is a BaseException, so
    # `except Exception` there does not catch it), so a stale sibling checkout
    # can take down startup on a machine whose install would have started fine.
    # $AgentDir and $Python are moved together so the exported
    # HERMES_WEBUI_AGENT_DIR and the interpreter stay the same install.
    $selectedIsRepoSibling = $AgentDir -and (
        $AgentDir -eq (Join-Path (Split-Path -Parent $RepoRoot) 'hermes-agent'))
    # Layout fallbacks that can displace a working install the same way the
    # repo sibling does: an Agent root at the repo parent itself, or at
    # %USERPROFILE%\hermes-agent (server candidate 7 / Path.home()). Both source
    # (run_agent.py) and pip-style (hermes_cli only) roots count: discovery can
    # select either shape there, and a no-venv pip-style root at those paths
    # cannot supply the Agent's dependencies any better than a source tree.
    $selectedIsLayoutFallback = $selectedIsRepoSibling -or (
        $AgentDir -and
        ($AgentDir -eq $repoParent -or
         $AgentDir -eq (Join-Path $env:USERPROFILE 'hermes-agent')))
    if ($AgentDir -and
        -not (Test-Path (Join-Path $AgentDir 'venv\Scripts\python.exe')) -and
        (
            -not (Test-Path (Join-Path $AgentDir 'hermes_cli') -PathType Container) -or
            $selectedIsLayoutFallback
        )) {
        # Master's candidate order: %USERPROFILE%\.hermes, LOCALAPPDATA, then
        # Program Files, then the repo sibling. Built incrementally for the same
        # null-Path reason as $launcherOnlyCandidates above.
        $masterCandidates = @()
        $masterCandidates += (Join-Path $env:USERPROFILE '.hermes\hermes-agent')
        foreach ($root in @($env:LOCALAPPDATA, ${env:ProgramW6432}, ${env:ProgramFiles}, ${env:ProgramFiles(x86)})) {
            if ($root) { $masterCandidates += (Join-Path $root 'hermes\hermes-agent') }
        }
        $masterCandidates += (Join-Path (Split-Path -Parent $RepoRoot) 'hermes-agent')
        $masterCandidates = $masterCandidates | Select-Object -Unique
        $masterPick = $null
        foreach ($c in $masterCandidates) {
            if (Test-Path (Join-Path $c 'hermes_cli') -PathType Container -ErrorAction SilentlyContinue) { $masterPick = $c; break }
        }
        # Layout-fallback selections may win over a pip install that has no
        # in-root venv of its own (deps already importable from the selected
        # Python). For those layouts only, accept masterPick without requiring
        # its venv; otherwise keep requiring a usable venv so a random later
        # install cannot displace a healthy selected root.
        if ($masterPick -and $masterPick -ne $AgentDir -and
            ($selectedIsLayoutFallback -or (Test-Path (Join-Path $masterPick 'venv\Scripts\python.exe')))) {
            Write-Warning "Agent dir '$AgentDir' is a source checkout with no install of its own and no venv, so its dependencies would come from hermes_bootstrap.py, which can exit before the server starts; using the installed Agent at '$masterPick' instead, which is what this script selected before agent discovery was aligned with the server."
            $AgentDir = $masterPick
        }
    }

    # Single gate shared by the repair above and the interpreter fallback below:
    # the selected root has no hermes_cli, i.e. it is a bare source checkout that
    # nothing was pip-installed into and master's hermes_cli-only pass would not
    # have accepted. Those two fallbacks exist to rescue a checkout that cannot
    # supply its own dependencies. They must never move a launch onto a venv
    # that belongs to a different install, so a selected root that DOES have
    # hermes_cli keeps both its root and its interpreter even when it has no venv
    # of its own - managed activation returns without importing a bootstrap for
    # an installed Agent, and its packages are already importable from the
    # interpreter this script runs.
    if ($AgentDir) {
        $selectedIsBareSourceCheckout =
            -not (Test-Path (Join-Path $AgentDir 'hermes_cli') -PathType Container)
    }
}
if (-not $AgentDir) {
    $searched = $candidates -join ', '
    Write-Error "hermes-agent not found. Searched: $searched. Set HERMES_WEBUI_AGENT_DIR explicitly to override."
    exit 1
}

# === Export the discovered agent dir to the child process ===============
# server.py calls activate_managed_agent() (managed_agent_startup.py) as its
# first Agent-related import, and that hook keys off HERMES_WEBUI_AGENT_DIR
# ONLY - it is what puts the Agent's dependency environment on sys.path.
# Without this export the value discovered above stays a PowerShell variable,
# activation silently no-ops, and the very next Agent import dies with
# ModuleNotFoundError on a dependency the managed environment already
# provides. api/config.py's own discovery fallback runs too late to help: it
# is imported after activate_managed_agent() has already returned.
# bootstrap.py does the equivalent at os.environ["HERMES_WEBUI_AGENT_DIR"];
# start.ps1 bypasses bootstrap.py, so it owns this export.
# The two-argument SetEnvironmentVariable form sets the variable for this
# process and its children, matching how this script already loads .env.
[Environment]::SetEnvironmentVariable('HERMES_WEBUI_AGENT_DIR', $AgentDir)

# === Prefer the agent's venv Python if available =======================
# A venv next to the selected Agent is the interpreter that already has its
# dependencies. Two ways an Agent can supply those instead, and either one
# means the venv override is unnecessary: its own venv, or the managed
# bootstrap that activate_managed_agent() imports (hermes_bootstrap.py).
$agentVenvPython = Join-Path $AgentDir 'venv\Scripts\python.exe'
if ($selectedIsBareSourceCheckout -and
    -not (Test-Path $agentVenvPython) -and
    -not (Test-Path (Join-Path $AgentDir 'hermes_bootstrap.py'))) {
    # Neither, and the selected root is a bare source checkout. Discovery has
    # already preferred an installed Agent when master's pass would have picked
    # one and only the install has a venv, so reaching here means no candidate
    # was such an install either (e.g. a venv next to a checkout that has no
    # hermes_cli/ yet). The first Agent import after activate_managed_agent()
    # would no-op and die with ModuleNotFoundError, so take the first candidate
    # that does have a venv, in candidate order. Only applies to discovery of a
    # bare source checkout — an explicit HERMES_WEBUI_AGENT_DIR is the caller's
    # choice and is left alone, and a selected root that has hermes_cli keeps
    # the interpreter it was selected with even without a venv of its own.
    if ($candidates) {
        foreach ($c in $candidates) {
            $fallback = Join-Path $c 'venv\Scripts\python.exe'
            if (Test-Path $fallback -ErrorAction SilentlyContinue) {
                Write-Warning "Agent dir '$AgentDir' has no venv and no hermes_bootstrap.py; falling back to the venv at '$fallback' for the Agent dependencies."
                $agentVenvPython = $fallback
                break
            }
        }
    }
}
if (Test-Path $agentVenvPython) {
    $Python = $agentVenvPython
}

# managed_agent_startup.activate_managed_agent() keys off the env var
# only: without this export the discovered/validated dir never reaches
# the child process, dependency activation is skipped, and server.py dies
# on ModuleNotFoundError before api/config.py's fallback discovery runs
# (#7937).
$env:HERMES_WEBUI_AGENT_DIR = $AgentDir

# === Resolve bind + state defaults =====================================
$BindHostFinal = if ($BindHost) { $BindHost } elseif ($env:HERMES_WEBUI_HOST) { $env:HERMES_WEBUI_HOST } else { '127.0.0.1' }
$PortFinal = if ($Port) {
    $Port
} elseif ($env:HERMES_WEBUI_PORT) {
    # TryParse + range guard on the env var. A plain [int] cast on the
    # env var throws InvalidCastException with no actionable context when
    # the env var is set to a non-integer (typo, accidental shell
    # expansion, etc.) — surface a targeted error message instead.
    $parsedPort = 0
    if (-not [int]::TryParse($env:HERMES_WEBUI_PORT, [ref]$parsedPort)) {
        Write-Error "HERMES_WEBUI_PORT='$($env:HERMES_WEBUI_PORT)' is not a valid integer port. Unset the variable to use the default (8787), or set it to a number 1-65535."
        exit 1
    }
    if ($parsedPort -lt 1 -or $parsedPort -gt 65535) {
        Write-Error "HERMES_WEBUI_PORT=$parsedPort is out of TCP-port range. Must be 1-65535."
        exit 1
    }
    $parsedPort
} else {
    8787
}
$env:HERMES_WEBUI_HOST = $BindHostFinal
$env:HERMES_WEBUI_PORT = "$PortFinal"
# HERMES_HOME default was resolved before agent discovery above so the
# exported HERMES_WEBUI_AGENT_DIR matches api/config.py's search order.
if (-not $env:HERMES_WEBUI_STATE_DIR) {
    $env:HERMES_WEBUI_STATE_DIR = Join-Path $env:HERMES_HOME 'webui'
    # #2905 migration, applied to the STATE_DIR default and nowhere else: while
    # the sessions still live under the legacy %USERPROFILE%\.hermes and the new
    # home holds none, read them from there. HERMES_HOME above deliberately
    # stayed on %LOCALAPPDATA%\hermes so the working config.yaml next to the
    # Agent is still the one api/config.py reads. Gated on this script having
    # chosen HERMES_HOME itself — an explicit value keeps master's behaviour of
    # putting webui/ under it.
    if ($hermesHomeIsDefault -and $serverPlatformDefaultHome -ne $platformDefaultHermesHome) {
        $env:HERMES_WEBUI_STATE_DIR = Join-Path $serverPlatformDefaultHome 'webui'
    }
}

# === Ensure dirs exist =================================================
New-Item -ItemType Directory -Force -Path $env:HERMES_HOME | Out-Null
New-Item -ItemType Directory -Force -Path $env:HERMES_WEBUI_STATE_DIR | Out-Null

# === Launch (foreground, matches start.sh) =============================
Write-Host "[start.ps1] Hermes WebUI native Windows launcher" -ForegroundColor Cyan
Write-Host "[start.ps1] Python:     $Python"
Write-Host "[start.ps1] Agent dir:  $AgentDir"
Write-Host "[start.ps1] State dir:  $env:HERMES_WEBUI_STATE_DIR"
Write-Host "[start.ps1] Binding:    ${BindHostFinal}:${PortFinal}"
Write-Host ""

$serverPath = Join-Path $RepoRoot 'server.py'
if (-not (Test-Path $serverPath)) {
    Write-Error "server.py not found at $serverPath - is this the hermes-webui repo root?"
    exit 1
}

# Capture exit code, let finally{} run Pop-Location, exit AFTER the try.
# Plain `exit $LASTEXITCODE` inside the try block can prevent the finally
# from running in some termination paths (especially when dot-sourced or
# in interactive sessions), leaving the caller's working directory stuck
# at $RepoRoot.
$script:serverExitCode = 0
Push-Location $RepoRoot
try {
    # @args was non-functional here — PowerShell does NOT populate $args when the
    # script declares [CmdletBinding()] with an explicit param() block (Copilot's
    # finding on PR #2807). Dropped rather than added a ValueFromRemainingArguments
    # parameter, because the existing tracked use case is the launcher running
    # server.py with the env-var-driven config — no pass-through args are needed.
    # If pass-through becomes a requirement later, add a [Parameter(ValueFromRemainingArguments=$true)] [string[]]$ServerArgs and splat that.
    & $Python $serverPath
    $script:serverExitCode = $LASTEXITCODE
} finally {
    Pop-Location
}
exit $script:serverExitCode
