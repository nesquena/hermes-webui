"""Codex CLI session bridge: threads-table scan, rollout parsing, path safety."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

_THREADS_DDL = """
CREATE TABLE threads (
    id TEXT PRIMARY KEY,
    rollout_path TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    source TEXT NOT NULL,
    model_provider TEXT NOT NULL,
    cwd TEXT NOT NULL,
    title TEXT NOT NULL,
    archived INTEGER NOT NULL DEFAULT 0,
    first_user_message TEXT NOT NULL DEFAULT '',
    preview TEXT NOT NULL DEFAULT '',
    model TEXT,
    name TEXT,
    git_branch TEXT,
    git_origin_url TEXT
);
"""

THREAD_A = "019fb931-6fde-70f3-b787-581cb3e15294"
THREAD_B = "019e20d1-3470-71f1-acff-a7d38bcc1f2f"


def _rollout_rows() -> list[dict]:
    """A realistic rollout: meta + events + injected context + a real exchange."""
    return [
        {
            "timestamp": "2026-07-31T17:21:21.230Z",
            "type": "session_meta",
            "payload": {"session_id": THREAD_A, "cwd": "/work", "cli_version": "0.146.0"},
        },
        {"timestamp": "2026-07-31T17:21:21.240Z", "type": "event_msg", "payload": {"type": "task_started"}},
        {
            "timestamp": "2026-07-31T17:21:21.682Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "developer",
                "content": [{"type": "input_text", "text": "<permissions instructions>\nsecret preamble"}],
            },
        },
        {
            "timestamp": "2026-07-31T17:21:21.683Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "# AGENTS.md instructions\n<INSTRUCTIONS>x</INSTRUCTIONS>"},
                    {"type": "input_text", "text": "<environment_context>\n  <cwd>/work</cwd>\n</environment_context>"},
                ],
            },
        },
        {
            "timestamp": "2026-07-31T17:21:25.814Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "hi"}],
            },
        },
        {
            "timestamp": "2026-07-31T17:21:26.000Z",
            "type": "response_item",
            "payload": {"type": "function_call", "name": "shell", "arguments": "{}"},
        },
        {
            "timestamp": "2026-07-31T17:21:26.500Z",
            "type": "response_item",
            "payload": {"type": "reasoning", "summary": []},
        },
        {
            "timestamp": "2026-07-31T17:21:28.023Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "Hi — what would you like to work on?"}],
            },
        },
        "not a dict",
        {"type": "response_item", "payload": {"type": "message", "role": "assistant", "content": []}},
    ]


def _write_rollout(path: Path, rows: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(row) if not isinstance(row, str) else row for row in rows) + "\n",
        encoding="utf-8",
    )


def _make_codex_home(tmp_path: Path, *, extra_rows: list[tuple] = ()) -> Path:
    home = tmp_path / "codex"
    rollout_a = home / "sessions" / "2026" / "08" / "01" / f"rollout-2026-08-01T01-20-52-{THREAD_A}.jsonl"
    _write_rollout(rollout_a, _rollout_rows())

    home.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(home / "state_5.sqlite") as conn:
        conn.executescript(_THREADS_DDL)
        conn.execute(
            "INSERT INTO threads (id, rollout_path, created_at, updated_at, source, "
            "model_provider, cwd, title, archived, first_user_message, preview, model, git_branch) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                THREAD_A,
                str(rollout_a),
                1785518452,
                1785518492,
                "cli",
                "openai",
                "/work",
                "hi",
                0,
                "hi",
                "hi",
                "gpt-5.6-terra",
                "main",
            ),
        )
        for row in extra_rows:
            conn.execute(
                "INSERT INTO threads (id, rollout_path, created_at, updated_at, source, "
                "model_provider, cwd, title, archived) VALUES (?, ?, ?, ?, 'cli', 'openai', '/work', ?, ?)",
                row,
            )
    return home


@pytest.fixture(autouse=True)
def _clear_codex_cache():
    from api.codex_sessions import clear_codex_parse_cache

    clear_codex_parse_cache()
    yield
    clear_codex_parse_cache()


def test_default_codex_scan_is_disabled_inside_test_state(monkeypatch, tmp_path):
    """Test runs must not accidentally scan the developer's real ~/.codex."""
    import api.codex_sessions as codex

    monkeypatch.delenv("HERMES_WEBUI_CODEX_HOME", raising=False)
    monkeypatch.setenv("HERMES_WEBUI_TEST_STATE_DIR", str(tmp_path / "state"))

    assert codex.codex_home() is None
    assert codex.get_codex_sessions() == []
    assert codex.get_codex_session_messages(codex.codex_session_id(THREAD_A)) == []


