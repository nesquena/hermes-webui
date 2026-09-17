"""Dependency-light helpers for launching child processes consistently."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit


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
# other than what the caller asked: the askpass/proxy entries turn a fail-closed
# error into an interactive prompt or helper command, and the GIT_DIR/work-tree/
# config entries point Git at state other than the checkout supplied by the caller.
GIT_ENV_SCRUB_KEYS = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_CONFIG",
    "GIT_CONFIG_GLOBAL",
    "GIT_CONFIG_SYSTEM",
    "GIT_CONFIG_NOSYSTEM",
    "GIT_CONFIG_COUNT",
    "GIT_CONFIG_PARAMETERS",
    "GIT_ASKPASS",
    "SSH_ASKPASS",
    "GIT_SSH",
    "GIT_SSH_COMMAND",
    "GIT_PROXY_COMMAND",
)
GIT_ENV_SCRUB_PREFIXES = ("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")

_CREDENTIAL_IN_URL_RE = re.compile(r"([a-zA-Z][a-zA-Z0-9+.-]*://)([^/@\s'\"]+)@")
_GITHUB_TOKEN_RE = re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,})\b")
_QUERY_SECRET_RE = re.compile(
    r"([?&](?:access_token|oauth_token|private_token|client_secret|app_secret|"
    r"api[_-]?key|token|password|secret|auth|key)=)[^&\s'\"]+",
    re.IGNORECASE,
)


def clean_git_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Return a Git environment without inherited prompts or redirections.

    ``GIT_TERMINAL_PROMPT=0`` prevents Git's built-in terminal prompt, but Git
    consults inherited askpass helpers first, so those entries must be removed.
    ``SSH_AUTH_SOCK`` is deliberately retained so non-interactive SSH agent
    authentication continues to work.
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


def _scoped_git_config_values(
    cwd: str | Path,
    env: dict[str, str],
    key: str,
    *,
    executable: str,
) -> tuple[tuple[str, str], ...]:
    try:
        result = subprocess.run(
            [
                executable, "config", "--includes", "--show-scope", "-z",
                "--get-all", key,
            ],
            cwd=str(cwd), shell=False, capture_output=True, timeout=10,
            env=env, creationflags=windows_hide_flags(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return ()
    if result.returncode != 0:
        return ()
    raw_output = result.stdout or b""
    if isinstance(raw_output, str):
        raw_output = raw_output.encode("utf-8", errors="replace")
    raw_fields = raw_output.split(b"\0")
    if raw_fields and raw_fields[-1] == b"":
        raw_fields.pop()
    fields = [value.decode("utf-8", errors="replace") for value in raw_fields]
    if len(fields) % 2:
        return ()
    return tuple(zip(fields[0::2], fields[1::2], strict=True))


def trusted_git_credential_helpers(
    cwd: str | Path,
    env: dict[str, str],
    *,
    executable: str = "git",
) -> tuple[str, ...]:
    """Read credential helpers from trusted system and user config scopes.

    Repository and worktree config are deliberately excluded. The returned
    values can be re-applied after an empty ``credential.helper`` value resets
    Git's accumulated helper list, preserving normal private-HTTPS access while
    preventing a checkout from supplying an executable helper.
    """
    return tuple(
        value
        for scope, value in _scoped_git_config_values(
            cwd, env, "credential.helper", executable=executable,
        )
        if scope in {"system", "global"}
    )


def noninteractive_git_argv(
    args: list[str],
    *,
    executable: str = "git",
    credential_helpers: tuple[str, ...] = (),
) -> list[str]:
    """Build Git argv that cannot prompt or run checkout-controlled helpers."""
    argv = [executable]
    for key, value in (
        ("core.askPass", ""),
        ("core.sshCommand", "ssh -oBatchMode=yes"),
        ("protocol.ext.allow", "never"),
        ("credential.helper", ""),
    ):
        argv.extend(["-c", f"{key}={value}"])
    for helper in credential_helpers:
        argv.extend(["-c", f"credential.helper={helper}"])
    argv.extend(args)
    return argv


def _git_config_value(
    cwd: str | Path,
    env: dict[str, str],
    key: str,
    *,
    executable: str,
) -> str | None:
    try:
        result = subprocess.run(
            [executable, "config", "--get", key],
            cwd=str(cwd),
            shell=False,
            capture_output=True,
            text=True,
            timeout=10,
            env=env,
            creationflags=windows_hide_flags(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return (result.stdout or "").strip() or None


def _explicit_remote_arg(args: list[str]) -> str | None:
    if not args or args[0] not in {"fetch", "pull", "push", "ls-remote"}:
        return None
    consumes_value = {
        "--depth", "--deepen", "--shallow-since", "--shallow-exclude",
        "--negotiation-tip", "--upload-pack", "--receive-pack", "--server-option",
    }
    skip_next = False
    for value in args[1:]:
        if skip_next:
            skip_next = False
            continue
        if value in consumes_value:
            skip_next = True
            continue
        if value.startswith("-"):
            continue
        return value
    return None


def _remote_url_for_command(
    args: list[str],
    cwd: str | Path,
    env: dict[str, str],
    *,
    executable: str,
) -> str | None:
    remote = _explicit_remote_arg(args)
    if not remote:
        branch = None
        # The WebUI callers use origin when no explicit remote is present. Honor
        # an explicit current-branch remote before falling back to origin.
        try:
            head = subprocess.run(
                [executable, "symbolic-ref", "--quiet", "--short", "HEAD"],
                cwd=str(cwd), shell=False, capture_output=True, text=True,
                timeout=10, env=env, creationflags=windows_hide_flags(),
            )
        except (OSError, subprocess.TimeoutExpired):
            head = None
        if head is not None and head.returncode == 0:
            branch_name = (head.stdout or "").strip()
            branch = _git_config_value(
                cwd, env, f"branch.{branch_name}.remote", executable=executable,
            ) or branch
        remote = branch or "origin"
    if remote == ".":
        return ""
    # --get-url performs only Git's configured URL rewrite; it does not contact
    # the remote. This exposes a repo-local url.*.insteadOf that turns an
    # apparently HTTPS remote into git:// before the proxy guard decides.
    try:
        resolved = subprocess.run(
            [executable, "ls-remote", "--get-url", remote],
            cwd=str(cwd), shell=False, capture_output=True, text=True,
            timeout=10, env=env, creationflags=windows_hide_flags(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if resolved.returncode != 0:
        return None
    return (resolved.stdout or "").strip() or None


def repository_git_proxy_blocks(
    args: list[str],
    cwd: str | Path,
    env: dict[str, str],
    *,
    executable: str = "git",
) -> bool:
    """Return whether repo config could proxy this command's git:// remote.

    ``core.gitProxy`` is multi-valued and a command-line empty value does not
    mask a lower-scope entry. Deny only a git:// network operation whose active
    checkout supplies that key; legitimate git:// remotes without such a local
    override remain usable.
    """
    if not args or args[0] not in {"fetch", "pull", "push", "ls-remote"}:
        return False
    local_proxy_values = [
        value
        for scope, value in _scoped_git_config_values(
            cwd, env, "core.gitProxy", executable=executable,
        )
        if scope in {"local", "worktree"}
    ]
    has_local_proxy = any(
        not value.strip() or value.split(maxsplit=1)[0].lower() != "none"
        for value in local_proxy_values
    )
    if not has_local_proxy:
        return False
    url = _remote_url_for_command(args, cwd, env, executable=executable)
    if url == "":
        return False
    # A local proxy plus an unresolvable active URL is not safe to pass through:
    # the subsequent network command may resolve more successfully and execute it.
    return url is None or url.lower().startswith("git://")


def sanitize_git_diagnostic(
    output: str,
    *,
    sensitive_paths: tuple[str | Path, ...] = (),
    limit: int = 300,
) -> str:
    """Remove credentials and caller-named private paths from Git diagnostics."""
    if not output:
        return ""
    sanitized = str(output)
    for path in sorted((str(value) for value in sensitive_paths if value), key=len, reverse=True):
        sanitized = sanitized.replace(path, "<redacted-path>")
        try:
            sanitized = sanitized.replace(str(Path(path).expanduser().resolve()), "<redacted-path>")
        except (OSError, ValueError):
            pass
    sanitized = _CREDENTIAL_IN_URL_RE.sub(r"\1<redacted>@", sanitized)
    sanitized = _GITHUB_TOKEN_RE.sub("<redacted>", sanitized)
    sanitized = _QUERY_SECRET_RE.sub(r"\1<redacted>", sanitized)
    sanitized = sanitized.strip()
    if len(sanitized) > limit:
        sanitized = sanitized[:limit].rstrip() + "…"
    return sanitized


def is_safe_diagnostic_remote(remote: str) -> bool:
    """Accept only built-in network transports that cannot name remote helpers."""
    value = remote.strip()
    if re.match(r"^[^/@:\s]+@[^/:\s]+:.+", value):
        return True
    scheme = urlsplit(value).scheme.lower()
    return scheme in {"http", "https", "ssh"}
