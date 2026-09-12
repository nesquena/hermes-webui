"""Stage-326 hardening tests for #1956 composer-draft input validation.

Opus advisor flagged that POST /api/session/draft accepted text/files of
arbitrary size and type. A misbehaving or malicious client could persist
multi-MB strings into the session JSON on every keystroke via the 400ms
debounced auto-save. The hardening:

- text: must be str; clamped to 50 KB
- files: must be list; clamped to 50 entries
"""
import json
import contextlib
import io
import os
import sys
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

# These tests directly call the handler logic by importing the routes module
# and exercising the validation through a minimal mock handler. We don't need
# a full HTTP server.


@pytest.fixture
def isolated_state_dir(tmp_path, monkeypatch):
    """Point STATE_DIR at a tmpdir so saved sessions don't pollute reality."""
    monkeypatch.setenv("HERMES_WEBUI_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_BASE_HOME", str(tmp_path))
    yield tmp_path


def test_draft_text_clamped_to_50kb(isolated_state_dir):
    """Posting a >50KB text field should be silently truncated to 50_000 chars."""
    # Read the routes.py source and assert the clamp logic is present.
    src = Path(__file__).parents[1].joinpath("api", "routes.py").read_text(encoding="utf-8")

    # The clamp constant must exist.
    assert "_MAX_DRAFT_TEXT = 50_000" in src or "_MAX_DRAFT_TEXT=50_000" in src.replace(" ", ""), (
        "routes.py must define _MAX_DRAFT_TEXT clamp for the composer-draft POST handler"
    )

    # And the truncation must be applied.
    assert "text = text[:_MAX_DRAFT_TEXT]" in src, (
        "routes.py must truncate over-large draft text to _MAX_DRAFT_TEXT"
    )


def test_draft_files_clamped_to_50_entries():
    """Posting a >50-entry files list should be silently truncated."""
    src = Path(__file__).parents[1].joinpath("api", "routes.py").read_text(encoding="utf-8")
    assert "_MAX_DRAFT_FILES = 50" in src, (
        "routes.py must define _MAX_DRAFT_FILES clamp"
    )
    assert "files = files[:_MAX_DRAFT_FILES]" in src, (
        "routes.py must truncate over-large draft files list"
    )


def test_draft_text_type_coerced_to_string():
    """Non-string text must be coerced to empty string, not stored as-is."""
    src = Path(__file__).parents[1].joinpath("api", "routes.py").read_text(encoding="utf-8")
    # The type-coerce pattern must be present.
    assert 'if text is not None and not isinstance(text, str):' in src, (
        "routes.py must coerce non-string text to empty string before persist"
    )


def test_draft_files_type_coerced_to_list():
    """Non-list files must be coerced to empty list."""
    src = Path(__file__).parents[1].joinpath("api", "routes.py").read_text(encoding="utf-8")
    assert 'if files is not None and not isinstance(files, list):' in src, (
        "routes.py must coerce non-list files to empty list before persist"
    )


def test_draft_validation_appears_before_persist():
    """The validation must run BEFORE the lock acquire / save, not after."""
    src = Path(__file__).parents[1].joinpath("api", "routes.py").read_text(encoding="utf-8")
    # Anchor on the unique POST-validation comment marker.
    marker_idx = src.find("Stage-326 hardening (per Opus advisor)")
    persist_idx = src.find("s.composer_draft = next_draft")
    assert marker_idx != -1 and persist_idx != -1, (
        "could not locate validation marker or persist site"
    )
    assert marker_idx < persist_idx, (
        "validation block must run before composer_draft persist"
    )


def test_draft_save_does_not_touch_session_updated_at():
    """Autosaving the composer must not look like conversation activity.

    If POST /api/session/draft bumps updated_at, the frontend's active-session
    external refresh poll treats every keystroke autosave as a remote session
    update and force-reloads the current chat a few seconds later.
    """
    src = Path(__file__).parents[1].joinpath("api", "routes.py").read_text(encoding="utf-8")
    persist_idx = src.find("s.composer_draft = next_draft")
    assert persist_idx != -1, "could not locate composer draft persist site"
    save_idx = src.find("s.save(touch_updated_at=False, skip_index=True)", persist_idx)
    assert save_idx != -1, "composer draft save must preserve session updated_at and skip index churn"


def test_draft_save_skips_unchanged_payload_before_persist():
    """Duplicate debounced draft POSTs should not rewrite the full session JSON."""
    src = Path(__file__).parents[1].joinpath("api", "routes.py").read_text(encoding="utf-8")
    draft_idx = src.find('current_draft = dict(getattr(s, "composer_draft", {}) or {})')
    unchanged_idx = src.find("if next_draft == current_draft", draft_idx)
    save_idx = src.find("s.save(touch_updated_at=False, skip_index=True)", draft_idx)

    assert draft_idx != -1, "draft route should snapshot current composer_draft"
    assert unchanged_idx != -1, "draft route should no-op unchanged normalized payloads"
    assert save_idx != -1, "draft route should still save changed drafts"
    assert unchanged_idx < save_idx, "unchanged guard must run before full session save"
    assert 'payload["unchanged"] = True' in src


