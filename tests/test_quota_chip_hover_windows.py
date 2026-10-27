"""Quota chip hover text lists every account window with its time to reset."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

UI_JS = (Path(__file__).resolve().parent.parent / "static" / "ui.js").read_text(encoding="utf-8")

NOW_MS = 1_800_000_000_000
HOUR_MS = 3_600_000


def _run_chip(statuses, mode="most_constrained"):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    start = UI_JS.index("function _isQuotaMeasurement")
    end = UI_JS.index("function renderProviderQuotaIndicator")
    script = (
        f"const window={{_quotaChipWindow:{json.dumps(mode)}}};"
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


# Fixed modes pick by limit_window_seconds, never by list position: the Codex pool
# (_best_remaining_by_window) returns windows in first-seen order, e.g. Weekly before Session.
WEEK = 604_800
SESSION = 18_000


def _w(label, remaining, seconds=None):
    w = {"label": label, "remaining_percent": remaining}
    if seconds is not None:
        w["limit_window_seconds"] = seconds
    return w


def _chip_label(windows, mode):
    (text,) = _run_chip([_status(windows)], mode=mode)
    return text


@pytest.mark.parametrize("windows", [
    [_w("Weekly", 70, WEEK), _w("Session", 90, SESSION)],
    [_w("Session", 90, SESSION), _w("Weekly", 70, WEEK)],
])
def test_fixed_modes_pick_by_duration_not_position(windows):
    assert _chip_label(windows, "shortest")["label"] == "90%"
    assert _chip_label(windows, "longest")["label"] == "70%"
    assert _chip_label(windows, "most_constrained")["label"] == "70%"


def test_pool_shape_weekly_first_then_session():
    # First credential reports only Weekly; a later one introduces Session.
    windows = [_w("Weekly", 55, WEEK), _w("Session", 30, SESSION), _w("Monthly", 80, 30 * 86_400)]
    assert _chip_label(windows, "shortest")["label"] == "30%"
    assert _chip_label(windows, "longest")["label"] == "80%"


def test_fixed_modes_ignore_untimed_windows_when_any_is_timed():
    windows = [_w("Untimed", 0), _w("Weekly", 80, WEEK)]
    assert _chip_label(windows, "shortest")["label"] == "80%"
    assert _chip_label(windows, "longest")["label"] == "80%"


@pytest.mark.parametrize("bad", [None, 0, -5, "", "abc", "0", True])
def test_fixed_modes_fall_back_to_most_constrained_without_valid_durations(bad):
    windows = [_w("A", 60, bad), _w("B", 20, bad), _w("C", 40)]
    for mode in ("shortest", "longest"):
        assert _chip_label(windows, mode)["label"] == "20%"


def test_numeric_string_durations_and_ties_keep_first():
    windows = [_w("Session", 90, "18000"), _w("Weekly", 70, "604800")]
    assert _chip_label(windows, "shortest")["label"] == "90%"
    assert _chip_label(windows, "longest")["label"] == "70%"
    tie = [_w("First", 50, WEEK), _w("Second", 10, WEEK)]
    assert _chip_label(tie, "shortest")["label"] == "50%"
    assert _chip_label(tie, "longest")["label"] == "50%"


def test_fixed_mode_keeps_tooltip_order_and_skips_invalid_measurements():
    windows = [_w("Weekly", 70, WEEK), _w("Broken", None, 60), _w("Session", 90, SESSION)]
    text = _chip_label(windows, "shortest")
    assert text["label"] == "90%"
    assert text["title"].split("\n")[1:] == ["Weekly: 70% remaining", "Session: 90% remaining"]
    # No usable measurement at all: the account-window chip is not rendered.
    assert _chip_label([_w("A", None, WEEK)], "shortest") is None
