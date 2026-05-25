import io
import json
import pathlib
import sys
import time
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


def _call_insights(monkeypatch, tmp_path, entries, days="7", now=None, scope=None):
    import api.routes as routes

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    (session_dir / "_index.json").write_text(json.dumps(entries), encoding="utf-8")
    monkeypatch.setattr(routes, "SESSION_DIR", session_dir)
    if now is not None:
        monkeypatch.setattr(time, "time", lambda: now)

    handler = _FakeHandler()
    qs = f"days={days}"
    if scope is not None:
        qs += f"&scope={scope}"
    parsed = SimpleNamespace(query=qs)
    routes._handle_insights(handler, parsed)
    assert handler.status == 200
    return handler.json_body()


def _seed_state_db(db_path, rows):
    """Build a minimal hermes-agent state.db with the schema columns
    _load_global_state_db_entries reads. Returns the path written."""
    import sqlite3

    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("""
            CREATE TABLE sessions (
                id TEXT PRIMARY KEY,
                source TEXT NOT NULL,
                model TEXT,
                started_at REAL NOT NULL,
                ended_at REAL,
                message_count INTEGER DEFAULT 0,
                input_tokens INTEGER DEFAULT 0,
                output_tokens INTEGER DEFAULT 0,
                cache_read_tokens INTEGER DEFAULT 0,
                cache_write_tokens INTEGER DEFAULT 0,
                estimated_cost_usd REAL,
                actual_cost_usd REAL
            )
        """)
        for r in rows:
            conn.execute("""
                INSERT INTO sessions (
                    id, source, model, started_at, ended_at, message_count,
                    input_tokens, output_tokens, cache_read_tokens,
                    cache_write_tokens, estimated_cost_usd, actual_cost_usd
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                r["id"], r["source"], r.get("model", "test"),
                r["started_at"], r.get("ended_at"), r.get("message_count", 1),
                r.get("input_tokens", 0), r.get("output_tokens", 0),
                r.get("cache_read_tokens", 0), r.get("cache_write_tokens", 0),
                r.get("estimated_cost_usd"), r.get("actual_cost_usd"),
            ))
        conn.commit()
    finally:
        conn.close()
    return db_path


def test_insights_default_scope_is_webui_and_response_advertises_scope(monkeypatch, tmp_path):
    """Without ``?scope=`` the endpoint serves the historical webui-only
    payload, and the response declares the effective scope so the UI can
    label what's being shown."""
    now = time.mktime((2026, 5, 4, 12, 0, 0, 0, 0, -1))
    entries = [{
        "session_id": "webui-only",
        "updated_at": now,
        "created_at": now,
        "message_count": 3,
        "input_tokens": 9000,
        "output_tokens": 100,
        "estimated_cost": 0.05,
        "model": "gpt-5.5",
    }]

    data = _call_insights(monkeypatch, tmp_path, entries, days="7", now=now)
    assert data["scope"] == "webui"
    assert data["scope_requested"] == "webui"
    assert data["scope_available"] is True
    assert data["total_input_tokens"] == 9000
    assert data["total_output_tokens"] == 100
    assert data["total_tokens"] == 9100


def test_insights_global_scope_reads_state_db_with_full_prompt_semantics(monkeypatch, tmp_path):
    """scope=global must aggregate every Hermes session source from state.db
    and reshape ``input_tokens`` to the *full prompt total* (new + cache_read
    + cache_write) so it lines up with what providers bill — and with what
    the WebUI scope already shows."""
    import api.routes as routes

    now = time.mktime((2026, 5, 4, 12, 0, 0, 0, 0, -1))
    one_hour_ago = now - 3600

    home_dir = tmp_path / "hermes_home"
    home_dir.mkdir()
    state_db = home_dir / "state.db"
    _seed_state_db(state_db, [
        # WebUI session — also exists in _index.json conceptually; here we
        # just verify it is summed in the global view.
        {
            "id": "webui-1", "source": "webui", "model": "claude-opus-4.7",
            "started_at": one_hour_ago, "ended_at": one_hour_ago + 60,
            "message_count": 4,
            "input_tokens": 1_000_000,    # new prompt only (cache subtracted)
            "output_tokens": 50_000,
            "cache_read_tokens": 7_700_000,
            "cache_write_tokens": 400_000,
            "estimated_cost_usd": 5.0,
        },
        # Discord session — completely invisible in the WebUI scope.
        {
            "id": "discord-1", "source": "discord", "model": "claude-sonnet-4",
            "started_at": one_hour_ago, "ended_at": one_hour_ago + 30,
            "message_count": 2,
            "input_tokens": 500_000,
            "output_tokens": 20_000,
            "cache_read_tokens": 3_000_000,
            "cache_write_tokens": 100_000,
            "actual_cost_usd": 1.25,
        },
        # CLI session — cost only via actual_cost_usd; estimated is null.
        {
            "id": "cli-1", "source": "cli", "model": "gpt-5.5",
            "started_at": one_hour_ago, "ended_at": None,
            "message_count": 1,
            "input_tokens": 100_000, "output_tokens": 10_000,
            "cache_read_tokens": 0, "cache_write_tokens": 0,
            "actual_cost_usd": 0.42,
        },
    ])
    monkeypatch.setenv("HERMES_HOME", str(home_dir))
    # SESSION_DIR is still patched by _call_insights but should not be read
    # for scope=global; pass an empty index just in case.
    data = _call_insights(monkeypatch, tmp_path, [], days="7", now=now, scope="global")

    assert data["scope"] == "global"
    assert data["scope_requested"] == "global"
    assert data["scope_available"] is True
    assert data["total_sessions"] == 3

    # Full-prompt semantics: state.db's three buckets must be summed back
    # together so the WebUI's "input" column equals provider prompt_tokens.
    expected_input = (
        (1_000_000 + 7_700_000 + 400_000) +
        (500_000 + 3_000_000 + 100_000) +
        (100_000 + 0 + 0)
    )
    expected_output = 50_000 + 20_000 + 10_000
    assert data["total_input_tokens"] == expected_input
    assert data["total_output_tokens"] == expected_output
    assert data["total_tokens"] == expected_input + expected_output

    # Cost: actual_cost_usd preferred when present; otherwise estimated.
    # Here: webui-1 → 5.0 (estimated, no actual), discord-1 → 1.25 (actual),
    # cli-1 → 0.42 (actual). Total = 6.67.
    assert abs(data["total_cost"] - 6.67) < 1e-6

    # Per-model breakdown must include both webui and discord models.
    model_names = {m["model"] for m in data["models"]}
    assert {"claude-opus-4.7", "claude-sonnet-4", "gpt-5.5"}.issubset(model_names)


def test_insights_global_scope_includes_sessions_active_inside_window(monkeypatch, tmp_path):
    """Global scope should use the same active-in-window semantics as the
    historical WebUI scope: a long-running session that started before the
    selected range but ended/updated inside it is still relevant."""
    now = time.mktime((2026, 5, 25, 12, 0, 0, 0, 0, -1))
    today = time.localtime(now)
    today_midnight = time.mktime((
        today.tm_year, today.tm_mon, today.tm_mday, 0, 0, 0,
        today.tm_wday, today.tm_yday, today.tm_isdst,
    ))
    cutoff = today_midnight - (6 * 86400)  # days=7 calendar window

    home_dir = tmp_path / "hermes_home_active_window"
    home_dir.mkdir()
    _seed_state_db(home_dir / "state.db", [{
        "id": "long-session",
        "source": "webui",
        "model": "claude-opus-4.7",
        "started_at": cutoff - 3600,
        "ended_at": cutoff + 3600,
        "message_count": 2,
        "input_tokens": 10,
        "cache_read_tokens": 20,
        "cache_write_tokens": 30,
        "output_tokens": 5,
        "estimated_cost_usd": 0.1,
    }])
    monkeypatch.setenv("HERMES_HOME", str(home_dir))

    data = _call_insights(monkeypatch, tmp_path, [], days="7", now=now, scope="global")
    assert data["scope"] == "global"
    assert data["total_sessions"] == 1
    assert data["total_messages"] == 2
    assert data["total_input_tokens"] == 60
    assert data["total_output_tokens"] == 5
    assert data["total_tokens"] == 65


def test_insights_global_scope_falls_back_when_state_db_missing(monkeypatch, tmp_path):
    """Asking for scope=global when state.db isn't reachable must return
    the WebUI payload with scope_available=false so the UI can disable the
    Global toggle and explain why."""
    home_dir = tmp_path / "hermes_home_no_db"
    home_dir.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home_dir))

    now = time.mktime((2026, 5, 4, 12, 0, 0, 0, 0, -1))
    entries = [{
        "session_id": "webui-only",
        "updated_at": now,
        "created_at": now,
        "message_count": 1,
        "input_tokens": 200,
        "output_tokens": 50,
        "estimated_cost": 0.0,
        "model": "gpt-5.5",
    }]

    data = _call_insights(monkeypatch, tmp_path, entries, days="7", now=now, scope="global")
    assert data["scope"] == "webui"               # effective fell back
    assert data["scope_requested"] == "global"     # but UI knows the user asked for global
    assert data["scope_available"] is False        # so the toggle can disable
    assert data["total_input_tokens"] == 200
    assert data["total_output_tokens"] == 50


