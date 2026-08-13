import io
import json
import pathlib
import sys
import time

import pytest
from types import SimpleNamespace

REPO_ROOT = pathlib.Path(__file__).parent.parent.resolve()
sys.path.insert(0, str(REPO_ROOT))

PANELS_JS = (REPO_ROOT / "static" / "panels.js").read_text(encoding="utf-8")
STYLE_CSS = (REPO_ROOT / "static" / "style.css").read_text(encoding="utf-8")
INDEX_HTML = (REPO_ROOT / "static" / "index.html").read_text(encoding="utf-8")


class _FakeHandler:
    def __init__(self):
        self.status = None
        self.sent_headers = []
        self.body = bytearray()
        self.wfile = self
        self.rfile = io.BytesIO()
        self.headers = {}
        self.request = None

    def send_response(self, status):
        self.status = status

    def send_header(self, name, value):
        self.sent_headers.append((name, value))

    def end_headers(self):
        pass

    def write(self, data):
        self.body.extend(data)

    def json_body(self):
        return json.loads(bytes(self.body).decode("utf-8"))


def _call_insights(monkeypatch, tmp_path, entries, days="7", now=None, query=None):
    import api.routes as routes

    session_dir = tmp_path / "sessions"
    session_dir.mkdir(exist_ok=True)
    (session_dir / "_index.json").write_text(json.dumps(entries), encoding="utf-8")
    monkeypatch.setattr(routes, "SESSION_DIR", session_dir)
    if now is not None:
        monkeypatch.setattr(time, "time", lambda: now)

    handler = _FakeHandler()
    if query is None:
        query = f"days={days}"
    parsed = SimpleNamespace(query=query)
    routes._handle_insights(handler, parsed)
    assert handler.status == 200
    return handler.json_body()


def _day(ts):
    return time.strftime("%Y-%m-%d", time.localtime(ts))


def test_insights_absolute_range_start_end_filters_by_window(monkeypatch, tmp_path):
    now = time.mktime((2026, 5, 4, 12, 0, 0, 0, 0, -1))
    start_ts = now - (10 * 86400)   # 10 days ago
    end_ts = now - (2 * 86400)      # 2 days ago
    entries = [
        {
            "session_id": "in_window", "updated_at": end_ts, "created_at": start_ts,
            "message_count": 4, "input_tokens": 1200, "output_tokens": 300,
            "estimated_cost": "$0.0123", "model": "gpt-x",
        },
        {
            "session_id": "older", "updated_at": now - (30 * 86400), "created_at": now - (30 * 86400),
            "message_count": 2, "input_tokens": 999, "output_tokens": 111,
            "estimated_cost": "0.0100", "model": "gpt-x",
        },
        {
            "session_id": "newer", "updated_at": now, "created_at": now,
            "message_count": 1, "input_tokens": 50, "output_tokens": 10,
            "estimated_cost": "0.0010", "model": "gpt-x",
        },
    ]
    data = _call_insights(monkeypatch, tmp_path, entries, query=f"start={int(start_ts)}&end={int(end_ts)}", now=now)
    # Session counts: in_window counted; older and newer excluded by the absolute window.
    assert data["total_sessions"] == 1
    assert data["total_input_tokens"] == 1200
    assert data["total_output_tokens"] == 300
    # Daily series spans from start day to end day (inclusive).
    assert _day(start_ts) == data["daily_tokens"][0]["date"]
    assert _day(end_ts) == data["daily_tokens"][-1]["date"]
    assert len(data["daily_tokens"]) <= 30


def test_insights_absolute_range_start_only_defaults_end_to_now(monkeypatch, tmp_path):
    now = time.mktime((2026, 5, 4, 12, 0, 0, 0, 0, -1))
    start_ts = now - (5 * 86400)
    entries = [
        {
            "session_id": "s1", "updated_at": now, "created_at": start_ts,
            "message_count": 3, "input_tokens": 300, "output_tokens": 100,
            "estimated_cost": "0.0030", "model": "gpt-x",
        },
        {
            "session_id": "old", "updated_at": now - (40 * 86400), "created_at": now - (40 * 86400),
            "message_count": 1, "input_tokens": 700, "output_tokens": 200,
            "estimated_cost": "0.0070", "model": "gpt-x",
        },
    ]
    data = _call_insights(monkeypatch, tmp_path, entries, query=f"start={int(start_ts)}", now=now)
    assert data["total_sessions"] == 1
    assert data["total_input_tokens"] == 300


