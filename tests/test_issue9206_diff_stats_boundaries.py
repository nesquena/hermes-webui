"""#9206 release-gate folds: patch-record boundaries in ``_diff_stats``.

Two reproduced gaps in the new hunk-aware counter:

* ``str.splitlines()`` also splits on CR, VT, FF, NEL and U+2028/U+2029, so a
  changed line holding one of those followed by ``--- ``/``+++ `` produced a
  phantom deletion/addition (``+1/-2`` where git reports ``+1/-1``). A lone CR
  was also translated by text-mode capture before the counter saw it.
* The header state reset only on ``diff --git``; combined diffs of unmerged
  paths start with ``diff --cc``, so a second conflicted file's ``---``/``+++``
  headers were counted as content.
"""
import subprocess

import pytest

from api.workspace_git import DIFF_SIZE_LIMIT, _diff_stats, _iter_lf_records, git_diff
from tests.test_workspace_git import _commit_all, _git, _init_repo

pytestmark = pytest.mark.usefixtures("isolated_host_git_config")

SEPARATORS = ["\u2028", "\u2029", "\x85", "\x0b", "\x0c", "\r"]


def _numstat(repo, *args):
    add = delete = 0
    for row in _git(repo, "diff", "--numstat", *args).splitlines():
        a, d, *_ = row.split("\t")
        if a != "-":
            add += int(a)
            delete += int(d)
    return add, delete


@pytest.mark.parametrize("separator", SEPARATORS)
@pytest.mark.parametrize("prefix", ["--- appendix", "+++ appendix"])
def test_tracked_content_separators_are_not_patch_records(tmp_path, separator, prefix):
    repo = _init_repo(tmp_path / "repo")
    (repo / "note.md").write_bytes(b"old note\n")
    _commit_all(repo)
    (repo / "note.md").write_bytes(("new note" + separator + prefix + "\n").encode())
    assert _numstat(repo, "--", "note.md") == (1, 1)
    payload = git_diff(repo, "note.md")
    assert (payload["additions"], payload["deletions"]) == (1, 1), payload


@pytest.mark.parametrize("separator", SEPARATORS)
def test_diff_stats_splits_on_literal_lf_only(separator):
    patch = ("diff --git a/a b/a\n--- a/a\n+++ b/a\n@@ -1 +1 @@\n-old\n+new"
             + separator + "--- appendix\n")
    assert _diff_stats(patch) == (1, 1)


def test_crlf_payload_keeps_translated_form(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    _git(repo, "config", "core.autocrlf", "false")
    (repo / "win.txt").write_bytes(b"one\r\ntwo\r\n")
    _commit_all(repo)
    (repo / "win.txt").write_bytes(b"one\r\nTWO\r\n")
    payload = git_diff(repo, "win.txt")
    assert (payload["additions"], payload["deletions"]) == (1, 1) == _numstat(repo, "--", "win.txt")
    assert "\r" not in payload["diff"]
    assert "-two\n+TWO\n" in payload["diff"]


def test_two_conflicted_files_count_like_each_file_alone(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    for name in ("h1.txt", "h2.txt"):
        (repo / name).write_text("base\n")
    _commit_all(repo)
    _git(repo, "checkout", "-q", "-b", "other")
    for name in ("h1.txt", "h2.txt"):
        (repo / name).write_text("theirs\n")
    _commit_all(repo, "other side")
    _git(repo, "checkout", "-q", "master")
    for name in ("h1.txt", "h2.txt"):
        (repo / name).write_text("ours\n")
    _commit_all(repo, "our side")
    merge = subprocess.run(
        ["git", "merge", "other"], cwd=str(repo), capture_output=True, text=True, timeout=20)
    assert merge.returncode != 0
    whole = _git(repo, "diff")
    assert whole.count("diff --cc ") == 2
    each = [_diff_stats(_git(repo, "diff", "--", name)) for name in ("h1.txt", "h2.txt")]
    assert _diff_stats(whole) == (sum(a for a, _ in each), sum(d for _, d in each))
    assert all(d == 0 for _, d in each)


def test_combined_diff_headers_reset_between_files():
    one = ("diff --cc h1\nindex 1,2..0\n--- a/h1\n+++ b/h1\n@@@ -1,1 -1,1 +1,3 @@@\n"
           "++<<<<<<< HEAD\n +ours\n++>>>>>>> x\n")
    two = one.replace("h1", "h2")
    assert _diff_stats(one + two) == tuple(2 * n for n in _diff_stats(one))


@pytest.mark.parametrize("eol", [b"\n", b"\r\n"])
def test_too_large_diff_counts_the_whole_patch(tmp_path, eol):
    repo = _init_repo(tmp_path / "repo")
    _git(repo, "config", "core.autocrlf", "false")
    rows = 20000
    (repo / "big.txt").write_bytes(b"".join(b"old row %05d" % i + eol for i in range(rows)))
    _commit_all(repo)
    (repo / "big.txt").write_bytes(b"".join(b"new row %05d" % i + eol for i in range(rows)))
    payload = git_diff(repo, "big.txt")
    assert payload["too_large"] is True
    assert len(payload["diff"]) == DIFF_SIZE_LIMIT
    assert (payload["additions"], payload["deletions"]) == (rows, rows) == _numstat(repo, "--", "big.txt")


@pytest.mark.parametrize("text", ["", "\n", "a", "a\n", "\n\na\n\nb", "x\r\ny\u2028z\n-q", "\n" * 5 + "tail"])
def test_lf_record_iterator_matches_split(text):
    assert list(_iter_lf_records(text)) == text.split("\n")


def test_lf_record_iterator_is_lazy():
    records = _iter_lf_records("first\n" + "x\n" * 1000)
    assert next(records) == "first"
    assert not isinstance(records, list)
