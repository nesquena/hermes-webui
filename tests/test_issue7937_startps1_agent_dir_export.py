"""#7937: start.ps1 must export the resolved agent dir to the child process.

`start.ps1` discovered the hermes-agent dir into the *local* `$AgentDir`,
validated/printed it, and launched `server.py` — but never wrote the value
back to `$env:HERMES_WEBUI_AGENT_DIR`. `managed_agent_startup.
activate_managed_agent()` keys off that env var only, so dependency
activation was silently skipped and the server died on
`ModuleNotFoundError: No module named 'yaml'` before api/config.py's own
fallback discovery could run.

The fix exports the resolved dir once `$AgentDir` is finalized (covers both
entry states: env var preset-and-validated, or unset-and-auto-discovered),
before the `& $Python $serverPath` launch.

Run:
    ./scripts/test.sh tests/test_issue7937_startps1_agent_dir_export.py -v
"""

from __future__ import annotations

import re
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
PS1 = (REPO_ROOT / "start.ps1").read_text(encoding="utf-8")
MANAGED = (REPO_ROOT / "managed_agent_startup.py").read_text(encoding="utf-8")


def _brace_depth(text: str, pos: int) -> int:
    """Net open-brace depth at a byte offset (PowerShell has no heredoc-free
    brace subtleties we care about here — used only to prove the assignment
    sits at file top level, not inside an if/foreach block)."""
    depth = 0
    i = 0
    while i < pos:
        ch = text[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
        i += 1
    return depth


def test_consumer_reads_only_the_env_var():
    # The contract being fixed: activate_managed_agent() has no other way to
    # learn the agent dir — if the launcher doesn't export it, activation is
    # skipped outright.
    assert 'os.environ.get("HERMES_WEBUI_AGENT_DIR")' in MANAGED
    idx = MANAGED.index('os.environ.get("HERMES_WEBUI_AGENT_DIR")')
    block = MANAGED[idx : idx + 120]
    assert re.search(r"if\s+not\s+agent_dir", block), (
        "activate_managed_agent must bail when the env var is unset"
    )


def test_startps1_exports_resolved_agent_dir_exactly_once():
    # A single unconditional export of the finalized $AgentDir — not a plain
    # script-scoped variable (which would never reach the child), and not
    # duplicated later where a stale value could win.
    writes = [
        m for m in re.finditer(
            r"\$env:HERMES_WEBUI_AGENT_DIR\s*=\s*\$AgentDir\b", PS1
        )
    ]
    assert len(writes) == 1, (
        "expected exactly one `$env:HERMES_WEBUI_AGENT_DIR = $AgentDir` export, "
        f"found {len(writes)}"
    )
    pos = writes[0].start()
    # Unconditional: the assignment must sit at file top level. If it were
    # nested inside an `if`/`foreach`, an entry state (e.g. env preset) could
    # skip it and re-create the bug.
    assert _brace_depth(PS1, pos) == 0, (
        "the export must be unconditional — it must run whether the dir was "
        "auto-discovered or supplied via HERMES_WEBUI_AGENT_DIR"
    )


def test_export_runs_after_agent_dir_is_finalized():
    # The export has to come after both failure gates: it must carry the
    # *resolved* value, so it sits after the discovery loop AND after the
    # 'not found' Write-Error/exit.
    export_pos = PS1.index("$env:HERMES_WEBUI_AGENT_DIR = $AgentDir")
    discovery_pos = PS1.index("$AgentDir = $c; break")
    gate_pos = PS1.index("hermes-agent not found. Searched:")
    assert discovery_pos < export_pos, (
        "export must follow auto-discovery — exporting earlier would ship "
        "an unset value when the env var started unset"
    )
    assert gate_pos < export_pos, (
        "export must follow the not-found gate — a failed discovery exits "
        "before ever reaching the launch env"
    )


def test_export_runs_before_the_server_launch():
    export_pos = PS1.index("$env:HERMES_WEBUI_AGENT_DIR = $AgentDir")
    launch_pos = PS1.index("& $Python $serverPath")
    assert export_pos < launch_pos, (
        "the env write must precede `& $Python $serverPath` — the child "
        "inherits the process env snapshot at invocation"
    )


def test_agent_dir_is_never_sourced_from_unvalidated_places():
    # The two only sanctioned origins: a preset env var (validated on disk by
    # the hermes_cli probe) or the candidate loop guarded by the same probe.
    # Re-assert the validation gate still wraps every write into $AgentDir so
    # the export can't carry a path that failed the hermes_cli check.
    assign_block = PS1[
        PS1.index("$AgentDir = $env:HERMES_WEBUI_AGENT_DIR") : PS1.index(
            "$env:HERMES_WEBUI_AGENT_DIR = $AgentDir"
        )
    ]
    assert "Test-Path (Join-Path $AgentDir 'hermes_cli')" in assign_block
    assert "Test-Path (Join-Path $c 'hermes_cli')" in assign_block
    # No other $env write may sneak the raw env value past validation.
    assert "HERMES_WEBUI_AGENT_DIR = $env:" not in assign_block
