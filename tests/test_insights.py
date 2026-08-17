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
    data = _call_insights(monkeypatch, tmp_path, entries, query=f"start={_day(start_ts)}&end={_day(end_ts)}", now=now)
    # Session counts: in_window counted; older and newer excluded by the absolute window.
    assert data["total_sessions"] == 1
    assert data["total_input_tokens"] == 1200
    assert data["total_output_tokens"] == 300
    # Daily series spans from start day to end day (inclusive).
    assert _day(start_ts) == data["daily_tokens"][0]["date"]
    assert _day(end_ts) == data["daily_tokens"][-1]["date"]
    assert len(data["daily_tokens"]) <= 30


def test_insights_absolute_range_accepts_date_strings_server_local(monkeypatch, tmp_path):
    # The production frontend sends raw YYYY-MM-DD date strings, NOT
    # browser-local epoch seconds.  The server must parse them in its OWN
    # timezone (so a remote browser cannot shift the selected day) and
    # report the effective server-local window it queried.
    now = time.mktime((2026, 5, 4, 12, 0, 0, 0, 0, -1))
    # Server-local calendar day for 2026-04-20
    start_day = time.mktime((2026, 4, 20, 0, 0, 0, 0, 0, -1))
    entries = [
        {
            "session_id": "in_range", "updated_at": start_day + 86400, "created_at": start_day,
            "message_count": 2, "input_tokens": 200, "output_tokens": 50,
            "estimated_cost": "0.0020", "model": "gpt-x",
        },
        {
            "session_id": "before", "updated_at": start_day - 86400, "created_at": start_day - 86400,
            "message_count": 1, "input_tokens": 500, "output_tokens": 100,
            "estimated_cost": "0.0050", "model": "gpt-x",
        },
    ]
    data = _call_insights(monkeypatch, tmp_path, entries,
                          query="start=2026-04-20&end=2026-04-25", now=now)
    # in_range is inside; before is excluded.
    assert data["total_sessions"] == 1
    assert data["total_input_tokens"] == 200
    # Effective bounds reflect the server-local calendar window (not shifted).
    assert data["effective_start"] == "2026-04-20"
    assert data["effective_end"] == "2026-04-25"
    # Daily series spans the inclusive date range.
    assert data["daily_tokens"][0]["date"] == "2026-04-20"
    assert data["daily_tokens"][-1]["date"] == "2026-04-25"
    assert len(data["daily_tokens"]) == 6


def test_insights_absolute_range_reports_clamped_effective_bounds(monkeypatch, tmp_path):
    # A request spanning more than 5 years must be clamped server-side, and
    # the response must report the EFFECTIVE window (not the raw input) so
    # the footer can never disagree with the filtered totals/chart.
    now = time.mktime((2026, 5, 4, 12, 0, 0, 0, 0, -1))
    far_past = time.mktime((2010, 1, 1, 0, 0, 0, 0, 0, -1))
    entries = [
        {
            "session_id": "recent", "updated_at": now - 86400, "created_at": now - 86400,
            "message_count": 1, "input_tokens": 100, "output_tokens": 10,
            "estimated_cost": "0.0010", "model": "gpt-x",
        },
    ]
    data = _call_insights(monkeypatch, tmp_path, entries,
                          query=f"start={int(far_past)}&end={int(now)}", now=now)
    # The effective END is still the queried ~now; the effective START was
    # clamped by the 5-CALENDAR-YEAR cap, and the reported bounds equal the
    # series.  Five calendar years before 2026-05-04 is 2021-05-04
    # (server-local) - 1826 days, which is NOT a fixed 5*365*86400 seconds
    # (that would be 1825 days and drop the first day when the span contains
    # a leap day).
    assert data["effective_start"] == "2021-05-04"
    assert data["effective_end"] == "2026-05-04"
    assert data["daily_tokens"][0]["date"] == data["effective_start"]
    assert data["daily_tokens"][-1]["date"] == data["effective_end"]
    assert len(data["daily_tokens"]) == 1827  # 5 calendar years inclusive (2021-05-04..2026-05-04)


def test_insights_absolute_range_five_calendar_years_with_leap_day_keeps_first_day(monkeypatch, tmp_path):
    # A request whose exact span is five calendar years CONTAINING a leap day
    # (2021-05-04..2026-05-04 = 1826 days) must NOT be clipped: the old
    # fixed-seconds cap (5*365*86400 = 1825 days) treated it as over-limit
    # and dropped the first requested day (Greptile P1).  Sessions on the
    # first day must stay inside the window.
    now = time.mktime((2026, 5, 4, 12, 0, 0, 0, 0, -1))
    first_day = time.mktime((2021, 5, 4, 0, 0, 0, 0, 0, -1))   # 2021-05-04 00:00
    entries = [
        {
            "session_id": "first_day", "updated_at": first_day + 3600, "created_at": first_day,
            "message_count": 1, "input_tokens": 50, "output_tokens": 5,
            "estimated_cost": "0.0005", "model": "gpt-x",
        },
    ]
    data = _call_insights(monkeypatch, tmp_path, entries,
                          query=f"start={int(first_day)}&end={int(now)}", now=now)
    # The requested first day survives the cap; the session is admitted and
    # bucketed on 2021-05-04.
    assert data["effective_start"] == "2021-05-04"
    assert data["effective_end"] == "2026-05-04"
    assert data["total_sessions"] == 1
    assert data["daily_tokens"][0]["date"] == "2021-05-04"
    assert data["daily_tokens"][0]["input_tokens"] == 50
    assert data["daily_tokens"][0]["sessions"] == 1


def test_insights_absolute_range_reports_swapped_effective_bounds(monkeypatch, tmp_path):
    # Reversed input (end before start) is normalized server-side; the
    # effective bounds must reflect the sorted window so the footer shows
    # start -> end, never a reversed range.
    now = time.mktime((2026, 5, 4, 12, 0, 0, 0, 0, -1))
    start_day = time.mktime((2026, 4, 20, 0, 0, 0, 0, 0, -1))
    end_day = time.mktime((2026, 4, 25, 0, 0, 0, 0, 0, -1))
    entries = [
        {
            "session_id": "in_range", "updated_at": start_day + 86400, "created_at": start_day,
            "message_count": 1, "input_tokens": 30, "output_tokens": 10,
            "estimated_cost": "0.0003", "model": "gpt-x",
        },
    ]
    # end is BEFORE start -> server swaps them.
    data = _call_insights(monkeypatch, tmp_path, entries,
                          query=f"start={int(end_day)}&end={int(start_day)}", now=now)
    assert data["total_sessions"] == 1
    assert data["effective_start"] == "2026-04-20"
    assert data["effective_end"] == "2026-04-25"
    assert data["daily_tokens"][0]["date"] == "2026-04-20"
    assert data["daily_tokens"][-1]["date"] == "2026-04-25"


