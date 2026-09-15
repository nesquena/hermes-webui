"""Dependency-light helpers for launching child processes consistently."""

from __future__ import annotations

import os
import subprocess
import sys


def windows_hide_flags() -> int:
    """Hide a short-lived console child on Win32 and remain a POSIX no-op.

    ``CREATE_NO_WINDOW`` keeps captured stdout and stderr connected, unlike
    detaching the process. Passing ``0`` elsewhere preserves the subprocess
    default. See #5692.
    """
    if sys.platform == "win32":
        return getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return 0


# Environment variables git honours that let a WebUI-spawned child do something
# other than what the caller asked: askpass and proxy entries turn a fail-closed
# error into an interactive prompt or arbitrary helper command, and the
# GIT_DIR/GIT_WORK_TREE/config entries point git at a different repository,
# index, or config than the one the caller passed as cwd.
GIT_ENV_SCRUB_KEYS = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_CONFIG_GLOBAL",
    "GIT_CONFIG_SYSTEM",
    "GIT_CONFIG_COUNT",
    "GIT_CONFIG_PARAMETERS",
    "GIT_ASKPASS",
    "SSH_ASKPASS",
    "GIT_SSH",
    "GIT_SSH_COMMAND",
    "GIT_PROXY_COMMAND",
)
GIT_ENV_SCRUB_PREFIXES = ("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")
GIT_NONINTERACTIVE_CONFIG = (
    ("core.askPass", ""),
    ("credential.helper", ""),
    ("core.sshCommand", "ssh -oBatchMode=yes"),
    # Refuse the two transports that run a command named by repository data: the
    # ext:: transport runs a local command named by the URL, and a git:// remote
    # can select a repository-configured ``core.gitProxy`` command.
    #
    # The git:// refusal is a transport restriction rather than a proxy override
    # because no command-line value can neutralize that key: ``core.gitProxy`` is
    # per-host and multi-valued, and measured against a repository-configured
    # proxy, both ``-c core.gitProxy=`` and ``-c core.gitProxy=none`` still let the
    # configured command run. It applies to every Git child, including workspace
    # fetch/pull/push, because a workspace repository is just as likely to be
    # supplied by an agent, a restored session, or a mount as the checkout is.
    # The cost is that a deliberately configured git:// workspace remote no longer
    # resolves; HTTPS and SSH remotes are unaffected.
    ("protocol.ext.allow", "never"),
    ("protocol.git.allow", "never"),
)


def noninteractive_git_argv(
    args: list[str], *, executable: str = "git",
) -> list[str]:
    """Build Git argv that disables prompts and external transport helpers.

    SSH transports run with ``BatchMode=yes`` so they use an available agent or
    fail instead of reading a password, key passphrase, or host-key answer from
    the WebUI process's controlling terminal. The ``ext::`` and ``git://``
    transports are refused; see ``GIT_NONINTERACTIVE_CONFIG``.
    """
    argv = [executable]
    for key, value in GIT_NONINTERACTIVE_CONFIG:
        argv.extend(["-c", f"{key}={value}"])
    argv.extend(args)
    return argv


def clean_git_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Return an environment without inherited Git prompts, proxies, or redirections.

    ``GIT_TERMINAL_PROMPT=0`` makes a command that needs credentials fail instead
    of waiting on a terminal, but git consults ``GIT_ASKPASS``/``SSH_ASKPASS``
    *before* it honours that, so the askpass entries have to be removed as well.
    Otherwise a desktop session's askpass helper (`ksshaskpass`, say) opens a
    modal credential dialog when a background check fetches a remote that answers
    401, and the caller blocks until the fetch times out.
    """
    env = os.environ.copy()
    if extra:
        env.update(extra)
    for key in GIT_ENV_SCRUB_KEYS:
        env.pop(key, None)
    for key in list(env):
        if key.startswith(GIT_ENV_SCRUB_PREFIXES):
            env.pop(key, None)
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env
