"""The rollback API must read the agent's current shared-store layout.

The agent's CheckpointManager keeps one shared shadow store
(``<hermes_home>/checkpoints/store``) with a ``refs/hermes/<sha256[:16]>`` ref
per project. A pre-v2 layout kept a separate repository per commit under
``<hermes_home>/checkpoints/<sha256[:12]>/<commit>/``.

These tests pin the shared-store layout, because that is what the agent writes
today: reading only the legacy layout makes the checkpoints panel permanently
empty even though the agent has snapshots for the workspace.
"""

import hashlib
import subprocess

from api import rollback


def _init_shared_store(checkpoint_root, workspace, commits):
    """Create ``<root>/store`` with one ref holding *commits* (oldest first).

    Returns ``(ref, commit_ids)`` with the ids in the same order as *commits*, so
    a test can name a specific commit instead of assuming which one is the tip.
    """
    store = checkpoint_root / "store"
    store.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "--bare", str(store)], check=True)
    subprocess.run(["git", "--git-dir", str(store), "config", "user.email", "t@example.com"], check=True)
    subprocess.run(["git", "--git-dir", str(store), "config", "user.name", "T"], check=True)

    project_hash = hashlib.sha256(str(workspace).encode()).hexdigest()[:16]
    ref = f"refs/hermes/{project_hash}"
    commit_ids: list[str] = []
    parent: str | None = None
    for message, files in commits:
        tree_input = "".join(
            f"100644 blob {_blob(store, content)}\t{path}\n" for path, content in files.items()
        )
        tree_sha = subprocess.run(
            ["git", "--git-dir", str(store), "mktree"],
            input=tree_input, capture_output=True, text=True, check=True,
        ).stdout.strip()
        commit_args = ["git", "--git-dir", str(store), "commit-tree", tree_sha, "-m", message]
        if parent:
            commit_args.append("-p")
            commit_args.append(parent)
        parent = subprocess.run(
            commit_args, capture_output=True, text=True, check=True,
        ).stdout.strip()
        commit_ids.append(parent)
    assert parent is not None
    subprocess.run(
        ["git", "--git-dir", str(store), "update-ref", ref, parent], check=True,
    )
    return ref, commit_ids


def _blob(store, content):
    return subprocess.run(
        ["git", "--git-dir", str(store), "hash-object", "-w", "--stdin"],
        input=content, capture_output=True, text=True, check=True,
    ).stdout.strip()


def test_list_checkpoints_reads_shared_store(monkeypatch, tmp_path):
    """A shared-store ref must appear in the panel; the legacy dir does not exist."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    checkpoint_root = tmp_path / "checkpoints"
    _ref, heads = _init_shared_store(
        checkpoint_root, workspace.resolve(),
        [("before patch", {"AGENTS.md": "changed\n"})],
    )
    head = heads[0]

    monkeypatch.setattr(rollback, "_resolve_workspace", lambda arg: str(workspace.resolve()))
    monkeypatch.setattr(rollback, "_checkpoint_root", lambda: checkpoint_root)

    result = rollback.list_checkpoints(str(workspace))

    assert len(result["checkpoints"]) == 1, result
    entry = result["checkpoints"][0]
    assert entry["id"] == head
    assert entry["message"] == "before patch"
    assert entry["files"] == 1


def test_legacy_layout_still_listed(monkeypatch, tmp_path):
    """An old per-commit repository keeps working (no silent regression)."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    checkpoint_root = tmp_path / "checkpoints"
    ckpt_dir = checkpoint_root / rollback._workspace_hash(str(workspace.resolve())) / "abc123"
    ckpt_dir.mkdir(parents=True)
    subprocess.run(["git", "-C", str(ckpt_dir), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(ckpt_dir), "config", "user.email", "t@example.com"], check=True)
    subprocess.run(["git", "-C", str(ckpt_dir), "config", "user.name", "T"], check=True)
    (ckpt_dir / "AGENTS.md").write_text("legacy\n")
    subprocess.run(["git", "-C", str(ckpt_dir), "add", "."], check=True)
    subprocess.run(["git", "-C", str(ckpt_dir), "commit", "-q", "-m", "legacy checkpoint"], check=True)

    monkeypatch.setattr(rollback, "_resolve_workspace", lambda arg: str(workspace.resolve()))
    monkeypatch.setattr(rollback, "_checkpoint_root", lambda: checkpoint_root)

    result = rollback.list_checkpoints(str(workspace))

    assert len(result["checkpoints"]) == 1, result
    assert result["checkpoints"][0]["id"] == "abc123"


