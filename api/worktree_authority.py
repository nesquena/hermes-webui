"""Durable, process-safe single-writer authority for linked Git worktrees."""
from __future__ import annotations

import hashlib
import os
import sqlite3
import subprocess
import time
from pathlib import Path


class WorktreeOwnershipError(RuntimeError):
    """A writable worktree operation has no valid exclusive owner."""


def _clean_git_env() -> dict[str, str]:
    env = os.environ.copy()
    for name in (
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_COMMON_DIR",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    ):
        env.pop(name, None)
    return env


def _git_value(root: Path, selector: str) -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", selector],
            cwd=root,
            env=_clean_git_env(),
            text=True,
            capture_output=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise WorktreeOwnershipError("unable to establish Git worktree identity") from exc
    value = result.stdout.strip()
    if result.returncode != 0 or not value:
        raise WorktreeOwnershipError("unable to establish Git worktree identity")
    path = Path(value)
    if not path.is_absolute():
        path = root / path
    try:
        return str(path.resolve(strict=True))
    except (OSError, RuntimeError) as exc:
        raise WorktreeOwnershipError("unable to establish Git worktree identity") from exc


def _git_paths(workspace: str | Path) -> tuple[str, str]:
    try:
        root = Path(workspace).expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise WorktreeOwnershipError("workspace does not exist") from exc
    if not root.is_dir():
        raise WorktreeOwnershipError("workspace is not a directory")
    common = _git_value(root, "--git-common-dir")
    git_dir = _git_value(root, "--git-dir")
    return common, git_dir


def canonical_worktree_identity(workspace: str | Path) -> str:
    """Identity is the canonical git-common-dir + git-dir pair, never pathname."""
    common, git_dir = _git_paths(workspace)
    return hashlib.sha256(f"{common}\0{git_dir}".encode()).hexdigest()


def is_linked_worktree(workspace: str | Path) -> bool:
    """Return linked-worktree state; non-Git directories are plain workspaces."""
    try:
        root = Path(workspace).expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise WorktreeOwnershipError("workspace does not exist") from exc
    if not root.is_dir():
        raise WorktreeOwnershipError("workspace is not a directory")
    try:
        probe = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=root,
            env=_clean_git_env(),
            text=True,
            capture_output=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise WorktreeOwnershipError("unable to inspect Git worktree state") from exc
    if probe.returncode != 0:
        return False
    if probe.stdout.strip().lower() != "true":
        raise WorktreeOwnershipError("unexpected Git worktree state")
    common, git_dir = _git_paths(root)
    return common != git_dir


class WorktreeAuthority:
    def __init__(self, database: str | Path):
        self.database = Path(database)
        self.database.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS claims ("
                "identity TEXT PRIMARY KEY, owner TEXT NOT NULL, "
                "common_dir TEXT NOT NULL, git_dir TEXT NOT NULL, updated_at REAL NOT NULL)"
            )

    def _connect(self):
        conn = sqlite3.connect(self.database, timeout=10, isolation_level=None)
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    def _identity(self, workspace):
        common, git_dir = _git_paths(workspace)
        if common == git_dir:
            raise WorktreeOwnershipError("workspace is not a linked Git worktree")
        identity = hashlib.sha256(f"{common}\0{git_dir}".encode()).hexdigest()
        return identity, common, git_dir

    def claim(self, workspace, session_id: str) -> str:
        if not session_id:
            raise WorktreeOwnershipError("owner session id is required")
        identity, common, git_dir = self._identity(workspace)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT owner FROM claims WHERE identity=?", (identity,)
            ).fetchone()
            if row and row[0] != session_id:
                conn.rollback()
                raise WorktreeOwnershipError("worktree is owned by another session")
            conn.execute(
                "INSERT INTO claims VALUES(?,?,?,?,?) "
                "ON CONFLICT(identity) DO UPDATE SET updated_at=excluded.updated_at",
                (identity, session_id, common, git_dir, time.time()),
            )
            conn.commit()
        return identity

    def assert_owner(self, workspace, session_id: str) -> None:
        identity, _, _ = self._identity(workspace)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT owner FROM claims WHERE identity=?", (identity,)
            ).fetchone()
        if not row:
            raise WorktreeOwnershipError("linked worktree has no owner")
        if row[0] != session_id:
            raise WorktreeOwnershipError("worktree is owned by another session")

    def transfer(self, workspace, old_session_id: str, new_session_id: str) -> None:
        if not old_session_id or not new_session_id:
            raise WorktreeOwnershipError("both transfer session ids are required")
        identity, _, _ = self._identity(workspace)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            changed = conn.execute(
                "UPDATE claims SET owner=?, updated_at=? "
                "WHERE identity=? AND owner=?",
                (new_session_id, time.time(), identity, old_session_id),
            ).rowcount
            if changed != 1:
                conn.rollback()
                raise WorktreeOwnershipError("worktree transfer source is not the owner")
            conn.commit()

    def release_after_removal(self, identity: str, session_id: str, repo_root: str | Path) -> None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT git_dir, owner FROM claims WHERE identity=?", (identity,)
            ).fetchone()
        if not row or row[1] != session_id:
            raise WorktreeOwnershipError("session is not the worktree owner")
        git_dir = row[0]
        try:
            registered = subprocess.run(
                ["git", "--git-dir", git_dir, "rev-parse", "--git-dir"],
                cwd=Path(repo_root).resolve(),
                env=_clean_git_env(),
                text=True,
                capture_output=True,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise WorktreeOwnershipError("unable to verify worktree removal") from exc
        if registered.returncode == 0:
            raise WorktreeOwnershipError("worktree is still registered")
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            changed = conn.execute(
                "DELETE FROM claims WHERE identity=? AND owner=?",
                (identity, session_id),
            ).rowcount
            if changed != 1:
                conn.rollback()
                raise WorktreeOwnershipError("session is not the worktree owner")
            conn.commit()


def default_authority() -> WorktreeAuthority:
    from api.config import STATE_DIR
    return WorktreeAuthority(Path(STATE_DIR) / "worktree-ownership.sqlite3")


def assert_workspace_owner(workspace: str | Path, session_id: str) -> None:
    if not is_linked_worktree(workspace):
        return
    default_authority().assert_owner(workspace, str(session_id or ""))


def assert_session_owner(session) -> None:
    workspace = getattr(session, "workspace", None)
    if not workspace:
        return
    assert_workspace_owner(workspace, str(getattr(session, "session_id", "")))