def test_get_codex_sessions_reads_threads_table(tmp_path):
    import api.codex_sessions as codex

    home = _make_codex_home(tmp_path)
    sessions = codex.get_codex_sessions(home_dir=home)

    assert len(sessions) == 1
    session = sessions[0]
    assert session["session_id"] == f"codex_{THREAD_A}"
    assert session["title"] == "hi"
    assert session["model"] == "gpt-5.6-terra"
    assert session["message_count"] == 2
    assert session["source_tag"] == "codex"
    assert session["raw_source"] == "codex"
    assert session["session_source"] == "external_agent"
    assert session["source_label"] == "Codex"
    assert session["is_cli_session"] is True
    assert session["read_only"] is True
    assert session["workspace"] == "/work"
    assert session["created_at"] == 1785518452
    assert session["updated_at"] == 1785518492


def test_rollout_parsing_keeps_only_real_user_and_assistant_turns(tmp_path):
    import api.codex_sessions as codex

    home = _make_codex_home(tmp_path)
    messages = codex.get_codex_session_messages(f"codex_{THREAD_A}", home_dir=home)

    assert [m["role"] for m in messages] == ["user", "assistant"]
    assert messages[0]["content"] == "hi"
    assert messages[1]["content"] == "Hi — what would you like to work on?"
    # developer turns, injected AGENTS.md/environment context, tool calls,
    # reasoning, event_msg and session_meta rows are all dropped.
    joined = "\n".join(m["content"] for m in messages)
    assert "secret preamble" not in joined
    assert "AGENTS.md" not in joined
    assert "environment_context" not in joined
    assert all("timestamp" in m for m in messages)


def test_real_user_text_survives_batched_context_parts(tmp_path):
    """A real prompt batched into the same content list as context blobs is kept."""
    import api.codex_sessions as codex

    home = _make_codex_home(tmp_path)
    rollout = home / "sessions" / "2026" / "08" / "01" / f"rollout-2026-08-01T01-20-52-{THREAD_A}.jsonl"
    _write_rollout(
        rollout,
        [
            {
                "timestamp": "2026-08-01T00:00:00Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": "<environment_context><cwd>/work</cwd></environment_context>"},
                        {"type": "input_text", "text": "refactor the parser"},
                    ],
                },
            },
        ],
    )

    messages = codex.get_codex_session_messages(f"codex_{THREAD_A}", home_dir=home)
    assert messages == [
        {"role": "user", "content": "refactor the parser", "timestamp": 1785542400.0},
    ]


def test_archived_and_pathless_threads_are_skipped(tmp_path):
    import api.codex_sessions as codex

    archived = (THREAD_B, "/nowhere/rollout.jsonl", 1, 2, "archived thread", 1)
    home = _make_codex_home(tmp_path, extra_rows=[archived])

    ids = [s["session_id"] for s in codex.get_codex_sessions(home_dir=home)]
    assert ids == [f"codex_{THREAD_A}"]


def test_rollout_path_outside_codex_sessions_is_rejected(tmp_path):
    """A tampered threads row must not be able to read arbitrary files."""
    import api.codex_sessions as codex

    home = _make_codex_home(tmp_path)
    outside = tmp_path / "outside" / "secrets.jsonl"
    _write_rollout(
        outside,
        [
            {
                "type": "response_item",
                "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "leak"}]},
            }
        ],
    )
    with sqlite3.connect(home / "state_5.sqlite") as conn:
        conn.execute("UPDATE threads SET rollout_path = ? WHERE id = ?", (str(outside), THREAD_A))

    assert codex.get_codex_sessions(home_dir=home) == []
    assert codex.get_codex_session_messages(f"codex_{THREAD_A}", home_dir=home) == []


def test_symlinked_rollout_is_rejected(tmp_path):
    import api.codex_sessions as codex

    home = _make_codex_home(tmp_path)
    outside = tmp_path / "outside" / "secrets.jsonl"
    _write_rollout(
        outside,
        [
            {
                "type": "response_item",
                "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "leak"}]},
            }
        ],
    )
    link = home / "sessions" / "2026" / "08" / "01" / "linked.jsonl"
    link.symlink_to(outside)
    with sqlite3.connect(home / "state_5.sqlite") as conn:
        conn.execute("UPDATE threads SET rollout_path = ? WHERE id = ?", (str(link), THREAD_A))

    assert codex.get_codex_sessions(home_dir=home) == []


