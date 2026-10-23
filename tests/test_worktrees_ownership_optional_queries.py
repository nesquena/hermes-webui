"""Regressions for the maintainer's review 5565050910 on #8162.

1. A *dirty* ordinary primary checkout (modified / staged / untracked-child
   under a committed directory) must still be recovered as the main root for a
   linked-worktree input. The previous head required a clean `git status`.
2. A failed *optional* `ls-files` query must not be read as ownership: it must
   fall back to the caller's own checkout, never select the storage dir as the
   project root.
3. An optional `status` OSError/timeout must not escape `create_worktree_for_workspace`.
"""

import subprocess
from pathlib import Path

import pytest


def _git(*args, cwd=None, check=True):
    result = subprocess.run(
        ["git", *args],
        cwd=str(cwd) if cwd else None,
        text=True,
        capture_output=True,
        check=False,
    )
    if check and result.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {result.stderr}")
    return result


@pytest.fixture()
def git_env(monkeypatch, tmp_path):
    monkeypatch.setenv("GIT_AUTHOR_NAME", "t")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "t@example.com")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "t")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "t@example.com")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig-none"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    return tmp_path


def _import():
    from api.worktrees import find_main_worktree_root

    return find_main_worktree_root


def _stub_agent_setup(monkeypatch, created_root):
    import api.worktrees as worktrees

    seen = {}

    def fake(repo_root):
        seen["repo_root"] = repo_root
        tree = Path(repo_root) / ".worktrees" / "hermes-test"
        tree.mkdir(parents=True, exist_ok=True)
        created_root.append(tree)
        return {"path": str(tree), "branch": "hermes-test", "repo_root": str(repo_root)}

    monkeypatch.setattr(worktrees, "_setup_agent_worktree", fake)
    return seen


def _ordinary_primary(base):
    """Ordinary primary with a committed file plus a committed `src/` dir."""
    primary = base / "primary"
    primary.mkdir(parents=True)
    _git("init", "-q", "-b", "main", cwd=primary)
    (primary / "tracked.txt").write_text("committed\n", encoding="utf-8")
    (primary / "src").mkdir()
    (primary / "src" / "mod.py").write_text("x = 1\n", encoding="utf-8")
    _git("add", "tracked.txt", "src/mod.py", cwd=primary)
    _git("commit", "-q", "-m", "init", cwd=primary)
    linked = base / "linked"
    _git("worktree", "add", "-q", "-b", "side", str(linked), cwd=primary)
    nested = linked / "nested"
    nested.mkdir()
    return primary, linked, nested


def _dirty(primary, kind):
    if kind == "modified":
        (primary / "tracked.txt").write_text("modified content\n", encoding="utf-8")
    elif kind == "staged":
        (primary / "tracked.txt").write_text("staged content\n", encoding="utf-8")
        _git("add", "tracked.txt", cwd=primary)
    elif kind == "untracked-in-dir":
        (primary / "src" / "new_untracked.py").write_text("y = 2\n", encoding="utf-8")
    else:  # pragma: no cover
        raise AssertionError(kind)


@pytest.mark.parametrize("kind", ["modified", "staged", "untracked-in-dir"])
def test_dirty_ordinary_primary_still_unnests_to_primary(git_env, monkeypatch, kind):
    """A working tree owns its repository whether or not it is clean."""
    primary, linked, nested = _ordinary_primary(git_env / f"dirty-{kind}")
    _dirty(primary, kind)

    find = _import()
    for workspace in (linked, nested):
        assert find(workspace) == primary.resolve(), kind

    created = []
    seen = _stub_agent_setup(monkeypatch, created)
    from api.worktrees import create_worktree_for_workspace

    info = create_worktree_for_workspace(nested)
    assert Path(seen["repo_root"]).resolve() == primary.resolve(), kind
    assert Path(info["path"]).resolve().is_relative_to(primary.resolve())
    assert not Path(info["path"]).resolve().is_relative_to(linked.resolve())


