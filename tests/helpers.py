"""Shared test helpers."""

from __future__ import annotations

from typing import Sequence

from api.subprocess_utils import noninteractive_git_argv


def source_between(src: str, start_marker: str, end_marker: str) -> str:
    start = src.find(start_marker)
    assert start >= 0, f"{start_marker} not found"
    end = src.find(end_marker, start)
    assert end > start, f"{end_marker} not found after {start_marker}"
    return src[start:end]


def git_subcommand_args(cmd: Sequence[str]) -> list[str]:
    """Return the git arguments in an argv built by a production git runner.

    ``api.updates._run_git`` builds its argv through
    ``api.subprocess_utils.noninteractive_git_argv(..., unattended=True)``, which
    prefixes the executable with ``-c`` overrides: credential helpers, SSH batch
    mode, and the transport refusals that only an unattended child applies. A
    stub that dispatches on the subcommand would otherwise have to hard-code that
    prefix and would break every time it changes, so derive it from the production
    helper instead — and assert it is present, so a runner that stops hardening
    its argv fails here rather than silently matching a stub that no longer
    describes it.
    """
    prefix = noninteractive_git_argv([], executable=cmd[0])
    assert list(cmd[: len(prefix)]) == prefix, (
        f'git argv is missing the noninteractive hardening prefix: {list(cmd)!r}'
    )
    return list(cmd[len(prefix):])
