from __future__ import annotations

import io
import json
import os
from pathlib import Path

import pytest

from api.worktree_gc_inventory import write_report_atomic
from scripts import worktree_gc


@pytest.fixture(scope="session", autouse=True)
def test_server():
    """These CLI tests do not need the repository's HTTP server fixture."""


def _report(repo: Path, *, blocking: bool) -> dict:
    return {
        "schema_version": 1,
        "generated_at": "2026-07-23T12:00:00+00:00",
        "mode": "dry-run",
        "collection_requested": False,
        "profile": "default",
        "repo": str(repo),
        "target_ref": "origin/master",
        "min_age_days": 7,
        "global_reasons": ["health_unavailable"] if blocking else [],
        "health": {
            "reachable": not blocking,
            "active_runs": None if blocking else 0,
        },
        "process_scan": {
            "available": True,
            "complete": True,
            "process_count": 0,
            "unreadable_count": 0,
            "disappeared_count": 0,
        },
        "session_scan": {
            "sidecars_scanned": 0,
            "errors": 0,
            "error_kinds": [],
        },
        "candidates": [],
        "counts": {
            "candidates": 0,
            "eligible": 0,
            "blocking": int(blocking),
            "uncertain": int(blocking),
            "active": 0,
        },
        "has_blocking_anomalies": blocking,
    }


def test_help_describes_observation_only_and_has_no_collect_flag():
    help_text = worktree_gc.build_parser().format_help()

    assert "--dry-run" in help_text
    assert "--collect" not in help_text
    assert "collect" not in help_text.lower()
    assert "audit" in help_text.lower()


def test_help_exits_before_loading_git_backend(monkeypatch):
    monkeypatch.setattr(
        worktree_gc,
        "load_git_backend",
        lambda: pytest.fail("help must not load the backend"),
    )

    with pytest.raises(SystemExit) as exc:
        worktree_gc.main(["--help"])

    assert exc.value.code == 0


def test_removed_collect_flag_is_argparse_error_before_any_action(
    tmp_path,
    monkeypatch,
):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(
        worktree_gc,
        "load_git_backend",
        lambda: pytest.fail("unknown arguments must fail before backend load"),
    )
    monkeypatch.setattr(
        worktree_gc,
        "write_report_atomic",
        lambda *_args, **_kwargs: pytest.fail(
            "unknown arguments must fail before report mutation"
        ),
    )

    with pytest.raises(SystemExit) as exc:
        worktree_gc.main(
            ["--repo", str(repo), "--collect"],
            audit_fn=lambda **_kwargs: pytest.fail(
                "unknown arguments must fail before audit"
            ),
        )

    assert exc.value.code == 2


def test_audit_writes_identical_non_collection_report_and_returns_zero(tmp_path):
    repo = tmp_path / "repo"
    state_dir = tmp_path / "state"
    report_path = tmp_path / "reports" / "worktree-gc.json"
    repo.mkdir()
    stdout = io.StringIO()
    calls = []
    backend = object()

    def fake_audit(**kwargs):
        calls.append(kwargs)
        return _report(repo.resolve(), blocking=False)

    return_code = worktree_gc.main(
        [
            "--repo",
            str(repo),
            "--state-dir",
            str(state_dir),
            "--health-url",
            "http://127.0.0.1:9898/health",
            "--report-path",
            str(report_path),
            "--json",
        ],
        git_backend=backend,
        audit_fn=fake_audit,
        stdout=stdout,
    )
    emitted = json.loads(stdout.getvalue())
    persisted = json.loads(report_path.read_text(encoding="utf-8"))

    assert return_code == 0
    assert emitted == persisted
    assert emitted["mode"] == "dry-run"
    assert emitted["collection_requested"] is False
    assert "collection" not in emitted
    assert "collection_allowed" not in emitted
    assert len(calls) == 1
    assert calls[0] == {
        "state_dir": state_dir,
        "profile": "default",
        "repo_filter": repo,
        "min_age_days": 7,
        "target_ref": "origin/master",
        "git_backend": backend,
        "health_url": "http://127.0.0.1:9898/health",
    }


def test_blocking_audit_returns_two_and_still_persists_report(tmp_path):
    repo = tmp_path / "repo"
    report_path = tmp_path / "report.json"
    repo.mkdir()

    return_code = worktree_gc.main(
        [
            "--repo",
            str(repo),
            "--report-path",
            str(report_path),
        ],
        git_backend=object(),
        audit_fn=lambda **_kwargs: _report(repo.resolve(), blocking=True),
        stdout=io.StringIO(),
    )

    assert return_code == 2
    persisted = json.loads(report_path.read_text(encoding="utf-8"))
    assert persisted["mode"] == "dry-run"
    assert persisted["collection_requested"] is False
    assert persisted["has_blocking_anomalies"] is True


@pytest.mark.parametrize("value", ["0", "-1", "not-an-integer"])
def test_invalid_minimum_age_uses_argparse_exit_two(tmp_path, value):
    repo = tmp_path / "repo"
    repo.mkdir()

    with pytest.raises(SystemExit) as exc:
        worktree_gc.main(
            ["--repo", str(repo), "--min-age-days", value],
            audit_fn=lambda **_kwargs: pytest.fail("invalid args must not audit"),
        )

    assert exc.value.code == 2


