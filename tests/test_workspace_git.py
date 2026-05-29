import json
import pathlib
import subprocess
import types
import uuid
import urllib.error
import urllib.parse
import urllib.request
from io import BytesIO

import pytest

from tests._pytest_port import BASE


ROOT = pathlib.Path(__file__).parent.parent


def _git(cwd, *args):
    result = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        shell=False,
        text=True,
        capture_output=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    return result.stdout


def _init_repo(path):
    path.mkdir(parents=True, exist_ok=True)
    init = subprocess.run(
        ["git", "init", "-b", "master"],
        cwd=str(path),
        shell=False,
        text=True,
        capture_output=True,
        timeout=20,
    )
    if init.returncode != 0:
        _git(path, "init")
        _git(path, "checkout", "-B", "master")
    _git(path, "config", "user.email", "hermes-tests@example.invalid")
    _git(path, "config", "user.name", "Hermes Tests")
    return path


def _init_bare_repo(path):
    init = subprocess.run(
        ["git", "init", "--bare", "-b", "master", str(path)],
        shell=False,
        text=True,
        capture_output=True,
        timeout=20,
    )
    if init.returncode != 0:
        _git(path.parent, "init", "--bare", str(path))
        _git(path, "symbolic-ref", "HEAD", "refs/heads/master")
    return path


def _commit_all(path, message="initial"):
    _git(path, "add", ".")
    _git(path, "commit", "-m", message)


def _get(path):
    try:
        with urllib.request.urlopen(BASE + path, timeout=10) as r:
            return json.loads(r.read()), r.status
    except urllib.error.HTTPError as e:
        return json.loads(e.read()), e.code


def _post(path, body=None):
    data = json.dumps(body or {}).encode()
    req = urllib.request.Request(
        BASE + path,
        data=data,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read()), r.status
    except urllib.error.HTTPError as e:
        return json.loads(e.read()), e.code


def _make_session(created_list, ws=None):
    body = {}
    if ws:
        body["workspace"] = str(ws)
    data, status = _post("/api/session/new", body)
    assert status == 200
    sid = data["session"]["session_id"]
    created_list.append(sid)
    return sid, pathlib.Path(data["session"]["workspace"])


class _CaptureHandler:
    def __init__(self):
        self.status = None
        self.headers = {}
        self.response_headers = []
        self.wfile = BytesIO()

    def send_response(self, status):
        self.status = status

    def send_header(self, key, value):
        self.response_headers.append((key, value))

    def end_headers(self):
        pass

    def payload(self):
        return json.loads(self.wfile.getvalue().decode("utf-8"))


def test_git_status_non_git_workspace(tmp_path):
    from api.workspace_git import git_status

    ws = tmp_path / "plain"
    ws.mkdir()
    assert git_status(ws) == {"is_git": False}


def test_git_status_handles_staged_unstaged_untracked_deleted_and_renamed(tmp_path):
    from api.workspace_git import git_status

    repo = _init_repo(tmp_path / "repo")
    (repo / "tracked.txt").write_text("one\n", encoding="utf-8")
    (repo / "delete-me.txt").write_text("bye\n", encoding="utf-8")
    (repo / "old name.txt").write_text("move\n", encoding="utf-8")
    _commit_all(repo)

    (repo / "tracked.txt").write_text("one\ntwo\n", encoding="utf-8")
    (repo / "staged.txt").write_text("staged\n", encoding="utf-8")
    _git(repo, "add", "staged.txt")
    (repo / "delete-me.txt").unlink()
    _git(repo, "mv", "old name.txt", "new name.txt")
    (repo / "untracked space.txt").write_text("new\nfile\n", encoding="utf-8")

    status = git_status(repo)
    by_path = {item["path"]: item for item in status["files"]}

    assert status["is_git"] is True
    assert by_path["tracked.txt"]["unstaged"] is True
    assert by_path["staged.txt"]["staged"] is True
    assert by_path["delete-me.txt"]["status"] == "D"
    assert by_path["new name.txt"]["old_path"] == "old name.txt"
    assert by_path["untracked space.txt"]["untracked"] is True
    assert by_path["untracked space.txt"]["additions"] == 2
    assert status["totals"]["changed"] >= 5


def test_git_status_reports_ignored_files_without_counting_them_as_changes(tmp_path):
    from api.workspace_git import git_status

    repo = _init_repo(tmp_path / "repo")
    (repo / ".gitignore").write_text("*.log\nbuild/\n", encoding="utf-8")
    (repo / "tracked.txt").write_text("one\n", encoding="utf-8")
    _commit_all(repo)

    (repo / "tracked.txt").write_text("one\ntwo\n", encoding="utf-8")
    (repo / "debug.log").write_text("ignored log\n", encoding="utf-8")
    build = repo / "build"
    build.mkdir()
    (build / "artifact.txt").write_text("ignored artifact\n", encoding="utf-8")

    status = git_status(repo)
    by_path = {item["path"]: item for item in status["files"]}

    assert by_path["tracked.txt"]["unstaged"] is True
    assert by_path["debug.log"]["ignored"] is True
    assert by_path["debug.log"]["status"] == "Ignored"
    assert by_path["build/"]["ignored"] is True
    assert by_path["build/"]["staged"] is False
    assert by_path["build/"]["untracked"] is False
    assert status["totals"]["changed"] == 1
    assert status["totals"]["untracked"] == 0