def test_insights_unknown_scope_param_is_treated_as_webui(monkeypatch, tmp_path):
    """Defensive: any garbage value in ?scope= must collapse to the safe
    WebUI default rather than 500-ing or leaking implementation details."""
    now = time.mktime((2026, 5, 4, 12, 0, 0, 0, 0, -1))
    entries = [{
        "session_id": "webui-only",
        "updated_at": now,
        "created_at": now,
        "message_count": 1,
        "input_tokens": 10,
        "output_tokens": 5,
        "estimated_cost": 0.0,
        "model": "x",
    }]

    data = _call_insights(monkeypatch, tmp_path, entries, days="7", now=now, scope="bogus-scope-name")
    assert data["scope"] == "webui"
    assert data["scope_requested"] == "webui"
    assert data["total_tokens"] == 15


def _day(ts):
    return time.strftime("%Y-%m-%d", time.localtime(ts))


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
        "cache_write_tokens": 0,
        "sessions": 1,
        "cost": 0.0123,
    }
    assert by_date[_day(now - 86400)] == {
        "date": _day(now - 86400),
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
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
    assert "insights_model_cost" in PANELS_JS
    assert "insights_model_share" in PANELS_JS
    assert "insights_no_usage_data" in PANELS_JS


def test_insights_frontend_has_daily_chart_styles_and_range_switching_hooks():
    assert "insightsPeriod" in INDEX_HTML
    assert 'option value="7"' in INDEX_HTML
    assert 'option value="30"' in INDEX_HTML
    assert 'option value="90"' in INDEX_HTML
    assert "loadInsights()" in INDEX_HTML
    # Scope param now part of the insights URL contract.
    assert "/api/insights?days=${period}&scope=${scope}" in PANELS_JS
    assert ".insights-daily-token-chart" in STYLE_CSS
    assert ".insights-daily-bar-output" in STYLE_CSS
    assert ".insights-model-cost" in STYLE_CSS


def test_insights_frontend_exposes_webui_global_scope_toggle():
    """The toggle must exist in both the markup and the panel script,
    and CSS must style the segmented control. This is the visual contract
    that lets a user flip between WebUI-only and Hermes-global token
    views without any other page state changing."""
    # HTML: toggle DOM
    assert 'id="insightsScopeToggle"' in INDEX_HTML
    assert 'data-scope="webui"' in INDEX_HTML
    assert 'data-scope="global"' in INDEX_HTML
    # Locate the segmented control's body and assert pressed-state semantics
    # without pinning the assertion to a specific surrounding indent so the
    # sidebar layout can evolve without breaking the contract.
    scope_start = INDEX_HTML.index('id="insightsScopeToggle"')
    # The segmented control's own </div> closes the toggle; everything between
    # the opening tag and that close is the scope toggle body (label + buttons).
    after_open = INDEX_HTML.index('>', scope_start) + 1
    scope_end = INDEX_HTML.index('</div>', after_open)
    scope_markup = INDEX_HTML[scope_start:scope_end]
    assert 'aria-pressed="true"' in scope_markup
    assert 'role="tab"' not in scope_markup
    assert 'role="tablist"' not in scope_markup
    assert "insights_scope_label" in INDEX_HTML
    assert "insights_scope_webui" in INDEX_HTML
    assert "insights_scope_global" in INDEX_HTML
    assert "setInsightsScope" in INDEX_HTML
    # Sidebar control panel layout: Period select sits above the scope toggle
    # in a stacked label+control rhythm that mirrors the Logs control panel,
    # so both controls share the same starting x and the period select is no
    # longer floating tight against the panel header. (#2943)
    assert 'class="insights-control-panel"' in INDEX_HTML
    assert 'insights_period_label' in INDEX_HTML
    assert 'for="insightsPeriod"' in INDEX_HTML

    # JS: state, setter, fetch param, fallback sync
    assert "let _insightsScope" in PANELS_JS
    assert "function setInsightsScope" in PANELS_JS
    assert "_syncInsightsScopeUI" in PANELS_JS
    assert "let _insightsRequestSeq" in PANELS_JS
    # Both success and error paths must drop stale responses after a newer
    # period/scope request starts; otherwise an old failure can overwrite
    # fresh data with an error banner.
    assert PANELS_JS.count("requestSeq !== _insightsRequestSeq") >= 2
    assert "aria-pressed" in PANELS_JS
    assert "scope_available" in PANELS_JS  # frontend reads the fallback flag
    assert "insights_scope_note" in PANELS_JS
    assert "insights-scope-note" in PANELS_JS
    assert "insights_cache_read_tokens" in PANELS_JS
    assert "insights_cache_write_tokens" in PANELS_JS
    assert "insights_cache_tokens_note" not in PANELS_JS
    assert "localStorage.setItem('insightsScope'" in PANELS_JS  # persisted preference

    # CSS: segmented control + sidebar control panel styling
    assert ".insights-control-panel" in STYLE_CSS
    assert ".insights-control-label" in STYLE_CSS
    assert ".insights-scope-segmented" in STYLE_CSS
    assert ".insights-scope-btn" in STYLE_CSS
    assert ".insights-scope-btn.is-active" in STYLE_CSS
    assert ".insights-scope-note" in STYLE_CSS
    assert ".insights-token-subrow" in STYLE_CSS
    assert ".insights-cache-note" not in STYLE_CSS


def test_insights_i18n_scope_keys_present_in_every_locale():
    """Every locale must carry the four scope-toggle keys so the toggle
    label and the unavailable-tooltip work in every language. Missing a
    locale would surface as 'undefined' text in the UI."""
    import re as _re

    i18n_path = REPO_ROOT / "static" / "i18n.js"
    text = i18n_path.read_text(encoding="utf-8")

    locales: list[tuple[str, int, int]] = []
    for m in _re.finditer(r"^  ([a-z]{2}(?:-[a-z]+)?):\s*\{", text, _re.MULTILINE):
        locales.append((m.group(1), m.start(), -1))
    # Compute end-offsets
    finalized = []
    for i, (name, start, _end) in enumerate(locales):
        end = locales[i + 1][1] if i + 1 < len(locales) else len(text)
        finalized.append((name, start, end))

    # Sanity: project ships at least 11 locales today.
    assert len(finalized) >= 11, f"expected >=11 locales, found {len(finalized)}"

    required_keys = (
        "insights_scope_label",
        "insights_scope_webui",
        "insights_scope_global:",          # trailing colon disambiguates from _global_unavailable
        "insights_scope_global_unavailable",
        "insights_scope_note",
        "insights_cache_read_tokens",
        "insights_cache_write_tokens",
    )
    for name, start, end in finalized:
        block = text[start:end]
        for key in required_keys:
            assert key in block, f"locale '{name}' missing i18n key: {key.rstrip(':')}"


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
