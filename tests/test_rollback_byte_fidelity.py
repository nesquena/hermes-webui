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
import subprocess
import sys

import pytest

from api import rollback


def _pathname_bytes(name):
    return name.encode(sys.getfilesystemencoding(), errors="surrogateescape")


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
    modes = rollback._checkpoint_entries(rollback._find_git(), ref)

    assert len(modes) == 1, modes
    rel_path = next(iter(modes))
    assert _pathname_bytes(rel_path) == raw_name, repr(_pathname_bytes(rel_path))
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
    modes = rollback._checkpoint_entries(git, ref)
    rel_path = next(iter(modes))

    blob = rollback._read_checkpoint_blob_for(git, ref, rel_path, modes[rel_path][1])
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
    assert _pathname_bytes(diff["files_changed"][0]["file"]) == raw_name


# The reviewer's six-case probe: LF and tab are the controls that passed before
# the repair; CR and 0xFF are the shapes that lost their name. All four go
# through `ls-tree -z`, so all four must round-trip.
PATHNAME_CASES = [
    ("lf", b"plain-name.txt"),
    ("tab", b"with\ttab.txt"),
    ("cr", b"with\rcarriage.txt"),
    ("byte-ff", b"with-\xff-byte.txt"),
]


@pytest.mark.parametrize("label,raw_name", PATHNAME_CASES, ids=[c[0] for c in PATHNAME_CASES])
def test_listing_recovers_every_pathname_shape(monkeypatch, tmp_path, label, raw_name):
    """`_checkpoint_entries` must return the exact bytes for each shape."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    checkpoint_root = tmp_path / "checkpoints"
    head = _init_shared_store(checkpoint_root, workspace.resolve(), {raw_name: b"x\n"})

    monkeypatch.setattr(rollback, "_resolve_workspace", lambda arg: str(workspace.resolve()))
    monkeypatch.setattr(rollback, "_checkpoint_root", lambda: checkpoint_root)

    ref = rollback._resolve_checkpoint_ref(str(workspace.resolve()), head)
    assert ref is not None
    modes = rollback._checkpoint_entries(rollback._find_git(), ref)

    assert len(modes) == 1, f"{label}: {modes}"
    assert _pathname_bytes(next(iter(modes))) == raw_name, label


@pytest.mark.parametrize("label,raw_name", PATHNAME_CASES, ids=[c[0] for c in PATHNAME_CASES])
def test_edited_file_is_reported_under_its_exact_name(monkeypatch, tmp_path, label, raw_name):
    """A checkpoint-only path must reach `files_changed` under its exact bytes.

    This is the reviewer's symptom: a name that loses bytes or fails to decode
    never appears in the diff at all.
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    checkpoint_root = tmp_path / "checkpoints"
    head = _init_shared_store(checkpoint_root, workspace.resolve(), {raw_name: b"before\n"})

    monkeypatch.setattr(rollback, "_resolve_workspace", lambda arg: str(workspace.resolve()))
    monkeypatch.setattr(rollback, "_checkpoint_root", lambda: checkpoint_root)

    diff = rollback.get_checkpoint_diff(str(workspace), head)

    assert diff["total_changes"] == 1, f"{label}: {diff}"
    assert _pathname_bytes(diff["files_changed"][0]["file"]) == raw_name, label



def test_undecodable_name_does_not_abort_the_listing(monkeypatch, tmp_path):
    """A name the host cannot decode must degrade, not fail the whole snapshot.

    Windows documents `os.fsdecode` as using the **strict** handler under `mbcs`,
    so a 0xE9 byte raises `UnicodeDecodeError`. If that escaped
    `_checkpoint_entries()` it would take every other file's diff and
    restore with it. Simulated here by forcing the strict path.
    """
    import os as _os

    real_fsdecode = _os.fsdecode
    bad_name = b"caf\xe9.txt"

    def strict_fsdecode(path):
        if isinstance(path, bytes) and path == bad_name:
            raise UnicodeDecodeError("utf-8", path, 3, 4, "invalid continuation byte")
        return real_fsdecode(path)

    monkeypatch.setattr(rollback.os, "fsdecode", strict_fsdecode)

    assert rollback._decode_git_path(bad_name) is not None
    # The good sibling still decodes normally in the same run.
    assert rollback._decode_git_path(b"plain.txt") == "plain.txt"


def test_restore_reports_unreadable_blob_and_restores_sibling(monkeypatch, tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    root = tmp_path / "checkpoints"
    head = _init_shared_store(root, workspace.resolve(), {b"missing": b"lost", b"good": b"ok"})
    monkeypatch.setattr(rollback, "_resolve_workspace", lambda _: str(workspace.resolve()))
    monkeypatch.setattr(rollback, "_checkpoint_root", lambda: root)
    store = root / "store"
    oid = subprocess.check_output(["git", "--git-dir", str(store), "rev-parse", head + ":missing"],
                                  text=True).strip()
    (store / "objects" / oid[:2] / oid[2:]).unlink()
    result = rollback.restore_checkpoint(str(workspace), head)
    assert result["errors"] == [{"file": "missing", "error": "Could not read checkpoint blob"}]
    assert result["files_restored"] == ["good"]
    assert (workspace / "good").read_bytes() == b"ok"


def test_restore_reports_windows_filename_error_and_restores_sibling(monkeypatch, tmp_path):
    from unittest.mock import Mock

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    root = tmp_path / "checkpoints"
    bad = b"bad-\xe9"
    head = _init_shared_store(root, workspace.resolve(), {bad: b"bad", b"good": b"ok"})
    monkeypatch.setattr(rollback, "_resolve_workspace", lambda _: str(workspace.resolve()))
    monkeypatch.setattr(rollback, "_checkpoint_root", lambda: root)
    # Model Windows filesystem decoding without changing pathlib or subprocess.
    windows_os = Mock(wraps=rollback.os)
    windows_os.name = "nt"
    windows_os.fsdecode.side_effect = lambda raw: raw.decode("utf-8", "surrogatepass")
    monkeypatch.setattr(rollback, "os", windows_os)

    result = rollback.restore_checkpoint(str(workspace), head)
    assert result["files_restored"] == ["good"]
    assert (workspace / "good").read_bytes() == b"ok"
    assert len(result["errors"]) == 1
    assert _pathname_bytes(result["errors"][0]["file"]) == bad
    assert "cannot be represented" in result["errors"][0]["error"]