def test_git_status_ignores_crlf_only_worktree_noise(tmp_path):
    from api.workspace_git import git_status

    repo = _init_repo(tmp_path / "repo")
    (repo / "tracked.txt").write_text("one\ntwo\n", encoding="utf-8", newline="\n")
    _commit_all(repo)

    (repo / "tracked.txt").write_text("one\r\ntwo\r\n", encoding="utf-8", newline="")

    raw = _git(repo, "status", "--porcelain", "--", "tracked.txt")
    assert raw.startswith(" M")

    status = git_status(repo)
    assert status["totals"]["changed"] == 0
    assert status["files"] == []
    assert status["noise_filtering"]["active"] is True
    assert status["noise_filtering"]["crlf_only"] == 1


def test_git_status_keeps_real_edit_with_crlf_endings(tmp_path):
    from api.workspace_git import git_status

    repo = _init_repo(tmp_path / "repo")
    (repo / "tracked.txt").write_text("one\ntwo\n", encoding="utf-8", newline="\n")
    _commit_all(repo)

    (repo / "tracked.txt").write_text("one\r\ntwo\r\nthree\r\n", encoding="utf-8", newline="")

    status = git_status(repo)
    by_path = {item["path"]: item for item in status["files"]}
    assert status["totals"]["changed"] == 1
    assert by_path["tracked.txt"]["unstaged"] is True
    assert by_path["tracked.txt"]["additions"] == 1
    assert by_path["tracked.txt"]["deletions"] == 0


def test_git_status_ignores_filemode_only_noise(tmp_path):
    from api.workspace_git import git_status

    repo = _init_repo(tmp_path / "repo")
    script = repo / "script.sh"
    script.write_text("#!/bin/sh\necho hi\n", encoding="utf-8")
    _commit_all(repo)

    _git(repo, "update-index", "--chmod=+x", "script.sh")

    raw = _git(repo, "status", "--porcelain", "--", "script.sh")
    assert "script.sh" in raw

    status = git_status(repo)
    assert status["totals"]["changed"] == 0
    assert status["files"] == []
    assert status["noise_filtering"]["active"] is True


def test_git_status_scopes_nested_workspace_to_that_directory(tmp_path):
    from api.workspace_git import git_status

    repo = _init_repo(tmp_path / "repo")
    nested = repo / "app"
    nested.mkdir()
    (nested / "inside.txt").write_text("inside\n", encoding="utf-8")
    (repo / "outside.txt").write_text("outside\n", encoding="utf-8")
    _commit_all(repo)

    (nested / "inside.txt").write_text("inside\nchanged\n", encoding="utf-8")
    (repo / "outside.txt").write_text("outside\nchanged\n", encoding="utf-8")

    status = git_status(nested)
    paths = {item["path"] for item in status["files"]}
    assert paths == {"inside.txt"}


def test_git_diff_generates_untracked_text_diff_and_blocks_escape(tmp_path):
    from api.workspace_git import GitWorkspaceError, git_diff

    repo = _init_repo(tmp_path / "repo")
    (repo / "tracked.txt").write_text("one\n", encoding="utf-8")
    _commit_all(repo)
    (repo / "new file.txt").write_text("hello\nworld\n", encoding="utf-8")

    diff = git_diff(repo, "new file.txt", "unstaged")
    assert diff["binary"] is False
    assert "+++ b/new file.txt" in diff["diff"]
    assert "+hello" in diff["diff"]

    with pytest.raises(GitWorkspaceError):
        git_diff(repo, "../outside.txt", "unstaged")


def test_git_status_reports_untracked_files_inside_directories(tmp_path):
    from api.workspace_git import git_discard, git_status

    repo = _init_repo(tmp_path / "repo")
    (repo / "tracked.txt").write_text("one\n", encoding="utf-8")
    _commit_all(repo)
    nested = repo / "newdir"
    nested.mkdir()
    (nested / "a.txt").write_text("hello\n", encoding="utf-8")

    status = git_status(repo)
    paths = {item["path"] for item in status["files"]}
    assert "newdir/a.txt" in paths
    assert "newdir/" not in paths

    git_discard(repo, ["newdir/a.txt"], delete_untracked=True)
    assert not (nested / "a.txt").exists()


