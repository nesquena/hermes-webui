"""Regression tests for issue #5339.

Post-restart stale user message permanently prepended to all subsequent turns.

Root cause was a KEY-FUNCTION MISMATCH between two dedup layers:

* The reconciliation delta ``state_db_delta_after_context`` keys rows via
  ``_session_message_content_key`` -> ``_normalized_session_message_content``
  in ``api/models.py``, which used to normalize whitespace ONLY, with NO
  workspace-prefix stripping.
* The streaming-side identity ``_message_identity`` in ``api/streaming.py``
  DOES strip the workspace prefix for user turns, because WebUI sends the model
  a workspace-prefixed ``user_message`` (``[Workspace::v1: /workspace]\\n<text>``)
  while the visible/optimistic bubble (and the WebUI sidecar row) carries only
  the bare ``<text>``.

So a state.db row carrying the workspace-prefixed text and a sidecar row
carrying the bare text produced DIFFERENT keys; the alignment loop in
``state_db_delta_after_context`` failed to match them, treated the state.db copy
as NEW, and appended a duplicate user row. The agent-side merge then
concatenated the two adjacent user rows into a permanent composite.

The fix makes ``_session_message_content_key`` strip the workspace prefix for
user-role messages (reusing the SAME ``_strip_workspace_prefix`` helper the
streaming side uses), so the two layers can no longer disagree.
"""
import json
import sqlite3
from collections import OrderedDict
from io import BytesIO
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

pytestmark = pytest.mark.requires_agent_modules


WORKSPACE_PREFIX = "[Workspace::v1: /workspace]\n"


class _GetHandler:
    def __init__(self, path):
        self.path = path
        self.headers = {}
        self.client_address = ("127.0.0.1", 12345)
        self.status = None
        self.wfile = BytesIO()
        self.response_headers = []

    def send_response(self, status):
        self.status = status

    def send_header(self, key, value):
        self.response_headers.append((key, value))

    def end_headers(self):
        pass

    @property
    def response_json(self):
        return json.loads(self.wfile.getvalue().decode("utf-8"))

    @property
    def query(self):
        return parse_qs(urlparse(self.path).query)

    def log_message(self, *args, **kwargs):
        pass


def _make_state_db(path: Path, sid: str, rows):
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, title TEXT, model TEXT, started_at REAL, message_count INTEGER)"
    )
    conn.execute(
        "CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, role TEXT, content TEXT, timestamp REAL, tool_call_id TEXT, tool_calls TEXT, tool_name TEXT)"
    )
    conn.execute(
        "INSERT INTO sessions (id, source, title, model, started_at, message_count) VALUES (?, ?, ?, ?, ?, ?)",
        (sid, "webui", "Reconcile", "test-model", 1000.0, len(rows)),
    )
    for row in rows:
        conn.execute(
            "INSERT INTO messages (session_id, role, content, timestamp, tool_call_id, tool_calls, tool_name) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                sid,
                row["role"],
                row["content"],
                row.get("timestamp", 1000.0),
                row.get("tool_call_id"),
                row.get("tool_calls"),
                row.get("tool_name"),
            ),
        )
    conn.commit()
    conn.close()


def _install_test_session(monkeypatch, tmp_path, sid, sidecar_messages):
    import api.config as config
    import api.models as models
    import api.routes as routes
    import api.profiles as profiles

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
        title="Reconcile",
        workspace=str(tmp_path),
        model="test-model",
        messages=sidecar_messages,
        created_at=1000.0,
        updated_at=1001.0,
    )
    session.save(touch_updated_at=False)
    return session


# ---------------------------------------------------------------------------
# Unit-level: the two dedup key functions must agree for user turns.
# ---------------------------------------------------------------------------


def test_content_key_strips_workspace_prefix_for_user_turns():
    """A prefixed state.db user row and a bare sidecar user row key IDENTICALLY.

    This is the core of #5339: the reconciliation key and the streaming
    identity must produce the same key for the same human turn.
    """
    from api.models import _session_message_content_key
    from api.streaming import _message_identity

    prefixed = {"role": "user", "content": WORKSPACE_PREFIX + "Hello world"}
    bare = {"role": "user", "content": "Hello world"}

    # The reconciliation key must now match across the prefix boundary.
    assert _session_message_content_key(prefixed) == _session_message_content_key(bare)

    # And it must agree with the streaming-side identity's view of the pair
    # (both treat the prefixed and bare form as the same user turn).
    assert _message_identity(prefixed) == _message_identity(bare)


