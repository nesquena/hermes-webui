"""Fail-closed Git primitives for auditing linked worktrees.

This module deliberately has no dependency on WebUI sessions or models.  Its
inputs are the Git identities needed to prove whether a linked worktree can be
considered inactive without inspecting file contents.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

KEEP_DIRTY = "KEEP_DIRTY"
KEEP_IGNORED_FILES = "KEEP_IGNORED_FILES"
KEEP_STALE_METADATA = "KEEP_STALE_METADATA"
KEEP_UNIQUE_COMMITS = "KEEP_UNIQUE_COMMITS"
KEEP_UNCERTAIN = "KEEP_UNCERTAIN"
REMOVE_ANCESTOR = "REMOVE_ANCESTOR"
REMOVE_PATCH_EQUIVALENT_KEEP_BRANCH = "REMOVE_PATCH_EQUIVALENT_KEEP_BRANCH"

GIT_TIMEOUT = 10
IGNORED_OUTPUT_LIMIT = 1024 * 1024
IGNORED_ENTRY_LIMIT = 10_000
_IGNORED_FILES_ARGS = (
    "ls-files",
    "--others",
    "--ignored",
    "--exclude-standard",
    "-z",
)

_GIT_ENV_KEYS = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_COMMON_DIR",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_NAMESPACE",
    "GIT_SHALLOW_FILE",
    "GIT_REPLACE_REF_BASE",
    "GIT_CONFIG_GLOBAL",
    "GIT_CONFIG_SYSTEM",
    "GIT_CONFIG_COUNT",
    "GIT_CONFIG_PARAMETERS",
    "GIT_ASKPASS",
    "SSH_ASKPASS",
    "GIT_SSH",
    "GIT_SSH_COMMAND",
)
_GIT_ENV_PREFIXES = ("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")
_GIT_CONFIG = (
    ("core.fsmonitor", "false"),
    ("core.sshCommand", "ssh"),
    ("core.askPass", ""),
    ("credential.helper", ""),
    ("protocol.ext.allow", "never"),
    ("core.gitProxy", ""),
    ("submodule.recurse", "false"),
    ("fetch.recurseSubmodules", "false"),
)


@dataclass(frozen=True)
class GitWorktreeDecision:
    path: str
    branch: str | None
    repo_root: str
    target_ref: str
    verdict: str
    eligible: bool
    exists: bool | None
    listed: bool | None
    dirty: bool | None
    untracked_count: int | None
    ignored_count: int | None
    ancestor_of_target: bool | None
    cherry_unique_count: int | None
    reasons: tuple[str, ...]


class _GitInvocationError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _clean_git_env() -> dict[str, str]:
    env = os.environ.copy()
    for key in _GIT_ENV_KEYS:
        env.pop(key, None)
    for key in tuple(env):
        if key.startswith(_GIT_ENV_PREFIXES):
            env.pop(key, None)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_NO_REPLACE_OBJECTS"] = "1"
    env["GIT_OPTIONAL_LOCKS"] = "0"
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_CONFIG_SYSTEM"] = os.devnull
    return env


def _git_argv(args: list[str], hooks_path: str) -> list[str]:
    argv = ["git", "--no-replace-objects"]
    for key, value in _GIT_CONFIG:
        argv.extend(["-c", f"{key}={value}"])
    argv.extend(["-c", f"core.hooksPath={hooks_path}"])
    argv.extend(args)
    return argv


def _read_only_git_args(args: list[str]) -> bool:
    command = tuple(args)
    if command in {
        ("rev-parse", "--show-toplevel"),
        ("worktree", "list", "--porcelain"),
        ("status", "--porcelain=v1", "-z", "--untracked-files=all"),
        _IGNORED_FILES_ARGS,
    }:
        return True
    if (
        len(command) == 5
        and command[:4]
        == (
            "rev-parse",
            "--verify",
            "--quiet",
            "--end-of-options",
        )
    ):
        return True
    if len(command) == 3 and command[:2] == ("check-ref-format", "--branch"):
        return True
    if len(command) == 4 and command[:2] == ("merge-base", "--is-ancestor"):
        return True
    if (
        len(command) == 4
        and command[:3] == ("show-ref", "--verify", "--quiet")
    ):
        return True
    return len(command) == 3 and command[0] == "cherry"


def _run_git(
    args: list[str],
    cwd: str | Path,
    *,
    timeout: float = GIT_TIMEOUT,
) -> subprocess.CompletedProcess[bytes]:
    if not _read_only_git_args(args):
        raise _GitInvocationError("git_command_not_allowed")
    hooks_path: str | None = None
    try:
        hooks_path = tempfile.mkdtemp(prefix="hermes-webui-worktree-git-hooks-")
        argv = _git_argv(args, hooks_path)
        if tuple(args) == _IGNORED_FILES_ARGS:
            with tempfile.TemporaryFile() as stdout:
                result = subprocess.run(
                    argv,
                    cwd=str(cwd),
                    shell=False,
                    stdout=stdout,
                    stderr=subprocess.DEVNULL,
                    check=False,
                    timeout=timeout,
                    env=_clean_git_env(),
                )
                stdout.seek(0)
                bounded_stdout = stdout.read(IGNORED_OUTPUT_LIMIT + 1)
            return subprocess.CompletedProcess(
                result.args,
                result.returncode,
                bounded_stdout,
                b"",
            )
        return subprocess.run(
            argv,
            cwd=str(cwd),
            shell=False,
            capture_output=True,
            text=False,
            check=False,
            timeout=timeout,
            env=_clean_git_env(),
        )
    except subprocess.TimeoutExpired as exc:
        raise _GitInvocationError("git_timeout") from exc
    except FileNotFoundError as exc:
        raise _GitInvocationError("git_missing") from exc
    except OSError as exc:
        raise _GitInvocationError("git_invocation_failed") from exc
    finally:
        if hooks_path:
            try:
                os.rmdir(hooks_path)
            except OSError:
                pass


def _input_text(value: object) -> str:
    try:
        return os.fsdecode(os.fspath(value))  # type: ignore[arg-type]
    except TypeError:
        return "" if value is None else str(value)


def _resolved_path(value: object) -> Path:
    return Path(_input_text(value)).expanduser().resolve(strict=False)


def _decode_path(value: bytes) -> Path:
    return Path(os.fsdecode(value)).expanduser().resolve(strict=False)


def _valid_ref_input(value: str) -> bool:
    return bool(value) and not value.startswith("-") and "\0" not in value and "\n" not in value


def _verify_repo_root(repo_root: Path) -> str | None:
    if not repo_root.is_dir():
        return "repo_root_missing"
    try:
        result = _run_git(["rev-parse", "--show-toplevel"], repo_root)
    except _GitInvocationError as exc:
        return exc.code
    if result.returncode != 0 or not result.stdout:
        return "repo_root_invalid"
    try:
        discovered = _decode_path(result.stdout.rstrip(b"\r\n"))
    except (OSError, RuntimeError, ValueError):
        return "repo_root_unparseable"
    if discovered != repo_root:
        return "repo_root_mismatch"
    return None


def _verify_target(repo_root: Path, target_ref: str) -> str | None:
    if not _valid_ref_input(target_ref):
        return "target_ref_invalid"
    try:
        result = _run_git(
            [
                "rev-parse",
                "--verify",
                "--quiet",
                "--end-of-options",
                f"{target_ref}^{{commit}}",
            ],
            repo_root,
        )
    except _GitInvocationError as exc:
        return exc.code
    if result.returncode != 0 or not result.stdout.strip():
        return "target_ref_missing"
    return None


def _local_branch_ref(branch: str | None) -> tuple[str | None, str | None]:
    if branch is None or not _valid_ref_input(branch):
        return None, "branch_invalid"
    return f"refs/heads/{branch}", None


def _verify_branch(repo_root: Path, branch: str | None) -> tuple[str | None, str | None]:
    branch_ref, error = _local_branch_ref(branch)
    if error:
        return None, error
    assert branch_ref is not None
    try:
        valid_name = _run_git(["check-ref-format", "--branch", branch], repo_root)
        if valid_name.returncode != 0:
            return None, "branch_invalid"
        exists = _run_git(
            ["show-ref", "--verify", "--quiet", branch_ref],
            repo_root,
        )
    except _GitInvocationError as exc:
        return None, exc.code
    if exists.returncode != 0:
        return None, "branch_missing"
    return branch_ref, None


def _parse_worktree_list(output: bytes) -> list[tuple[Path, str | None]]:
    records: list[tuple[Path, str | None]] = []
    path: Path | None = None
    branch_ref: str | None = None
    branch_kind_seen = False

    def finish_record() -> None:
        nonlocal path, branch_ref, branch_kind_seen
        if path is None or not branch_kind_seen:
            raise ValueError("incomplete worktree record")
        records.append((path, branch_ref))
        path = None
        branch_ref = None
        branch_kind_seen = False

    if not output:
        raise ValueError("empty worktree list")
    for raw_line in output.splitlines():
        if not raw_line:
            if path is not None:
                finish_record()
            continue
        if raw_line.startswith(b"worktree "):
            if path is not None:
                raise ValueError("unterminated worktree record")
            raw_path = raw_line[len(b"worktree ") :]
            if not raw_path:
                raise ValueError("empty worktree path")
            path = _decode_path(raw_path)
            continue
        if path is None:
            raise ValueError("field outside worktree record")
        if raw_line.startswith(b"HEAD "):
            head = raw_line[len(b"HEAD ") :]
            if not head or any(char not in b"0123456789abcdefABCDEF" for char in head):
                raise ValueError("invalid worktree head")
        elif raw_line.startswith(b"branch "):
            if branch_kind_seen:
                raise ValueError("duplicate worktree branch kind")
            raw_branch = raw_line[len(b"branch ") :]
            if not raw_branch:
                raise ValueError("empty worktree branch")
            branch_ref = os.fsdecode(raw_branch)
            branch_kind_seen = True
        elif raw_line in {b"detached", b"bare"}:
            if branch_kind_seen:
                raise ValueError("duplicate worktree branch kind")
            branch_kind_seen = True
        elif raw_line == b"locked" or raw_line.startswith(b"locked "):
            continue
        elif raw_line == b"prunable" or raw_line.startswith(b"prunable "):
            continue
        else:
            raise ValueError("unknown worktree field")
    if path is not None:
        finish_record()
    if not records:
        raise ValueError("no worktree records")
    return records


def _worktree_record(
    repo_root: Path,
    worktree_path: Path,
) -> tuple[bool | None, str | None, str | None]:
    try:
        result = _run_git(["worktree", "list", "--porcelain"], repo_root)
    except _GitInvocationError as exc:
        return None, None, exc.code
    if result.returncode != 0:
        return None, None, "worktree_list_failed"
    try:
        matches = [
            branch_ref
            for listed_path, branch_ref in _parse_worktree_list(result.stdout)
            if listed_path == worktree_path
        ]
    except (OSError, RuntimeError, UnicodeError, ValueError):
        return None, None, "worktree_list_unparseable"
    if len(matches) > 1:
        return None, None, "worktree_list_ambiguous"
    if not matches:
        return False, None, None
    return True, matches[0], None


def _parse_status_porcelain(output: bytes) -> tuple[bool, int]:
    if not output:
        return False, 0
    if not output.endswith(b"\0"):
        raise ValueError("status output is not NUL terminated")
    fields = output.split(b"\0")[:-1]
    untracked_count = 0
    index = 0
    allowed = b" MADRCUT?!"
    while index < len(fields):
        entry = fields[index]
        if len(entry) < 4 or entry[2:3] != b" " or not entry[3:]:
            raise ValueError("invalid status entry")
        status = entry[:2]
        if status[0] not in allowed or status[1] not in allowed:
            raise ValueError("invalid status code")
        if status in {b"??", b"!!"}:
            if status == b"??":
                untracked_count += 1
        elif status == b"  " or b"?" in status or b"!" in status:
            raise ValueError("invalid ordinary status code")
        if status[0:1] in {b"R", b"C"} or status[1:2] in {b"R", b"C"}:
            index += 1
            if index >= len(fields) or not fields[index]:
                raise ValueError("rename/copy source missing")
        index += 1
    return True, untracked_count


def _status(repo_path: Path) -> tuple[bool | None, int | None, str | None]:
    try:
        result = _run_git(
            ["status", "--porcelain=v1", "-z", "--untracked-files=all"],
            repo_path,
        )
    except _GitInvocationError as exc:
        return None, None, exc.code
    if result.returncode != 0:
        return None, None, "status_failed"
    try:
        dirty, untracked_count = _parse_status_porcelain(result.stdout)
    except ValueError:
        return None, None, "status_unparseable"
    return dirty, untracked_count, None


def _parse_ignored_paths(output: bytes) -> int:
    if not output:
        return 0
    if len(output) > IGNORED_OUTPUT_LIMIT or not output.endswith(b"\0"):
        raise ValueError("ignored-file output is invalid")
    fields = output[:-1].split(b"\0")
    if (
        not fields
        or any(not field for field in fields)
        or len(fields) > IGNORED_ENTRY_LIMIT
    ):
        raise ValueError("ignored-file output is invalid")
    return len(fields)


def _ignored_files(repo_path: Path) -> tuple[int | None, str | None]:
    try:
        result = _run_git(list(_IGNORED_FILES_ARGS), repo_path)
    except _GitInvocationError as exc:
        return None, exc.code
    if result.returncode != 0:
        return None, "ignored_files_failed"
    try:
        return _parse_ignored_paths(result.stdout), None
    except ValueError:
        return None, "ignored_files_unparseable"


def _decision(
    *,
    path: str,
    branch: str | None,
    repo_root: str,
    target_ref: str,
    verdict: str,
    eligible: bool = False,
    exists: bool | None = None,
    listed: bool | None = None,
    dirty: bool | None = None,
    untracked_count: int | None = None,
    ignored_count: int | None = None,
    ancestor_of_target: bool | None = None,
    cherry_unique_count: int | None = None,
    reasons: tuple[str, ...],
) -> GitWorktreeDecision:
    return GitWorktreeDecision(
        path=path,
        branch=branch,
        repo_root=repo_root,
        target_ref=target_ref,
        verdict=verdict,
        eligible=eligible,
        exists=exists,
        listed=listed,
        dirty=dirty,
        untracked_count=untracked_count,
        ignored_count=ignored_count,
        ancestor_of_target=ancestor_of_target,
        cherry_unique_count=cherry_unique_count,
        reasons=reasons,
    )


def classify_git_worktree(
    path,
    branch,
    repo_root,
    *,
    target_ref: str = "origin/master",
) -> GitWorktreeDecision:
    """Return a conservative decision based only on current Git evidence."""
    raw_path = _input_text(path)
    raw_repo_root = _input_text(repo_root)
    branch_name = None if branch is None else str(branch)
    target_name = str(target_ref or "")
    if not raw_path:
        return _decision(
            path=raw_path,
            branch=branch_name,
            repo_root=raw_repo_root,
            target_ref=target_name,
            verdict=KEEP_UNCERTAIN,
            reasons=("path_input_invalid",),
        )
    if not raw_repo_root:
        return _decision(
            path=raw_path,
            branch=branch_name,
            repo_root=raw_repo_root,
            target_ref=target_name,
            verdict=KEEP_UNCERTAIN,
            reasons=("repo_root_input_invalid",),
        )
    try:
        worktree_path = _resolved_path(raw_path)
        repo_path = _resolved_path(raw_repo_root)
    except (OSError, RuntimeError, ValueError):
        return _decision(
            path=raw_path,
            branch=branch_name,
            repo_root=raw_repo_root,
            target_ref=target_name,
            verdict=KEEP_UNCERTAIN,
            reasons=("path_input_invalid",),
        )

    audit = {
        "path": str(worktree_path),
        "branch": branch_name,
        "repo_root": str(repo_path),
        "target_ref": target_name,
        "exists": worktree_path.exists(),
        "listed": None,
        "dirty": None,
        "untracked_count": None,
        "ignored_count": None,
        "ancestor_of_target": None,
        "cherry_unique_count": None,
    }

    def result(verdict: str, *reasons: str, eligible: bool = False) -> GitWorktreeDecision:
        return _decision(
            **audit,
            verdict=verdict,
            eligible=eligible,
            reasons=tuple(reasons),
        )

    repo_error = _verify_repo_root(repo_path)
    if repo_error:
        return result(KEEP_UNCERTAIN, repo_error)

    listed, listed_branch, list_error = _worktree_record(repo_path, worktree_path)
    audit["listed"] = listed
    if list_error:
        return result(KEEP_UNCERTAIN, list_error)

    target_error = _verify_target(repo_path, target_name)
    if target_error:
        return result(KEEP_UNCERTAIN, target_error)

    branch_ref, branch_error = _verify_branch(repo_path, branch_name)
    if branch_error:
        return result(KEEP_UNCERTAIN, branch_error)
    assert branch_ref is not None

    if listed and listed_branch != branch_ref:
        return result(KEEP_UNCERTAIN, "worktree_branch_mismatch")
    if audit["exists"] is False:
        if listed:
            return result(KEEP_STALE_METADATA, "worktree_metadata_stale")
        return result(KEEP_UNCERTAIN, "worktree_path_absent")
    if not worktree_path.is_dir():
        return result(KEEP_UNCERTAIN, "worktree_path_not_directory")
    if not listed:
        return result(KEEP_UNCERTAIN, "worktree_not_listed")

    dirty, untracked_count, status_error = _status(worktree_path)
    audit["dirty"] = dirty
    audit["untracked_count"] = untracked_count
    if status_error:
        return result(KEEP_UNCERTAIN, status_error)

    ignored_count, ignored_error = _ignored_files(worktree_path)
    audit["ignored_count"] = ignored_count
    if ignored_error:
        return result(KEEP_UNCERTAIN, ignored_error)
    if dirty:
        reasons = ["dirty_worktree"]
        if untracked_count:
            reasons.append("untracked_files_present")
        if ignored_count:
            reasons.append("ignored_files_present")
        return result(KEEP_DIRTY, *reasons)
    if ignored_count:
        return result(KEEP_IGNORED_FILES, "ignored_files_present")

    try:
        ancestor = _run_git(
            ["merge-base", "--is-ancestor", branch_ref, target_name],
            repo_path,
        )
    except _GitInvocationError as exc:
        return result(KEEP_UNCERTAIN, exc.code)
    if ancestor.returncode == 0:
        audit["ancestor_of_target"] = True
        return result(
            REMOVE_ANCESTOR,
            "branch_is_ancestor_of_target",
            eligible=True,
        )
    if ancestor.returncode != 1:
        return result(KEEP_UNCERTAIN, "ancestor_check_failed")
    audit["ancestor_of_target"] = False

    try:
        cherry = _run_git(
            ["cherry", target_name, branch_ref],
            repo_path,
        )
    except _GitInvocationError as exc:
        return result(KEEP_UNCERTAIN, exc.code)
    if cherry.returncode != 0:
        return result(KEEP_UNCERTAIN, "cherry_failed")
    lines = [line for line in cherry.stdout.splitlines() if line]
    if not lines:
        return result(KEEP_UNCERTAIN, "cherry_empty")
    signs: list[bytes] = []
    for line in lines:
        parts = line.split()
        if (
            len(parts) != 2
            or parts[0] not in {b"+", b"-"}
            or not parts[1]
            or any(char not in b"0123456789abcdefABCDEF" for char in parts[1])
        ):
            return result(KEEP_UNCERTAIN, "cherry_unparseable")
        signs.append(parts[0])
    unique_count = signs.count(b"+")
    audit["cherry_unique_count"] = unique_count
    if unique_count:
        return result(KEEP_UNIQUE_COMMITS, "unique_commits_present")
    if all(sign == b"-" for sign in signs):
        return result(
            REMOVE_PATCH_EQUIVALENT_KEEP_BRANCH,
            "all_branch_commits_patch_equivalent",
            eligible=True,
        )
    return result(KEEP_UNCERTAIN, "cherry_unparseable")