def test_git_status_reports_ignored_files_without_counting_them_as_changed(tmp_path):
    from api.workspace_git import git_status

    repo = _init_repo(tmp_path / "repo")
    (repo / ".gitignore").write_text("*.log\nbuild/\n", encoding="utf-8")
    (repo / "tracked.txt").write_text("one\n", encoding="utf-8")
    _commit_all(repo)

    (repo / "tracked.txt").write_text("one\ntwo\n", encoding="utf-8")
    (repo / "debug.log").write_text("ignored log\n", encoding="utf-8")
    build = repo / "build"
    build.mkdir()
    (build / "artifact.txt").write_text("ignored artifact\n", encoding="utf-8")

    status = git_status(repo)
    by_path = {item["path"]: item for item in status["files"]}

    assert by_path["tracked.txt"]["unstaged"] is True
    assert by_path["debug.log"]["ignored"] is True
    assert by_path["debug.log"]["status"] == "Ignored"
    assert by_path["debug.log"]["staged"] is False
    assert by_path["debug.log"]["unstaged"] is False
    assert by_path["debug.log"]["untracked"] is False
    assert any(item["ignored"] and item["path"].startswith("build") for item in status["files"])
    assert status["totals"]["changed"] == 1
    assert status["totals"]["untracked"] == 0


def test_git_diff_large_untracked_file_is_bounded(tmp_path):
    from api.workspace_git import DIFF_SIZE_LIMIT, git_diff, git_status

    repo = _init_repo(tmp_path / "repo")
    (repo / "tracked.txt").write_text("one\n", encoding="utf-8")
    _commit_all(repo)
    large = repo / "large.txt"
    large.write_text("x" * (DIFF_SIZE_LIMIT + 1), encoding="utf-8")

    status = git_status(repo)
    by_path = {item["path"]: item for item in status["files"]}
    assert by_path["large.txt"]["untracked"] is True
    assert by_path["large.txt"]["additions"] == 0

    diff = git_diff(repo, "large.txt", "unstaged")
    assert diff["too_large"] is True
    assert diff["diff"] == ""


def test_git_stage_unstage_discard_and_commit(tmp_path):
    from api.workspace_git import git_commit, git_discard, git_stage, git_status, git_unstage

    repo = _init_repo(tmp_path / "repo")
    (repo / "tracked.txt").write_text("one\n", encoding="utf-8")
    _commit_all(repo)

    (repo / "tracked.txt").write_text("one\ntwo\n", encoding="utf-8")
    staged = git_stage(repo, ["tracked.txt"])
    assert staged["totals"]["staged"] == 1

    unstaged = git_unstage(repo, ["tracked.txt"])
    assert unstaged["totals"]["staged"] == 0
    assert unstaged["totals"]["unstaged"] == 1

    git_discard(repo, ["tracked.txt"])
    assert git_status(repo)["totals"]["changed"] == 0

    (repo / "tracked.txt").write_text("one\nthree\n", encoding="utf-8")
    git_stage(repo, ["tracked.txt"])
    committed = git_commit(repo, "Update tracked file")
    assert committed["ok"] is True
    assert committed["commit"]
    assert committed["status"]["totals"]["changed"] == 0


def test_git_commit_selected_ignores_unrelated_real_index(tmp_path):
    from api.workspace_git import git_commit_selected, git_status

    repo = _init_repo(tmp_path / "repo")
    (repo / "selected.txt").write_text("one\n", encoding="utf-8")
    (repo / "staged.txt").write_text("alpha\n", encoding="utf-8")
    _commit_all(repo)

    (repo / "selected.txt").write_text("one\ntwo\n", encoding="utf-8")
    (repo / "staged.txt").write_text("alpha\nbeta\n", encoding="utf-8")
    _git(repo, "add", "staged.txt")

    committed = git_commit_selected(repo, "Commit selected only", ["selected.txt"])
    assert committed["ok"] is True
    assert committed["paths"] == ["selected.txt"]
    assert _git(repo, "show", "--name-only", "--format=", "HEAD").splitlines() == ["selected.txt"]

    by_path = {item["path"]: item for item in git_status(repo)["files"]}
    assert "selected.txt" not in by_path
    assert by_path["staged.txt"]["staged"] is True


def test_git_commit_selected_supports_initial_commit(tmp_path):
    from api.workspace_git import git_commit_selected, git_status

    repo = _init_repo(tmp_path / "repo")
    (repo / "first.txt").write_text("first\n", encoding="utf-8")

    committed = git_commit_selected(repo, "Initial selected commit", ["first.txt"])
    assert committed["ok"] is True
    assert _git(repo, "show", "--name-only", "--format=", "HEAD").splitlines() == ["first.txt"]
    assert git_status(repo)["totals"]["changed"] == 0


def test_git_commit_selected_preserves_rename_semantics(tmp_path):
    from api.workspace_git import git_commit_selected, git_status

    repo = _init_repo(tmp_path / "repo")
    (repo / "old.txt").write_text("old\n", encoding="utf-8")
    _commit_all(repo)

    _git(repo, "mv", "old.txt", "new.txt")

    committed = git_commit_selected(repo, "Rename selected file", ["new.txt"])
    assert committed["ok"] is True
    assert _git(repo, "ls-tree", "--name-only", "HEAD").splitlines() == ["new.txt"]
    assert "old.txt" not in _git(repo, "status", "--porcelain=v2")
    assert git_status(repo)["totals"]["changed"] == 0