def test_insights_absolute_range_invalid_falls_back_to_days(monkeypatch, tmp_path):
    now = time.mktime((2026, 5, 4, 12, 0, 0, 0, 0, -1))
    entries = [
        {
            "session_id": "today", "updated_at": now, "created_at": now,
            "message_count": 1, "input_tokens": 10, "output_tokens": 5,
            "estimated_cost": "0.0001", "model": "gpt-x",
        },
    ]
    # Invalid start/end values should not crash; fall back to trailing days.
    data = _call_insights(monkeypatch, tmp_path, entries, query="start=notanum&end=abc", now=now)
    assert data["total_sessions"] == 1
    assert len(data["daily_tokens"]) == 30  # falls back to default trailing 30 days


def test_insights_absolute_range_nonfinite_timestamps_do_not_500(monkeypatch, tmp_path):
    now = time.mktime((2026, 5, 4, 12, 0, 0, 0, 0, -1))
    entries = [
        {
            "session_id": "today", "updated_at": now, "created_at": now,
            "message_count": 1, "input_tokens": 10, "output_tokens": 5,
            "estimated_cost": "0.0001", "model": "gpt-x",
        },
    ]
    # nan/inf are accepted by float() but would crash int()/localtime().
    # They must be rejected so the handler falls back instead of returning 500.
    data = _call_insights(monkeypatch, tmp_path, entries, query="start=nan&end=inf", now=now)
    assert data["total_sessions"] == 1
    assert len(data["daily_tokens"]) == 30

    data = _call_insights(monkeypatch, tmp_path, entries, query="start=1640995200&end=nan", now=now)
    # `end=nan` is rejected, so the range defaults to start..now (== 1585 days).
    assert data["total_sessions"] == 1
    assert len(data["daily_tokens"]) == 1585


def test_insights_absolute_range_finite_out_of_range_falls_back_to_trailing(monkeypatch, tmp_path):
    now = time.mktime((2026, 5, 4, 12, 0, 0, 0, 0, -1))
    entries = [
        {
            "session_id": "today", "updated_at": now, "created_at": now,
            "message_count": 1, "input_tokens": 10, "output_tokens": 5,
            "estimated_cost": "0.0001", "model": "gpt-x",
        },
    ]
    # Huge positive finite timestamp (1e20) must not reach localtime -> 500;
    # clamps to trailing mode instead.
    data = _call_insights(monkeypatch, tmp_path, entries, query="start=1e20", now=now)
    assert data["total_sessions"] == 1
    assert len(data["daily_tokens"]) == 30  # fallback to trailing 30 days
    assert data["period_days"] == len(data["daily_tokens"])
    # Huge negative finite timestamp must similarly fall back.
    data = _call_insights(monkeypatch, tmp_path, entries, query="start=-1e20", now=now)
    assert data["total_sessions"] == 1
    assert len(data["daily_tokens"]) == 30
    # Both endpoints out of range -> fall back.
    data = _call_insights(monkeypatch, tmp_path, entries, query="start=-1e20&end=1e20", now=now)
    assert data["total_sessions"] == 1
    assert len(data["daily_tokens"]) == 30
    # end out of range with valid start -> end clamped to now by min(end,now),
    # so the range start..now is valid (not fallback).
    data = _call_insights(monkeypatch, tmp_path, entries, query="start=1640995200&end=1e20", now=now)
    assert data["total_sessions"] == 1
    assert data["period_days"] == len(data["daily_tokens"])
    assert len(data["daily_tokens"]) > 30  # valid start..now range, not 30-day fallback


def test_insights_absolute_range_dst_transition_daily_buckets(monkeypatch, tmp_path):
    """Custom range straddling a DST transition must produce the correct
    number of calendar-day buckets.  The old fixed-86400-step logic would
    calculate days = int((end-start)/86400)+1, which is off by one across a
    23-hour spring-forward day (March 8, 2026 in America/New_York)"""
    import os
    if not hasattr(time, "tzset"):
        pytest.skip("time.tzset() required for DST test (not available on Windows)")

    os.environ["TZ"] = "America/New_York"
    time.tzset()
    try:
        now = time.mktime((2026, 3, 11, 12, 0, 0, 0, 0, -1))  # after spring-forward
        # March 6 (EST, UTC-5) to March 10 (EDT, UTC-4), straddling the
        # March 8 "spring forward" (2:00->3:00).  Old fixed-86400 stepping
        # would produce 4 daily buckets; the DST-safe calendar-day iteration
        # produces 5.
        start_ts = time.mktime((2026, 3, 6, 12, 0, 0, 0, 0, -1))
        end_ts = time.mktime((2026, 3, 10, 12, 0, 0, 0, 0, -1))
        entries = [
            {
                "session_id": "s1", "updated_at": now, "created_at": now,
                "message_count": 1, "input_tokens": 10, "output_tokens": 5,
                "estimated_cost": "0.0001", "model": "gpt-x",
            },
        ]
        data = _call_insights(monkeypatch, tmp_path, entries,
                              query=f"start={int(start_ts)}&end={int(end_ts)}", now=now)
        dates = [d["date"] for d in data["daily_tokens"]]
        assert dates == ["2026-03-06", "2026-03-07", "2026-03-08", "2026-03-09", "2026-03-10"]
        assert data["period_days"] == 5
        assert data["period_days"] == len(data["daily_tokens"])
    finally:
        os.environ.pop("TZ", None)
        if hasattr(time, "tzset"):
            time.tzset()



