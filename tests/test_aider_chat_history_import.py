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
# Each blockquoted ask/confirm prompt becomes an assistant row carrying the
# synthetic `tool_calls` metadata plus the matching `tool` result row, because
# _messageIsRenderable() hides bare role=tool rows (see the tool-card contract
# test below) -- so a confirmation would otherwise vanish from the transcript.
AIDER_ROLES = [
    "user",
    "assistant",
    "assistant",
    "tool",
    "user",
    "assistant",
]
# Index of the `tool` result row for the confirmation.
AIDER_TOOL_INDEX = 3


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
    assert messages[AIDER_TOOL_INDEX]["content"] == "y"
    # Multi-line user input: every line carries its own "#### " prefix on disk,
    # so the real turns are stitched back into one message.
    assert messages[4]["content"] == "Follow-up question\nsecond line of user input"
    assert messages[5]["content"] == "Second assistant paragraph."


def test_aider_confirmation_entries_carry_matching_tool_call_metadata(tmp_path):
    """A blockquoted confirm prompt must survive the renderable filter.

    _messageIsRenderable() (static/ui.js) returns false for every role=tool
    row, and the frontend builds tool cards from the CALL side: an assistant
    row carrying tool_calls, joined to its result by tool_call_id. A bare tool
    row therefore renders nothing, so the confirmation disappeared from the
    transcript. Assert the pairing the frontend actually joins on.
    """
    import api.models as models

    scan_dir = tmp_path / "repo"
    _write_history(scan_dir / ".aider.chat.history.md")

    sessions = models.get_aider_sessions(scan_dir=scan_dir)
    messages = models.get_aider_session_messages(
        sessions[0]["session_id"], scan_dir=scan_dir
    )

    call = messages[AIDER_TOOL_INDEX - 1]
    result = messages[AIDER_TOOL_INDEX]
    assert call["role"] == "assistant"
    assert call["content"] == ""
    assert len(call["tool_calls"]) == 1
    declaration = call["tool_calls"][0]
    assert declaration["type"] == "function"
    assert declaration["function"]["name"] == models.AIDER_CONFIRM_TOOL
    # The result joins back to the declaration by tool_call_id -- the key the
    # renderer indexes (resultsByTid in static/ui.js).
    assert result["role"] == "tool"
    assert result["tool_call_id"] == declaration["id"]
    assert result["tool_name"] == declaration["function"]["name"]
    assert result["name"] == declaration["function"]["name"]


def test_aider_blank_history_parses_promptly(tmp_path):
    """A blank/degenerate history must return promptly, not stall the sidebar.

    _join_aider_lines() previously trimmed the padding-only leading lines with
    list.pop(0) -- O(n) per line, so a history that is nothing but aider's
    line-break padding was O(n^2) (64k lines took 0.53s and scaled ~4x per
    doubling). Bound the cost so the scan cannot stall the sidebar.
    """
    import time

    import api.models as models

    blank = "".join("  \n" for _ in range(200_000))
    scan_dir = tmp_path / "repo"
    _write_history(scan_dir / ".aider.chat.history.md", blank)

    started = time.perf_counter()
    sessions = models.get_aider_sessions(scan_dir=scan_dir)
    elapsed = time.perf_counter() - started

    # A history with no real turn yields no row at all...
    assert sessions == []
    # ...and it returns promptly. The old quadratic trim needed many seconds
    # here; 5s leaves generous headroom on slow CI while still failing loudly
    # if the O(n^2) pop(0) trim ever comes back.
    assert elapsed < 5.0, f"blank history parse took {elapsed:.2f}s"


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
    """The stat-keyed cache must self-invalidate; no explicit cache clear.

    This test used to call clear_aider_parse_cache() immediately after rewriting
    the file, which forced the second read cold and never exercised the
    automatic-invalidation path at all. Drop the clear so the second read is
    genuinely warm-then-invalidated, and prove the warm read is served from the
    cache (not just re-parsed to the same answer).
    """
    import api.models as models

    scan_dir = tmp_path / "repo"
    fixture = scan_dir / ".aider.chat.history.md"
    _write_history(fixture, "\n#### first turn\n")
    models.clear_aider_parse_cache()

    before = models.get_aider_sessions(scan_dir=scan_dir)[0]["message_count"]

    # Warm read with the file UNCHANGED must be served from the cache: count
    # the real parses to prove the memo is doing something.
    parse_calls = []
    real_parse = models._parse_aider_chat_history

    def counting_parse(path, **kwargs):
        parse_calls.append(path)
        return real_parse(path, **kwargs)

    models._parse_aider_chat_history = counting_parse
    try:
        cached = models.get_aider_sessions(scan_dir=scan_dir)[0]["message_count"]
    finally:
        models._parse_aider_chat_history = real_parse

    assert cached == before
    assert parse_calls == [], "unchanged file should be served from the parse cache"

    # Now change the content and read again WITHOUT clearing the cache. The
    # (mtime_ns, size, ctime_ns) key must miss so the new turn count is picked
    # up automatically.
    _write_history(fixture, "\n#### first turn\n\n#### second turn\n\n#### third turn\n")
    after = models.get_aider_sessions(scan_dir=scan_dir)[0]["message_count"]

    assert before == 1
    assert after == 3