def test_git_commit_selected_handles_untracked_and_mixed_paths(tmp_path):
    from api.workspace_git import git_commit_selected

    repo = _init_repo(tmp_path / "repo")
    (repo / "tracked.txt").write_text("one\n", encoding="utf-8")
    _commit_all(repo)

    (repo / "tracked.txt").write_text("one\ntwo\n", encoding="utf-8")
    (repo / "new.txt").write_text("new\n", encoding="utf-8")

    committed = git_commit_selected(repo, "Commit mixed selected files", ["tracked.txt", "new.txt"])
    assert committed["ok"] is True
    assert set(_git(repo, "show", "--name-only", "--format=", "HEAD").splitlines()) == {
        "tracked.txt",
        "new.txt",
    }


def test_git_commit_selected_respects_nested_workspace_scope(tmp_path):
    from api.workspace_git import GitWorkspaceError, git_commit_selected

    repo = _init_repo(tmp_path / "repo")
    nested = repo / "app"
    nested.mkdir()
    (nested / "inside.txt").write_text("inside\n", encoding="utf-8")
    (repo / "outside.txt").write_text("outside\n", encoding="utf-8")
    _commit_all(repo)

    (nested / "inside.txt").write_text("inside\nchanged\n", encoding="utf-8")
    (repo / "outside.txt").write_text("outside\nchanged\n", encoding="utf-8")

    committed = git_commit_selected(nested, "Nested selected commit", ["inside.txt"])
    assert committed["paths"] == ["inside.txt"]
    assert _git(repo, "show", "--name-only", "--format=", "HEAD").splitlines() == ["app/inside.txt"]

    with pytest.raises(GitWorkspaceError) as outside:
        git_commit_selected(nested, "Outside", ["../outside.txt"])
    assert outside.value.code == "path_outside_workspace"


def test_git_commit_selected_rejects_conflicts_and_path_traversal(tmp_path):
    from api.workspace_git import GitWorkspaceError, git_commit_selected

    repo = _init_repo(tmp_path / "repo")
    (repo / "conflict.txt").write_text("base\n", encoding="utf-8")
    _commit_all(repo)
    _git(repo, "checkout", "-b", "side")
    (repo / "conflict.txt").write_text("side\n", encoding="utf-8")
    _commit_all(repo, "side")
    _git(repo, "checkout", "master")
    (repo / "conflict.txt").write_text("main\n", encoding="utf-8")
    _commit_all(repo, "main")
    subprocess.run(["git", "merge", "side"], cwd=repo, shell=False, text=True, capture_output=True, timeout=20)

    with pytest.raises(GitWorkspaceError) as conflict:
        git_commit_selected(repo, "Nope", ["conflict.txt"])
    assert conflict.value.code == "conflict"

    with pytest.raises(GitWorkspaceError) as traversal:
        git_commit_selected(repo, "Nope", ["../outside.txt"])
    assert traversal.value.code == "path_outside_workspace"


def test_selected_commit_message_prompt_uses_selected_diff(tmp_path):
    from api.workspace_git import selected_commit_message_prompt

    repo = _init_repo(tmp_path / "repo")
    (repo / "selected.txt").write_text("one\n", encoding="utf-8")
    (repo / "other.txt").write_text("alpha\n", encoding="utf-8")
    _commit_all(repo)
    (repo / "selected.txt").write_text("one\ntwo\n", encoding="utf-8")
    (repo / "other.txt").write_text("alpha\nbeta\n", encoding="utf-8")

    prompt = selected_commit_message_prompt(repo, ["selected.txt"])
    assert "selected.txt" in prompt["user_prompt"]
    assert "+two" in prompt["user_prompt"]
    assert "other.txt" not in prompt["user_prompt"]
    assert "beta" not in prompt["user_prompt"]


def test_staged_commit_message_prompt_uses_only_staged_diff(tmp_path):
    from api.workspace_git import (
        GitWorkspaceError,
        clean_generated_commit_message,
        staged_commit_message_prompt,
    )

    repo = _init_repo(tmp_path / "repo")
    (repo / "tracked.txt").write_text("one\n", encoding="utf-8")
    _commit_all(repo)

    (repo / "tracked.txt").write_text("one\nstaged\n", encoding="utf-8")
    _git(repo, "add", "tracked.txt")
    (repo / "tracked.txt").write_text("one\nstaged\nunstaged\n", encoding="utf-8")

    prompt = staged_commit_message_prompt(repo)
    assert prompt["truncated"] is False
    assert "tracked.txt" in prompt["user_prompt"]
    assert "+staged" in prompt["user_prompt"]
    assert "unstaged" not in prompt["user_prompt"]
    assert "Never mention AI, Cursor, Zed, agents" in prompt["system_prompt"]

    _git(repo, "restore", "--staged", "tracked.txt")
    with pytest.raises(GitWorkspaceError):
        staged_commit_message_prompt(repo)

    assert clean_generated_commit_message("```text\nSubject\n\n- Body\n```") == "Subject\n\n- Body"


