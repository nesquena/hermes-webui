"""Tests for the deterministic Upstream Watch collector."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from urllib.error import HTTPError

import pytest

from scripts import upstream_watch


class FakeResponse:
    def __init__(self, payload, *, status=200, headers=None):
        self.status = status
        self._payload = payload
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return json.dumps(self._payload).encode("utf-8")


def test_fetch_github_commits_follows_pagination(monkeypatch):
    calls = []
    responses = [
        FakeResponse(
            [{"sha": "aaa111", "commit": {"message": "feat: one"}}],
            headers={"Link": '<https://api.github.com/page2>; rel="next"'},
        ),
        FakeResponse([{"sha": "bbb222", "commit": {"message": "fix: two"}}]),
    ]

    def fake_urlopen(req, timeout):
        calls.append((req, timeout))
        return responses.pop(0)

    monkeypatch.setattr(upstream_watch.urllib.request, "urlopen", fake_urlopen)

    commits, meta = upstream_watch.fetch_github_commits(
        "NousResearch/hermes-agent",
        "2026-05-10T08:30:00Z",
        token="dummy",
    )

    assert [c["sha"] for c in commits] == ["aaa111", "bbb222"]
    assert meta["status"] == "ok"
    assert meta["pages"] == 2
    assert calls[0][0].headers["Authorization"] == "token dummy"
    assert calls[0][1] == 15


def test_fetch_github_commits_returns_structured_http_error(monkeypatch):
    def fake_urlopen(req, timeout):
        raise HTTPError(req.full_url, 403, "rate limited", {}, None)

    monkeypatch.setattr(upstream_watch.urllib.request, "urlopen", fake_urlopen)

    commits, meta = upstream_watch.fetch_github_commits(
        "NousResearch/hermes-agent", "2026-05-10T08:30:00Z"
    )

    assert commits == []
    assert meta["status"] == "error"
    assert meta["error_class"] == "http_error"
    assert meta["http_status"] == 403
    assert "token" not in json.dumps(meta).lower()


def _git(path: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


@pytest.fixture
def git_pair(tmp_path):
    remote = tmp_path / "remote.git"
    work = tmp_path / "work"
    clone = tmp_path / "clone"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    subprocess.run(["git", "clone", str(remote), str(work)], check=True, capture_output=True)
    _git(work, "config", "user.name", "Test User")
    _git(work, "config", "user.email", "test@example.invalid")
    (work / "README.md").write_text("hello\n", encoding="utf-8")
    _git(work, "add", "README.md")
    _git(work, "commit", "-m", "initial")
    _git(work, "push", "-u", "origin", "HEAD")
    subprocess.run(["git", "clone", str(remote), str(clone)], check=True, capture_output=True)
    _git(clone, "config", "user.name", "Test User")
    _git(clone, "config", "user.email", "test@example.invalid")
    return remote, work, clone


def test_collect_local_git_state_reports_clean_and_dirty(git_pair):
    _, _, clone = git_pair

    clean = upstream_watch.collect_local_git_state(clone)
    assert clean["is_git"] is True
    assert clean["dirty"] is False
    assert clean["behind"] == 0
    assert clean["ahead"] == 0
    assert clean["diverged"] is False

    (clone / "README.md").write_text("changed\n", encoding="utf-8")
    dirty = upstream_watch.collect_local_git_state(clone)
    assert dirty["dirty"] is True


def test_collect_local_git_state_counts_untracked_files_as_dirty(git_pair):
    _, _, clone = git_pair

    (clone / "scratch.txt").write_text("not staged yet\n", encoding="utf-8")

    dirty = upstream_watch.collect_local_git_state(clone)

    assert dirty["dirty"] is True


def test_tests_do_not_use_secret_like_github_token_literals():
    content = Path(__file__).read_text(encoding="utf-8")
    secret_prefix = "gh" + "p_"
    assert secret_prefix not in content


def test_collect_local_git_state_reports_ahead_behind_after_fetch(git_pair):
    _, work, clone = git_pair
    (work / "upstream.txt").write_text("remote\n", encoding="utf-8")
    _git(work, "add", "upstream.txt")
    _git(work, "commit", "-m", "remote change")
    _git(work, "push")

    fetch = upstream_watch.maybe_fetch(clone)
    state = upstream_watch.collect_local_git_state(clone)

    assert fetch["ok"] is True
    assert state["behind"] == 1
    assert state["ahead"] == 0
    assert state["diverged"] is False


def test_collect_local_git_state_handles_non_git_path(tmp_path):
    state = upstream_watch.collect_local_git_state(tmp_path)
    assert state["is_git"] is False
    assert state["error"]


def test_build_report_writes_latest_and_historical_files(tmp_path, monkeypatch, git_pair):
    _, _, clone = git_pair

    def fake_fetch(owner_repo, since_iso, token=None, *, max_pages=10, timeout=15):
        return (
            [
                {
                    "sha": "abcdef123456",
                    "html_url": "https://github.com/example/repo/commit/abcdef1",
                    "commit": {
                        "message": "fix: improve gateway restart safety",
                        "author": {"name": "A", "date": "2026-05-11T06:00:00Z"},
                    },
                    "author": {"login": "alice"},
                }
            ],
            {"status": "ok", "pages": 1, "rate_limit_remaining": "59"},
        )

    monkeypatch.setattr(upstream_watch, "fetch_github_commits", fake_fetch)
    report = upstream_watch.build_report(
        repo_specs=[("example/repo", clone)],
        window_hours=24,
        out_dir=tmp_path / "upstream-watch",
        fetch_remote=False,
    )

    latest_json = tmp_path / "upstream-watch-latest.json"
    latest_md = tmp_path / "upstream-watch-latest.md"
    historical_dir = tmp_path / "upstream-watch"
    assert latest_json.exists()
    assert latest_md.exists()
    assert report["status"] == "ok"
    assert report["repos"][0]["upstream"]["commit_count"] == 1
    assert report["watchlist_hits"][0]["topics"] == ["gateway", "restart"]
    assert "gateway" in latest_md.read_text(encoding="utf-8")
    assert list(historical_dir.glob("20*.json")), "historical JSON copy should be written"


def test_build_report_does_not_fail_whole_run_for_local_no_upstream(tmp_path, monkeypatch):
    def fake_fetch(owner_repo, since_iso, token=None, *, max_pages=10, timeout=15):
        return ([], {"status": "ok", "pages": 1, "rate_limit_remaining": "59"})

    def fake_local(path):
        return {
            "path": str(path),
            "is_git": True,
            "branch": "feature/local",
            "upstream": None,
            "ahead": 0,
            "behind": 0,
            "dirty": True,
            "diverged": False,
            "error": "no upstream tracking branch",
        }

    monkeypatch.setattr(upstream_watch, "fetch_github_commits", fake_fetch)
    monkeypatch.setattr(upstream_watch, "collect_local_git_state", fake_local)
    report = upstream_watch.build_report(
        repo_specs=[("example/repo", tmp_path)],
        window_hours=24,
        out_dir=tmp_path / "upstream-watch",
        fetch_remote=False,
    )

    assert report["status"] == "ok"
    assert report["repos"][0]["local"]["error"] == "no upstream tracking branch"
