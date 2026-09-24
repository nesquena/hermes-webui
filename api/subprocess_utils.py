"""Dependency-light helpers for launching child processes consistently."""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import sys
from pathlib import Path
from urllib.parse import unquote, urlsplit


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
    "GIT_SSH_VARIANT",
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
_URL_REMOTE_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*://")
_REMOTE_HELPER_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*::")
_SCP_SSH_REMOTE_RE = re.compile(
    r"^(?:[^/@:\s]+@)?(?:\[[^\[\]/\s]+\]|[^/@:\s]+):(?!:).+$"
)
_SHELL_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")



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


def _scoped_git_config_entries(
    cwd: str | Path,
    env: dict[str, str],
    pattern: str,
    *,
    executable: str,
) -> tuple[tuple[str, str, str], ...]:
    """Read ``(scope, key, value)`` triples matching a Git config regex."""
    try:
        result = subprocess.run(
            [
                executable, "config", "--includes", "--show-scope", "-z",
                "--get-regexp", pattern,
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
    if len(raw_fields) % 2:
        return ()
    entries = []
    for raw_scope, raw_entry in zip(raw_fields[0::2], raw_fields[1::2], strict=True):
        if b"\n" not in raw_entry:
            return ()
        raw_key, raw_value = raw_entry.split(b"\n", 1)
        entries.append(
            (
                raw_scope.decode("utf-8", errors="replace"),
                raw_key.decode("utf-8", errors="replace"),
                raw_value.decode("utf-8", errors="replace"),
            )
        )
    return tuple(entries)


def trusted_git_credential_config(
    cwd: str | Path,
    env: dict[str, str],
    *,
    executable: str = "git",
) -> tuple[tuple[str, str], ...]:
    """Read credential-helper entries from trusted system and user scopes.

    This preserves both generic ``credential.helper`` entries and URL-scoped
    entries such as ``credential.https://github.com.helper``. Repository and
    worktree config are deliberately excluded. Git performs its normal URL
    matching after the trusted entries are re-applied on the command line.
    """
    return tuple(
        (key, value)
        for scope, key, value in _scoped_git_config_entries(
            cwd,
            env,
            r"^credential(\..*)?\.helper$",
            executable=executable,
        )
        if scope in {"system", "global"}
    )


def noninteractive_git_env(
    cwd: str | Path,
    env: dict[str, str],
    *,
    executable: str = "git",
) -> dict[str, str]:
    """Force SSH batch mode while preserving a trusted custom SSH command."""
    trusted_commands = tuple(
        value
        for scope, value in _scoped_git_config_values(
            cwd, env, "core.sshCommand", executable=executable,
        )
        if scope in {"system", "global"}
    )
    trusted_variants = tuple(
        value
        for scope, value in _scoped_git_config_values(
            cwd, env, "ssh.variant", executable=executable,
        )
        if scope in {"system", "global"}
    )
    ssh_command = trusted_commands[-1] if trusted_commands else "ssh"
    try:
        command_words = shlex.split(ssh_command, posix=True)
    except ValueError:
        command_words = []
    variant = trusted_variants[-1].strip().lower() if trusted_variants else "auto"
    if variant == "auto":
        # Git interprets core.sshCommand as a shell command on every platform.
        executable_words = list(command_words)
        while executable_words and _SHELL_ASSIGNMENT_RE.match(executable_words[0]):
            executable_words.pop(0)
        if executable_words and executable_words[0] in {"command", "exec"}:
            executable_words.pop(0)
            while executable_words and executable_words[0].startswith("-"):
                executable_words.pop(0)
        if executable_words and executable_words[0] == "env":
            executable_words.pop(0)
            while executable_words and (
                executable_words[0].startswith("-")
                or _SHELL_ASSIGNMENT_RE.match(executable_words[0])
            ):
                executable_words.pop(0)
        executable_name = executable_words[0] if executable_words else ""
        executable_name = executable_name.strip("\"'").replace("\\", "/").rsplit("/", 1)[-1]
        executable_name = executable_name.lower().removesuffix(".exe")
        variant = {
            "ssh": "ssh",
            "plink": "plink",
            "putty": "putty",
            "tortoiseplink": "tortoiseplink",
        }.get(executable_name, "unsupported")
    if variant not in {"ssh", "plink", "putty", "tortoiseplink"}:
        # Git's "simple" variant and unknown custom transports have no general
        # non-interactive flag. Do not run one when the no-prompt invariant
        # cannot be established.
        ssh_command = "git-ssh-variant-is-not-supported-by-hermes-webui"
        variant = "simple"
        batch_option = ""
    else:
        batch_option = (
            "-batch"
            if variant in {"plink", "putty", "tortoiseplink"}
            else "-oBatchMode=yes"
        )
    if variant == "ssh":
        normalized_words = [word.lower().replace(" ", "") for word in command_words]
        for index, word in enumerate(normalized_words):
            if word in {"-obatchmode=no", "-obatchmode=false"}:
                ssh_command = "git-ssh-command-disables-batch-mode"
                variant = "simple"
                batch_option = ""
                break
            if word == "-o" and index + 1 < len(normalized_words):
                if normalized_words[index + 1] in {"batchmode=no", "batchmode=false"}:
                    ssh_command = "git-ssh-command-disables-batch-mode"
                    variant = "simple"
                    batch_option = ""
                    break
    if batch_option:
        ssh_command = f"{ssh_command} {batch_option}"
    configured = dict(env)
    configured["GIT_SSH_COMMAND"] = ssh_command
    configured["GIT_SSH_VARIANT"] = variant
    return configured


def noninteractive_git_argv(
    args: list[str],
    *,
    executable: str = "git",
    credential_config: tuple[tuple[str, str], ...] = (),
) -> list[str]:
    """Build Git argv that cannot prompt or run checkout-controlled helpers."""
    argv = [executable]
    for key, value in (
        ("core.askPass", ""),
        ("protocol.ext.allow", "never"),
        ("credential.helper", ""),
    ):
        argv.extend(["-c", f"{key}={value}"])
    for key, value in credential_config:
        argv.extend(["-c", f"{key}={value}"])
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
    proxy_values = _scoped_git_config_values(
        cwd, env, "core.gitProxy", executable=executable,
    )
    if not any(scope in {"local", "worktree"} for scope, _value in proxy_values):
        return False
    url = _remote_url_for_command(args, cwd, env, executable=executable)
    if url == "":
        return False
    # A local proxy plus an unresolvable active URL is not safe to pass through:
    # the subsequent network command may resolve more successfully and execute it.
    if url is None:
        return True
    if not url.lower().startswith("git://"):
        return False
    try:
        # Git matches core.gitProxy's DOMAIN against parse_connect_url()'s
        # URL-decoded hostandport, before brackets and an explicit port are
        # removed for the connection. Preserve their spelling and case here.
        hostandport = unquote(urlsplit(url).netloc)
    except (UnicodeError, ValueError):
        return True
    if not hostandport:
        return True

    # Git considers all scopes in config order and selects the first value whose
    # optional ``for DOMAIN`` suffix matches the complete hostandport or a suffix
    # following a dot. Only reject when that selected value came from the repo.
    selected_proxy: tuple[str, str] | None = None
    for scope, value in proxy_values:
        for_pos = value.find(" for ")
        if for_pos < 0:
            selected_proxy = (scope, value)
            break
        domain = value[for_pos + 5 :]
        suffix_start = len(hostandport) - len(domain)
        if (
            suffix_start >= 0
            and hostandport.endswith(domain)
            and (suffix_start == 0 or hostandport[suffix_start - 1] == ".")
        ):
            selected_proxy = (scope, value[:for_pos])
            break
    if selected_proxy is None:
        return False
    scope, command = selected_proxy
    return scope in {"local", "worktree"} and command not in {"", "none"}


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
    if _REMOTE_HELPER_RE.match(value):
        return False
    if _URL_REMOTE_RE.match(value):
        try:
            parsed = urlsplit(value)
            hostname = parsed.hostname
            _port = parsed.port
        except ValueError:
            return False
        return bool(hostname) and parsed.scheme.lower() in {"http", "https", "ssh"}
    return _SCP_SSH_REMOTE_RE.match(value) is not None