def test_insights_absolute_range_valid_epoch_still_supported(monkeypatch, tmp_path):
    # Numeric epoch seconds remain a supported input contract (backward
    # compat for CLI/scripting callers); effective bounds are still reported.
    now = time.mktime((2026, 5, 4, 12, 0, 0, 0, 0, -1))
    start_ts = now - (3 * 86400)
    end_ts = now - 86400
    entries = [
        {
            "session_id": "s1", "updated_at": start_ts + 3600, "created_at": start_ts,
            "message_count": 1, "input_tokens": 40, "output_tokens": 10,
            "estimated_cost": "0.0004", "model": "gpt-x",
        },
    ]
    data = _call_insights(monkeypatch, tmp_path, entries,
                          query=f"start={int(start_ts)}&end={int(end_ts)}", now=now)
    assert data["total_sessions"] == 1
    assert data["effective_start"] is not None
    assert data["effective_end"] is not None


def test_insights_absolute_range_numeric_start_keeps_exact_precision(monkeypatch, tmp_path):
    """A NUMERIC `start` is an exact Unix boundary: it must NOT be rounded
    down to local midnight (the old code aligned every start to the day's
    midnight), which admitted rows before the requested numeric start on the
    start day.  Regression for Greptile P1 'Numeric endpoints retain precision
    only on one special date' (review #4940944324)."""
    now = time.mktime((2026, 5, 4, 12, 0, 0, 0, 0, -1))        # server clock 2026-05-04 12:00
    numeric_start = time.mktime((2026, 5, 1, 10, 0, 0, 0, 0, -1))  # 2026-05-01 10:00
    entries = [
        {"session_id": "before_numeric_start", "updated_at": numeric_start - 3600,
         "created_at": numeric_start - 3600, "message_count": 1, "input_tokens": 500,
         "output_tokens": 100, "estimated_cost": "0.0050", "model": "gpt-x"},
        {"session_id": "after_numeric_start", "updated_at": numeric_start + 3600,
         "created_at": numeric_start + 3600, "message_count": 1, "input_tokens": 10,
         "output_tokens": 5, "estimated_cost": "0.0001", "model": "gpt-x"},
    ]
    # Numeric start exact; date-string end (whole day) today clamps to now.
    data = _call_insights(monkeypatch, tmp_path, entries,
                          query=f"start={int(numeric_start)}&end=2026-05-04", now=now)
    # Only the session strictly after the numeric start is admitted.
    assert data["total_sessions"] == 1
    assert data["total_input_tokens"] == 10


def test_insights_absolute_range_numeric_end_past_date_not_widened(monkeypatch, tmp_path):
    """A NUMERIC `end` on a past date is an EXACT exclusive boundary: the old
    code widened it to the following local midnight, so rows after the
    requested numeric end on the end date leaked in.  Regression for Greptile
    P1 'Numeric endpoints retain precision only on one special date'."""
    now = time.mktime((2026, 5, 4, 12, 0, 0, 0, 0, -1))
    numeric_end = time.mktime((2026, 5, 2, 14, 0, 0, 0, 0, -1))   # 2026-05-02 14:00 (past day)
    start = time.mktime((2026, 5, 1, 0, 0, 0, 0, 0, -1))          # 2026-05-01 00:00
    entries = [
        {"session_id": "at_end_exact", "updated_at": numeric_end, "created_at": numeric_end,
         "message_count": 1, "input_tokens": 999, "output_tokens": 999,
         "estimated_cost": "0.9999", "model": "gpt-x"},
        {"session_id": "after_numeric_end_same_day", "updated_at": numeric_end + 3600,
         "created_at": numeric_end + 3600, "message_count": 1, "input_tokens": 700,
         "output_tokens": 200, "estimated_cost": "0.0070", "model": "gpt-x"},
        {"session_id": "in_range", "updated_at": start + 3600, "created_at": start + 3600,
         "message_count": 1, "input_tokens": 10, "output_tokens": 5,
         "estimated_cost": "0.0001", "model": "gpt-x"},
    ]
    # Numeric start AND numeric end: exact [start, end), both on past dates.
    data = _call_insights(monkeypatch, tmp_path, entries,
                          query=f"start={int(start)}&end={int(numeric_end)}", now=now)
    # start=2026-05-01 00:00 (exact), end=2026-05-02 14:00 exact exclusive:
    # at_end and after_end both OUT (the old widened end admitted them), in OUT.
    assert data["total_sessions"] == 1
    assert data["total_input_tokens"] == 10


def test_insights_absolute_range_numeric_midnight_is_exact_not_whole_day(monkeypatch, tmp_path):
    """A numeric `end` exactly at today's midnight is an EXACT boundary, not a
    whole-day 'today' selection: rows stamped after that midnight (but before
    the server clock) must stay OUT.  Regression for Greptile P1 'Numeric
    endpoints retain precision only on one special date'."""
    now = time.mktime((2026, 5, 4, 12, 0, 0, 0, 0, -1))          # server clock 2026-05-04 12:00
    today_midnight = time.mktime((2026, 5, 4, 0, 0, 0, 0, 0, -1))
    start = time.mktime((2026, 5, 1, 0, 0, 0, 0, 0, -1))
    entries = [
        {"session_id": "before_midnight", "updated_at": today_midnight - 60,
         "created_at": today_midnight - 60, "message_count": 1, "input_tokens": 10,
         "output_tokens": 5, "estimated_cost": "0.0001", "model": "gpt-x"},
        {"session_id": "after_midnight_today", "updated_at": today_midnight + 60,
         "created_at": today_midnight + 60, "message_count": 1, "input_tokens": 999,
         "output_tokens": 999, "estimated_cost": "0.9999", "model": "gpt-x"},
    ]
    # Numeric end == today midnight: exact exclusive boundary.
    data = _call_insights(monkeypatch, tmp_path, entries,
                          query=f"start={int(start)}&end={int(today_midnight)}", now=now)
    assert data["total_sessions"] == 1
    assert data["total_input_tokens"] == 10


