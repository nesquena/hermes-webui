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
