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


# ── PR #6789 review regressions ──────────────────────────────────────────────


def test_large_rollout_still_visible_and_keeps_newest_turns(tmp_path, monkeypatch):
    """Blocker 1: a rollout over the byte cap must NOT vanish.

    Previously ``_resolve_rollout_path`` rejected any file over
    ``CODEX_MAX_ROLLOUT_BYTES``, so a long-lived session disappeared from the
    sidebar and viewer the moment it crossed the threshold. Now the parser
    tail-reads the file and keeps the newest turns, and the session stays
    listed.
    """
    import api.codex_sessions as codex

    home = _make_codex_home(tmp_path)
    rollout = (
        home / "sessions" / "2026" / "08" / "01"
        / f"rollout-2026-08-01T01-20-52-{THREAD_A}.jsonl"
    )
    # Inflate the rollout past the tail-read threshold with many real turns.
    rows = []
    n = 5000
    for i in range(n):
        rows.append({
            "timestamp": f"2026-08-01T00:00:{i % 60:02d}.000Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                # Pad each line so the file comfortably exceeds the cap.
                "content": f"turn-{i}-" + ("x" * 8 * 1024),
            },
        })
    _write_rollout(rollout, rows)
    assert rollout.stat().st_size > codex.CODEX_MAX_ROLLOUT_BYTES

    monkeypatch.setenv("HERMES_WEBUI_CODEX_HOME", str(home))
    codex.clear_codex_parse_cache()

    # The session is still listed (not dropped for being large).
    sessions = codex.get_codex_sessions(home_dir=home)
    assert any(s["session_id"] == f"codex_{THREAD_A}" for s in sessions)

    detail = codex.get_codex_session_detail(THREAD_A, home_dir=home)
    assert detail is not None
    # The newest turns are retained, and truncation is signalled because older
    # turns were omitted by the tail read.
    assert len(detail["messages"]) == codex.CODEX_MAX_MESSAGES_PER_FILE
    assert detail["truncated"] is True
    assert detail["messages"][-1]["content"].startswith(f"turn-{n - 1}-")


def test_get_codex_sessions_message_count_uses_bounded_read(tmp_path, monkeypatch):
    """Blocker 2: the sidebar must not full-parse every rollout for a count.

    ``message_count`` now comes from a bounded tail read. Assert the cold scan
    never invokes the full (1000-message) parser when the bounded counter
    already sees messages, and that the count is still correct for short files.
    """
    import api.codex_sessions as codex

    home = _make_codex_home(tmp_path)
    monkeypatch.setenv("HERMES_WEBUI_CODEX_HOME", str(home))
    codex.clear_codex_parse_cache()

    full_calls = 0
    real_full = codex._parse_codex_rollout_impl

    def counting_full(path, **kwargs):
        nonlocal full_calls
        full_calls += 1
        return real_full(path, **kwargs)

    monkeypatch.setattr(codex, "_parse_codex_rollout_impl", counting_full)
    # The cached wrapper calls the impl through the module-level name the
    # sidebar uses, so rebind the cache miss path too.
    monkeypatch.setattr(
        codex, "_parse_codex_rollout_cached_impl",
        lambda path, **kw: counting_full(path, **kw),
    )

    sessions = codex.get_codex_sessions(home_dir=home)
    assert f"codex_{THREAD_A}" in [s["session_id"] for s in sessions]
    row = next(s for s in sessions if s["session_id"] == f"codex_{THREAD_A}")
    # The fixture rollout renders exactly two messages (user "hi" + assistant).
    assert row["message_count"] == 2
    # The bounded counter saw messages, so the cold scan never fell through to
    # a full parse of the rollout.
    assert full_calls == 0


def test_bounded_counter_caps_read_bytes(tmp_path):
    """Blocker 2: the counter reads at most CODEX_SIDEBAR_COUNT_BYTES."""
    import api.codex_sessions as codex

    rollout = tmp_path / "big.jsonl"
    # Write far more than the cap; only the newest lines should be counted.
    n = 4000
    rows = [
        {
            "type": "response_item",
            "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": f"m-{i}"}]},
        }
        for i in range(n)
    ]
    _write_rollout(rollout, rows)
    assert rollout.stat().st_size > codex.CODEX_SIDEBAR_COUNT_BYTES

    read_bytes = []

    import pathlib

    orig_open = pathlib.Path.open

    def spy_open(self, *args, **kwargs):
        if str(self) == str(rollout) and args and args[0] == "rb":
            fh = orig_open(self, *args, **kwargs)
            return _ReadSpy(fh, read_bytes)
        return orig_open(self, *args, **kwargs)

    class _ReadSpy:
        def __init__(self, fh, sink):
            self._fh = fh
            self._sink = sink

        def __getattr__(self, name):
            return getattr(self._fh, name)

        def read(self, *a, **k):
            data = self._fh.read(*a, **k)
            self._sink.append(len(data))
            return data

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self._fh.close()

    pathlib.Path.open = spy_open
    try:
        count = codex._count_codex_rollout_messages_impl(rollout)
    finally:
        pathlib.Path.open = orig_open

    # Exactly one read, never larger than the cap.
    assert read_bytes and max(read_bytes) <= codex.CODEX_SIDEBAR_COUNT_BYTES
    # Only the tail was visible, so the count is a bounded lower bound (< n).
    assert 0 < count < n