class _DraftRouteHandler:
    command = "POST"

    def __init__(self):
        self.wfile = io.BytesIO()
        self.headers = {}
        self.status = None

    def send_response(self, status):
        self.status = status

    def send_header(self, *_args):
        pass

    def end_headers(self):
        pass


def _post_draft(monkeypatch, session, body):
    from api import routes

    handler = _DraftRouteHandler()
    monkeypatch.setattr(routes, "_check_csrf", lambda _handler: True)
    monkeypatch.setattr(routes, "_handle_extension_sidecar_proxy", lambda *args, **kwargs: False)
    monkeypatch.setattr(routes, "_guard_request_session_visibility", lambda *args, **kwargs: True)
    monkeypatch.setattr(routes, "read_body", lambda _handler: body)
    monkeypatch.setattr(routes, "_session_is_subagent_view_only", lambda _sid: False)
    monkeypatch.setattr(routes, "get_session", lambda _sid: session)
    monkeypatch.setattr(routes, "_get_session_agent_lock", lambda _sid: contextlib.nullcontext())
    routes.handle_post(handler, SimpleNamespace(path="/api/session/draft", query=""))
    payload = json.loads(handler.wfile.getvalue().decode("utf-8"))
    return payload


def test_compare_and_clear_is_atomic_and_preserves_newer_draft(monkeypatch):
    """Interrupt settlement clears only the exact draft it submitted."""
    from types import SimpleNamespace as _Namespace

    matching = _Namespace(
        composer_draft={"text": "captured \n", "files": [{"name": "a.pdf"}]},
        save=lambda **_kwargs: None,
    )
    body = {
        "session_id": "sid-draft",
        "text": "captured \n",
        "files": [{"name": "a.pdf"}],
        "compare_and_clear": True,
    }
    payload = _post_draft(monkeypatch, matching, body)
    assert payload["compare_cleared"] is True
    assert matching.composer_draft == {"text": "", "files": []}

    newer = _Namespace(
        composer_draft={"text": "newer owner draft", "files": []},
        save=lambda **_kwargs: None,
    )
    payload = _post_draft(monkeypatch, newer, body)
    assert payload["compare_cleared"] is False
    assert newer.composer_draft == {"text": "newer owner draft", "files": []}


def test_expected_stream_guard_rejects_stale_draft_write(monkeypatch):
    saves = []
    session = SimpleNamespace(
        active_stream_id="successor",
        composer_draft={"text": "successor draft", "files": []},
        save=lambda **_kwargs: saves.append(True),
    )
    payload = _post_draft(monkeypatch, session, {
        "session_id": "sid-draft",
        "text": "old stream draft",
        "files": [],
        "expected_stream_id": "old",
    })

    assert payload == {"error": "Session stream changed"}
    assert saves == []
    assert session.composer_draft == {"text": "successor draft", "files": []}


def test_expected_stream_guard_allows_current_draft_write(monkeypatch):
    saves = []
    session = SimpleNamespace(
        active_stream_id="old",
        composer_draft={"text": "old", "files": []},
        save=lambda **_kwargs: saves.append(True),
    )
    payload = _post_draft(monkeypatch, session, {
        "session_id": "sid-draft",
        "text": "current stream draft",
        "files": [],
        "expected_stream_id": "old",
    })

    assert payload["ok"] is True
    assert saves == [True]
    assert session.composer_draft == {"text": "current stream draft", "files": []}


def test_compare_clear_allows_idle_after_expected_stream_cancel(monkeypatch):
    saves = []
    session = SimpleNamespace(
        active_stream_id=None,
        composer_draft={"text": "old", "files": [{"name": "old.pdf"}]},
        save=lambda **_kwargs: saves.append(True),
    )
    payload = _post_draft(monkeypatch, session, {
        "session_id": "sid-draft",
        "text": "old",
        "files": [{"name": "old.pdf"}],
        "compare_and_clear": True,
        "expected_stream_id": "old",
    })

    assert payload["compare_cleared"] is True
    assert saves == [True]
    assert session.composer_draft == {"text": "", "files": []}


def test_compare_clear_rejects_successor_even_when_payload_matches(monkeypatch):
    saves = []
    session = SimpleNamespace(
        active_stream_id="successor",
        composer_draft={"text": "old", "files": [{"name": "old.pdf"}]},
        save=lambda **_kwargs: saves.append(True),
    )
    payload = _post_draft(monkeypatch, session, {
        "session_id": "sid-draft",
        "text": "old",
        "files": [{"name": "old.pdf"}],
        "compare_and_clear": True,
        "expected_stream_id": "old",
    })

    assert payload == {"error": "Session stream changed"}
    assert saves == []
    assert session.composer_draft == {"text": "old", "files": [{"name": "old.pdf"}]}