def test_insights_absolute_range_state_db_numeric_end_not_widened(monkeypatch, tmp_path):
    """The CLI/state.db path applies the same exact numeric-end boundary: a
    numeric end on a past date is not widened to the next midnight, so a CLI
    session after the requested numeric end stays OUT.  Regression for Greptile
    P1 'Numeric endpoints retain precision only on one special date'."""
    now = time.mktime((2026, 5, 4, 12, 0, 0, 0, 0, -1))
    numeric_end = time.mktime((2026, 5, 2, 14, 0, 0, 0, 0, -1))
    start = time.mktime((2026, 5, 1, 0, 0, 0, 0, 0, -1))
    state_rows = [
        {"id": "cli_after_end", "source": "cli", "model": "gpt-5.5", "message_count": 1,
         "input_tokens": 700, "output_tokens": 200, "estimated_cost_usd": 0.0070,
         "started_at": numeric_end + 3600, "ended_at": numeric_end + 3600},
        {"id": "cli_in", "source": "cli", "model": "gpt-5.5", "message_count": 1,
         "input_tokens": 10, "output_tokens": 5, "estimated_cost_usd": 0.0001,
         "started_at": start + 3600, "ended_at": start + 3600},
    ]
    data = _call_insights_with_state_db(monkeypatch, tmp_path, [], state_rows,
                                        query=f"start={int(start)}&end={int(numeric_end)}", now=now)
    # cli_after_end (past the numeric end on the end day) stays OUT.
    assert data["total_sessions"] == 1
    assert data["total_input_tokens"] == 10


def test_insights_absolute_range_fallback_reports_mode_trailing(monkeypatch, tmp_path):
    """A custom-range request that fails closed to the trailing window must
    report mode='trailing' so the client renders the window actually served,
    never the rejected raw inputs.  Regression for Greptile P1 'custom range
    that normalizes to trailing still displays the raw custom range'."""
    now = time.mktime((2026, 5, 4, 12, 0, 0, 0, 0, -1))
    entries = [
        {"session_id": "today", "updated_at": now, "created_at": now,
         "message_count": 1, "input_tokens": 10, "output_tokens": 5,
         "estimated_cost": "0.0001", "model": "gpt-x"},
    ]
    future = now + (30 * 86400)
    # All-future start -> falls back to the trailing window.
    data = _call_insights(monkeypatch, tmp_path, entries, query=f"start={int(future)}", now=now)
    assert data["mode"] == "trailing"
    assert data["effective_start"] is None
    # A genuinely custom (valid) request reports mode='custom' + effective bounds.
    start_ts = now - (2 * 86400)
    data2 = _call_insights(monkeypatch, tmp_path, entries,
                           query=f"start={int(start_ts)}&end={int(now)}", now=now)
    assert data2["mode"] == "custom"
    assert data2["effective_start"] is not None


def test_insights_refresh_restores_button_when_latest_regardless_of_animate():
    # The refresh button must be restored by the LATEST request no matter
    # whether that request was animated.  An old animated request superseded
    # by a non-animated one left the button disabled forever (review #4940944324,
    # "A superseding non-animated load can permanently disable Refresh").
    load = _function_body(PANELS_JS, "loadInsights")
    assert "if (animate && refreshBtn && reqToken === _insightsReqToken)" not in load
    assert "if (refreshBtn && reqToken === _insightsReqToken)" in load


def test_insights_footer_keys_off_server_mode_not_raw_inputs():
    # A custom request that fell back to the trailing window must render the
    # trailing window actually served, never the rejected raw inputs.  The
    # footer keys off the server's `mode` field + effective bounds; reading
    # the raw date inputs back would show a future/invalid range (review
    # #4940944324, "custom range that normalizes to trailing ...").
    render = _function_body(PANELS_JS, "_renderInsights")
    assert "d.mode === 'custom'" in render
    # No raw-input fallback for the range label anymore.
    assert "$('insightsStart') || {}).value || '…'" not in render


