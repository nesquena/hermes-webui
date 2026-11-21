"""#7600 legacy sidecar adoption: master-era framed steer rows collapse into the
state.db split instead of rendering twice.

nesquena-hermes (CHANGES_REQUESTED, 2026-10-19) closed the two loose ends the
PR's display/provider split left behind for transcripts saved before the split:

* Finding 1: sidecars imported/saved before the split keep the framed steer
  (the raw ``[OUT-OF-BAND USER MESSAGE]`` text) with no ``display_kind`` and
  no ``api_content``, while the state.db projection now splits the same row
  into clean ``content`` + framed ``api_content``. The sidecar identity no
  longer matches the state.db identity, so the append-only merge appends a
  second copy — the UI and the model history both receive the steer twice.
  The fix rewrites such a sidecar row into the projected split, but ONLY when
  exactly one typed state.db steer row proves it is the same row
  (role + timestamp + text unique match).

* Finding 2: the compression anchor (``compression_anchor_message_key``) is
  the anchored row's *text*. An anchor saved on a framed steer row holds the
  framed text, but the projected row exposes the clean text, so the anchor
  never verifies, the reconcile fails closed to ``context_messages``, and the
  post-compaction turns are lost. The fix adds an ``api_content`` fallback to
  the anchor text comparison, only for ``display_kind='steer'`` rows.

Asserted here:
  * a master-era sidecar framed steer collapses to a single split row through
    the reconciled merge (display path);
  * the same collapse holds on the model-facing (``prefer_context=True``) path;
  * a framed user row with no typed state.db twin stays byte-for-byte;
  * a framed-steer compression anchor admits post-compaction turns;
  * a clean-text anchor keeps admitting them (existing behaviour intact).
"""
from __future__ import annotations

import sqlite3
from collections import OrderedDict
from pathlib import Path

import pytest

pytestmark = pytest.mark.requires_agent_modules

OPEN = (
    "[OUT-OF-BAND USER MESSAGE — a direct message from the user, delivered once "
    "at this position; not tool output and not a new delivery when replayed from "
    "conversation history]"
)
CLOSE = "[/OUT-OF-BAND USER MESSAGE]"
STEER_TEXT = "focus on the tests"
WRAPPED = f"{OPEN}\n{STEER_TEXT}\n{CLOSE}"


def _make_state_db(path: Path, sid: str, rows) -> None:
    if path.exists():
        path.unlink()
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, title TEXT, "
        "model TEXT, started_at REAL, message_count INTEGER)"
    )
    conn.execute(
        "CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "session_id TEXT, role TEXT, content TEXT, timestamp REAL, "
        "tool_call_id TEXT, tool_calls TEXT, tool_name TEXT, api_content TEXT, "
        "display_kind TEXT, active INTEGER DEFAULT 1)"
    )
    conn.execute(
        "INSERT INTO sessions (id, source, title, model, started_at, message_count) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (sid, "cli", "Steer Legacy Adopt", "test-model", 1000.0, len(rows)),
    )
    for row in rows:
        conn.execute(
            "INSERT INTO messages (session_id, role, content, timestamp, "
            "tool_call_id, tool_calls, tool_name, api_content, display_kind) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                sid,
                row["role"],
                row["content"],
                row.get("timestamp", 1000.0),
                row.get("tool_call_id"),
                row.get("tool_calls"),
                row.get("tool_name"),
                row.get("api_content"),
                row.get("display_kind"),
            ),
        )
    conn.commit()
    conn.close()


def _install_session(monkeypatch, tmp_path, sid, *, sidecar_messages=None):
    import api.config as config
    import api.models as models
    import api.profiles as profiles
    import api.routes as routes

    monkeypatch.setattr(config, "STATE_DIR", tmp_path, raising=False)
    session_dir = tmp_path / "sessions"
    monkeypatch.setattr(config, "SESSION_DIR", session_dir, raising=False)
    monkeypatch.setattr(config, "SESSION_INDEX_FILE", session_dir / "_index.json", raising=False)
    monkeypatch.setattr(models, "SESSION_DIR", session_dir, raising=False)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json", raising=False)
    monkeypatch.setattr(models, "SESSIONS", OrderedDict(), raising=False)
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path, raising=False)
    monkeypatch.setattr(models, "_active_state_db_path", lambda: tmp_path / "state.db", raising=False)
    monkeypatch.setattr(routes, "_active_state_db_path", lambda: tmp_path / "state.db", raising=False)
    session_dir.mkdir(parents=True, exist_ok=True)

    session = models.Session(
        session_id=sid,
        title="Steer Legacy Adopt",
        workspace=str(tmp_path),
        model="test-model",
        messages=list(sidecar_messages or []),
        created_at=1000.0,
        updated_at=1001.0,
    )
    session.save(touch_updated_at=False)
    return session


