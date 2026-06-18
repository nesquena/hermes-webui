from __future__ import annotations

import json
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")


def _claude_fixture_rows() -> list[dict]:
    return [
        {"summary": "Claude Code import QA"},
        {"timestamp": "2026-04-18T12:00:01Z", "message": {"role": "user", "content": [{"type": "text", "text": "Can Hermes show this Claude Code history read-only?"}]}},
        {"timestamp": "2026-04-18T12:00:02Z", "message": {"role": "assistant", "content": "Yes — it appears with a Claude Code source badge."}},
        "not a dict",
        {"not_json_message": True},
    ]


def test_default_claude_code_scan_is_disabled_inside_test_state(monkeypatch, tmp_path):
    """Test runs must not accidentally scan Michael's real ~/.claude/projects."""
    import api.models as models

    monkeypatch.delenv("HERMES_WEBUI_CLAUDE_PROJECTS_DIR", raising=False)
    monkeypatch.setenv("HERMES_WEBUI_TEST_STATE_DIR", str(tmp_path / "state"))

    assert models._default_claude_code_projects_dir() is None
    assert models.get_claude_code_sessions() == []


def test_get_claude_code_sessions_reads_fixture_jsonl_without_real_home(tmp_path):
    import api.models as models

    projects_dir = tmp_path / "claude" / "projects"
    fixture = projects_dir / "project-a" / "session.jsonl"
    _write_jsonl(fixture, _claude_fixture_rows())

    sessions = models.get_claude_code_sessions(projects_dir=projects_dir)

    assert len(sessions) == 1
    session = sessions[0]
    assert session["session_id"].startswith("claude_code_")
    assert session["title"] == "Claude Code import QA"
    assert session["model"] == "claude-code"
    assert session["message_count"] == 2
    assert session["source_tag"] == "claude_code"
    assert session["raw_source"] == "claude_code"
    assert session["session_source"] == "external_agent"
    assert session["source_label"] == "Claude Code"
    assert session["is_cli_session"] is True
    assert session["read_only"] is True

    messages = models.get_claude_code_session_messages(session["session_id"], projects_dir=projects_dir)
    assert messages == [
        {"role": "user", "content": "Can Hermes show this Claude Code history read-only?", "timestamp": 1776513601.0},
        {"role": "assistant", "content": "Yes — it appears with a Claude Code source badge.", "timestamp": 1776513602.0},
    ]


def test_claude_code_scan_skips_symlinks_and_oversized_files(tmp_path):
    import api.models as models

    projects_dir = tmp_path / "claude" / "projects"
    valid = projects_dir / "project-a" / "valid.jsonl"
    _write_jsonl(valid, [{"message": {"role": "user", "content": "valid import"}}])
    oversized = projects_dir / "project-a" / "oversized.jsonl"
    oversized.write_text("x" * 1024, encoding="utf-8")

    outside = tmp_path / "outside"
    outside.mkdir()
    _write_jsonl(outside / "leaked.jsonl", [{"message": {"role": "user", "content": "do not import"}}])
    symlink_project = projects_dir / "symlink-project"
    symlink_project.symlink_to(outside, target_is_directory=True)

    root_link = tmp_path / "root-link"
    root_link.symlink_to(projects_dir, target_is_directory=True)

    sessions = models.get_claude_code_sessions(projects_dir=projects_dir, max_file_bytes=512)

    assert [session["title"] for session in sessions] == ["valid import"]
    assert models.get_claude_code_sessions(projects_dir=root_link) == []


