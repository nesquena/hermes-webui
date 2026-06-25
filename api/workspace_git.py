"""Git helpers for the workspace panel.

The browser only sends session ids and workspace-relative paths.  This module
resolves the active workspace server-side, scopes paths before they become Git
pathspecs, and keeps all Git subprocess calls shell-free and bounded.
"""

from __future__ import annotations

import difflib
import logging
import os
import shutil
import subprocess
import tempfile
import threading
import re
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

logger = logging.getLogger(__name__)

from api.workspace import rmtree_anchored, safe_resolve_ws, unlink_anchored


GIT_TIMEOUT = 5
GIT_REMOTE_TIMEOUT = 60
STATUS_FILE_LIMIT = 500
DIFF_SIZE_LIMIT = 512 * 1024
COMMIT_MESSAGE_DIFF_LIMIT = 64 * 1024
WORKSPACE_GIT_DESTRUCTIVE_ENV = "HERMES_WEBUI_WORKSPACE_GIT_DESTRUCTIVE"
_GIT_ENV_SCRUB_KEYS = (
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
)
_GIT_ENV_SCRUB_PREFIXES = ("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")
_HERMES_BRANCH_SWITCH_STASH_PREFIX = "hermes-webui branch switch"
_GIT_HARDENED_CONFIG = (
    # Workspace Git operations can run against repositories provided by agents,
    # restored sessions, or mounted workspaces. Keep repo-local configuration
    # from turning read/status/fetch calls into host command execution.
    ("core.fsmonitor", "false"),
    # Force the unmodified system ssh binary rather than clearing it — an empty
    # value would break legitimate ssh fetches, while "ssh" overrides any
    # repo-local core.sshCommand that points at an attacker helper.
    ("core.sshCommand", "ssh"),
    ("core.askPass", ""),
    ("credential.helper", ""),
    ("protocol.ext.allow", "never"),
    # Neutralize repo-local core.gitProxy, which specifies an external proxy
    # command reachable on `git fetch` against a git:// remote.
    ("core.gitProxy", ""),
    # Prevent submodule operations from recursing into nested repos, which
    # could trigger hooks or fetch from attacker-controlled submodule URLs.
    ("submodule.recurse", "false"),
    ("fetch.recurseSubmodules", "false"),
)
_GIT_DESTRUCTIVE_HARDENED_CONFIG = (
    # Disable signing helper command resolution while performing destructive
    # Git operations. Hooks are redirected to a temporary empty directory in
    # _run_git() so Git never falls back to .git/hooks.
    ("commit.gpgSign", "false"),
    ("push.gpgSign", "false"),
    ("gpg.program", ""),
    ("gpg.ssh.program", ""),
    ("gpg.x509.program", ""),
    ("core.alternateRefsCommand", ""),
)


def _hardened_git_argv(
    args: list[str],
    *,
    destructive: bool = False,
    attributes_file: str | None = None,
    hooks_path: str | None = None,
) -> list[str]:
    argv = ["git"]
    for key, value in _GIT_HARDENED_CONFIG:
        argv.extend(["-c", f"{key}={value}"])
    if destructive:
        for key, value in _GIT_DESTRUCTIVE_HARDENED_CONFIG:
            argv.extend(["-c", f"{key}={value}"])
        if hooks_path:
            argv.extend(["-c", f"core.hooksPath={hooks_path}"])
    if attributes_file:
        argv.extend(["-c", f"core.attributesFile={attributes_file}"])
    argv.extend(args)
    return argv