def test_ownership_rule_consults_no_index_and_no_path_presence(git_env, monkeypatch):
    """Pin the rule itself: layout and claims, never content, presence or index.

    A working tree owns its repository whether its files match the index or not,
    so the rule must not consult the index, ``git status`` or per-path existence:
    an edited, staged, ``touch``-ed or partially deleted checkout stays the tree.
    The discrimination against a ``--separate-git-dir`` storage dir is done one
    layer up, by the gitfile claim that a storage dir cannot make, so no ownership
    question ever reaches an optional Git query.

    A failed ``ls-files`` is therefore not merely conservative — it is not asked.
    """
    import api.worktrees as worktrees

    primary = git_env / "rule" / "primary"
    primary.mkdir(parents=True)
    (primary / "src").mkdir()
    _git("init", "-q", "-b", "main", cwd=primary)
    (primary / "tracked.txt").write_text("committed\n", encoding="utf-8")
    (primary / "src" / "second.txt").write_text("committed\n", encoding="utf-8")
    _git("add", "tracked.txt", "src/second.txt", cwd=primary)
    _git("commit", "-q", "-m", "init", cwd=primary)
    linked = primary.parent / "linked"
    _git("worktree", "add", "-q", "-b", "side", str(linked), cwd=primary)

    # Uncommitted states that must not change the answer: edited, staged, and a
    # deleted nested leaf (the ordinary-primary regression).
    (primary / "tracked.txt").write_text("edited\n", encoding="utf-8")
    (primary / "src" / "second.txt").unlink()

    seen: list[list[str]] = []
    real_run = subprocess.run

    def recording_run(cmd, *a, **kw):
        if isinstance(cmd, (list, tuple)):
            seen.append([str(part) for part in cmd])
        return real_run(cmd, *a, **kw)

    monkeypatch.setattr(worktrees.subprocess, "run", recording_run)

    assert worktrees.find_main_worktree_root(linked) == primary.resolve()
    queried = [cmd for cmd in seen if any("ls-files" in part for part in cmd)]
    assert not queried, f"ownership must not read the index: {queried}"


def _distant_separate(base):
    """Distant primary with a gitfile, separate storage, linked input nested."""
    checkout = base.joinpath("elsewhere", "deep", "more", "further", "actual-checkout")
    storage = base / "storage-only"
    checkout.mkdir(parents=True)
    storage.mkdir(parents=True)
    _git("init", "-q", "-b", "main", f"--separate-git-dir={storage / '.git'}", cwd=checkout)
    (checkout / "sentinel.txt").write_text("keep me\n", encoding="utf-8")
    _git("add", "sentinel.txt", cwd=checkout)
    _git("commit", "-q", "-m", "init", cwd=checkout)
    linked = base / "linked-wt"
    _git("worktree", "add", "-q", "-b", "side", str(linked), cwd=checkout)
    nested = linked / "nested"
    nested.mkdir()
    return checkout, storage, linked, nested


@pytest.mark.parametrize("failure", ["oserror", "returncode", "timeout"])
def test_failed_ls_files_query_never_selects_storage(git_env, monkeypatch, failure):
    """A failed optional `ls-files` is unknown, not ownership: keep our checkout."""
    import api.worktrees as worktrees

    checkout, storage, linked, nested = _distant_separate(git_env / f"lsfail-{failure}")
    real_run = subprocess.run

    def fake_run(cmd, *a, **kw):
        if isinstance(cmd, (list, tuple)) and "ls-files" in cmd:
            if failure == "oserror":
                raise OSError("injected: index unreadable")
            if failure == "timeout":
                raise subprocess.TimeoutExpired(cmd="git", timeout=5)
            return subprocess.CompletedProcess(args=list(cmd), returncode=128, stdout="", stderr="boom")
        return real_run(cmd, *a, **kw)

    monkeypatch.setattr(worktrees.subprocess, "run", fake_run)

    created = []
    seen = _stub_agent_setup(monkeypatch, created)
    from api.worktrees import create_worktree_for_workspace

    info = create_worktree_for_workspace(nested)
    root = Path(seen["repo_root"]).resolve()
    assert root != storage.resolve(), f"{failure}: storage was handed to the Agent"
    # A failed optional query is unknown, never a claim on storage. What remains
    # is decided by recorded facts: here the primary's gitfile still claims the
    # metadata, so the real checkout is recovered — otherwise the caller keeps
    # its own checkout. Either answer is acceptable; storage is not.
    assert root in (checkout.resolve(), linked.resolve()), failure
    assert Path(info["path"]).resolve().is_relative_to(root)
    assert not Path(info["path"]).resolve().is_relative_to(storage.resolve())


