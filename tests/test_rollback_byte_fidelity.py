"""Byte fidelity for the shared-store reader's pathnames.

A checkpoint records the workspace as it was on disk. Git tracks pathnames as
**bytes** — only NUL and ``/`` are forbidden — so a repository can legitimately
hold a name that is not valid UTF-8.

The shared-store reader lists paths with ``git ls-tree -z``. Reading that output
with ``text=True`` decodes it as UTF-8 and replaces the invalid bytes with
U+FFFD, so the name can no longer be handed back to git to read the blob: the
diff and restore silently skip the file, or fail on it.

Only the pathname side is pinned here. Blob *content* is already read as bytes
(``capture_output=True`` without ``text=True``), and the workspace side opens the
file with ``os.fdopen(fd, "rb")``, so ``\\r`` survives on both — asserting that
would be a test that passes with or without the change.
"""

import hashlib
import os
import subprocess

from api import rollback


def _init_shared_store(checkpoint_root, workspace, files):
    """Create ``<root>/store`` with one commit holding *files*.

    *files* maps path (bytes) → content (bytes) so a test can express a name that
    is not valid UTF-8.
    """
    store = checkpoint_root / "store"
    store.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "--bare", str(store)], check=True)
    subprocess.run(["git", "--git-dir", str(store), "config", "user.email", "t@example.com"], check=True)
    subprocess.run(["git", "--git-dir", str(store), "config", "user.name", "T"], check=True)

    entries = []
    for raw_path, content in files.items():
        blob = subprocess.run(
            ["git", "--git-dir", str(store), "hash-object", "-w", "--stdin"],
            input=content, capture_output=True, check=True,
        ).stdout.strip()
        entries.append(b"100644 blob " + blob + b"\t" + raw_path + b"\n")
    tree_sha = subprocess.run(
        ["git", "--git-dir", str(store), "mktree"],
        input=b"".join(entries), capture_output=True, check=True,
    ).stdout.strip()
    commit = subprocess.run(
        ["git", "--git-dir", str(store), "commit-tree", tree_sha, "-m", "cp"],
        capture_output=True, check=True,
    ).stdout.strip()

    project_hash = hashlib.sha256(str(workspace).encode()).hexdigest()[:16]
    subprocess.run(
        ["git", "--git-dir", str(store), "update-ref", f"refs/hermes/{project_hash}", commit],
        check=True,
    )
    return commit.decode()


def test_non_utf8_filename_is_listed_losslessly(monkeypatch, tmp_path):
    """A latin-1 filename must round-trip to its exact bytes, not U+FFFD."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    checkpoint_root = tmp_path / "checkpoints"
    # 0xE9 is not valid UTF-8 on its own; git permits it in a pathname.
    raw_name = b"caf\xe9.txt"
    head = _init_shared_store(checkpoint_root, workspace.resolve(), {raw_name: b"hello\n"})

    monkeypatch.setattr(rollback, "_resolve_workspace", lambda arg: str(workspace.resolve()))
    monkeypatch.setattr(rollback, "_checkpoint_root", lambda: checkpoint_root)

    ref = rollback._resolve_checkpoint_ref(str(workspace.resolve()), head)
    assert ref is not None
    modes = rollback._checkpoint_entry_modes_for(rollback._find_git(), ref)

    assert len(modes) == 1, modes
    rel_path = next(iter(modes))
    assert os.fsencode(rel_path) == raw_name, repr(os.fsencode(rel_path))
    assert "\ufffd" not in rel_path, "the name was lossily decoded"


def test_non_utf8_filename_blob_is_readable(monkeypatch, tmp_path):
    """The decoded name must still address the blob through git."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    checkpoint_root = tmp_path / "checkpoints"
    head = _init_shared_store(
        checkpoint_root, workspace.resolve(), {b"caf\xe9.txt": b"payload\n"},
    )

    monkeypatch.setattr(rollback, "_resolve_workspace", lambda arg: str(workspace.resolve()))
    monkeypatch.setattr(rollback, "_checkpoint_root", lambda: checkpoint_root)

    ref = rollback._resolve_checkpoint_ref(str(workspace.resolve()), head)
    assert ref is not None
    git = rollback._find_git()
    modes = rollback._checkpoint_entry_modes_for(git, ref)
    rel_path = next(iter(modes))

    blob = rollback._read_checkpoint_blob_for(git, ref, rel_path)
    assert blob == b"payload\n"


def test_non_utf8_filename_appears_in_diff(monkeypatch, tmp_path):
    """The diff must see the file at all — a mangled name means it is skipped.

    The workspace side is left without the file (a checkpoint-only path), which
    is the "deleted" case: it only reaches the diff if the reader recovered the
    name. Creating a non-UTF-8 filename on disk is not required for that, and on
    APFS it is refused outright (Errno 92), so the test does not depend on it.
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    checkpoint_root = tmp_path / "checkpoints"
    raw_name = b"caf\xe9.txt"
    head = _init_shared_store(checkpoint_root, workspace.resolve(), {raw_name: b"before\n"})

    monkeypatch.setattr(rollback, "_resolve_workspace", lambda arg: str(workspace.resolve()))
    monkeypatch.setattr(rollback, "_checkpoint_root", lambda: checkpoint_root)

    diff = rollback.get_checkpoint_diff(str(workspace), head)

    assert diff["total_changes"] == 1, diff
    assert diff["files_changed"][0]["status"] == "deleted"
    assert os.fsencode(diff["files_changed"][0]["file"]) == raw_name
