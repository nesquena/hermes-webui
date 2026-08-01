from __future__ import annotations

import io
import json
import time
from pathlib import Path

import api.worktree_gc_git as git_backend
from api.worktree_gc_inventory import (
    HealthProbe,
    ProcessScan,
    audit_managed_worktrees,
)
from scripts import worktree_gc as cli
from tests.test_worktree_gc_git_classification import (
    _git,
    add_worktree,
    make_remote_repo,
)


def _audit_without_host_activity(**kwargs):
    return audit_managed_worktrees(
        **kwargs,
        health_probe=lambda _url: HealthProbe(True, 0),
        process_scan=ProcessScan(True, True, ()),
    )


def _write_archived_sidecar(
    state_dir: Path,
    *,
    session_id: str,
    worktree: Path,
    branch: str,
    repo: Path,
) -> None:
    sessions = state_dir / "sessions"
    sessions.mkdir(parents=True)
    old_timestamp = time.time() - (8 * 24 * 60 * 60)
    (sessions / f"{session_id}.json").write_text(
        json.dumps(
            {
                "session_id": session_id,
                "profile": "default",
                "archived": True,
                "workspace": str(worktree),
                "worktree_path": str(worktree),
                "worktree_branch": branch,
                "worktree_repo_root": str(repo),
                "worktree_created_at": old_timestamp,
                "updated_at": old_timestamp,
                "active_stream_id": None,
                "pending_user_message": None,
                "pending_attachments": None,
                "pending_started_at": None,
            }
        ),
        encoding="utf-8",
    )


def _branch_exists(repo: Path, branch: str) -> bool:
    return (
        _git(
            repo,
            "show-ref",
            "--verify",
            "--quiet",
            f"refs/heads/{branch}",
            check=False,
        ).returncode
        == 0
    )


def test_real_git_audit_never_mutates_eligible_or_ignored_worktree(tmp_path):
    case = make_remote_repo(tmp_path)
    repo = case["repo"]
    assert isinstance(repo, Path)
    (repo / ".gitignore").write_text("/.env\n", encoding="utf-8")
    _git(repo, "add", ".gitignore")
    _git(repo, "commit", "-m", "ignore local environment")
    _git(repo, "push", "origin", "master")

    branch = "gc/integration-ancestor"
    worktree = add_worktree(case, tmp_path, branch, start="origin/master")
    state_dir = tmp_path / "state"
    report_path = tmp_path / "reports" / "worktree-gc.json"
    _write_archived_sidecar(
        state_dir,
        session_id="integration-session",
        worktree=worktree,
        branch=branch,
        repo=repo,
    )
    common_args = [
        "--repo",
        str(repo),
        "--state-dir",
        str(state_dir),
        "--health-url",
        "http://unused.invalid/health",
        "--report-path",
        str(report_path),
        "--min-age-days",
        "7",
        "--json",
    ]

    eligible_stdout = io.StringIO()
    eligible_rc = cli.main(
        common_args,
        git_backend=git_backend,
        audit_fn=_audit_without_host_activity,
        stdout=eligible_stdout,
    )
    eligible_report = json.loads(eligible_stdout.getvalue())

    assert eligible_rc == 0
    assert eligible_report["mode"] == "dry-run"
    assert eligible_report["collection_requested"] is False
    assert "collection" not in eligible_report
    assert eligible_report["counts"]["eligible"] == 1
    assert eligible_report["candidates"][0]["verdict"] == "REMOVE_ANCESTOR"
    assert worktree.is_dir()
    assert _branch_exists(repo, branch)

    (worktree / ".env").write_text("PRIVATE=must-not-be-read\n", encoding="utf-8")
    ignored_stdout = io.StringIO()
    ignored_rc = cli.main(
        common_args,
        git_backend=git_backend,
        audit_fn=_audit_without_host_activity,
        stdout=ignored_stdout,
    )
    ignored_report = json.loads(ignored_stdout.getvalue())
    persisted_report = json.loads(report_path.read_text(encoding="utf-8"))

    assert ignored_rc == 2
    assert ignored_report == persisted_report
    assert ignored_report["mode"] == "dry-run"
    assert ignored_report["collection_requested"] is False
    assert ignored_report["candidates"][0]["verdict"] == "KEEP_IGNORED_FILES"
    assert ignored_report["candidates"][0]["git"]["ignored_count"] == 1
    assert "must-not-be-read" not in json.dumps(ignored_report)
    assert worktree.is_dir()
    assert _branch_exists(repo, branch)
