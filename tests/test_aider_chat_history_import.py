from __future__ import annotations

import datetime
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]

# Verbatim shape emitted by Aider's own writer:
#   io.py:336  append_chat_history("\n# aider chat started at {ts}\n\n")
#   io.py:775  user_input() -> "\n#### <line1>  \n#### <line2>  \n"   (EVERY line prefixed)
#   io.py:793  ai_output()  -> "\n<content>\n\n"
#   io.py:1118 blockquote=True -> "> " prefix (ask/confirm prompts)
AIDER_FIXTURE = (
    "\n# aider chat started at 2026-04-18 12:00:00\n\n"
    "\n#### Can Hermes show Aider history read-only?  \n"
    "\nYes — it appears with an Aider source badge.\n\n"
    "> y  \n"
    "\n#### Follow-up question  \n#### second line of user input  \n"
    "\nSecond assistant paragraph.\n\n"
)
AIDER_STARTED_AT = datetime.datetime(2026, 4, 18, 12, 0, 0).timestamp()
AIDER_ROLES = ["user", "assistant", "tool", "user", "assistant"]


def _write_history(path: Path, text: str = AIDER_FIXTURE) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_default_aider_scan_is_disabled_inside_test_state(monkeypatch, tmp_path):
    """Test runs must not accidentally scan a developer's real aider history."""
    import api.models as models

    monkeypatch.delenv("HERMES_WEBUI_AIDER_SCAN_DIR", raising=False)
    monkeypatch.setenv("HERMES_WEBUI_TEST_STATE_DIR", str(tmp_path / "state"))

    assert models._default_aider_scan_dir() is None
    assert models.get_aider_sessions() == []


def test_default_aider_scan_dir_uses_env_override(monkeypatch, tmp_path):
    import api.models as models

    monkeypatch.setenv("HERMES_WEBUI_AIDER_SCAN_DIR", str(tmp_path / "anywhere"))

    assert models._default_aider_scan_dir() == tmp_path / "anywhere"


def test_get_aider_sessions_reads_fixture_without_touching_real_home(tmp_path):
    import api.models as models

    scan_dir = tmp_path / "repo"
    _write_history(scan_dir / ".aider.chat.history.md")

    sessions = models.get_aider_sessions(scan_dir=scan_dir)

    assert len(sessions) == 1
    session = sessions[0]
    assert session["session_id"].startswith("aider_")
    # No stored title in aider's format: infer from the first user turn.
    assert session["title"] == "Can Hermes show Aider history read-only?"
    assert session["model"] == "aider"
    assert session["source_tag"] == "aider"
    assert session["raw_source"] == "aider"
    assert session["session_source"] == "external_agent"
    assert session["source_label"] == "Aider"
    assert session["is_cli_session"] is True
    assert session["read_only"] is True
    # session_meta-free format: created_at comes from the "# aider chat started
    # at" header, which is the only real timestamp the file carries.
    assert session["created_at"] == pytest.approx(AIDER_STARTED_AT, abs=1)


def test_aider_parser_matches_role_split_of_aider_split_chat_history_markdown(tmp_path):
    import api.models as models

    scan_dir = tmp_path / "repo"
    _write_history(scan_dir / ".aider.chat.history.md")

    sessions = models.get_aider_sessions(scan_dir=scan_dir)
    messages = models.get_aider_session_messages(
        sessions[0]["session_id"], scan_dir=scan_dir
    )

    # Roles follow aider/utils.py:148 split_chat_history_markdown():
    # "#### " -> user, "> " -> tool (ask/confirm blockquote), bare -> assistant.
    # Aider's own reader drops tool entries by default; the WebUI keeps them so
    # the transcript shows what the user actually answered, and the read-only
    # transcript already renders tool cards for state.db CLI sessions.
    assert [m["role"] for m in messages] == AIDER_ROLES
    assert messages[0]["content"] == "Can Hermes show Aider history read-only?"
    assert messages[1]["content"] == "Yes — it appears with an Aider source badge."
    assert messages[2]["content"] == "y"
    # Multi-line user input: every line carries its own "#### " prefix on disk,
    # so the real turns are stitched back into one message.
    assert messages[3]["content"] == "Follow-up question\nsecond line of user input"
    assert messages[4]["content"] == "Second assistant paragraph."