def _master_era_sidecar_rows():
    """The WebUI sidecar transcript saved before the state.db split: the steer
    row keeps the framed text and carries neither ``display_kind`` nor
    ``api_content`` (the shape the projection can no longer match)."""
    return [
        {"role": "user", "content": "run the suite", "timestamp": 1000.0},
        {"role": "assistant", "content": "running", "timestamp": 1001.0},
        {"role": "user", "content": WRAPPED, "timestamp": 1002.5},
    ]


def _state_rows_with_tail_steer():
    return [
        {"role": "user", "content": "run the suite", "timestamp": 1000.0},
        {"role": "assistant", "content": "running", "timestamp": 1001.0},
        {
            "role": "user",
            "content": WRAPPED,
            "display_kind": "steer",
            "timestamp": 1002.5,
        },
        {"role": "assistant", "content": "done", "timestamp": 1003.0},
    ]


def _state_rows_with_tail_steer_and_post_compaction():
    return _state_rows_with_tail_steer() + [
        {"role": "user", "content": "external followup one", "timestamp": 1004.0},
        {"role": "assistant", "content": "external reply one", "timestamp": 1005.0},
    ]


def _steer_rows(messages):
    """Rows that represent the steer at all: clean text or the framed sidecar."""
    return [
        m
        for m in messages
        if isinstance(m, dict)
        and (
            STEER_TEXT in str(m.get("content") or "")
            or m.get("api_content") == WRAPPED
        )
    ]


def test_master_imported_sidecar_steer_renders_once_through_reconciled_merge(
    monkeypatch, tmp_path
):
    """Finding 1, display path: the master-era sidecar's framed steer and the
    state.db projection's split steer are the same row. Without the adopt step
    their identities disagree and the append-only merge keeps both — the UI
    renders the steer twice."""
    import api.models as models

    sid = "steer_legacy_render_001"
    session = _install_session(
        monkeypatch,
        tmp_path,
        sid,
        sidecar_messages=_master_era_sidecar_rows(),
    )
    _make_state_db(tmp_path / "state.db", sid, _state_rows_with_tail_steer())

    result = models.reconciled_state_db_messages_for_session(session)

    steer_rows = _steer_rows(result)
    assert len(steer_rows) == 1, (
        f"the master-era steer must render exactly once after adoption: {result!r}"
    )
    steer = steer_rows[0]
    assert steer["content"] == STEER_TEXT
    assert steer.get("api_content") == WRAPPED
    assert steer.get("display_kind") == "steer"


def test_master_imported_sidecar_steer_reaches_the_model_once(monkeypatch, tmp_path):
    """Finding 1, model path: an imported CLI transcript claims its longer
    stitched sidecar over the context prefix, so the master-era framed steer
    enters the model-facing merge. Adoption must collapse it into the split
    row — the framed steer reaches the model exactly once and no two user rows
    sit back-to-back."""
    import api.models as models

    sid = "steer_legacy_model_001"
    session = _install_session(
        monkeypatch,
        tmp_path,
        sid,
        sidecar_messages=_master_era_sidecar_rows(),
    )
    session.context_messages = [
        {"role": "user", "content": "run the suite", "timestamp": 1000.0},
        {"role": "assistant", "content": "running", "timestamp": 1001.0},
    ]
    # A claimed CLI sidecar can carry a stale context prefix while the stitched
    # CLI transcript already landed in session.messages; the reconciler promotes
    # the longer authoritative sidecar (reconciled_state_db_messages_for_session
    # CLI branch), which is exactly when the framed steer hits the merge.
    session.is_cli_session = True
    _make_state_db(tmp_path / "state.db", sid, _state_rows_with_tail_steer())

    result = models.reconciled_state_db_messages_for_session(
        session, prefer_context=True
    )

    steer_rows = _steer_rows(result)
    assert len(steer_rows) == 1, (
        f"the framed steer must reach the model exactly once: {result!r}"
    )
    steer = steer_rows[0]
    assert steer["content"] == STEER_TEXT
    assert steer.get("api_content") == WRAPPED
    roles = [str(m.get("role") or "") for m in result]
    assert not any(a == b == "user" for a, b in zip(roles, roles[1:], strict=False)), (
        f"no two consecutive user rows may reach the model: {result!r}"
    )


