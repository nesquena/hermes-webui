import os
import subprocess

import pytest

from api import rollback


def _init_checkpoint_repo(path, files):
    path.mkdir(parents=True)
    subprocess.run(["git", "-C", str(path), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "Test"], check=True)
    for rel_path, content in files.items():
        target = path / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    subprocess.run(["git", "-C", str(path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-q", "-m", "checkpoint"], check=True)


@pytest.mark.skipif(os.open not in os.supports_dir_fd, reason="requires descriptor-relative opens")
@pytest.mark.parametrize("existing", [True, False])
def test_restore_keeps_authorized_root_when_path_is_replaced(monkeypatch, tmp_path, existing):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    if existing:
        (workspace / "victim.txt").write_text("OLD")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "victim.txt").write_text("UNTOUCHED")
    checkpoint_root = tmp_path / "checkpoints"
    checkpoint_id = "root-replacement"
    ckpt_dir = checkpoint_root / rollback._workspace_hash(str(workspace)) / checkpoint_id
    _init_checkpoint_repo(ckpt_dir, {"victim.txt": "RESTORED"})
    monkeypatch.setattr(rollback, "_resolve_workspace", lambda _workspace: str(workspace))
    monkeypatch.setattr(rollback, "_checkpoint_root", lambda: checkpoint_root)
    real_entries = rollback._checkpoint_entry_modes
    retained = tmp_path / "original-workspace"

    def replace_root(*args):
        entries = real_entries(*args)
        workspace.rename(retained)
        workspace.symlink_to(outside, target_is_directory=True)
        return entries

    monkeypatch.setattr(rollback, "_checkpoint_entry_modes", replace_root)
    result = rollback.restore_checkpoint(str(workspace), checkpoint_id)
    assert (outside / "victim.txt").read_text() == "UNTOUCHED"
    assert result["files_restored"] == ["victim.txt"]
    assert (retained / "victim.txt").read_text() == "RESTORED"


def test_restore_checkpoint_refuses_symlink_escape(monkeypatch, tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    outside_target = outside / "target.txt"
    outside_target.write_text("ORIGINAL")
    (workspace / "victim.txt").symlink_to(outside_target)

    checkpoint_root = tmp_path / "checkpoints"
    checkpoint_id = "checkpoint-1"
    ckpt_dir = checkpoint_root / rollback._workspace_hash(str(workspace.resolve())) / checkpoint_id
    _init_checkpoint_repo(ckpt_dir, {"victim.txt": "CHECKPOINT_MARKER"})

    monkeypatch.setattr(rollback, "_resolve_workspace", lambda workspace_arg: str(workspace.resolve()))
    monkeypatch.setattr(rollback, "_checkpoint_root", lambda: checkpoint_root)

    result = rollback.restore_checkpoint(str(workspace), checkpoint_id)

    assert result["ok"] is True
    assert result["files_restored"] == []
    assert result["files_restored_count"] == 0
    assert result["errors"]
    assert result["errors"][0]["file"] == "victim.txt"
    assert outside_target.read_text() == "ORIGINAL"


def test_restore_checkpoint_restores_regular_file(monkeypatch, tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "nested").mkdir()
    (workspace / "nested" / "file.txt").write_text("OLD")

    checkpoint_root = tmp_path / "checkpoints"
    checkpoint_id = "checkpoint-2"
    ckpt_dir = checkpoint_root / rollback._workspace_hash(str(workspace.resolve())) / checkpoint_id
    _init_checkpoint_repo(ckpt_dir, {"nested/file.txt": "NEW"})

    monkeypatch.setattr(rollback, "_resolve_workspace", lambda workspace_arg: str(workspace.resolve()))
    monkeypatch.setattr(rollback, "_checkpoint_root", lambda: checkpoint_root)

    result = rollback.restore_checkpoint(str(workspace), checkpoint_id)

    assert result["ok"] is True
    assert result["files_restored"] == ["nested/file.txt"]
    assert (workspace / "nested" / "file.txt").read_text() == "NEW"


def test_restore_checkpoint_creates_missing_regular_file(monkeypatch, tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    checkpoint_root = tmp_path / "checkpoints"
    checkpoint_id = "checkpoint-3"
    ckpt_dir = checkpoint_root / rollback._workspace_hash(str(workspace.resolve())) / checkpoint_id
    _init_checkpoint_repo(ckpt_dir, {"nested/new-file.txt": "NEW"})

    monkeypatch.setattr(rollback, "_resolve_workspace", lambda workspace_arg: str(workspace.resolve()))
    monkeypatch.setattr(rollback, "_checkpoint_root", lambda: checkpoint_root)

    result = rollback.restore_checkpoint(str(workspace), checkpoint_id)

    assert result["ok"] is True
    assert result["files_restored"] == ["nested/new-file.txt"]
    assert (workspace / "nested" / "new-file.txt").read_text() == "NEW"
