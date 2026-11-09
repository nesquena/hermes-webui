"""Regression tests for issue #6460 and PR #8646.

Image-annotated user turn reconciliation with image attachment provenance.

The hermes-agent persists a user turn that carried a native image part by
flattening it to text and writing the ``[screenshot]`` annotation as a LINE OF
ITS OWN (``agent/session_persistence.py``::

    return "\\n".join(txt) if txt else None

with ``_image_marker`` returning ``[screenshot]`` / ``[screenshot detail=low]``
for each image part). The WebUI sidecar keeps the bare display text with no
generated annotation, but with attachment provenance: either in the WebUI
composer text suffix (``[Attached files: ...]``) or structured in the
``attachments`` list.

Universal scalar identity remains strictly literal: it collapses whitespace
without deleting marker-shaped lines. Reconciling agent-generated marker lines
with sidecar display text requires verified image provenance linking the turns
(matching timestamps, private identity compatibility, matching tools, and
two-way bucket uniqueness). Without provenance, ordinary scalar prose and
assistant code blocks are preserved literally.
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

# The display/sidecar form of an image turn: what the user sees.
DISPLAY_TEXT = "Here are my app settings\n\n[Attached files: /tmp/shot.jpg]"
# The state.db form of the SAME turn: workspace-prefixed plus the agent's
# durable projection of the image part, on its own line.
CONTEXT_TEXT = WORKSPACE_PREFIX + DISPLAY_TEXT + "\n[screenshot]"


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
# (a) The generated whole-line marker must reconcile with the display row.
# ---------------------------------------------------------------------------


def test_state_db_delta_does_not_reappend_image_turn_with_generated_marker():
    """The state.db projection of an image turn is NOT surfaced as a new turn.

    Registered symptom of #6460: the state.db copy carries the agent's
    ``[screenshot]`` annotation on its own line, the sidecar copy does not.
    Before the fix the keys differed and the whole turn came back as a delta,
    which the agent merged into the next user message.
    """
    from api.models import state_db_delta_after_context

    sidecar = [
        {"role": "user", "content": DISPLAY_TEXT, "timestamp": 1000.0},
        {"role": "assistant", "content": "Looks good", "timestamp": 1001.0},
    ]
    state = [
        {"role": "user", "content": CONTEXT_TEXT, "timestamp": 1000.0},
        {"role": "assistant", "content": "Looks good", "timestamp": 1001.0},
    ]

    assert state_db_delta_after_context(sidecar, state) == []


def test_state_db_delta_still_surfaces_next_text_only_turn():
    """Guard: the turn AFTER an image turn is still a real delta."""
    from api.models import state_db_delta_after_context

    sidecar = [
        {"role": "user", "content": DISPLAY_TEXT, "timestamp": 1000.0},
        {"role": "assistant", "content": "Looks good", "timestamp": 1001.0},
    ]
    state = [
        {"role": "user", "content": CONTEXT_TEXT, "timestamp": 1000.0},
        {"role": "assistant", "content": "Looks good", "timestamp": 1001.0},
        {
            "role": "user",
            "content": WORKSPACE_PREFIX + "Actually that toggle is off",
            "timestamp": 1002.0,
        },
        {"role": "assistant", "content": "Understood", "timestamp": 1003.0},
    ]

    delta = state_db_delta_after_context(sidecar, state)

    assert [m["content"] for m in delta] == [
        WORKSPACE_PREFIX + "Actually that toggle is off",
        "Understood",
    ]


def test_merge_keeps_the_image_turn_once_with_its_display_form():
    """The merged transcript keeps the sidecar row and no ctx twin."""
    from api.models import merge_session_messages_append_only

    sidecar = {
        "id": "webui-6460",
        "role": "user",
        "content": DISPLAY_TEXT,
        "attachments": ["/tmp/shot.jpg"],
        "timestamp": 1000.0,
    }
    state = {"role": "user", "content": CONTEXT_TEXT, "timestamp": 1000.0}

    merged = merge_session_messages_append_only([sidecar], [state])

    assert merged == [sidecar]
    assert merged[0] is sidecar


# ---------------------------------------------------------------------------
# (b) Literal prose containing the marker text is NOT the generated marker.
# ---------------------------------------------------------------------------


def test_literal_inline_and_multiline_markers_share_scalar_content_key_without_provenance():
    """Without image provenance, literal inline and multiline markers share scalar identity.

    Master semantics: universal scalar identity collapses whitespace with split(),
    so inline and multiline marker text key identically. Collision protection
    comes from image provenance linkage, not from unconditionally deleting
    marker shapes.
    """
    from api.models import _session_message_content_key

    inline = {"role": "user", "content": "type [screenshot] literally to annotate"}
    multiline = {
        "role": "user",
        "content": "type\n[screenshot]\nliterally to annotate",
    }

    assert _session_message_content_key(inline) == _session_message_content_key(multiline)


def test_literal_inline_marker_is_preserved_in_identity():
    """The marker text inside a prose line keeps contributing to the key."""
    from api.models import _session_message_content_key

    prose = {"role": "user", "content": "explain the [screenshot] token"}
    same_words_without_token = {"role": "user", "content": "explain the token"}

    assert _session_message_content_key(prose) != _session_message_content_key(
        same_words_without_token
    )


def test_literal_standalone_marker_without_provenance_is_never_collapsed():
    """Literal standalone marker without attachment provenance keeps both lines in delta."""
    from api.models import state_db_delta_after_context

    sidecar = [
        {"role": "user", "content": "alpha omega", "timestamp": 1000.0},
        {"role": "assistant", "content": "ok", "timestamp": 1001.0},
    ]
    state = [
        {"role": "user", "content": "alpha\n[screenshot]\nomega", "timestamp": 2000.0},
        {"role": "assistant", "content": "ok", "timestamp": 2001.0},
    ]
    assert len(state_db_delta_after_context(sidecar, state)) == 2


@pytest.mark.parametrize(
    "marker_body",
    [
        "alpha\n[screenshot detail=low]\nomega",
        "alpha\n[screenshot]\n[screenshot]\nomega",
    ],
)
def test_literal_detail_and_repeated_markers_without_provenance_are_never_collapsed(
    marker_body,
):
    """Literal detail or repeated markers without provenance are never collapsed."""
    from api.models import state_db_delta_after_context

    sidecar = [
        {"role": "user", "content": "alpha omega", "timestamp": 1000.0},
        {"role": "assistant", "content": "ok", "timestamp": 1001.0},
    ]
    state = [
        {"role": "user", "content": marker_body, "timestamp": 2000.0},
        {"role": "assistant", "content": "ok", "timestamp": 2001.0},
    ]
    assert len(state_db_delta_after_context(sidecar, state)) == 2


def test_assistant_code_block_with_screenshot_line_preserved_without_provenance():
    """Assistant code block with a [screenshot] line does not collapse with code lacking it."""
    from api.models import state_db_delta_after_context

    sidecar = [
        {"role": "user", "content": "show code", "timestamp": 1000.0},
        {"role": "assistant", "content": "```\ncode\n```", "timestamp": 1001.0},
    ]
    state = [
        {"role": "user", "content": WORKSPACE_PREFIX + "show code", "timestamp": 2000.0},
        {"role": "assistant", "content": "```\ncode\n[screenshot]\n```", "timestamp": 2001.0},
    ]
    assert len(state_db_delta_after_context(sidecar, state)) == 2


def test_merge_does_not_fold_literal_prose_into_a_marker_turn():
    """Two genuinely different user turns stay two rows."""
    from api.models import merge_session_messages_append_only

    sidecar = {
        "id": "webui-literal",
        "role": "user",
        "content": "alpha omega",
        "timestamp": 1000.0,
    }
    state = {
        "role": "user",
        "content": WORKSPACE_PREFIX + "alpha\n[screenshot]\nomega",
        "timestamp": 1000.0,
    }

    assert len(merge_session_messages_append_only([sidecar], [state])) == 2


@pytest.mark.parametrize(
    "marker",
    ["[screenshot]", "[screenshot detail=low]", "[screenshot detail=high]", "  [screenshot]  "],
)
def test_genuine_producer_composed_image_positive_control_reconciles(marker):
    """Genuine producer-composed image turn reconciles across all marker forms.

    Tests both WebUI composer representations:
    (a) Suffix format: '[Attached files: ...]'
    (b) Structured metadata: 'attachments: [...]'
    """
    from api.models import state_db_delta_after_context

    # Variant (a): WebUI composer suffix
    sidecar_suffix = [
        {
            "role": "user",
            "content": "what is wrong here?\n\n[Attached files: /tmp/shot.jpg]",
            "timestamp": 1000.0,
        },
        {"role": "assistant", "content": "Analyzing...", "timestamp": 1001.0},
    ]
    state_suffix = [
        {
            "role": "user",
            "content": (
                WORKSPACE_PREFIX
                + "what is wrong here?\n\n[Attached files: /tmp/shot.jpg]\n"
                + marker
            ),
            "timestamp": 1000.0,
        },
        {"role": "assistant", "content": "Analyzing...", "timestamp": 1001.0},
    ]
    assert state_db_delta_after_context(sidecar_suffix, state_suffix) == []

    # Variant (b): attachments list metadata
    sidecar_meta = [
        {
            "role": "user",
            "content": "what is wrong here?",
            "attachments": ["/tmp/shot.jpg"],
            "timestamp": 1000.0,
        },
        {"role": "assistant", "content": "Analyzing...", "timestamp": 1001.0},
    ]
    state_meta = [
        {
            "role": "user",
            "content": WORKSPACE_PREFIX + "what is wrong here?\n" + marker,
            "timestamp": 1000.0,
        },
        {"role": "assistant", "content": "Analyzing...", "timestamp": 1001.0},
    ]
    assert state_db_delta_after_context(sidecar_meta, state_meta) == []


@pytest.mark.parametrize(
    "prose_line",
    [
        "[screenshot] is the annotation the agent writes",
        "paste [screenshot] here",
        "[screenshots] are not the marker",
    ],
)
def test_a_prose_line_that_only_mentions_the_marker_is_literal(prose_line):
    """Only a line that IS the marker is an annotation -- never a prefix match."""
    from api.models import _session_message_content_key

    msg = {"role": "user", "content": WORKSPACE_PREFIX + prose_line}

    assert _session_message_content_key(msg) != _session_message_content_key(
        {"role": "user", "content": ""}
    )


# ---------------------------------------------------------------------------
# End-to-end: /api/session load must not duplicate the image turn.
# ---------------------------------------------------------------------------


def test_api_session_load_does_not_duplicate_image_turn(monkeypatch, tmp_path):
    """The registered symptom: a ghost image turn plus a merged follow-up."""
    import api.routes as routes

    sid = "webui_issue6460_screenshot"
    sidecar_messages = [
        {"role": "user", "content": DISPLAY_TEXT, "timestamp": 1000.0},
        {"role": "assistant", "content": "Looks good", "timestamp": 1001.0},
    ]
    state_rows = [
        {"role": "user", "content": CONTEXT_TEXT, "timestamp": 1000.0},
        {"role": "assistant", "content": "Looks good", "timestamp": 1001.0},
        {
            "role": "user",
            "content": WORKSPACE_PREFIX + "Actually that toggle is off",
            "timestamp": 1002.0,
        },
        {"role": "assistant", "content": "Understood", "timestamp": 1003.0},
    ]
    _install_test_session(monkeypatch, tmp_path, sid, sidecar_messages)
    _make_state_db(tmp_path / "state.db", sid, state_rows)

    handler = _GetHandler(f"/api/session?session_id={sid}&messages=1&resolve_model=0")
    routes.handle_get(handler, urlparse(handler.path))

    assert handler.status == 200
    messages = handler.response_json["session"]["messages"]
    contents = [str(m["content"]) for m in messages]

    # The image turn appears exactly once, in its display form.
    assert sum(1 for c in contents if "Here are my app settings" in c) == 1
    # And no state.db twin carrying the annotation leaks into the transcript.
    assert not any(c.endswith("[screenshot]") for c in contents)

    # The follow-up turn is not merged with the image turn.
    follow_up = [c for c in contents if "Actually that toggle is off" in c]
    assert len(follow_up) == 1
    assert "Here are my app settings" not in follow_up[0]