def test_git_fetch_pull_and_push_with_upstream(tmp_path):
    from api.workspace_git import git_fetch, git_pull, git_push, git_status

    remote = _init_bare_repo(tmp_path / "remote.git")

    origin = _init_repo(tmp_path / "origin")
    (origin / "tracked.txt").write_text("one\n", encoding="utf-8")
    _commit_all(origin)
    _git(origin, "remote", "add", "origin", str(remote))
    _git(origin, "push", "-u", "origin", "HEAD")
    _git(remote, "symbolic-ref", "HEAD", "refs/heads/master")

    clone = tmp_path / "clone"
    _git(tmp_path, "clone", str(remote), str(clone))
    _git(clone, "config", "user.email", "hermes-tests@example.invalid")
    _git(clone, "config", "user.name", "Hermes Tests")

    (origin / "tracked.txt").write_text("one\ntwo\n", encoding="utf-8")
    _commit_all(origin, "Remote update")
    _git(origin, "push")

    fetched = git_fetch(clone)
    assert fetched["status"]["behind"] == 1

    pulled = git_pull(clone)
    assert pulled["status"]["behind"] == 0
    assert (clone / "tracked.txt").read_text(encoding="utf-8") == "one\ntwo\n"

    (clone / "tracked.txt").write_text("one\ntwo\nthree\n", encoding="utf-8")
    _git(clone, "add", "tracked.txt")
    _git(clone, "commit", "-m", "Local update")
    assert git_status(clone)["ahead"] == 1

    pushed = git_push(clone)
    assert pushed["status"]["ahead"] == 0


def test_git_branches_lists_local_remote_and_upstream(tmp_path):
    from api.workspace_git import git_branches

    remote = _init_bare_repo(tmp_path / "remote.git")
    origin = _init_repo(tmp_path / "origin")
    (origin / "tracked.txt").write_text("one\n", encoding="utf-8")
    _commit_all(origin)
    _git(origin, "branch", "-M", "main")
    _git(origin, "remote", "add", "origin", str(remote))
    _git(origin, "push", "-u", "origin", "main")
    _git(remote, "symbolic-ref", "HEAD", "refs/heads/main")

    clone = tmp_path / "clone"
    _git(tmp_path, "clone", str(remote), str(clone))
    branches = git_branches(clone)
    assert branches["current"] == "main"
    assert branches["detached"] is False
    assert any(item["name"] == "main" and item["upstream"] == "origin/main" for item in branches["local"])
    main = next(item for item in branches["local"] if item["name"] == "main")
    assert "updated_relative" in main and "author" in main and "subject" in main
    assert any(item["name"] == "origin/main" for item in branches["remote"])
    assert not any(item["name"] == "origin" for item in branches["remote"])


def test_git_checkout_local_new_remote_dirty_and_invalid_refs(tmp_path):
    from api.workspace_git import GitWorkspaceError, git_branches, git_checkout

    remote = _init_bare_repo(tmp_path / "remote.git")
    origin = _init_repo(tmp_path / "origin")
    (origin / "tracked.txt").write_text("one\n", encoding="utf-8")
    _commit_all(origin)
    _git(origin, "branch", "-M", "main")
    _git(origin, "remote", "add", "origin", str(remote))
    _git(origin, "push", "-u", "origin", "main")
    _git(remote, "symbolic-ref", "HEAD", "refs/heads/main")
    _git(origin, "checkout", "-b", "remote-feature")
    (origin / "remote.txt").write_text("remote\n", encoding="utf-8")
    _commit_all(origin, "remote feature")
    _git(origin, "push", "-u", "origin", "remote-feature")

    clone = tmp_path / "clone"
    _git(tmp_path, "clone", str(remote), str(clone))
    _git(clone, "config", "user.email", "hermes-tests@example.invalid")
    _git(clone, "config", "user.name", "Hermes Tests")

    created = git_checkout(clone, "main", "new", new_branch="local-work")
    assert created["current_branch"] == "local-work"
    assert git_branches(clone)["current"] == "local-work"

    switched = git_checkout(clone, "main", "local")
    assert switched["current_branch"] == "main"

    tracked = git_checkout(clone, "origin/remote-feature", "remote", new_branch="remote-feature", track=True)
    assert tracked["current_branch"] == "remote-feature"
    assert git_branches(clone)["upstream"] == "origin/remote-feature"

    (clone / "tracked.txt").write_text("dirty\n", encoding="utf-8")
    with pytest.raises(GitWorkspaceError) as dirty:
        git_checkout(clone, "main", "local")
    assert dirty.value.code == "dirty_worktree"
    _git(clone, "restore", "tracked.txt")

    with pytest.raises(GitWorkspaceError) as invalid:
        git_checkout(clone, "does-not-exist", "local")
    assert invalid.value.code in {"invalid_ref", "git_failed"}


