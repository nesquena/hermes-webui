"""Tests for the sidebar's high-level session-origin taxonomy."""

from pathlib import Path
import json
import shutil
import subprocess

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")


def _extract_function(source_text, function_name):
    marker = f"function {function_name}("
    start = source_text.index(marker)
    brace_start = source_text.index("{", start)
    depth = 0
    for index in range(brace_start, len(source_text)):
        char = source_text[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return source_text[start : index + 1]
    raise AssertionError(f"Could not extract {function_name}")


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_source_filter_model_keeps_every_origin_readable_without_a_tab_strip():
    """Dropping or truncating dynamic adapters must break the source-control contract."""
    source = (REPO_ROOT / "static" / "sessions.js").read_text(encoding="utf-8")
    normalize_fn = _extract_function(source, "_normalizeSessionSourceFilters")
    model_fn = _extract_function(source, "_sessionSourceFilterModel")
    script = f"""
global._sessionSourceFilters = ['matrix', 'telegram', 'slack', 'discord'];
global._serverSessionOriginCounts = {{webui: 17, cli: 2, matrix: 220, telegram: 14, slack: 8, discord: 5}};
global._serverSessionOriginLabels = {{
  webui: 'WebUI sessions',
  cli: 'CLI sessions',
  matrix: 'Matrix sessions',
  telegram: 'Telegram sessions',
  slack: 'Slack sessions',
  discord: 'Discord sessions',
}};
global._sessionOriginKeys = () => ['webui', 'cli', 'matrix', 'telegram', 'slack', 'discord'];
global._sessionSourceTabCount = (origin) => global._serverSessionOriginCounts[origin];
global._sessionSourceLabel = (origin, count) => `${{global._serverSessionOriginLabels[origin]}} (${{count}})`;
global._sessionOriginLabel = (origin) => global._serverSessionOriginLabels[origin];
{normalize_fn}
{model_fn}
console.log(JSON.stringify(_sessionSourceFilterModel(null, null)));
"""
    result = subprocess.run([NODE, "-e", script], capture_output=True, text=True, check=True)
    model = json.loads(result.stdout)

    assert model == {
        "selectedOrigins": ["matrix", "telegram", "slack", "discord"],
        "visibleChips": [
            {"origin": "matrix", "label": "Matrix sessions"},
            {"origin": "telegram", "label": "Telegram sessions"},
        ],
        "overflowCount": 2,
        "originCount": 6,
        "items": [
            {"origin": "webui", "label": "WebUI sessions", "count": 17, "selected": False},
            {"origin": "cli", "label": "CLI sessions", "count": 2, "selected": False},
            {"origin": "matrix", "label": "Matrix sessions", "count": 220, "selected": True},
            {"origin": "telegram", "label": "Telegram sessions", "count": 14, "selected": True},
            {"origin": "slack", "label": "Slack sessions", "count": 8, "selected": True},
            {"origin": "discord", "label": "Discord sessions", "count": 5, "selected": True},
        ],
    }


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_source_menu_item_uses_checkbox_and_reports_immediate_checked_state():
    source = (REPO_ROOT / "static" / "sessions.js").read_text(encoding="utf-8")
    render_fn = _extract_function(source, "_renderSessionSourceMenuItem")
    script = f"""
global.document = {{
  createElement(tag) {{
    return {{
      tagName: tag.toUpperCase(), type: '', className: '', textContent: '', checked: false,
      dataset: {{}}, attrs: {{}},
      children: [],
      appendChild(child) {{ this.children.push(child); }},
      setAttribute(key, value) {{ this.attrs[key] = value; }},
    }};
  }},
}};
{render_fn}
const changes = [];
const row = _renderSessionSourceMenuItem(
  {{origin:'slack', label:'Slack sessions', count:8, selected:false}},
  (origin, selected) => changes.push([origin, selected])
);
const checkbox = row.children[0];
checkbox.checked = true;
checkbox.onchange({{stopPropagation(){{}}}});
console.log(JSON.stringify({{
  rowTag: row.tagName,
  checkboxTag: checkbox.tagName,
  checkboxType: checkbox.type,
  origin: checkbox.dataset.origin,
  initialSelected: row.attrs['aria-checked'],
  changes,
}}));
"""
    result = subprocess.run([NODE, "-e", script], capture_output=True, text=True, check=True)
    assert json.loads(result.stdout) == {
        "rowTag": "LABEL",
        "checkboxTag": "INPUT",
        "checkboxType": "checkbox",
        "origin": "slack",
        "initialSelected": "false",
        "changes": [["slack", True]],
    }


def test_sidebar_origin_preserves_raw_channel_identity():
    from api.routes import _normalize_sidebar_source_flags, _sidebar_session_origin

    cases = {
        "webui": "webui",
        "cli": "cli",
        "tui": "tui",
        "matrix": "matrix",
        "telegram": "telegram",
        "slack": "slack",
        "discord": "discord",
        "api_server": "api",
    }
    for raw, expected in cases.items():
        row = {"source_tag": raw, "session_source": "messaging"}
        assert _sidebar_session_origin(row) == expected
        assert _normalize_sidebar_source_flags(row)["session_origin"] == expected


def test_sidebar_origin_defaults_blank_rows_to_webui_and_unknown_rows_to_their_source():
    from api.routes import _sidebar_session_origin

    assert _sidebar_session_origin({"session_id": "native"}) == "webui"
    assert _sidebar_session_origin({"source_tag": "new_adapter"}) == "new_adapter"
    assert _sidebar_session_origin({"session_source": "cli", "is_cli_session": True}) == "cli"


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_client_origin_preserves_legacy_webui_markers():
    source = (REPO_ROOT / "static" / "sessions.js").read_text(encoding="utf-8")
    origin_fn = _extract_function(source, "_sessionOrigin")
    script = f"""
const _SESSION_ORIGIN_ORDER = ['webui','cli','subagent','other'];
function _isCliSession() {{ return false; }}
{origin_fn}
console.log(JSON.stringify([
  _sessionOrigin({{source:'webui', session_source:'webui'}}),
  _sessionOrigin({{raw_source:'webui'}}),
  _sessionOrigin({{source_tag:'webui'}}),
]));
"""
    result = subprocess.run([NODE, "-e", script], capture_output=True, text=True, check=True)
    assert json.loads(result.stdout) == ["webui", "webui", "webui"]


def test_sidebar_payload_exposes_origin_metadata_and_dynamic_filtering_contract(monkeypatch):
    import api.routes as routes
    import api.profiles as profiles

    rows = []
    for sid, source in (("matrix-1", "matrix"), ("telegram-1", "telegram"), ("tui-1", "tui")):
        rows.append({
            "session_id": sid,
            "title": sid,
            "profile": "default",
            "source_tag": source,
            "raw_source": source,
            "session_source": "messaging" if source in {"matrix", "telegram"} else "cli",
            "source_label": source.title(),
            "message_count": 2,
            "actual_message_count": 2,
            "updated_at": 10,
            "last_message_at": 10,
            "archived": False,
        })
    monkeypatch.setattr(routes, "all_sessions", lambda diag=None: [])
    monkeypatch.setattr(routes, "get_cli_sessions", lambda **_kwargs: list(rows))
    monkeypatch.setattr(routes, "_enrich_sidebar_lineage_metadata", lambda _rows: None)
    monkeypatch.setattr(routes, "_reconcile_stale_stream_state_for_session_rows", lambda _rows: False)
    monkeypatch.setattr(profiles, "get_active_profile_name", lambda: "default")

    payload = routes._build_session_list_cache_payload(
        active_profile="default",
        all_profiles=False,
        show_cli_sessions=True,
        show_previous_messaging_sessions=True,
        show_cron_sessions=False,
        show_matrix_sessions=True,
        sidebar_source="matrix",
        visible_only=True,
    )
    assert [row["session_id"] for row in payload["sessions"]] == ["matrix-1"]
    assert payload["session_origin_counts"] == {"matrix": 1, "telegram": 1, "tui": 1}
    assert payload["session_origin_labels"]["matrix"] == "Matrix sessions"


def test_webui_filtered_payload_counts_available_state_db_origins(monkeypatch, tmp_path):
    import sqlite3
    import api.routes as routes

    db_path = tmp_path / "state.db"
    con = sqlite3.connect(db_path)
    con.execute("create table sessions (id text primary key, source text)")
    con.executemany(
        "insert into sessions (id, source) values (?, ?)",
        [("matrix-1", "matrix"), ("telegram-1", "telegram")],
    )
    con.commit()
    con.close()

    monkeypatch.setattr(routes, "_active_state_db_path", lambda: db_path)
    webui_row = {
        "session_id": "webui-1",
        "title": "WebUI session",
        "profile": "default",
        "source": "webui",
        "message_count": 2,
        "updated_at": 20,
        "last_message_at": 20,
        "archived": False,
    }
    monkeypatch.setattr(routes, "all_sessions", lambda diag=None: [dict(webui_row)])
    monkeypatch.setattr(routes, "_reconcile_stale_stream_state_for_session_rows", lambda _rows: False)
    monkeypatch.setattr(routes, "get_cli_sessions", lambda **_kwargs: [])

    payload = routes._build_session_list_cache_payload(
        active_profile="default",
        all_profiles=False,
        show_cli_sessions=False,
        show_previous_messaging_sessions=False,
        show_cron_sessions=False,
        show_matrix_sessions=False,
        sidebar_source="webui",
    )

    assert [row["session_id"] for row in payload["sessions"]] == ["webui-1"]
    assert payload["session_origin_counts"] == {
        "webui": 1,
        "matrix": 1,
        "telegram": 1,
    }


def test_sidebar_payload_exposes_origin_metadata_fields():
    routes = REPO_ROOT / "api" / "routes.py"
    source = routes.read_text(encoding="utf-8")
    assert '"session_origin_counts"' in source
    assert '"session_origin_labels"' in source
    assert "_sidebar_session_origin(s) in selected_sidebar_source_set" in source
    assert '"session_origin",' in source


def test_sidebar_frontend_renders_origin_tabs_and_accepts_non_cli_origins():
    source = (REPO_ROOT / "static" / "sessions.js").read_text(encoding="utf-8")
    assert "_serverSessionOriginCounts" in source
    assert "session_origin_counts" in source
    assert "_sessionOriginKeys" in source
    assert "selectedOrigins.has(_sessionOrigin(s))" in source
    assert "session_origin" in source
