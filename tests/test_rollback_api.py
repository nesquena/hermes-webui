import subprocess

from api import rollback


def _init_checkpoint_repo(path):
    subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True, text=True)
    subprocess.run(["git", "add", "."], cwd=path, check=True, capture_output=True, text=True)


def test_checkpoint_diff_reports_files_added_after_checkpoint(monkeypatch, tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "baseline.txt").write_text("baseline\n", encoding="utf-8")
    (workspace / "workspace-tool.json").write_text('{"kind":"metadata-only"}\n', encoding="utf-8")

    hermes_home = tmp_path / "hermes-home"
    checkpoint_id = "ckpt-added"
    checkpoint_dir = hermes_home / "checkpoints" / rollback._workspace_hash(str(workspace)) / checkpoint_id
    checkpoint_dir.mkdir(parents=True)
    (checkpoint_dir / "baseline.txt").write_text("baseline\n", encoding="utf-8")
    _init_checkpoint_repo(checkpoint_dir)

    monkeypatch.setattr(rollback, "_hermes_home", lambda: hermes_home)
    monkeypatch.setattr(rollback, "_resolve_workspace", lambda value: str(workspace))

    result = rollback.get_checkpoint_diff(str(workspace), checkpoint_id)

    assert result["total_changes"] == 1
    assert {"file": "workspace-tool.json", "status": "added"} in result["files_changed"]
    assert "--- /dev/null" in result["diff"]
    assert "+++ b/workspace-tool.json" in result["diff"]
    assert '+{"kind":"metadata-only"}' in result["diff"]