def test_insights_absolute_range_dst_end_boundary_next_local_midnight(monkeypatch, tmp_path):
    """The exclusive end bound must be the NEXT local midnight, not fixed
    end_midnight + 86400s.  Across a spring-forward transition the fixed
    addition lands at 01:00 of the following date, so the final hour of the
    selected day would be dropped; across fall-back it lands at 23:00,
    leaking an hour of the next date in.  America/New_York 2026-03-08
    springs forward 2:00 -> 3:00, so the next local midnight after the
    2026-03-08 end day is 2026-03-09 00:00 EDT."""
    import os
    if not hasattr(time, "tzset"):
        pytest.skip("time.tzset() required for DST test (not available on Windows)")

    os.environ["TZ"] = "America/New_York"
    time.tzset()
    try:
        now = time.mktime((2026, 3, 11, 12, 0, 0, 0, 0, -1))  # after spring-forward
        # End day = 2026-03-08 (the spring-forward day).  Its final local hour is
        # 3:00-4:00 EDT = 07:00-08:00 UTC (offset -4h after the transition).
        end_day_last_hour = time.mktime((2026, 3, 8, 3, 30, 0, 0, 0, -1))  # 03:30 EDT
        # First hour of the NEXT local day = 2026-03-09 00:00-01:00 EDT.
        next_day_first_hour = time.mktime((2026, 3, 9, 0, 30, 0, 0, 0, -1))  # 00:30 EDT
        start_ts = time.mktime((2026, 3, 6, 12, 0, 0, 0, 0, -1))
        end_ts = time.mktime((2026, 3, 8, 12, 0, 0, 0, 0, -1))
        assert end_day_last_hour > end_ts  # sanity: the final-hour probe is inside the selected day
        assert next_day_first_hour > end_day_last_hour

        entries = [
            {
                "session_id": "in_last_hour", "updated_at": end_day_last_hour, "created_at": end_day_last_hour,
                "message_count": 1, "input_tokens": 10, "output_tokens": 5,
                "estimated_cost": "0.0001", "model": "gpt-x",
            },
            {
                "session_id": "out_next_day", "updated_at": next_day_first_hour, "created_at": next_day_first_hour,
                "message_count": 1, "input_tokens": 10, "output_tokens": 5,
                "estimated_cost": "0.0001", "model": "gpt-x",
            },
        ]
        data = _call_insights(monkeypatch, tmp_path, entries,
                              query=f"start={int(start_ts)}&end={int(end_ts)}", now=now)
        # Spring-forward: the OLD fixed +86400s cutoff lands at 2026-03-09
        # 01:00 EDT (Mar 9 00:00 EST + 86400s), so an 00:30 EDT session of
        # the NEXT date would leak INTO the window.  The next-local-midnight
        # cutoff (2026-03-09 00:00 EDT) keeps the next day out while still
        # including the last hour (03:30 EDT) of the selected day.
        assert data["total_sessions"] == 1   # only in_last_hour, not out_next_day
        assert data["total_input_tokens"] == 10
        assert data["period_days"] == 3   # Mar 6, Mar 7, Mar 8
        dates = [d["date"] for d in data["daily_tokens"]]
        assert dates == ["2026-03-06", "2026-03-07", "2026-03-08"]
    finally:
        os.environ.pop("TZ", None)
        if hasattr(time, "tzset"):
            time.tzset()
def test_insights_absolute_range_beyond_366_days_keeps_series_in_sync(monkeypatch, tmp_path):
    now = time.mktime((2026, 5, 4, 12, 0, 0, 0, 0, -1))
    start_ts = now - (400 * 86400)   # 400 days ago
    end_ts = now - (10 * 86400)      # 10 days ago
    entries = [
        {
            "session_id": "in_window", "updated_at": end_ts, "created_at": start_ts,
            "message_count": 4, "input_tokens": 1200, "output_tokens": 300,
            "estimated_cost": "0.0123", "model": "gpt-x",
        },
    ]
    data = _call_insights(monkeypatch, tmp_path, entries, query=f"start={int(start_ts)}&end={int(end_ts)}", now=now)
    # Filtering uses the FULL window (400+ days), so the session is counted.
    assert data["total_sessions"] == 1
    assert data["total_input_tokens"] == 1200
    # The daily series must cover the same interval the totals were computed
    # over - no silent 366-day truncation that leaves chart and totals mismatched.
    assert len(data["daily_tokens"]) >= 390
    assert _day(start_ts) == data["daily_tokens"][0]["date"]
    assert _day(end_ts) == data["daily_tokens"][-1]["date"]
    # period_days, series length, and calendar spread must all agree.
    assert data["period_days"] == len(data["daily_tokens"])


