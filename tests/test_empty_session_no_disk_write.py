"""
Regression tests for the "no disk write for empty sessions" follow-up to #1171.

Lifecycle contract:
  1. ``new_session()`` adds the session to the in-memory ``SESSIONS`` dict but
     does NOT write a JSON file to disk.
  2. The first ``s.save()`` happens when the session has real state to persist
     (a user message via ``/api/chat/start``, or a populated title/messages
     for btw / background agents).
  3. ``get_session(sid)`` is unchanged: it checks ``SESSIONS`` first, so an
     unsaved session is still findable by ID for the brief window between
     create and first message.
  4. ``all_sessions()`` already filters Untitled + 0-message sessions (#1171),
     so an unsaved in-memory session does not surface in the sidebar even
     though it lives in the SESSIONS dict.

Crash-safety: if the process exits between create and first message, the
session is lost. There were no messages to lose, so this is an explicit
trade-off documented in ``new_session``'s docstring.
"""
import json
import time

import pytest

import api.models as models
from api.models import (
    SESSIONS,
    Session,
    all_sessions,
    get_session,
    new_session,
)


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """Redirect SESSION_DIR and SESSION_INDEX_FILE to a fresh tmp dir."""
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    index_file = session_dir / "_index.json"
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", index_file)
    SESSIONS.clear()
    yield session_dir
    SESSIONS.clear()


# ── 1. new_session does not write to disk ───────────────────────────────────


def test_new_session_does_not_write_to_disk(_isolate):
    s = new_session()
    assert not s.path.exists(), (
        "new_session() must not eagerly persist an empty session — disk write "
        "is deferred until the first message is appended (#1171 follow-up)"
    )


def test_new_session_lives_in_memory(_isolate):
    s = new_session()
    assert s.session_id in SESSIONS
    assert SESSIONS[s.session_id] is s


def test_get_session_finds_unsaved_session_by_id(_isolate):
    """The brief window between create and first message must still allow
    /api/chat/start to look up the session by its returned session_id."""
    s = new_session()
    found = get_session(s.session_id)
    assert found is s, (
        "get_session must return the in-memory unsaved session — _handle_chat_start "
        "depends on this for the very first message in a fresh session."
    )


# ── 2. unsaved sessions never surface in the sidebar ─────────────────────────


def test_unsaved_empty_session_hidden_from_sidebar(_isolate):
    """all_sessions filters Untitled+0-message regardless of save state (#1171)."""
    s = new_session()
    ids = {row["session_id"] for row in all_sessions()}
    assert s.session_id not in ids, (
        "An unsaved empty Untitled session must not appear in /api/sessions"
    )


# ── 3. save() materialises the file when state is real ─────────────────────


def test_save_writes_to_disk_when_first_invoked(_isolate):
    """The first save() (typically from _handle_chat_start after appending a
    user message) creates the JSON file."""
    s = new_session()
    assert not s.path.exists()
    s.messages.append({"role": "user", "content": "hello"})
    s.save()
    assert s.path.exists(), "save() must create the file once it's called"
    content = json.loads(s.path.read_text(encoding="utf-8"))
    assert content["session_id"] == s.session_id
    assert content["messages"] and content["messages"][0]["role"] == "user"


def test_btw_background_pattern_still_persists(_isolate):
    """btw / background agents at api/routes.py call save() right after
    populating title/messages — that path must continue to write to disk
    even though new_session itself no longer saves."""
    s = new_session()
    s.title = "btw: question"
    s.messages = [{"role": "user", "content": "hi"}]
    s.save()  # mirrors api/routes.py:_handle_btw / _handle_background
    assert s.path.exists()
    on_disk = json.loads(s.path.read_text(encoding="utf-8"))
    assert on_disk["title"] == "btw: question"


# ── 4. crash-safety semantics: no orphan files accumulate on the new path ──


def test_repeated_new_session_creates_no_disk_files(_isolate, tmp_path):
    """Five news in a row produce zero disk files. Pre-fix this would have
    written five orphan JSON files to SESSION_DIR."""
    session_dir = tmp_path / "sessions"
    for _ in range(5):
        new_session()
    on_disk_jsons = [p for p in session_dir.glob("*.json") if not p.name.startswith("_")]
    assert on_disk_jsons == [], (
        f"new_session() produced disk files: {[p.name for p in on_disk_jsons]}"
    )
