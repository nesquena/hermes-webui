from __future__ import annotations

import re
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
DOC = REPO_ROOT / "docs" / "wsl-autostart.md"
WSL_SCRIPT = REPO_ROOT / "scripts" / "wsl" / "hermes_webui_autostart.sh"
POWERSHELL_SCRIPT = REPO_ROOT / "scripts" / "windows" / "setup_webui_autostart.ps1"
README = REPO_ROOT / "README.md"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_wsl_autostart_docs_cover_session_and_task_scheduler_options():
    doc = _read(DOC)
    readme = _read(README)

    assert "docs/wsl-autostart.md" in readme
    assert "WSL session startup" in doc
    assert "Windows Task Scheduler" in doc
    assert "scripts/wsl/hermes_webui_autostart.sh" in doc
    assert "scripts/windows/setup_webui_autostart.ps1" in doc
    assert "HERMES_WEBUI_REPO" in doc
    assert "HERMES_WEBUI_LOG_DIR" in doc
    assert "HERMES_WEBUI_REQUIRE_AGENT_PROCESS" in doc
    assert "/root" not in doc
    assert "C:\\Users\\Michael" not in doc


def test_wsl_autostart_launcher_has_safe_duplicate_prevention_and_exports_runtime_env():
    script = _read(WSL_SCRIPT)

    assert script.startswith("#!/usr/bin/env bash\n")
    assert "set -euo pipefail" in script
    assert "flock -n" in script
    assert "HERMES_WEBUI_LOCK_FILE" in script
    assert "HERMES_WEBUI_PID_FILE" in script
    assert "curl -fsS --max-time 3" in script
    assert "bash \"${HERMES_WEBUI_REPO}/start.sh\" --foreground" in script
    assert "nohup" in script

    # The launcher documents HERMES_WEBUI_HOST/PORT as runtime knobs; they must
    # be exported so bootstrap.py/server.py receive the selected WSL values.
    assert re.search(r"^export HERMES_WEBUI_HOST HERMES_WEBUI_PORT$", script, re.MULTILINE)

    assert "/root" not in script
    assert "/home/michael" not in script


def test_wsl_autostart_launcher_passes_bash_syntax_check():
    subprocess.run(["bash", "-n", str(WSL_SCRIPT)], check=True, cwd=REPO_ROOT)


def test_windows_task_scheduler_helper_is_idempotent_and_validates_wsl_script_path():
    script = _read(POWERSHELL_SCRIPT)

    assert "[CmdletBinding(SupportsShouldProcess = $true)]" in script
    assert "Register-ScheduledTask" in script
    assert "-Force" in script
    assert "New-ScheduledTaskSettingsSet" in script
    assert "-MultipleInstances IgnoreNew" in script
    assert "Get-ScheduledTask -TaskName $TaskName" in script
    assert "wsl.exe" in script
    assert '"--exec", "bash", $WslScriptPath' in script
    assert '"--exec", "test", "-f", $WslScriptPath' in script
    assert "Start-ScheduledTask -TaskName $TaskName" in script
    assert "/root" not in script
    assert "C:\\Users\\Michael" not in script


def test_powershell_helper_passes_parser_when_pwsh_is_available():
    pwsh = None
    for candidate in ("pwsh", "powershell"):
        result = subprocess.run(["bash", "-lc", f"command -v {candidate}"], capture_output=True, text=True)
        if result.returncode == 0:
            pwsh = result.stdout.strip()
            break
    if not pwsh:
        # Linux CI often does not include PowerShell. The source-string tests
        # above still pin the safety/idempotency invariants in that case.
        return

    subprocess.run(
        [pwsh, "-NoProfile", "-Command", f"$null = [scriptblock]::Create((Get-Content -Raw '{POWERSHELL_SCRIPT.as_posix()}'))"],
        check=True,
        cwd=REPO_ROOT,
    )
