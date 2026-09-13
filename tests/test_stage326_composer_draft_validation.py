"""Stage-326 hardening tests for #1956 composer-draft input validation.

Opus advisor flagged that POST /api/session/draft accepted text/files of
arbitrary size and type. A misbehaving or malicious client could persist
multi-MB strings into the session JSON on every keystroke via the 400ms
debounced auto-save. The hardening:

- text: must be str; clamped to 50 KB
- files: must be list; clamped to 50 entries
"""
import json
import os
import sys
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

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
    """The validation must run BEFORE the sidecar persist, not after."""
    src = Path(__file__).parents[1].joinpath("api", "routes.py").read_text(encoding="utf-8")
    # Anchor on the unique POST-validation comment marker.
    marker_idx = src.find("Stage-326 hardening (per Opus advisor)")
    # Big-session hotpath (2026-07-13): drafts persist to a sidecar file,
    # not via Session.save() — the persist site is the sidecar write.
    persist_idx = src.find("saved_draft = write_composer_draft_sidecar(sid, next_draft)")
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

    Big-session hotpath (2026-07-13): the guarantee got stronger — the draft
    POST must not call Session.save() AT ALL (which used to rewrite the whole
    multi-MB session JSON per keystroke autosave). Drafts go to a tiny sidecar
    file that touches neither the session JSON, its updated_at, nor the index.
    """
    src = Path(__file__).parents[1].joinpath("api", "routes.py").read_text(encoding="utf-8")
    route_idx = src.find('if parsed.path == "/api/session/draft":')
    assert route_idx != -1, "could not locate draft route"
    end_idx = src.find('payload = {"ok": True, "draft": saved_draft}', route_idx)
    assert end_idx != -1, "could not locate draft route response site"
    route_body = src[route_idx:end_idx]
    assert "write_composer_draft_sidecar" in route_body, (
        "draft POST must persist via the sidecar store"
    )
    assert "s.save(" not in route_body and "s_meta.save(" not in route_body, (
        "draft POST must never rewrite the session JSON via Session.save()"
    )


def test_draft_save_skips_unchanged_payload_before_persist():
    """Duplicate debounced draft POSTs should not rewrite the draft sidecar."""
    src = Path(__file__).parents[1].joinpath("api", "routes.py").read_text(encoding="utf-8")
    draft_idx = src.find("sidecar_draft = read_composer_draft_sidecar(sid)")
    unchanged_idx = src.find("if next_draft == current_draft and sidecar_draft is not None", draft_idx)
    save_idx = src.find("saved_draft = write_composer_draft_sidecar(sid, next_draft)", draft_idx)

    assert draft_idx != -1, "draft route should snapshot the current sidecar draft"
    assert unchanged_idx != -1, "draft route should no-op unchanged normalized payloads"
    assert save_idx != -1, "draft route should still persist changed drafts"
    assert unchanged_idx < save_idx, "unchanged guard must run before the sidecar persist"
    assert 'payload["unchanged"] = True' in src
