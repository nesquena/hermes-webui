"""#8721: Scripts panel hardening (exp-v0.52.613, #7685).

1. The `.py` description parse runs in linear time on whitespace-only heads
   (it was quadratic: ~127 ms per 4 KiB head while holding the GIL), with the
   same descriptions as before on ordinary input.
2. Hardlinked files (st_nlink > 1) are neither listed nor served, the same
   rule as the wiki browser (#4375).
3. A listing walks a bounded number of directory entries, describes a bounded
   number of scripts in name order, and reports `truncated` when cut short.
"""

from __future__ import annotations

import os
import random
import re
import time

import pytest

import api.scripts_panel as sp

# The pre-#8721 expression, used only to show the new parse returns the same
# descriptions on ordinary input.
_OLD_PY_DOCSTRING_RE = re.compile(
    r'^\s*(?:r|u|f|rf|fr)?\s*(?:"""|\'\'\')(.*?)(?:"""|\'\'\')',
    re.DOTALL,
)


def _old_description(raw: bytes) -> str:
    head = raw[:4096].decode("utf-8", errors="replace")
    head = sp._SHEBANG_RE.sub("", head, count=1)
    m = _OLD_PY_DOCSTRING_RE.search(head)
    if not m:
        return ""
    for line in m.group(1).strip().splitlines():
        line = line.strip().lstrip("#").strip()
        if line:
            return line[:280]
    return ""


@pytest.fixture
def scripts(tmp_path, monkeypatch):
    base = tmp_path / "scripts"
    base.mkdir()
    monkeypatch.setattr(sp, "scripts_dir", lambda: base)
    return base


# ── 1. linear description parse ────────────────────────────────────────────


@pytest.mark.parametrize("filler", [" ", "\n", "\t", " \n"])
def test_whitespace_only_heads_parse_in_linear_time(filler):
    """50 whitespace-only 4 KiB heads took ~6 s before #8721; allow 1 s."""
    raw = (filler * 4096)[:4096].encode()
    start = time.perf_counter()
    for _ in range(50):
        assert sp._read_description_bytes(raw, ".py") == ""
    elapsed = time.perf_counter() - start
    assert elapsed < 1.0, f"50 whitespace-only heads took {elapsed:.2f}s"


def test_prefixed_whitespace_run_is_also_linear():
    raw = ("r" + " " * 4095).encode()
    start = time.perf_counter()
    for _ in range(50):
        assert sp._read_description_bytes(raw, ".py") == ""
    assert time.perf_counter() - start < 1.0


def test_descriptions_match_the_previous_parse_on_generated_heads():
    """Same description as the old expression across varied ordinary heads:
    shebangs, leading whitespace, string prefixes, both quote styles,
    multi-line and unterminated docstrings, and no docstring at all."""
    rng = random.Random(8721)
    shebangs = ["", "#!/usr/bin/env python3\n", "#!/bin/python\n"]
    leads = ["", "\n", "\n\n  ", "   ", "\t\n"]
    prefixes = ["", "r", "u", "f", "rf", "fr", "R", "b"]
    quotes = ['"""', "'''", '"', "'"]
    bodies = ["Back up notes.", "\n  First line.\n  Second.\n", "", "  # hash line\nreal", "x" * 300]
    tails = ['"""', "'''", "", '"', "\nimport os\n"]
    for _ in range(3000):
        head = (
            rng.choice(shebangs) + rng.choice(leads) + rng.choice(prefixes)
            + rng.choice(["", " "]) + rng.choice(quotes) + rng.choice(bodies)
            + rng.choice(tails) + rng.choice(["", "\nimport sys\n", "x = 1\n" * 20])
        )
        raw = head.encode()
        assert sp._read_description_bytes(raw, ".py") == _old_description(raw), repr(head)


# ── 2. hardlinks ───────────────────────────────────────────────────────────


@pytest.mark.skipif(not hasattr(os, "link"), reason="no hardlink support")
def test_hardlinked_scripts_are_not_listed_or_served(scripts, tmp_path):
    secret = tmp_path / "other-profile.env"
    secret.write_text('"""OTHER PROFILE SECRET"""\nKEY=abc\n')
    os.link(secret, scripts / "linked.py")
    (scripts / "plain.py").write_text('"""A normal script."""\n')

    listing = sp.list_scripts()
    names = [s["name"] for s in listing["scripts"]]
    assert names == ["plain.py"], listing
    assert all("SECRET" not in (s["description"] or "") for s in listing["scripts"])
    assert sp.read_script("linked.py") is None
    served = sp.read_script("plain.py")
    assert served is not None
    assert served["content"].startswith('"""A normal script."""')


