#!/usr/bin/env python3
"""Check whether a checkout's origin is reachable by update-check Git."""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from api.subprocess_utils import (  # noqa: E402
    clean_git_env,
    is_safe_diagnostic_remote,
    noninteractive_git_env,
    noninteractive_git_argv,
    sanitize_git_diagnostic,
    repository_git_proxy_blocks,
    trusted_git_credential_config,
    windows_hide_flags,
)


def _emit(
    message: str,
    *,
    sensitive_paths: tuple[str | Path, ...],
    error: bool = False,
) -> None:
    """Print one sanitized diagnostic line."""
    print(
        sanitize_git_diagnostic(message, sensitive_paths=sensitive_paths, limit=1000),
        file=sys.stderr if error else sys.stdout,
    )


def _run(
    args: list[str],
    checkout: Path,
    git: str,
    env: dict[str, str],
    credential_config: tuple[tuple[str, str], ...],
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        noninteractive_git_argv(
            args,
            executable=git,
            credential_config=credential_config,
        ),
        cwd=str(checkout),
        capture_output=True,
        text=True,
        timeout=30,
        encoding="utf-8",
        errors="replace",
        env=env,
        creationflags=windows_hide_flags(),
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Mirror the WebUI update check's unattended Git transport behavior."
    )
    parser.add_argument("checkout", type=Path, help="WebUI or Hermes Agent Git checkout")
    args = parser.parse_args()
    supplied_checkout = args.checkout.expanduser()
    sensitive: tuple[str | Path, ...] = (supplied_checkout,)

    git = shutil.which("git")
    if not git:
        _emit("git executable not found", sensitive_paths=sensitive, error=True)
        return 1
    try:
        checkout = supplied_checkout.resolve()
    except OSError:
        _emit(
            "Update Git diagnostic cannot resolve checkout path",
            sensitive_paths=sensitive,
            error=True,
        )
        return 1
    sensitive = (*sensitive, checkout)
    if not checkout.is_dir():
        _emit(
            f"checkout is not a directory: {checkout}",
            sensitive_paths=sensitive,
            error=True,
        )
        return 1

    env = clean_git_env()
    credential_config = trusted_git_credential_config(
        checkout,
        env,
        executable=git,
    )
    try:
        origin = _run(
            ["remote", "get-url", "origin"],
            checkout,
            git,
            env,
            credential_config,
        )
        if origin.returncode != 0:
            _emit(
                "Update Git diagnostic failed: origin is not configured or cannot be read",
                sensitive_paths=sensitive,
                error=True,
            )
            return 1

        origin_url = (origin.stdout or "").strip()
        if not is_safe_diagnostic_remote(origin_url):
            _emit(
                "Update Git diagnostic failed: origin uses an unsupported or malformed transport",
                sensitive_paths=sensitive,
                error=True,
            )
            return 1
        if repository_git_proxy_blocks(["fetch", "origin"], checkout, env, executable=git):
            _emit(
                "Update Git diagnostic failed: repository-configured core.gitProxy is not allowed",
                sensitive_paths=sensitive, error=True,
            )
            return 1
        env = noninteractive_git_env(
            checkout, env, executable=git, args=["ls-remote", origin_url],
        )
        origin_path = urlsplit(origin_url).path
        sensitive = (*sensitive, origin_url, origin_path)

        # Probe the captured URL outside the checkout. This keeps repository-local
        # url rewrites, remote.<name>.uploadpack, and other executable overrides
        # out of the read-only diagnostic while retaining trusted user/system
        # credential helpers for private HTTPS origins.
        with tempfile.TemporaryDirectory(prefix="hermes-update-git-diagnostic-") as probe_dir:
            probe = _run(
                ["ls-remote", origin_url],
                Path(probe_dir),
                git,
                env,
                credential_config,
            )
    except subprocess.TimeoutExpired:
        _emit(
            "Update Git diagnostic failed: Git command timed out after 30s",
            sensitive_paths=sensitive,
            error=True,
        )
        return 1
    except OSError:
        _emit(
            "Update Git diagnostic failed: could not start Git",
            sensitive_paths=sensitive,
            error=True,
        )
        return 1

    if probe.returncode != 0:
        _emit(
            "Update Git diagnostic failed: origin is unreachable or authentication failed",
            sensitive_paths=sensitive,
            error=True,
        )
        return 1

    _emit(
        "Update Git diagnostic succeeded: origin is reachable without prompting.",
        sensitive_paths=sensitive,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