def test_git_checkout_detached_requires_explicit_mode(tmp_path):
    from api.workspace_git import git_branches, git_checkout

    repo = _init_repo(tmp_path / "repo")
    (repo / "tracked.txt").write_text("one\n", encoding="utf-8")
    _commit_all(repo)
    sha = _git(repo, "rev-parse", "--short", "HEAD").strip()

    result = git_checkout(repo, sha, "detached")
    assert result["ok"] is True
    branches = git_branches(repo)
    assert branches["detached"] is True
    assert branches["current"] == sha


def test_git_stash_and_checkout_is_explicit(tmp_path):
    from api.workspace_git import git_stash_and_checkout, git_status

    repo = _init_repo(tmp_path / "repo")
    (repo / "tracked.txt").write_text("one\n", encoding="utf-8")
    _commit_all(repo)
    _git(repo, "checkout", "-b", "target")
    _git(repo, "checkout", "master")
    (repo / "tracked.txt").write_text("dirty\n", encoding="utf-8")

    result = git_stash_and_checkout(repo, "target", "local")
    assert result["ok"] is True
    assert result["stashed"] is True
    assert result["stash_name"].startswith("hermes-webui branch switch")
    assert result["current_branch"] == "target"
    assert git_status(repo)["totals"]["changed"] == 0
    assert "hermes-webui branch switch to target" in _git(repo, "stash", "list")


def test_git_stash_and_checkout_restores_branch_changes_when_returning(tmp_path):
    from api.workspace_git import git_stash_and_checkout, git_status

    repo = _init_repo(tmp_path / "repo")
    _git(repo, "branch", "-M", "main")
    (repo / "tracked.txt").write_text("one\n", encoding="utf-8")
    _commit_all(repo)
    _git(repo, "checkout", "-b", "feature")
    _git(repo, "checkout", "main")

    (repo / "tracked.txt").write_text("main dirty\n", encoding="utf-8")
    (repo / "main-only.txt").write_text("untracked on main\n", encoding="utf-8")

    to_feature = git_stash_and_checkout(repo, "feature", "local")
    assert to_feature["ok"] is True
    assert to_feature["stashed"] is True
    assert to_feature["current_branch"] == "feature"
    assert git_status(repo)["totals"]["changed"] == 0
    assert not (repo / "main-only.txt").exists()

    (repo / "feature-only.txt").write_text("untracked on feature\n", encoding="utf-8")
    to_main = git_stash_and_checkout(repo, "main", "local")

    assert to_main["ok"] is True
    assert to_main["stashed"] is True
    assert to_main["current_branch"] == "main"
    assert to_main["restored_stash"]["branch"] == "main"
    assert (repo / "tracked.txt").read_text(encoding="utf-8") == "main dirty\n"
    assert (repo / "main-only.txt").read_text(encoding="utf-8") == "untracked on main\n"
    assert not (repo / "feature-only.txt").exists()
    stash_list = _git(repo, "stash", "list")
    assert "On main: hermes-webui branch switch" not in stash_list
    assert "On feature: hermes-webui branch switch" in stash_list


def test_git_stash_and_checkout_reports_restore_conflicts_without_dropping_stash(tmp_path):
    from api.workspace_git import git_stash_and_checkout

    repo = _init_repo(tmp_path / "repo")
    _git(repo, "branch", "-M", "main")
    (repo / "tracked.txt").write_text("one\n", encoding="utf-8")
    _commit_all(repo)
    _git(repo, "checkout", "-b", "feature")
    _git(repo, "checkout", "main")
    (repo / "tracked.txt").write_text("main dirty\n", encoding="utf-8")

    git_stash_and_checkout(repo, "feature", "local")
    _git(repo, "checkout", "main")
    (repo / "tracked.txt").write_text("main changed while parked\n", encoding="utf-8")
    _commit_all(repo, "advance main")
    _git(repo, "checkout", "feature")

    result = git_stash_and_checkout(repo, "main", "local")

    assert result["ok"] is True
    assert result["current_branch"] == "main"
    assert result["restore_failed"] is True
    assert result["restore_stash"]["branch"] == "main"
    assert "On main: hermes-webui branch switch" in _git(repo, "stash", "list")


def test_git_stash_checkout_validates_before_stashing(tmp_path):
    from api.workspace_git import GitWorkspaceError, git_stash_and_checkout

    repo = _init_repo(tmp_path / "repo")
    (repo / "tracked.txt").write_text("one\n", encoding="utf-8")
    _commit_all(repo)
    (repo / "tracked.txt").write_text("dirty\n", encoding="utf-8")

    with pytest.raises(GitWorkspaceError) as invalid:
        git_stash_and_checkout(repo, "missing-branch", "local")

    assert invalid.value.code == "invalid_ref"
    assert "M tracked.txt" in _git(repo, "status", "--porcelain")
    assert _git(repo, "stash", "list") == ""


