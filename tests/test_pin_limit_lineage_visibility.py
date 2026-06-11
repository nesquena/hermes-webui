import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from api.routes import _session_row_lineage_root_id, _visible_pinned_lineage_ids


def test_visible_pinned_lineage_ids_dedupes_multiple_pinned_continuations():
    rows = [
        {
            "session_id": "gov-new",
            "title": "Project OS Governor",
            "pinned": True,
            "archived": False,
            "parent_session_id": "gov-mid",
        },
        {
            "session_id": "gov-mid",
            "title": "Project OS Governor",
            "pinned": True,
            "archived": False,
            "parent_session_id": "gov-root",
        },
        {
            "session_id": "gov-root",
            "title": "Project OS Governor",
            "pinned": True,
            "archived": False,
            "parent_session_id": None,
        },
        {
            "session_id": "other-pin",
            "title": "Import Preview",
            "pinned": True,
            "archived": False,
            "parent_session_id": None,
        },
    ]
    roots = _visible_pinned_lineage_ids(rows)
    assert roots == {"gov-root", "other-pin"}


def test_visible_pinned_lineage_ids_ignores_hidden_precompression_snapshots():
    rows = [
        {
            "session_id": "snap-root",
            "title": "Project OS Governor",
            "pinned": True,
            "archived": False,
            "pre_compression_snapshot": True,
            "parent_session_id": None,
        },
        {
            "session_id": "live-root",
            "title": "Project OS Governor",
            "pinned": True,
            "archived": False,
            "parent_session_id": "snap-root",
        },
    ]
    roots = _visible_pinned_lineage_ids(rows)
    assert roots == {"snap-root"}


def test_session_row_lineage_root_uses_explicit_root_when_present():
    row = {
        "session_id": "tip",
        "_lineage_root_id": "root-123",
        "parent_session_id": "older",
    }
    assert _session_row_lineage_root_id(row, {"tip": row}) == "root-123"


def test_pinned_forks_of_same_parent_count_as_separate_lineages():
    """A branch/fork (session_source='fork') is an independent visible session and
    must consume its own pin slot — two pinned forks of the same parent must NOT
    collapse to one quota lineage (would let the user exceed the pin limit). #3288."""
    rows = [
        {
            "session_id": "parent-root",
            "title": "Original",
            "pinned": True,
            "archived": False,
            "parent_session_id": None,
        },
        {
            "session_id": "fork-a",
            "title": "Fork A",
            "pinned": True,
            "archived": False,
            "session_source": "fork",
            "parent_session_id": "parent-root",
        },
        {
            "session_id": "fork-b",
            "title": "Fork B",
            "pinned": True,
            "archived": False,
            "session_source": "fork",
            "parent_session_id": "parent-root",
        },
    ]
    # Each fork is its own lineage root; the parent is its own root too → 3 lineages.
    assert _session_row_lineage_root_id(rows[1], {r["session_id"]: r for r in rows}) == "fork-a"
    assert _session_row_lineage_root_id(rows[2], {r["session_id"]: r for r in rows}) == "fork-b"
    roots = _visible_pinned_lineage_ids(rows)
    assert roots == {"parent-root", "fork-a", "fork-b"}
    # Three distinct pinned lineages → would exceed a limit of 2 (no false collapse).
    assert len(roots) == 3
