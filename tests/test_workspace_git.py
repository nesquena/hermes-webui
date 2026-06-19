import json
import pathlib
import subprocess
import threading
import types
import uuid
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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


def test_git_diff_skips_repo_local_textconv(tmp_path):
    import os
    import sys

    if os.name == "nt":
        pytest.skip("scripted textconv helper setup is POSIX-only")

    from api.workspace_git import git_diff

    repo = _init_repo(tmp_path / "repo")
    (repo / ".gitattributes").write_text("*.txt diff=demo\n", encoding="utf-8")
    (repo / "tracked.txt").write_text("one\n", encoding="utf-8")
    _commit_all(repo)
    marker = tmp_path / "git-diff-textconv-ran"
    helper = tmp_path / "git_diff_textconv_helper.py"
    helper.write_text(
        "#!/usr/bin/env python3\n"
        "import pathlib, sys\n"
        "pathlib.Path(sys.argv[1]).write_text('textconv ran', encoding='utf-8')\n"
        "print('converted')\n",
        encoding="utf-8",
    )
    helper.chmod(0o755)
    _git(repo, "config", "diff.demo.textconv", f'"{sys.executable}" "{helper}" "{marker}"')

    (repo / "tracked.txt").write_text("one\ntwo\n", encoding="utf-8")

    diff = git_diff(repo, "tracked.txt", "unstaged")

    assert "+two" in diff["diff"]
    assert "converted" not in diff["diff"]
    assert not marker.exists()


def test_git_diff_skips_repo_local_clean_filter_without_destructive_mode(tmp_path, monkeypatch):
    import os
    import sys

    if os.name == "nt":
        pytest.skip("scripted clean filter setup is POSIX-only")

    from api.workspace_git import WORKSPACE_GIT_DESTRUCTIVE_ENV, git_diff

    repo = _init_repo(tmp_path / "repo")
    (repo / ".gitattributes").write_text("*.txt filter=demo\n", encoding="utf-8")
    (repo / "tracked.txt").write_text("one\n", encoding="utf-8")
    _commit_all(repo)
    marker = tmp_path / "git-diff-clean-filter-ran"
    helper = tmp_path / "git_diff_clean_filter_helper.py"
    helper.write_text(
        "#!/usr/bin/env python3\n"
        "import pathlib, sys\n"
        "pathlib.Path(sys.argv[1]).write_text('clean filter ran', encoding='utf-8')\n"
        "print(sys.stdin.read(), end='')\n",
        encoding="utf-8",
    )
    helper.chmod(0o755)
    _git(repo, "config", "filter.demo.clean", f'"{sys.executable}" "{helper}" "{marker}"')
    (repo / "tracked.txt").write_text("one\ntwo\n", encoding="utf-8")

    monkeypatch.delenv(WORKSPACE_GIT_DESTRUCTIVE_ENV, raising=False)
    diff = git_diff(repo, "tracked.txt", "unstaged")

    assert "+two" in diff["diff"]
    assert not marker.exists()


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


def test_git_discard_untracked_file_tolerates_concurrent_missing_file(tmp_path, monkeypatch):
    import api.workspace_git as workspace_git

    repo = _init_repo(tmp_path / "repo")
    (repo / "tracked.txt").write_text("one\n", encoding="utf-8")
    _commit_all(repo)
    transient = repo / "transient.txt"
    transient.write_text("gone soon\n", encoding="utf-8")

    original_unlink_anchored = workspace_git.unlink_anchored
    raced = {"seen": False}

    def remove_before_unlink(root, target):
        if target == transient:
            raced["seen"] = True
            transient.unlink()
        return original_unlink_anchored(root, target)

    monkeypatch.setattr(workspace_git, "unlink_anchored", remove_before_unlink)

    status = workspace_git.git_discard(repo, ["transient.txt"], delete_untracked=True)

    assert raced["seen"] is True
    assert not transient.exists()
    assert status["totals"]["changed"] == 0


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


def test_commit_message_prompts_skip_repo_local_textconv_when_destructive_mode_enabled(tmp_path, monkeypatch):
    import os
    import sys

    if os.name == "nt":
        pytest.skip("scripted textconv helper setup is POSIX-only")

    from api.workspace_git import (
        WORKSPACE_GIT_DESTRUCTIVE_ENV,
        selected_commit_message_prompt,
        staged_commit_message_prompt,
    )

    repo = _init_repo(tmp_path / "repo")
    (repo / ".gitattributes").write_text("*.txt diff=demo\n", encoding="utf-8")
    (repo / "tracked.txt").write_text("one\n", encoding="utf-8")
    _commit_all(repo)
    marker = tmp_path / "textconv-ran"
    helper = tmp_path / "textconv_helper.py"
    helper.write_text(
        "#!/usr/bin/env python3\n"
        "import pathlib, sys\n"
        "pathlib.Path(sys.argv[1]).write_text('textconv ran', encoding='utf-8')\n"
        "print('converted')\n",
        encoding="utf-8",
    )
    helper.chmod(0o755)
    _git(repo, "config", "diff.demo.textconv", f'"{sys.executable}" "{helper}" "{marker}"')

    (repo / "tracked.txt").write_text("one\ntwo\n", encoding="utf-8")
    _git(repo, "add", "tracked.txt")
    monkeypatch.setenv(WORKSPACE_GIT_DESTRUCTIVE_ENV, "1")

    staged = staged_commit_message_prompt(repo)
    selected = selected_commit_message_prompt(repo, ["tracked.txt"])

    assert "+two" in staged["user_prompt"]
    assert "+two" in selected["user_prompt"]
    assert "converted" not in staged["user_prompt"]
    assert "converted" not in selected["user_prompt"]
    assert not marker.exists()


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


def test_git_fetch_pull_and_push_skip_repo_local_remote_helpers_when_destructive_mode_enabled(tmp_path, monkeypatch):
    import os
    import sys

    if os.name == "nt":
        pytest.skip("scripted remote helper setup is POSIX-only")

    from api.workspace_git import WORKSPACE_GIT_DESTRUCTIVE_ENV, git_fetch, git_pull, git_push

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
    _git(clone, "config", "user.email", "hermes-tests@example.invalid")
    _git(clone, "config", "user.name", "Hermes Tests")

    marker = tmp_path / "remote-helper-ran"
    helper = tmp_path / "remote_helper.py"
    helper.write_text(
        "#!/usr/bin/env python3\n"
        "import pathlib, sys\n"
        "pathlib.Path(sys.argv[1]).write_text('remote helper ran', encoding='utf-8')\n"
        "raise SystemExit(1)\n",
        encoding="utf-8",
    )
    helper.chmod(0o755)
    helper_cmd = f'"{sys.executable}" "{helper}" "{marker}"'
    _git(clone, "config", "remote.origin.uploadpack", helper_cmd)
    _git(clone, "config", "remote.origin.receivepack", helper_cmd)

    (origin / "tracked.txt").write_text("one\ntwo\n", encoding="utf-8")
    _commit_all(origin, "Remote update")
    _git(origin, "push")

    monkeypatch.setenv(WORKSPACE_GIT_DESTRUCTIVE_ENV, "1")
    fetched = git_fetch(clone)
    pulled = git_pull(clone)

    assert fetched["status"]["behind"] == 1
    assert pulled["status"]["behind"] == 0
    assert not marker.exists()

    (clone / "tracked.txt").write_text("one\ntwo\nthree\n", encoding="utf-8")
    _git(clone, "add", "tracked.txt")
    _git(clone, "commit", "-m", "Local update")

    pushed = git_push(clone)

    assert pushed["status"]["ahead"] == 0
    assert not marker.exists()