def test_content_key_is_idempotent_for_bare_user_message():
    """A user message with no prefix keys identically before and after the fix."""
    from api.models import _session_message_content_key

    bare = {"role": "user", "content": "just a plain message"}
    assert _session_message_content_key(bare) == (
        "user",
        "just a plain message",
        "",
        "",
    )


def test_content_key_does_not_strip_for_non_user_roles():
    """Prefix stripping must be scoped to user turns only (matches streaming)."""
    from api.models import _session_message_content_key

    prefixed = {"role": "assistant", "content": WORKSPACE_PREFIX + "Hello world"}
    bare = {"role": "assistant", "content": "Hello world"}
    # Assistant rows are untouched: a literal prefix stays part of the key.
    assert _session_message_content_key(prefixed) != _session_message_content_key(bare)


def test_content_key_keeps_distinct_user_messages_distinct():
    """Guard: two genuinely different user messages still key differently."""
    from api.models import _session_message_content_key

    one = {"role": "user", "content": WORKSPACE_PREFIX + "first question"}
    two = {"role": "user", "content": WORKSPACE_PREFIX + "second question"}
    assert _session_message_content_key(one) != _session_message_content_key(two)


def test_native_multimodal_sidecar_reconciles_with_agent_text_projection():
    """A completed native-image turn keeps the rich sidecar row exactly once."""
    from api.models import merge_session_messages_append_only

    timestamp = 1766352000.123456
    prompt = (
        "[Workspace::v1: /workspace]\n"
        "Describe this image\n\n"
        "[Attached files: /absolute/path/image.png]"
    )
    sidecar = {
        "id": "webui-112",
        "role": "user",
        "content": [
            {"type": "text", "text": prompt},
            {
                "type": "image_url",
                "image_url": {
                    "url": "data:image/png;base64,ZmFrZS1pbWFnZS1ieXRlcw==",
                },
            },
        ],
        "attachments": ["image.png"],
        "timestamp": timestamp,
    }
    state = {
        "role": "user",
        "content": f"{prompt}\n[screenshot]",
        "timestamp": timestamp,
        "api_content": "trusted provider wire content",
    }

    merged = merge_session_messages_append_only([sidecar], [state])

    assert merged == [sidecar]
    assert merged[0] is sidecar
    assert merged[0]["content"] is sidecar["content"]
    assert merged[0]["attachments"] is sidecar["attachments"]
    assert merged[0]["api_content"] == "trusted provider wire content"


def test_ordinary_reconciliation_preserves_conflicting_provider_sidecars():
    """Conflicting provider payloads remain separate ordinary rows."""
    from api.models import merge_session_messages_append_only

    timestamp = 1766352000.123456
    sidecar = {
        "role": "assistant",
        "content": "same answer",
        "timestamp": timestamp,
        "_state_db_row_id": 7,
        "api_content": "wire-a",
    }
    state = {
        "role": "assistant",
        "content": "same answer",
        "timestamp": timestamp,
        "_state_db_row_id": 7,
        "api_content": "wire-b",
    }

    assert merge_session_messages_append_only([sidecar], [state]) == [sidecar, state]


