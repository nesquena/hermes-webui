"""Regression tests for rollback checkpoint diff symlink disclosure."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from api import rollback
import api.workspace as workspace_mod


def _commit_checkpoint_file(repo: Path, rel: str, content: str) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", rel], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "checkpoint"], check=True)


def _init_checkpoint(tmp_path, monkeypatch):
    hermes_home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    hermes_home.mkdir()
    workspace.mkdir()
    ws_hash = rollback._workspace_hash(str(workspace.resolve()))
    checkpoint = "abc123"
    ckpt_dir = hermes_home / "checkpoints" / ws_hash / checkpoint
    ckpt_dir.mkdir(parents=True)
    subprocess.run(["git", "-C", str(ckpt_dir), "init"], check=True)
    subprocess.run(["git", "-C", str(ckpt_dir), "config", "user.email", "test@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(ckpt_dir), "config", "user.name", "Test"], check=True)
    monkeypatch.setattr(rollback, "_hermes_home", lambda: hermes_home)
    monkeypatch.setattr(workspace_mod, "load_workspaces", lambda: [{"path": str(workspace)}])
    return workspace, ckpt_dir, checkpoint


def test_checkpoint_diff_does_not_follow_checkpoint_symlink(tmp_path, monkeypatch):
    workspace, ckpt_dir, checkpoint = _init_checkpoint(tmp_path, monkeypatch)
    secret = tmp_path / "outside-secret.txt"
    secret.write_text("SAFE_SECRET_MARKER_SHOULD_NOT_APPEAR\n", encoding="utf-8")

    os.symlink(secret, ckpt_dir / "leak.txt")
    subprocess.run(["git", "-C", str(ckpt_dir), "add", "leak.txt"], check=True)
    subprocess.run(["git", "-C", str(ckpt_dir), "commit", "-m", "checkpoint"], check=True)

    result = rollback.get_checkpoint_diff(str(workspace), checkpoint)

    assert result["files_changed"] == []
    assert "SAFE_SECRET_MARKER_SHOULD_NOT_APPEAR" not in result["diff"]
    assert "leak.txt" not in result["diff"]


def test_checkpoint_diff_does_not_follow_workspace_symlink_escape(tmp_path, monkeypatch):
    workspace, ckpt_dir, checkpoint = _init_checkpoint(tmp_path, monkeypatch)
    _commit_checkpoint_file(ckpt_dir, "leak.txt", "checkpoint content\n")
    secret = tmp_path / "outside-secret.txt"
    secret.write_text("WORKSPACE_SECRET_MARKER_SHOULD_NOT_APPEAR\n", encoding="utf-8")
    os.symlink(secret, workspace / "leak.txt")

    result = rollback.get_checkpoint_diff(str(workspace), checkpoint)

    assert "WORKSPACE_SECRET_MARKER_SHOULD_NOT_APPEAR" not in result["diff"]
    assert "checkpoint content" in result["diff"]


def test_restore_checkpoint_skips_checkpoint_symlink_sources(tmp_path, monkeypatch):
    workspace, ckpt_dir, checkpoint = _init_checkpoint(tmp_path, monkeypatch)
    secret = tmp_path / "outside-secret.txt"
    secret.write_text("RESTORE_SECRET_MARKER_SHOULD_NOT_COPY\n", encoding="utf-8")

    os.symlink(secret, ckpt_dir / "leak.txt")
    subprocess.run(["git", "-C", str(ckpt_dir), "add", "leak.txt"], check=True)
    subprocess.run(["git", "-C", str(ckpt_dir), "commit", "-m", "checkpoint"], check=True)

    result = rollback.restore_checkpoint(str(workspace), checkpoint)

    assert result["files_restored"] == []
    assert not (workspace / "leak.txt").exists()
    assert "RESTORE_SECRET_MARKER_SHOULD_NOT_COPY" not in "\n".join(
        p.read_text(encoding="utf-8", errors="replace")
        for p in workspace.rglob("*")
        if p.is_file()
    )


def test_restore_checkpoint_reads_git_blob_after_worktree_symlink_swap(tmp_path, monkeypatch):
    workspace, ckpt_dir, checkpoint = _init_checkpoint(tmp_path, monkeypatch)
    _commit_checkpoint_file(ckpt_dir, "file.txt", "checkpoint blob content\n")
    secret = tmp_path / "outside-secret.txt"
    secret.write_text("POST_COMMIT_SECRET_MARKER_SHOULD_NOT_COPY\n", encoding="utf-8")

    (ckpt_dir / "file.txt").unlink()
    os.symlink(secret, ckpt_dir / "file.txt")

    result = rollback.restore_checkpoint(str(workspace), checkpoint)

    assert result["files_restored"] == ["file.txt"]
    assert (workspace / "file.txt").read_text(encoding="utf-8") == "checkpoint blob content\n"
    assert "POST_COMMIT_SECRET_MARKER_SHOULD_NOT_COPY" not in (workspace / "file.txt").read_text(
        encoding="utf-8"
    )


def test_checkpoint_diff_renders_large_workspace_file_as_modified_not_deleted(tmp_path, monkeypatch):
    """Regression (Codex gate): a large but legitimate workspace file (> the
    read_file_content MAX_FILE_BYTES 400KB cap) that differs from the checkpoint
    must render as MODIFIED, not be silently dropped to None and reported as
    DELETED. The symlink-safe workspace reader must have no size cap.
    """
    workspace, ckpt_dir, checkpoint = _init_checkpoint(tmp_path, monkeypatch)
    # Checkpoint has a small version of the file.
    _commit_checkpoint_file(ckpt_dir, "big.txt", "old small checkpoint content\n")
    # Workspace has a LARGE (> 400KB) modified version of the same file.
    big_content = "X" * (500 * 1024) + "\nMODIFIED_LARGE_MARKER\n"
    (workspace / "big.txt").write_text(big_content, encoding="utf-8")

    result = rollback.get_checkpoint_diff(str(workspace), checkpoint)

    # The file must appear in the diff as a change, NOT be treated as deleted.
    assert "big.txt" in result["diff"]
    statuses = {f["file"]: f.get("status") for f in result["files_changed"]}
    assert statuses.get("big.txt") == "modified", (
        "a large modified workspace file must render as modified, not deleted "
        "(the capped read_file_content path regressed this): got "
        f"{statuses.get('big.txt')!r}"
    )


def test_checkpoint_diff_skips_workspace_fifo_without_hanging(tmp_path, monkeypatch):
    """Regression (Codex gate): a workspace FIFO/special file at a checkpoint-
    tracked regular-file path must NOT hang the diff (open_anchored_fd opens
    blocking O_RDONLY, which blocks forever on a FIFO with no writer) and must
    not leak an fd — the leaf type is pre-checked via lstat before any open.
    """
    import threading

    workspace, ckpt_dir, checkpoint = _init_checkpoint(tmp_path, monkeypatch)
    _commit_checkpoint_file(ckpt_dir, "pipe.txt", "checkpoint content\n")
    # Replace the workspace-side file with a FIFO (no writer → blocking open hangs).
    os.mkfifo(workspace / "pipe.txt")

    result_box = {}

    def _run():
        result_box["r"] = rollback.get_checkpoint_diff(str(workspace), checkpoint)

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    t.join(timeout=10)
    assert not t.is_alive(), "get_checkpoint_diff hung on a workspace FIFO"
    # FIFO workspace side is treated as absent → checkpoint file renders as deleted,
    # never blocks and never leaks the secret/hangs.
    statuses = {f["file"]: f.get("status") for f in result_box["r"]["files_changed"]}
    assert statuses.get("pipe.txt") == "deleted"