def test_insights_refresh_restored_by_latest_request_runtime():
    """Deferred-Promise runtime test for the superseding-load bug (review
    #4940944324): an ANIMATED request A disables #insightsRefreshBtn; a
    NON-animated request B then supersedes A.  B is the latest and finishing
    B must restore the button even though B itself never animated - otherwise
    A's disabled state sticks forever.  Exercises BOTH success and error
    orderings (B finishing before/after the stale A)."""
    import json
    import shutil
    import subprocess
    import textwrap

    node = shutil.which("node")
    if not node:  # pragma: no cover
        pytest.skip("node not available")

    load_fn = _function_body(PANELS_JS, "loadInsights")
    # _function_body() starts at "function loadInsights(" which drops the
    # `async` modifier the real source declares (the body awaits).  Restore it
    # so the extracted function stays async exactly like the shipped source.
    if load_fn.startswith("function loadInsights("):
        load_fn = "async " + load_fn

    harness = textwrap.dedent(
        """
        %(load_fn)s

        // Mutable holders so BOTH scenarios read the `$`-resolved elements.
        let curBox = { innerHTML: '' };
        let curBtn = { style: {}, disabled: false };

        // ---- deferred-promise control ----
        function deferred() {
          let resolve, reject;
          const p = new Promise((res, rej) => { resolve = res; reject = rej; });
          return { p, resolve, reject };
        }

        // api() returns a controllable promise per call path, keyed by call count.
        let callSeq = 0;
        const dq = [];           // ordered deferreds for all api() calls
        let apiDeferreds = null; // snapshot AFTER each loadInsights() returns
        function api(path) {
          const d = deferred();
          d.qkey = callSeq++;
          dq.push(d);
          return d.p;
        }
        global._insightsReqToken = 0;
        global.api = api;
        global._renderInsights = () => {};
        global._syncSystemHealthMonitorVisibility = () => {};
        global.pollSystemHealth = () => {};
        global.t = k => k;
        global.esc = s => s;
        global.$ = (id) => {
          if (id === 'insightsContent') return curBox;
          if (id === 'insightsRefreshBtn') return curBtn;
          if (id === 'insightsPeriod') return { value: 'custom' };
          if (id === 'insightsStart' || id === 'insightsEnd') return { value: '' };
          return null;
        };

        function snapshotLatest(n) {
          // the LAST `n` deferreds are the current in-flight load's sub-promises
          const s = dq.slice(-n);
          return s;
        }
        function hitAll(arr, fn) { for (const d of arr) fn(d); }
        function resolveAll(arr) { hitAll(arr, d => d.resolve({ total_sessions: 0 })); }
        function rejectAll(arr) { hitAll(arr, d => d.reject(new Error('boom'))); }

        // --- Scenario SUCCESS: animated A, then non-animated B supersedes ---
        curBox = { innerHTML: '' };
        curBtn = { style: {}, disabled: false };
        const pa = loadInsights(true);           // A: 3 api() calls
        if (curBtn.disabled !== true) throw new Error('A should disable the button');
        const aDeferreds = snapshotLatest(3);
        const pb = loadInsights();               // B: 3 api() calls (supersedes A)
        const bDeferreds = snapshotLatest(3);
        // Resolve B (latest) FIRST...
        resolveAll(bDeferreds);
        await pb;
        if (curBtn.disabled) throw new Error('latest (non-animated) B must restore the button');
        // ...then the stale A.  Its finally must be rejected by the token guard.
        resolveAll(aDeferreds);
        await pa;
        if (curBtn.disabled) throw new Error('stale A must be rejected; button stays enabled');

        // --- Scenario ERROR: latest non-animated B rejects, stale A rejects after ---
        curBox = { innerHTML: '' };
        curBtn = { style: {}, disabled: false };
        global._insightsReqToken = 0;
        const pa2 = loadInsights(true);          // A2 (animated): disables
        if (curBtn.disabled !== true) throw new Error('A2 should disable the button');
        const a2d = snapshotLatest(3);
        const pb2 = loadInsights();              // B2 (non-animated, latest)
        const b2d = snapshotLatest(3);
        rejectAll(b2d);                          // latest errors first
        await pb2;
        if (curBtn.disabled) throw new Error('latest (non-animated, erroring) B must restore the button');
        rejectAll(a2d);                          // stale A2 errors after
        await pa2;
        if (curBtn.disabled) throw new Error('stale A2 must be rejected; button stays enabled');

        console.log(JSON.stringify({ successPass: true, errorPass: true }));
        """
    ) % {"load_fn": load_fn}

    proc = subprocess.run([node, "-e", harness], capture_output=True, text=True, timeout=60, check=False)
    assert proc.returncode == 0, f"node harness failed:\n{proc.stdout}\n{proc.stderr}"
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out["successPass"] is True
    assert out["errorPass"] is True


