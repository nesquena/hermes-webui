"""Fail-closed Git primitives for auditing linked worktrees.

This module deliberately has no dependency on WebUI sessions or models.  Its
inputs are the Git identities needed to prove whether a linked worktree can be
considered inactive without inspecting file contents.

Safety boundary: no audit command may execute repository-controlled programs.
Porcelain commands such as ``git status`` (and plumbing that rehashes worktree
content, such as ``git diff-files``) can invoke clean/smudge filters configured
by the repository itself.  The audit therefore combines:

- plumbing commands restricted to the object database and refs
  (``rev-parse``, ``show-ref``, ``merge-base``, ``rev-list``, ``cherry``,
  ``worktree list``, ``diff-index --cached``), which never read worktree file
  contents, never consult conversion attributes, and never run hooks, diff
  drivers, or filters;
- ``ls-files --others`` directory scans (no content reads, no filters);
- a bounded, read-only parse of the index file plus ``lstat`` comparisons
  implemented here, replacing the worktree half of ``git status`` outright.

Racy index entries (mtime not older than the index timestamp) are treated as
dirty: verifying them would require hashing worktree content through the
conversion machinery, which is exactly the execution path this audit refuses.
"""

from __future__ import annotations

import os
import stat
import struct
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

KEEP_DIRTY = "KEEP_DIRTY"
KEEP_IGNORED_FILES = "KEEP_IGNORED_FILES"
KEEP_STALE_METADATA = "KEEP_STALE_METADATA"
KEEP_UNIQUE_COMMITS = "KEEP_UNIQUE_COMMITS"
KEEP_UNIQUE_MERGE_COMMITS = "KEEP_UNIQUE_MERGE_COMMITS"
KEEP_UNCERTAIN = "KEEP_UNCERTAIN"
REMOVE_ANCESTOR = "REMOVE_ANCESTOR"
REMOVE_PATCH_EQUIVALENT_KEEP_BRANCH = "REMOVE_PATCH_EQUIVALENT_KEEP_BRANCH"

GIT_TIMEOUT = 10
IGNORED_OUTPUT_LIMIT = 1024 * 1024
IGNORED_ENTRY_LIMIT = 10_000
_UNTRACKED_OUTPUT_LIMIT = 1024 * 1024
_UNTRACKED_ENTRY_LIMIT = 100_000
_GIT_OUTPUT_LIMIT = 1024 * 1024
_INDEX_SIZE_LIMIT = 64 * 1024 * 1024
_ENV_GIT_PREFIX = "GIT_"
_IGNORED_FILES_ARGS = (
    "ls-files",
    "--others",
    "--ignored",
    "--exclude-standard",
    "-z",
)
_UNTRACKED_FILES_ARGS = (
    "ls-files",
    "--others",
    "--exclude-standard",
    "-z",
)
_DIFF_INDEX_PREFIX = (
    "diff-index",
    "--cached",
    "-z",
    "--name-only",
    "--no-renames",
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
_GIT_ENV_KEYS_LOWER = {key.lower() for key in _GIT_ENV_KEYS}
_GIT_CONFIG = (
    ("core.fsmonitor", "false"),
    ("core.untrackedCache", "false"),
    ("core.sshCommand", "ssh"),
    ("core.askPass", ""),
    ("credential.helper", ""),
    ("protocol.ext.allow", "never"),
    ("core.gitProxy", ""),
    ("submodule.recurse", "false"),
    ("fetch.recurseSubmodules", "false"),
    ("diff.renames", "false"),
)

_HEX_DIGEST_LENGTHS = (40, 64)
_INDEX_MAGIC = b"DIRC"
_INDEX_VERSIONS = (2, 3, 4)
_GITLINK_MODE = 0o160000
_SYMLINK_MODE = 0o120000
_REGULAR_MODES = (0o100644, 0o100755)


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
    index_masked_count: int | None
    submodule_count: int | None
    ancestor_of_target: bool | None
    cherry_unique_count: int | None
    branch_exclusive_merge_count: int | None
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class LinkedWorktree:
    """One record from ``git worktree list --porcelain -z``."""

    path: str
    branch: str | None
    head_oid: str | None
    locked: bool


@dataclass(frozen=True)
class _IndexEntry:
    path: bytes
    mode: int
    stage: int
    assume_valid: bool
    skip_worktree: bool
    intent_to_add: bool
    ctime_ns: int
    mtime_ns: int
    dev: int
    ino: int
    uid: int
    gid: int
    size: int


@dataclass(frozen=True)
class _IndexSnapshot:
    entries: tuple[_IndexEntry, ...]
    index_mtime_sec: int
    uses_nsec: bool
    masked_count: int
    gitlink_count: int
    unmerged_count: int


@dataclass(frozen=True)
class _WorktreeScan:
    dirty: bool
    untracked_count: int
    masked_count: int
    gitlink_count: int


class _GitInvocationError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _clean_git_env() -> dict[str, str]:
    """Build a Git subprocess env with every inherited ``GIT_*`` variable removed.

    Tracing/config variables such as ``GIT_TRACE`` can make a read-only Git
    command create arbitrary files.  Inherit nothing Git-specific from the
    audit process; add back only the fixed, non-persistent values required by
    the audit.
    """
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.upper().startswith(_ENV_GIT_PREFIX)
        and key.lower() not in _GIT_ENV_KEYS_LOWER
    }
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