def test_get_cli_sessions_reuses_short_ttl_cache(monkeypatch, tmp_path):
    import api.models as models
    import api.profiles as profiles

    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir()
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: str(hermes_home))
    monkeypatch.setattr(profiles, "get_active_profile_name", lambda: "default")
    monkeypatch.setattr(models, "_CLI_SESSIONS_CACHE_TTL_SECONDS", 60.0, raising=False)
    models.clear_cli_sessions_cache()

    calls = 0

    def fake_claude_code_sessions():
        nonlocal calls
        calls += 1
        return [
            {
                "session_id": "claude_code_cached",
                "title": "Cached Claude Code",
                "updated_at": calls,
                "message_count": 1,
                "source_tag": "claude_code",
                "is_cli_session": True,
            }
        ]

    monkeypatch.setattr(models, "get_claude_code_sessions", fake_claude_code_sessions)

    first = models.get_cli_sessions()
    first[0]["title"] = "mutated by caller"
    second = models.get_cli_sessions()

    assert calls == 1
    assert second[0]["title"] == "Cached Claude Code"
    assert second[0]["updated_at"] == 1


def test_get_cli_sessions_cache_invalidates_when_sqlite_wal_changes(monkeypatch, tmp_path):
    import api.models as models
    import api.profiles as profiles

    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir()
    db_path = hermes_home / "state.db"
    db_path.write_text("initial", encoding="utf-8")
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: str(hermes_home))
    monkeypatch.setattr(profiles, "get_active_profile_name", lambda: "default")
    monkeypatch.setattr(models, "_CLI_SESSIONS_CACHE_TTL_SECONDS", 60.0, raising=False)
    monkeypatch.setattr(models, "get_claude_code_sessions", lambda: [])
    models.clear_cli_sessions_cache()

    calls = 0

    def fake_rows(_db_path, **_kwargs):
        nonlocal calls
        calls += 1
        return [
            {
                "id": "cli_cached_state_db",
                "title": "State DB Session",
                "model": "test-model",
                "source": "cli",
                "raw_source": "cli",
                "message_count": calls,
                "actual_message_count": calls,
                "actual_user_message_count": 1,
                "last_activity": float(calls),
                "started_at": 1.0,
            }
        ]

    monkeypatch.setattr(models, "read_importable_agent_session_rows", fake_rows)

    first = models.get_cli_sessions()
    Path(f"{db_path}-wal").write_text("new wal contents", encoding="utf-8")
    second = models.get_cli_sessions()

    # Two calls to get_cli_sessions() × 2 invocations each (first pass +
    # second cron-only pass) = 4 total calls to the mock.
    assert calls == 4
    # First pass of first call returned message_count=1 (calls was 1).
    assert first[0]["message_count"] == 1
    # First pass of second call returned message_count=3 (calls was 3;
    # the second pass incremented calls to 2 and 4 but cron-only filter
    # excluded the cli-source session from both second passes).
    assert second[0]["message_count"] == 3


def test_session_import_cli_returns_read_only_claude_code_payload(monkeypatch, tmp_path):
    import api.routes as routes

    sid = "claude_code_fixture"
    messages = [{"role": "user", "content": "history"}]
    meta = {
        "session_id": sid,
        "title": "Claude Code fixture",
        "model": "claude-code",
        "created_at": 10.0,
        "updated_at": 20.0,
        "source_tag": "claude_code",
        "raw_source": "claude_code",
        "session_source": "external_agent",
        "source_label": "Claude Code",
        "is_cli_session": True,
        "read_only": True,
    }

    monkeypatch.setattr(routes.Session, "load", classmethod(lambda _cls, _sid: None))
    monkeypatch.setattr(routes, "require", lambda body, *keys: None)
    monkeypatch.setattr(routes, "bad", lambda _handler, msg, status=400: {"ok": False, "error": msg, "status": status})
    monkeypatch.setattr(routes, "j", lambda _handler, payload, status=200, extra_headers=None: payload)
    monkeypatch.setattr(routes, "get_cli_session_messages", lambda _sid, profile=None: messages if _sid == sid else [])
    monkeypatch.setattr(routes, "get_cli_sessions", lambda source_filter=None, all_profiles=False: [meta])
    monkeypatch.setattr(routes, "get_last_workspace", lambda: tmp_path / "workspace")
    monkeypatch.setattr(routes, "import_cli_session", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("read-only import must not persist")))

    response = routes._handle_session_import_cli(object(), {"session_id": sid})

    assert response["imported"] is False
    session = response["session"]
    assert session["session_id"] == sid
    assert session["title"] == "Claude Code fixture"
    assert session["model"] == "claude-code"
    assert session["messages"] == messages
    assert session["read_only"] is True
    assert session["source_tag"] == "claude_code"
    assert session["raw_source"] == "claude_code"
    assert session["session_source"] == "external_agent"
    assert session["source_label"] == "Claude Code"
    assert session["is_cli_session"] is True