def test_aider_scan_skips_symlinks_oversized_files_and_missing_root(tmp_path):
    import api.models as models

    scan_dir = tmp_path / "repo"
    _write_history(scan_dir / ".aider.chat.history.md", "\n#### real turn\n")
    _write_history(scan_dir / "sub" / ".aider.chat.history.md", "\n#### other repo turn\n")

    outside = tmp_path / "outside"
    _write_history(outside / ".aider.chat.history.md", "\n#### leaked turn\n")
    (scan_dir / "linked").symlink_to(outside, target_is_directory=True)

    # Oversized transcripts are skipped, not parsed.
    big = scan_dir / "big"
    big.mkdir()
    (big / ".aider.chat.history.md").write_text("x" * 4096, encoding="utf-8")

    titles = sorted(
        s["title"]
        for s in models.get_aider_sessions(scan_dir=scan_dir, max_file_bytes=512)
    )
    assert titles == ["other repo turn", "real turn"]
    # A missing root yields nothing rather than raising.
    assert models.get_aider_sessions(scan_dir=tmp_path / "missing") == []

    # A symlinked scan root is refused outright, exactly like the Claude Code bridge.
    root_link = tmp_path / "root-link"
    root_link.symlink_to(scan_dir, target_is_directory=True)
    assert models.get_aider_sessions(scan_dir=root_link) == []


def test_aider_scan_respects_max_files_bound(tmp_path):
    import api.models as models

    scan_dir = tmp_path / "repo"
    for i in range(4):
        _write_history(scan_dir / f"d{i}" / ".aider.chat.history.md", f"\n#### turn {i}\n")

    assert len(models.get_aider_sessions(scan_dir=scan_dir, max_files=2)) == 2


def test_aider_scan_does_not_descend_into_git_or_node_modules(tmp_path):
    import api.models as models

    scan_dir = tmp_path / "repo"
    _write_history(scan_dir / ".aider.chat.history.md", "\n#### real turn\n")
    _write_history(scan_dir / ".git" / ".aider.chat.history.md", "\n#### git junk\n")
    _write_history(
        scan_dir / "node_modules" / ".aider.chat.history.md", "\n#### vendored junk\n"
    )

    titles = [s["title"] for s in models.get_aider_sessions(scan_dir=scan_dir)]
    assert titles == ["real turn"]


def test_aider_session_messages_rejects_foreign_and_unknown_ids(tmp_path):
    import api.models as models

    scan_dir = tmp_path / "repo"
    _write_history(scan_dir / ".aider.chat.history.md")

    assert models.get_aider_session_messages("claude_code_abc", scan_dir=scan_dir) == []
    assert models.get_aider_session_messages("aider_deadbeef", scan_dir=scan_dir) == []
    assert models.get_aider_session_messages("", scan_dir=scan_dir) == []


def test_aider_parse_cache_invalidates_on_content_change(tmp_path):
    import api.models as models

    scan_dir = tmp_path / "repo"
    fixture = scan_dir / ".aider.chat.history.md"
    _write_history(fixture, "\n#### first turn\n")
    models.clear_aider_parse_cache()

    before = models.get_aider_sessions(scan_dir=scan_dir)[0]["message_count"]

    _write_history(fixture, "\n#### first turn\n\n#### second turn\n\n#### third turn\n")
    models.clear_aider_parse_cache()
    after = models.get_aider_sessions(scan_dir=scan_dir)[0]["message_count"]

    assert before == 1
    assert after == 3