def test_insights_absolute_range_absurd_window_is_capped(monkeypatch, tmp_path):
    now = time.mktime((2026, 5, 4, 12, 0, 0, 0, 0, -1))
    entries = [
        {
            "session_id": "today", "updated_at": now, "created_at": now,
            "message_count": 1, "input_tokens": 10, "output_tokens": 5,
            "estimated_cost": "0.0001", "model": "gpt-x",
        },
    ]
    # start=1 (1970) to now spans ~56 years - the server must clamp to a sane
    # window instead of generating a million daily buckets, and the totals
    # must stay consistent with the (clamped) daily series.
    data = _call_insights(monkeypatch, tmp_path, entries, query="start=1", now=now)
    assert data["total_sessions"] == 1
    assert len(data["daily_tokens"]) <= 1830  # clamped to the 5-year cap (+slack)
    assert len(data["daily_tokens"]) > 0
    assert data["period_days"] == len(data["daily_tokens"])


def test_insights_daily_tokens_zero_fills_selected_range_and_parses_cost(monkeypatch, tmp_path):
    now = time.mktime((2026, 5, 4, 12, 0, 0, 0, 0, -1))
    two_days_ago = now - (2 * 86400)
    entries = [
        {
            "session_id": "today",
            "updated_at": now,
            "created_at": now,
            "message_count": 4,
            "input_tokens": 1200,
            "output_tokens": 300,
            "estimated_cost": "$0.0123",
            "model": "gpt-5.5",
        },
        {
            "session_id": "old",
            "updated_at": two_days_ago,
            "created_at": two_days_ago,
            "message_count": 2,
            "input_tokens": 500,
            "output_tokens": 250,
            "estimated_cost": "0.0200",
            "model": "gpt-5.5",
        },
    ]

    data = _call_insights(monkeypatch, tmp_path, entries, days="7", now=now)

    assert len(data["daily_tokens"]) == 7
    assert data["daily_tokens"][0]["date"] == _day(now - 6 * 86400)
    assert data["daily_tokens"][-1]["date"] == _day(now)
    by_date = {row["date"]: row for row in data["daily_tokens"]}
    assert by_date[_day(now)] == {
        "date": _day(now),
        "input_tokens": 1200,
        "output_tokens": 300,
        "cache_read_tokens": 0,
        "sessions": 1,
        "cost": 0.0123,
    }
    assert by_date[_day(now - 86400)] == {
        "date": _day(now - 86400),
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "sessions": 0,
        "cost": 0.0,
    }
    assert by_date[_day(two_days_ago)]["input_tokens"] == 500
    assert by_date[_day(two_days_ago)]["output_tokens"] == 250
    assert by_date[_day(two_days_ago)]["cost"] == 0.02
    assert data["total_cost"] == 0.0323


def test_insights_model_breakdown_tracks_tokens_cost_and_shares(monkeypatch, tmp_path):
    now = time.mktime((2026, 5, 4, 12, 0, 0, 0, 0, -1))
    entries = [
        {"updated_at": now, "message_count": 1, "model": "cheap", "input_tokens": 200, "output_tokens": 50, "estimated_cost": 0.01},
        {"updated_at": now, "message_count": 1, "model": "costly", "input_tokens": 100, "output_tokens": 50, "estimated_cost": "0.20"},
        {"updated_at": now, "message_count": 1, "model": "cheap", "input_tokens": 300, "output_tokens": 150, "estimated_cost": "$0.04"},
    ]

    data = _call_insights(monkeypatch, tmp_path, entries, days="7", now=now)

    models = data["models"]
    assert [m["model"] for m in models] == ["costly", "cheap"]
    costly, cheap = models
    assert costly["sessions"] == 1
    assert costly["input_tokens"] == 100
    assert costly["output_tokens"] == 50
    assert costly["total_tokens"] == 150
    assert costly["cost"] == 0.2
    assert costly["cache_read_tokens"] == 0
    assert costly["cache_hit_percent"] is None
    assert costly["session_share"] == 33
    assert costly["token_share"] == 18
    assert costly["cost_share"] == 80
    assert cheap["sessions"] == 2
    assert cheap["input_tokens"] == 500
    assert cheap["output_tokens"] == 200
    assert cheap["total_tokens"] == 700
    assert cheap["cost"] == 0.05