def test_diff_resolves_commit_in_shared_store(monkeypatch, tmp_path):
    """The diff must come from the named commit, not from whatever HEAD holds."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "AGENTS.md").write_text("current\n")
    checkpoint_root = tmp_path / "checkpoints"
    _ref, heads = _init_shared_store(
        checkpoint_root, workspace.resolve(),
        [("before patch", {"AGENTS.md": "original\n"})],
    )
    head = heads[0]

    monkeypatch.setattr(rollback, "_resolve_workspace", lambda arg: str(workspace.resolve()))
    monkeypatch.setattr(rollback, "_checkpoint_root", lambda: checkpoint_root)

    result = rollback.get_checkpoint_diff(str(workspace), head)

    assert result["total_changes"] == 1, result
    assert result["files_changed"] == [{"file": "AGENTS.md", "status": "modified"}]
    assert "-original" in result["diff"]
    assert "+current" in result["diff"]


def test_restore_resolves_commit_in_shared_store(monkeypatch, tmp_path):
    """Restoring must write the named commit's blob back into the workspace."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "AGENTS.md").write_text("current\n")
    checkpoint_root = tmp_path / "checkpoints"
    _ref, heads = _init_shared_store(
        checkpoint_root, workspace.resolve(),
        [("before patch", {"AGENTS.md": "original\n"})],
    )
    head = heads[0]

    monkeypatch.setattr(rollback, "_resolve_workspace", lambda arg: str(workspace.resolve()))
    monkeypatch.setattr(rollback, "_checkpoint_root", lambda: checkpoint_root)

    result = rollback.restore_checkpoint(str(workspace), head)

    assert result["ok"] is True
    assert result["files_restored"] == ["AGENTS.md"]
    assert (workspace / "AGENTS.md").read_text() == "original\n"


def test_unknown_commit_is_not_resolved(monkeypatch, tmp_path):
    """A commit that is not the project's current ref tip must not be served."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    checkpoint_root = tmp_path / "checkpoints"
    _init_shared_store(
        checkpoint_root, workspace.resolve(),
        [("before patch", {"AGENTS.md": "x\n"})],
    )

    monkeypatch.setattr(rollback, "_resolve_workspace", lambda arg: str(workspace.resolve()))
    monkeypatch.setattr(rollback, "_checkpoint_root", lambda: checkpoint_root)

    try:
        rollback.get_checkpoint_diff(str(workspace), "deadbeef")
    except ValueError as exc:
        assert "not found" in str(exc).lower()
    else:
        raise AssertionError("an unknown commit must not resolve")


def test_abbreviated_id_resolves_older_commit(monkeypatch, tmp_path):
    """The UI shows ``id[:12]``; an older snapshot must still resolve from it."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "AGENTS.md").write_text("newest\n")
    checkpoint_root = tmp_path / "checkpoints"
    _ref, commits = _init_shared_store(
        checkpoint_root, workspace.resolve(),
        [
            ("first", {"AGENTS.md": "oldest\n"}),
            ("second", {"AGENTS.md": "newest\n"}),
        ],
    )
    oldest = commits[0]

    monkeypatch.setattr(rollback, "_resolve_workspace", lambda arg: str(workspace.resolve()))
    monkeypatch.setattr(rollback, "_checkpoint_root", lambda: checkpoint_root)

    listed = rollback.list_checkpoints(str(workspace))
    assert len(listed["checkpoints"]) == 2, listed
    # Newest first, and the tip is not the commit we then resolve.
    assert listed["checkpoints"][0]["id"] != oldest

    result = rollback.get_checkpoint_diff(str(workspace), oldest[:12])

    assert result["total_changes"] == 1, result
    assert "-oldest" in result["diff"]