def test_aider_rows_are_profile_agnostic_for_detail_load(tmp_path):
    import api.models as models
    import api.routes as routes

    scan_dir = tmp_path / "repo"
    _write_history(scan_dir / ".aider.chat.history.md")
    row = models.get_aider_sessions(scan_dir=scan_dir)[0]

    # Aider transcripts live in a git root, outside the Hermes profile tree.
    assert row["profile"] is None
    assert routes._is_profile_agnostic_foreign_session(row) is True


def test_aider_source_filter_normalization_and_cli_merge(monkeypatch, tmp_path):
    import api.models as models

    assert models._normalize_cli_session_source_filter("aider") == models.AIDER_SOURCE

    scan_dir = tmp_path / "repo"
    _write_history(scan_dir / ".aider.chat.history.md")
    # The production loader calls get_aider_sessions() with the default scan
    # root (there is no global aider store), so point that root at the fixture.
    monkeypatch.setenv("HERMES_WEBUI_AIDER_SCAN_DIR", str(scan_dir))

    merged = models._load_cli_sessions_uncached(
        tmp_path / "hermes",
        tmp_path / "hermes" / "state.db",
        "default",
        source_filter=models.AIDER_SOURCE,
        include_aider=True,
    )
    assert [s["source_tag"] for s in merged] == ["aider"]

    # An explicit different source filter must not leak Aider rows in.
    assert models._load_cli_sessions_uncached(
        tmp_path / "hermes",
        tmp_path / "hermes" / "state.db",
        "default",
        source_filter="telegram",
        include_aider=True,
    ) == []

    # include_aider=False must suppress them even with no source filter.
    assert models._load_cli_sessions_uncached(
        tmp_path / "hermes",
        tmp_path / "hermes" / "state.db",
        "default",
        source_filter=None,
        include_aider=False,
    ) == []


def test_get_cli_session_messages_dispatches_aider_prefix(monkeypatch, tmp_path):
    import api.models as models

    scan_dir = tmp_path / "repo"
    _write_history(scan_dir / ".aider.chat.history.md")
    monkeypatch.setenv("HERMES_WEBUI_AIDER_SCAN_DIR", str(scan_dir))
    sid = models.get_aider_sessions()[0]["session_id"]

    messages = models.get_cli_session_messages(sid)
    assert [m["role"] for m in messages] == AIDER_ROLES


def test_aider_visibility_toggle_ui_contract_is_present():
    index_html = (REPO_ROOT / "static" / "index.html").read_text(encoding="utf-8")
    style_css = (REPO_ROOT / "static" / "style.css").read_text(encoding="utf-8")
    panels_js = (REPO_ROOT / "static" / "panels.js").read_text(encoding="utf-8")
    i18n_js = (REPO_ROOT / "static" / "i18n.js").read_text(encoding="utf-8")

    assert 'data-source-key="aider"' in style_css
    assert 'id="settingsShowAiderSessions"' in index_html
    assert 'data-i18n="settings_label_aider_sessions"' in index_html
    assert "settings.show_aider_sessions" in panels_js
    assert "body.show_aider_sessions=showAiderSessions" in panels_js
    # The label and description must exist in every locale bundle (15 bundles
    # ship today, same as settings_label_claude_code_sessions).
    assert i18n_js.count("settings_label_claude_code_sessions") == 15
    assert i18n_js.count("settings_desc_claude_code_sessions") == 15
    assert i18n_js.count("settings_label_aider_sessions") == 15
    assert i18n_js.count("settings_desc_aider_sessions") == 15


def test_aider_setting_is_registered_in_settings_defaults():
    config_py = (REPO_ROOT / "api" / "config.py").read_text(encoding="utf-8")
    routes_py = (REPO_ROOT / "api" / "routes.py").read_text(encoding="utf-8")

    assert '"show_aider_sessions": True' in config_py
    # Grandfather list + settings-write cache invalidation must know the key.
    assert config_py.count('"show_aider_sessions"') >= 2
    assert '"show_aider_sessions"' in routes_py