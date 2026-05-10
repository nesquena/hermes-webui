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
    monkeypatch.setattr(routes, "get_cli_session_messages", lambda _sid: messages if _sid == sid else [])
    monkeypatch.setattr(routes, "get_cli_sessions", lambda: [meta])
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
    assert ".session-item.cli-session.read-only-session:hover::after" in style_css
    assert "Read-only imported sessions cannot be deleted" in routes_py
    assert "Read-only imported sessions cannot be archived" in routes_py
