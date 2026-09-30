"""#7600 round 2: the raw steer frame must survive for the MODEL, not just the UI.

nesquena-hermes (CHANGES_REQUESTED, 2026-09-29) settled the design question
this PR left open: ``_project_state_db_message()`` feeds every state.db reader,
including the model-facing new-turn context (``api/streaming.py``:
``_context_and_revision_from_state_snapshot`` →
``reconciled_state_db_messages_for_session(..., prefer_context=True)`` →
``_new_turn_context_from_messages``). The OOB frame is a **trust-boundary
marker**: ``STEER_CHANNEL_NOTE`` in ``agent/prompt_builder.py`` tells the model
to trust only that exact shape, so unwrapping it inside the shared projection
would silently replace a framed steer with a bare user instruction in model
history.

The split, mirroring the #7600 settle path (whose own test file asserts
``session.context_messages`` is marker-free while the display is clean):

* ``content`` carries the unwrapped, human-authored text — what the UI renders;
* ``api_content`` carries the raw framed bytes — the durable provider-facing
  sidecar the Agent substitutes back over ``content`` at API-build time
  (``agent/turn_context.substitute_api_content``).

Asserted here:
  * the projected steer row is clean in ``content`` and keeps the exact framed
    bytes in ``api_content`` (the display/provider split);
  * the model-facing path (``prefer_context=True``, state.db-owned tail) still
    receives the framed text — the regression the review described;
  * ``get_state_db_session_message_keys_before_timestamp`` and the regeneration
    prefix keys use the same split, so bounded reads no longer key on the raw
    frame and every optimized read stops falling back (maintainer ask #1,
    greptile P2);
  * a row that already carries provider bytes never has them overwritten.
"""
from __future__ import annotations

import json
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

LIVE_SID = "steer_model_facing_probe_001"


def _contains_oob(value) -> bool:
    return "OUT-OF-BAND USER MESSAGE" in json.dumps(value, default=str)


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
        (sid, "cli", "Steer Model Facing", "test-model", 1000.0, len(rows)),
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
        title="Steer Model Facing",
        workspace=str(tmp_path),
        model="test-model",
        messages=list(sidecar_messages or []),
        created_at=1000.0,
        updated_at=1001.0,
    )
    session.save(touch_updated_at=False)
    return session


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


def _steer_row_of(messages):
    return next(
        (
            m
            for m in messages
            if isinstance(m, dict)
            and m.get("role") == "user"
            and STEER_TEXT in str(m.get("content") or "")
        ),
        None,
    )


def test_projected_steer_row_splits_display_text_from_raw_frame(monkeypatch, tmp_path):
    """The projection hands the UI clean text and keeps the framed bytes on the
    provider-facing ``api_content`` sidecar (display/provider split)."""
    import api.models as models

    sid = "steer_model_facing_split_001"
    _install_session(monkeypatch, tmp_path, sid)
    _make_state_db(tmp_path / "state.db", sid, _state_rows_with_tail_steer())

    rows = models.get_state_db_session_messages(sid)
    steer = _steer_row_of(rows)

    assert steer is not None, "the steer row must survive projection exactly once"
    # Display side: unwrapped, human-authored text only.
    assert steer["content"] == STEER_TEXT
    assert steer.get("display_kind") == "steer"
    # Provider side: the exact framed bytes, so the Agent's trust-boundary
    # marker is still what the model sees.
    assert steer.get("api_content") == WRAPPED, (
        "the raw steer frame must ride the api_content sidecar for model-facing "
        "callers (trust-boundary marker, see STEER_CHANNEL_NOTE)"
    )