def test_session_import_cli_queues_generated_title_for_writable_default_cli_title(monkeypatch):
    import api.routes as routes

    sid = "cli_writable_default_title"
    messages = [{"role": "user", "content": "Need a better imported title"}]
    cli_meta = {
        "session_id": sid,
        "title": "CLI Session",
        "model": "claude-code",
        "created_at": 10.0,
        "updated_at": 20.0,
        "source_tag": "cli",
        "raw_source": "cli",
        "session_source": "external_agent",
        "source_label": "CLI",
        "is_cli_session": True,
        "read_only": False,
    }
    persisted = {}
    queued = []
    published = []

    class FakeImportedSession:
        def __init__(self):
            self.session_id = sid
            self.title = "CLI Session"
            self.messages = list(messages)
            self.profile = "default"
            self.model = "claude-code"
            self.read_only = False
            self.is_cli_session = True

        def save(self, touch_updated_at=False):
            persisted["saved"] = touch_updated_at

        def compact(self):
            return {"session_id": sid, "title": self.title}

    imported = FakeImportedSession()

    monkeypatch.setattr(routes.Session, "load", classmethod(lambda _cls, _sid: None))
    monkeypatch.setattr(routes, "require", lambda body, *keys: None)
    monkeypatch.setattr(routes, "bad", lambda _handler, msg, status=400: {"ok": False, "error": msg, "status": status})
    monkeypatch.setattr(routes, "j", lambda _handler, payload, status=200, extra_headers=None: payload)
    monkeypatch.setattr(
        routes,
        "get_cli_session_messages",
        lambda _sid, profile=None: messages if _sid == sid else [],
    )
    monkeypatch.setattr(
        routes,
        "get_cli_sessions",
        lambda source_filter=None, all_profiles=False: [cli_meta],
    )
    monkeypatch.setattr(routes, "import_cli_session", lambda *args, **kwargs: imported)
    monkeypatch.setattr(routes, "publish_session_list_changed", lambda reason, profile=None: published.append((reason, profile)))
    monkeypatch.setattr(routes, "_queue_generated_title_for_imported_session", lambda session, meta: queued.append((session, meta.copy())))

    response = routes._handle_session_import_cli(object(), {"session_id": sid})

    assert response["imported"] is True
    assert persisted["saved"] is False
    assert published == [("session_import_cli", "default")]
    assert queued == [(imported, {
        "title": "CLI Session",
        "source_tag": "cli",
        "raw_source": "cli",
        "session_source": "external_agent",
        "source_label": "CLI",
        "read_only": False,
    })]