@pytest.mark.skipif(not hasattr(os, "link"), reason="no hardlink support")
def test_a_script_hardlinked_inside_the_directory_is_refused_too(scripts):
    """Both names of an in-directory hardlink carry nlink 2; neither is served."""
    (scripts / "a.py").write_text('"""A."""\n')
    os.link(scripts / "a.py", scripts / "b.py")
    assert sp.list_scripts()["scripts"] == []
    assert sp.read_script("a.py") is None


# ── 3. listing caps ────────────────────────────────────────────────────────


def test_listing_is_not_truncated_normally(scripts):
    for name in ("b.py", "a.sh", "c.py"):
        (scripts / name).write_text("# s\n")
    listing = sp.list_scripts()
    assert [s["name"] for s in listing["scripts"]] == ["a.sh", "b.py", "c.py"]
    assert listing["truncated"] is False


def test_script_cap_keeps_the_first_scripts_by_name(scripts, monkeypatch):
    monkeypatch.setattr(sp, "_MAX_LISTED_SCRIPTS", 3)
    for name in ("e.py", "b.py", "d.sh", "a.py", "c.py"):
        (scripts / name).write_text("# s\n")
    (scripts / "notes.txt").write_text("not a script")
    listing = sp.list_scripts()
    assert [s["name"] for s in listing["scripts"]] == ["a.py", "b.py", "c.py"]
    assert listing["truncated"] is True


def test_entry_cap_bounds_the_directory_walk(scripts, monkeypatch):
    monkeypatch.setattr(sp, "_MAX_LIST_ENTRIES", 4)
    for i in range(10):
        (scripts / f"n{i}.txt").write_text("x")
    listing = sp.list_scripts()
    assert listing["scripts"] == []
    assert listing["truncated"] is True


def test_exactly_the_script_cap_is_not_truncated(scripts, monkeypatch):
    monkeypatch.setattr(sp, "_MAX_LISTED_SCRIPTS", 3)
    for name in ("a.py", "b.py", "c.py"):
        (scripts / name).write_text("# s\n")
    listing = sp.list_scripts()
    assert len(listing["scripts"]) == 3
    assert listing["truncated"] is False


# ── #8723 gate: post-read containment and lazy enumeration ────────────────


class _OsProxy:
    """Stand-in for ``api.scripts_panel.os`` that runs ``hook`` right after the
    leaf open of ``target`` succeeds: the interleaving the gate used, where the
    entry changes between ``open`` and ``fstat``."""

    def __init__(self, target, hook):
        self._target = target
        self._hook = hook
        self._fired = False

    def __getattr__(self, name):
        return getattr(os, name)

    def open(self, path, flags, *args, **kwargs):
        fd = os.open(path, flags, *args, **kwargs)
        if path == self._target and "dir_fd" in kwargs and not self._fired:
            self._fired = True
            self._hook()
        return fd


def _plant_outside_link(scripts, tmp_path):
    secret = tmp_path / "other-profile.env"
    secret.write_text('"""OTHER PROFILE SECRET"""\nKEY=abc\n')
    os.link(secret, scripts / "s.py")
    return secret


@pytest.mark.skipif(not hasattr(os, "link"), reason="no hardlink support")
def test_entry_unlinked_after_open_is_not_served(scripts, tmp_path, monkeypatch):
    """Gate repro: link an outside file in, unlink the scripts entry after open.
    The opened inode then has link count 1, but its only name is outside."""
    _plant_outside_link(scripts, tmp_path)
    monkeypatch.setattr(sp, "os", _OsProxy("s.py", lambda: (scripts / "s.py").unlink()))
    assert sp.read_script("s.py") is None


@pytest.mark.skipif(not hasattr(os, "link"), reason="no hardlink support")
def test_entry_unlinked_after_open_is_not_listed(scripts, tmp_path, monkeypatch):
    _plant_outside_link(scripts, tmp_path)
    monkeypatch.setattr(sp, "os", _OsProxy("s.py", lambda: (scripts / "s.py").unlink()))
    listing = sp.list_scripts()
    assert listing["scripts"] == [], listing