@pytest.mark.parametrize(
    "candidate",
    [
        "",
        None,
        "codex_",
        "codex_../../etc/passwd",
        "codex_not-a-uuid",
        "019fb931-6fde-70f3-b787",
        "claude_code_abc123",
    ],
)
def test_invalid_session_ids_are_rejected(candidate):
    import api.codex_sessions as codex

    assert codex.thread_id_from_session_id(candidate) is None
    assert codex.is_codex_session_id(candidate) is False


@pytest.mark.parametrize("candidate", [THREAD_A, f"codex_{THREAD_A}", THREAD_A.upper()])
def test_valid_session_ids_normalize_to_the_thread_uuid(candidate):
    import api.codex_sessions as codex

    assert codex.thread_id_from_session_id(candidate) == THREAD_A


def test_missing_codex_install_returns_empty(tmp_path):
    import api.codex_sessions as codex

    assert codex.get_codex_sessions(home_dir=tmp_path / "absent") == []
    assert codex.get_codex_session_messages(f"codex_{THREAD_A}", home_dir=tmp_path / "absent") == []
    assert codex.get_codex_session_detail(THREAD_A, home_dir=tmp_path / "absent") is None


def test_session_detail_carries_metadata_and_messages(tmp_path):
    import api.codex_sessions as codex

    home = _make_codex_home(tmp_path)
    detail = codex.get_codex_session_detail(THREAD_A, home_dir=home)

    assert detail is not None
    assert detail["session_id"] == f"codex_{THREAD_A}"
    assert detail["source"] == "cli"
    assert detail["cwd"] == "/work"
    assert detail["git_branch"] == "main"
    assert [m["role"] for m in detail["messages"]] == ["user", "assistant"]
    assert codex.get_codex_session_detail(THREAD_B, home_dir=home) is None


def test_parse_cache_is_keyed_on_file_stat(tmp_path):
    import api.codex_sessions as codex

    home = _make_codex_home(tmp_path)
    rollout = home / "sessions" / "2026" / "08" / "01" / f"rollout-2026-08-01T01-20-52-{THREAD_A}.jsonl"

    calls = 0
    real_parse = codex._parse_codex_rollout_impl

    def counting_parse(path, **kwargs):
        nonlocal calls
        calls += 1
        return real_parse(path, **kwargs)

    codex._parse_codex_rollout_impl = counting_parse
    try:
        first = codex.parse_codex_rollout_cached(rollout)
        second = codex.parse_codex_rollout_cached(rollout)
        assert calls == 1
        assert first == second
        # A caller mutating the returned list must not corrupt the cache entry.
        second.clear()
        assert codex.parse_codex_rollout_cached(rollout) == first
        assert calls == 1

        _write_rollout(
            rollout,
            [
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "changed"}],
                    },
                }
            ],
        )
        third = codex.parse_codex_rollout_cached(rollout)
        assert calls == 2
        assert [m["content"] for m in third] == ["changed"]
    finally:
        codex.parse_codex_rollout = real_parse


def test_cli_session_messages_dispatches_codex_ids(monkeypatch, tmp_path):
    """get_cli_session_messages must route codex_<uuid> to the Codex reader."""
    import api.models as models

    home = _make_codex_home(tmp_path)
    monkeypatch.setenv("HERMES_WEBUI_CODEX_HOME", str(home))

    messages = models.get_cli_session_messages(f"codex_{THREAD_A}")
    assert [m["role"] for m in messages] == ["user", "assistant"]


def test_cli_sessions_include_and_exclude_codex_rows(monkeypatch, tmp_path):
    import api.models as models

    home = _make_codex_home(tmp_path)
    monkeypatch.setenv("HERMES_WEBUI_CODEX_HOME", str(home))
    monkeypatch.setattr(models, "get_claude_code_sessions", lambda: [])
    monkeypatch.setattr(models, "_CLI_SESSIONS_CACHE_TTL_SECONDS", 0.0, raising=False)
    models.clear_cli_sessions_cache()

    included = models.get_cli_sessions(include_codex=True)
    assert f"codex_{THREAD_A}" in [s["session_id"] for s in included]

    excluded = models.get_cli_sessions(include_codex=False)
    assert f"codex_{THREAD_A}" not in [s["session_id"] for s in excluded]