def test_optional_status_failure_does_not_abort_creation(git_env, monkeypatch):
    """No `status` query may raise out of create_worktree_for_workspace."""
    import api.worktrees as worktrees

    primary, linked, nested = _ordinary_primary(git_env / "statusfail")
    real_run = subprocess.run

    def fake_run(cmd, *a, **kw):
        if isinstance(cmd, (list, tuple)) and "status" in cmd:
            raise OSError("injected: status unavailable")
        return real_run(cmd, *a, **kw)

    monkeypatch.setattr(worktrees.subprocess, "run", fake_run)

    created = []
    seen = _stub_agent_setup(monkeypatch, created)
    from api.worktrees import create_worktree_for_workspace

    info = create_worktree_for_workspace(nested)  # must not raise
    assert Path(seen["repo_root"]).resolve() == primary.resolve()
    assert Path(info["path"]).resolve().is_relative_to(primary.resolve())


# --- re-gate 6d508a948: two ownership problems -------------------------------------
#
# 1. The cleanliness adjudicator was applied to EVERY claimant, although it only
#    describes the ambiguous pair (claimant below the plain candidate). An
#    ordinary external separate checkout lost its claim whenever the primary
#    carried any modified/staged/deleted state, so allocation nested under the
#    linked checkout instead of the real one.
# 2. A failed `status` probe returned `None` from `_holds_its_committed_tree`,
#    and the caller read unknown as "the claimant wins" -- reauthorizing a
#    stale alias the moment git status misbehaved.


def _external_separate(base):
    """`base/checkout/.git -> base/storage-only/.git`, linked worktree at base/linked.

    Two committed files so the "deleted" variant can remove a file *different*
    from the modified one, exactly as the re-gate described.
    """
    storage = base / "storage-only"
    checkout = base / "checkout"
    storage.mkdir(parents=True)
    checkout.mkdir(parents=True)
    _git("init", "-q", "-b", "main", f"--separate-git-dir={storage / '.git'}", cwd=checkout)
    for name in ("tracked.txt", "second.txt"):
        (checkout / name).write_text(f"committed {name}\n", encoding="utf-8")
        _git("add", name, cwd=checkout)
    _git("commit", "-q", "-m", "init", cwd=checkout)
    linked = base / "linked"
    _git("worktree", "add", "-q", "-b", "side", str(linked), cwd=checkout)
    nested = linked / "nested"
    nested.mkdir()
    return checkout, storage, linked, nested


def _make_dirty(checkout, kind):
    if kind == "modified":
        (checkout / "tracked.txt").write_text("modified content\n", encoding="utf-8")
    elif kind == "staged":
        (checkout / "tracked.txt").write_text("staged content\n", encoding="utf-8")
        _git("add", "tracked.txt", cwd=checkout)
    elif kind == "deleted":
        (checkout / "second.txt").unlink()
    else:  # pragma: no cover
        raise AssertionError(kind)


@pytest.mark.parametrize("kind", ["modified", "staged", "deleted"])
def test_external_separate_checkout_keeps_ownership_while_primary_is_dirty(
    git_env, monkeypatch, kind
):
    """Ordinary working-tree state is not evidence the checkout stopped owning it."""
    from api.worktrees import create_worktree_for_workspace

    checkout, storage, linked, nested = _external_separate(git_env / f"dirty-ext-{kind}")
    _make_dirty(checkout, kind)

    created = []
    seen = _stub_agent_setup(monkeypatch, created)
    info = create_worktree_for_workspace(nested)

    root = Path(seen["repo_root"]).resolve()
    assert root == checkout.resolve(), (
        f"{kind}: the real checkout still owns the repository; the cleanliness "
        f"probe answered for a pair that is not ambiguous at all"
    )
    assert root != storage.resolve()
    assert Path(info["path"]).resolve().is_relative_to(checkout.resolve())
    assert not Path(info["path"]).resolve().is_relative_to(linked.resolve()), (
        f"{kind}: allocation must not nest under the linked checkout"
    )