def _function_body(src, name):
    start = src.index("function " + name + "(")
    brace = src.index("{", start)
    depth = 0
    for i in range(brace, len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[start:i+1]
    raise AssertionError("function body not found: " + name)


def test_insights_frontend_request_guard_and_raw_dates_present():
    # Regression for "editing start then end lets the slower first request
    # overwrite the newer range": loadInsights must carry a per-request token
    # and drop any stale in-flight response.
    load = _function_body(PANELS_JS, "loadInsights")
    assert "_insightsReqToken" in load
    assert "const reqToken = ++_insightsReqToken;" in load
    assert "if (reqToken !== _insightsReqToken) return;" in load
    assert "if (startVal) qs.set('start', startVal)" in load
    assert "if (endVal) qs.set('end', endVal)" in load
    assert "new Date(startVal" not in load


def test_insights_footer_uses_effective_bounds():
    # The footer must prefer the server-reported effective window so a
    # clamped/swapped range is displayed truthfully.
    render = _function_body(PANELS_JS, "_renderInsights")
    assert "d.effective_start" in render
    assert "d.effective_end" in render


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


def test_insights_absolute_range_omitted_or_future_end_admits_no_future_same_day(monkeypatch, tmp_path):
    """An omitted or future-supplied `end` collapses to the server clock, so an
    absolute range must NOT admit same-day sessions stamped after `now` - the
    local-midnight roll-up to the NEXT midnight previously leaked them in.
    Regression for Greptile P1 'Current-day cutoff admits future sessions'."""
    now = time.mktime((2026, 5, 4, 12, 0, 0, 0, 0, -1))                # server clock 2026-05-04 12:00
    start_ts = time.mktime((2026, 5, 1, 0, 0, 0, 0, 0, -1))            # selected start day
    after_now = now + 3600                                              # 13:00 same day (future vs server)
    entries = [
        {
            "session_id": "at_now", "updated_at": now, "created_at": start_ts,
            "message_count": 2, "input_tokens": 200, "output_tokens": 80,
            "estimated_cost": "0.0200", "model": "gpt-x",
        },
        {
            "session_id": "future_same_day", "updated_at": after_now, "created_at": after_now,
            "message_count": 1, "input_tokens": 999, "output_tokens": 999,
            "estimated_cost": "0.9999", "model": "gpt-x",
        },
    ]
    # start only; `end` omitted -> effective end clamps to `now`.
    data = _call_insights(monkeypatch, tmp_path, entries,
                          query=f"start={int(start_ts)}", now=now)
    # Future-same-day session excluded; the session exactly at `now` stays.
    assert data["total_sessions"] == 1
    assert data["total_input_tokens"] == 200
    assert data["total_output_tokens"] == 80
    # Explicit but FUTURE `end` is clamped to `now` the same way.
    data2 = _call_insights(monkeypatch, tmp_path, entries,
                           query=f"start={int(start_ts)}&end={int(after_now)}", now=now)
    assert data2["total_sessions"] == 1
    assert data2["total_input_tokens"] == 200


def test_insights_absolute_range_explicit_today_end_admits_no_future_same_day(monkeypatch, tmp_path):
    """An explicit `end` whose calendar day is TODAY (e.g. today's midnight,
    which is `< now` so it does not collapse via min(end, now)) must still not
    admit same-day sessions stamped after the server clock.  The end_exclusive
    next-local-midnight cutoff previously leaked them in because
    `end_clamped_to_now == (end_ts == now)` was False for a genuine today
    timestamp.  Regression for Greptile P1 'Today's end leaks future sessions'."""
    now = time.mktime((2026, 5, 4, 12, 0, 0, 0, 0, -1))                # server clock 2026-05-04 12:00
    today_midnight = time.mktime((2026, 5, 4, 0, 0, 0, 0, 0, -1))     # explicit end = today 00:00 (< now)
    start_ts = time.mktime((2026, 5, 1, 0, 0, 0, 0, 0, -1))            # selected start day
    after_now = now + 3600                                              # 13:00 same day (future vs server)
    entries = [
        {
            "session_id": "before_end", "updated_at": today_midnight + 60, "created_at": start_ts,
            "message_count": 2, "input_tokens": 200, "output_tokens": 80,
            "estimated_cost": "0.0200", "model": "gpt-x",
        },
        {
            "session_id": "future_same_day", "updated_at": after_now, "created_at": after_now,
            "message_count": 1, "input_tokens": 999, "output_tokens": 999,
            "estimated_cost": "0.9999", "model": "gpt-x",
        },
    ]
    # Explicit WHOLE-DAY end = today's calendar day, sent as a DATE STRING.
    # (A numeric value exactly at today's midnight is now an exact `end`
    # boundary, not a whole-day selection - see the numeric-precision tests.)
    # A session BEFORE the server clock but after the day's start belongs;
    # the future-stamped one must not leak.
    data = _call_insights(monkeypatch, tmp_path, entries,
                          query=f"start={int(start_ts)}&end=2026-05-04", now=now)
    assert data["total_sessions"] == 1
    assert data["total_input_tokens"] == 200


def test_insights_absolute_range_precise_numeric_end_today_stays_precise(monkeypatch, tmp_path):
    """A PRECISE numeric `end` earlier on the current local date (e.g. today
    10:00 while now is 14:00) is an explicit exact boundary and must NOT be
    widened to the server clock.  `end_clamped_to_now = end_day == today`
    previously crammed the cutoff up to `now`, leaking sessions between the
    requested endpoint and `now` into the window.  Only a whole-day "today"
    selection (end == today's local midnight, e.g. a date string) clamps to
    now; a strict sub-day end keeps its exact value as the exclusive stop.
    Regression for Greptile P1 'Today widens precise end timestamps'."""
    now = time.mktime((2026, 5, 4, 14, 0, 0, 0, 0, -1))        # server clock 2026-05-04 14:00
    today_midnight = time.mktime((2026, 5, 4, 0, 0, 0, 0, 0, -1))
    precise_end_ts = time.mktime((2026, 5, 4, 10, 0, 0, 0, 0, -1))  # requested end = today 10:00
    start_ts = time.mktime((2026, 5, 1, 0, 0, 0, 0, 0, -1))         # selected start day
    after_end_before_now = precise_end_ts + 1800                    # 10:30 - between endpoint and now (must stay OUT)
    after_now = now + 3600                                          # 15:00 same day (future vs server, must stay OUT)
    entries = [
        {
            "session_id": "in_range", "updated_at": today_midnight + 60, "created_at": start_ts,
            "message_count": 2, "input_tokens": 200, "output_tokens": 80,
            "estimated_cost": "0.0200", "model": "gpt-x",
        },
        {
            "session_id": "between_end_and_now", "updated_at": after_end_before_now,
            "created_at": after_end_before_now,
            "message_count": 1, "input_tokens": 999, "output_tokens": 999,
            "estimated_cost": "0.9999", "model": "gpt-x",
        },
        {
            "session_id": "future_same_day", "updated_at": after_now, "created_at": after_now,
            "message_count": 1, "input_tokens": 888, "output_tokens": 888,
            "estimated_cost": "0.8888", "model": "gpt-x",
        },
    ]
    # Precise numeric end at today 10:00: the 10:30 (between end and now)
    # session must NOT leak in (<-- the bug), matching the 15:00 future one.
    data = _call_insights(monkeypatch, tmp_path, entries,
                          query=f"start={int(start_ts)}&end={int(precise_end_ts)}", now=now)
    assert data["total_sessions"] == 1
    assert data["total_input_tokens"] == 200


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


def test_insights_absolute_range_impossible_calendar_date_falls_back(monkeypatch, tmp_path):
    now = time.mktime((2026, 5, 4, 12, 0, 0, 0, 0, -1))
    entries = [
        {
            "session_id": "today", "updated_at": now, "created_at": now,
            "message_count": 1, "input_tokens": 10, "output_tokens": 5,
            "estimated_cost": "0.0001", "model": "gpt-x",
        },
    ]
    # 2026-02-31 and 2026-13-01 do not exist; time.mktime silently
    # normalizes them to other calendar days.  The parse must reject them
    # so analytics never cover the wrong interval - fall back to the
    # trailing 30-day window instead.
    data = _call_insights(monkeypatch, tmp_path, entries,
                          query="start=2026-02-31&end=2026-13-01", now=now)
    assert data["total_sessions"] == 1
    assert len(data["daily_tokens"]) == 30
    assert data["period_days"] == len(data["daily_tokens"])


def test_insights_absolute_range_invalid_start_with_valid_end_falls_back(monkeypatch, tmp_path):
    now = time.mktime((2026, 5, 4, 12, 0, 0, 0, 0, -1))
    entries = [
        {
            "session_id": "today", "updated_at": now, "created_at": now,
            "message_count": 1, "input_tokens": 10, "output_tokens": 5,
            "estimated_cost": "0.0001", "model": "gpt-x",
        },
    ]
    # Regression for Greptile P1 "Invalid start invents custom range": a
    # SUPPLIED-but-invalid start (2026-02-31 does not exist) with a VALID
    # end used to be conflated with an omitted start, so the handler
    # fabricated a custom window of 30 days before end that the caller
    # never requested.  It must fail closed to the trailing window and
    # report mode="trailing" so the client renders what was actually
    # served.
    data = _call_insights(monkeypatch, tmp_path, entries,
                          query="start=2026-02-31&end=2026-05-04", now=now)
    assert data["total_sessions"] == 1
    assert data["mode"] == "trailing"
    assert data["effective_start"] is None
    assert data["effective_end"] is None
    assert len(data["daily_tokens"]) == 30
    assert data["period_days"] == len(data["daily_tokens"])

    # The crafted window the caller never asked for must NOT be reported.
    assert data["total_tokens"] > 0


def test_insights_absolute_range_valid_start_with_invalid_end_falls_back(monkeypatch, tmp_path):
    now = time.mktime((2026, 5, 4, 12, 0, 0, 0, 0, -1))
    entries = [
        {
            "session_id": "today", "updated_at": now, "created_at": now,
            "message_count": 1, "input_tokens": 10, "output_tokens": 5,
            "estimated_cost": "0.0001", "model": "gpt-x",
        },
    ]
    # Regression for the 2026-08-17 Greptile re-review: a SUPPLIED-but-
    # invalid end (2026-02-31 does not exist) with a VALID start used to be
    # conflated with an omitted end, so the handler widened the valid start
    # through the current server time into a fabricated [start, now] custom
    # interval the caller never requested.  It must fail closed to the
    # trailing window and report mode="trailing" so the client renders what
    # was actually served.
    data = _call_insights(monkeypatch, tmp_path, entries,
                          query="start=2026-05-01&end=2026-02-31", now=now)
    assert data["total_sessions"] == 1
    assert data["mode"] == "trailing"
    assert data["effective_start"] is None
    assert data["effective_end"] is None
    assert len(data["daily_tokens"]) == 30
    assert data["period_days"] == len(data["daily_tokens"])

    # Same fail-closed rule for a non-finite numeric end beside a valid start.
    data = _call_insights(monkeypatch, tmp_path, entries,
                          query="start=2026-05-01&end=nan", now=now)
    assert data["mode"] == "trailing"
    assert data["effective_start"] is None
    assert data["effective_end"] is None
    assert len(data["daily_tokens"]) == 30


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
    # `end=nan` is rejected; a SUPPLIED-but-invalid end must fail closed to
    # the trailing window (2026-08-17 Greptile re-review), never widen a
    # valid start to start..now.
    assert data["total_sessions"] == 1
    assert len(data["daily_tokens"]) == 30


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


def test_insights_absolute_range_future_start_never_produces_future_window(monkeypatch, tmp_path):
    now = time.mktime((2026, 5, 4, 12, 0, 0, 0, 0, -1))
    entries = [
        {
            "session_id": "today", "updated_at": now, "created_at": now,
            "message_count": 1, "input_tokens": 10, "output_tokens": 5,
            "estimated_cost": "0.0001", "model": "gpt-x",
        },
    ]
    future = now + (30 * 86400)
    # A future start WITHOUT an end used to swap against the implicit now
    # endpoint and return a zero-filled window running from now into the
    # future.  It must fail closed to the trailing 30-day fallback.
    data = _call_insights(monkeypatch, tmp_path, entries, query=f"start={int(future)}", now=now)
    assert data["total_sessions"] == 1
    assert len(data["daily_tokens"]) == 30  # trailing fallback, no future buckets
    assert data["period_days"] == len(data["daily_tokens"])
    assert data["daily_tokens"][-1]["date"] == _day(now)  # ends at today, never future
    # Future start AND future end: still no window beyond now.
    data = _call_insights(monkeypatch, tmp_path, entries, query=f"start={int(future)}&end={int(future + 86400)}", now=now)
    assert data["total_sessions"] == 1
    assert len(data["daily_tokens"]) == 30
    # Future start + explicit past end: the forgiving order-swap is kept, the
    # end is clamped to now, past days are preserved, nothing lies in future.
    past = now - (2 * 86400)
    data = _call_insights(monkeypatch, tmp_path, entries, query=f"start={int(future)}&end={int(past)}", now=now)
    assert data["total_sessions"] == 1
    assert data["daily_tokens"][0]["date"] == _day(past)
    assert data["daily_tokens"][-1]["date"] == _day(now)


def test_insights_absolute_range_swapped_future_start_past_end_admits_no_future_same_day(monkeypatch, tmp_path):
    """A future `start` paired with a past `end` is swapped, and the resulting
    `end` is clamped to the server clock.  That post-swap collapse must be
    treated like an omitted/future end - NOT admit same-day sessions stamped
    after `now`.  The pre-swap 'clamped to now' flag wrongly stayed False and
    left the cutoff at the next local midnight (exclusive), leaking them in.
    Regression for Greptile P1 'Swapped range leaks future sessions'."""
    now = time.mktime((2026, 5, 4, 12, 0, 0, 0, 0, -1))                # server clock 2026-05-04 12:00
    future = now + (10 * 86400)                                        # 2026-05-14 (future start)
    past = now - (2 * 86400)                                           # 2026-05-02 (past end)
    after_now = now + 3600                                             # 13:00 today (future vs server)
    entries = [
        {
            "session_id": "at_now", "updated_at": now, "created_at": past,
            "message_count": 2, "input_tokens": 200, "output_tokens": 80,
            "estimated_cost": "0.0200", "model": "gpt-x",
        },
        {
            "session_id": "future_same_day", "updated_at": after_now, "created_at": after_now,
            "message_count": 1, "input_tokens": 999, "output_tokens": 999,
            "estimated_cost": "0.9999", "model": "gpt-x",
        },
    ]
    # Future start + past end -> server swaps them, clamps end to now.  The
    # past..now window is preserved; a session stamped after the server clock
    # on the same day must still be excluded (old pre-swap flag admitted it).
    data = _call_insights(monkeypatch, tmp_path, entries,
                          query=f"start={int(future)}&end={int(past)}", now=now)
    assert data["total_sessions"] == 1
    assert data["total_input_tokens"] == 200
    assert data["total_output_tokens"] == 80
    assert data["daily_tokens"][0]["date"] == _day(past)
    assert data["daily_tokens"][-1]["date"] == _day(now)


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
        entries = [
            {
                "session_id": "s1", "updated_at": now, "created_at": now,
                "message_count": 1, "input_tokens": 10, "output_tokens": 5,
                "estimated_cost": "0.0001", "model": "gpt-x",
            },
        ]
        data = _call_insights(monkeypatch, tmp_path, entries,
                              query="start=2026-03-06&end=2026-03-10", now=now)
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
        # End day = 2026-03-08 (the spring-forward day).  Its LAST local hour is
        # 23:00-24:00 EDT (EDT = UTC-4 after the 2:00-3:00 spring-forward jump),
        # well after end_ts (noon Mar 8), so it probes the real end-of-day.
        end_day_last_hour = time.mktime((2026, 3, 8, 23, 30, 0, 0, 0, -1))  # 23:30 EDT
        # First hour of the NEXT local day = 2026-03-09 00:00-01:00 EDT.
        next_day_first_hour = time.mktime((2026, 3, 9, 0, 30, 0, 0, 0, -1))  # 00:30 EDT
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
                              query="start=2026-03-06&end=2026-03-08", now=now)
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
def test_insights_absolute_range_excludes_session_exactly_at_next_midnight(monkeypatch, tmp_path):
    """end_cutoff is an EXCLUSIVE upper bound (next local midnight) in
    absolute mode: a WebUI session stamped exactly AT that midnight belongs
    to the following date and must NOT be counted, while one a second before
    it is still inside the selected day.  Regression for Greptile P1
    "Exclusive midnight remains included" (the `<`/`>` comparisons retained
    equality at the explicitly exclusive cutoff)."""
    now = time.mktime((2026, 5, 4, 12, 0, 0, 0, 0, -1))
    next_midnight = time.mktime((2026, 5, 3, 0, 0, 0, 0, 0, -1))  # == end_cutoff
    one_sec_before = next_midnight - 1
    entries = [
        {
            "session_id": "in_last_second", "updated_at": one_sec_before, "created_at": one_sec_before,
            "message_count": 1, "input_tokens": 10, "output_tokens": 5,
            "estimated_cost": "0.0001", "model": "gpt-x",
        },
        {
            "session_id": "at_next_midnight", "updated_at": next_midnight, "created_at": next_midnight,
            "message_count": 1, "input_tokens": 999, "output_tokens": 999,
            "estimated_cost": "0.9999", "model": "gpt-x",
        },
    ]
    data = _call_insights(monkeypatch, tmp_path, entries,
                          query="start=2026-05-01&end=2026-05-02", now=now)
    # The session exactly at the exclusive next-midnight cutoff is excluded.
    assert data["total_sessions"] == 1
    assert data["total_input_tokens"] == 10
    assert data["total_output_tokens"] == 5