def test_default_paths_stay_outside_checkout(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    env = {
        "HERMES_HOME": str(tmp_path / "hermes"),
        "XDG_STATE_HOME": str(tmp_path / "xdg"),
    }

    state_dir = worktree_gc.default_state_dir(env)
    report_path = worktree_gc.default_report_path(env)

    assert state_dir == tmp_path / "hermes" / "webui"
    assert report_path == (
        tmp_path / "xdg" / "hermes-webui" / "worktree-gc" / "report.json"
    )
    assert not worktree_gc._is_within(state_dir, worktree_gc.REPO_ROOT)
    assert not worktree_gc._is_within(report_path, worktree_gc.REPO_ROOT)


def test_atomic_report_replaces_only_after_complete_fsync(tmp_path, monkeypatch):
    report_path = tmp_path / "nested" / "report.json"
    report_path.parent.mkdir()
    report_path.write_text('{"old": true}\n', encoding="utf-8")
    real_replace = os.replace
    real_fsync = os.fsync
    events = []

    def observing_fsync(fd):
        events.append(("fsync", fd))
        return real_fsync(fd)

    def observing_replace(source, destination, **kwargs):
        events.append(("replace", Path(source), Path(destination)))
        source_path = Path(source)
        if not source_path.is_absolute():
            # dirfd-relative rename: the source names the pinned parent dir.
            source_path = report_path.parent / source_path
        assert json.loads(source_path.read_text(encoding="utf-8")) == {"new": True}
        assert json.loads(report_path.read_text(encoding="utf-8")) == {"old": True}
        return real_replace(source, destination, **kwargs)

    monkeypatch.setattr(os, "fsync", observing_fsync)
    monkeypatch.setattr(os, "replace", observing_replace)

    write_report_atomic({"new": True}, report_path)

    replace_index = next(
        index for index, event in enumerate(events) if event[0] == "replace"
    )
    assert any(event[0] == "fsync" for event in events[:replace_index])
    assert any(event[0] == "fsync" for event in events[replace_index + 1 :])
    assert json.loads(report_path.read_text(encoding="utf-8")) == {"new": True}
    assert list(report_path.parent.glob(f".{report_path.name}.*")) == []


def test_report_path_inside_audited_repo_is_rejected(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()

    with pytest.raises(SystemExit) as exc:
        worktree_gc.main(
            ["--repo", str(repo), "--report-path", str(repo / "audit.json")],
            git_backend=object(),
            audit_fn=lambda **_kwargs: pytest.fail("audit must not run"),
            stdout=io.StringIO(),
        )

    assert exc.value.code == 2
    assert not (repo / "audit.json").exists()


def test_report_path_git_head_is_never_overwritten(tmp_path):
    repo = tmp_path / "repo"
    git_dir = repo / ".git"
    git_dir.mkdir(parents=True)
    head = git_dir / "HEAD"
    original = "ref: refs/heads/master\n"
    head.write_text(original, encoding="utf-8")

    with pytest.raises(SystemExit) as exc:
        worktree_gc.main(
            ["--repo", str(repo), "--report-path", str(head)],
            git_backend=object(),
            audit_fn=lambda **_kwargs: pytest.fail("audit must not run"),
            stdout=io.StringIO(),
        )

    assert exc.value.code == 2
    assert head.read_text(encoding="utf-8") == original


def test_report_path_inside_state_dir_is_rejected(tmp_path):
    repo = tmp_path / "repo"
    sessions = tmp_path / "state" / "sessions"
    repo.mkdir()
    sessions.mkdir(parents=True)
    sidecar = sessions / "existing.json"
    original = "{}\n"
    sidecar.write_text(original, encoding="utf-8")

    with pytest.raises(SystemExit) as exc:
        worktree_gc.main(
            [
                "--repo",
                str(repo),
                "--state-dir",
                str(sessions.parent),
                "--report-path",
                str(sidecar),
            ],
            git_backend=object(),
            audit_fn=lambda **_kwargs: pytest.fail("audit must not run"),
            stdout=io.StringIO(),
        )

    assert exc.value.code == 2
    assert sidecar.read_text(encoding="utf-8") == original


def test_report_destination_symlink_is_rejected_without_being_followed(tmp_path):
    target = tmp_path / "target.json"
    original = '{"original": true}\n'
    target.write_text(original, encoding="utf-8")
    link = tmp_path / "link.json"
    os.symlink(target, link)

    with pytest.raises(ValueError):
        write_report_atomic({"new": True}, link)

    assert target.read_text(encoding="utf-8") == original
    assert link.is_symlink()


def test_report_destination_fifo_is_rejected_without_blocking(tmp_path):
    fifo = tmp_path / "fifo.json"
    os.mkfifo(fifo)

    with pytest.raises(ValueError):
        write_report_atomic({"new": True}, fifo)


def test_report_intermediate_parent_symlink_swap_cannot_redirect_write(
    tmp_path,
    monkeypatch,
):
    """Every parent component is pinned/no-follow at the point of publication."""
    safe = tmp_path / "safe"
    movable_parent = safe / "reports"
    report_parent = movable_parent / "daily"
    report_parent.mkdir(parents=True)
    destination = report_parent / "report.json"

    forbidden = tmp_path / "forbidden-repo"
    redirected_parent = forbidden / "daily"
    redirected_parent.mkdir(parents=True)
    redirected = redirected_parent / destination.name
    original = '{"repository": "must-stay-intact"}\n'
    redirected.write_text(original, encoding="utf-8")

    real_dumps = json.dumps
    swapped = {"done": False}

    def swap_intermediate_parent(*args, **kwargs):
        if not swapped["done"]:
            moved_aside = safe / "reports-before-swap"
            movable_parent.rename(moved_aside)
            os.symlink(forbidden, movable_parent)
            swapped["done"] = True
        return real_dumps(*args, **kwargs)

    monkeypatch.setattr(json, "dumps", swap_intermediate_parent)

    with pytest.raises((OSError, ValueError)):
        write_report_atomic(
            {"new": True},
            destination,
            forbidden_roots=(forbidden,),
        )

    assert swapped["done"] is True
    assert redirected.read_text(encoding="utf-8") == original
