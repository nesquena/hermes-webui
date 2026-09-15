"""Contract tests for the shared test helpers."""

from __future__ import annotations

import pytest

from api.subprocess_utils import noninteractive_git_argv
from tests.helpers import git_subcommand_args


def test_git_subcommand_args_ignores_the_noninteractive_hardening_prefix():
    """A stub dispatching on the subcommand must not have to know the prefix."""
    argv = noninteractive_git_argv(
        ['fetch', 'origin'], executable='/usr/bin/git', unattended=True,
    )

    assert git_subcommand_args(argv) == ['fetch', 'origin']


def test_git_subcommand_args_matches_the_unattended_prefix_only():
    """The helper must describe the argv update checks actually build: a
    non-unattended argv is not what ``api.updates._run_git`` produces."""
    argv = noninteractive_git_argv(['fetch', 'origin'], executable='/usr/bin/git')

    with pytest.raises(AssertionError, match='hardening prefix'):
        git_subcommand_args(argv)


def test_git_subcommand_args_fails_loudly_when_the_argv_is_not_hardened():
    """A runner that stops hardening its argv must not silently match stubs."""
    with pytest.raises(AssertionError, match='hardening prefix'):
        git_subcommand_args(['/usr/bin/git', 'fetch', 'origin'])