def test_git_routes_status_diff_stage_unstage_discard_commit(cleanup_test_sessions):
    sid, base_ws = _make_session(cleanup_test_sessions)
    repo = base_ws / f"git-route-{uuid.uuid4().hex[:8]}"
    _init_repo(repo)
    (repo / "tracked.txt").write_text("one\n", encoding="utf-8")
    _commit_all(repo)

    _post("/api/session/update", {"session_id": sid, "workspace": str(repo), "model": "openai/gpt-5.4-mini"})
    (repo / "tracked.txt").write_text("one\ntwo\n", encoding="utf-8")

    status, code = _get(f"/api/git/status?session_id={sid}")
    assert code == 200
    assert status["git"]["totals"]["unstaged"] == 1

    diff, code = _get(
        f"/api/git/diff?session_id={sid}&path={urllib.parse.quote('tracked.txt')}&kind=unstaged"
    )
    assert code == 200
    assert "+two" in diff["diff"]["diff"]

    staged, code = _post("/api/git/stage", {"session_id": sid, "paths": ["tracked.txt"]})
    assert code == 200 and staged["git"]["totals"]["staged"] == 1

    unstaged, code = _post("/api/git/unstage", {"session_id": sid, "paths": ["tracked.txt"]})
    assert code == 200 and unstaged["git"]["totals"]["unstaged"] == 1

    discarded, code = _post("/api/git/discard", {"session_id": sid, "paths": ["tracked.txt"]})
    assert code == 200 and discarded["git"]["totals"]["changed"] == 0

    (repo / "tracked.txt").write_text("one\nthree\n", encoding="utf-8")
    _post("/api/git/stage", {"session_id": sid, "paths": ["tracked.txt"]})
    committed, code = _post("/api/git/commit", {"session_id": sid, "message": "Route commit"})
    assert code == 200
    assert committed["ok"] is True
    assert committed["status"]["totals"]["changed"] == 0


def test_git_routes_branches_and_checkout(cleanup_test_sessions):
    sid, base_ws = _make_session(cleanup_test_sessions)
    repo = base_ws / f"git-branch-route-{uuid.uuid4().hex[:8]}"
    _init_repo(repo)
    (repo / "tracked.txt").write_text("one\n", encoding="utf-8")
    _commit_all(repo)
    _git(repo, "branch", "-M", "main")
    _git(repo, "checkout", "-b", "feature")
    _git(repo, "checkout", "main")

    _post("/api/session/update", {"session_id": sid, "workspace": str(repo), "model": "openai/gpt-5.4-mini"})
    branches, code = _get(f"/api/git/branches?session_id={sid}")
    assert code == 200
    assert branches["branches"]["current"] == "main"
    assert any(item["name"] == "feature" for item in branches["branches"]["local"])

    checked, code = _post(
        "/api/git/checkout",
        {"session_id": sid, "ref": "feature", "mode": "local", "dirty_mode": "block"},
    )
    assert code == 200
    assert checked["ok"] is True
    assert checked["current_branch"] == "feature"
    assert checked["git"]["branch"] == "feature"


def test_git_routes_selected_commit_and_structured_error(cleanup_test_sessions):
    sid, base_ws = _make_session(cleanup_test_sessions)
    repo = base_ws / f"git-selected-route-{uuid.uuid4().hex[:8]}"
    _init_repo(repo)
    (repo / "selected.txt").write_text("one\n", encoding="utf-8")
    (repo / "other.txt").write_text("alpha\n", encoding="utf-8")
    _commit_all(repo)

    _post("/api/session/update", {"session_id": sid, "workspace": str(repo), "model": "openai/gpt-5.4-mini"})
    (repo / "selected.txt").write_text("one\ntwo\n", encoding="utf-8")
    (repo / "other.txt").write_text("alpha\nbeta\n", encoding="utf-8")
    _git(repo, "add", "other.txt")

    bad, code = _post("/api/git/commit-selected", {"session_id": sid, "message": "Bad", "paths": ["../x"]})
    assert code == 400
    assert bad["code"] == "path_outside_workspace"

    committed, code = _post(
        "/api/git/commit-selected",
        {"session_id": sid, "message": "Selected route commit", "paths": ["selected.txt"]},
    )
    assert code == 200
    assert committed["ok"] is True
    assert committed["paths"] == ["selected.txt"]
    assert _git(repo, "show", "--name-only", "--format=", "HEAD").splitlines() == ["selected.txt"]


