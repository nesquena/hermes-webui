#!/usr/bin/env python3
"""Check whether a checkout's origin is reachable by update-check Git."""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from api.subprocess_utils import (  # noqa: E402
    clean_git_env,
    noninteractive_git_argv,
    windows_hide_flags,
)


def _run(args: list[str], checkout: Path, git: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        noninteractive_git_argv(args, executable=git),
        cwd=str(checkout),
        capture_output=True,
        text=True,
        timeout=30,
        encoding="utf-8",
        errors="replace",
        env=clean_git_env(),
        creationflags=windows_hide_flags(),
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Mirror the WebUI update check's unattended Git transport behavior."
    )
    parser.add_argument("checkout", type=Path, help="WebUI or Hermes Agent Git checkout")
    args = parser.parse_args()
    checkout = args.checkout.expanduser().resolve()

    git = shutil.which("git")
    if not git:
        print("git executable not found", file=sys.stderr)
        return 1
    if not checkout.is_dir():
        print(f"checkout is not a directory: {checkout}", file=sys.stderr)
        return 1

    try:
        origin = _run(["remote", "get-url", "origin"], checkout, git)
        if origin.returncode != 0:
            detail = (origin.stderr or origin.stdout or "origin is not configured").strip()
            print(f"Update Git diagnostic failed: {detail}", file=sys.stderr)
            return 1

        probe = _run(["ls-remote", "origin"], checkout, git)
    except subprocess.TimeoutExpired:
        print("Update Git diagnostic failed: Git command timed out after 30s", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"Update Git diagnostic failed to start Git: {exc}", file=sys.stderr)
        return 1

    if probe.returncode != 0:
        detail = (probe.stderr or probe.stdout or f"git exited with status {probe.returncode}").strip()
        print(f"Update Git diagnostic failed: {detail}", file=sys.stderr)
        return 1

    print("Update Git diagnostic succeeded: origin is reachable without prompting.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