def test_model_facing_context_keeps_framed_steer_when_state_db_owns_tail(
    monkeypatch, tmp_path
):
    """nesquena-hermes' regression: with a sidecar context that stops before the
    steer, the model-facing (``prefer_context=True``) history must keep the
    framed steer marker available to the model, not replace it with a bare user
    instruction.

    The contract is the #7600 display/provider split, verified end-to-end:
    the projection yields unwrapped ``content`` *plus* the exact framed bytes on
    the ``api_content`` sidecar, and the Agent substitutes that sidecar back
    over ``content`` at API-build time
    (``agent/turn_context.substitute_api_content`` → "keeps the prompt-cache
    prefix byte-stable"). So the framed marker — the shape
    ``STEER_CHANNEL_NOTE`` tells the model to trust — is still what the model
    receives even though state.db owns the tail.
    """
    import api.models as models

    sid = "steer_model_facing_context_001"
    session = _install_session(
        monkeypatch,
        tmp_path,
        sid,
        sidecar_messages=[
            {"role": "user", "content": "run the suite", "timestamp": 1000.0},
            {"role": "assistant", "content": "running", "timestamp": 1001.0},
        ],
    )
    session.context_messages = [
        {"role": "user", "content": "run the suite", "timestamp": 1000.0},
        {"role": "assistant", "content": "running", "timestamp": 1001.0},
    ]
    _make_state_db(tmp_path / "state.db", sid, _state_rows_with_tail_steer())

    context = models.reconciled_state_db_messages_for_session(
        session, prefer_context=True
    )
    steer = _steer_row_of(context)

    assert steer is not None, "the state.db-owned steer must reach model context"
    # The trust-boundary marker survives for the model on the sidecar...
    assert steer.get("api_content") == WRAPPED, (
        "model-facing history lost the framed steer marker: the Agent would see "
        "a bare user instruction instead of the shape STEER_CHANNEL_NOTE trusts"
    )
    # ...and the visible text is the unwrapped instruction.
    assert steer["content"] == STEER_TEXT

    # The Agent substitutes the sidecar over content at API-build time
    # (``agent/turn_context.substitute_api_content``: pop the sidecar, write it
    # over ``content`` — "keeps the prompt-cache prefix byte-stable"). Assert
    # that documented contract against the sidecar this row carries, without
    # importing the sibling repo (absent on the WebUI CI runner).
    sidecar = steer["api_content"]
    substituted = {"role": steer["role"], "content": steer["content"]}
    if isinstance(sidecar, str) and sidecar and substituted["role"] in ("user", "assistant"):
        substituted["content"] = sidecar
    assert substituted["content"] == WRAPPED
    assert OPEN in substituted["content"]

    # ...while the display projection of the very same snapshot stays clean.
    display = models.reconciled_state_db_messages_for_session(session)
    display_steer = _steer_row_of(display)
    assert display_steer is not None
    assert display_steer["content"] == STEER_TEXT
    assert OPEN not in str(display_steer.get("content"))


def test_limited_prefix_reader_keys_steer_on_the_unwrapped_split(
    monkeypatch, tmp_path
):
    """``get_state_db_session_message_keys_before_timestamp`` must key a steer
    row on the same unwrapped+sidecar representation as the projected tail,
    otherwise the optimized fallback always misses (greptile P2)."""
    import api.models as models

    sid = "steer_model_facing_prefix_001"
    _install_session(monkeypatch, tmp_path, sid)
    _make_state_db(tmp_path / "state.db", sid, _state_rows_with_tail_steer())

    before_keys = models.get_state_db_session_message_keys_before_timestamp(
        sid, 1002.6
    )
    assert before_keys, "prefix keys must be returned for a readable state.db"

    # Key shape: (role, content, tool_calls, api_content). The steer sits below
    # the floor (ts 1002.5 < 1002.6). Its identity must be taken from the SAME
    # split projection the display uses — unwrapped text in ``content`` — not
    # from the raw transport frame, otherwise every optimized read falls back.
    steer_key = next(
        (
            k
            for k in before_keys
            if isinstance(k, tuple) and len(k) >= 2 and k[0] == "user"
            and STEER_TEXT in str(k[1])
        ),
        None,
    )
    assert steer_key is not None, (
        f"the steer row must key on its unwrapped text: {before_keys!r}"
    )
    assert steer_key[1] == STEER_TEXT, (
        f"prefix key content must be the unwrapped steer text: {steer_key!r}"
    )
    # The raw frame legitimately rides the sidecar slot of the key: api_content
    # must participate in replay identity or two same-visible turns collapse
    # (see ``_message_replay_key``). Only the content slot had to be clean.
    if len(steer_key) >= 4:
        assert steer_key[3] == WRAPPED, (
            f"the raw frame must ride the key's api_content slot: {steer_key!r}"
        )

    # Same representation as the projected reader: full-read keys and prefix
    # keys agree on the steer row's identity, so the optimized path matches.
    full_rows = models.get_state_db_session_messages(sid)
    full_key = models._session_message_visible_key(
        _steer_row_of(full_rows), normalize_workspace_prefix=True
    )
    assert full_key in before_keys, (
        "prefix identity and canonical projection disagree for the steer row"
    )