def test_git_env_scrub_removes_redirecting_vars_and_preserves_temp_index(monkeypatch):
    from api.workspace_git import _clean_git_env

    monkeypatch.setenv("GIT_DIR", "/tmp/evil-git-dir")
    monkeypatch.setenv("GIT_WORK_TREE", "/tmp/evil-work-tree")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/tmp/evil-config")
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", "/tmp/evil-system-config")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.sshCommand")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "ssh -i /tmp/evil-key")
    monkeypatch.setenv("GIT_CONFIG_PARAMETERS", "'core.sshCommand=ssh -i /tmp/evil-key'")

    env = _clean_git_env({"GIT_INDEX_FILE": "/tmp/hermes-index"})

    assert "GIT_DIR" not in env
    assert "GIT_WORK_TREE" not in env
    assert "GIT_CONFIG_GLOBAL" not in env
    assert "GIT_CONFIG_SYSTEM" not in env
    assert "GIT_CONFIG_COUNT" not in env
    assert "GIT_CONFIG_KEY_0" not in env
    assert "GIT_CONFIG_VALUE_0" not in env
    assert "GIT_CONFIG_PARAMETERS" not in env
    assert env["GIT_INDEX_FILE"] == "/tmp/hermes-index"


def test_git_error_classifier_identifies_non_fast_forward_push():
    from api.workspace_git import _classify_git_error

    assert _classify_git_error("Updates were rejected", ["push"]) == "non_fast_forward"
    assert _classify_git_error("non-fast-forward", ["push"]) == "non_fast_forward"
    assert _classify_git_error("fetch first", ["push"]) == "non_fast_forward"


def test_git_commit_hook_failure_returns_hook_failed_code(tmp_path):
    from api.workspace_git import GitWorkspaceError, git_commit, git_stage

    repo = _init_repo(tmp_path / "repo")
    (repo / "tracked.txt").write_text("one\n", encoding="utf-8")
    _commit_all(repo)
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\necho hook blocked >&2\nexit 1\n", encoding="utf-8")
    hook.chmod(0o755)

    (repo / "tracked.txt").write_text("one\ntwo\n", encoding="utf-8")
    git_stage(repo, ["tracked.txt"])

    with pytest.raises(GitWorkspaceError) as exc:
        git_commit(repo, "Hook should fail")
    assert exc.value.code == "hook_failed"


def test_destructive_workspace_git_flag_defaults_off_and_accepts_truthy(monkeypatch):
    from api.workspace_git import WORKSPACE_GIT_DESTRUCTIVE_ENV, workspace_git_destructive_enabled

    monkeypatch.delenv(WORKSPACE_GIT_DESTRUCTIVE_ENV, raising=False)
    assert workspace_git_destructive_enabled() is False

    monkeypatch.setenv(WORKSPACE_GIT_DESTRUCTIVE_ENV, "1")
    assert workspace_git_destructive_enabled() is True

    monkeypatch.setenv(WORKSPACE_GIT_DESTRUCTIVE_ENV, "true")
    assert workspace_git_destructive_enabled() is True


def test_git_active_stream_lock_detection(monkeypatch):
    from api import routes
    from api.config import STREAMS, STREAMS_LOCK

    session = types.SimpleNamespace(active_stream_id="stream-git-lock-test")
    with STREAMS_LOCK:
        STREAMS[session.active_stream_id] = object()
    try:
        assert routes._git_locked_by_active_stream(session) is True
    finally:
        with STREAMS_LOCK:
            STREAMS.pop(session.active_stream_id, None)

    assert routes._git_locked_by_active_stream(session) is False


def test_git_commit_route_rejects_active_stream(monkeypatch, tmp_path):
    from api import routes
    from api.config import STREAMS, STREAMS_LOCK
    from api.workspace_git import WORKSPACE_GIT_DESTRUCTIVE_ENV

    # Enable destructive ops for this in-process test — conftest.py sets the env
    # var on the test_server subprocess env block, but this test calls
    # _handle_git_commit() directly in the pytest process, which inherits
    # the default-OFF setting. Without this monkeypatch, the destructive-mode
    # gate fires first (403) before the active-stream check (409) can run.
    monkeypatch.setenv(WORKSPACE_GIT_DESTRUCTIVE_ENV, "1")

    repo = _init_repo(tmp_path / "repo")
    (repo / "tracked.txt").write_text("one\n", encoding="utf-8")
    _commit_all(repo)
    _git(repo, "add", "tracked.txt")
    session = types.SimpleNamespace(
        session_id="sid-active-git",
        workspace=str(repo),
        active_stream_id="stream-active-git",
    )

    monkeypatch.setattr(routes, "get_session", lambda sid: session)
    handler = _CaptureHandler()
    with STREAMS_LOCK:
        STREAMS[session.active_stream_id] = object()
    try:
        assert routes._handle_git_commit(
            handler,
            {"session_id": session.session_id, "message": "Should be blocked"},
        ) is True
    finally:
        with STREAMS_LOCK:
            STREAMS.pop(session.active_stream_id, None)

    assert handler.status == 409
    payload = handler.payload()
    assert payload["code"] == "active_stream"
    assert "active" in payload["error"].lower()