def test_match_commit_refuses_ambiguous_prefix(monkeypatch, tmp_path):
    """A prefix matching two commits must not silently pick one.

    The prefix is seven hex characters — long enough to pass
    ``_match_commit()``'s own length guard — so the comparison itself is what
    decides. A test using a one-character prefix would pass even if the code
    started returning the first match.
    """
    store = tmp_path / "store"
    store.mkdir()
    subprocess.run(["git", "init", "-q", "--bare", str(store)], check=True)

    shared = "abc1234"
    first, second = shared + "0" * 33, shared + "1" * 33
    monkeypatch.setattr(
        rollback, "_commit_ids",
        lambda git, git_dir, ref: [first, second],
    )

    ref = "refs/hermes/deadbeefdeadbeef"
    # Two candidates share the prefix → refuse.
    assert rollback._match_commit("git", store, ref, shared) is None
    # A longer prefix unique to one commit still resolves to it.
    assert rollback._match_commit("git", store, ref, shared + "0" * 10) == first
    assert rollback._match_commit("git", store, ref, shared + "1" * 10) == second


def test_packed_ref_is_still_listed(monkeypatch, tmp_path):
    """A checkpoint whose ref git packed must not disappear from the panel.

    ``git pack-refs`` (run by the agent's own pruning/GC) moves
    ``refs/hermes/<hash>`` into ``packed-refs`` and deletes the loose file, so a
    filesystem existence check reports a valid project as missing.
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    checkpoint_root = tmp_path / "checkpoints"
    _ref, heads = _init_shared_store(
        checkpoint_root, workspace.resolve(),
        [("before patch", {"AGENTS.md": "x\n"})],
    )
    store = checkpoint_root / "store"
    project_hash = hashlib.sha256(str(workspace.resolve()).encode()).hexdigest()[:16]
    loose = store / "refs" / "hermes" / project_hash
    assert loose.is_file(), "precondition: the ref starts out loose"

    subprocess.run(["git", "--git-dir", str(store), "pack-refs", "--all"], check=True)
    assert not loose.exists(), "precondition: pack-refs removed the loose ref"
    assert (store / "packed-refs").is_file()

    monkeypatch.setattr(rollback, "_resolve_workspace", lambda arg: str(workspace.resolve()))
    monkeypatch.setattr(rollback, "_checkpoint_root", lambda: checkpoint_root)

    listed = rollback.list_checkpoints(str(workspace))
    assert len(listed["checkpoints"]) == 1, listed

    diff = rollback.get_checkpoint_diff(str(workspace), heads[0][:12])
    assert diff["total_changes"] == 1, diff



def test_both_layouts_are_listed_after_upgrade(monkeypatch, tmp_path):
    """A workspace that predates the shared store keeps its old restore points.

    Upgrade scenario: legacy per-commit checkpoints already exist on disk, then the
    agent writes its first shared-store snapshot. Listing only the store would hide
    the older restore points from the panel even though they stay readable by id.
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    checkpoint_root = tmp_path / "checkpoints"

    # Legacy layout: one commit repository under <hash12>/<id>/
    legacy_dir = checkpoint_root / rollback._workspace_hash(str(workspace.resolve())) / "legacyid01"
    legacy_dir.mkdir(parents=True)
    subprocess.run(["git", "-C", str(legacy_dir), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(legacy_dir), "config", "user.email", "t@example.com"], check=True)
    subprocess.run(["git", "-C", str(legacy_dir), "config", "user.name", "T"], check=True)
    (legacy_dir / "AGENTS.md").write_text("legacy\n")
    subprocess.run(["git", "-C", str(legacy_dir), "add", "."], check=True)
    subprocess.run(["git", "-C", str(legacy_dir), "commit", "-q", "-m", "legacy checkpoint"], check=True)

    # Shared store: a newer snapshot for the same workspace.
    _ref, heads = _init_shared_store(
        checkpoint_root, workspace.resolve(), [("new checkpoint", {"AGENTS.md": "new\n"})],
    )

    monkeypatch.setattr(rollback, "_resolve_workspace", lambda arg: str(workspace.resolve()))
    monkeypatch.setattr(rollback, "_checkpoint_root", lambda: checkpoint_root)

    listed = rollback.list_checkpoints(str(workspace))
    ids = [c["id"] for c in listed["checkpoints"]]

    assert "legacyid01" in ids, listed
    assert heads[0] in ids, listed
    assert len(ids) == 2, listed

    # Both stay readable through their own layout.
    assert rollback.get_checkpoint_diff(str(workspace), "legacyid01")["total_changes"] >= 0
    assert rollback.get_checkpoint_diff(str(workspace), heads[0][:12])["total_changes"] >= 0