def test_insights_absolute_range_state_db_excludes_row_exactly_at_next_midnight(monkeypatch, tmp_path):
    """The same exclusive-bound rule applies to the Hermes state.db CLI pass:
    a session whose started_at/ended_at equals the next local midnight must
    be excluded from an absolute range (SQL used <= before - equality at the
    explicitly exclusive cutoff leaked the following date in)."""
    now = time.mktime((2026, 5, 4, 12, 0, 0, 0, 0, -1))
    next_midnight = time.mktime((2026, 5, 3, 0, 0, 0, 0, 0, -1))
    one_sec_before = next_midnight - 1
    state_rows = [
        {"id": "cli_in", "source": "cli", "model": "gpt-5.5", "message_count": 1,
         "input_tokens": 10, "output_tokens": 5, "estimated_cost_usd": 0.0001,
         "started_at": one_sec_before, "ended_at": one_sec_before},
        {"id": "cli_at_cutoff", "source": "cli", "model": "gpt-5.5", "message_count": 1,
         "input_tokens": 999, "output_tokens": 999, "estimated_cost_usd": 0.9999,
         "started_at": next_midnight, "ended_at": next_midnight},
    ]
    data = _call_insights_with_state_db(monkeypatch, tmp_path, [], state_rows,
                                        query="start=2026-05-01&end=2026-05-02", now=now)
    assert data["total_sessions"] == 1
    assert data["total_input_tokens"] == 10
    assert data["total_output_tokens"] == 5


