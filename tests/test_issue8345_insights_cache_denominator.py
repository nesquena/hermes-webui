"""#8345: Insights cache % was capped at 50% for WebUI sessions.

WebUI session rows store the full prompt (uncached + cache reads + cache
writes) in input_tokens. Agent state.db rows store uncached input only, with
cache reads and writes in their own columns. Insights now brings state.db rows
to the full-prompt meaning before summing, and divides cache reads by that
total once.
"""

import json
import pathlib
import shutil
import sqlite3
import subprocess
import textwrap
import time
from types import SimpleNamespace

import pytest

from tests.test_insights import _FakeHandler

REPO_ROOT = pathlib.Path(__file__).parent.parent.resolve()
PANELS_JS = (REPO_ROOT / "static" / "panels.js").read_text(encoding="utf-8")

NOW = time.mktime((2026, 10, 17, 12, 0, 0, 0, 0, -1))

# One session, described the way each source stores it:
# 36,522 uncached + 20,703,305 cache reads + 403,799 cache writes.
UNCACHED = 36_522
READ = 20_703_305
WRITE = 403_799
FULL_PROMPT = UNCACHED + READ + WRITE  # 21,143,626
EXPECTED_PCT = round(READ / FULL_PROMPT * 100)  # 98


def _webui_entry(model="claude-opus"):
    return {
        "session_id": "w1", "updated_at": NOW, "created_at": NOW,
        "message_count": 4, "model": model,
        "input_tokens": FULL_PROMPT, "output_tokens": 1_000,
        "cache_read_tokens": READ, "cache_write_tokens": WRITE,
        "estimated_cost": 1.0,
    }


def _cli_row(model="claude-opus", id_="cli1", source="cli"):
    return {
        "id": id_, "source": source, "model": model, "message_count": 4,
        "input_tokens": UNCACHED, "output_tokens": 1_000,
        "cache_read_tokens": READ, "cache_write_tokens": WRITE,
        "estimated_cost_usd": 1.0, "started_at": NOW, "ended_at": NOW,
    }


def _insights(monkeypatch, tmp_path, entries, state_rows=None, cache_cols=("cache_read_tokens", "cache_write_tokens")):
    import api.models as models
    import api.routes as routes

    session_dir = tmp_path / "sessions"
    session_dir.mkdir(parents=True, exist_ok=True)
    (session_dir / "_index.json").write_text(json.dumps(entries), encoding="utf-8")
    monkeypatch.setattr(routes, "SESSION_DIR", session_dir)
    monkeypatch.setattr(time, "time", lambda: NOW)

    db_path = tmp_path / "state.db"
    if state_rows is not None:
        cols = ["id", "source", "model", "message_count", "input_tokens",
                "output_tokens", "estimated_cost_usd", *cache_cols,
                "started_at", "ended_at"]
        conn = sqlite3.connect(str(db_path))
        conn.execute(f"CREATE TABLE sessions ({', '.join(cols)})")
        for r in state_rows:
            conn.execute(
                f"INSERT INTO sessions ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                [r.get(c) for c in cols],
            )
        conn.commit()
        conn.close()
    monkeypatch.setattr(models, "_active_state_db_path", lambda: db_path)

    handler = _FakeHandler()
    routes._handle_insights(handler, SimpleNamespace(query="days=7"))
    assert handler.status == 200
    return handler.json_body()


def test_webui_session_shows_its_real_cache_rate(monkeypatch, tmp_path):
    data = _insights(monkeypatch, tmp_path, [_webui_entry()])
    assert data["models"][0]["cache_hit_percent"] == EXPECTED_PCT
    assert data["total_cache_hit_percent"] == EXPECTED_PCT
    assert data["total_input_tokens"] == FULL_PROMPT


def test_cli_session_shows_its_real_cache_rate(monkeypatch, tmp_path):
    data = _insights(monkeypatch, tmp_path, [], [_cli_row()])
    model = data["models"][0]
    assert model["cache_hit_percent"] == EXPECTED_PCT
    assert model["input_tokens"] == FULL_PROMPT
    assert model["total_tokens"] == FULL_PROMPT + 1_000
    assert data["total_cache_hit_percent"] == EXPECTED_PCT


def test_same_session_reads_the_same_from_either_source(monkeypatch, tmp_path):
    webui = _insights(monkeypatch, tmp_path / "a", [_webui_entry()])
    cli = _insights(monkeypatch, tmp_path / "b", [], [_cli_row()])
    for key in ("input_tokens", "cache_read_tokens", "cache_hit_percent", "total_tokens"):
        assert webui["models"][0][key] == cli["models"][0][key], key
    assert webui["daily_tokens"] == cli["daily_tokens"]


