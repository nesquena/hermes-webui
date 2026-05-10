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


def _call_insights(monkeypatch, tmp_path, entries, days="7", now=None):
    import api.routes as routes

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    (session_dir / "_index.json").write_text(json.dumps(entries), encoding="utf-8")
    monkeypatch.setattr(routes, "SESSION_DIR", session_dir)
    if now is not None:
        monkeypatch.setattr(time, "time", lambda: now)

    handler = _FakeHandler()
    parsed = SimpleNamespace(query=f"days={days}")
    routes._handle_insights(handler, parsed)
    assert handler.status == 200
    return handler.json_body()


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
        "sessions": 1,
        "cost": 0.0123,
    }
    assert by_date[_day(now - 86400)] == {
        "date": _day(now - 86400),
        "input_tokens": 0,
        "output_tokens": 0,
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
    assert "/api/insights?days=${period}" in PANELS_JS
    assert ".insights-daily-token-chart" in STYLE_CSS
    assert ".insights-daily-bar-output" in STYLE_CSS
    assert ".insights-model-cost" in STYLE_CSS