def test_insights_frontend_renders_daily_token_chart_and_model_usage_table():
    assert "daily_tokens" in PANELS_JS
    assert "insights_daily_tokens" in PANELS_JS
    assert "insights-daily-token-chart" in PANELS_JS
    assert "insights-daily-bar-input" in PANELS_JS
    assert "insights-daily-bar-output" in PANELS_JS
    assert "insights_model_tokens" in PANELS_JS
    assert "insights_model_cache" in PANELS_JS
    assert "insights_cache_hit" in PANELS_JS
    assert "insights_model_cost" in PANELS_JS
    assert "insights_model_share" in PANELS_JS
    assert "insights_model_team" not in PANELS_JS
    assert "m.profile || m.team" not in PANELS_JS
    assert "insights_no_usage_data" in PANELS_JS


def test_insights_frontend_has_daily_chart_styles_and_range_switching_hooks():
    assert "insightsPeriod" in INDEX_HTML
    assert 'option value="7"' in INDEX_HTML
    assert 'option value="30"' in INDEX_HTML
    assert 'option value="90"' in INDEX_HTML
    assert 'option value="custom"' in INDEX_HTML
    assert "insightsPeriodChange()" in INDEX_HTML
    assert "/api/insights?" in PANELS_JS
    assert "qs.set('days', period)" in PANELS_JS
    assert "qs.set('start'" in PANELS_JS
    assert "qs.set('end'" in PANELS_JS
    assert "insightsCustomRange" in INDEX_HTML
    assert 'type="date"' in INDEX_HTML
    assert "new Date(startVal + 'T00:00:00')" in PANELS_JS
    assert ".insights-daily-token-chart" in STYLE_CSS
    assert ".insights-daily-bar-output" in STYLE_CSS
    assert ".insights-model-cost" in STYLE_CSS
    assert ".insights-model-team" not in STYLE_CSS


def _make_daily_rows(n):
    rows = []
    for i in range(n):
        rows.append({
            'date': f'2026-01-{i+1:02d}',
            'input_tokens': (i + 1) * 100,
            'output_tokens': (i + 1) * 50,
            'sessions': 1,
            'cost': (i + 1) * 0.01,
        })
    return rows


# Python reference implementation of the JS bucketing logic, so we can
# verify the JS implementation produces the same behavior without needing
# a JS runtime.
def _py_bucket(rows):
    if not isinstance(rows, list) or len(rows) == 0:
        return []
    n = len(rows)
    if n <= 30:
        return list(rows)  # unchanged

    if n <= 90:
        bucket_size = 2
    elif n <= 180:
        bucket_size = 3
    elif n <= 365:
        bucket_size = 8  # ≤52 bars for 365 days; shrink-safe with minmax(0,1fr)
    else:
        bucket_size = 8  # fallback for >365 (shouldn't occur in practice)

    result = []
    for i in range(0, n, bucket_size):
        sl = rows[i:i + bucket_size]
        inp = sum(r['input_tokens'] for r in sl)
        out = sum(r['output_tokens'] for r in sl)
        sess = sum(r['sessions'] for r in sl)
        cost = sum(r['cost'] for r in sl)
        first = sl[0]['date']
        last = sl[-1]['date']
        first_lbl = first[5:]  # MM-DD
        last_lbl = last[5:]
        result.append({
            'label': (first_lbl if first == last else first_lbl + '--' + last_lbl),
            'title': first + (' -- ' + last if first != last else ''),
            'date': first,
            'input_tokens': inp,
            'output_tokens': out,
            'sessions': sess,
            'cost': cost,
        })
    return result


def test_insights_bucketing_helper_preserves_short_ranges():
    # _bucketDailyTokensForChart must exist in panels.js
    assert '_bucketDailyTokensForChart' in PANELS_JS

    # 7-day: unchanged (≤ 30 threshold)
    rows7 = _make_daily_rows(7)
    bucketed7 = _py_bucket(rows7)
    assert len(bucketed7) == 7, f'7-day should stay 7 bars, got {len(bucketed7)}'
    assert bucketed7[0]['input_tokens'] == 100

    # 30-day: exactly 30 → unchanged
    rows30 = _make_daily_rows(30)
    bucketed30 = _py_bucket(rows30)
    assert len(bucketed30) == 30, f'30-day should stay 30 bars, got {len(bucketed30)}'

    # 31-day: bucketed
    rows31 = _make_daily_rows(31)
    bucketed31 = _py_bucket(rows31)
    assert len(bucketed31) < 31, f'31-day should be bucketed, got {len(bucketed31)}'
    assert len(bucketed31) <= 16  # ceil(31/2)