def test_multimodal_mirror_requires_exact_timestamp_and_image_parts():
    """Cross-store image identity never participates in timestamp-free replay."""
    from api.models import (
        _session_message_multimodal_mirror_key,
        merge_session_messages_append_only,
    )

    timestamp = 1766352000.123456
    multimodal = {
        "id": "webui-image",
        "role": "user",
        "content": [
            {"type": "text", "text": "type [screenshot] literally"},
            {"type": "image", "image": "data:one"},
            {"type": "input_image", "image_url": "data:two"},
        ],
        "timestamp": timestamp,
    }
    exact_mirror = {
        "role": "user",
        "content": "type [screenshot] literally\n[screenshot]\n[screenshot]",
        "timestamp": timestamp,
    }
    mixed_case_role = {**exact_mirror, "role": "  UsEr "}
    assert _session_message_multimodal_mirror_key(mixed_case_role) == (
        _session_message_multimodal_mirror_key(exact_mirror)
    )
    assert merge_session_messages_append_only([multimodal], [exact_mirror]) == [multimodal]

    later_literal = {**exact_mirror, "timestamp": timestamp + 0.5}
    later_sidecar = {
        "id": "later-assistant",
        "role": "assistant",
        "content": "later sidecar row",
        "timestamp": timestamp + 1,
    }
    merged = merge_session_messages_append_only(
        [multimodal, later_sidecar],
        [later_literal],
    )
    assert merged == [multimodal, later_literal, later_sidecar]

    unknown_parts = [
        {"type": "text", "text": "keep the original list identity"},
        {"type": "document", "document": {"id": "doc-1"}},
    ]
    text_only_parts = [{"type": "text", "text": "not an image-bearing list"}]
    malformed_text_parts = [
        {"type": "text", "text": 7},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA=="}},
    ]
    for parts in (unknown_parts, text_only_parts, malformed_text_parts, []):
        sidecar = {
            "id": f"structured-{len(parts)}",
            "role": "user",
            "content": parts,
            "timestamp": timestamp,
        }
        assert _session_message_multimodal_mirror_key(
            sidecar,
            require_image_parts=True,
        ) is None

    distinct_state = {
        "role": "user",
        "content": "different text\n[screenshot]\n[screenshot]",
        "timestamp": timestamp,
    }
    assert len(merge_session_messages_append_only([multimodal], [distinct_state])) == 2