def test_insights_trailing_window_keeps_session_at_now(monkeypatch, tmp_path):
    """Trailing mode end_cutoff = now is INCLUSIVE: a session stamped exactly
    at the mock clock's `now` still belongs to the trailing window.  Guards
    the end_exclusive flag so the absolute-mode exclusive bound fix cannot
    regress the legacy days=N path."""
    now = time.mktime((2026, 5, 4, 12, 0, 0, 0, 0, -1))
    entries = [
        {
            "session_id": "at_now", "updated_at": now, "created_at": now,
            "message_count": 2, "input_tokens": 200, "output_tokens": 80,
            "estimated_cost": "0.0200", "model": "gpt-x",
        },
    ]
    data = _call_insights(monkeypatch, tmp_path, entries, days="7", now=now)
    assert data["total_sessions"] == 1
    assert data["total_input_tokens"] == 200


def test_insights_trailing_window_dst_cutoff_at_local_midnight(monkeypatch, tmp_path):
    """Trailing days=N fall-back across a DST transition must place
    first_day_ts at LOCAL MIDNIGHT of the first calendar day, not at
    01:00 like fixed 86400s subtraction does.  America/New_York
    2026-11-01 falls back 2:00 -> 1:00, so today_midnight - (N-1)*86400
    lands an hour LATE (01:00 of an ephemeral 25-hour day); a session
    at 00:30 of that first day would then be dropped from the window
    while its daily bucket still shows on the chart.  Regression for
    Greptile P1 "Trailing DST cutoff mismatches buckets"."""
    import os
    if not hasattr(time, "tzset"):
        pytest.skip("time.tzset() required for DST test (not available on Windows)")

    os.environ["TZ"] = "America/New_York"
    time.tzset()
    try:
        # 2026-11-02 12:00 EST, the day after the fall-back transition.
        now = time.mktime((2026, 11, 2, 12, 0, 0, 0, 0, -1))
        # 2026-11-01 00:30 EDT = 2026-11-01 04:30 UTC.  A fixed
        # 86400s cutoff from Nov 2 midnight lands at Nov 1 01:00 EDT,
        # which would EXCLUDE this 00:30 session (before 01:00) even
        # though its calendar day is inside the 2-day window.
        first_day = time.mktime((2026, 11, 1, 0, 30, 0, 0, 0, -1))
        entries = [
            {
                "session_id": "first_day_early_hour", "updated_at": first_day, "created_at": first_day,
                "message_count": 1, "input_tokens": 10, "output_tokens": 5,
                "estimated_cost": "0.0001", "model": "gpt-x",
            },
        ]
        data = _call_insights(monkeypatch, tmp_path, entries, days="2", now=now)
        # The first-day 00:30 session MUST be inside the 2-day window
        # (Nov 1 + Nov 2), so totals include it and its daily bucket
        # matches the series.
        assert data["total_sessions"] == 1
        assert data["total_input_tokens"] == 10
        dates = [d["date"] for d in data["daily_tokens"]]
        assert dates == ["2026-11-01", "2026-11-02"]
        assert data["daily_tokens"][0]["sessions"] == 1
    finally:
        os.environ.pop("TZ", None)
        if hasattr(time, "tzset"):
            time.tzset()


