from pathlib import Path

import api.worktree_gc_inventory as inventory
from api.worktree_gc_inventory import ManagedWorktreeCandidate, ProcessCwd, ProcessScan


def _candidate(path: Path, index: int) -> ManagedWorktreeCandidate:
    return ManagedWorktreeCandidate(
        session_id=f"managed-{index}",
        session_ids=(f"managed-{index}",),
        profile="default",
        worktree_path=str(path),
        worktree_branch=f"hermes/{index}",
        worktree_repo_root=str(path.parent),
        worktree_created_at=1.0,
        updated_at=1.0,
        archived=True,
        has_active_stream=False,
        has_pending_user_message=False,
        has_pending_attachments=False,
        has_pending_started_at=False,
    )


def _workspace(path: Path, index: int) -> inventory._WorkspaceSession:
    return inventory._WorkspaceSession(
        session_id=f"fork-{index}",
        workspace=str(path),
        updated_at=1.0,
        archived=True,
        has_active_stream=False,
        has_pending_user_message=False,
        has_pending_attachments=False,
        has_pending_started_at=False,
    )


def test_workspace_linking_is_indexed_instead_of_candidate_times_session(
    tmp_path, monkeypatch
):
    candidates = tuple(
        _candidate(tmp_path / "worktrees" / f"feature-{index}", index)
        for index in range(100)
    )
    sessions = [
        _workspace(Path(candidate.worktree_path) / "nested", index)
        for index, candidate in enumerate(candidates)
    ]
    original = inventory._path_is_within
    calls = 0

    def counted(child, parent):
        nonlocal calls
        calls += 1
        return original(child, parent)

    monkeypatch.setattr(inventory, "_path_is_within", counted)

    linked = inventory._attach_workspace_sessions(candidates, sessions)

    assert all(len(candidate.session_ids) == 2 for candidate in linked)
    assert calls < 2_000


def test_batch_process_counts_cover_nested_candidates_without_lookalikes(tmp_path):
    parent = tmp_path / "worktrees" / "feature"
    nested = parent / "nested"
    lookalike = tmp_path / "worktrees" / "feature-copy"
    scan = ProcessScan(
        available=True,
        complete=True,
        process_cwds=(
            ProcessCwd(1, str(parent)),
            ProcessCwd(2, str(nested / "child")),
            ProcessCwd(3, str(lookalike)),
        ),
    )

    counts = scan.blocking_process_counts((parent, nested, lookalike))

    assert counts == {
        str(parent.resolve()): 2,
        str(nested.resolve()): 1,
        str(lookalike.resolve()): 1,
    }