@pytest.mark.skipif(not hasattr(os, "link"), reason="no hardlink support")
def test_entry_replaced_after_open_is_not_served(scripts, tmp_path, monkeypatch):
    """The entry is swapped for a different single-link file after open; the
    opened (outside) inode is no longer what the scripts entry names."""
    _plant_outside_link(scripts, tmp_path)

    def swap():
        (scripts / "s.py").unlink()
        (scripts / "s.py").write_text('"""decoy"""\n')

    monkeypatch.setattr(sp, "os", _OsProxy("s.py", swap))
    assert sp.read_script("s.py") is None


def test_file_unlinked_entirely_after_open_is_not_served(scripts, monkeypatch):
    """Link count 0 (the only name removed after open) is refused too."""
    (scripts / "gone.py").write_text('"""gone"""\n')
    monkeypatch.setattr(sp, "os", _OsProxy("gone.py", lambda: (scripts / "gone.py").unlink()))
    assert sp.read_script("gone.py") is None


def test_ordinary_scripts_still_pass_the_post_read_check(scripts):
    (scripts / "ok.py").write_text('"""Fine."""\n')
    (scripts / "big.sh").write_bytes(b"#" * (sp._MAX_SCRIPT_BYTES + 1))
    served = sp.read_script("ok.py")
    assert served is not None and served["content"] == '"""Fine."""\n'
    large = sp.read_script("big.sh")
    assert large is not None and large["too_large"] is True
    assert [s["name"] for s in sp.list_scripts()["scripts"]] == ["big.sh", "ok.py"]


def test_listing_does_not_materialise_the_whole_directory(scripts, monkeypatch):
    """Gate repro: Path.iterdir() is os.listdir(), so the entry cap bounded
    nothing. Enumeration must be lazy and stop at the cap plus one lookahead."""
    for i in range(60):
        (scripts / f"n{i:02d}.txt").write_text("x")
    monkeypatch.setattr(sp, "_MAX_LIST_ENTRIES", 10)
    consumed = []
    real_scandir = os.scandir

    class _Counting:
        def __init__(self, path):
            self._cm = real_scandir(path)

        def __enter__(self):
            self._it = self._cm.__enter__()
            return self

        def __exit__(self, *exc):
            return self._cm.__exit__(*exc)

        def __iter__(self):
            for entry in self._it:
                consumed.append(entry.name)
                yield entry

    def _no_listdir(*_a, **_k):
        raise AssertionError("os.listdir materialises the whole directory")

    proxy = _OsProxy(None, lambda: None)
    proxy.scandir = _Counting
    monkeypatch.setattr(sp, "os", proxy)
    # pathlib calls os.listdir through its own module reference, so patch the
    # real module too: any listdir-based walk fails this test.
    monkeypatch.setattr(os, "listdir", _no_listdir)
    listing = sp.list_scripts()
    assert consumed, "enumeration must go through os.scandir"
    assert listing["truncated"] is True
    assert len(consumed) <= 11, len(consumed)


# ── #8723 senior gate: say why scripts are missing ─────────────────────────


@pytest.mark.skipif(not hasattr(os, "link"), reason="no hardlink support")
def test_listing_counts_skipped_hardlinked_scripts(scripts, tmp_path):
    """A `cp -al` copy gives every script link count 2. The listing must say
    so instead of reading as an empty folder."""
    backup = tmp_path / "scripts.bak"
    backup.mkdir()
    for name in ("a.py", "b.sh"):
        (scripts / name).write_text("# s\n")
        os.link(scripts / name, backup / name)
    (scripts / "c.py").write_text("# s\n")
    (scripts / "notes.txt").write_text("x")
    listing = sp.list_scripts()
    assert [s["name"] for s in listing["scripts"]] == ["c.py"]
    assert listing["skipped_hardlinked"] == 2
    assert listing["truncated"] is False


def test_listing_reports_zero_skipped_normally(scripts):
    (scripts / "a.py").write_text("# s\n")
    assert sp.list_scripts()["skipped_hardlinked"] == 0


def test_entry_cap_is_far_above_the_script_cap():
    """Walking a name is cheap with scandir; only describing is expensive, so
    the entry cap is a pathological-directory stop, not the working limit."""
    assert sp._MAX_LIST_ENTRIES >= 100 * sp._MAX_LISTED_SCRIPTS
