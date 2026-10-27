"""Quota chip hover text lists every account window with its time to reset."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

UI_JS = (Path(__file__).resolve().parent.parent / "static" / "ui.js").read_text(encoding="utf-8")

NOW_MS = 1_800_000_000_000
HOUR_MS = 3_600_000


def _run_chip(statuses):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    start = UI_JS.index("function _isQuotaMeasurement")
    end = UI_JS.index("function renderProviderQuotaIndicator")
    script = (
        "const window={_quotaChipWindow:'most_constrained'};"
        f"Date.now=()=>{NOW_MS};"
        + UI_JS[start:end]
        + f";console.log(JSON.stringify({json.dumps(statuses)}.map(_providerQuotaIndicatorText)));"
    )
    out = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=30, check=True)
    return json.loads(out.stdout)


def _iso(offset_ms):
    from datetime import datetime, timezone

    return datetime.fromtimestamp((NOW_MS + offset_ms) / 1000, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _status(windows, details=()):
    return {
        "status": "available", "display_name": "OpenCode Go", "message": "loaded",
        "account_limits": {"windows": windows, "details": list(details)},
    }


def test_hover_lists_all_windows_with_reset_countdown():
    (text,) = _run_chip([_status(
        [
            {"label": "Rolling", "remaining_percent": 82, "reset_at": _iso(2 * HOUR_MS + 14 * 60_000)},
            {"label": "Weekly", "remaining_percent": 61.4, "reset_at": _iso(77 * HOUR_MS)},
            {"label": "Monthly", "remaining_percent": 40, "reset_at": _iso(12 * 24 * HOUR_MS)},
        ],
        details=["Credits remaining: $12.5"],
    )])
    assert text["label"] == "40%"
    assert text["title"].split("\n") == [
        "OpenCode Go — loaded",
        "Rolling: 82% remaining · resets in 2h 14m",
        "Weekly: 61% remaining · resets in 3d 5h",
        "Monthly: 40% remaining · resets in 12d",
        "Credits remaining: $12.5",
    ]


def test_hover_omits_reset_when_missing_past_or_unparseable():
    (text,) = _run_chip([_status([
        {"label": "A", "remaining_percent": None, "reset_at": _iso(HOUR_MS)},
        {"label": "B", "remaining_percent": 10, "reset_at": _iso(-HOUR_MS)},
        {"label": "C", "remaining_percent": 20, "reset_at": "garbage"},
        {"label": "D", "remaining_percent": 30},
    ])])
    assert text["title"].split("\n") == [
        "OpenCode Go — loaded", "B: 10% remaining", "C: 20% remaining", "D: 30% remaining",
    ]


def test_single_window_keeps_one_line_title():
    (text,) = _run_chip([_status([{"label": "Weekly", "remaining_percent": 50, "reset_at": _iso(5 * HOUR_MS)}])])
    assert text["title"] == "OpenCode Go — loaded — Weekly: 50% remaining · resets in 5h"


def test_hover_title_is_rebuilt_on_hover_and_focus():
    assert "chip.addEventListener('mouseenter',_refreshProviderQuotaTitle)" in UI_JS
    assert "chip.addEventListener('focus',_refreshProviderQuotaTitle)" in UI_JS
