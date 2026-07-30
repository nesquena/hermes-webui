import json
from pathlib import Path

import pytest

from api import workspace


def test_load_workspaces_preserves_unavailable_entries_on_disk(tmp_path, monkeypatch):
    """A transient stat/is_dir failure must not silently delete a saved workspace."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    existing = tmp_path / "existing"
    existing.mkdir()
    unavailable = tmp_path / "missing-or-inaccessible"
    ws_file = state_dir / "workspaces.json"
    raw = [
        {"path": str(existing), "name": "Existing"},
        {"path": str(unavailable), "name": "Unavailable"},
    ]
    ws_file.write_text(json.dumps(raw), encoding="utf-8")
    monkeypatch.setattr(workspace, "_workspaces_file", lambda: ws_file)

    loaded = workspace.load_workspaces()

    assert [w["path"] for w in loaded] == [str(existing.resolve()), str(unavailable.resolve())]
    assert json.loads(ws_file.read_text(encoding="utf-8")) == raw


def test_clean_workspace_list_still_renames_default_without_dropping_missing(tmp_path):
    missing = tmp_path / "temporarily-unavailable"

    cleaned = workspace._clean_workspace_list([
        {"path": str(missing), "name": "default"},
    ])

    assert cleaned == [{"path": str(missing.resolve()), "name": "Home"}]


def test_validate_workspace_to_add_distinguishes_permission_denied(monkeypatch, tmp_path):
    candidate = tmp_path / "Documents"
    candidate.mkdir()

    target = str(candidate.resolve())
    original_stat = Path.stat

    def fake_stat(self):
        if str(self) == target:
            raise PermissionError("Operation not permitted")
        return original_stat(self)

    monkeypatch.setattr(Path, "stat", fake_stat)

    with pytest.raises(ValueError) as excinfo:
        workspace.validate_workspace_to_add(str(candidate))

    message = str(excinfo.value)
    assert "Cannot access path" in message
    assert "Operation not permitted" in message
    assert "macOS" in message
    assert "Full Disk Access" in message


def test_resolve_trusted_workspace_distinguishes_missing_from_permission_denied(monkeypatch, tmp_path):
    candidate = tmp_path / "Documents"
    candidate.mkdir()

    target = str(candidate.resolve())
    original_stat = Path.stat

    def fake_stat(self):
        if str(self) == target:
            raise PermissionError("Operation not permitted")
        return original_stat(self)

    monkeypatch.setattr(Path, "stat", fake_stat)

    with pytest.raises(ValueError) as excinfo:
        workspace.resolve_trusted_workspace(str(candidate))

    assert "Cannot access path" in str(excinfo.value)
    assert "Path does not exist" not in str(excinfo.value)