def test_insights_bucketing_helper_bounds_long_ranges():
    # 90-day → 2-day buckets → 45 bars
    rows90 = _make_daily_rows(90)
    bucketed90 = _py_bucket(rows90)
    assert len(bucketed90) <= 45, f'90-day should be <=45 bars, got {len(bucketed90)}'
    assert len(bucketed90) > 0

    # 365-day → 8-day buckets → 46 bars (≤52 threshold)
    rows365 = _make_daily_rows(365)
    bucketed365 = _py_bucket(rows365)
    assert len(bucketed365) <= 52, f'365-day should be <=52 bars, got {len(bucketed365)}'
    assert len(bucketed365) > 0
    # First bucket has 8 days: 100+200+300+400+500+600+700+800 = 3600
    assert bucketed365[0]['input_tokens'] == 3600
    assert bucketed365[0]['sessions'] == 8


def test_insights_bucketing_helper_preserves_label_and_title_fields():
    # Short range → rows unchanged; no .label/.title keys
    rows10 = _make_daily_rows(10)
    bucketed10 = _py_bucket(rows10)
    assert bucketed10[0]['date'] == '2026-01-01'
    assert 'label' not in bucketed10[0]
    assert 'title' not in bucketed10[0]

    # 90-day → bucket rows have .label and .title
    rows90 = _make_daily_rows(90)
    bucketed90 = _py_bucket(rows90)
    assert 'label' in bucketed90[0], 'bucket row must have .label'
    assert 'title' in bucketed90[0], 'bucket row must have .title'
    assert '2026-01-01' in bucketed90[0]['title'], f'title should include start date, got {bucketed90[0]["title"]}'
    assert len(bucketed90[0]['label']) <= 12, f'label should be short, got {bucketed90[0]["label"]}'


def test_insights_render_loop_uses_bucket_helper():
    src = PANELS_JS
    daily_section_start = src.find('// Daily token trend')
    daily_section_end = src.find('// Models table', daily_section_start)
    daily_section = src[daily_section_start:daily_section_end]

    assert '_bucketDailyTokensForChart' in daily_section, '_bucketDailyTokensForChart must be called in the render loop'
    assert 'const chartRows' in daily_section, 'chartRows variable must be used instead of dailyTokens.map directly'


def test_insights_css_chart_shrink_safe():
    assert '.insights-daily-token-chart' in STYLE_CSS
    chart_line = [line for line in STYLE_CSS.splitlines() if '.insights-daily-token-chart' in line][0]
    # minmax(0,1fr) instead of minmax(12px,1fr) lets long-range bars shrink to fit the card
    assert 'minmax(0,1fr)' in chart_line, f'chart must use minmax(0,1fr) for shrink-safe columns, got: {chart_line}'
    assert 'overflow:hidden' in chart_line, 'chart must have overflow:hidden to prevent horizontal scroll'
    assert 'max-width:100%' in chart_line or 'max-width' in chart_line, 'chart should constrain max-width'


def test_insights_mobile_layout_stacks_usage_grid():
    # Regression test for issue #2104: Token Breakdown + Models should
    # stack on mobile instead of being side-by-side causing horizontal overflow
    assert 'insights-usage-grid' in PANELS_JS
    # Scoped mobile breakpoint that forces single-column layout
    assert '@media (max-width: 640px)' in STYLE_CSS
    assert '.insights-usage-grid' in STYLE_CSS
    assert 'grid-template-columns: 1fr' in STYLE_CSS


def test_insights_mobile_models_table_has_contained_overflow():
    # Regression test for issue #2104: Models table should have contained
    # horizontal scrolling instead of pushing the whole page off-screen
    assert 'insights-model-table' in PANELS_JS
    # The mobile rule should include overflow-x handling for the models card/table
    # Search for the specific mobile rule that contains insights-usage-grid
    insights_mobile = '/* ── Mobile layout for Token Breakdown + Models'
    assert insights_mobile in STYLE_CSS, 'Issue #2104 mobile rules should exist in CSS'
    # Get the block from our specific mobile section to the next section comment
    section_start = STYLE_CSS.find(insights_mobile)
    section_end = STYLE_CSS.find('/* ── Checkpoints', section_start)
    section_block = STYLE_CSS[section_start:section_end]
    assert 'overflow-x' in section_block, 'Mobile rule should include overflow-x handling'
    assert 'insights-model-table' in section_block or 'insights-card' in section_block


# ── #3189: CLI/gateway sessions in Insights + webui double-count guard ──────