def test_get_cli_session_messages_and_truncated_surfaces_cap(monkeypatch, tmp_path):
    """Blocker 3: the real viewer reader reports truncation.

    The frontend opens a Codex session via import_cli → GET /api/session, which
    reads through ``get_cli_session_messages``. That path must now also carry
    the ``truncated`` bit so the viewer can show an "earlier turns omitted"
    notice without hitting the Codex detail endpoint.
    """
    import api.models as models

    home = _make_codex_home(tmp_path)
    rollout = (
        home / "sessions" / "2026" / "08" / "01"
        / f"rollout-2026-08-01T01-20-52-{THREAD_A}.jsonl"
    )
    rows = [
        {
            "timestamp": f"2026-08-01T00:00:{i % 60:02d}.000Z",
            "type": "response_item",
            "payload": {"type": "message", "role": "user", "content": f"turn-{i}"},
        }
        for i in range(5)
    ]
    _write_rollout(rollout, rows)
    monkeypatch.setenv("HERMES_WEBUI_CODEX_HOME", str(home))
    import api.codex_sessions as codex
    codex.clear_codex_parse_cache()

    sid = f"codex_{THREAD_A}"
    # Within the cap: not truncated.
    msgs, trunc = models.get_cli_session_messages_and_truncated(sid)
    assert trunc is False
    assert [m["content"] for m in msgs] == [f"turn-{i}" for i in range(5)]

    # Over the cap: truncated, newest retained.
    rows = [
        {
            "timestamp": f"2026-08-01T00:00:{i % 60:02d}.000Z",
            "type": "response_item",
            "payload": {"type": "message", "role": "user", "content": f"turn-{i}"},
        }
        for i in range(codex.CODEX_MAX_MESSAGES_PER_FILE + 3)
    ]
    _write_rollout(rollout, rows)
    codex.clear_codex_parse_cache()
    msgs, trunc = models.get_cli_session_messages_and_truncated(sid)
    assert trunc is True
    assert len(msgs) == codex.CODEX_MAX_MESSAGES_PER_FILE
    assert msgs[-1]["content"] == f"turn-{codex.CODEX_MAX_MESSAGES_PER_FILE + 2}"


def test_imported_codex_session_compact_exposes_truncation(tmp_path, monkeypatch):
    """Blocker 3: the imported sidecar surfaces ``cli_transcript_truncated``.

    The real viewer serves the imported WebUI sidecar, so the truncation flag
    must round-trip through Session save/load/compact for the frontend banner.
    """
    import api.models as models
    import api.codex_sessions as codex

    home = _make_codex_home(tmp_path)
    rollout = (
        home / "sessions" / "2026" / "08" / "01"
        / f"rollout-2026-08-01T01-20-52-{THREAD_A}.jsonl"
    )
    rows = [
        {
            "timestamp": f"2026-08-01T00:00:{i % 60:02d}.000Z",
            "type": "response_item",
            "payload": {"type": "message", "role": "user", "content": f"turn-{i}"},
        }
        for i in range(codex.CODEX_MAX_MESSAGES_PER_FILE + 2)
    ]
    _write_rollout(rollout, rows)
    monkeypatch.setenv("HERMES_WEBUI_CODEX_HOME", str(home))
    monkeypatch.setenv("HERMES_WEBUI_TEST_STATE_DIR", str(tmp_path / "state"))
    codex.clear_codex_parse_cache()

    sid = f"codex_{THREAD_A}"
    msgs, trunc = models.get_cli_session_messages_and_truncated(sid)
    assert trunc is True

    s = models.import_cli_session(sid, "Codex", msgs, "codex")
    s.cli_transcript_truncated = trunc
    compact = s.compact()
    assert compact["cli_transcript_truncated"] is True

    # Round-trips through persistence (save then load).
    s.save(touch_updated_at=False)
    reloaded = models.Session.load(sid)
    assert reloaded is not None
    assert reloaded.cli_transcript_truncated is True
    assert reloaded.compact()["cli_transcript_truncated"] is True