def test_read_only_source_badge_ui_guards_are_present():
    sessions_js = (REPO_ROOT / "static" / "sessions.js").read_text(encoding="utf-8")
    messages_js = (REPO_ROOT / "static" / "messages.js").read_text(encoding="utf-8")
    ui_js = (REPO_ROOT / "static" / "ui.js").read_text(encoding="utf-8")
    panels_js = (REPO_ROOT / "static" / "panels.js").read_text(encoding="utf-8")
    style_css = (REPO_ROOT / "static" / "style.css").read_text(encoding="utf-8")
    routes_py = (REPO_ROOT / "api" / "routes.py").read_text(encoding="utf-8")

    assert "function _isReadOnlySession" in sessions_js
    assert "read-only-session" in sessions_js
    assert "if(!readOnly)" in sessions_js
    assert "Read-only imported sessions cannot be renamed" in sessions_js
    assert "Read-only imported sessions cannot be modified" in sessions_js
    assert "S.session.read_only||S.session.is_read_only" in messages_js
    assert "topbar-source-badge" in ui_js
    assert " · read-only" in ui_js
    assert "topbar-source-badge" in panels_js
    assert "S.session.read_only || S.session.is_read_only" in panels_js
    assert 'data-source-key="claude_code"' in style_css
    assert ".session-item.read-only-session:hover .session-source-chip" in style_css
    assert "Read-only imported sessions cannot be deleted" in routes_py
    assert "Read-only imported sessions cannot be archived" in routes_py


def test_messaging_source_badge_not_gated_on_is_cli_session():
    # Messaging sessions (Telegram, WeChat, Discord) populate source_label/source_tag/raw_source
    # but reach the chat pane with is_cli_session=false, so the topbar badge must not be gated on
    # is_cli_session or it never renders for them (#3338).
    ui_js = (REPO_ROOT / "static" / "ui.js").read_text(encoding="utf-8")
    panels_js = (REPO_ROOT / "static" / "panels.js").read_text(encoding="utf-8")

    assert "S.session.is_cli_session&&(S.session.source_label" not in ui_js
    assert "if (S.session.is_cli_session) sourceLabel" not in panels_js
    assert "S.session.source_label||S.session.source_tag||S.session.raw_source" in ui_js
    assert "S.session.source_label || S.session.source_tag || S.session.raw_source" in panels_js
    # The native WebUI self-source must be suppressed so recovered sidecars
    # (source_label 'WebUI' from api/session_recovery.py) don't badge the chat pane (#3338).
    assert "/^webui$/i.test(sourceLabel)" in ui_js
    assert "/^webui$/i.test(sourceLabel)" in panels_js


def test_messaging_source_badge_in_sidebar_not_gated_on_is_cli_session():
    sessions_js = (REPO_ROOT / "static" / "sessions.js").read_text(encoding="utf-8")
    style_css = (REPO_ROOT / "static" / "style.css").read_text(encoding="utf-8")

    assert "function _isMessagingSession" in sessions_js
    assert sessions_js.count("if(s.is_cli_session||_isMessagingSession(s)){") == 2
    assert "if(s.is_cli_session&&sourceLabel) metaBits.push(sourceLabel);" not in sessions_js
    assert (
        "if(sourceLabel&&(s.is_cli_session||_isMessagingSession(s))) metaBits.push(sourceLabel);"
        in sessions_js
    )
    assert "session-source-chip" in sessions_js
    assert ".session-source-chip" in style_css
    assert '.session-source-chip[data-source-key="telegram"]' in style_css
    assert '.session-source-chip[data-source-key="discord"]' in style_css
    assert '.session-item.cli-session[data-source-key="telegram"]' in style_css


def test_compression_queue_discoverability_ux():
    ui_js = (REPO_ROOT / "static" / "ui.js").read_text(encoding="utf-8")
    i18n_js = (REPO_ROOT / "static" / "i18n.js").read_text(encoding="utf-8")

    # Old misleading tooltip key must be gone from the compression branch
    assert "composer_disabled_compression','Waiting for compression to finish'" not in ui_js

    # New will-queue key must be present in ui.js (tooltip and placeholder)
    assert "composer_compression_will_queue" in ui_js

    # getComposerPrimaryAction must return 'queue' for compressionRunning+no-content
    assert "if(compressionRunning) return 'queue';" in ui_js

    # clearCompressionUi must restore the placeholder
    assert "_compressionPlaceholderSaved" in ui_js

    # New i18n key must appear in all 11 locales (count occurrences)
    assert i18n_js.count("composer_compression_will_queue") >= 11