def test_multimodal_mirror_collapses_identical_duplicate_state_rows():
    """Byte-identical scalar mirrors reconcile to the rich sidecar row."""
    from api.models import merge_session_messages_append_only

    timestamp = 1766352000.123456
    sidecar = {
        "role": "user",
        "content": [
            {"type": "text", "text": "describe this image"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA=="}},
        ],
        "timestamp": timestamp,
    }
    state = {
        "role": "user",
        "content": "describe this image\n[screenshot]",
        "timestamp": timestamp,
    }

    merged = merge_session_messages_append_only([sidecar], [state, dict(state)])

    assert merged == [sidecar]
    assert merged[0] is sidecar


def test_multimodal_mirror_state_identity_ambiguity_is_order_independent():
    """Contradictory state provenance does not fold every mirror into sidecar."""
    from api.models import merge_session_messages_append_only

    timestamp = 1766352000.123456
    sidecar = {
        "role": "user",
        "content": [
            {"type": "text", "text": "describe this image"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA=="}},
        ],
        "timestamp": timestamp,
    }
    state_rows = [
        {
            "role": "user",
            "content": "describe this image\n[screenshot]",
            "timestamp": timestamp,
            "_state_db_row_id": 7,
        },
        {
            "role": "user",
            "content": "describe this image\n[screenshot]",
            "timestamp": timestamp,
        },
        {
            "role": "user",
            "content": "describe this image\n[screenshot]",
            "timestamp": timestamp,
            "_state_db_row_id": 8,
        },
    ]

    merged = merge_session_messages_append_only([sidecar], state_rows)

    assert merged == [sidecar, state_rows[0]]
    assert merged[0]["content"] is sidecar["content"]
    assert merged[1]["_state_db_row_id"] == 7


def test_multimodal_mirror_rejects_conflicting_private_identity():
    """A projected image mirror never overrides contradictory provenance."""
    from api.models import merge_session_messages_append_only

    timestamp = 1766352000.123456
    base_sidecar = {
        "role": "user",
        "content": [
            {"type": "text", "text": "describe this image"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA=="}},
        ],
        "timestamp": timestamp,
    }
    base_state = {
        "role": "user",
        "content": "describe this image\n[screenshot]",
        "timestamp": timestamp,
    }
    conflicts = (
        ({"id": "sidecar-a", "message_id": "sidecar-b"}, {}),
        ({"id": "sidecar"}, {"id": "state"}),
        ({}, {"_state_db_row_id": True}),
        ({"_state_db_row_id": 7}, {"_state_db_row_id": 8}),
        ({"api_content": "wire-a"}, {"api_content": "wire-b"}),
    )

    for sidecar_identity, state_identity in conflicts:
        sidecar = {**base_sidecar, **sidecar_identity}
        state = {**base_state, **state_identity}
        assert merge_session_messages_append_only([sidecar], [state]) == [sidecar, state]


# ---------------------------------------------------------------------------
# Delta-level: state_db_delta_after_context must not append a duplicate.
# ---------------------------------------------------------------------------


def test_state_db_delta_treats_prefixed_state_row_as_mirror_of_bare_sidecar():
    """state.db copy of a completed turn (prefixed) is NOT re-appended.

    Repro of #5339 at the reconciliation layer: after a restart the state.db
    holds a workspace-prefixed copy of the last completed user turn while the
    sidecar holds the bare optimistic bubble. They must reconcile to no delta.
    """
    from api.models import state_db_delta_after_context

    sidecar = [
        {"role": "user", "content": "hello there", "timestamp": 1000.0},
        {"role": "assistant", "content": "hi back", "timestamp": 1001.0},
    ]
    state = [
        {"role": "user", "content": WORKSPACE_PREFIX + "hello there", "timestamp": 1000.0},
        {"role": "assistant", "content": "hi back", "timestamp": 1001.0},
    ]

    # No duplicate user row surfaces -- the prefixed state.db copy aligns with
    # the bare sidecar row.
    assert state_db_delta_after_context(sidecar, state) == []


def test_state_db_delta_still_surfaces_genuinely_new_user_turn():
    """Guard: a genuinely new (post-restart) user turn is still a real delta."""
    from api.models import state_db_delta_after_context

    sidecar = [
        {"role": "user", "content": "hello there", "timestamp": 1000.0},
        {"role": "assistant", "content": "hi back", "timestamp": 1001.0},
    ]
    state = [
        {"role": "user", "content": WORKSPACE_PREFIX + "hello there", "timestamp": 1000.0},
        {"role": "assistant", "content": "hi back", "timestamp": 1001.0},
        {"role": "user", "content": WORKSPACE_PREFIX + "brand new question", "timestamp": 1002.0},
        {"role": "assistant", "content": "brand new answer", "timestamp": 1003.0},
    ]

    delta = state_db_delta_after_context(sidecar, state)
    assert [m["content"] for m in delta] == [
        WORKSPACE_PREFIX + "brand new question",
        "brand new answer",
    ]


# ---------------------------------------------------------------------------
# End-to-end: /api/session load must not duplicate the restart-boundary turn.
# ---------------------------------------------------------------------------


def test_api_session_load_does_not_duplicate_prefixed_restart_turn(monkeypatch, tmp_path):
    """After a restart, the state.db (prefixed) copy of a completed turn is not
    added as a second user bubble alongside the bare sidecar copy (#5339)."""
    import api.routes as routes

    sid = "webui_issue5339_restart"
    sidecar_messages = [
        {"role": "user", "content": "first turn", "timestamp": 1000.0},
        {"role": "assistant", "content": "first answer", "timestamp": 1001.0},
    ]
    # state.db mirrors the completed turn with the workspace-prefixed user text
    # (what the model actually received) plus a genuinely new post-restart turn.
    state_rows = [
        {"role": "user", "content": WORKSPACE_PREFIX + "first turn", "timestamp": 1000.0},
        {"role": "assistant", "content": "first answer", "timestamp": 1001.0},
        {"role": "user", "content": WORKSPACE_PREFIX + "second turn", "timestamp": 1002.0},
        {"role": "assistant", "content": "second answer", "timestamp": 1003.0},
    ]
    _install_test_session(monkeypatch, tmp_path, sid, sidecar_messages)
    _make_state_db(tmp_path / "state.db", sid, state_rows)

    handler = _GetHandler(f"/api/session?session_id={sid}&messages=1&resolve_model=0")
    routes.handle_get(handler, urlparse(handler.path))

    assert handler.status == 200
    messages = handler.response_json["session"]["messages"]
    contents = [m["content"] for m in messages]

    # Before the fix, the prefixed state.db copy of the completed turn keyed
    # differently from the bare sidecar row, so reconciliation re-appended it:
    # the transcript grew to 6 rows with "first answer" twice and a stray
    # "[Workspace::v1: ...]\nfirst turn" mirror inserted at the restart
    # boundary. After the fix the completed turn reconciles to a single copy.
    assert contents.count("first turn") == 1
    assert contents.count("first answer") == 1
    assert (WORKSPACE_PREFIX + "first turn") not in contents  # no duplicate mirror

    # The genuinely new post-restart turn is still surfaced (in whatever form
    # the reconciliation layer carries it through).
    assert any("second turn" in str(c) for c in contents)
    assert any("second answer" in str(c) for c in contents)