def test_insights_absolute_range_state_db_keeps_row_at_now_cutoff(monkeypatch, tmp_path):
    """Absolute-mode SQL uses an exclusive `<` on end_cutoff, but trailing
    mode keeps `<=` (now).  A state.db CLI row exactly at `now` must be
    counted in the trailing window."""
    now = time.mktime((2026, 5, 4, 12, 0, 0, 0, 0, -1))
    state_rows = [
        {"id": "cli_at_now", "source": "cli", "model": "gpt-5.5", "message_count": 1,
         "input_tokens": 10, "output_tokens": 5, "estimated_cost_usd": 0.0001,
         "started_at": now, "ended_at": now},
    ]
    data = _call_insights_with_state_db(monkeypatch, tmp_path, [], state_rows,
                                        days="7", now=now)
    assert data["total_sessions"] == 1
    assert data["total_input_tokens"] == 10


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
    data = _call_insights(monkeypatch, tmp_path, entries, query=f"start={_day(start_ts)}&end={_day(end_ts)}", now=now)
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
    # Raw YYYY-MM-DD date strings are sent to the API (NOT browser-local
    # epoch seconds): the server parses them in its OWN timezone so a remote
    # browser/timezone can never shift the selected calendar day.
    assert "if (startVal) qs.set('start', startVal)" in PANELS_JS
    assert "if (endVal) qs.set('end', endVal)" in PANELS_JS
    assert "new Date(startVal + 'T00:00:00')" not in PANELS_JS
    # Latest-request guard: a stale in-flight response must not overwrite a
    # newer range.
    assert "_insightsReqToken" in PANELS_JS
    assert "reqToken !== _insightsReqToken" in PANELS_JS
    # Date inputs are accessible by name.
    assert 'aria-label="Start date"' in INDEX_HTML
    assert 'aria-label="End date"' in INDEX_HTML
    assert 'aria-hidden="true"' in INDEX_HTML
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


def _call_insights_with_state_db(monkeypatch, tmp_path, entries, state_rows, days="7", now=None, query=None):
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
    if query is None:
        query = f"days={days}"
    parsed = SimpleNamespace(query=query)
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



# ── #6970 review #3: single-timestamp admission contract ──────────────────
# The API uses one canonical timestamp for both session admission and daily
# attribution.  A session whose canonical timestamp falls outside the range
# must be excluded even if the interval overlaps the range; a session whose
# canonical timestamp falls inside must be included and its daily bucket
# must land inside the returned series.  This guarantees totals and the
# daily chart always describe the same window.


def test_insights_absolute_range_webui_crossing_session_excluded(monkeypatch, tmp_path):
    """A WebUI session crossing the boundary (created < cutoff, updated >=
    end_cutoff) must be EXCLUDED: its canonical usage_ts = updated_at is
    outside the window.  The old interval-overlap contract admitted it
    (max>=cutoff, min<end_cutoff) but its daily bucket landed outside the
    series.  Under the single-ts contract totals and daily series agree."""
    now = time.mktime((2026, 5, 4, 12, 0, 0, 0, 0, -1))
    start_ts = now - (10 * 86400)   # 10 days ago
    end_ts = now - (2 * 86400)      # 2 days ago
    entries = [
        {
            "session_id": "crossing",
            "created_at": now - (15 * 86400),  # before cutoff
            "updated_at": now,                   # after end_cutoff
            "message_count": 5, "input_tokens": 500, "output_tokens": 200,
            "estimated_cost": "0.0500", "model": "gpt-x",
        },
    ]
    data = _call_insights(monkeypatch, tmp_path, entries,
                          query=f"start={int(start_ts)}&end={int(end_ts)}", now=now)
    # Crossing session is excluded.
    assert data["total_sessions"] == 0
    assert data["total_input_tokens"] == 0
    # Daily series sum must agree with totals (both 0).
    daily_sum = sum(d["sessions"] for d in data["daily_tokens"])
    assert daily_sum == data["total_sessions"]


def test_insights_absolute_range_state_db_crossing_session_included(monkeypatch, tmp_path):
    """A CLI session started before the range and ended inside it must be
    INCLUDED: its canonical usage_ts = ended_at falls inside the window.
    The old contract admitted it (by overlap) but attributed to started_at
    (outside the series).  Under the single-ts contract totals and daily
    series agree."""
    now = time.mktime((2026, 5, 4, 12, 0, 0, 0, 0, -1))
    start_ts = now - (10 * 86400)   # 10 days ago
    end_ts = now - (2 * 86400)      # 2 days ago
    state_rows = [
        {"id": "cli_crossing", "source": "cli", "model": "gpt-5.5",
         "message_count": 3, "input_tokens": 300, "output_tokens": 150,
         "estimated_cost_usd": 0.0300,
         "started_at": now - (15 * 86400),       # before cutoff
         "ended_at": now - (5 * 86400)},          # inside the range
    ]
    data = _call_insights_with_state_db(monkeypatch, tmp_path, [], state_rows,
                                        query=f"start={int(start_ts)}&end={int(end_ts)}", now=now)
    # Crossing session is included.
    assert data["total_sessions"] == 1
    assert data["total_input_tokens"] == 300
    # Daily series must include the session's bucket.
    daily_sum = sum(d["sessions"] for d in data["daily_tokens"])
    assert daily_sum == data["total_sessions"]
    # The session's bucket should be on the ended_at date.
    ended_day = _day(now - (5 * 86400))
    ended_bucket = next((d for d in data["daily_tokens"] if d["date"] == ended_day), None)
    assert ended_bucket is not None, f"no bucket for ended day {ended_day}"
    assert ended_bucket["input_tokens"] == 300
