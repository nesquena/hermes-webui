"""Quota chip hover text lists every account window with its time to reset."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

UI_JS = (Path(__file__).resolve().parent.parent / "static" / "ui.js").read_text(encoding="utf-8")

NOW_MS = 1_800_000_000_000
HOUR_MS = 3_600_000
# With several windows, the hover marks the one the chip shows and indents the rest.
MARK = "\u25b8 "
PAD = "\u2002 "


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
        PAD + "Rolling: 82% remaining · resets in 2h 14m",
        PAD + "Weekly: 61% remaining · resets in 3d 5h",
        MARK + "Monthly: 40% remaining · resets in 12d",
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
        "OpenCode Go — loaded", MARK + "B: 10% remaining", PAD + "C: 20% remaining", PAD + "D: 30% remaining",
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


def test_fixed_modes_fall_back_when_any_measured_window_is_untimed():
    # An untimed window can't be ordered by duration; skipping it would show a
    # healthy share while that limit is depleted.
    windows = [_w("Untimed", 0), _w("Weekly", 80, WEEK)]
    assert _chip_label(windows, "shortest")["label"] == "0%"
    assert _chip_label(windows, "longest")["label"] == "0%"


def test_codex_shaped_partial_durations_fall_back_to_most_constrained():
    # Codex pool after a failed duration probe: Weekly backfilled with its raw
    # duration, Session measured but untimed (tests/test_provider_quota_status.py
    # test_codex_singleton_raw_durations_survive_failed_probe).
    windows = [_w("Weekly", 80, WEEK), _w("Session", 5)]
    for mode in ("shortest", "longest", "most_constrained"):
        assert _chip_label(windows, mode)["label"] == "5%"


def _scoped(label, remaining, scope, seconds=None):
    w = _w(label, remaining, seconds)
    w["scope"] = scope
    return w


# Anthropic: the Agent tags the per-model weekly caps scope="model"; none of the
# windows reports a duration. A depleted model-only cap must not hide a healthy
# account.
ANTHROPIC = [
    _scoped("Current session", 80, "account"),
    _scoped("Current week", 60, "account"),
    _scoped("Opus week", 0, "model"),
    _scoped("Sonnet week", 90, "model"),
]


def test_model_only_windows_do_not_drive_the_chip_when_account_windows_exist():
    for mode in ("most_constrained", "shortest", "longest"):
        text = _chip_label(ANTHROPIC, mode)
        assert text["label"] == "60%", mode
        # The hover still lists every window, model-only ones included.
        assert text["title"].split("\n")[1:] == [
            PAD + "Current session: 80% remaining", MARK + "Current week: 60% remaining",
            PAD + "Opus week: 0% remaining", PAD + "Sonnet week: 90% remaining",
        ]


def test_fixed_modes_order_account_windows_only():
    windows = [
        _scoped("Session", 70, "account", SESSION), _scoped("Weekly", 40, "account", WEEK),
        _scoped("Model day", 5, "model", 86_400),
    ]
    assert _chip_label(windows, "shortest")["label"] == "70%"
    assert _chip_label(windows, "longest")["label"] == "40%"
    assert _chip_label(windows, "most_constrained")["label"] == "40%"


def test_hover_marks_only_the_window_the_chip_shows():
    windows = [_w("Session", 90, SESSION), _w("Weekly", 70, WEEK)]
    assert _chip_label(windows, "shortest")["title"].split("\n")[1:] == [
        MARK + "Session: 90% remaining", PAD + "Weekly: 70% remaining"]
    assert _chip_label(windows, "longest")["title"].split("\n")[1:] == [
        PAD + "Session: 90% remaining", MARK + "Weekly: 70% remaining"]
    # One window keeps its unmarked single-line title.
    assert _chip_label([_w("Weekly", 70, WEEK)], "shortest")["title"] == "OpenCode Go — loaded — Weekly: 70% remaining"


def _anthropic_status_from_agent_snapshot(windows):
    """The Agent's Anthropic snapshot shape, through the server's serializer."""
    from types import SimpleNamespace

    from api.providers import _serialize_account_usage_snapshot

    snapshot = SimpleNamespace(
        provider="anthropic", source="oauth_usage_api", title="Account limits", plan=None,
        windows=tuple(SimpleNamespace(reset_at=None, detail=None, **w) for w in windows),
        details=(), available=True, unavailable_reason=None, fetched_at=None,
    )
    return {"status": "available", "display_name": "Anthropic", "message": "loaded",
            "account_limits": _serialize_account_usage_snapshot(snapshot)}


def test_anthropic_shortest_and_longest_change_the_chip():
    """Anthropic reports no window lengths; the server's nominal lengths let the fixed modes work."""
    status = _anthropic_status_from_agent_snapshot([
        {"label": "Current session", "used_percent": 20, "scope": "account"},
        {"label": "Current week", "used_percent": 40, "scope": "account"},
        {"label": "Opus week", "used_percent": 100, "scope": "model"},
        {"label": "Sonnet week", "used_percent": 10, "scope": "model"},
    ])
    chosen = {}
    for mode in ("shortest", "longest", "most_constrained"):
        (text,) = _run_chip([status], mode=mode)
        chosen[mode] = text["label"]
        assert len(text["title"].split("\n")) == 5, text  # every window stays in the hover
    assert chosen == {"shortest": "80%", "longest": "60%", "most_constrained": "60%"}