def test_mixed_model_row_is_not_dragged_toward_50(monkeypatch, tmp_path):
    data = _insights(monkeypatch, tmp_path, [_webui_entry()], [_cli_row()])
    model = data["models"][0]
    assert model["sessions"] == 2
    assert model["input_tokens"] == 2 * FULL_PROMPT
    assert model["cache_hit_percent"] == EXPECTED_PCT
    day = next(d for d in data["daily_tokens"] if d["sessions"])
    assert day["input_tokens"] == 2 * FULL_PROMPT
    assert day["cache_read_tokens"] == 2 * READ


def test_webui_mirror_rows_in_state_db_are_still_skipped(monkeypatch, tmp_path):
    mirror = _cli_row(id_="w1", source="webui")
    mirror["input_tokens"] = FULL_PROMPT
    data = _insights(monkeypatch, tmp_path, [_webui_entry()], [mirror])
    assert data["total_sessions"] == 1
    assert data["total_input_tokens"] == FULL_PROMPT
    assert data["total_cache_hit_percent"] == EXPECTED_PCT


def test_state_db_with_reads_but_no_writes_keeps_its_reads(monkeypatch, tmp_path):
    row = _cli_row()
    data = _insights(monkeypatch, tmp_path, [], [row], cache_cols=("cache_read_tokens",))
    model = data["models"][0]
    assert model["cache_read_tokens"] == READ
    assert model["input_tokens"] == UNCACHED + READ
    assert model["cache_hit_percent"] == round(READ / (UNCACHED + READ) * 100)


def test_state_db_without_cache_columns_still_counts_sessions(monkeypatch, tmp_path):
    data = _insights(monkeypatch, tmp_path, [], [_cli_row()], cache_cols=())
    model = data["models"][0]
    assert data["total_sessions"] == 1
    assert model["input_tokens"] == UNCACHED
    assert model["cache_read_tokens"] == 0
    assert model["cache_hit_percent"] is None


def test_no_cache_reads_still_shows_no_rate(monkeypatch, tmp_path):
    row = _cli_row()
    row["cache_read_tokens"] = 0
    row["cache_write_tokens"] = 0
    data = _insights(monkeypatch, tmp_path, [], [row])
    assert data["models"][0]["cache_hit_percent"] is None
    assert data["total_cache_hit_percent"] is None


def _function_source(src, name):
    start = src.index("function " + name + "(")
    depth = 0
    for i in range(src.index("{", start), len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[start:i + 1]
    raise AssertionError(f"could not find the end of {name}")


def test_daily_tooltip_uses_the_full_prompt_as_denominator():
    node = shutil.which("node")
    if not node:  # pragma: no cover
        pytest.skip("node not available")

    harness = textwrap.dedent(
        """
        globalThis.t = k => k;
        globalThis.esc = s => String(s);
        globalThis.li = () => '';
        globalThis.$ = () => null;
        globalThis.document = { getElementById: () => null, querySelector: () => null, querySelectorAll: () => [] };
        for (const name of ['_renderStaticModelHealthTable', '_renderSkillUsage', '_renderLlmWikiStatus', '_renderSystemHealthPanel']) {
          globalThis[name] = () => '';
        }
        %(bucket)s
        %(render)s
        const box = { innerHTML: '' };
        _renderInsights({
          total_sessions: 1,
          models: [],
          daily_tokens: [{ date: '2026-10-17', input_tokens: 1000, output_tokens: 10, cache_read_tokens: 980, sessions: 1, cost: 0 }],
        }, box, null, null);
        const m = box.innerHTML.match(/insights-daily-bar" title="([^"]*)"/);
        console.log(JSON.stringify({ title: m ? m[1] : null }));
        """
    ) % {
        "bucket": _function_source(PANELS_JS, "_bucketDailyTokensForChart"),
        "render": _function_source(PANELS_JS, "_renderInsights"),
    }
    proc = subprocess.run(
        [node, "--input-type=module", "-e", harness],
        capture_output=True, text=True, timeout=60, check=False,
    )
    assert proc.returncode == 0, f"node harness failed:\n{proc.stdout}\n{proc.stderr}"
    title = json.loads(proc.stdout.strip().splitlines()[-1])["title"]
    assert title is not None
    assert "98% insights_model_cache" in title
