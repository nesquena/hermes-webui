"""#8500 — an unreadable profile sibling must fail closed, not be skipped.

`_legacy_profile_homes` enumerated `<base>/profiles`, but when
`entry.is_dir()` raised `OSError` it dropped the entry. A profiles directory can
allow listing names yet deny the search needed to stat them, so a live legacy
update in that sibling stayed invisible and the recovery restart proceeded on
missing proof. AGENTS.md: "fail closed when safety cannot be confirmed: unknown
is not allowed."
"""
from __future__ import annotations

import errno
import os
import time
from pathlib import Path

import pytest

from api import agent_runtime


class _UnstatableEntry:
    """A `Path`-like entry whose `is_dir()` always raises `OSError`.

    This is exactly the "we can list a name but cannot stat it" case: the
    answer is *denied*, not "no".
    """

    def __init__(self, name: str):
        self.name = name
        self._path = Path("/nonexistent") / name

    def __fspath__(self) -> str:
        return str(self._path)

    def __str__(self) -> str:
        return str(self._path)

    def is_dir(self) -> bool:
        raise OSError(errno.EACCES, "cannot stat", str(self._path))


class _ProfilesDir:
    """A profiles tree that lists a name but refuses to answer `is_dir()`."""

    def __init__(self, entries):
        self._entries = entries

    def iterdir(self):
        return iter(self._entries)


def _ghost(monkeypatch, unstatable, base: Path):
    monkeypatch.setattr(
        Path,
        "iterdir",
        lambda self: iter([unstatable]) if self == base / "profiles" else iter([]),
    )


def test_an_unstatable_profile_sibling_stays_in_the_home_set(tmp_path, monkeypatch):
    base = tmp_path / "base"
    base.mkdir()
    ghost = _UnstatableEntry("ghost")
    _ghost(monkeypatch, ghost, base)

    homes = agent_runtime._legacy_profile_homes(base)

    names = {os.path.basename(str(entry)) for entry in homes}
    assert "ghost" in names, (
        "an entry whose is_dir() raised OSError was dropped instead of failing "
        f"closed: {homes}"
    )


def test_an_unstatable_sibling_reaches_the_marker_home_set(tmp_path, monkeypatch):
    """The fail-closed entry must survive into the marker homes, or the gate
    that refuses the restart never sees it."""
    base = tmp_path / "base"
    base.mkdir()
    ghost = _UnstatableEntry("ghost")
    monkeypatch.setattr(
        agent_runtime,
        "_base_marker_home",
        lambda: base,
    )
    _ghost(monkeypatch, ghost, base)

    homes = agent_runtime._agent_update_marker_homes()

    names = {os.path.basename(os.path.normpath(str(entry))) for entry in homes}
    assert "ghost" in names, (
        "the unstatable sibling was lost between enumeration and the final home "
        f"set: {names}"
    )


def test_a_plain_file_entry_is_still_skipped(tmp_path):
    """Control: a real file is not a profile home and must not linger."""
    base = tmp_path / "base"
    profiles = base / "profiles"
    profiles.mkdir(parents=True)
    (profiles / "not-a-profile.txt").write_text("x", encoding="utf-8")

    homes = agent_runtime._legacy_profile_homes(base)

    assert homes == ()


_ROOT_USER = hasattr(os, "geteuid") and os.geteuid() == 0

# The mode-bit tests below rely on POSIX permission semantics: 0o300 must make
# the directory searchable but NOT listable, so ``iterdir()`` is denied. On
# native Windows ``os.chmod`` only toggles the read-only attribute, so listing
# still succeeds and the tree is never "unverifiable" — the assertion would
# fail for a platform reason, not because the guard is broken. Skip there.
# The root check stays: root bypasses the mode bits everywhere, so CI running
# as root would see a listable tree as well.
_POSIX_PERMISSION_BITS = os.name == "posix"
_PERMISSION_BITS_UNUSABLE = _ROOT_USER or not _POSIX_PERMISSION_BITS


def _unlistable_tree(base: Path, *, sibling: str = "sibling") -> Path:
    """``<base>/profiles`` mode 0o300 with a live marker in a named sibling.

    0o300 is searchable but not readable: the names cannot be listed, yet
    ``profiles/<sibling>/.hermes-update-in-progress`` is still reachable by
    name — the shape that used to read as "no update".
    """
    profiles = base / "profiles"
    home = profiles / sibling
    home.mkdir(parents=True, exist_ok=True)
    (home / ".hermes-update-in-progress").write_text(
        f"{os.getpid()}\n{time.time()}\n", encoding="utf-8"
    )
    os.chmod(profiles, 0o300)
    return profiles


@pytest.mark.skipif(
    _PERMISSION_BITS_UNUSABLE,
    reason="0o300 permission bits are POSIX-only (and root ignores them)",
)
def test_a_searchable_but_unlistable_profile_tree_is_named_not_invented(tmp_path):
    """The tree itself must not stand in for the homes it hides: its own
    marker is absent, and that absent read authorised the restart."""
    base = tmp_path / "base"
    base.mkdir()
    profiles = _unlistable_tree(base)
    try:
        with pytest.raises(agent_runtime._ProfileTreeUnverifiable):
            agent_runtime._legacy_profile_homes(base)
    finally:
        os.chmod(profiles, 0o700)


@pytest.mark.skipif(
    _PERMISSION_BITS_UNUSABLE,
    reason="0o300 permission bits are POSIX-only (and root ignores them)",
)
def test_a_searchable_but_unlistable_profile_tree_reads_unknown(tmp_path, monkeypatch):
    base = tmp_path / "base"
    base.mkdir()
    profiles = _unlistable_tree(base)
    monkeypatch.setattr(agent_runtime, "_base_marker_home", lambda: base)
    try:
        assert agent_runtime._agent_update_transaction_state() == "unknown"
    finally:
        os.chmod(profiles, 0o700)


def test_a_regular_file_at_profiles_reads_unknown(tmp_path, monkeypatch):
    """Control: a non-directory tree cannot be enumerated either."""
    base = tmp_path / "base"
    base.mkdir()
    (base / "profiles").write_text("not a tree", encoding="utf-8")
    monkeypatch.setattr(agent_runtime, "_base_marker_home", lambda: base)
    assert agent_runtime._agent_update_transaction_state() == "unknown"