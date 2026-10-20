"""#8185: pricing overrides for local/Ollama routes on the Insights tab.

The Estimated Cost card renders N/A forever when the Agent core has no pricing
source for a route (ollama-cloud, LM Studio, any local OpenAI-compatible
endpoint): stored `estimated_cost_usd` is 0 and there was no way to declare a
rate.  These tests pin the WebUI-side contract:

* `pricing_overrides` settings entry (per-model rates in USD per million
  tokens, or an explicit `free: true` marker) is applied by the insights
  aggregation ONLY when the stored cost is 0/unknown — stored history with a
  real cost is never repriced.
* a route marked free renders as $0.00 (known zero), not N/A.
* sessions the aggregation still cannot price are COUNTED and the affected
  models are listed, so the card can explain why it says N/A.
"""
import io
import json
import pathlib
import sys
import time
from types import SimpleNamespace

import pytest

REPO_ROOT = pathlib.Path(__file__).parent.parent.resolve()
sys.path.insert(0, str(REPO_ROOT))

PANELS_JS = (REPO_ROOT / "static" / "panels.js").read_text(encoding="utf-8")
I18N_JS = (REPO_ROOT / "static" / "i18n.js").read_text(encoding="utf-8")
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


def _call_insights(monkeypatch, tmp_path, entries, days="7", now=None, settings=None,
                   state_rows=None):
    """Run _handle_insights against a seeded _index.json (and optional state.db)."""
    import api.routes as routes

    session_dir = tmp_path / "sessions"
    session_dir.mkdir(exist_ok=True)
    (session_dir / "_index.json").write_text(json.dumps(entries), encoding="utf-8")
    monkeypatch.setattr(routes, "SESSION_DIR", session_dir)
    if now is not None:
        monkeypatch.setattr(time, "time", lambda: now)
    monkeypatch.setattr(routes, "load_settings", lambda: dict(settings or {}))

    if state_rows is not None:
        import sqlite3
        import api.models as models

        db_path = tmp_path / "state.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            """CREATE TABLE sessions (
                id TEXT PRIMARY KEY, source TEXT, model TEXT, message_count INTEGER,
                input_tokens INTEGER, output_tokens INTEGER, estimated_cost_usd REAL,
                cache_read_tokens INTEGER DEFAULT 0,
                billing_provider TEXT, cost_status TEXT,
                started_at REAL, ended_at REAL
            )"""
        )
        for r in state_rows:
            conn.execute(
                "INSERT INTO sessions (id, source, model, message_count, input_tokens, "
                "output_tokens, estimated_cost_usd, cache_read_tokens, billing_provider, "
                "cost_status, started_at, ended_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (r["id"], r.get("source"), r.get("model"), r.get("message_count", 0),
                 r.get("input_tokens", 0), r.get("output_tokens", 0),
                 r.get("estimated_cost_usd", 0.0), r.get("cache_read_tokens", 0),
                 r.get("billing_provider"), r.get("cost_status"),
                 r.get("started_at"), r.get("ended_at")),
            )
        conn.commit()
        conn.close()
        monkeypatch.setattr(models, "_active_state_db_path", lambda: db_path)

    handler = _FakeHandler()
    parsed = SimpleNamespace(query=f"days={days}")
    routes._handle_insights(handler, parsed)
    assert handler.status == 200
    return handler.json_body()


def _now():
    return time.mktime((2026, 10, 10, 12, 0, 0, 0, 0, -1))


# ── override application ────────────────────────────────────────────────────


def test_insights_pricing_override_rates_price_an_unknown_cost_session(monkeypatch, tmp_path):
    """A zero-cost session whose model has override rates gets a real cost."""
    now = _now()
    entries = [
        {"updated_at": now, "message_count": 3, "model": "deepseek-v4.1-flash",
         "input_tokens": 1_000_000, "output_tokens": 250_000,
         "cache_read_tokens": 0, "estimated_cost": 0.0},
    ]
    settings = {"pricing_overrides": {
        "deepseek-v4.1-flash": {"input": 0.2, "output": 0.8},
    }}
    data = _call_insights(monkeypatch, tmp_path, entries, now=now, settings=settings)
    # 1M * $0.2/M + 250k * $0.8/M = 0.2 + 0.2 = $0.40
    assert data["total_cost"] == pytest.approx(0.40)
    model = data["models"][0]
    assert model["cost"] == pytest.approx(0.40)
    assert model["cost_status"] == "override"
    assert data["unknown_cost_sessions"] == 0


def test_insights_pricing_override_free_marker_is_known_zero_not_unknown(monkeypatch, tmp_path):
    """`free: true` marks the route $0.00 — counted as priced-free, not unknown."""
    now = _now()
    entries = [
        {"updated_at": now, "message_count": 1, "model": "qwen3-coder",
         "input_tokens": 5000, "output_tokens": 1000,
         "cache_read_tokens": 0, "estimated_cost": 0.0},
    ]
    settings = {"pricing_overrides": {"qwen3-coder": {"free": True}}}
    data = _call_insights(monkeypatch, tmp_path, entries, now=now, settings=settings)
    assert data["total_cost"] == 0.0
    assert data["unknown_cost_sessions"] == 0
    assert data["models"][0]["cost_status"] == "free"


def test_insights_override_never_reprices_a_session_with_a_stored_cost(monkeypatch, tmp_path):
    """Overrides fill gaps only — a real stored cost always wins."""
    now = _now()
    entries = [
        {"updated_at": now, "message_count": 3, "model": "gpt-5.5",
         "input_tokens": 1_000_000, "output_tokens": 250_000,
         "cache_read_tokens": 0, "estimated_cost": 1.23},
    ]
    settings = {"pricing_overrides": {"gpt-5.5": {"input": 100.0, "output": 100.0}}}
    data = _call_insights(monkeypatch, tmp_path, entries, now=now, settings=settings)
    assert data["total_cost"] == pytest.approx(1.23)
    assert data["models"][0]["cost_status"] == "priced"


def test_insights_unpriced_sessions_are_counted_and_listed(monkeypatch, tmp_path):
    """Sessions with no cost and no override stay unknown but become VISIBLE."""
    now = _now()
    entries = [
        {"updated_at": now, "message_count": 3, "model": "glm-5.3",
         "input_tokens": 100, "output_tokens": 50,
         "cache_read_tokens": 0, "estimated_cost": 0.0},
        {"updated_at": now, "message_count": 1, "model": "glm-5.3",
         "input_tokens": 10, "output_tokens": 5,
         "cache_read_tokens": 0, "estimated_cost": 0.0},
        {"updated_at": now, "message_count": 2, "model": "gpt-5.5",
         "input_tokens": 100, "output_tokens": 50,
         "cache_read_tokens": 0, "estimated_cost": 0.5},
    ]
    data = _call_insights(monkeypatch, tmp_path, entries, now=now)
    assert data["unknown_cost_sessions"] == 2
    by_model = {m["model"]: m for m in data["models"]}
    assert by_model["glm-5.3"]["cost_status"] == "unknown"
    assert by_model["gpt-5.5"]["cost_status"] == "priced"
    unpriced = {m["model"]: m["sessions"] for m in data["models_without_pricing"]}
    assert unpriced == {"glm-5.3": 2}


def test_insights_cli_state_db_sessions_honor_cost_status_and_overrides(monkeypatch, tmp_path):
    """CLI sessions from state.db: cost_status='free' is known-zero; an
    override rates in the gap; 'unknown' without override stays unknown."""
    now = _now()
    state_rows = [
        # free by cost_status
        {"id": "c1", "source": "cli", "model": "qwen-local", "message_count": 1,
         "input_tokens": 100, "output_tokens": 10, "estimated_cost_usd": 0.0,
         "billing_provider": "lmstudio", "cost_status": "free",
         "started_at": now, "ended_at": now},
        # unknown by cost_status, but an override exists
        {"id": "c2", "source": "cli", "model": "kimi-k2.7-code", "message_count": 1,
         "input_tokens": 1_000_000, "output_tokens": 0, "estimated_cost_usd": 0.0,
         "billing_provider": "ollama-cloud", "cost_status": "unknown",
         "started_at": now, "ended_at": now},
        # unknown, no override
        {"id": "c3", "source": "cli", "model": "mystery-model", "message_count": 1,
         "input_tokens": 100, "output_tokens": 10, "estimated_cost_usd": 0.0,
         "billing_provider": "ollama-cloud", "cost_status": "unknown",
         "started_at": now, "ended_at": now},
    ]
    settings = {"pricing_overrides": {"kimi-k2.7-code": {"input": 1.0}}}
    data = _call_insights(monkeypatch, tmp_path, [], now=now, settings=settings,
                          state_rows=state_rows)
    assert data["total_cost"] == pytest.approx(1.0)
    assert data["unknown_cost_sessions"] == 1
    by_model = {m["model"]: m for m in data["models"]}
    assert by_model["qwen-local"]["cost_status"] == "free"
    assert by_model["kimi-k2.7-code"]["cost_status"] == "override"
    assert by_model["mystery-model"]["cost_status"] == "unknown"
    unpriced = {m["model"]: m["sessions"] for m in data["models_without_pricing"]}
    assert unpriced == {"mystery-model": 1}


def test_insights_cache_read_tokens_are_priced_by_override(monkeypatch, tmp_path):
    now = _now()
    entries = [
        {"updated_at": now, "message_count": 1, "model": "cached-model",
         "input_tokens": 100_000, "output_tokens": 0,
         "cache_read_tokens": 900_000, "estimated_cost": 0.0},
    ]
    settings = {"pricing_overrides": {
        "cached-model": {"input": 1.0, "cache_read": 0.1},
    }}
    data = _call_insights(monkeypatch, tmp_path, entries, now=now, settings=settings)
    # 100k * $1/M + 900k * $0.1/M = 0.1 + 0.09 = $0.19
    assert data["total_cost"] == pytest.approx(0.19)


# ── settings persistence/validation ─────────────────────────────────────────


def test_pricing_overrides_setting_is_allowlisted_and_validated(monkeypatch, tmp_path):
    import api.config as config

    assert "pricing_overrides" in config._SETTINGS_ALLOWED_KEYS

    settings_file = tmp_path / "settings.json"
    monkeypatch.setattr(config, "SETTINGS_FILE", settings_file)

    saved = config.save_settings({"pricing_overrides": {
        "deepseek-v4.1-flash": {"input": 0.2, "output": 0.8, "cache_read": -5, "bogus": "x"},
        "lmstudio:qwen3-coder": {"free": True},
        42: {"input": 1.0},                      # non-str key dropped
        "junk-entry": "not-a-dict",              # non-dict entry dropped
        "": {"free": True},                      # empty key dropped
    }})
    assert saved["pricing_overrides"] == {
        "deepseek-v4.1-flash": {"input": 0.2, "output": 0.8},
        "lmstudio:qwen3-coder": {"free": True},
    }

    # A non-dict payload must not clobber the stored value.
    saved = config.save_settings({"pricing_overrides": "nope"})
    assert saved["pricing_overrides"] == {
        "deepseek-v4.1-flash": {"input": 0.2, "output": 0.8},
        "lmstudio:qwen3-coder": {"free": True},
    }


def test_pricing_overrides_survives_reload_roundtrip(monkeypatch, tmp_path):
    import api.config as config

    settings_file = tmp_path / "settings.json"
    monkeypatch.setattr(config, "SETTINGS_FILE", settings_file)
    config.save_settings({"pricing_overrides": {"ollama:llama": {"free": True}}})
    assert config.load_settings()["pricing_overrides"] == {"ollama:llama": {"free": True}}


def test_settings_default_pricing_overrides_is_empty_dict():
    import api.config as config
    assert config._SETTINGS_DEFAULTS["pricing_overrides"] == {}


# ── frontend presentation ───────────────────────────────────────────────────


def test_frontend_fmtcost_distinguishes_free_zero_from_unknown():
    """fmtCost renders $0.00 for a known-free route and N/A only for unknown."""
    assert "fmtCost" in PANELS_JS
    # The formatter must accept a status argument instead of mapping every
    # nonpositive amount straight to the N/A string.
    assert "insights_no_cost" in PANELS_JS
    assert "cost_status" in PANELS_JS


def test_frontend_cost_card_explains_unknown_pricing():
    """The Estimated Cost card carries the unknown-session hint + settings hint."""
    assert "unknown_cost_sessions" in PANELS_JS
    assert "insights_cost_unknown_hint" in PANELS_JS
    assert "insights_cost_unknown_hint" in I18N_JS
    # models_without_pricing powers the per-model breakdown of the N/A.
    assert "models_without_pricing" in PANELS_JS


def test_frontend_settings_surface_for_pricing_overrides():
    """Settings modal exposes a JSON textarea for pricing overrides."""
    assert "settingsPricingOverrides" in INDEX_HTML
    assert "settingsPricingOverrides" in PANELS_JS
    assert "pricing_overrides" in PANELS_JS
    assert "settings_label_pricing_overrides" in I18N_JS