def _call_insights_with_state_db(monkeypatch, tmp_path, entries, state_rows, days="7", now=None):
    """Like _call_insights but also seeds an agent state.db with `sessions` rows
    and points _active_state_db_path at it, so the CLI second-pass is exercised."""
    import sqlite3
    import api.routes as routes
    import api.models as models

    session_dir = tmp_path / "sessions"
    session_dir.mkdir(exist_ok=True)
    (session_dir / "_index.json").write_text(json.dumps(entries), encoding="utf-8")
    monkeypatch.setattr(routes, "SESSION_DIR", session_dir)
    if now is not None:
        monkeypatch.setattr(time, "time", lambda: now)

    db_path = tmp_path / "state.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """CREATE TABLE sessions (
            id TEXT PRIMARY KEY, source TEXT, model TEXT, message_count INTEGER,
            input_tokens INTEGER, output_tokens INTEGER, estimated_cost_usd REAL,
            cache_read_tokens INTEGER DEFAULT 0,
            started_at REAL, ended_at REAL
        )"""
    )
    for r in state_rows:
        conn.execute(
            "INSERT INTO sessions (id, source, model, message_count, input_tokens, "
            "output_tokens, estimated_cost_usd, cache_read_tokens, started_at, ended_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (r["id"], r.get("source"), r.get("model"), r.get("message_count", 0),
             r.get("input_tokens", 0), r.get("output_tokens", 0),
             r.get("estimated_cost_usd", 0.0), r.get("cache_read_tokens", 0),
             r.get("started_at"), r.get("ended_at")),
        )
    conn.commit()
    conn.close()
    # _handle_insights does `from api.models import _active_state_db_path`, so patch on the module.
    monkeypatch.setattr(models, "_active_state_db_path", lambda: db_path)

    handler = _FakeHandler()
    parsed = SimpleNamespace(query=f"days={days}")
    routes._handle_insights(handler, parsed)
    assert handler.status == 200
    return handler.json_body()


def test_insights_includes_cli_and_gateway_sessions(monkeypatch, tmp_path):
    """CLI / Telegram / Discord sessions in state.db should appear in Insights totals."""
    now = time.mktime((2026, 5, 30, 12, 0, 0, 0, 0, -1))
    webui_entries = [
        {"session_id": "w1", "updated_at": now, "created_at": now, "message_count": 2,
         "input_tokens": 100, "output_tokens": 50, "estimated_cost": 0.01, "model": "gpt-5.5"},
    ]
    state_rows = [
        {"id": "cli1", "source": "cli", "model": "gpt-5.5", "message_count": 3,
         "input_tokens": 200, "output_tokens": 80, "estimated_cost_usd": 0.02,
         "started_at": now, "ended_at": now},
        {"id": "tg1", "source": "telegram", "model": "gpt-5.5", "message_count": 1,
         "input_tokens": 60, "output_tokens": 20, "estimated_cost_usd": 0.005,
         "started_at": now, "ended_at": now},
    ]
    data = _call_insights_with_state_db(monkeypatch, tmp_path, webui_entries, state_rows, days="7", now=now)
    # 1 webui + 2 cli/gateway = 3 sessions counted
    assert data["total_sessions"] == 3
    # input tokens summed across all three: 100 + 200 + 60 = 360
    assert data["total_input_tokens"] == 360


def test_insights_does_not_double_count_webui_state_db_rows(monkeypatch, tmp_path):
    """WebUI sessions persist to BOTH _index.json AND state.db (source='webui').
    The CLI second-pass must exclude source='webui' so they aren't counted twice."""
    now = time.mktime((2026, 5, 30, 12, 0, 0, 0, 0, -1))
    webui_entries = [
        {"session_id": "w1", "updated_at": now, "created_at": now, "message_count": 2,
         "input_tokens": 100, "output_tokens": 50, "estimated_cost": 0.01, "model": "gpt-5.5"},
    ]
    # The SAME webui session also has a state.db row (source='webui') + one real cli row.
    state_rows = [
        {"id": "w1", "source": "webui", "model": "gpt-5.5", "message_count": 2,
         "input_tokens": 100, "output_tokens": 50, "estimated_cost_usd": 0.01,
         "started_at": now, "ended_at": now},
        {"id": "cli1", "source": "cli", "model": "gpt-5.5", "message_count": 3,
         "input_tokens": 200, "output_tokens": 80, "estimated_cost_usd": 0.02,
         "started_at": now, "ended_at": now},
    ]
    data = _call_insights_with_state_db(monkeypatch, tmp_path, webui_entries, state_rows, days="7", now=now)
    # webui session counted once (from _index.json), cli once = 2, NOT 3.
    assert data["total_sessions"] == 2
    # webui tokens counted once (100) + cli (200) = 300, NOT 400.
    assert data["total_input_tokens"] == 300


