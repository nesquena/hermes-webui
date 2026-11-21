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
from pathlib import Path

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