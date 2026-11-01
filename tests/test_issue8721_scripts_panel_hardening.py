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