def _format_is_hex_oid(value: bytes) -> bool:
    """Accept full SHA-1 (40) or SHA-256 (64) hexadecimal object IDs."""
    return (
        len(value) in _HEX_DIGEST_LENGTHS
        and all(char in b"0123456789abcdefABCDEF" for char in value)
    )


def _parse_oid(output: bytes) -> str | None:
    line = output.strip().splitlines()[0] if output.strip() else b""
    return os.fsdecode(line) if _format_is_hex_oid(line) else None


_REV_LIST_COUNT_MERGES_ARGS = (
    "rev-list",
    "--count",
    "--merges",
    "--end-of-options",
)


def _read_only_git_args(args: list[str]) -> bool:
    """Allowlist of plumbing that cannot execute repository programs.

    Every entry is object-store/ref/directory-scan only: no worktree content
    hashing (no clean filters), no patch emission (no diff drivers or
    textconv), no hooks, no pager.
    """
    command = tuple(args)
    if command in {
        ("rev-parse", "--show-toplevel"),
        ("rev-parse", "--absolute-git-dir"),
        ("worktree", "list", "--porcelain", "-z"),
        _IGNORED_FILES_ARGS,
        _UNTRACKED_FILES_ARGS,
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
    if (
        len(command) == 4
        and command[:2] == ("merge-base", "--is-ancestor")
        and _format_is_hex_oid(os.fsencode(command[2]))
        and _format_is_hex_oid(os.fsencode(command[3]))
    ):
        return True
    if len(command) == 6 and command[:4] == _REV_LIST_COUNT_MERGES_ARGS:
        return _valid_merge_range_args(command[4], command[5])
    if (
        len(command) == 4
        and command[:3] == ("show-ref", "--verify", "--quiet")
        and command[3].startswith("refs/")
    ):
        return True
    if (
        len(command) == 3
        and command[0] == "cherry"
        and _format_is_hex_oid(os.fsencode(command[1]))
        and _format_is_hex_oid(os.fsencode(command[2]))
    ):
        return True
    return (
        len(command) == 7
        and command[:5] == _DIFF_INDEX_PREFIX
        and _format_is_hex_oid(os.fsencode(command[5]))
        and command[6] == "--"
    )


def _valid_merge_range_args(first: str, second: str) -> bool:
    """Accept exactly ``<oid> ^<oid>`` (or swapped) for the merge count."""

    def is_plain_oid(value: str) -> bool:
        return _format_is_hex_oid(os.fsencode(value))

    def is_negated_oid(value: str) -> bool:
        return value.startswith("^") and is_plain_oid(value[1:])

    return (
        is_plain_oid(first)
        and is_negated_oid(second)
        or is_negated_oid(first)
        and is_plain_oid(second)
    )


def _run_git(
    args: list[str],
    cwd: str | Path,
    *,
    timeout: float = GIT_TIMEOUT,
) -> subprocess.CompletedProcess[bytes]:
    """Run an allowlisted read-only Git command with hard-bounded output.

    Both streams are spooled to a temporary file so a hostile or corrupt
    repository cannot exhaust memory; output beyond ``_GIT_OUTPUT_LIMIT``
    fails closed instead of being parsed.
    """
    if not _read_only_git_args(args):
        raise _GitInvocationError("git_command_not_allowed")
    hooks_path: str | None = None
    try:
        hooks_path = tempfile.mkdtemp(prefix="hermes-webui-worktree-git-hooks-")
        argv = _git_argv(args, hooks_path)
        with tempfile.TemporaryFile(
            prefix="hermes-webui-worktree-git-out-",
        ) as spool:
            result = subprocess.run(
                argv,
                cwd=str(cwd),
                shell=False,
                stdout=spool,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=timeout,
                env=_clean_git_env(),
            )
            spool.seek(0)
            bounded_stdout = spool.read(_GIT_OUTPUT_LIMIT + 1)
        if len(bounded_stdout) > _GIT_OUTPUT_LIMIT:
            raise _GitInvocationError("git_output_oversized")
        return subprocess.CompletedProcess(
            result.args,
            result.returncode,
            bounded_stdout,
            b"",
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


def _resolve_commit_oid(
    repo_root: Path,
    ref: str,
) -> tuple[str | None, str | None]:
    """Pin a mutable ref to a commit OID without trusting it again later."""
    try:
        result = _run_git(
            [
                "rev-parse",
                "--verify",
                "--quiet",
                "--end-of-options",
                f"{ref}^{{commit}}",
            ],
            repo_root,
        )
    except _GitInvocationError as exc:
        return None, exc.code
    if result.returncode != 0:
        return None, "ref_unresolvable"
    oid = _parse_oid(result.stdout)
    if oid is None:
        return None, "ref_unparseable"
    return oid, None


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


def _verify_target_oid(
    repo_root: Path,
    target_ref: str,
) -> tuple[str | None, str | None]:
    """Resolve the target ref once to an immutable commit OID."""
    if not _valid_ref_input(target_ref):
        return None, "target_ref_invalid"
    oid, error = _resolve_commit_oid(repo_root, target_ref)
    if error:
        if error in {"ref_unresolvable", "ref_unparseable"}:
            return None, "target_ref_missing"
        return None, error
    return oid, None


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


def _verify_branch_oid(
    repo_root: Path,
    branch: str | None,
) -> tuple[str | None, str | None, str | None]:
    """Verify the branch and pin its current commit OID."""
    branch_ref, error = _local_branch_ref(branch)
    if error:
        return None, None, error
    assert branch_ref is not None
    assert branch is not None
    try:
        valid_name = _run_git(["check-ref-format", "--branch", branch], repo_root)
        if valid_name.returncode != 0:
            return None, None, "branch_invalid"
        exists = _run_git(
            ["show-ref", "--verify", "--quiet", branch_ref],
            repo_root,
        )
    except _GitInvocationError as exc:
        return None, None, exc.code
    if exists.returncode != 0:
        return None, None, "branch_missing"
    oid, resolve_error = _resolve_commit_oid(repo_root, branch_ref)
    if resolve_error:
        return None, None, resolve_error
    return branch_ref, oid, None


@dataclass(frozen=True)
class _WorktreeRecord:
    path: Path
    branch_ref: str | None
    head_oid: str | None
    locked: bool


def _parse_worktree_list(output: bytes) -> list[_WorktreeRecord]:
    """Parse ``git worktree list --porcelain -z`` output.

    NUL termination keeps paths containing newlines (or any other byte)
    unambiguous; records are separated by an empty field.
    """
    if not output or not output.endswith(b"\0"):
        raise ValueError("malformed worktree list")
    records: list[_WorktreeRecord] = []
    path: Path | None = None
    branch_ref: str | None = None
    head_oid: str | None = None
    locked = False
    branch_kind_seen = False

    def finish_record() -> None:
        nonlocal path, branch_ref, head_oid, locked, branch_kind_seen
        if path is None or head_oid is None or not branch_kind_seen:
            raise ValueError("incomplete worktree record")
        records.append(
            _WorktreeRecord(
                path=path,
                branch_ref=branch_ref,
                head_oid=head_oid,
                locked=locked,
            )
        )
        path = None
        branch_ref = None
        head_oid = None
        locked = False
        branch_kind_seen = False

    for raw_field in output[:-1].split(b"\0"):
        if not raw_field:
            if path is None:
                raise ValueError("worktree field outside record")
            finish_record()
            continue
        if raw_field.startswith(b"worktree "):
            if path is not None:
                raise ValueError("unterminated worktree record")
            raw_path = raw_field[len(b"worktree ") :]
            if not raw_path:
                raise ValueError("empty worktree path")
            path = _decode_path(raw_path)
            continue
        if path is None:
            raise ValueError("field outside worktree record")
        if raw_field.startswith(b"HEAD "):
            raw_head = raw_field[len(b"HEAD ") :]
            if not _format_is_hex_oid(raw_head):
                raise ValueError("invalid worktree head")
            head_oid = os.fsdecode(raw_head)
        elif raw_field.startswith(b"branch "):
            if branch_kind_seen:
                raise ValueError("duplicate worktree branch kind")
            raw_branch = raw_field[len(b"branch ") :]
            if not raw_branch:
                raise ValueError("empty worktree branch")
            branch_ref = os.fsdecode(raw_branch)
            branch_kind_seen = True
        elif raw_field in {b"detached", b"bare"}:
            if branch_kind_seen:
                raise ValueError("duplicate worktree branch kind")
            branch_kind_seen = True
        elif raw_field == b"locked" or raw_field.startswith(b"locked "):
            locked = True
        elif raw_field == b"prunable" or raw_field.startswith(b"prunable "):
            continue
        else:
            raise ValueError("unknown worktree field")
    if path is not None:
        raise ValueError("unterminated worktree record")
    if not records:
        raise ValueError("no worktree records")
    return records


def _list_worktree_records(
    repo_root: Path,
) -> tuple[list[_WorktreeRecord] | None, str | None]:
    try:
        result = _run_git(["worktree", "list", "--porcelain", "-z"], repo_root)
    except _GitInvocationError as exc:
        return None, exc.code
    if result.returncode != 0:
        return None, "worktree_list_failed"
    try:
        return _parse_worktree_list(result.stdout), None
    except (OSError, RuntimeError, UnicodeError, ValueError):
        return None, "worktree_list_unparseable"


def list_linked_worktrees(
    repo_root: str | os.PathLike[str],
) -> tuple[tuple[LinkedWorktree, ...] | None, str | None]:
    """Enumerate every worktree registered for ``repo_root``, read-only.

    Used to surface worktrees that no session sidecar references, so they
    cannot silently disappear from the audit inventory.
    """
    repo_path = _resolved_path(repo_root)
    repo_error = _verify_repo_root(repo_path)
    if repo_error:
        return None, repo_error
    records, error = _list_worktree_records(repo_path)
    if error or records is None:
        return None, error or "worktree_list_failed"
    return (
        tuple(
            LinkedWorktree(
                path=str(record.path),
                branch=(
                    record.branch_ref.removeprefix("refs/heads/")
                    if record.branch_ref
                    else None
                ),
                head_oid=record.head_oid,
                locked=record.locked,
            )
            for record in records
        ),
        None,
    )


def _worktree_record(
    repo_root: Path,
    worktree_path: Path,
) -> tuple[bool | None, str | None, str | None]:
    records, error = _list_worktree_records(repo_root)
    if error or records is None:
        return None, None, error or "worktree_list_failed"
    matches = [record.branch_ref for record in records if record.path == worktree_path]
    if len(matches) > 1:
        return None, None, "worktree_list_ambiguous"
    if not matches:
        return False, None, None
    return True, matches[0], None


def _read_index_bytes(index_path: Path) -> tuple[bytes, int] | str:
    """Read the index file without following symlinks, with a hard size cap.

    Returns ``(data, index_mtime_sec)`` or an error code.
    """
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        file_descriptor = os.open(index_path, flags)
    except FileNotFoundError:
        return "index_missing"
    except OSError:
        return "index_unreadable"
    try:
        info = os.fstat(file_descriptor)
        if not stat.S_ISREG(info.st_mode):
            return "index_not_regular_file"
        if info.st_size > _INDEX_SIZE_LIMIT:
            return "index_oversized"
        with os.fdopen(file_descriptor, "rb") as handle:
            file_descriptor = -1
            data = handle.read(_INDEX_SIZE_LIMIT + 1)
        if len(data) > _INDEX_SIZE_LIMIT:
            return "index_oversized"
        return data, info.st_mtime_ns // 1_000_000_000
    except OSError:
        return "index_unreadable"
    finally:
        if file_descriptor >= 0:
            os.close(file_descriptor)


def _read_index_varint(data: bytes, position: int) -> tuple[int, int]:
    """Decode the offset-style varint used by index v4 name compression."""
    byte = data[position]
    position += 1
    value = byte & 0x7F
    while byte & 0x80:
        byte = data[position]
        position += 1
        value = ((value + 1) << 7) | (byte & 0x7F)
    return value, position


def _parse_index_entries(data: bytes, hash_length: int) -> list[_IndexEntry]:
    if len(data) < 12 + hash_length or data[:4] != _INDEX_MAGIC:
        raise ValueError("bad index header")
    version, entry_count = struct.unpack_from(">II", data, 4)
    if version not in _INDEX_VERSIONS:
        raise ValueError("unsupported index version")
    position = 12
    fixed_length = 40 + hash_length + 2
    entries: list[_IndexEntry] = []
    previous_name = b""
    for _ in range(entry_count):
        if position + fixed_length > len(data):
            raise ValueError("truncated index entry")
        (
            ctime_sec,
            ctime_nsec,
            mtime_sec,
            mtime_nsec,
            dev,
            ino,
            mode,
            uid,
            gid,
            size,
        ) = struct.unpack_from(">10I", data, position)
        flags = struct.unpack_from(">H", data, position + 40 + hash_length)[0]
        cursor = position + fixed_length
        assume_valid = bool(flags & 0x8000)
        extended = bool(flags & 0x4000)
        stage = (flags >> 12) & 0x3
        name_length = flags & 0x0FFF
        skip_worktree = False
        intent_to_add = False
        if extended:
            if version < 3:
                raise ValueError("extended flags in v2 index")
            if cursor + 2 > len(data):
                raise ValueError("truncated extended flags")
            extended_flags = struct.unpack_from(">H", data, cursor)[0]
            cursor += 2
            skip_worktree = bool(extended_flags & 0x4000)
            intent_to_add = bool(extended_flags & 0x2000)
        if version == 4:
            strip, cursor = _read_index_varint(data, cursor)
            if strip > len(previous_name):
                raise ValueError("invalid name compression")
            end = data.index(b"\0", cursor)
            name = previous_name[: len(previous_name) - strip] + data[cursor:end]
            cursor = end + 1
        else:
            if name_length == 0x0FFF:
                end = data.index(b"\0", cursor)
                name = data[cursor:end]
            else:
                if cursor + name_length > len(data):
                    raise ValueError("truncated entry name")
                name = data[cursor : cursor + name_length]
            entry_length = (cursor - position) + len(name)
            padding = 8 - (entry_length % 8)
            if padding == 0:
                padding = 8
            end = position + entry_length + padding
            if end > len(data) or any(data[position + entry_length : end]):
                raise ValueError("invalid entry padding")
            cursor = end
        if not name or b"\0" in name:
            raise ValueError("invalid entry name")
        entries.append(
            _IndexEntry(
                path=name,
                mode=mode,
                stage=stage,
                assume_valid=assume_valid,
                skip_worktree=skip_worktree,
                intent_to_add=intent_to_add,
                ctime_ns=ctime_sec * 1_000_000_000 + ctime_nsec,
                mtime_ns=mtime_sec * 1_000_000_000 + mtime_nsec,
                dev=dev,
                ino=ino,
                uid=uid,
                gid=gid,
                size=size,
            )
        )
        previous_name = name
        position = cursor
    # Extensions: signature (4 bytes) + length (4 bytes) + payload, then the
    # trailing index checksum.  Walk them to confirm the structure is exact.
    while position < len(data) - hash_length:
        if position + 8 > len(data) - hash_length:
            raise ValueError("truncated index extension")
        signature = data[position : position + 4]
        if not all(
            char in b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
            for char in signature
        ):
            raise ValueError("invalid index extension signature")
        (extension_size,) = struct.unpack_from(">I", data, position + 4)
        position += 8 + extension_size
        if position > len(data) - hash_length:
            raise ValueError("index extension overruns checksum")
    if position != len(data) - hash_length:
        raise ValueError("index length mismatch")
    return entries


def _parse_index(data: bytes) -> list[_IndexEntry]:
    """Parse an index for either object format; reject ambiguous layouts."""
    for hash_length in (20, 32):
        try:
            return _parse_index_entries(data, hash_length)
        except (ValueError, IndexError, struct.error):
            continue
    raise ValueError("index_unparseable")


def _index_snapshot(worktree_path: Path) -> tuple[_IndexSnapshot | None, str | None]:
    """Read the worktree index directly; never through content-hashing Git."""
    try:
        result = _run_git(["rev-parse", "--absolute-git-dir"], worktree_path)
    except _GitInvocationError as exc:
        return None, exc.code
    if result.returncode != 0 or not result.stdout.endswith(b"\n"):
        return None, "git_dir_unresolvable"
    raw_git_dir = result.stdout[:-1]
    if not raw_git_dir or b"\0" in raw_git_dir:
        return None, "git_dir_unparseable"
    try:
        git_dir = _decode_path(raw_git_dir)
    except (OSError, RuntimeError, ValueError):
        return None, "git_dir_unparseable"
    if not git_dir.is_absolute():
        return None, "git_dir_unparseable"
    read_result = _read_index_bytes(git_dir / "index")
    if isinstance(read_result, str):
        return None, read_result
    data, index_mtime_sec = read_result
    try:
        entries = _parse_index(data)
    except ValueError:
        return None, "index_unparseable"
    masked = sum(
        1 for entry in entries if entry.assume_valid or entry.skip_worktree
    )
    gitlinks = sum(1 for entry in entries if entry.mode == _GITLINK_MODE)
    unmerged = sum(1 for entry in entries if entry.stage != 0)
    uses_nsec = any(
        entry.ctime_ns % 1_000_000_000 or entry.mtime_ns % 1_000_000_000
        for entry in entries
    )
    return (
        _IndexSnapshot(
            entries=tuple(entries),
            index_mtime_sec=index_mtime_sec,
            uses_nsec=uses_nsec,
            masked_count=masked,
            gitlink_count=gitlinks,
            unmerged_count=unmerged,
        ),
        None,
    )


def _entry_path_is_safe(name: bytes) -> bool:
    return (
        bool(name)
        and not name.startswith(b"/")
        and b"\0" not in name
        and all(part not in {b"", b".."} for part in name.split(b"/"))
    )


def _stat_scan_dirty(
    worktree_path: Path,
    snapshot: _IndexSnapshot,
) -> tuple[bool | None, str | None]:
    """Compare index stat data with the worktree; racy entries fail dirty.

    This reproduces the worktree half of ``git status`` without ever hashing
    file contents, so clean filters and other repository programs cannot run.
    A file whose stat data matches but which is racy (mtime not older than
    the index timestamp) is reported dirty rather than verified by hashing.
    """
    for entry in snapshot.entries:
        if entry.mode == _GITLINK_MODE:
            continue
        if not _entry_path_is_safe(entry.path):
            return None, "index_path_suspicious"
        try:
            info = os.lstat(worktree_path / os.fsdecode(entry.path))
        except FileNotFoundError:
            return True, None
        except OSError:
            return None, "worktree_stat_failed"
        if entry.mode == _SYMLINK_MODE:
            if not stat.S_ISLNK(info.st_mode):
                return True, None
        elif entry.mode in _REGULAR_MODES:
            if not stat.S_ISREG(info.st_mode):
                return True, None
            if info.st_size & 0xFFFFFFFF != entry.size:
                return True, None
            if bool(info.st_mode & 0o111) != (entry.mode == 0o100755):
                return True, None
        else:
            return None, "index_mode_unexpected"
        if (
            info.st_dev & 0xFFFFFFFF != entry.dev
            or info.st_ino & 0xFFFFFFFF != entry.ino
            or info.st_uid & 0xFFFFFFFF != entry.uid
            or info.st_gid & 0xFFFFFFFF != entry.gid
        ):
            return True, None
        ctime_differs = (
            info.st_ctime_ns // 1_000_000_000 != entry.ctime_ns // 1_000_000_000
        )
        mtime_differs = (
            info.st_mtime_ns // 1_000_000_000 != entry.mtime_ns // 1_000_000_000
        )
        if snapshot.uses_nsec:
            ctime_differs = ctime_differs or info.st_ctime_ns != entry.ctime_ns
            mtime_differs = mtime_differs or info.st_mtime_ns != entry.mtime_ns
        if ctime_differs or mtime_differs:
            return True, None
        if entry.mtime_ns // 1_000_000_000 >= snapshot.index_mtime_sec:
            # Racy entry: only a content hash could prove it clean, and
            # hashing would invoke repository-controlled conversion programs.
            return True, None
    return False, None


def _index_differs_from_head(
    worktree_path: Path,
    head_oid: str,
) -> tuple[bool | None, str | None]:
    """Compare the index against the pinned HEAD tree (object store only)."""
    try:
        result = _run_git(
            [
                "diff-index",
                "--cached",
                "-z",
                "--name-only",
                "--no-renames",
                head_oid,
                "--",
            ],
            worktree_path,
        )
    except _GitInvocationError as exc:
        return None, exc.code
    if result.returncode != 0:
        return None, "status_failed"
    if result.stdout and not result.stdout.endswith(b"\0"):
        return None, "status_unparseable"
    return bool(result.stdout), None


def _parse_nul_path_count(
    output: bytes,
    *,
    output_limit: int,
    entry_limit: int,
) -> int:
    if not output:
        return 0
    if len(output) > output_limit or not output.endswith(b"\0"):
        raise ValueError("path-list output is invalid")
    fields = output[:-1].split(b"\0")
    if (
        not fields
        or any(not field for field in fields)
        or len(fields) > entry_limit
    ):
        raise ValueError("path-list output is invalid")
    return len(fields)


def _parse_ignored_paths(output: bytes) -> int:
    return _parse_nul_path_count(
        output,
        output_limit=IGNORED_OUTPUT_LIMIT,
        entry_limit=IGNORED_ENTRY_LIMIT,
    )


def _untracked_files(repo_path: Path) -> tuple[int | None, str | None]:
    try:
        result = _run_git(list(_UNTRACKED_FILES_ARGS), repo_path)
    except _GitInvocationError as exc:
        return None, exc.code
    if result.returncode != 0:
        return None, "untracked_files_failed"
    try:
        return (
            _parse_nul_path_count(
                result.stdout,
                output_limit=_UNTRACKED_OUTPUT_LIMIT,
                entry_limit=_UNTRACKED_ENTRY_LIMIT,
            ),
            None,
        )
    except ValueError:
        return None, "untracked_files_unparseable"


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


def _scan_worktree_state(
    worktree_path: Path,
    head_oid: str,
) -> tuple[_WorktreeScan | None, str | None]:
    """Dirtiness, masked-index, submodule, and untracked evidence in one pass.

    No step reads worktree file contents or invokes repository-configured
    programs.
    """
    snapshot, snapshot_error = _index_snapshot(worktree_path)
    if snapshot_error or snapshot is None:
        return None, snapshot_error or "index_unreadable"
    stat_dirty, stat_error = _stat_scan_dirty(worktree_path, snapshot)
    if stat_error:
        return None, stat_error
    dirty = bool(stat_dirty) or snapshot.unmerged_count > 0
    if not dirty:
        differs, diff_error = _index_differs_from_head(worktree_path, head_oid)
        if diff_error or differs is None:
            return None, diff_error or "status_failed"
        dirty = differs
    untracked_count, untracked_error = _untracked_files(worktree_path)
    if untracked_error or untracked_count is None:
        return None, untracked_error or "untracked_files_failed"
    return (
        _WorktreeScan(
            dirty=dirty or untracked_count > 0,
            untracked_count=untracked_count,
            masked_count=snapshot.masked_count,
            gitlink_count=snapshot.gitlink_count,
        ),
        None,
    )


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
    index_masked_count: int | None = None,
    submodule_count: int | None = None,
    ancestor_of_target: bool | None = None,
    cherry_unique_count: int | None = None,
    branch_exclusive_merge_count: int | None = None,
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
        index_masked_count=index_masked_count,
        submodule_count=submodule_count,
        ancestor_of_target=ancestor_of_target,
        cherry_unique_count=cherry_unique_count,
        branch_exclusive_merge_count=branch_exclusive_merge_count,
        reasons=reasons,
    )


def _commit_tree_oid(repo_root: Path, commit_oid: str) -> str | None:
    """Resolve the tree OID of a pinned commit, or ``None`` when impossible."""
    try:
        result = _run_git(
            [
                "rev-parse",
                "--verify",
                "--quiet",
                "--end-of-options",
                f"{commit_oid}^{{tree}}",
            ],
            repo_root,
        )
    except _GitInvocationError:
        return None
    if result.returncode != 0:
        return None
    return _parse_oid(result.stdout)


def _trees_proven_equal(
    repo_root: Path,
    branch_oid: str,
    target_oid: str,
) -> bool:
    """True only when both trees resolve and are strictly identical."""
    branch_tree = _commit_tree_oid(repo_root, branch_oid)
    target_tree = _commit_tree_oid(repo_root, target_oid)
    return (
        branch_tree is not None
        and target_tree is not None
        and branch_tree == target_tree
    )


def _worktree_head_oid(worktree_path: Path) -> str | None:
    """Resolve the current HEAD OID of a worktree through Git evidence."""
    try:
        result = _run_git(
            ["rev-parse", "--verify", "--quiet", "--end-of-options", "HEAD^{commit}"],
            worktree_path,
        )
    except _GitInvocationError:
        return None
    if result.returncode != 0:
        return None
    return _parse_oid(result.stdout)


def _pins_still_valid(
    repo_root: Path,
    worktree_path: Path,
    branch_ref: str,
    branch_oid: str,
    target_ref: str,
    target_oid: str,
    worktree_head_oid: str,
) -> bool:
    """Revalidate every pinned OID before publishing eligibility.

    A concurrent ref or HEAD move between the clean-status read and the
    published decision must downgrade eligibility to uncertainty, never
    certify a moved target.  The worktree is also scanned again: a mutation
    landing after the first scan invalidates the clean evidence as well.
    """
    current_branch_oid, branch_error = _resolve_commit_oid(repo_root, branch_ref)
    if branch_error or current_branch_oid != branch_oid:
        return False
    current_target_oid, target_error = _resolve_commit_oid(repo_root, target_ref)
    if target_error or current_target_oid != target_oid:
        return False
    current_worktree_head = _worktree_head_oid(worktree_path)
    if current_worktree_head != worktree_head_oid:
        return False
    scan, scan_error = _scan_worktree_state(worktree_path, worktree_head_oid)
    if scan_error or scan is None:
        return False
    if (
        scan.dirty
        or scan.untracked_count
        or scan.masked_count
        or scan.gitlink_count
    ):
        return False
    ignored_count, ignored_error = _ignored_files(worktree_path)
    if ignored_error or ignored_count:
        return False
    return True


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
        "index_masked_count": None,
        "submodule_count": None,
        "ancestor_of_target": None,
        "cherry_unique_count": None,
        "branch_exclusive_merge_count": None,
    }
    pinned_branch_ref: str | None = None
    pinned_branch_oid: str | None = None
    pinned_target_oid: str | None = None
    pinned_worktree_head: str | None = None

    def result(verdict: str, *reasons: str, eligible: bool = False) -> GitWorktreeDecision:
        if (
            eligible
            and pinned_branch_ref is not None
            and pinned_branch_oid is not None
            and pinned_target_oid is not None
            and pinned_worktree_head is not None
            and not _pins_still_valid(
                repo_path,
                worktree_path,
                pinned_branch_ref,
                pinned_branch_oid,
                target_name,
                pinned_target_oid,
                pinned_worktree_head,
            )
        ):
            return _decision(
                **audit,
                verdict=KEEP_UNCERTAIN,
                eligible=False,
                reasons=(*reasons, "pin_revalidation_failed"),
            )
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

    target_oid, target_error = _verify_target_oid(repo_path, target_name)
    if target_error:
        return result(KEEP_UNCERTAIN, target_error)
    pinned_target_oid = target_oid

    branch_ref, branch_oid, branch_error = _verify_branch_oid(
        repo_path,
        branch_name,
    )
    if branch_error:
        return result(KEEP_UNCERTAIN, branch_error)
    assert branch_ref is not None
    assert branch_oid is not None and target_oid is not None
    pinned_branch_ref = branch_ref
    pinned_branch_oid = branch_oid

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

    worktree_head_oid = _worktree_head_oid(worktree_path)
    if worktree_head_oid is None:
        return result(KEEP_UNCERTAIN, "worktree_head_unpinnable")
    pinned_worktree_head = worktree_head_oid

    scan, scan_error = _scan_worktree_state(worktree_path, worktree_head_oid)
    if scan_error or scan is None:
        return result(KEEP_UNCERTAIN, scan_error or "index_unreadable")
    audit["dirty"] = scan.dirty
    audit["untracked_count"] = scan.untracked_count
    audit["index_masked_count"] = scan.masked_count
    audit["submodule_count"] = scan.gitlink_count
    if scan.masked_count:
        return result(
            KEEP_UNCERTAIN,
            "index_masked_entries_present",
        )
    if scan.gitlink_count:
        return result(
            KEEP_UNCERTAIN,
            "submodules_present",
        )

    ignored_count, ignored_error = _ignored_files(worktree_path)
    audit["ignored_count"] = ignored_count
    if ignored_error:
        return result(KEEP_UNCERTAIN, ignored_error)
    if scan.dirty:
        reasons = ["dirty_worktree"]
        if scan.untracked_count:
            reasons.append("untracked_files_present")
        if ignored_count:
            reasons.append("ignored_files_present")
        return result(KEEP_DIRTY, *reasons)
    if ignored_count:
        return result(KEEP_IGNORED_FILES, "ignored_files_present")

    try:
        ancestor = _run_git(
            ["merge-base", "--is-ancestor", branch_oid, target_oid],
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
            ["cherry", target_oid, branch_oid],
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
            or not _format_is_hex_oid(parts[1])
        ):
            return result(KEEP_UNCERTAIN, "cherry_unparseable")
        signs.append(parts[0])
    unique_count = signs.count(b"+")
    audit["cherry_unique_count"] = unique_count
    if unique_count:
        return result(KEEP_UNIQUE_COMMITS, "unique_commits_present")
    if all(sign == b"-" for sign in signs):
        try:
            merges = _run_git(
                [
                    *_REV_LIST_COUNT_MERGES_ARGS,
                    branch_oid,
                    f"^{target_oid}",
                ],
                repo_path,
            )
        except _GitInvocationError as exc:
            return result(KEEP_UNCERTAIN, exc.code)
        if merges.returncode != 0:
            return result(KEEP_UNCERTAIN, "merge_count_failed")
        merge_count_text = merges.stdout.strip()
        if not merge_count_text.isdigit():
            return result(KEEP_UNCERTAIN, "merge_count_unparseable")
        merge_count = int(merge_count_text)
        audit["branch_exclusive_merge_count"] = merge_count
        if merge_count and not _trees_proven_equal(
            repo_path,
            branch_oid,
            target_oid,
        ):
            return result(
                KEEP_UNIQUE_MERGE_COMMITS,
                "branch_exclusive_merge_present",
            )
        return result(
            REMOVE_PATCH_EQUIVALENT_KEEP_BRANCH,
            "all_branch_commits_patch_equivalent",
            eligible=True,
        )
    return result(KEEP_UNCERTAIN, "cherry_unparseable")