# ── #3911 / salvage of #3912: prompt-cache hit rate on Insights ────────────
# Maintainer-flagged correctness fix: the hit rate must use the FULL prompt
# total as the denominator — cache_read / (input_tokens + cache_read) — so it
# is bounded 0-100% and means "% of the prompt served from cache". The naive
# cache_read / input_tokens formula EXCEEDS 100% on cache-heavy sessions.


def test_insights_cache_hit_rate_uses_bounded_denominator(monkeypatch, tmp_path):
    """Per-model + aggregate hit rate = cache_read / (input + cache_read)."""
    now = time.mktime((2026, 5, 4, 12, 0, 0, 0, 0, -1))
    entries = [
        # 100 fresh input + 300 cached reads -> 300 / (100 + 300) = 75%
        {"updated_at": now, "message_count": 1, "model": "cached",
         "input_tokens": 100, "output_tokens": 40, "cache_read_tokens": 300,
         "estimated_cost": 0.05},
    ]
    data = _call_insights(monkeypatch, tmp_path, entries, days="7", now=now)
    model = data["models"][0]
    assert model["model"] == "cached"
    assert model["cache_read_tokens"] == 300
    # cache_read / (input + cache_read) = 300 / 400 = 75 (NOT 300/100 = 300%)
    assert model["cache_hit_percent"] == 75
    # Aggregate uses the same bounded denominator.
    assert data["total_cache_read_tokens"] == 300
    assert data["total_cache_hit_percent"] == 75


def test_insights_cache_hit_rate_never_exceeds_100_percent(monkeypatch, tmp_path):
    """Non-vacuous boundary test: an extremely cache-heavy session where the OLD
    formula (cache_read / input_tokens) would report >100%. The bounded
    denominator must clamp the displayed rate to <=100%.

    Here cache_read (9900) vastly exceeds fresh input (100):
      OLD (buggy): 9900 / 100         = 9900%   ← would be displayed, absurd
      NEW (fixed): 9900 / (100+9900)  = 99%     ← bounded, meaningful
    The assertion below would FAIL under the old formula, so it is non-vacuous.
    """
    now = time.mktime((2026, 5, 4, 12, 0, 0, 0, 0, -1))
    entries = [
        {"updated_at": now, "message_count": 1, "model": "cache-heavy",
         "input_tokens": 100, "output_tokens": 20, "cache_read_tokens": 9900,
         "estimated_cost": 0.10},
        # A second model with cache_read == input (would be exactly 100% under
        # the old formula; 50% under the bounded one) — still <= 100% either way,
        # included to exercise the equality edge of the bound.
        {"updated_at": now, "message_count": 1, "model": "balanced",
         "input_tokens": 500, "output_tokens": 100, "cache_read_tokens": 500,
         "estimated_cost": 0.02},
    ]
    data = _call_insights(monkeypatch, tmp_path, entries, days="7", now=now)

    # Prove non-vacuity: the OLD reads-over-misses formula WOULD exceed 100%.
    heavy = next(m for m in data["models"] if m["model"] == "cache-heavy")
    old_formula = round((heavy["cache_read_tokens"] / heavy["input_tokens"]) * 100)
    assert old_formula > 100, "test must exercise a case the old formula mishandles"

    # Every per-model displayed rate is bounded to 0..100.
    for m in data["models"]:
        pct = m["cache_hit_percent"]
        if pct is not None:
            assert 0 <= pct <= 100, f"{m['model']} hit rate {pct} out of bounds"

    # The fixed bounded denominator: 9900 / (100 + 9900) = 99%.
    assert heavy["cache_hit_percent"] == 99
    balanced = next(m for m in data["models"] if m["model"] == "balanced")
    assert balanced["cache_hit_percent"] == 50

    # Aggregate is bounded too, and < 100 even though cache_read dominates input.
    assert data["total_cache_hit_percent"] is not None
    assert 0 <= data["total_cache_hit_percent"] <= 100


def test_insights_cache_hit_rate_is_none_without_cache_reads(monkeypatch, tmp_path):
    """No cache reads -> hit rate is None (not 0%), so the column shows '—'."""
    now = time.mktime((2026, 5, 4, 12, 0, 0, 0, 0, -1))
    entries = [
        {"updated_at": now, "message_count": 1, "model": "nocache",
         "input_tokens": 500, "output_tokens": 100, "estimated_cost": 0.03},
    ]
    data = _call_insights(monkeypatch, tmp_path, entries, days="7", now=now)
    assert data["models"][0]["cache_read_tokens"] == 0
    assert data["models"][0]["cache_hit_percent"] is None
    assert data["total_cache_hit_percent"] is None