def workspace_git_destructive_enabled() -> bool:
    return os.getenv(WORKSPACE_GIT_DESTRUCTIVE_ENV, "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _clean_git_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = os.environ.copy()
    if extra:
        env.update(extra)
    for key in _GIT_ENV_SCRUB_KEYS:
        env.pop(key, None)
    for key in list(env):
        if key.startswith(_GIT_ENV_SCRUB_PREFIXES):
            env.pop(key, None)
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


class GitWorkspaceError(RuntimeError):
    """User-facing Git operation error."""

    def __init__(self, message: str, code: str = "git_failed"):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class GitContext:
    workspace: Path
    repo_root: Path
    workspace_prefix: str


_LOCKS_GUARD = threading.Lock()
_OP_LOCKS: dict[str, threading.Lock] = {}


@contextmanager
def _git_mutation_lock(ctx: GitContext):
    # Key by repo root so sessions in the same repository serialize mutations.
    # Separate worktrees get separate locks; Git still protects shared metadata
    # with its own locks.
    key = str(ctx.repo_root)
    with _LOCKS_GUARD:
        lock = _OP_LOCKS.setdefault(key, threading.Lock())
    if not lock.acquire(timeout=GIT_REMOTE_TIMEOUT):
        raise GitWorkspaceError("Another Git operation is still running", "operation_in_progress")
    try:
        yield
    finally:
        lock.release()


def _classify_git_error(message: str, args: list[str] | None = None) -> str:
    text = (message or "").lower()
    joined = " ".join(args or []).lower()
    if "timed out" in text:
        return "timeout"
    if "not installed" in text or "no such file or directory: 'git'" in text:
        return "missing_git"
    if "not a git repository" in text:
        return "not_a_repo"
    if "outside the workspace" in text or "outside the git repository" in text:
        return "path_outside_workspace"
    if "authentication failed" in text or "permission denied" in text or "could not read username" in text:
        return "auth_failed"
    if "no upstream" in text or "no configured push destination" in text or "has no upstream branch" in text:
        return "no_upstream"
    if (
        "non-fast-forward" in text
        or "fetch first" in text
        or ("rejected" in text and "push" in joined)
    ):
        return "non_fast_forward"
    if "conflict" in text or "unmerged" in text or ("merge" in text and "needs" in text):
        return "conflict"
    if "working tree" in text and ("clean" in text or "dirty" in text):
        return "dirty_worktree"
    if "local changes" in text or "would be overwritten by checkout" in text:
        return "dirty_worktree"
    if "invalid reference" in text or "not a valid" in text or "unknown revision" in text:
        return "invalid_ref"
    if "hook" in text:
        return "hook_failed"
    return "git_failed"


def _run_git(
    ctx_or_cwd: GitContext | Path,
    args: list[str],
    *,
    timeout: int = GIT_TIMEOUT,
    check: bool = False,
    env: dict[str, str] | None = None,
    destructive: bool = False,
    force_destructive_hardening: bool = False,
    disable_filter_attributes: bool = False,
    neutralize_filter_programs: bool = False,
    neutralize_remote_helpers: bool = False,
) -> subprocess.CompletedProcess[str]:
    cwd = ctx_or_cwd.repo_root if isinstance(ctx_or_cwd, GitContext) else ctx_or_cwd
    run_env = _clean_git_env(env)
    effective_destructive = destructive and workspace_git_destructive_enabled()
    hardened_destructive_path = effective_destructive or force_destructive_hardening
    attributes_file = None
    hooks_path = None
    extra_configs: list[tuple[str, str]] = []
    temporary_attributes: list[str] = []
    temporary_dirs: list[str] = []
    try:
        if disable_filter_attributes:
            fd, attributes_path = tempfile.mkstemp(prefix="hermes-webui-git-attrs-")
            os.close(fd)
            attributes_file = attributes_path
            temporary_attributes = [attributes_path]
        if disable_filter_attributes or neutralize_filter_programs:
            # Read/status/fetch paths treat repo-local filter programs as
            # untrusted code. Prefer raw-byte visibility over executing them.
            extra_configs.extend(_destructive_filter_overrides(cwd, run_env))
        if effective_destructive:
            extra_configs.extend(_destructive_merge_driver_overrides(cwd, run_env))
        if effective_destructive or neutralize_remote_helpers:
            extra_configs.extend(_destructive_remote_helper_overrides(cwd, run_env))
            args = _destructive_remote_command_args(args, cwd, run_env)
        if hardened_destructive_path:
            hooks_path = tempfile.mkdtemp(prefix="hermes-webui-git-hooks-")
            temporary_dirs = [hooks_path]
        if extra_configs:
            run_env["GIT_CONFIG_COUNT"] = str(len(extra_configs))
            for i, (key, value) in enumerate(extra_configs):
                run_env[f"GIT_CONFIG_KEY_{i}"] = key
                run_env[f"GIT_CONFIG_VALUE_{i}"] = value
        result = subprocess.run(
            _hardened_git_argv(
                args,
                destructive=hardened_destructive_path,
                attributes_file=attributes_file,
                hooks_path=hooks_path,
            ),
            cwd=str(cwd),
            shell=False,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=run_env,
        )
    except subprocess.TimeoutExpired as exc:
        raise GitWorkspaceError("Git command timed out", "timeout") from exc
    except FileNotFoundError as exc:
        raise GitWorkspaceError("Git is not installed or not available on PATH", "missing_git") from exc
    except OSError as exc:
        raise GitWorkspaceError(str(exc), _classify_git_error(str(exc), args)) from exc
    finally:
        for path in temporary_attributes:
            Path(path).unlink(missing_ok=True)
        for path in temporary_dirs:
            shutil.rmtree(path, ignore_errors=True)
    if check and result.returncode != 0:
        message = (result.stderr or result.stdout or "Git command failed").strip()
        raise GitWorkspaceError(message, _classify_git_error(message, args))
    return result


_FILTER_CONFIG_RE = re.compile(r"^filter\.(.+)\.(clean|smudge|process|required)$")


_MERGE_DRIVER_CONFIG_RE = re.compile(r"^merge\.(.+)\.driver$")
_REMOTE_HELPER_CONFIG_RE = re.compile(r"^remote\.(.+)\.(uploadpack|receivepack)$")


def _config_names_for_scope(
    scope: str,
    cwd: Path,
    env: dict[str, str],
    config_pattern: str,
    name_re: re.Pattern[str],
    *,
    ignore_unsupported: bool = False,
) -> set[str]:
    result = subprocess.run(
        ["git", "config", "--includes", scope, "--name-only", "--get-regexp", config_pattern],
        cwd=str(cwd),
        shell=False,
        text=True,
        capture_output=True,
        timeout=GIT_TIMEOUT,
        env=env,
    )
    if result.returncode not in {0, 1}:
        if ignore_unsupported:
            return set()
        message = (result.stderr or result.stdout or "Git command failed").strip()
        raise GitWorkspaceError(message, _classify_git_error(message, ["config"]))
    names: set[str] = set()
    for line in (result.stdout or "").splitlines():
        match = name_re.match(line.strip())
        if match:
            names.add(match.group(1))
    return names


def _filter_names_for_scope(
    scope: str,
    cwd: Path,
    env: dict[str, str],
    *,
    ignore_unsupported: bool = False,
) -> set[str]:
    return _config_names_for_scope(
        scope,
        cwd,
        env,
        r"^filter\..*\.(clean|smudge|process|required)$",
        _FILTER_CONFIG_RE,
        ignore_unsupported=ignore_unsupported,
    )


def _merge_driver_names_for_scope(
    scope: str,
    cwd: Path,
    env: dict[str, str],
    *,
    ignore_unsupported: bool = False,
) -> set[str]:
    return _config_names_for_scope(
        scope,
        cwd,
        env,
        r"^merge\..*\.driver$",
        _MERGE_DRIVER_CONFIG_RE,
        ignore_unsupported=ignore_unsupported,
    )


def _remote_helper_names_for_scope(
    scope: str,
    cwd: Path,
    env: dict[str, str],
    *,
    ignore_unsupported: bool = False,
) -> set[str]:
    return _config_names_for_scope(
        scope,
        cwd,
        env,
        r"^remote\..*\.(uploadpack|receivepack)$",
        _REMOTE_HELPER_CONFIG_RE,
        ignore_unsupported=ignore_unsupported,
    )


def _destructive_filter_overrides(cwd: Path, env: dict[str, str]) -> list[tuple[str, str]]:
    names = _filter_names_for_scope("--local", cwd, env)
    names |= _filter_names_for_scope(
        "--worktree",
        cwd,
        env,
        ignore_unsupported=True,
    )
    overrides: list[tuple[str, str]] = []
    for name in sorted(names):
        if "\n" in name or "\0" in name:
            logger.warning("Skipping filter name with illegal characters: %r", name)
            continue
        overrides.extend(
            [
                (f"filter.{name}.clean", "cat"),
                (f"filter.{name}.smudge", "cat"),
                (f"filter.{name}.process", ""),
                (f"filter.{name}.required", "false"),
            ]
        )
    return overrides


def _destructive_merge_driver_overrides(cwd: Path, env: dict[str, str]) -> list[tuple[str, str]]:
    names = _merge_driver_names_for_scope("--local", cwd, env)
    names |= _merge_driver_names_for_scope(
        "--worktree",
        cwd,
        env,
        ignore_unsupported=True,
    )
    # Replace repo-defined merge drivers with Git's trusted three-way merge
    # binary so stash restores cannot invoke workspace-controlled helpers.
    overrides: list[tuple[str, str]] = []
    for name in sorted(names):
        if "\n" in name or "\0" in name:
            logger.warning("Skipping merge driver name with illegal characters: %r", name)
            continue
        overrides.append((f"merge.{name}.driver", 'git merge-file "%A" "%O" "%B"'))
    return overrides


def _destructive_remote_helper_overrides(cwd: Path, env: dict[str, str]) -> list[tuple[str, str]]:
    names = _remote_helper_names_for_scope("--local", cwd, env)
    names |= _remote_helper_names_for_scope(
        "--worktree",
        cwd,
        env,
        ignore_unsupported=True,
    )
    overrides: list[tuple[str, str]] = []
    for name in sorted(names):
        if "\n" in name or "\0" in name:
            logger.warning("Skipping remote helper name with illegal characters: %r", name)
            continue
        overrides.extend(
            [
                (f"remote.{name}.uploadpack", "git-upload-pack"),
                (f"remote.{name}.receivepack", "git-receive-pack"),
            ]
        )
    return overrides


def _destructive_remote_command_args(args: list[str], cwd: Path, env: dict[str, str]) -> list[str]:
    if not args:
        return args
    names = _remote_helper_names_for_scope("--local", cwd, env)
    names |= _remote_helper_names_for_scope(
        "--worktree",
        cwd,
        env,
        ignore_unsupported=True,
    )
    if not names:
        return args
    command = args[0]
    if command in {"fetch", "pull"}:
        return [command, "--upload-pack=git-upload-pack", *args[1:]]
    if command == "push":
        return [command, "--receive-pack=git-receive-pack", *args[1:]]
    return args


def _has_repo_local_filters(cwd: Path, env: dict[str, str]) -> bool:
    names = _filter_names_for_scope("--local", cwd, env)
    names |= _filter_names_for_scope("--worktree", cwd, env, ignore_unsupported=True)
    return bool(names)


def _block_filtered_destructive_write(ctx: GitContext, message: str) -> None:
    if workspace_git_destructive_enabled() and _has_repo_local_filters(ctx.repo_root, _clean_git_env()):
        raise GitWorkspaceError(message, "filtered_path")


def resolve_git_context(workspace: str | Path) -> GitContext | None:
    ws = Path(workspace).expanduser().resolve()
    result = _run_git(ws, ["rev-parse", "--show-toplevel"], check=False)
    if result.returncode != 0:
        return None
    repo_root = Path(result.stdout.strip()).resolve()
    try:
        prefix = ws.relative_to(repo_root).as_posix()
    except ValueError:
        return None
    return GitContext(workspace=ws, repo_root=repo_root, workspace_prefix="" if prefix == "." else prefix)


def _workspace_pathspec(ctx: GitContext) -> str:
    return ctx.workspace_prefix or "."


def _repo_rel(ctx: GitContext, workspace_rel: str) -> str:
    try:
        target = safe_resolve_ws(ctx.workspace, workspace_rel or ".")
    except ValueError as exc:
        raise GitWorkspaceError(str(exc), "path_outside_workspace") from exc
    try:
        repo_rel = target.relative_to(ctx.repo_root).as_posix()
    except ValueError as exc:
        raise GitWorkspaceError("Path is outside the Git repository", "path_outside_workspace") from exc
    if ctx.workspace_prefix:
        try:
            target.relative_to(ctx.workspace)
        except ValueError as exc:
            raise GitWorkspaceError("Path is outside the workspace", "path_outside_workspace") from exc
    return repo_rel


def _workspace_rel(ctx: GitContext, repo_rel: str) -> str | None:
    repo_rel = repo_rel.replace("\\", "/")
    if not ctx.workspace_prefix:
        return repo_rel
    prefix = ctx.workspace_prefix.rstrip("/") + "/"
    if repo_rel == ctx.workspace_prefix:
        return "."
    if repo_rel.startswith(prefix):
        return repo_rel[len(prefix) :]
    return None


def _empty_status() -> dict:
    return {
        "changed": 0,
        "staged": 0,
        "unstaged": 0,
        "untracked": 0,
        "conflicts": 0,
    }


def _status_code(xy: str, *, untracked: bool = False, renamed: bool = False) -> str:
    if untracked:
        return "??"
    if xy in {"DD", "AU", "UD", "UA", "DU", "AA", "UU"}:
        return xy
    if renamed:
        return "R"
    for ch in xy:
        if ch in "MADRCUT":
            return ch
    return xy.strip(".") or "M"


def _parse_numstat(text: str, ctx: GitContext) -> dict[str, tuple[int, int, bool]]:
    stats: dict[str, tuple[int, int, bool]] = {}
    for line in text.splitlines():
        parts = line.split("\t", 2)
        if len(parts) < 3:
            continue
        raw_add, raw_del, raw_path = parts
        binary = raw_add == "-" or raw_del == "-"
        additions = 0 if binary else int(raw_add or "0")
        deletions = 0 if binary else int(raw_del or "0")
        workspace_path = _workspace_rel(ctx, raw_path)
        if workspace_path is None:
            continue
        stats[workspace_path] = (additions, deletions, binary)
    return stats


def _parse_path_list(text: str, ctx: GitContext) -> set[str]:
    paths: set[str] = set()
    for raw_path in text.split("\0"):
        if not raw_path:
            continue
        workspace_path = _workspace_rel(ctx, raw_path)
        if workspace_path is not None:
            paths.add(workspace_path)
    return paths


def _collect_diff_paths(ctx: GitContext, cached: bool, *, ignore_cr_at_eol: bool = True) -> set[str] | None:
    args = ["diff", "--name-only", "-z"]
    args.append("--no-textconv")
    if ignore_cr_at_eol:
        args.append("--ignore-cr-at-eol")
    if cached:
        args.append("--cached")
    args.extend(["--", _workspace_pathspec(ctx)])
    result = _run_git(
        ctx,
        args,
        check=False,
        disable_filter_attributes=workspace_git_destructive_enabled(),
        neutralize_filter_programs=True,
    )
    if result.returncode != 0:
        return None
    return _parse_path_list(result.stdout, ctx)


def _collect_numstat(
    ctx: GitContext,
    cached: bool,
    *,
    ignore_cr_at_eol: bool = True,
) -> dict[str, tuple[int, int, bool]]:
    args = ["diff", "--numstat"]
    args.append("--no-textconv")
    if ignore_cr_at_eol:
        args.append("--ignore-cr-at-eol")
    if cached:
        args.append("--cached")
    args.extend(["--", _workspace_pathspec(ctx)])
    result = _run_git(
        ctx,
        args,
        check=False,
        disable_filter_attributes=workspace_git_destructive_enabled(),
        neutralize_filter_programs=True,
    )
    if result.returncode != 0:
        return {}
    return _parse_numstat(result.stdout, ctx)


def _count_untracked_file(path: Path) -> tuple[int, int, bool]:
    try:
        if not path.is_file() or path.stat().st_size > DIFF_SIZE_LIMIT:
            return 0, 0, False
    except OSError:
        return 0, 0, False
    try:
        data = path.read_bytes()
    except OSError:
        return 0, 0, False
    if b"\0" in data:
        return 0, 0, True
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return 0, 0, True
    return len(text.splitlines()) or (1 if text else 0), 0, False


def git_status(workspace: str | Path) -> dict:
    ctx = resolve_git_context(workspace)
    if ctx is None:
        return {"is_git": False}

    result = _run_git(
        ctx,
        [
            "status",
            "--porcelain=v2",
            "-z",
            "--branch",
            "--ignored=matching",
            "--untracked-files=all",
            "--",
            _workspace_pathspec(ctx),
        ],
        check=True,
        disable_filter_attributes=workspace_git_destructive_enabled(),
        neutralize_filter_programs=True,
    )
    staged_stats = _collect_numstat(ctx, cached=True)
    unstaged_stats = _collect_numstat(ctx, cached=False)
    staged_raw_stats = _collect_numstat(ctx, cached=True, ignore_cr_at_eol=False)
    unstaged_raw_stats = _collect_numstat(ctx, cached=False, ignore_cr_at_eol=False)
    staged_diff_paths = _collect_diff_paths(ctx, cached=True)
    unstaged_diff_paths = _collect_diff_paths(ctx, cached=False)

    branch = ""
    upstream = ""
    ahead = 0
    behind = 0
    files: dict[str, dict] = {}
    filtered_noise = {"filemode_only": 0, "crlf_only": 0}
    tokens = result.stdout.split("\0")
    i = 0
    truncated = False
    while i < len(tokens):
        rec = tokens[i]
        i += 1
        if not rec:
            continue
        if rec.startswith("# "):
            parts = rec.split(" ", 2)
            if len(parts) >= 3 and parts[1] == "branch.head":
                branch = "" if parts[2] == "(detached)" else parts[2]
            elif len(parts) >= 3 and parts[1] == "branch.upstream":
                upstream = parts[2]
            elif len(parts) >= 3 and parts[1] == "branch.ab":
                for bit in parts[2].split():
                    if bit.startswith("+") and bit[1:].isdigit():
                        ahead = int(bit[1:])
                    elif bit.startswith("-") and bit[1:].isdigit():
                        behind = int(bit[1:])
            continue

        old_path = None
        renamed = False
        if rec.startswith("? "):
            xy = "??"
            repo_path = rec[2:]
            untracked = True
            ignored = False
        elif rec.startswith("! "):
            xy = "!!"
            repo_path = rec[2:]
            untracked = False
            ignored = True
        elif rec.startswith("1 "):
            parts = rec.split(" ", 8)
            if len(parts) < 9:
                continue
            xy = parts[1]
            repo_path = parts[8]
            untracked = False
            ignored = False
        elif rec.startswith("2 "):
            parts = rec.split(" ", 9)
            if len(parts) < 10:
                continue
            xy = parts[1]
            repo_path = parts[9]
            if i < len(tokens):
                old_path = tokens[i]
                i += 1
            renamed = True
            untracked = False
            ignored = False
        elif rec.startswith("u "):
            parts = rec.split(" ", 10)
            if len(parts) < 11:
                continue
            xy = parts[1]
            repo_path = parts[10]
            untracked = False
            ignored = False
        else:
            continue

        workspace_path = _workspace_rel(ctx, repo_path)
        if workspace_path is None:
            continue
        old_workspace_path = _workspace_rel(ctx, old_path) if old_path else None
        x = xy[0] if xy else "."
        y = xy[1] if len(xy) > 1 else "."
        conflict = xy in {"DD", "AU", "UD", "UA", "DU", "AA", "UU"} or rec.startswith("u ")
        additions, deletions, binary = 0, 0, False
        for source in (staged_stats, unstaged_stats):
            if workspace_path in source:
                a, d, b = source[workspace_path]
                additions += a
                deletions += d
                binary = binary or b
        if untracked:
            additions, deletions, binary = _count_untracked_file(ctx.workspace / workspace_path)

        staged = (x not in {".", "?"}) and not untracked
        unstaged = (y not in {".", " "}) and not untracked
        if staged and staged_diff_paths is not None and not renamed:
            raw_staged = staged
            staged = workspace_path in staged_diff_paths or (
                old_workspace_path is not None and old_workspace_path in staged_diff_paths
            )
            if raw_staged and not staged:
                if workspace_path in staged_raw_stats or (
                    old_workspace_path is not None and old_workspace_path in staged_raw_stats
                ):
                    filtered_noise["crlf_only"] += 1
                else:
                    filtered_noise["filemode_only"] += 1
        if unstaged and unstaged_diff_paths is not None and not renamed:
            raw_unstaged = unstaged
            unstaged = workspace_path in unstaged_diff_paths or (
                old_workspace_path is not None and old_workspace_path in unstaged_diff_paths
            )
            if raw_unstaged and not unstaged:
                if workspace_path in unstaged_raw_stats or (
                    old_workspace_path is not None and old_workspace_path in unstaged_raw_stats
                ):
                    filtered_noise["crlf_only"] += 1
                else:
                    filtered_noise["filemode_only"] += 1
        if ignored:
            files[workspace_path] = {
                "path": workspace_path,
                "old_path": None,
                "workspace_path": workspace_path,
                "status": "Ignored",
                "staged": False,
                "unstaged": False,
                "untracked": False,
                "ignored": True,
                "conflict": False,
                "additions": 0,
                "deletions": 0,
                "binary": False,
            }
            if len(files) >= STATUS_FILE_LIMIT:
                truncated = True
                break
            continue

        if not (staged or unstaged or untracked or conflict or renamed):
            continue
        if not (untracked or conflict or renamed or binary) and additions == 0 and deletions == 0:
            filtered_noise["crlf_only"] += 1
            continue

        files[workspace_path] = {
            "path": workspace_path,
            "old_path": old_workspace_path,
            "workspace_path": workspace_path,
            "status": _status_code(xy, untracked=untracked, renamed=renamed),
            "staged": staged,
            "unstaged": unstaged,
            "untracked": untracked,
            "ignored": False,
            "conflict": conflict,
            "additions": additions,
            "deletions": deletions,
            "binary": binary,
        }
        if len(files) >= STATUS_FILE_LIMIT:
            truncated = True
            break

    file_list = sorted(files.values(), key=lambda f: (f["path"].lower()))
    totals = _empty_status()
    for item in file_list:
        if item.get("ignored"):
            continue
        if item["staged"]:
            totals["staged"] += 1
        if item["unstaged"]:
            totals["unstaged"] += 1
        if item["untracked"]:
            totals["untracked"] += 1
        if item["conflict"]:
            totals["conflicts"] += 1
    totals["changed"] = sum(1 for item in file_list if not item.get("ignored"))

    if not branch:
        branch = (_run_git(ctx, ["rev-parse", "--short", "HEAD"], check=False).stdout or "").strip()
    return {
        "is_git": True,
        "branch": branch or "HEAD",
        "upstream": upstream,
        "ahead": ahead,
        "behind": behind,
        "totals": totals,
        "files": file_list,
        "truncated": truncated,
        "noise_filtering": {
            **filtered_noise,
            "active": any(filtered_noise.values()),
        },
    }


def _branch_ahead_behind(ctx: GitContext, branch: str, upstream: str) -> tuple[int, int]:
    if not upstream:
        return 0, 0
    result = _run_git(ctx, ["rev-list", "--left-right", "--count", f"{branch}...{upstream}"], check=False)
    if result.returncode != 0:
        return 0, 0
    parts = result.stdout.strip().split()
    if len(parts) != 2:
        return 0, 0
    try:
        return int(parts[0]), int(parts[1])
    except ValueError:
        return 0, 0


def _for_each_ref(ctx: GitContext, ref_prefix: str) -> list[dict]:
    fmt = (
        "%(refname)%00%(refname:short)%00%(upstream:short)%00%(objectname:short)%00"
        "%(committerdate:unix)%00%(committerdate:relative)%00%(authorname)%00%(subject)"
    )
    result = _run_git(ctx, ["for-each-ref", f"--format={fmt}", ref_prefix], check=True)
    refs = []
    for line in result.stdout.splitlines():
        full_name, name, upstream, sha, updated, updated_relative, author, subject = (
            line.split("\0") + ["", "", "", "", "", "", "", ""]
        )[:8]
        if not name or full_name.endswith("/HEAD") or name.endswith("/HEAD"):
            continue
        if ref_prefix == "refs/remotes" and "/" not in name:
            continue
        item = {
            "name": name,
            "sha": sha,
            "updated": int(updated) if str(updated).isdigit() else 0,
            "updated_relative": updated_relative,
            "author": author,
            "subject": subject,
        }
        if upstream:
            ahead, behind = _branch_ahead_behind(ctx, name, upstream)
            item.update({"upstream": upstream, "ahead": ahead, "behind": behind})
        else:
            item.update({"upstream": "", "ahead": 0, "behind": 0})
        refs.append(item)
    return sorted(refs, key=lambda item: item["name"].lower())


def git_branches(workspace: str | Path) -> dict:
    ctx = resolve_git_context(workspace)
    if ctx is None:
        raise GitWorkspaceError("Workspace is not a Git repository", "not_a_repo")
    head_name = _run_git(ctx, ["branch", "--show-current"], check=True).stdout.strip()
    detached = not bool(head_name)
    head_sha = _run_git(ctx, ["rev-parse", "--short", "HEAD"], check=True).stdout.strip()
    status = git_status(workspace)
    local = _for_each_ref(ctx, "refs/heads")
    remote = _for_each_ref(ctx, "refs/remotes")
    return {
        "is_git": True,
        "current": head_name or head_sha or "HEAD",
        "detached": detached,
        "head": head_sha,
        "local": local,
        "remote": remote,
        "upstream": status.get("upstream", ""),
        "ahead": status.get("ahead", 0),
        "behind": status.get("behind", 0),
    }


def _validate_local_branch(ctx: GitContext, ref: str) -> str:
    ref = str(ref or "").strip()
    if not ref:
        raise GitWorkspaceError("Branch name is required", "invalid_ref")
    _run_git(ctx, ["show-ref", "--verify", f"refs/heads/{ref}"], check=True)
    return ref


def _validate_remote_branch(ctx: GitContext, ref: str) -> str:
    ref = str(ref or "").strip()
    if not ref:
        raise GitWorkspaceError("Remote branch name is required", "invalid_ref")
    _run_git(ctx, ["show-ref", "--verify", f"refs/remotes/{ref}"], check=True)
    return ref


def _validate_checkout_start(ctx: GitContext, ref: str) -> str:
    ref = str(ref or "HEAD").strip() or "HEAD"
    result = _run_git(ctx, ["rev-parse", "--verify", f"{ref}^{{commit}}"], check=False)
    if result.returncode != 0:
        raise GitWorkspaceError("Invalid checkout reference", "invalid_ref")
    return ref


def _validate_new_branch_name(ctx: GitContext, name: str) -> str:
    name = str(name or "").strip()
    if not name:
        raise GitWorkspaceError("New branch name is required", "invalid_ref")
    result = _run_git(ctx, ["check-ref-format", "--branch", name], check=False)
    if result.returncode != 0:
        raise GitWorkspaceError("Invalid branch name", "invalid_ref")
    exists = _run_git(ctx, ["show-ref", "--verify", f"refs/heads/{name}"], check=False)
    if exists.returncode == 0:
        raise GitWorkspaceError("A local branch with that name already exists", "invalid_ref")
    return name


def _dirty_worktree(ctx: GitContext) -> bool:
    result = _run_git(ctx, ["status", "--porcelain=v2", "--untracked-files=all"],
                      check=True, neutralize_filter_programs=True)
    return bool(result.stdout.strip())


def _current_checkout_label(ctx: GitContext) -> str:
    branch = _run_git(ctx, ["branch", "--show-current"], check=False).stdout.strip()
    if branch:
        return branch
    return _run_git(ctx, ["rev-parse", "--short", "HEAD"], check=True).stdout.strip() or "HEAD"


def _stash_subject_parts(subject: str) -> tuple[str, str] | None:
    subject = str(subject or "").strip()
    if not subject.startswith("On ") or ": " not in subject:
        return None
    branch, message = subject[3:].split(": ", 1)
    branch = branch.strip()
    message = message.strip()
    if not branch or not message.startswith(_HERMES_BRANCH_SWITCH_STASH_PREFIX):
        return None
    return branch, message


def _hermes_branch_switch_stashes(ctx: GitContext) -> list[dict]:
    result = _run_git(ctx, ["stash", "list", "--format=%gd%x00%gs"], check=False)
    if result.returncode != 0:
        return []
    stashes = []
    for line in result.stdout.splitlines():
        try:
            ref, subject = line.split("\0", 1)
        except ValueError:
            continue
        parts = _stash_subject_parts(subject)
        if not parts:
            continue
        branch, message = parts
        stashes.append({"ref": ref, "branch": branch, "message": message})
    return stashes


def _restore_branch_switch_stash_locked(ctx: GitContext, branch: str) -> dict:
    if workspace_git_destructive_enabled() and _has_repo_local_filters(ctx.repo_root, _clean_git_env()):
        return {
            "restore_blocked": True,
            "restore_reason": "Repository defines local filter programs",
        }
    if _dirty_worktree(ctx):
        return {}
    for item in _hermes_branch_switch_stashes(ctx):
        if item.get("branch") != branch:
            continue
        result = _run_git(
            ctx,
            ["stash", "pop", "--index", item["ref"]],
            check=False,
            destructive=True,
            disable_filter_attributes=True,
        )
        if result.returncode == 0:
            return {"restored_stash": item}
        return {
            "restore_failed": True,
            "restore_error": (result.stderr or result.stdout or "Git stash restore failed").strip(),
            "restore_stash": item,
        }
    return {}


def _validate_checkout_request_locked(
    ctx: GitContext,
    ref: str,
    mode: str,
    new_branch: str | None,
) -> None:
    if mode == "local":
        _validate_local_branch(ctx, ref)
        return
    if mode in {"new", "create"}:
        _validate_new_branch_name(ctx, new_branch or ref)
        _validate_checkout_start(ctx, ref if (new_branch and ref and ref != new_branch) else "HEAD")
        return
    if mode == "remote":
        remote_ref = _validate_remote_branch(ctx, ref)
        branch_name = str(new_branch or remote_ref.split("/", 1)[-1]).strip()
        exists = _run_git(ctx, ["show-ref", "--verify", f"refs/heads/{branch_name}"], check=False)
        if exists.returncode != 0:
            _validate_new_branch_name(ctx, branch_name)
        return
    if mode in {"detached", "detach"}:
        _validate_checkout_start(ctx, ref)
        return
    raise GitWorkspaceError("Unsupported checkout mode", "invalid_ref")


def _perform_checkout_locked(
    ctx: GitContext,
    workspace: str | Path,
    ref: str,
    mode: str,
    new_branch: str | None,
    track: bool,
) -> subprocess.CompletedProcess[str]:
    if workspace_git_destructive_enabled() and _has_repo_local_filters(ctx.repo_root, _clean_git_env()):
        raise GitWorkspaceError(
            "Cannot checkout: repository defines local filter programs that would alter file content",
            "filtered_path",
        )
    if mode == "local":
        target = _validate_local_branch(ctx, ref)
        return _run_git(
            ctx,
            ["switch", "--recurse-submodules=no", target],
            check=True,
            destructive=True,
            disable_filter_attributes=True,
        )
    if mode in {"new", "create"}:
        branch = _validate_new_branch_name(ctx, new_branch or ref)
        start_ref = _validate_checkout_start(ctx, ref if (new_branch and ref and ref != new_branch) else "HEAD")
        return _run_git(
            ctx,
            ["switch", "--recurse-submodules=no", "-c", branch, start_ref],
            check=True,
            destructive=True,
            disable_filter_attributes=True,
        )
    if mode == "remote":
        remote_ref = _validate_remote_branch(ctx, ref)
        branch_name = str(new_branch or remote_ref.split("/", 1)[-1]).strip()
        exists = _run_git(ctx, ["show-ref", "--verify", f"refs/heads/{branch_name}"], check=False)
        if exists.returncode == 0:
            result = _run_git(
                ctx,
                ["switch", "--recurse-submodules=no", branch_name],
                check=True,
                destructive=True,
                disable_filter_attributes=True,
            )
            if track:
                _run_git(ctx, ["branch", "--set-upstream-to", remote_ref, branch_name], check=False)
            return result
        branch = _validate_new_branch_name(ctx, branch_name)
        args = ["switch", "--recurse-submodules=no", "-c", branch]
        if track:
            args.append("--track")
        args.append(remote_ref)
        return _run_git(
            ctx,
            args,
            check=True,
            destructive=True,
            disable_filter_attributes=True,
        )
    if mode in {"detached", "detach"}:
        target = _validate_checkout_start(ctx, ref)
        return _run_git(
            ctx,
            ["switch", "--recurse-submodules=no", "--detach", target],
            check=True,
            destructive=True,
            disable_filter_attributes=True,
        )
    raise GitWorkspaceError("Unsupported checkout mode", "invalid_ref")


def git_checkout(
    workspace: str | Path,
    ref: str,
    mode: str,
    new_branch: str | None = None,
    track: bool = False,
    dirty_mode: str = "block",
) -> dict:
    ctx = resolve_git_context(workspace)
    if ctx is None:
        raise GitWorkspaceError("Workspace is not a Git repository", "not_a_repo")
    mode = str(mode or "local").strip().lower()
    dirty_mode = str(dirty_mode or "block").strip().lower()
    if dirty_mode != "block":
        raise GitWorkspaceError("Only dirty_mode=block is supported for branch checkout", "dirty_worktree")
    with _git_mutation_lock(ctx):
        _validate_checkout_request_locked(ctx, ref, mode, new_branch)
        if _dirty_worktree(ctx):
            raise GitWorkspaceError(
                "Checkout blocked because the Git worktree has uncommitted changes",
                "dirty_worktree",
            )
        result = _perform_checkout_locked(ctx, workspace, ref, mode, new_branch, track)
    status = git_status(workspace)
    branches = git_branches(workspace)
    return {
        "ok": True,
        "message": _remote_message(result),
        "current_branch": branches.get("current"),
        "status": status,
        "branches": branches,
    }


def git_stash_and_checkout(
    workspace: str | Path,
    ref: str,
    mode: str,
    new_branch: str | None = None,
    track: bool = False,
) -> dict:
    ctx = resolve_git_context(workspace)
    if ctx is None:
        raise GitWorkspaceError("Workspace is not a Git repository", "not_a_repo")
    mode = str(mode or "local").strip().lower()
    target_label = str(new_branch or ref or "HEAD").strip() or "HEAD"
    stash_name = f"{_HERMES_BRANCH_SWITCH_STASH_PREFIX} to {target_label}".strip()
    restored: dict = {}
    with _git_mutation_lock(ctx):
        _validate_checkout_request_locked(ctx, ref, mode, new_branch)
        if workspace_git_destructive_enabled() and _has_repo_local_filters(ctx.repo_root, _clean_git_env()):
            raise GitWorkspaceError(
                "Cannot stash: repository defines local filter programs that would alter file content",
                "filtered_path",
            )
        stashed = False
        if _dirty_worktree(ctx):
            stash_result = _run_git(
                ctx,
                ["stash", "push", "-u", "-m", stash_name],
                check=True,
                destructive=True,
                disable_filter_attributes=True,
            )
            stash_text = _remote_message(stash_result)
            stashed = "No local changes to save" not in stash_text
        try:
            result = _perform_checkout_locked(ctx, workspace, ref, mode, new_branch, track)
        except Exception:
            if stashed:
                _run_git(
                    ctx,
                    ["stash", "pop", "--index", "stash@{0}"],
                    check=False,
                    destructive=True,
                    disable_filter_attributes=True,
                )
            raise
        current_branch = _current_checkout_label(ctx)
        restored = _restore_branch_switch_stash_locked(ctx, current_branch)
    status = git_status(workspace)
    branches = git_branches(workspace)
    return {
        "ok": True,
        "message": _remote_message(result),
        "stash_name": stash_name if stashed else "",
        "stashed": stashed,
        "restored_stash": restored.get("restored_stash"),
        "restore_failed": bool(restored.get("restore_failed")),
        "restore_error": restored.get("restore_error", ""),
        "restore_stash": restored.get("restore_stash"),
        "current_branch": branches.get("current"),
        "status": status,
        "branches": branches,
    }


def _diff_stats(diff_text: str) -> tuple[int, int]:
    additions = deletions = 0
    for line in diff_text.splitlines():
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+"):
            additions += 1
        elif line.startswith("-"):
            deletions += 1
    return additions, deletions


def _synthetic_untracked_diff(path: Path, label: str) -> dict:
    try:
        if not path.is_file():
            raise GitWorkspaceError("Path is not a file")
        if path.stat().st_size > DIFF_SIZE_LIMIT:
            return {
                "binary": False,
                "too_large": True,
                "diff": "",
                "additions": 0,
                "deletions": 0,
            }
    except OSError as exc:
        raise GitWorkspaceError(str(exc)) from exc
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise GitWorkspaceError(str(exc)) from exc
    if b"\0" in data:
        return {"binary": True, "too_large": False, "diff": "", "additions": 0, "deletions": 0}
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return {"binary": True, "too_large": False, "diff": "", "additions": 0, "deletions": 0}
    lines = text.splitlines()
    diff_lines = list(
        difflib.unified_diff([], lines, fromfile="/dev/null", tofile=f"b/{label}", lineterm="")
    )
    diff = "\n".join(diff_lines) + ("\n" if diff_lines else "")
    too_large = len(diff.encode("utf-8", errors="replace")) > DIFF_SIZE_LIMIT
    if too_large:
        diff = diff[:DIFF_SIZE_LIMIT]
    additions, deletions = _diff_stats(diff)
    return {
        "binary": False,
        "too_large": too_large,
        "diff": diff,
        "additions": additions,
        "deletions": deletions,
    }


def git_diff(workspace: str | Path, path: str, kind: str = "unstaged") -> dict:
    ctx = resolve_git_context(workspace)
    if ctx is None:
        raise GitWorkspaceError("Workspace is not a Git repository")
    if kind not in {"unstaged", "staged"}:
        raise GitWorkspaceError("kind must be staged or unstaged")
    repo_rel = _repo_rel(ctx, path)
    workspace_rel = _workspace_rel(ctx, repo_rel) or path

    status = git_status(workspace)
    file_state = next((f for f in status.get("files", []) if f.get("path") == workspace_rel), None)
    if kind == "unstaged" and file_state and file_state.get("untracked"):
        payload = _synthetic_untracked_diff(ctx.workspace / workspace_rel, workspace_rel)
        return {"path": workspace_rel, "kind": kind, **payload}

    args = ["diff", "--no-ext-diff", "--no-textconv", "--unified=3"]
    if kind == "staged":
        args.append("--cached")
    args.extend(["--", repo_rel])
    result = _run_git(ctx, args, check=True, neutralize_filter_programs=True)
    diff = result.stdout
    binary = "Binary files " in diff or "GIT binary patch" in diff
    too_large = len(diff.encode("utf-8", errors="replace")) > DIFF_SIZE_LIMIT
    if too_large:
        diff = diff[:DIFF_SIZE_LIMIT]
    additions, deletions = _diff_stats(diff)
    return {
        "path": workspace_rel,
        "kind": kind,
        "binary": binary,
        "too_large": too_large,
        "additions": additions,
        "deletions": deletions,
        "diff": "" if binary else diff,
    }


def _clean_paths(paths: Iterable[str]) -> list[str]:
    cleaned = []
    for path in paths:
        value = str(path or "").strip()
        if value and value not in cleaned:
            cleaned.append(value)
    if not cleaned:
        raise GitWorkspaceError("At least one path is required")
    return cleaned


def _pathspecs(ctx: GitContext, paths: Iterable[str]) -> list[str]:
    return [_repo_rel(ctx, path) for path in _clean_paths(paths)]


def git_stage(workspace: str | Path, paths: Iterable[str]) -> dict:
    ctx = resolve_git_context(workspace)
    if ctx is None:
        raise GitWorkspaceError("Workspace is not a Git repository", "not_a_repo")
    with _git_mutation_lock(ctx):
        _block_filtered_destructive_write(
            ctx,
            "Repository uses local Git filters; stage may corrupt index content. Use the terminal to stage manually.",
        )
        _run_git(
            ctx,
            ["add", "--", *_pathspecs(ctx, paths)],
            check=True,
            destructive=True,
            disable_filter_attributes=True,
        )
    return git_status(workspace)


def git_unstage(workspace: str | Path, paths: Iterable[str]) -> dict:
    ctx = resolve_git_context(workspace)
    if ctx is None:
        raise GitWorkspaceError("Workspace is not a Git repository", "not_a_repo")
    specs = _pathspecs(ctx, paths)
    with _git_mutation_lock(ctx):
        result = _run_git(
            ctx,
            ["restore", "--staged", "--", *specs],
            check=False,
            destructive=True,
        )
        if result.returncode != 0:
            _run_git(ctx, ["reset", "HEAD", "--", *specs], check=True, destructive=True)
    return git_status(workspace)


def git_discard(workspace: str | Path, paths: Iterable[str], *, delete_untracked: bool = False) -> dict:
    ctx = resolve_git_context(workspace)
    if ctx is None:
        raise GitWorkspaceError("Workspace is not a Git repository", "not_a_repo")
    with _git_mutation_lock(ctx):
        _block_filtered_destructive_write(
            ctx,
            "Repository uses local Git filters; discard may corrupt working-tree content. "
            "Use the terminal to discard manually.",
        )
        status = git_status(workspace)
        by_path = {f["path"]: f for f in status.get("files", [])}
        for path in _clean_paths(paths):
            repo_rel = _repo_rel(ctx, path)
            workspace_rel = _workspace_rel(ctx, repo_rel) or path
            state = by_path.get(workspace_rel) or by_path.get(workspace_rel.rstrip("/") + "/")
            if state and state.get("conflict"):
                raise GitWorkspaceError("Conflicted files cannot be discarded from this panel", "conflict")
            if state and state.get("untracked"):
                if not delete_untracked:
                    raise GitWorkspaceError("Untracked files require delete_untracked=true")
                target = safe_resolve_ws(ctx.workspace, workspace_rel)
                if target.is_dir():
                    rmtree_anchored(ctx.workspace, target)
                else:
                    try:
                        unlink_anchored(ctx.workspace, target)
                    except FileNotFoundError:
                        # Preserve the previous Path.unlink(missing_ok=True)
                        # behavior for benign races where another process
                        # removes the untracked file after git_status() has
                        # reported it but before this discard reaches unlink.
                        pass
                continue
            _run_git(
                ctx,
                ["restore", "--worktree", "--", repo_rel],
                check=True,
                destructive=True,
                disable_filter_attributes=True,
            )
    return git_status(workspace)


COMMIT_MESSAGE_SYSTEM_PROMPT = """When writing commit messages, PR titles, or PR descriptions:

- Inspect the staged diff before suggesting a commit message.
- Do not use vague subjects like "update", "improve", "refine", "misc changes", "fix stuff", or "various changes".
- For large commits, write a concise subject plus a short body with 2-5 bullets summarizing the main areas changed.
- The subject should describe the actual user-facing result or bug fixed, not just broad implementation activity.
- Keep wording short, clear, and natural.
- Never mention AI, Cursor, Zed, agents, or similar tooling in commits, branch names, PR titles, or PR descriptions.
- Never add your own thoughts or questions into the commit message, the commit message is definitive in nature.

Return only the commit message text. Do not wrap it in Markdown fences.
""".strip()


def _staged_diff_text(ctx: GitContext) -> tuple[str, bool]:
    result = _run_git(
        ctx,
        [
            "diff",
            "--cached",
            "--no-ext-diff",
            "--no-textconv",
            "--unified=3",
            "--",
            _workspace_pathspec(ctx),
        ],
        check=True,
        neutralize_filter_programs=True,
    )
    diff = result.stdout or ""
    encoded = diff.encode("utf-8", errors="replace")
    if len(encoded) <= COMMIT_MESSAGE_DIFF_LIMIT:
        return diff, False
    return encoded[:COMMIT_MESSAGE_DIFF_LIMIT].decode("utf-8", errors="replace"), True


def _selected_temp_index_env(ctx: GitContext, specs: list[str]) -> tuple[dict[str, str], str]:
    _block_filtered_destructive_write(
        ctx,
        "Repository uses local Git filters; selected commit staging may corrupt index content. "
        "Use the terminal to commit manually.",
    )
    fd, index_path = tempfile.mkstemp(prefix="hermes-webui-git-index-")
    os.close(fd)
    Path(index_path).unlink(missing_ok=True)
    env = {"GIT_INDEX_FILE": index_path}
    try:
        head = _run_git(
            ctx,
            ["rev-parse", "--verify", "HEAD"],
            check=False,
            env=env,
            destructive=True,
        )
        if head.returncode == 0:
            _run_git(ctx, ["read-tree", "HEAD"], check=True, env=env, destructive=True)
        else:
            _run_git(ctx, ["read-tree", "--empty"], check=True, env=env, destructive=True)
        _run_git(
            ctx,
            ["add", "-A", "--", *specs],
            check=True,
            env=env,
            destructive=True,
            disable_filter_attributes=True,
        )
        return env, index_path
    except Exception:
        Path(index_path).unlink(missing_ok=True)
        raise


def _selected_files(ctx: GitContext, paths: Iterable[str]) -> tuple[list[str], list[str], list[dict]]:
    requested = _clean_paths(paths)
    requested_specs = [_repo_rel(ctx, path) for path in requested]
    workspace_paths = [_workspace_rel(ctx, spec) or path for spec, path in zip(requested_specs, requested, strict=True)]
    status = git_status(ctx.workspace)
    by_path = {f["path"]: f for f in status.get("files", [])}
    specs: list[str] = []
    selected = []
    for path, repo_rel in zip(workspace_paths, requested_specs, strict=True):
        state = by_path.get(path)
        if not state:
            continue
        if state.get("conflict"):
            raise GitWorkspaceError("Resolve conflicts before committing selected files", "conflict")
        if state.get("staged") or state.get("unstaged") or state.get("untracked"):
            selected.append(state)
            for spec in (repo_rel, _repo_rel(ctx, state["old_path"]) if state.get("old_path") else ""):
                if spec and spec not in specs:
                    specs.append(spec)
    if len(selected) != len(workspace_paths):
        raise GitWorkspaceError("Selected paths have no committable changes")
    return specs, workspace_paths, selected


def _selected_diff_text(ctx: GitContext, specs: list[str]) -> tuple[str, bool]:
    env, index_path = _selected_temp_index_env(ctx, specs)
    try:
        result = _run_git(
            ctx,
            ["diff", "--cached", "--no-ext-diff", "--no-textconv", "--unified=3", "--", *specs],
            check=True,
            env=env,
            destructive=True,
            disable_filter_attributes=True,
        )
        diff = result.stdout or ""
        encoded = diff.encode("utf-8", errors="replace")
        if len(encoded) <= COMMIT_MESSAGE_DIFF_LIMIT:
            return diff, False
        return encoded[:COMMIT_MESSAGE_DIFF_LIMIT].decode("utf-8", errors="replace"), True
    finally:
        Path(index_path).unlink(missing_ok=True)


def selected_commit_message_prompt(workspace: str | Path, paths: Iterable[str]) -> dict:
    ctx = resolve_git_context(workspace)
    if ctx is None:
        raise GitWorkspaceError("Workspace is not a Git repository", "not_a_repo")
    specs, _workspace_paths, selected_files = _selected_files(ctx, paths)
    diff, truncated = _selected_diff_text(ctx, specs)
    if not diff.strip():
        raise GitWorkspaceError("No selected diff is available")
    status = git_status(workspace)
    file_lines = []
    for item in selected_files[:80]:
        stats = (
            "binary"
            if item.get("binary")
            else f"+{item.get('additions') or 0} -{item.get('deletions') or 0}"
        )
        file_lines.append(f"- {item.get('status') or 'M'} {item.get('path')} ({stats})")
    if len(selected_files) > 80:
        file_lines.append(f"- ... {len(selected_files) - 80} more selected file(s)")
    user_prompt = (
        "Write a commit message for the selected Git diff below.\n\n"
        f"Branch: {status.get('branch') or 'HEAD'}\n"
        f"Selected files ({len(selected_files)}):\n"
        + "\n".join(file_lines)
        + (
            "\n\nDiff was truncated for size; summarize only what is visible.\n"
            if truncated
            else "\n"
        )
        + "\nSelected diff:\n```diff\n"
        + diff
        + "\n```"
    )
    return {
        "system_prompt": COMMIT_MESSAGE_SYSTEM_PROMPT,
        "user_prompt": user_prompt,
        "truncated": truncated,
        "status": status,
    }


def staged_commit_message_prompt(workspace: str | Path) -> dict:
    ctx = resolve_git_context(workspace)
    if ctx is None:
        raise GitWorkspaceError("Workspace is not a Git repository")
    status = git_status(workspace)
    if int((status.get("totals") or {}).get("staged") or 0) <= 0:
        raise GitWorkspaceError("Stage changes before generating a commit message")
    diff, truncated = _staged_diff_text(ctx)
    if not diff.strip():
        raise GitWorkspaceError("No staged diff is available")
    staged_files = [f for f in status.get("files", []) if f.get("staged")]
    file_lines = []
    for item in staged_files[:80]:
        stats = (
            "binary"
            if item.get("binary")
            else f"+{item.get('additions') or 0} -{item.get('deletions') or 0}"
        )
        file_lines.append(f"- {item.get('status') or 'M'} {item.get('path')} ({stats})")
    if len(staged_files) > 80:
        file_lines.append(f"- ... {len(staged_files) - 80} more staged file(s)")
    user_prompt = (
        "Write a commit message for the staged Git diff below.\n\n"
        f"Branch: {status.get('branch') or 'HEAD'}\n"
        f"Staged files ({len(staged_files)}):\n"
        + "\n".join(file_lines)
        + (
            "\n\nDiff was truncated for size; summarize only what is visible.\n"
            if truncated
            else "\n"
        )
        + "\nStaged diff:\n```diff\n"
        + diff
        + "\n```"
    )
    return {
        "system_prompt": COMMIT_MESSAGE_SYSTEM_PROMPT,
        "user_prompt": user_prompt,
        "truncated": truncated,
        "status": status,
    }


def clean_generated_commit_message(message: str) -> str:
    text = str(message or "").strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    if (text.startswith('"') and text.endswith('"')) or (
        text.startswith("'") and text.endswith("'")
    ):
        text = text[1:-1].strip()
    return text


def git_commit(workspace: str | Path, message: str) -> dict:
    msg = str(message or "").strip()
    if not msg:
        raise GitWorkspaceError("Commit message is required")
    ctx = resolve_git_context(workspace)
    if ctx is None:
        raise GitWorkspaceError("Workspace is not a Git repository", "not_a_repo")
    with _git_mutation_lock(ctx):
        _run_git(
            ctx,
            ["commit", "-m", msg],
            timeout=10,
            check=True,
            destructive=True,
            disable_filter_attributes=True,
        )
    sha = _run_git(ctx, ["rev-parse", "--short", "HEAD"], check=True).stdout.strip()
    return {"ok": True, "commit": sha, "status": git_status(workspace)}


def git_commit_selected(workspace: str | Path, message: str, paths: Iterable[str]) -> dict:
    msg = str(message or "").strip()
    if not msg:
        raise GitWorkspaceError("Commit message is required")
    ctx = resolve_git_context(workspace)
    if ctx is None:
        raise GitWorkspaceError("Workspace is not a Git repository", "not_a_repo")
    with _git_mutation_lock(ctx):
        specs, workspace_paths, _selected_files_list = _selected_files(ctx, paths)
        env, index_path = _selected_temp_index_env(ctx, specs)
        try:
            quiet = _run_git(
                ctx,
                ["diff", "--cached", "--quiet", "--no-textconv", "--", *specs],
                check=False,
                env=env,
                destructive=True,
                disable_filter_attributes=True,
            )
            if quiet.returncode == 0:
                raise GitWorkspaceError("Selected paths have no committable changes")
            _run_git(
                ctx,
                ["commit", "-m", msg],
                timeout=10,
                check=True,
                env=env,
                destructive=True,
                disable_filter_attributes=True,
            )
            _run_git(
                ctx,
                ["reset", "-q", "HEAD", "--", *specs],
                check=True,
                destructive=True,
            )
        finally:
            Path(index_path).unlink(missing_ok=True)
    sha = _run_git(ctx, ["rev-parse", "--short", "HEAD"], check=True).stdout.strip()
    return {"ok": True, "commit": sha, "paths": workspace_paths, "status": git_status(workspace)}


def _branch_name(ctx: GitContext) -> str:
    branch = _run_git(ctx, ["branch", "--show-current"], check=True).stdout.strip()
    if not branch:
        raise GitWorkspaceError("Cannot push from a detached HEAD")
    return branch


def _remote_message(result: subprocess.CompletedProcess[str]) -> str:
    return (result.stdout or result.stderr or "").strip()


def git_fetch(workspace: str | Path) -> dict:
    ctx = resolve_git_context(workspace)
    if ctx is None:
        raise GitWorkspaceError("Workspace is not a Git repository", "not_a_repo")
    with _git_mutation_lock(ctx):
        result = _run_git(
            ctx,
            ["fetch", "--prune", "--no-recurse-submodules"],
            timeout=GIT_REMOTE_TIMEOUT,
            check=True,
            force_destructive_hardening=True,
            disable_filter_attributes=workspace_git_destructive_enabled(),
            neutralize_filter_programs=True,
            neutralize_remote_helpers=True,
        )
    return {"ok": True, "message": _remote_message(result), "status": git_status(workspace)}


def git_pull(workspace: str | Path) -> dict:
    ctx = resolve_git_context(workspace)
    if ctx is None:
        raise GitWorkspaceError("Workspace is not a Git repository", "not_a_repo")
    with _git_mutation_lock(ctx):
        _block_filtered_destructive_write(
            ctx,
            "Repository uses local Git filters; pull may corrupt working-tree content. Use the terminal to pull manually.",
        )
        result = _run_git(
            ctx,
            ["pull", "--ff-only", "--no-recurse-submodules"],
            timeout=GIT_REMOTE_TIMEOUT,
            check=True,
            destructive=True,
            disable_filter_attributes=True,
            neutralize_filter_programs=True,
            neutralize_remote_helpers=True,
        )
    return {"ok": True, "message": _remote_message(result), "status": git_status(workspace)}


def git_push(workspace: str | Path) -> dict:
    ctx = resolve_git_context(workspace)
    if ctx is None:
        raise GitWorkspaceError("Workspace is not a Git repository", "not_a_repo")
    with _git_mutation_lock(ctx):
        status = git_status(workspace)
        args = ["push"]
        if not status.get("upstream"):
            branch = _branch_name(ctx)
            remotes = _run_git(ctx, ["remote"], check=True).stdout.split()
            if "origin" not in remotes:
                raise GitWorkspaceError("No upstream branch or origin remote is configured", "no_upstream")
            args.extend(["-u", "origin", branch])
        result = _run_git(
            ctx,
            args,
            timeout=GIT_REMOTE_TIMEOUT,
            check=True,
            destructive=True,
            disable_filter_attributes=True,
            neutralize_filter_programs=True,
            neutralize_remote_helpers=True,
        )
    return {"ok": True, "message": _remote_message(result), "status": git_status(workspace)}