def test_all_model_scoped_windows_keep_todays_selection():
    windows = [_scoped("Opus week", 30, "model"), _scoped("Sonnet week", 70, "model")]
    for mode in ("most_constrained", "shortest", "longest"):
        assert _chip_label(windows, mode)["label"] == "30%", mode


def test_windows_without_scope_count_as_account():
    # Providers (and older Agents) that send no scope are unchanged.
    windows = [_w("Weekly", 70, WEEK), _w("Session", 90, SESSION), _scoped("Monthly", 10, "", 30 * 86_400)]
    assert _chip_label(windows, "shortest")["label"] == "90%"
    assert _chip_label(windows, "longest")["label"] == "10%"
    assert _chip_label(windows, "most_constrained")["label"] == "10%"


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
    assert text["title"].split("\n")[1:] == [PAD + "Weekly: 70% remaining", MARK + "Session: 90% remaining"]
    # No usable measurement at all: the account-window chip is not rendered.
    assert _chip_label([_w("A", None, WEEK)], "shortest") is None


def test_compact_quota_button_refreshes_its_countdown_on_hover_and_focus():
    """The compact composer hides the desktop chip (CSS), so its quota button needs the same refresh."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    start = UI_JS.index("function _isQuotaMeasurement")
    end = UI_JS.index("// Dynamic model labels")
    status = _status([
        {"label": "5-hour", "remaining_percent": 40, "reset_at": _iso(3 * HOUR_MS), "limit_window_seconds": 18000},
        {"label": "Weekly", "remaining_percent": 70, "reset_at": _iso(50 * HOUR_MS), "limit_window_seconds": 604800},
    ])
    script = (
        "let now=" + str(NOW_MS) + ";Date.now=()=>now;"
        "const window={_quotaChipWindow:'most_constrained',addEventListener(){}};"
        "function el(){const l={};return {hidden:false,title:'',textContent:'',"
        "addEventListener(t,f){(l[t]=l[t]||[]).push(f)},fire(t){(l[t]||[]).forEach(f=>f())},removeAttribute(a){this[a]=''}}}"
        "const els={providerQuotaChip:el(),composerMobileQuotaAction:el()};const $=id=>els[id]||null;"
        + UI_JS[start:end]
        + ";_providerQuotaLastStatus=" + json.dumps(status) + ";_initProviderQuotaTitleRefresh();"
        "const m=els.composerMobileQuotaAction;m.fire('mouseenter');const a=m.title;"
        "now+=2*" + str(HOUR_MS) + ";m.fire('focus');const b=m.title;"
        "_initProviderQuotaTitleRefresh();m.title='';m.fire('mouseenter');"
        "console.log(JSON.stringify({a,b,once:m.title===b}));"
    )
    out = json.loads(subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=30, check=True).stdout)
    assert out["a"] and out["b"] and out["a"] != out["b"]  # the countdown moved with the clock
    assert out["once"]  # re-init does not double-bind (title rebuilt by exactly the bound listeners)


def test_shortest_and_longest_work_for_opencode_go_shaped_windows():
    """#8548 review finding: with the sanitizer's durations, the fixed modes move off most-constrained."""
    from api.providers import _sanitize_opencode_go_quota
    windows = _sanitize_opencode_go_quota({"quota": {
        "rolling": {"percent": 20}, "monthly": {"percent": 80}}})["windows"]  # 80% left 5-hour, 20% left monthly
    short, = _run_chip([_status(windows)], mode="shortest")
    long_, = _run_chip([_status(windows)], mode="longest")
    most, = _run_chip([_status(windows)], mode="most_constrained")
    assert "80" in short["label"] and "20" in long_["label"] and "20" in most["label"]