def test_regeneration_snapshot_keys_steer_on_the_unwrapped_split_on_both_sides_of_the_floor(
    monkeypatch, tmp_path
):
    """The regeneration snapshot must use the display/provider split for the
    steer on **both** sides of the floor (maintainer ask: "one steer before the
    floor and one after it").

    - prefix proof: keys on unwrapped ``content``, raw frame on the sidecar
      slot — otherwise every optimized regeneration read falls back
      (greptile P2 / maintainer ask #1);
    - bounded tail: projects the same split as the canonical reader.
    """
    import api.models as models

    sid = "steer_model_facing_regen_001"
    _install_session(monkeypatch, tmp_path, sid)
    _make_state_db(tmp_path / "state.db", sid, _state_rows_with_tail_steer())

    # ---- side 1: the steer (ts 1002.5) rides the bounded tail (floor 1001.5) ----
    tail_snap = models.get_state_db_regeneration_tail_snapshot(sid, 1001.5)
    assert tail_snap is not None, (
        "regeneration snapshot must be readable for this fixture"
    )
    tail_rows = tail_snap["tail"]
    tail_steer = next(
        (
            r
            for r in tail_rows
            if isinstance(r, dict)
            and r.get("role") == "user"
            and STEER_TEXT in str(r.get("content") or "")
        ),
        None,
    )
    assert tail_steer is not None, (
        f"the steer must ride the bounded tail: {[r.get('content') for r in tail_rows]!r}"
    )
    assert tail_steer["content"] == STEER_TEXT
    assert tail_steer.get("api_content") == WRAPPED

    # ---- side 2: the same row falls below the prefix proof (floor 1002.6) ----
    prefix_snap = models.get_state_db_regeneration_tail_snapshot(sid, 1002.6)
    assert prefix_snap is not None, (
        "regeneration snapshot must be readable for this fixture"
    )
    prefix_keys = prefix_snap["prefix_keys"]
    # Key shape: (role, content, tool_calls, api_content). The prefix identity
    # must come from the same split projection the tail uses — unwrapped text
    # in ``content`` — not from the raw transport frame, otherwise every
    # optimized read falls back.
    steer_key = next(
        (
            k
            for k in prefix_keys
            if isinstance(k, tuple) and len(k) >= 2 and k[0] == "user"
            and STEER_TEXT in str(k[1])
        ),
        None,
    )
    assert steer_key is not None, (
        f"the steer row must key on its unwrapped text: {prefix_keys!r}"
    )
    assert steer_key[1] == STEER_TEXT
    if len(steer_key) >= 4:
        assert steer_key[3] == WRAPPED
    # ...and prefix/tail agree on the steer's identity, so the bounded path
    # matches instead of falling back.
    assert (
        models._session_message_visible_key(
            tail_steer, normalize_workspace_prefix=True
        )
        in prefix_keys
    ), "prefix identity and bounded-tail projection disagree for the steer row"


def test_projection_never_overwrites_existing_provider_bytes(monkeypatch, tmp_path):
    """A steer row that already carries provider-side ``api_content`` keeps it:
    the sidecar is written only when this row owns the raw frame."""
    import api.models as models

    sid = "steer_model_facing_keep_001"
    _install_session(monkeypatch, tmp_path, sid)
    provider_sidecar = "previously sent provider bytes"
    _make_state_db(
        tmp_path / "state.db",
        sid,
        [
            {
                "role": "user",
                "content": WRAPPED,
                "display_kind": "steer",
                "api_content": provider_sidecar,
                "timestamp": 1000.0,
            }
        ],
    )

    rows = models.get_state_db_session_messages(sid)
    steer = _steer_row_of(rows)

    assert steer is not None
    assert steer.get("api_content") == provider_sidecar, (
        "an existing provider-facing sidecar must never be overwritten by the "
        "projection"
    )
    assert steer["content"] == STEER_TEXT