def test_git_fetch_skips_repo_local_remote_helpers_without_destructive_mode(tmp_path, monkeypatch):
    import os
    import sys

    if os.name == "nt":
        pytest.skip("scripted remote helper setup is POSIX-only")

    from api.workspace_git import WORKSPACE_GIT_DESTRUCTIVE_ENV, git_fetch

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
    _git(clone, "config", "user.email", "hermes-tests@example.invalid")
    _git(clone, "config", "user.name", "Hermes Tests")

    marker = tmp_path / "remote-helper-default-fetch-ran"
    helper = tmp_path / "remote_helper_default_fetch.py"
    helper.write_text(
        "#!/usr/bin/env python3\n"
        "import pathlib, sys\n"
        "pathlib.Path(sys.argv[1]).write_text('remote helper ran', encoding='utf-8')\n"
        "raise SystemExit(1)\n",
        encoding="utf-8",
    )
    helper.chmod(0o755)
    helper_cmd = f'"{sys.executable}" "{helper}" "{marker}"'
    _git(clone, "config", "remote.origin.uploadpack", helper_cmd)

    (origin / "tracked.txt").write_text("one\ntwo\n", encoding="utf-8")
    _commit_all(origin, "Remote update")
    _git(origin, "push")

    monkeypatch.delenv(WORKSPACE_GIT_DESTRUCTIVE_ENV, raising=False)
    fetched = git_fetch(clone)

    assert fetched["status"]["behind"] == 1
    assert not marker.exists()


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


