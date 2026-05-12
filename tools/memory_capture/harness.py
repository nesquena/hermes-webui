from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from .capture import DEFAULT_QUARANTINE, capture_tool_error


@dataclass(frozen=True)
class HarnessRunResult:
    returncode: int
    stdout: str
    stderr: str
    capture: dict[str, Any] | None


def run_harness_command(
    *,
    command: Sequence[str],
    root: Path = DEFAULT_QUARANTINE,
    session_id: str,
    project: str,
    agent: str,
    tool: str,
    cwd: Path | None = None,
    capture_success: bool = False,
) -> HarnessRunResult:
    proc = subprocess.run(
        list(command),
        cwd=cwd,
        text=True,
        capture_output=True,
        check=False,
    )
    capture: dict[str, Any] | None = None
    if proc.returncode != 0 or capture_success:
        capture = capture_tool_error(
            root=root,
            session_id=session_id,
            project=project,
            agent=agent,
            tool=tool,
            command=" ".join(command),
            exit_code=proc.returncode,
            stderr=proc.stderr,
            stdout=proc.stdout,
        )
    return HarnessRunResult(proc.returncode, proc.stdout, proc.stderr, capture)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run a local command with automatic sanitized Yuto capture on failure")
    parser.add_argument("--root", type=Path, default=DEFAULT_QUARANTINE)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--project", default="kei-jarvis")
    parser.add_argument("--agent", default="yuto")
    parser.add_argument("--tool", default="terminal")
    parser.add_argument("--cwd", type=Path)
    parser.add_argument("--capture-success", action="store_true")
    parser.add_argument("command", nargs=argparse.REMAINDER, help="command to run after --")
    args = parser.parse_args(argv)
    command = args.command
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        parser.error("command is required after --")

    result = run_harness_command(
        command=command,
        root=args.root,
        session_id=args.session_id,
        project=args.project,
        agent=args.agent,
        tool=args.tool,
        cwd=args.cwd,
        capture_success=args.capture_success,
    )
    if result.stdout:
        print(result.stdout, end="")
    if result.stderr:
        print(result.stderr, end="", file=sys.stderr)
    payload = {"returncode": result.returncode, "capture": result.capture}
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True), file=sys.stderr)
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