def test_external_separate_checkout_adjudicator_is_scoped_to_the_ambiguous_pair(monkeypatch):
    """The unit rule: a claimant outside the plain candidate is never adjudicated."""
    import api.worktrees as worktrees

    storage = Path("/tmp/base/storage-only")
    external = Path("/tmp/base/checkout")
    ambiguous = storage / "node_modules" / "pkg" / "checkout"

    calls = []

    def _boom(*a, **kw):  # pragma: no cover - must never be consulted
        calls.append(a)
        return True

    monkeypatch.setattr(worktrees, "_holds_its_committed_tree", _boom)
    monkeypatch.setattr(worktrees, "_plain_git_dir_owner", lambda *a, **kw: True)
    monkeypatch.setattr(worktrees, "_as_dir", lambda p: Path(p))

    assert worktrees._prefer_checkout_over_plain(external, storage / ".git") == external
    assert calls == [], "cleanliness was consulted for an unambiguous external checkout"

    # The ambiguous pair still consults it.
    worktrees._prefer_checkout_over_plain(ambiguous, storage / ".git")
    assert calls, "the ambiguous pair must still be adjudicated by git"


def _stale_alias(base):
    """`primary/new/deep/.git -> primary/.git`: a stale alias inside the primary."""
    primary = base / "primary"
    primary.mkdir(parents=True)
    _git("init", "-q", "-b", "main", cwd=primary)
    for name in ("tracked.txt", "second.txt"):
        (primary / name).write_text(f"committed {name}\n", encoding="utf-8")
    _git("add", "tracked.txt", "second.txt", cwd=primary)
    _git("commit", "-q", "-m", "init", cwd=primary)
    linked = base / "linked"
    _git("worktree", "add", "-q", "-b", "side", str(linked), cwd=primary)
    deep = primary / "new" / "deep"
    deep.mkdir(parents=True)
    (deep / ".git").write_text(f"gitdir: {(primary / '.git').resolve()}\n", encoding="utf-8")
    nested = linked / "nested"
    nested.mkdir()
    return primary, linked, deep, nested


@pytest.mark.parametrize("failure", ["oserror", "timeout", "nonzero"])
def test_unknown_status_probe_never_authorizes_the_stale_alias(
    git_env, monkeypatch, failure
):
    """Unknown is not an affirmative ownership answer: keep our own checkout."""
    import api.worktrees as worktrees
    from api.worktrees import create_worktree_for_workspace

    primary, linked, deep, nested = _stale_alias(git_env / f"stalestat-{failure}")
    real_run = worktrees._run_git

    def fake_run(args, cwd, timeout=2):
        if list(args[:1]) == ["status"] and "--untracked-files=no" in args:
            if failure == "oserror":
                raise OSError("injected: status unavailable")
            if failure == "timeout":
                raise subprocess.TimeoutExpired(cmd="git", timeout=5)
            return subprocess.CompletedProcess(
                args=["git", *args], returncode=1, stdout="", stderr="boom"
            )
        return real_run(args, cwd, timeout=timeout)

    monkeypatch.setattr(worktrees, "_run_git", fake_run)

    created = []
    seen = _stub_agent_setup(monkeypatch, created)
    info = create_worktree_for_workspace(nested)

    root = Path(seen["repo_root"]).resolve()
    assert root != deep.resolve(), (
        f"{failure}: an unproved stale alias was handed to the Agent just "
        f"because the status probe failed"
    )
    assert not Path(info["path"]).resolve().is_relative_to(deep.resolve())
    assert root in (primary.resolve(), linked.resolve()), failure