def test_git_discard_untracked_delete_uses_anchored_unlink_after_validation_race(tmp_path, monkeypatch):
    import os
    import shutil

    import api.workspace_git as workspace_git
    from api.workspace import safe_resolve_ws as real_safe_resolve_ws

    repo = _init_repo(tmp_path / "repo")
    (repo / "tracked.txt").write_text("tracked\n", encoding="utf-8")
    _commit_all(repo)

    (repo / "d").mkdir()
    (repo / "d" / "f").write_text("workspace untracked\n", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    victim = outside / "f"
    victim.write_text("outside victim\n", encoding="utf-8")

    state = {"calls": 0, "swapped": False}

    def racing_safe_resolve(root, requested):
        target = real_safe_resolve_ws(root, requested)
        if requested == "d/f":
            state["calls"] += 1
        # git_discard validates once for the Git pathspec and once immediately
        # before deletion. Race the second validation-to-use window.
        if requested == "d/f" and state["calls"] == 2 and not state["swapped"]:
            shutil.rmtree(repo / "d")
            os.symlink(outside, repo / "d")
            state["swapped"] = True
        return target

    monkeypatch.setattr(workspace_git, "safe_resolve_ws", racing_safe_resolve)

    with pytest.raises(ValueError, match="Path traversal blocked"):
        workspace_git.git_discard(repo, ["d/f"], delete_untracked=True)

    assert state["swapped"] is True
    assert victim.exists()
    assert victim.read_text(encoding="utf-8") == "outside victim\n"


def test_git_status_ignores_repo_local_fsmonitor_command(tmp_path):
    import os
    import sys

    if os.name == "nt":
        pytest.skip("executable fsmonitor helper setup is POSIX-only")

    from api.workspace_git import git_status

    repo = _init_repo(tmp_path / "repo")
    (repo / "tracked.txt").write_text("one\n", encoding="utf-8")
    _commit_all(repo)
    marker = tmp_path / "fsmonitor-ran"
    helper = tmp_path / "fsmonitor_helper.py"
    helper.write_text(
        "#!/usr/bin/env python3\n"
        "import pathlib, sys\n"
        "pathlib.Path(sys.argv[1]).write_text('fsmonitor executed', encoding='utf-8')\n"
        "print('')\n",
        encoding="utf-8",
    )
    helper.chmod(0o755)
    _git(repo, "config", "core.fsmonitor", f"{sys.executable} {helper} {marker}")

    status = git_status(repo)

    assert status["is_git"] is True
    assert not marker.exists()


def test_git_status_skips_repo_local_clean_filter_without_destructive_mode(tmp_path, monkeypatch):
    import os
    import sys

    if os.name == "nt":
        pytest.skip("scripted clean filter setup is POSIX-only")

    from api.workspace_git import WORKSPACE_GIT_DESTRUCTIVE_ENV, git_status

    repo = _init_repo(tmp_path / "repo")
    (repo / ".gitattributes").write_text("*.txt filter=demo\n", encoding="utf-8")
    (repo / "tracked.txt").write_text("one\n", encoding="utf-8")
    _commit_all(repo)
    marker = tmp_path / "git-status-clean-filter-ran"
    helper = tmp_path / "git_status_clean_filter_helper.py"
    helper.write_text(
        "#!/usr/bin/env python3\n"
        "import pathlib, sys\n"
        "pathlib.Path(sys.argv[1]).write_text('clean filter ran', encoding='utf-8')\n"
        "print(sys.stdin.read(), end='')\n",
        encoding="utf-8",
    )
    helper.chmod(0o755)
    _git(repo, "config", "filter.demo.clean", f'"{sys.executable}" "{helper}" "{marker}"')
    (repo / "tracked.txt").write_text("one\ntwo\n", encoding="utf-8")

    monkeypatch.delenv(WORKSPACE_GIT_DESTRUCTIVE_ENV, raising=False)
    status = git_status(repo)

    assert status["is_git"] is True
    assert status["totals"]["unstaged"] == 1
    assert not marker.exists()


def test_git_fetch_blocks_repo_local_ext_transport_execution(tmp_path):
    import os
    import sys

    if os.name == "nt":
        pytest.skip("ext transport helper setup is POSIX-only")

    from api.workspace_git import GitWorkspaceError, git_fetch

    repo = _init_repo(tmp_path / "repo")
    (repo / "tracked.txt").write_text("one\n", encoding="utf-8")
    _commit_all(repo)
    marker = tmp_path / "ext-transport-ran"
    helper = tmp_path / "ext_helper.py"
    helper.write_text(
        "#!/usr/bin/env python3\n"
        "import pathlib, sys\n"
        "pathlib.Path(sys.argv[1]).write_text('ext executed: ' + ' '.join(sys.argv[2:]), encoding='utf-8')\n"
        "sys.exit(1)\n",
        encoding="utf-8",
    )
    helper.chmod(0o755)
    _git(repo, "config", "protocol.ext.allow", "always")
    _git(repo, "remote", "add", "origin", f"ext::{sys.executable} {helper} {marker} %S foo")

    with pytest.raises(GitWorkspaceError) as exc:
        git_fetch(repo)

    assert exc.value.code == "git_failed"
    assert not marker.exists()


def test_git_fetch_blocks_repo_local_credential_helper_execution(tmp_path):
    import os
    import sys

    if os.name == "nt":
        pytest.skip("executable credential helper setup is POSIX-only")

    from api.workspace_git import GitWorkspaceError, git_fetch

    class AuthRequiredHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="hermes-test"')
            self.end_headers()

        def log_message(self, format, *args):
            del format, args

    repo = _init_repo(tmp_path / "repo")
    (repo / "tracked.txt").write_text("one\n", encoding="utf-8")
    _commit_all(repo)
    marker = tmp_path / "credential-helper-ran"
    helper = tmp_path / "credential_helper.py"
    helper.write_text(
        "#!/usr/bin/env python3\n"
        "import pathlib, sys\n"
        "pathlib.Path(sys.argv[1]).write_text('credential helper executed', encoding='utf-8')\n",
        encoding="utf-8",
    )
    helper.chmod(0o755)

    server = ThreadingHTTPServer(("127.0.0.1", 0), AuthRequiredHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_port}/repo.git"
        _git(repo, "remote", "add", "origin", url)
        _git(repo, "config", "credential.helper", f"!{sys.executable} {helper} {marker}")

        with pytest.raises(GitWorkspaceError) as exc:
            git_fetch(repo)
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()

    assert exc.value.code == "auth_failed"
    assert not marker.exists()


def test_git_fetch_blocks_repo_local_askpass_execution(tmp_path):
    import os
    import sys

    if os.name == "nt":
        pytest.skip("executable askpass helper setup is POSIX-only")

    from api.workspace_git import GitWorkspaceError, git_fetch

    class AuthRequiredHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="hermes-test"')
            self.end_headers()

        def log_message(self, format, *args):
            del format, args

    repo = _init_repo(tmp_path / "repo")
    (repo / "tracked.txt").write_text("one\n", encoding="utf-8")
    _commit_all(repo)
    marker = tmp_path / "askpass-ran"
    helper = tmp_path / "askpass_helper.py"
    helper.write_text(
        "#!/usr/bin/env python3\n"
        "import pathlib, sys\n"
        "pathlib.Path(sys.argv[1]).write_text('askpass executed', encoding='utf-8')\n"
        "print('pw')\n",
        encoding="utf-8",
    )
    helper.chmod(0o755)

    server = ThreadingHTTPServer(("127.0.0.1", 0), AuthRequiredHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_port}/repo.git"
        _git(repo, "remote", "add", "origin", url)
        _git(repo, "config", "core.askPass", f"{sys.executable} {helper} {marker}")

        with pytest.raises(GitWorkspaceError) as exc:
            git_fetch(repo)
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()

    assert exc.value.code == "auth_failed"
    assert not marker.exists()


def test_git_commit_skips_repo_local_hooks_when_destructive_mode_enabled(tmp_path, monkeypatch):
    import os
    import sys

    if os.name == "nt":
        pytest.skip("hook script setup is POSIX-only")

    from api.workspace_git import WORKSPACE_GIT_DESTRUCTIVE_ENV, git_commit, git_stage

    repo = _init_repo(tmp_path / "repo")
    (repo / "tracked.txt").write_text("one\n", encoding="utf-8")
    _commit_all(repo)
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    marker = tmp_path / "pre-commit-ran"
    helper = tmp_path / "pre_commit_helper.py"
    helper.write_text(
        "#!/usr/bin/env python3\n"
        "import pathlib, sys\n"
        "pathlib.Path(sys.argv[1]).write_text('pre-commit executed', encoding='utf-8')\n"
        "raise SystemExit(1)\n",
        encoding="utf-8",
    )
    helper.chmod(0o755)
    pre_commit = hooks / "pre-commit"
    pre_commit.write_text(
        "#!/bin/sh\n"
        f"\"{sys.executable}\" \"{helper}\" \"{marker}\"\n",
        encoding="utf-8",
    )
    pre_commit.chmod(0o755)
    _git(repo, "config", "core.hooksPath", str(hooks))

    (repo / "tracked.txt").write_text("one\ntwo\n", encoding="utf-8")
    git_stage(repo, ["tracked.txt"])

    monkeypatch.setenv(WORKSPACE_GIT_DESTRUCTIVE_ENV, "1")
    git_commit(repo, "Commit with hooks disabled")

    assert not marker.exists()


def test_git_commit_skips_default_repo_hooks_when_destructive_mode_enabled(tmp_path, monkeypatch):
    import os
    import sys

    if os.name == "nt":
        pytest.skip("hook script setup is POSIX-only")

    from api.workspace_git import WORKSPACE_GIT_DESTRUCTIVE_ENV, git_commit, git_stage

    repo = _init_repo(tmp_path / "repo")
    (repo / "tracked.txt").write_text("one\n", encoding="utf-8")
    _commit_all(repo)
    marker = tmp_path / "default-pre-commit-ran"
    helper = tmp_path / "default_pre_commit_helper.py"
    helper.write_text(
        "#!/usr/bin/env python3\n"
        "import pathlib, sys\n"
        "pathlib.Path(sys.argv[1]).write_text('default pre-commit executed', encoding='utf-8')\n"
        "raise SystemExit(1)\n",
        encoding="utf-8",
    )
    helper.chmod(0o755)
    pre_commit = repo / ".git" / "hooks" / "pre-commit"
    pre_commit.write_text(
        "#!/bin/sh\n"
        f"\"{sys.executable}\" \"{helper}\" \"{marker}\"\n",
        encoding="utf-8",
    )
    pre_commit.chmod(0o755)

    (repo / "tracked.txt").write_text("one\ntwo\n", encoding="utf-8")
    git_stage(repo, ["tracked.txt"])

    monkeypatch.setenv(WORKSPACE_GIT_DESTRUCTIVE_ENV, "1")
    git_commit(repo, "Commit with default hooks disabled")

    assert not marker.exists()


def test_git_checkout_skips_repo_local_hooks_when_destructive_mode_enabled(tmp_path, monkeypatch):
    import os
    import sys

    if os.name == "nt":
        pytest.skip("hook script setup is POSIX-only")

    from api.workspace_git import WORKSPACE_GIT_DESTRUCTIVE_ENV, git_checkout

    repo = _init_repo(tmp_path / "repo")
    (repo / "tracked.txt").write_text("one\n", encoding="utf-8")
    _commit_all(repo)
    _git(repo, "branch", "-M", "main")
    _git(repo, "checkout", "-b", "feature")
    _git(repo, "checkout", "main")
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    marker = tmp_path / "post-checkout-ran"
    helper = tmp_path / "post_checkout_helper.py"
    helper.write_text(
        "#!/usr/bin/env python3\n"
        "import pathlib, sys\n"
        "pathlib.Path(sys.argv[1]).write_text('post-checkout executed', encoding='utf-8')\n",
        encoding="utf-8",
    )
    helper.chmod(0o755)
    post_checkout = hooks / "post-checkout"
    post_checkout.write_text(
        "#!/bin/sh\n"
        f"\"{sys.executable}\" \"{helper}\" \"{marker}\"\n",
        encoding="utf-8",
    )
    post_checkout.chmod(0o755)
    _git(repo, "config", "core.hooksPath", str(hooks))

    monkeypatch.setenv(WORKSPACE_GIT_DESTRUCTIVE_ENV, "1")
    result = git_checkout(repo, "feature", "local")

    assert result["ok"] is True
    assert result["current_branch"] == "feature"
    assert not marker.exists()


def test_git_pull_skips_repo_local_hooks_when_destructive_mode_enabled(tmp_path, monkeypatch):
    import os
    import sys

    if os.name == "nt":
        pytest.skip("hook script setup is POSIX-only")

    from api.workspace_git import WORKSPACE_GIT_DESTRUCTIVE_ENV, git_pull

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
    _git(clone, "config", "user.email", "hermes-tests@example.invalid")
    _git(clone, "config", "user.name", "Hermes Tests")

    (origin / "tracked.txt").write_text("one\ntwo\n", encoding="utf-8")
    _commit_all(origin, "Remote update")
    _git(origin, "push")

    hooks = tmp_path / "hooks"
    hooks.mkdir()
    marker = tmp_path / "post-merge-ran"
    helper = tmp_path / "post_merge_helper.py"
    helper.write_text(
        "#!/usr/bin/env python3\n"
        "import pathlib, sys\n"
        "pathlib.Path(sys.argv[1]).write_text('post-merge executed', encoding='utf-8')\n",
        encoding="utf-8",
    )
    helper.chmod(0o755)
    post_merge = hooks / "post-merge"
    post_merge.write_text(
        "#!/bin/sh\n"
        f"\"{sys.executable}\" \"{helper}\" \"{marker}\"\n",
        encoding="utf-8",
    )
    post_merge.chmod(0o755)
    _git(clone, "config", "core.hooksPath", str(hooks))

    monkeypatch.setenv(WORKSPACE_GIT_DESTRUCTIVE_ENV, "1")
    git_pull(clone)

    assert not marker.exists()


def test_git_stage_skips_repo_local_filters_when_destructive_mode_disabled(tmp_path, monkeypatch):
    import os
    import sys

    if os.name == "nt":
        pytest.skip("scripted filter helper setup is POSIX-only")

    from api.workspace_git import WORKSPACE_GIT_DESTRUCTIVE_ENV, git_stage

    repo = _init_repo(tmp_path / "repo")
    (repo / ".gitattributes").write_text("*.txt filter=demo\n", encoding="utf-8")
    marker = tmp_path / "filter-ran"
    helper = tmp_path / "filter_helper.py"
    helper.write_text(
        "#!/usr/bin/env python3\n"
        "import pathlib, sys\n"
        "pathlib.Path(sys.argv[1]).write_text('filter ran', encoding='utf-8')\n"
        "print(sys.stdin.read(), end='')\n",
        encoding="utf-8",
    )
    helper.chmod(0o755)
    _git(repo, "config", "filter.demo.clean", f"\"{sys.executable}\" \"{helper}\" \"{marker}\"")
    _git(repo, "config", "filter.demo.smudge", f"\"{sys.executable}\" \"{helper}\" \"{marker}\"")

    (repo / "tracked.txt").write_text("one\n", encoding="utf-8")
    _commit_all(repo)
    marker.unlink(missing_ok=True)
    (repo / "tracked.txt").write_text("one\ntwo\n", encoding="utf-8")

    monkeypatch.delenv(WORKSPACE_GIT_DESTRUCTIVE_ENV, raising=False)
    staged = git_stage(repo, ["tracked.txt"])

    assert staged["totals"]["staged"] == 1
    assert not marker.exists()


def test_git_checkout_blocks_repo_local_filters_when_destructive_mode_enabled(tmp_path, monkeypatch):
    import os
    import sys

    if os.name == "nt":
        pytest.skip("scripted filter helper setup is POSIX-only")

    from api.workspace_git import (
        WORKSPACE_GIT_DESTRUCTIVE_ENV,
        GitWorkspaceError,
        git_checkout,
    )

    repo = _init_repo(tmp_path / "repo")
    (repo / ".gitattributes").write_text("*.txt filter=demo\n", encoding="utf-8")
    marker = tmp_path / "filter-smudge-ran"
    helper = tmp_path / "filter_checkout_helper.py"
    helper.write_text(
        "#!/usr/bin/env python3\n"
        "import pathlib, sys\n"
        "pathlib.Path(sys.argv[1]).write_text('filter ran', encoding='utf-8')\n"
        "print(sys.stdin.read(), end='')\n",
        encoding="utf-8",
    )
    helper.chmod(0o755)
    _git(repo, "config", "filter.demo.clean", f"\"{sys.executable}\" \"{helper}\" \"{marker}\"")
    _git(repo, "config", "filter.demo.smudge", f"\"{sys.executable}\" \"{helper}\" \"{marker}\"")
    _git(repo, "branch", "-M", "main")
    _git(repo, "add", ".gitattributes")
    _git(repo, "commit", "-m", "Initial")
    _git(repo, "branch", "feature")
    _git(repo, "checkout", "feature")
    (repo / "tracked.txt").write_text("from feature\n", encoding="utf-8")
    _git(repo, "add", "tracked.txt")
    _git(repo, "commit", "-m", "Feature update")
    _git(repo, "checkout", "main")
    marker.unlink(missing_ok=True)

    monkeypatch.setenv(WORKSPACE_GIT_DESTRUCTIVE_ENV, "1")
    with pytest.raises(GitWorkspaceError) as exc:
        git_checkout(repo, "feature", "local")
    assert exc.value.code == "filtered_path"
    assert not marker.exists()


def test_git_stage_blocks_repo_local_filters_when_destructive_mode_enabled(tmp_path, monkeypatch):
    import os
    import sys

    if os.name == "nt":
        pytest.skip("scripted filter helper setup is POSIX-only")

    from api.workspace_git import WORKSPACE_GIT_DESTRUCTIVE_ENV, GitWorkspaceError, git_stage

    repo = _init_repo(tmp_path / "repo")
    (repo / ".gitattributes").write_text("*.txt filter=demo\n", encoding="utf-8")
    marker = tmp_path / "filter-stage-ran"
    helper = tmp_path / "filter_stage_helper.py"
    helper.write_text(
        "#!/usr/bin/env python3\n"
        "import pathlib, sys\n"
        "pathlib.Path(sys.argv[1]).write_text('filter ran', encoding='utf-8')\n"
        "print(sys.stdin.read(), end='')\n",
        encoding="utf-8",
    )
    helper.chmod(0o755)
    _git(repo, "config", "filter.demo.clean", f"\"{sys.executable}\" \"{helper}\" \"{marker}\"")
    _git(repo, "config", "filter.demo.smudge", f"\"{sys.executable}\" \"{helper}\" \"{marker}\"")
    (repo / "tracked.txt").write_text("one\n", encoding="utf-8")
    _commit_all(repo)
    marker.unlink(missing_ok=True)
    (repo / "tracked.txt").write_text("one\ntwo\n", encoding="utf-8")

    monkeypatch.setenv(WORKSPACE_GIT_DESTRUCTIVE_ENV, "1")
    with pytest.raises(GitWorkspaceError) as exc:
        git_stage(repo, ["tracked.txt"])

    assert exc.value.code == "filtered_path"
    assert not marker.exists()


def test_git_discard_blocks_repo_local_filters_when_destructive_mode_enabled(tmp_path, monkeypatch):
    import os
    import sys

    if os.name == "nt":
        pytest.skip("scripted filter helper setup is POSIX-only")

    from api.workspace_git import WORKSPACE_GIT_DESTRUCTIVE_ENV, GitWorkspaceError, git_discard

    repo = _init_repo(tmp_path / "repo")
    (repo / ".gitattributes").write_text("*.txt filter=demo\n", encoding="utf-8")
    marker = tmp_path / "filter-discard-ran"
    helper = tmp_path / "filter_discard_helper.py"
    helper.write_text(
        "#!/usr/bin/env python3\n"
        "import pathlib, sys\n"
        "pathlib.Path(sys.argv[1]).write_text('filter ran', encoding='utf-8')\n"
        "print(sys.stdin.read(), end='')\n",
        encoding="utf-8",
    )
    helper.chmod(0o755)
    _git(repo, "config", "filter.demo.clean", f"\"{sys.executable}\" \"{helper}\" \"{marker}\"")
    _git(repo, "config", "filter.demo.smudge", f"\"{sys.executable}\" \"{helper}\" \"{marker}\"")
    (repo / "tracked.txt").write_text("one\n", encoding="utf-8")
    _commit_all(repo)
    marker.unlink(missing_ok=True)
    (repo / "tracked.txt").write_text("one\ntwo\n", encoding="utf-8")

    monkeypatch.setenv(WORKSPACE_GIT_DESTRUCTIVE_ENV, "1")
    with pytest.raises(GitWorkspaceError) as exc:
        git_discard(repo, ["tracked.txt"])
    assert exc.value.code == "filtered_path"
    assert (repo / "tracked.txt").read_text(encoding="utf-8") == "one\ntwo\n"
    assert not marker.exists()


def test_destructive_filter_overrides_include_worktree_scope(tmp_path):
    import os

    from api.workspace_git import _destructive_filter_overrides

    repo = _init_repo(tmp_path / "repo")
    _git(repo, "config", "extensions.worktreeConfig", "true")
    _git(repo, "config", "--worktree", "filter.demo.clean", "cat")
    _git(repo, "config", "--worktree", "filter.demo.required", "true")

    overrides = dict(_destructive_filter_overrides(repo, os.environ.copy()))

    assert overrides["filter.demo.clean"] == "cat"
    assert overrides["filter.demo.smudge"] == "cat"
    assert overrides["filter.demo.process"] == ""
    assert overrides["filter.demo.required"] == "false"


def test_destructive_filter_overrides_include_included_scope(tmp_path):
    import os

    from api.workspace_git import _destructive_filter_overrides

    repo = _init_repo(tmp_path / "repo")
    included = tmp_path / "included-filter.cfg"
    included.write_text(
        "[filter \"demo\"]\n"
        "\tclean = cat\n"
        "\trequired = true\n",
        encoding="utf-8",
    )
    _git(repo, "config", "include.path", str(included))

    overrides = dict(_destructive_filter_overrides(repo, os.environ.copy()))

    assert overrides["filter.demo.clean"] == "cat"
    assert overrides["filter.demo.smudge"] == "cat"
    assert overrides["filter.demo.process"] == ""
    assert overrides["filter.demo.required"] == "false"


def test_destructive_merge_driver_overrides_include_local_worktree_and_included_scope(tmp_path):
    import os

    from api.workspace_git import _destructive_merge_driver_overrides

    repo = _init_repo(tmp_path / "repo")
    included = tmp_path / "included-merge.cfg"
    included.write_text(
        "[merge \"included\"]\n"
        "\tdriver = cat\n",
        encoding="utf-8",
    )
    _git(repo, "config", "include.path", str(included))
    _git(repo, "config", "merge.local.driver", "cat")
    _git(repo, "config", "extensions.worktreeConfig", "true")
    _git(repo, "config", "--worktree", "merge.worktree.driver", "cat")

    overrides = dict(_destructive_merge_driver_overrides(repo, os.environ.copy()))

    trusted_driver = 'git merge-file "%A" "%O" "%B"'
    assert overrides["merge.included.driver"] == trusted_driver
    assert overrides["merge.local.driver"] == trusted_driver
    assert overrides["merge.worktree.driver"] == trusted_driver


def test_git_checkout_blocks_worktree_scope_filters_when_destructive_mode_enabled(tmp_path, monkeypatch):
    import os
    import sys

    if os.name == "nt":
        pytest.skip("scripted filter helper setup is POSIX-only")

    from api.workspace_git import (
        WORKSPACE_GIT_DESTRUCTIVE_ENV,
        GitWorkspaceError,
        git_checkout,
    )

    repo = _init_repo(tmp_path / "repo")
    (repo / ".gitattributes").write_text("*.txt filter=demo\n", encoding="utf-8")
    marker = tmp_path / "filter-worktree-smudge-ran"
    helper = tmp_path / "filter_worktree_checkout_helper.py"
    helper.write_text(
        "#!/usr/bin/env python3\n"
        "import pathlib, sys\n"
        "pathlib.Path(sys.argv[1]).write_text('filter ran', encoding='utf-8')\n"
        "print(sys.stdin.read(), end='')\n",
        encoding="utf-8",
    )
    helper.chmod(0o755)
    _git(repo, "config", "extensions.worktreeConfig", "true")
    _git(repo, "config", "--worktree", "filter.demo.clean", f"\"{sys.executable}\" \"{helper}\" \"{marker}\"")
    _git(repo, "config", "--worktree", "filter.demo.smudge", f"\"{sys.executable}\" \"{helper}\" \"{marker}\"")
    _git(repo, "branch", "-M", "main")
    _git(repo, "add", ".gitattributes")
    _git(repo, "commit", "-m", "Initial")
    _git(repo, "branch", "feature")
    _git(repo, "checkout", "feature")
    (repo / "tracked.txt").write_text("from feature\n", encoding="utf-8")
    _git(repo, "add", "tracked.txt")
    _git(repo, "commit", "-m", "Feature update")
    _git(repo, "checkout", "main")
    marker.unlink(missing_ok=True)

    monkeypatch.setenv(WORKSPACE_GIT_DESTRUCTIVE_ENV, "1")
    with pytest.raises(GitWorkspaceError) as exc:
        git_checkout(repo, "feature", "local")
    assert exc.value.code == "filtered_path"
    assert not marker.exists()


def test_git_fetch_and_pull_disable_submodule_recursion(monkeypatch, tmp_path):
    from api import workspace_git

    ctx = workspace_git.GitContext(tmp_path, tmp_path, "")
    calls = []

    def fake_run_git(ctx_or_cwd, args, **kwargs):
        calls.append(list(args))
        return types.SimpleNamespace(stdout="", stderr="", returncode=0)

    monkeypatch.setattr(workspace_git, "resolve_git_context", lambda workspace: ctx)
    monkeypatch.setattr(workspace_git, "_run_git", fake_run_git)
    monkeypatch.setattr(workspace_git, "git_status", lambda workspace: {"is_git": True})

    workspace_git.git_fetch(tmp_path)
    workspace_git.git_pull(tmp_path)

    assert ["fetch", "--prune", "--no-recurse-submodules"] in calls
    assert ["pull", "--ff-only", "--no-recurse-submodules"] in calls


def test_git_fetch_forces_destructive_hardening_without_flag(monkeypatch, tmp_path):
    from api import workspace_git

    ctx = workspace_git.GitContext(tmp_path, tmp_path, "")
    captured = {}

    def fake_run_git(ctx_or_cwd, args, **kwargs):
        captured["args"] = list(args)
        captured["kwargs"] = dict(kwargs)
        return types.SimpleNamespace(stdout="", stderr="", returncode=0)

    monkeypatch.setattr(workspace_git, "resolve_git_context", lambda workspace: ctx)
    monkeypatch.setattr(workspace_git, "_run_git", fake_run_git)
    monkeypatch.setattr(workspace_git, "git_status", lambda workspace: {"is_git": True})

    workspace_git.git_fetch(tmp_path)

    assert captured["args"] == ["fetch", "--prune", "--no-recurse-submodules"]
    assert captured["kwargs"]["force_destructive_hardening"] is True


def test_run_git_force_destructive_hardening_applies_hook_redirect_without_flag(monkeypatch, tmp_path):
    from api import workspace_git

    captured = {}
    hooks_dir = tmp_path / "hooks"
    hooks_dir.mkdir()

    def fake_subprocess_run(argv, **kwargs):
        captured["argv"] = list(argv)
        captured["kwargs"] = dict(kwargs)
        return types.SimpleNamespace(stdout="", stderr="", returncode=0)

    monkeypatch.setattr(workspace_git, "workspace_git_destructive_enabled", lambda: False)
    monkeypatch.setattr(workspace_git.subprocess, "run", fake_subprocess_run)
    monkeypatch.setattr(workspace_git.tempfile, "mkdtemp", lambda prefix: str(hooks_dir))

    workspace_git._run_git(tmp_path, ["fetch"], force_destructive_hardening=True)

    assert any(str(arg).startswith("core.hooksPath=") for arg in captured["argv"])
    assert "core.alternateRefsCommand=" in captured["argv"]


def test_git_pull_blocks_repo_local_filters_before_run_when_destructive_mode_enabled(monkeypatch, tmp_path):
    from api import workspace_git

    ctx = workspace_git.GitContext(tmp_path, tmp_path, "")

    monkeypatch.setattr(workspace_git, "resolve_git_context", lambda workspace: ctx)
    monkeypatch.setattr(workspace_git, "workspace_git_destructive_enabled", lambda: True)
    monkeypatch.setattr(workspace_git, "_has_repo_local_filters", lambda cwd, env: True)

    with pytest.raises(workspace_git.GitWorkspaceError) as exc:
        workspace_git.git_pull(tmp_path)

    assert exc.value.code == "filtered_path"


def test_git_stage_blocks_repo_local_filters_before_run_when_destructive_mode_enabled(monkeypatch, tmp_path):
    from api import workspace_git

    ctx = workspace_git.GitContext(tmp_path, tmp_path, "")

    monkeypatch.setattr(workspace_git, "resolve_git_context", lambda workspace: ctx)
    monkeypatch.setattr(workspace_git, "workspace_git_destructive_enabled", lambda: True)
    monkeypatch.setattr(workspace_git, "_has_repo_local_filters", lambda cwd, env: True)

    with pytest.raises(workspace_git.GitWorkspaceError) as exc:
        workspace_git.git_stage(tmp_path, ["tracked.txt"])

    assert exc.value.code == "filtered_path"


def test_git_fetch_skips_repo_local_reference_transaction_hook_without_destructive_mode(tmp_path, monkeypatch):
    import os
    import sys

    if os.name == "nt":
        pytest.skip("hook script setup is POSIX-only")

    from api.workspace_git import WORKSPACE_GIT_DESTRUCTIVE_ENV, git_fetch

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
    _git(clone, "config", "user.email", "hermes-tests@example.invalid")
    _git(clone, "config", "user.name", "Hermes Tests")

    (origin / "tracked.txt").write_text("one\ntwo\n", encoding="utf-8")
    _commit_all(origin, "Remote update")
    _git(origin, "push")

    hooks = tmp_path / "hooks"
    hooks.mkdir()
    marker = tmp_path / "reference-transaction-ran"
    helper = tmp_path / "reference_transaction_helper.py"
    helper.write_text(
        "#!/usr/bin/env python3\n"
        "import pathlib, sys\n"
        "pathlib.Path(sys.argv[1]).write_text('reference-transaction executed', encoding='utf-8')\n"
        "sys.stdin.read()\n",
        encoding="utf-8",
    )
    helper.chmod(0o755)
    hook = hooks / "reference-transaction"
    hook.write_text(
        "#!/bin/sh\n"
        f"\"{sys.executable}\" \"{helper}\" \"{marker}\" \"$@\"\n",
        encoding="utf-8",
    )
    hook.chmod(0o755)
    _git(clone, "config", "core.hooksPath", str(hooks))

    monkeypatch.delenv(WORKSPACE_GIT_DESTRUCTIVE_ENV, raising=False)
    fetched = git_fetch(clone)

    assert fetched["status"]["behind"] == 1
    assert not marker.exists()


def test_git_checkout_disables_submodule_recursion(monkeypatch, tmp_path):
    from api import workspace_git

    ctx = workspace_git.GitContext(tmp_path, tmp_path, "")
    calls = []

    def fake_run_git(ctx_or_cwd, args, **kwargs):
        calls.append(list(args))
        return types.SimpleNamespace(stdout="", stderr="", returncode=0)

    monkeypatch.setattr(workspace_git, "resolve_git_context", lambda workspace: ctx)
    monkeypatch.setattr(workspace_git, "_run_git", fake_run_git)
    monkeypatch.setattr(workspace_git, "_dirty_worktree", lambda ctx: False)
    monkeypatch.setattr(workspace_git, "_current_checkout_label", lambda ctx: "main")
    monkeypatch.setattr(workspace_git, "_restore_branch_switch_stash_locked", lambda ctx, branch: {})
    monkeypatch.setattr(workspace_git, "git_status", lambda workspace: {"is_git": True, "branch": "feature"})
    monkeypatch.setattr(workspace_git, "git_branches", lambda workspace: {"current": "feature"})

    result = workspace_git.git_checkout(tmp_path, "feature", "local")

    assert result["current_branch"] == "feature"
    assert ["switch", "--recurse-submodules=no", "feature"] in calls


def test_perform_checkout_locked_disables_submodule_recursion_across_modes(monkeypatch, tmp_path):
    from api import workspace_git

    ctx = workspace_git.GitContext(tmp_path, tmp_path, "")
    calls = []

    def fake_run_git(ctx_or_cwd, args, **kwargs):
        calls.append(list(args))
        if args[:2] == ["show-ref", "--verify"]:
            return types.SimpleNamespace(stdout="", stderr="", returncode=1)
        return types.SimpleNamespace(stdout="", stderr="", returncode=0)

    monkeypatch.setattr(workspace_git, "_run_git", fake_run_git)
    monkeypatch.setattr(workspace_git, "_validate_local_branch", lambda ctx, ref: ref)
    monkeypatch.setattr(workspace_git, "_validate_new_branch_name", lambda ctx, name: name)
    monkeypatch.setattr(workspace_git, "_validate_checkout_start", lambda ctx, ref: ref)
    monkeypatch.setattr(workspace_git, "_validate_remote_branch", lambda ctx, ref: ref)

    workspace_git._perform_checkout_locked(ctx, tmp_path, "feature", "new", "topic", False)
    workspace_git._perform_checkout_locked(ctx, tmp_path, "origin/topic", "remote", "topic", True)
    workspace_git._perform_checkout_locked(ctx, tmp_path, "deadbeef", "detach", None, False)

    assert ["switch", "--recurse-submodules=no", "-c", "topic", "feature"] in calls
    assert ["switch", "--recurse-submodules=no", "-c", "topic", "--track", "origin/topic"] in calls
    assert ["switch", "--recurse-submodules=no", "--detach", "deadbeef"] in calls


def test_git_stage_skips_included_repo_local_filters_when_destructive_mode_disabled(tmp_path, monkeypatch):
    import os
    import sys

    if os.name == "nt":
        pytest.skip("scripted filter helper setup is POSIX-only")

    from api.workspace_git import WORKSPACE_GIT_DESTRUCTIVE_ENV, git_stage

    repo = _init_repo(tmp_path / "repo")
    (repo / ".gitattributes").write_text("*.txt filter=demo\n", encoding="utf-8")
    marker = tmp_path / "included-filter-ran"
    helper = tmp_path / "included_filter_helper.py"
    helper.write_text(
        "#!/usr/bin/env python3\n"
        "import pathlib, sys\n"
        "pathlib.Path(sys.argv[1]).write_text('filter ran', encoding='utf-8')\n"
        "print(sys.stdin.read(), end='')\n",
        encoding="utf-8",
    )
    helper.chmod(0o755)
    included = tmp_path / "included-filter.cfg"
    included.write_text(
        "[filter \"demo\"]\n"
        f"\tclean = \"{sys.executable}\" \"{helper}\" \"{marker}\"\n"
        f"\tsmudge = \"{sys.executable}\" \"{helper}\" \"{marker}\"\n",
        encoding="utf-8",
    )
    _git(repo, "config", "include.path", str(included))

    (repo / "tracked.txt").write_text("one\n", encoding="utf-8")
    _commit_all(repo)
    marker.unlink(missing_ok=True)
    (repo / "tracked.txt").write_text("one\ntwo\n", encoding="utf-8")

    monkeypatch.delenv(WORKSPACE_GIT_DESTRUCTIVE_ENV, raising=False)
    staged = git_stage(repo, ["tracked.txt"])

    assert staged["totals"]["staged"] == 1
    assert not marker.exists()


def test_selected_temp_index_env_blocks_repo_local_filters_when_destructive_mode_enabled(monkeypatch, tmp_path):
    from api import workspace_git

    ctx = workspace_git.GitContext(tmp_path, tmp_path, "")

    monkeypatch.setattr(workspace_git, "workspace_git_destructive_enabled", lambda: True)
    monkeypatch.setattr(workspace_git, "_has_repo_local_filters", lambda cwd, env: True)

    with pytest.raises(workspace_git.GitWorkspaceError) as exc:
        workspace_git._selected_temp_index_env(ctx, ["tracked.txt"])

    assert exc.value.code == "filtered_path"


def test_git_stage_blocks_included_repo_local_filters_when_destructive_mode_enabled(tmp_path, monkeypatch):
    import os
    import sys

    if os.name == "nt":
        pytest.skip("scripted filter helper setup is POSIX-only")

    from api.workspace_git import WORKSPACE_GIT_DESTRUCTIVE_ENV, GitWorkspaceError, git_stage

    repo = _init_repo(tmp_path / "repo")
    (repo / ".gitattributes").write_text("*.txt filter=demo\n", encoding="utf-8")
    marker = tmp_path / "included-filter-block-ran"
    helper = tmp_path / "included_filter_block_helper.py"
    helper.write_text(
        "#!/usr/bin/env python3\n"
        "import pathlib, sys\n"
        "pathlib.Path(sys.argv[1]).write_text('filter ran', encoding='utf-8')\n"
        "print(sys.stdin.read(), end='')\n",
        encoding="utf-8",
    )
    helper.chmod(0o755)
    included = tmp_path / "included-filter-block.cfg"
    included.write_text(
        "[filter \"demo\"]\n"
        f"\tclean = \"{sys.executable}\" \"{helper}\" \"{marker}\"\n"
        f"\tsmudge = \"{sys.executable}\" \"{helper}\" \"{marker}\"\n",
        encoding="utf-8",
    )
    _git(repo, "config", "include.path", str(included))

    (repo / "tracked.txt").write_text("one\n", encoding="utf-8")
    _commit_all(repo)
    marker.unlink(missing_ok=True)
    (repo / "tracked.txt").write_text("one\ntwo\n", encoding="utf-8")

    monkeypatch.setenv(WORKSPACE_GIT_DESTRUCTIVE_ENV, "1")
    with pytest.raises(GitWorkspaceError) as exc:
        git_stage(repo, ["tracked.txt"])

    assert exc.value.code == "filtered_path"
    assert not marker.exists()


def test_git_stash_restore_skips_repo_local_merge_drivers_when_destructive_mode_enabled(tmp_path, monkeypatch):
    import os
    import sys

    if os.name == "nt":
        pytest.skip("scripted merge driver setup is POSIX-only")

    from api.workspace_git import WORKSPACE_GIT_DESTRUCTIVE_ENV, git_stash_and_checkout

    repo = _init_repo(tmp_path / "repo")
    _git(repo, "branch", "-M", "main")
    (repo / ".gitattributes").write_text("tracked.txt merge=demo\n", encoding="utf-8")
    (repo / "tracked.txt").write_text("one\n", encoding="utf-8")
    _commit_all(repo)
    _git(repo, "checkout", "-b", "feature")
    _git(repo, "checkout", "main")
    (repo / "tracked.txt").write_text("main dirty\n", encoding="utf-8")

    marker = tmp_path / "merge-driver-ran"
    helper = tmp_path / "merge_driver_helper.py"
    helper.write_text(
        "#!/usr/bin/env python3\n"
        "import pathlib, sys\n"
        "pathlib.Path(sys.argv[1]).write_text('merge driver ran', encoding='utf-8')\n"
        "raise SystemExit(1)\n",
        encoding="utf-8",
    )
    helper.chmod(0o755)
    _git(repo, "config", "merge.demo.driver", f'"{sys.executable}" "{helper}" "{marker}"')

    monkeypatch.setenv(WORKSPACE_GIT_DESTRUCTIVE_ENV, "1")
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
    assert not marker.exists()


def test_git_commit_skips_repo_local_gpg_program_when_destructive_mode_enabled(tmp_path, monkeypatch):
    import os
    import sys

    if os.name == "nt":
        pytest.skip("non-interactive gpg helper test is POSIX-only")

    from api.workspace_git import (
        WORKSPACE_GIT_DESTRUCTIVE_ENV,
        git_commit,
        git_stage,
    )

    repo = _init_repo(tmp_path / "repo")
    (repo / "tracked.txt").write_text("one\n", encoding="utf-8")
    _commit_all(repo)
    marker = tmp_path / "gpg-ran"
    helper = tmp_path / "gpg_helper.py"
    helper.write_text(
        "#!/usr/bin/env python3\n"
        "import pathlib, sys\n"
        "pathlib.Path(sys.argv[1]).write_text('gpg executed', encoding='utf-8')\n"
        "raise SystemExit(1)\n",
        encoding="utf-8",
    )
    helper.chmod(0o755)
    _git(repo, "config", "commit.gpgsign", "true")
    _git(repo, "config", "gpg.program", f"\"{sys.executable}\" \"{helper}\" \"{marker}\"")

    (repo / "tracked.txt").write_text("one\ntwo\n", encoding="utf-8")
    git_stage(repo, ["tracked.txt"])
    monkeypatch.setenv(WORKSPACE_GIT_DESTRUCTIVE_ENV, "1")
    result = git_commit(repo, "Signed commit path blocked")

    assert result["ok"] is True
    assert not marker.exists()


def test_git_pull_blocks_repo_local_filters_when_destructive_mode_enabled(tmp_path, monkeypatch):
    import os
    import sys

    if os.name == "nt":
        pytest.skip("scripted filter helper setup is POSIX-only")

    from api.workspace_git import WORKSPACE_GIT_DESTRUCTIVE_ENV, GitWorkspaceError, git_pull

    remote = _init_bare_repo(tmp_path / "remote.git")
    origin = _init_repo(tmp_path / "origin")
    (origin / ".gitattributes").write_text("*.txt filter=demo\n", encoding="utf-8")
    (origin / "tracked.txt").write_text("one\n", encoding="utf-8")
    _git(origin, "config", "filter.demo.clean", "cat")
    _git(origin, "config", "filter.demo.smudge", "cat")
    _commit_all(origin)
    _git(origin, "branch", "-M", "main")
    _git(origin, "remote", "add", "origin", str(remote))
    _git(origin, "push", "-u", "origin", "main")
    _git(remote, "symbolic-ref", "HEAD", "refs/heads/main")

    clone = tmp_path / "clone"
    _git(tmp_path, "clone", str(remote), str(clone))
    _git(clone, "config", "user.email", "hermes-tests@example.invalid")
    _git(clone, "config", "user.name", "Hermes Tests")
    _git(clone, "config", "filter.demo.clean", f"\"{sys.executable}\" -c \"import sys; print(sys.stdin.read(), end='')\"")
    _git(clone, "config", "filter.demo.smudge", f"\"{sys.executable}\" -c \"import sys; print(sys.stdin.read(), end='')\"")

    (origin / "tracked.txt").write_text("one\ntwo\n", encoding="utf-8")
    _commit_all(origin, "Remote update")
    _git(origin, "push")

    monkeypatch.setenv(WORKSPACE_GIT_DESTRUCTIVE_ENV, "1")
    with pytest.raises(GitWorkspaceError) as exc:
        git_pull(clone)

    assert exc.value.code == "filtered_path"
    assert (clone / "tracked.txt").read_text(encoding="utf-8") == "one\n"


def test_git_commit_selected_blocks_repo_local_filters_when_destructive_mode_enabled(tmp_path, monkeypatch):
    import os
    import sys

    if os.name == "nt":
        pytest.skip("scripted filter helper setup is POSIX-only")

    from api.workspace_git import WORKSPACE_GIT_DESTRUCTIVE_ENV, GitWorkspaceError, git_commit_selected

    repo = _init_repo(tmp_path / "repo")
    (repo / ".gitattributes").write_text("*.txt filter=demo\n", encoding="utf-8")
    marker = tmp_path / "selected-filter-ran"
    helper = tmp_path / "selected_filter_helper.py"
    helper.write_text(
        "#!/usr/bin/env python3\n"
        "import pathlib, sys\n"
        "pathlib.Path(sys.argv[1]).write_text('filter ran', encoding='utf-8')\n"
        "print(sys.stdin.read(), end='')\n",
        encoding="utf-8",
    )
    helper.chmod(0o755)
    _git(repo, "config", "filter.demo.clean", f"\"{sys.executable}\" \"{helper}\" \"{marker}\"")
    _git(repo, "config", "filter.demo.smudge", f"\"{sys.executable}\" \"{helper}\" \"{marker}\"")
    (repo / "tracked.txt").write_text("one\n", encoding="utf-8")
    _commit_all(repo)
    marker.unlink(missing_ok=True)
    (repo / "tracked.txt").write_text("one\ntwo\n", encoding="utf-8")

    monkeypatch.setenv(WORKSPACE_GIT_DESTRUCTIVE_ENV, "1")
    with pytest.raises(GitWorkspaceError) as exc:
        git_commit_selected(repo, "Commit selected path", ["tracked.txt"])

    assert exc.value.code == "filtered_path"
    assert not marker.exists()


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
    monkeypatch.setenv("GIT_ASKPASS", "/tmp/evil-askpass")
    monkeypatch.setenv("SSH_ASKPASS", "/tmp/evil-ssh-askpass")
    monkeypatch.setenv("GIT_SSH", "/tmp/evil-ssh")
    monkeypatch.setenv("GIT_SSH_COMMAND", "ssh -i /tmp/evil-key")
    monkeypatch.setenv("GIT_TERMINAL_PROMPT", "1")

    env = _clean_git_env({"GIT_INDEX_FILE": "/tmp/hermes-index"})

    assert "GIT_DIR" not in env
    assert "GIT_WORK_TREE" not in env
    assert "GIT_CONFIG_GLOBAL" not in env
    assert "GIT_CONFIG_SYSTEM" not in env
    assert "GIT_CONFIG_COUNT" not in env
    assert "GIT_CONFIG_KEY_0" not in env
    assert "GIT_CONFIG_VALUE_0" not in env
    assert "GIT_CONFIG_PARAMETERS" not in env
    assert "GIT_ASKPASS" not in env
    assert "SSH_ASKPASS" not in env
    assert "GIT_SSH" not in env
    assert "GIT_SSH_COMMAND" not in env
    assert env["GIT_TERMINAL_PROMPT"] == "0"
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


def test_selected_commit_message_prompt_skips_clean_filter_without_destructive_mode(tmp_path, monkeypatch):
    import os
    import sys

    if os.name == "nt":
        pytest.skip("scripted clean filter setup is POSIX-only")

    from api.workspace_git import WORKSPACE_GIT_DESTRUCTIVE_ENV, selected_commit_message_prompt

    repo = _init_repo(tmp_path / "repo")
    (repo / ".gitattributes").write_text("*.txt filter=demo\n", encoding="utf-8")
    (repo / "selected.txt").write_text("one\n", encoding="utf-8")
    _commit_all(repo)
    marker = tmp_path / "selected-clean-filter-ran"
    helper = tmp_path / "selected_clean_filter_helper.py"
    helper.write_text(
        "#!/usr/bin/env python3\n"
        "import pathlib, sys\n"
        "pathlib.Path(sys.argv[1]).write_text('clean filter ran', encoding='utf-8')\n"
        "print(sys.stdin.read(), end='')\n",
        encoding="utf-8",
    )
    helper.chmod(0o755)
    _git(repo, "config", "filter.demo.clean", f'"{sys.executable}" "{helper}" "{marker}"')
    (repo / "selected.txt").write_text("one\ntwo\n", encoding="utf-8")

    monkeypatch.delenv(WORKSPACE_GIT_DESTRUCTIVE_ENV, raising=False)
    prompt = selected_commit_message_prompt(repo, ["selected.txt"])

    assert "+two" in prompt["user_prompt"]
    assert not marker.exists()


def test_staged_commit_message_prompt_skips_clean_filter_without_destructive_mode(tmp_path, monkeypatch):
    import os
    import sys

    if os.name == "nt":
        pytest.skip("scripted clean filter setup is POSIX-only")

    from api.workspace_git import WORKSPACE_GIT_DESTRUCTIVE_ENV, staged_commit_message_prompt

    repo = _init_repo(tmp_path / "repo")
    # Install .gitattributes without a filter program so git add runs as identity
    (repo / ".gitattributes").write_text("*.txt filter=demo\n", encoding="utf-8")
    (repo / "staged.txt").write_text("one\n", encoding="utf-8")
    _commit_all(repo)

    # Stage the modification before the malicious filter program is registered,
    # so the git add step itself does not trigger the payload.
    (repo / "staged.txt").write_text("one\ntwo\n", encoding="utf-8")
    _git(repo, "add", "staged.txt")

    # Now install the malicious filter — staged_commit_message_prompt's git diff
    # --cached must not invoke clean filters on index-vs-HEAD comparisons.
    marker = tmp_path / "staged-clean-filter-ran"
    helper = tmp_path / "staged_clean_filter_helper.py"
    helper.write_text(
        "#!/usr/bin/env python3\n"
        "import pathlib, sys\n"
        "pathlib.Path(sys.argv[1]).write_text('clean filter ran', encoding='utf-8')\n"
        "print(sys.stdin.read(), end='')\n",
        encoding="utf-8",
    )
    helper.chmod(0o755)
    _git(repo, "config", "filter.demo.clean", f'"{sys.executable}" "{helper}" "{marker}"')

    monkeypatch.delenv(WORKSPACE_GIT_DESTRUCTIVE_ENV, raising=False)
    prompt = staged_commit_message_prompt(repo)

    assert "+two" in prompt["user_prompt"]
    assert not marker.exists()


def test_git_hardened_config_blocks_submodule_recursion():
    from api.workspace_git import _GIT_HARDENED_CONFIG

    config = dict(_GIT_HARDENED_CONFIG)
    assert config.get("submodule.recurse") == "false"
    assert config.get("fetch.recurseSubmodules") == "false"


def test_git_status_blocks_injection_via_filter_name_with_equals(tmp_path, monkeypatch):
    import os
    import sys

    if os.name == "nt":
        pytest.skip("scripted filter setup is POSIX-only")

    from api.workspace_git import WORKSPACE_GIT_DESTRUCTIVE_ENV, git_status

    repo = _init_repo(tmp_path / "repo")
    (repo / ".gitattributes").write_text("*.txt filter=evil=core.sshCommand\n", encoding="utf-8")
    (repo / "tracked.txt").write_text("one\n", encoding="utf-8")
    _commit_all(repo)
    marker = tmp_path / "injected-command-ran"
    helper = tmp_path / "injected_helper.py"
    helper.write_text(
        "#!/usr/bin/env python3\n"
        "import pathlib, sys\n"
        f"pathlib.Path('{marker}').write_text('injected', encoding='utf-8')\n"
        "print(sys.stdin.read(), end='')\n",
        encoding="utf-8",
    )
    helper.chmod(0o755)
    _git(repo, "config", "filter.evil=core.sshCommand.clean", f'"{sys.executable}" "{helper}"')
    _git(repo, "config", "core.sshCommand", f'"{sys.executable}" "{helper}"')
    (repo / "tracked.txt").write_text("two\n", encoding="utf-8")

    monkeypatch.delenv(WORKSPACE_GIT_DESTRUCTIVE_ENV, raising=False)
    status = git_status(repo)

    assert status["is_git"] is True
    assert not marker.exists()


def test_git_discard_blocks_when_repo_local_filters_present(tmp_path, monkeypatch):
    import os

    if os.name == "nt":
        pytest.skip("scripted filter setup is POSIX-only")

    from api.workspace_git import WORKSPACE_GIT_DESTRUCTIVE_ENV, GitWorkspaceError, git_discard

    repo = _init_repo(tmp_path / "repo")
    (repo / ".gitattributes").write_text("*.txt filter=keyword\n", encoding="utf-8")
    (repo / "tracked.txt").write_text("original\n", encoding="utf-8")
    _commit_all(repo)
    _git(repo, "config", "filter.keyword.clean", "cat")
    _git(repo, "config", "filter.keyword.smudge", "tr a-z A-Z")
    (repo / "tracked.txt").write_text("modified\n", encoding="utf-8")

    monkeypatch.setenv(WORKSPACE_GIT_DESTRUCTIVE_ENV, "1")
    with pytest.raises(GitWorkspaceError) as exc:
        git_discard(repo, ["tracked.txt"])
    assert exc.value.code == "filtered_path"
    assert (repo / "tracked.txt").read_text(encoding="utf-8") == "modified\n"


def test_dirty_worktree_uses_filter_neutralization(tmp_path):
    import os
    import sys

    if os.name == "nt":
        pytest.skip("scripted filter setup is POSIX-only")

    from api.workspace_git import _dirty_worktree, resolve_git_context

    repo = _init_repo(tmp_path / "repo")
    (repo / ".gitattributes").write_text("*.txt filter=demo\n", encoding="utf-8")
    (repo / "tracked.txt").write_text("one\n", encoding="utf-8")
    _commit_all(repo)
    marker = tmp_path / "dirty-worktree-filter-ran"
    helper = tmp_path / "dirty_filter_helper.py"
    helper.write_text(
        "#!/usr/bin/env python3\n"
        "import pathlib, sys\n"
        f"pathlib.Path('{marker}').write_text('filter ran', encoding='utf-8')\n"
        "print(sys.stdin.read(), end='')\n",
        encoding="utf-8",
    )
    helper.chmod(0o755)
    _git(repo, "config", "filter.demo.clean", f'"{sys.executable}" "{helper}"')

    ctx = resolve_git_context(repo)
    _dirty_worktree(ctx)

    assert not marker.exists()
