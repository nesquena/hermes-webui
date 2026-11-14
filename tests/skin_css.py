"""Shared accessor for the per-skin stylesheets.

Skins live in ``static/skins/<key>.css`` (see ``static/skins/README.md``).
Tests used to read one big ``static/style.css``, which made every skin assertion
depend on the whole file's order; these helpers keep assertions scoped to the
piece under test.

Three layers, in load order (matches ``static/index.html``):

1. ``static/style.css``      base palette + every non-skin component rule
2. ``static/skins/<key>.css`` one file per skin, self-scoped
3. ``static/scaffold.css``   shared ``:root[data-skin]`` resets that must beat
                             the skin rules, so this file loads last

``read_all_css()`` returns the three layers in that order, which is what tests
that used to read the monolithic ``style.css`` want.
"""

from pathlib import Path

REPO = Path(__file__).parent.parent
SKIN_DIR = REPO / "static" / "skins"
BASE_CSS = (REPO / "static" / "style.css").read_text(encoding="utf-8")
SCAFFOLD_CSS = (REPO / "static" / "scaffold.css").read_text(encoding="utf-8")


def skin_keys():
    """Every skin key that has a file, sorted."""
    return sorted(p.stem for p in SKIN_DIR.glob("*.css"))


def read_skin_css(key):
    """CSS for one skin (no base rules). Raises if the file is missing."""
    path = SKIN_DIR / f"{key}.css"
    if not path.is_file():
        raise AssertionError(f"missing skin stylesheet: static/skins/{key}.css")
    return path.read_text(encoding="utf-8")


def read_all_skins_css():
    """Concatenation of every skin file (base rules excluded)."""
    return "\n".join(read_skin_css(k) for k in skin_keys())


def read_base_css():
    return BASE_CSS


def read_scaffold_css():
    """The shared ``:root[data-skin]`` layer that loads after every skin."""
    return SCAFFOLD_CSS


def read_all_css():
    """base + skins + scaffold, in the order ``index.html`` loads them."""
    return "\n".join((BASE_CSS, read_all_skins_css(), SCAFFOLD_CSS))