def test_aider_scan_bounds_wide_directory_entries(tmp_path, monkeypatch):
    """A very wide directory must not escape the per-directory scan bound.

    sorted(current.iterdir()) read and sorted EVERY entry of a directory before
    the scan's file limit was consulted, so one directory holding a huge number
    of vendored files could do unbounded work on the sidebar path.
    """
    import api.models as models

    scan_dir = tmp_path / "repo"
    scan_dir.mkdir(parents=True)
    limit = 5
    total_entries = 41
    _write_history(scan_dir / ".aider.chat.history.md", "\n#### wide dir turn\n")
    for i in range(total_entries - 1):
        (scan_dir / f"f{i:03d}.txt").write_text("x", encoding="utf-8")

    consumed = []

    class _CountingScandir:
        """Wraps a real ScandirIterator and records every entry handed out."""

        def __init__(self, iterator):
            self._iterator = iterator

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self._iterator.close()
            return False

        def __iter__(self):
            for entry in self._iterator:
                consumed.append(entry.name)
                yield entry

    real_scandir = models.os.scandir
    real_limit = models.AIDER_MAX_DIR_ENTRIES

    def counting_scandir(path):
        return _CountingScandir(real_scandir(path))

    monkeypatch.setattr(models, "AIDER_MAX_DIR_ENTRIES", limit)
    monkeypatch.setattr(models.os, "scandir", counting_scandir)

    models.get_aider_sessions(scan_dir=scan_dir)

    # The bound is enforced while reading, not by sorting everything and
    # discarding it afterwards: the walk stopped at the cap instead of
    # enumerating all 41 entries as sorted(iterdir()) did.
    assert len(consumed) <= limit + 1, f"read {len(consumed)} entries, bound was {limit}"
    assert len(consumed) < total_entries

    # A directory narrower than the bound must still be read in full, so the
    # real history is found -- bounding must not hide ordinary repos.
    monkeypatch.setattr(models, "AIDER_MAX_DIR_ENTRIES", real_limit)
    titles = [s["title"] for s in models.get_aider_sessions(scan_dir=scan_dir)]
    assert titles == ["wide dir turn"]


def test_aider_detail_load_resolves_same_workspace_as_list(monkeypatch, tmp_path):
    """List and detail load must resolve the same profile's workspace.

    The list build resolves get_last_workspace(profile=_cli_profile) while the
    detail load used a bare get_last_workspace(); with multiple profiles those
    return different git roots, so a listed conversation would not open. Both
    sides must go through the one profile-scoped resolver.
    """
    import api.models as models

    repo_a = tmp_path / "repo-a"
    repo_b = tmp_path / "repo-b"
    _write_history(repo_a / ".aider.chat.history.md", "\n#### profile a turn\n")
    _write_history(repo_b / ".aider.chat.history.md", "\n#### profile b turn\n")
    (repo_a / ".git").mkdir()
    (repo_b / ".git").mkdir()

    monkeypatch.delenv("HERMES_WEBUI_AIDER_SCAN_DIR", raising=False)
    monkeypatch.delenv("HERMES_WEBUI_TEST_STATE_DIR", raising=False)

    workspaces = {"default": str(repo_a), "work": str(repo_b)}
    seen_profiles = []

    def fake_get_last_workspace(profile=None):
        seen_profiles.append(profile)
        return workspaces.get(str(profile), workspaces["default"])

    monkeypatch.setattr(models, "get_last_workspace", fake_get_last_workspace)
    monkeypatch.setattr(
        "api.profiles.get_active_profile_name", lambda: "default", raising=False
    )

    # List for the named profile, then open that conversation with the same
    # profile: the conversation must resolve and load.
    listed = models.get_aider_sessions(profile="work")
    assert [s["title"] for s in listed] == ["profile b turn"]
    sid = listed[0]["session_id"]

    # The profile was actually threaded into the resolve.
    assert "work" in seen_profiles

    detail = models.get_cli_session_messages(sid, profile="work")
    assert [m["content"] for m in detail] == ["profile b turn"]

    # A detail load for a DIFFERENT profile resolves that profile's own root,
    # so it must not silently return another profile's conversation.
    assert models.get_cli_session_messages(sid, profile="default") == []


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