def test_codex_source_filter_returns_only_codex_rows(monkeypatch, tmp_path):
    import api.models as models

    home = _make_codex_home(tmp_path)
    monkeypatch.setenv("HERMES_WEBUI_CODEX_HOME", str(home))
    monkeypatch.setattr(models, "get_claude_code_sessions", lambda: [])
    monkeypatch.setattr(models, "_CLI_SESSIONS_CACHE_TTL_SECONDS", 0.0, raising=False)
    models.clear_cli_sessions_cache()

    rows = models.get_cli_sessions(source_filter="codex")
    assert [s["session_id"] for s in rows] == [f"codex_{THREAD_A}"]


def test_session_list_cache_key_changes_with_codex_toggle():
    import api.routes as routes

    common = dict(
        active_profile="default",
        all_profiles=False,
        show_cli_sessions=True,
        show_previous_messaging_sessions=False,
        show_cron_sessions=False,
    )
    assert routes._session_list_cache_key(show_codex_sessions=False, **common) != routes._session_list_cache_key(
        show_codex_sessions=True, **common
    )
    # Default keeps Codex rows enabled, matching the settings default.
    assert routes._session_list_cache_key(**common) == routes._session_list_cache_key(
        show_codex_sessions=True, **common
    )


def test_codex_styling_and_settings_wiring_are_present():
    style_css = (ROOT / "static" / "style.css").read_text(encoding="utf-8")
    index_html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    panels_js = (ROOT / "static" / "panels.js").read_text(encoding="utf-8")

    assert 'data-source-key="codex"' in style_css
    assert '.session-item.cli-session[data-source-key="codex"]' in style_css
    assert 'id="settingsShowCodexSessions"' in index_html
    assert "settingsShowCodexSessions" in panels_js
    assert "body.show_codex_sessions=showCodexSessions;" in panels_js


def test_show_codex_sessions_is_a_known_bool_setting():
    from api.config import _SETTINGS_BOOL_KEYS, _SETTINGS_DEFAULTS

    assert _SETTINGS_DEFAULTS["show_codex_sessions"] is True
    assert "show_codex_sessions" in _SETTINGS_BOOL_KEYS


def test_parse_keeps_newest_messages_and_reports_truncation(tmp_path):
    """Long rollouts keep the NEWEST turns (not the oldest) and flag truncation."""
    import api.codex_sessions as codex

    rollout = tmp_path / "long.jsonl"
    rows = []
    for i in range(5):
        rows.append(
            {
                "timestamp": f"2026-08-01T00:00:{i:02d}.000Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": f"turn-{i}",
                },
            }
        )
    _write_rollout(rollout, rows)

    messages, truncated = codex._parse_codex_rollout_impl(rollout, max_messages=3)
    assert truncated is True
    # The window holds the newest 3 of 5 rendered turns.
    assert [m["content"] for m in messages] == ["turn-2", "turn-3", "turn-4"]
    # First message is the oldest RETAINED one (not the true transcript head),
    # so only the oldest turns were dropped.
    assert messages[0]["content"] == "turn-2"

    # Within the cap: no truncation, all turns kept in order.
    messages2, truncated2 = codex._parse_codex_rollout_impl(rollout, max_messages=10)
    assert truncated2 is False
    assert [m["content"] for m in messages2] == [f"turn-{i}" for i in range(5)]


def test_detail_exposes_truncated_flag(tmp_path):
    """get_codex_session_detail sets truncated when the rollout exceeds the cap."""
    import api.codex_sessions as codex

    home = _make_codex_home(tmp_path)
    # Overwrite the single thread's rollout with a long one exceeding the cap.
    rollout = (
        home
        / "sessions"
        / "2026"
        / "08"
        / "01"
        / f"rollout-2026-08-01T01-20-52-{THREAD_A}.jsonl"
    )
    rows = []
    for i in range(codex.CODEX_MAX_MESSAGES_PER_FILE + 5):
        rows.append(
            {
                "timestamp": f"2026-08-01T00:00:{i % 60:02d}.000Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": f"turn-{i}",
                },
            }
        )
    _write_rollout(rollout, rows)

    detail = codex.get_codex_session_detail(f"codex_{THREAD_A}", home_dir=home)
    assert detail is not None
    assert detail["truncated"] is True
    assert len(detail["messages"]) == codex.CODEX_MAX_MESSAGES_PER_FILE
    # The retained window starts after the dropped head.
    assert detail["messages"][0]["content"] == "turn-5"
    assert detail["messages"][-1]["content"] == f"turn-{codex.CODEX_MAX_MESSAGES_PER_FILE + 4}"
