"""Route-facing regression coverage for exact workspace timestamp transport."""

from api import workspace as workspace_api


def test_browser_workspace_entries_stringify_nanosecond_fields():
    entries = [
        {
            "name": "newer.txt",
            "path": "newer.txt",
            "type": "file",
            "mtime_ns": 1_752_598_800_000_000_001,
            "birthtime_ns": 1_752_598_800_000_000_000,
        },
        {
            "name": "missing.txt",
            "path": "missing.txt",
            "type": "file",
            "mtime_ns": None,
            "birthtime_ns": None,
        },
    ]

    payload = workspace_api.serialize_workspace_entries_for_browser(entries)

    assert payload[0]["mtime_ns"] == "1752598800000000001"
    assert payload[0]["birthtime_ns"] == "1752598800000000000"
    assert payload[1]["mtime_ns"] is None
    assert payload[1]["birthtime_ns"] is None
    assert entries[0]["mtime_ns"] == 1_752_598_800_000_000_001
    assert entries[0]["birthtime_ns"] == 1_752_598_800_000_000_000