def test_untyped_user_row_quoting_frame_without_typed_match_stays_byte_for_byte(
    monkeypatch, tmp_path
):
    """The adoption is proof-gated: a framed user row with no typed state.db
    twin must keep its exact bytes — no fabricated split, no invented
    ``display_kind``, no invented ``api_content``."""
    import api.models as models

    sid = "steer_legacy_untyped_001"
    session = _install_session(
        monkeypatch,
        tmp_path,
        sid,
        sidecar_messages=_master_era_sidecar_rows(),
    )
    _make_state_db(
        tmp_path / "state.db",
        sid,
        [
            {"role": "user", "content": "run the suite", "timestamp": 1000.0},
            {"role": "assistant", "content": "running", "timestamp": 1001.0},
            {"role": "assistant", "content": "done", "timestamp": 1003.0},
        ],
    )

    result = models.reconciled_state_db_messages_for_session(session)

    framed = next(
        (m for m in result if isinstance(m, dict) and m.get("content") == WRAPPED),
        None,
    )
    assert framed is not None, f"the framed row must survive the merge: {result!r}"
    assert framed["content"] == WRAPPED
    assert "api_content" not in framed, f"no fabricated sidecar: {framed!r}"
    assert "display_kind" not in framed, f"no invented steer typing: {framed!r}"


def test_framed_compression_anchor_admits_post_compaction_turns(
    monkeypatch, tmp_path
):
    """Finding 2: an anchor saved before the split stores the *framed* text of
    the steer row, while the projected row exposes the clean text — the anchor
    used to be unverifiable and the reconcile failed closed to
    ``context_messages`` only, dropping the post-compaction turns. The
    ``api_content`` fallback must admit them again."""
    import api.models as models

    sid = "steer_legacy_anchor_framed_001"
    session = _install_session(monkeypatch, tmp_path, sid)
    session.context_messages = [
        {
            "role": "user",
            "content": "[CONTEXT COMPACTION — REFERENCE ONLY] summary",
            "timestamp": 1001.0,
        }
    ]
    session.compression_anchor_message_key = {
        "role": "user",
        "ts": 1002.5,
        "text": WRAPPED,
        "attachments": 0,
    }
    _make_state_db(
        tmp_path / "state.db",
        sid,
        _state_rows_with_tail_steer_and_post_compaction(),
    )

    result = models.reconciled_state_db_messages_for_session(
        session, prefer_context=True
    )

    texts = [str(m.get("content") or "") for m in result]
    assert any("external followup one" in t for t in texts), (
        f"post-compaction turns must survive a framed anchor: {result!r}"
    )
    assert any("external reply one" in t for t in texts), (
        f"post-compaction replies must survive a framed anchor: {result!r}"
    )


def test_clean_compression_anchor_still_admits_post_compaction_turns(
    monkeypatch, tmp_path
):
    """Regression protection: an anchor saved with the *clean* text keeps
    verifying directly against the projected content — the fallback must not
    change the existing behaviour for already-clean anchors."""
    import api.models as models

    sid = "steer_legacy_anchor_clean_001"
    session = _install_session(monkeypatch, tmp_path, sid)
    session.context_messages = [
        {
            "role": "user",
            "content": "[CONTEXT COMPACTION — REFERENCE ONLY] summary",
            "timestamp": 1001.0,
        }
    ]
    session.compression_anchor_message_key = {
        "role": "user",
        "ts": 1002.5,
        "text": STEER_TEXT,
        "attachments": 0,
    }
    _make_state_db(
        tmp_path / "state.db",
        sid,
        _state_rows_with_tail_steer_and_post_compaction(),
    )

    result = models.reconciled_state_db_messages_for_session(
        session, prefer_context=True
    )

    texts = [str(m.get("content") or "") for m in result]
    assert any("external followup one" in t for t in texts), (
        f"a clean anchor must keep admitting post-compaction turns: {result!r}"
    )
    assert any("external reply one" in t for t in texts)