def test_synthetic_detection_keeps_real_user_prompts_with_context_prefix(tmp_path):
    """Blocker 4A: a real prompt that merely starts with an injection token is kept.

    Previously ``_is_synthetic_user_text`` matched by prefix, so a user who
    pasted AGENTS.md or opened a message with ``<environment_context>`` had
    their real text dropped. Now only COMPLETE injected blobs are dropped.
    """
    import api.codex_sessions as codex

    # Complete injected blobs are still dropped.
    assert codex._is_synthetic_user_text("<environment_context>\n  <cwd>/x</cwd>\n</environment_context>") is True
    assert codex._is_synthetic_user_text("# AGENTS.md instructions\n<INSTRUCTIONS>y</INSTRUCTIONS>") is True

    # A real prompt that merely STARTS with the token (no matching close) is kept.
    assert codex._is_synthetic_user_text("<environment_context>\nwhat does this block do?") is False
    assert codex._is_synthetic_user_text("# AGENTS.md instructions are great, let's review mine") is False
    assert codex._is_synthetic_user_text("hi") is False

    # A user pasting the AGENTS.md header but adding prose after the block survives.
    assert codex._is_synthetic_user_text(
        "# AGENTS.md instructions\n<INSTRUCTIONS>y</INSTRUCTIONS>\n\nbut my real question is below"
    ) is False


def test_parse_timestamp_rejects_non_finite_values():
    """Blocker 4B: NaN/Infinity must never reach the JSON transcript payload."""
    import math
    import api.codex_sessions as codex

    assert codex._parse_timestamp(float("nan")) is None
    assert codex._parse_timestamp(float("inf")) is None
    assert codex._parse_timestamp(float("-inf")) is None
    assert codex._parse_timestamp("nan") is None
    assert codex._parse_timestamp("Infinity") is None
    # Valid values still parse.
    assert codex._parse_timestamp(0) == 0.0
    assert codex._parse_timestamp("1785518452") == 1785518452.0
    assert math.isfinite(codex._parse_timestamp("2026-08-01T00:00:00Z"))


def test_import_cli_route_surfaces_truncation_on_real_codex_path(
    monkeypatch, tmp_path
):
    """Blocker 3: the real production path surfaces ``cli_transcript_truncated``.

    The frontend opens a Codex session via POST /api/session/import_cli, which
    takes the read-only stub branch (Codex sessions are read_only). That
    response — not GET /api/codex/session/<id> — is what the viewer renders, so
    the truncation flag must travel through this handler for the frontend
    banner to appear. Exercises the actual ``_handle_session_import_cli`` path.
    """
    import api.routes as routes
    import api.codex_sessions as codex

    home = _make_codex_home(tmp_path)
    rollout = (
        home / "sessions" / "2026" / "08" / "01"
        / f"rollout-2026-08-01T01-20-52-{THREAD_A}.jsonl"
    )
    rows = [
        {
            "timestamp": f"2026-08-01T00:00:{i % 60:02d}.000Z",
            "type": "response_item",
            "payload": {"type": "message", "role": "user", "content": f"turn-{i}"},
        }
        for i in range(codex.CODEX_MAX_MESSAGES_PER_FILE + 2)
    ]
    _write_rollout(rollout, rows)
    monkeypatch.setenv("HERMES_WEBUI_CODEX_HOME", str(home))
    monkeypatch.setenv("HERMES_WEBUI_TEST_STATE_DIR", str(tmp_path / "state"))
    codex.clear_codex_parse_cache()

    sid = f"codex_{THREAD_A}"
    # The real metadata lookup + real (truncated) message read, unpatched, so
    # this is the genuine production data flow.
    meta = routes._lookup_cli_session_metadata(sid)
    assert meta and meta.get("read_only") is True

    monkeypatch.setattr(routes.Session, "load", classmethod(lambda _cls, _sid: None))
    monkeypatch.setattr(routes, "require", lambda body, *keys: None)
    monkeypatch.setattr(routes, "bad", lambda _h, msg, status=400: {"error": msg, "status": status})
    monkeypatch.setattr(routes, "j", lambda _h, payload, status=200, extra_headers=None: payload)

    response = routes._handle_session_import_cli(object(), {"session_id": sid})

    assert response.get("imported") is False
    session = response["session"]
    # The read-only stub carries the truncation flag for the frontend banner.
    assert session["cli_transcript_truncated"] is True
    assert len(session["messages"]) == codex.CODEX_MAX_MESSAGES_PER_FILE
    # Newest turn retained, matching the production ordering fix.
    assert session["messages"][-1]["content"] == f"turn-{codex.CODEX_MAX_MESSAGES_PER_FILE + 1}"
