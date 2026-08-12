import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import pytest

from api.worktree_gc_inventory import (
    HealthProbe,
    ProcessScan,
    audit_managed_worktrees,
    load_managed_worktree_sessions,
)


NOW = datetime(2026, 7, 23, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(scope="session", autouse=True)
def test_server():
    """These unit tests do not need the repository's HTTP server fixture."""


@dataclass(frozen=True)
class FakeGitDecision:
    path: str
    branch: str
    repo_root: str
    target_ref: str
    verdict: str = "REMOVE_ANCESTOR"
    eligible: bool = True
    reasons: tuple[str, ...] = ("branch_is_ancestor",)


class FakeGitBackend:
    def __init__(self):
        self.classify_calls = []

    def classify_git_worktree(self, path, branch, repo_root, *, target_ref):
        self.classify_calls.append((path, branch, repo_root, target_ref))
        return FakeGitDecision(path, branch, repo_root, target_ref)


def _write_session(
    state_dir: Path,
    session_id: str,
    worktree: Path,
    repo: Path,
    **overrides,
) -> Path:
    sessions = state_dir / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    payload = {
        "session_id": session_id,
        "profile": "default",
        "archived": True,
        "workspace": str(worktree),
        "updated_at": NOW.timestamp() - 30 * 86400,
        "worktree_path": str(worktree),
        "worktree_branch": f"hermes/{session_id}",
        "worktree_repo_root": str(repo),
        "worktree_created_at": NOW.timestamp() - 30 * 86400,
        "active_stream_id": None,
        "pending_user_message": None,
        "pending_attachments": [],
        "pending_started_at": None,
        "messages": [{"role": "user", "content": "must never reach reports"}],
        "title": "must never reach reports",
    }
    payload.update(overrides)
    path = sessions / f"{session_id}.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _safe_health(_url):
    return HealthProbe(reachable=True, active_runs=0)


def _empty_process_scan():
    return ProcessScan(available=True, complete=True, process_cwds=())


def _write_workspace_session(
    state_dir: Path,
    session_id: str,
    workspace: Path,
    **overrides,
) -> Path:
    sessions = state_dir / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    payload = {
        "session_id": session_id,
        "profile": "default",
        "archived": True,
        "workspace": str(workspace),
        "updated_at": NOW.timestamp() - 30 * 86400,
        "active_stream_id": None,
        "pending_user_message": None,
        "pending_attachments": [],
        "pending_started_at": None,
        "messages": [{"role": "user", "content": "private derived content"}],
        "title": "private derived title",
    }
    payload.update(overrides)
    path = sessions / f"{session_id}.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _audit(state_dir, repo, backend, **kwargs):
    process_scan = kwargs.pop("process_scan", _empty_process_scan())
    return audit_managed_worktrees(
        state_dir=state_dir,
        profile="default",
        repo_filter=repo,
        min_age_days=7,
        target_ref="origin/master",
        git_backend=backend,
        health_url="http://127.0.0.1:8787/health",
        health_probe=_safe_health,
        process_scan=process_scan,
        now=NOW,
        **kwargs,
    )


def test_session_loader_filters_profile_and_repo_by_canonical_equality(tmp_path):
    state_dir = tmp_path / "state"
    repo = tmp_path / "repo"
    other_repo = tmp_path / "repo-copy"
    worktrees = tmp_path / "worktrees"
    repo.mkdir()
    other_repo.mkdir()
    worktrees.mkdir()
    _write_session(state_dir, "included", worktrees / "included", repo)
    _write_session(
        state_dir,
        "legacy-default",
        worktrees / "legacy",
        repo,
        profile_key_is_absent=True,
    )
    legacy_path = state_dir / "sessions" / "legacy-default.json"
    legacy = json.loads(legacy_path.read_text(encoding="utf-8"))
    legacy.pop("profile")
    legacy.pop("profile_key_is_absent")
    legacy_path.write_text(json.dumps(legacy), encoding="utf-8")
    _write_session(
        state_dir,
        "wrong-profile",
        worktrees / "wrong-profile",
        repo,
        profile="other",
    )
    _write_session(
        state_dir,
        "explicit-null-profile",
        worktrees / "null-profile",
        repo,
        profile=None,
    )
    _write_session(
        state_dir,
        "wrong-repo",
        worktrees / "wrong-repo",
        other_repo,
    )
    (state_dir / "sessions" / "_index.json").write_text("{}", encoding="utf-8")
    (state_dir / "sessions" / "ignored.json.tmp.1").write_text(
        "{not json", encoding="utf-8"
    )

    candidates = load_managed_worktree_sessions(
        state_dir,
        profile="default",
        repo_filter=repo,
    )

    assert [candidate.session_id for candidate in candidates] == [
        "included",
        "legacy-default",
    ]


def test_repo_filter_is_canonicalized_from_a_relative_cli_path(
    tmp_path,
    monkeypatch,
):
    state_dir = tmp_path / "state"
    repo = tmp_path / "repo"
    worktree = tmp_path / "worktree"
    repo.mkdir()
    worktree.mkdir()
    _write_session(state_dir, "relative-repo", worktree, repo)
    monkeypatch.chdir(tmp_path)

    candidates = load_managed_worktree_sessions(
        state_dir,
        profile="default",
        repo_filter=Path("repo"),
    )

    assert [candidate.session_id for candidate in candidates] == [
        "relative-repo"
    ]


@pytest.mark.parametrize(
    ("overrides", "expected_verdict"),
    [
        ({"archived": False}, "KEEP_NOT_ARCHIVED"),
        (
            {"worktree_created_at": NOW.timestamp() - 2 * 86400},
            "KEEP_RECENT",
        ),
        (
            {"worktree_created_at": None, "updated_at": None},
            "KEEP_UNCERTAIN",
        ),
        ({"worktree_created_at": "not-a-date"}, "KEEP_UNCERTAIN"),
        ({"worktree_branch": "not a valid branch"}, "KEEP_UNCERTAIN"),
        ({"active_stream_id": "stream-1"}, "KEEP_ACTIVE"),
        ({"pending_user_message": "queued"}, "KEEP_ACTIVE"),
        ({"pending_attachments": [{"name": "secret.txt"}]}, "KEEP_ACTIVE"),
        ({"pending_started_at": NOW.timestamp()}, "KEEP_ACTIVE"),
    ],
)
def test_session_guards_stop_candidates_before_git(
    tmp_path,
    overrides,
    expected_verdict,
):
    state_dir = tmp_path / "state"
    repo = tmp_path / "repo"
    worktree = tmp_path / "worktree"
    repo.mkdir()
    worktree.mkdir()
    _write_session(state_dir, "guarded", worktree, repo, **overrides)
    backend = FakeGitBackend()

    report = _audit(state_dir, repo, backend)

    assert backend.classify_calls == []
    assert report["candidates"][0]["verdict"] == expected_verdict
    serialized = json.dumps(report)
    assert "must never reach reports" not in serialized
    assert "secret.txt" not in serialized


def test_missing_creation_date_falls_back_to_old_updated_at(tmp_path):
    state_dir = tmp_path / "state"
    repo = tmp_path / "repo"
    worktree = tmp_path / "worktree"
    repo.mkdir()
    worktree.mkdir()
    _write_session(
        state_dir,
        "fallback-date",
        worktree,
        repo,
        worktree_created_at=None,
        updated_at=NOW.timestamp() - 30 * 86400,
    )
    backend = FakeGitBackend()

    report = _audit(state_dir, repo, backend)

    assert len(backend.classify_calls) == 1
    assert report["candidates"][0]["age_source"] == "updated_at"
    assert report["candidates"][0]["verdict"] == "REMOVE_ANCESTOR"


@pytest.mark.parametrize(
    "probe",
    [
        HealthProbe(reachable=True, active_runs=1),
        HealthProbe(reachable=False, active_runs=None, reason="unreachable"),
    ],
)
def test_health_activity_or_failure_blocks_all_git_classification(
    tmp_path,
    probe,
):
    state_dir = tmp_path / "state"
    repo = tmp_path / "repo"
    worktree = tmp_path / "worktree"
    repo.mkdir()
    worktree.mkdir()
    _write_session(state_dir, "health-guarded", worktree, repo)
    backend = FakeGitBackend()

    report = audit_managed_worktrees(
        state_dir=state_dir,
        profile="default",
        repo_filter=repo,
        min_age_days=7,
        target_ref="origin/master",
        git_backend=backend,
        health_url="http://127.0.0.1:8787/health",
        health_probe=lambda _url: probe,
        process_scan=_empty_process_scan(),
        now=NOW,
    )

    assert backend.classify_calls == []
    assert report["mode"] == "dry-run"
    assert report["collection_requested"] is False
    assert report["has_blocking_anomalies"] is True
    assert report["candidates"][0]["verdict"] in {"KEEP_ACTIVE", "KEEP_UNCERTAIN"}


def test_incomplete_process_scan_blocks_git_classification(tmp_path):
    state_dir = tmp_path / "state"
    repo = tmp_path / "repo"
    worktree = tmp_path / "worktree"
    repo.mkdir()
    worktree.mkdir()
    _write_session(state_dir, "proc-uncertain", worktree, repo)
    backend = FakeGitBackend()

    report = _audit(
        state_dir,
        repo,
        backend,
        process_scan=ProcessScan(
            available=False,
            complete=False,
            process_cwds=(),
            error="PermissionError",
        ),
    )

    assert backend.classify_calls == []
    assert report["has_blocking_anomalies"] is True
    assert report["candidates"][0]["verdict"] == "KEEP_UNCERTAIN"
    assert "process_scan_incomplete" in report["candidates"][0]["reasons"]


def test_unreadable_sidecar_blocks_collection_without_exposing_contents(tmp_path):
    state_dir = tmp_path / "state"
    repo = tmp_path / "repo"
    worktree = tmp_path / "worktree"
    repo.mkdir()
    worktree.mkdir()
    _write_session(state_dir, "valid", worktree, repo)
    (state_dir / "sessions" / "broken.json").write_text(
        '{"secret": "must-not-leak"',
        encoding="utf-8",
    )
    backend = FakeGitBackend()

    report = _audit(state_dir, repo, backend)

    assert backend.classify_calls == []
    assert report["has_blocking_anomalies"] is True
    assert report["session_scan"]["errors"] == 1
    assert "must-not-leak" not in json.dumps(report)


def test_missing_sessions_directory_is_a_blocking_inventory_failure(tmp_path):
    state_dir = tmp_path / "state"
    repo = tmp_path / "repo"
    state_dir.mkdir()
    repo.mkdir()
    backend = FakeGitBackend()

    report = _audit(state_dir, repo, backend)

    assert backend.classify_calls == []
    assert report["candidates"] == []
    assert report["has_blocking_anomalies"] is True
    assert report["global_reasons"] == ["session_scan_incomplete"]
    assert report["session_scan"] == {
        "sidecars_scanned": 0,
        "errors": 1,
        "error_kinds": ["session_directory_missing"],
    }


def test_contradictory_duplicate_path_is_uncertain(tmp_path):
    state_dir = tmp_path / "state"
    repo = tmp_path / "repo"
    worktree = tmp_path / "worktree"
    repo.mkdir()
    worktree.mkdir()
    _write_session(state_dir, "duplicate-a", worktree, repo)
    _write_session(
        state_dir,
        "duplicate-b",
        worktree,
        repo,
        worktree_branch="hermes/different",
    )
    backend = FakeGitBackend()

    report = _audit(state_dir, repo, backend)

    assert backend.classify_calls == []
    assert len(report["candidates"]) == 1
    assert report["candidates"][0]["verdict"] == "KEEP_UNCERTAIN"
    assert "duplicate_conflict" in report["candidates"][0]["reasons"]
    assert report["candidates"][0]["session_ids"] == [
        "duplicate-a",
        "duplicate-b",
    ]


def test_duplicate_repo_conflict_is_not_hidden_by_repo_filter(tmp_path):
    state_dir = tmp_path / "state"
    repo = tmp_path / "repo"
    other_repo = tmp_path / "other-repo"
    worktree = tmp_path / "worktree"
    repo.mkdir()
    other_repo.mkdir()
    worktree.mkdir()
    _write_session(
        state_dir,
        "duplicate-a",
        worktree,
        other_repo,
        worktree_branch="hermes/shared",
    )
    _write_session(
        state_dir,
        "duplicate-b",
        worktree,
        repo,
        worktree_branch="hermes/shared",
    )
    backend = FakeGitBackend()

    report = _audit(state_dir, repo, backend)

    assert backend.classify_calls == []
    assert len(report["candidates"]) == 1
    assert report["candidates"][0]["worktree_repo_root"] == str(repo.resolve())
    assert report["candidates"][0]["verdict"] == "KEEP_UNCERTAIN"
    assert "duplicate_conflict" in report["candidates"][0]["reasons"]


def test_equivalent_duplicate_path_with_different_session_recency_is_deduplicated(
    tmp_path,
):
    state_dir = tmp_path / "state"
    repo = tmp_path / "repo"
    worktree = tmp_path / "worktree"
    repo.mkdir()
    worktree.mkdir()
    _write_session(
        state_dir,
        "duplicate-a",
        worktree,
        repo,
        worktree_branch="hermes/shared",
        updated_at=NOW.timestamp() - 20 * 86400,
    )
    _write_session(
        state_dir,
        "duplicate-b",
        worktree,
        repo,
        worktree_branch="hermes/shared",
        updated_at=NOW.timestamp() - 10 * 86400,
    )
    backend = FakeGitBackend()

    report = _audit(state_dir, repo, backend)

    assert len(backend.classify_calls) == 1
    assert len(report["candidates"]) == 1
    assert report["candidates"][0]["verdict"] == "REMOVE_ANCESTOR"
    assert report["candidates"][0]["session_ids"] == [
        "duplicate-a",
        "duplicate-b",
    ]


def test_complete_old_inactive_session_reaches_git_backend(tmp_path):
    state_dir = tmp_path / "state"
    repo = tmp_path / "repo"
    worktree = tmp_path / "worktree"
    repo.mkdir()
    worktree.mkdir()
    _write_session(state_dir, "eligible", worktree, repo)
    backend = FakeGitBackend()

    report = _audit(state_dir, repo, backend)

    assert backend.classify_calls == [
        (
            str(worktree.resolve()),
            "hermes/eligible",
            str(repo.resolve()),
            "origin/master",
        )
    ]
    assert report["mode"] == "dry-run"
    assert report["collection_requested"] is False
    assert "collection_allowed" not in report
    assert report["candidates"][0]["eligible"] is True
    assert report["candidates"][0]["verdict"] == "REMOVE_ANCESTOR"


def test_non_archived_fork_without_worktree_fields_blocks_shared_workspace(
    tmp_path,
):
    state_dir = tmp_path / "state"
    repo = tmp_path / "repo"
    worktree = tmp_path / "worktree"
    repo.mkdir()
    worktree.mkdir()
    _write_session(state_dir, "managed", worktree, repo)
    _write_workspace_session(
        state_dir,
        "fork",
        worktree,
        archived=False,
    )
    backend = FakeGitBackend()

    report = _audit(state_dir, repo, backend)

    assert backend.classify_calls == []
    assert report["candidates"][0]["verdict"] == "KEEP_NOT_ARCHIVED"
    assert report["candidates"][0]["session_ids"] == ["fork", "managed"]
    serialized = json.dumps(report)
    assert "private derived content" not in serialized
    assert "private derived title" not in serialized


def test_recent_archived_duplicate_without_worktree_fields_blocks(tmp_path):
    state_dir = tmp_path / "state"
    repo = tmp_path / "repo"
    worktree = tmp_path / "worktree"
    repo.mkdir()
    worktree.mkdir()
    _write_session(state_dir, "managed", worktree, repo)
    _write_workspace_session(
        state_dir,
        "duplicate",
        worktree,
        updated_at=NOW.timestamp() - 2 * 86400,
    )
    backend = FakeGitBackend()

    report = _audit(state_dir, repo, backend)

    assert backend.classify_calls == []
    assert report["candidates"][0]["verdict"] == "KEEP_RECENT"
    assert report["candidates"][0]["session_ids"] == ["duplicate", "managed"]
    assert "linked_session_younger_than_min_age" in report["candidates"][0]["reasons"]


def test_workspace_prefix_lookalike_does_not_block(tmp_path):
    state_dir = tmp_path / "state"
    repo = tmp_path / "repo"
    worktree = tmp_path / "worktree"
    lookalike = tmp_path / "worktree-copy"
    repo.mkdir()
    worktree.mkdir()
    lookalike.mkdir()
    _write_session(state_dir, "managed", worktree, repo)
    _write_workspace_session(
        state_dir,
        "lookalike",
        lookalike,
        archived=False,
        active_stream_id="active",
    )
    backend = FakeGitBackend()

    report = _audit(state_dir, repo, backend)

    assert len(backend.classify_calls) == 1
    assert report["candidates"][0]["verdict"] == "REMOVE_ANCESTOR"
    assert report["candidates"][0]["session_ids"] == ["managed"]


def test_descendant_workspace_blocks(tmp_path):
    state_dir = tmp_path / "state"
    repo = tmp_path / "repo"
    worktree = tmp_path / "worktree"
    descendant = worktree / "nested" / "project"
    repo.mkdir()
    descendant.mkdir(parents=True)
    _write_session(state_dir, "managed", worktree, repo)
    _write_workspace_session(
        state_dir,
        "descendant",
        descendant,
        archived=False,
    )
    backend = FakeGitBackend()

    report = _audit(state_dir, repo, backend)

    assert backend.classify_calls == []
    assert report["candidates"][0]["verdict"] == "KEEP_NOT_ARCHIVED"
    assert report["candidates"][0]["session_ids"] == ["descendant", "managed"]


def test_different_profile_shared_workspace_does_not_block(tmp_path):
    state_dir = tmp_path / "state"
    repo = tmp_path / "repo"
    worktree = tmp_path / "worktree"
    repo.mkdir()
    worktree.mkdir()
    _write_session(state_dir, "managed", worktree, repo)
    _write_workspace_session(
        state_dir,
        "other-profile",
        worktree,
        profile="other",
        archived=False,
        active_stream_id="active",
    )
    backend = FakeGitBackend()

    report = _audit(state_dir, repo, backend)

    assert len(backend.classify_calls) == 1
    assert report["candidates"][0]["verdict"] == "REMOVE_ANCESTOR"
    assert report["candidates"][0]["session_ids"] == ["managed"]


@pytest.mark.parametrize(
    "overrides",
    [
        {"active_stream_id": "stream"},
        {"pending_user_message": "queued"},
        {"pending_attachments": [{"name": "private.txt"}]},
        {"pending_started_at": NOW.timestamp()},
    ],
)
def test_linked_workspace_activity_blocks(tmp_path, overrides):
    state_dir = tmp_path / "state"
    repo = tmp_path / "repo"
    worktree = tmp_path / "worktree"
    repo.mkdir()
    worktree.mkdir()
    _write_session(state_dir, "managed", worktree, repo)
    _write_workspace_session(
        state_dir,
        "active-derived",
        worktree,
        **overrides,
    )
    backend = FakeGitBackend()

    report = _audit(state_dir, repo, backend)

    assert backend.classify_calls == []
    assert report["candidates"][0]["verdict"] == "KEEP_ACTIVE"
    assert report["candidates"][0]["session_ids"] == [
        "active-derived",
        "managed",
    ]
    assert "private.txt" not in json.dumps(report)


def test_linked_workspace_invalid_date_is_uncertain(tmp_path):
    state_dir = tmp_path / "state"
    repo = tmp_path / "repo"
    worktree = tmp_path / "worktree"
    repo.mkdir()
    worktree.mkdir()
    _write_session(state_dir, "managed", worktree, repo)
    _write_workspace_session(
        state_dir,
        "invalid-date",
        worktree,
        updated_at="not-a-date",
    )
    backend = FakeGitBackend()

    report = _audit(state_dir, repo, backend)

    assert backend.classify_calls == []
    assert report["candidates"][0]["verdict"] == "KEEP_UNCERTAIN"
    assert "linked_session_invalid_or_missing_age" in report["candidates"][0]["reasons"]


def test_creation_lock_pid_alone_does_not_count_as_activity(tmp_path):
    state_dir = tmp_path / "state"
    repo = tmp_path / "repo"
    worktree = tmp_path / "worktree"
    repo.mkdir()
    worktree.mkdir()
    _write_session(
        state_dir,
        "bookkeeping-lock",
        worktree,
        repo,
        worktree_lock_pid=99999,
    )
    backend = FakeGitBackend()

    report = _audit(state_dir, repo, backend)

    assert len(backend.classify_calls) == 1
    assert report["candidates"][0]["verdict"] == "REMOVE_ANCESTOR"
