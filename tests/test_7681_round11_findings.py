"""Regression coverage for the 2026-11-18 #7681 review findings.

1. ``all_sessions()`` recovers a sidecar missing from the sidebar index with
   ``Session.load_metadata_only()``, whose ``messages=[]`` made ``compact()``
   report ``user_message_count: 0`` for a conversation that has user turns.
2. The sidebar payload drops ``pending_user_message`` but keeps
   ``has_pending_user_message`` / ``pending_started_at`` / ``is_streaming``,
   so the render gate must consult those fields before showing a count.
"""

from __future__ import annotations

from collections import OrderedDict

import api.models as models


def _isolate(monkeypatch, tmp_path):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(models, "SESSION_DIR", session_dir, raising=False)
    monkeypatch.setattr(
        models, "SESSION_INDEX_FILE", session_dir / "_index.json", raising=False
    )
    monkeypatch.setattr(models, "SESSIONS", OrderedDict(), raising=False)
    return session_dir


def test_metadata_only_stub_reports_unknown_user_turns(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    session = models.Session(
        session_id="meta-stub",
        title="meta stub",
        messages=[
            {"role": "user", "content": "one", "timestamp": 1.0},
            {"role": "assistant", "content": "two", "timestamp": 2.0},
            {"role": "user", "content": "three", "timestamp": 3.0},
        ],
        created_at=1.0,
        updated_at=3.0,
    )
    session.save()

    stub = models.Session.load_metadata_only("meta-stub")
    assert stub is not None
    assert getattr(stub, "_loaded_metadata_only", False) is True
    assert stub.messages == []

    compact = stub.compact(sidebar_metadata_only=True)
    assert compact["user_message_count"] is None, (
        "a metadata-only stub must not report a confident zero for an unread "
        "transcript"
    )


def test_full_load_still_reports_the_user_turn_count(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    session = models.Session(
        session_id="full-load",
        title="full load",
        messages=[
            {"role": "user", "content": "one", "timestamp": 1.0},
            {"role": "assistant", "content": "two", "timestamp": 2.0},
            {"role": "user", "content": "three", "timestamp": 3.0},
        ],
        created_at=1.0,
        updated_at=3.0,
    )
    session.save()

    loaded = models.Session.load("full-load")
    assert loaded is not None
    compact = loaded.compact(sidebar_metadata_only=True)
    assert compact["user_message_count"] == 2
