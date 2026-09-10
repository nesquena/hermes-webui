"""Regression tests for the state.db structured-content sentinel.

hermes_state stores list/dict message content as a NUL-sentinel JSON string.
While the WebUI projector left it undecoded, an uploaded image's base64 data
URI reached the transcript as literal text -- one unbreakable ~65k-character
run -- and the browser spent minutes computing its min-content width.

The decode is deliberately narrow: it must not widen ``content`` into any
shape the rest of the WebUI pipeline cannot already render.
"""
import json

from api.models import (
    _content_identity_for_key,
    _decode_state_db_content,
    _project_state_db_message,
    _session_message_dedup_key,
    _session_message_merge_key,
    _session_message_multimodal_mirror_key,
    _session_message_visible_key,
)

PREFIX = "\x00json:"
TEXT_AND_IMAGE = [
    {"type": "text", "text": "here is a screenshot"},
    {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
]


def _sentinel(payload_json: str) -> str:
    return PREFIX + payload_json


# --- the fix itself -------------------------------------------------------

def test_supported_list_root_is_decoded():
    assert _decode_state_db_content(_sentinel(json.dumps(TEXT_AND_IMAGE))) == TEXT_AND_IMAGE


def test_projector_decodes_content():
    row = {"role": "user", "content": _sentinel(json.dumps(TEXT_AND_IMAGE)),
           "timestamp": 1.0, "id": 1}
    msg = _project_state_db_message(row, available=set(), id_col=False, optional=())
    assert msg["content"] == TEXT_AND_IMAGE


# --- finding #1: dict roots must NOT be widened ---------------------------

def test_dict_root_falls_back_to_raw_string():
    """A dict root reaches _renderCacheKey(), which calls text.slice() on it
    and throws, blanking the turn. It must stay a string."""
    raw = _sentinel(json.dumps({"type": "text", "text": "hi"}))
    assert _decode_state_db_content(raw) == raw


def test_scalar_roots_fall_back_to_raw_string():
    for payload in ("42", '"just a string"', "true", "null"):
        raw = _sentinel(payload)
        assert _decode_state_db_content(raw) == raw


# --- finding #2: non-finite numbers break browser JSON.parse --------------

def test_non_finite_constants_fall_back_to_raw_string():
    for literal in ("NaN", "Infinity", "-Infinity"):
        raw = _sentinel('[{"type": "text", "text": 1}, %s]' % literal)
        assert _decode_state_db_content(raw) == raw


def test_overflowed_float_falls_back_to_raw_string():
    raw = _sentinel('[{"type": "text", "text": "x", "score": 1e400}]')
    assert _decode_state_db_content(raw) == raw


def test_decoded_payload_is_always_browser_parseable():
    decoded = _decode_state_db_content(_sentinel(json.dumps(TEXT_AND_IMAGE)))
    json.loads(json.dumps(decoded, allow_nan=False))


# --- finding #3: only the schema the UI actually renders ------------------

def test_unsupported_part_shapes_fall_back_to_raw_string():
    unsupported = [
        [{"type": "input_text", "text": "dropped by the JS readers"}],
        [{"type": "output_text", "text": "also dropped"}],
        [{"type": "tool_use", "id": "t1"}],
        ["a bare scalar part"],
        [{"text": "no type key"}],
        [{"type": "text", "text": {"not": "a string"}}],
        [],
    ]
    for payload in unsupported:
        raw = _sentinel(json.dumps(payload))
        assert _decode_state_db_content(raw) == raw, payload


def test_image_only_list_is_supported():
    payload = [{"type": "image_url", "image_url": {"url": "data:image/png;base64,AA"}}]
    assert _decode_state_db_content(_sentinel(json.dumps(payload))) == payload


# --- passthrough / malformed ---------------------------------------------

def test_plain_values_pass_through_unchanged():
    for value in ("hello", "", None, 42, ["already", "a", "list"], "see json: below"):
        assert _decode_state_db_content(value) == value


def test_malformed_sentinel_payload_returns_raw_string():
    raw = _sentinel("{not valid json")
    assert _decode_state_db_content(raw) == raw


# --- finding #4: identities must be type-namespaced ----------------------

def test_structured_content_cannot_collide_with_its_own_repr():
    structured = {"role": "user", "content": TEXT_AND_IMAGE, "timestamp": 1.0}
    scalar = {"role": "user", "content": str(TEXT_AND_IMAGE), "timestamp": 1.0}
    assert _session_message_merge_key(structured) != _session_message_merge_key(scalar)
    assert _session_message_dedup_key(structured) != _session_message_dedup_key(scalar)


def test_rich_turns_with_different_images_keep_distinct_identities():
    def turn(url):
        return {"role": "user", "timestamp": 1.0, "content": [
            {"type": "text", "text": "same visible text"},
            {"type": "image_url", "image_url": {"url": url}},
        ]}
    a, b = turn("data:image/png;base64,AAAA"), turn("data:image/png;base64,BBBB")
    assert _session_message_merge_key(a) != _session_message_merge_key(b)
    assert _session_message_dedup_key(a) != _session_message_dedup_key(b)


def test_scalar_content_identity_is_unchanged():
    """Existing scalar behaviour must not shift."""
    assert _content_identity_for_key("plain text") == "plain text"
    assert _content_identity_for_key(None) == ""
    assert _content_identity_for_key("") == ""


def test_mirror_bridge_never_pairs_rich_to_rich():
    rich = {"role": "user", "timestamp": 1.0, "content": TEXT_AND_IMAGE}
    assert _session_message_multimodal_mirror_key(rich, require_image_parts=True) is not None
    # the scalar side of the bridge must refuse a rich row
    assert _session_message_multimodal_mirror_key(rich, require_scalar_mirror=True) is None
    # and the two flags are mutually exclusive by construction
    assert _session_message_multimodal_mirror_key(
        rich, require_image_parts=True, require_scalar_mirror=True
    ) is None


def test_mirror_bridge_still_accepts_a_scalar_mirror():
    scalar = {"role": "user", "timestamp": 1.0, "content": "[screenshot] here is a screenshot"}
    assert _session_message_multimodal_mirror_key(scalar, require_scalar_mirror=True) is not None


# --- finding #5: prefix and tail keys share one representation -----------

def test_prefix_and_tail_keys_agree_for_a_sentinel_row():
    raw = _sentinel(json.dumps(TEXT_AND_IMAGE))
    row = {"role": "user", "content": raw, "timestamp": 5.0, "id": 7}
    tail_msg = _project_state_db_message(row, available=set(), id_col=False, optional=())
    prefix_msg = {
        "role": row["role"],
        "content": _decode_state_db_content(row["content"]),
        "tool_calls": None,
        "api_content": None,
    }
    tail_key = _session_message_visible_key(
        {"role": tail_msg.get("role"), "content": tail_msg.get("content"),
         "tool_calls": None, "api_content": None},
        normalize_workspace_prefix=True,
    )
    prefix_key = _session_message_visible_key(prefix_msg, normalize_workspace_prefix=True)
    assert prefix_key == tail_key


# --- every read path that projects content must decode (behavioural) ------
#
# Keys derived on one read path are compared against keys derived on another,
# so a read path that projected the column raw while another decoded it would
# silently reintroduce the prefix/tail mismatch. These exercise each path
# against a real sentinel-encoded row rather than inspecting source.

def _make_state_db(path, sid, rows):
    import sqlite3
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, title TEXT, "
        "model TEXT, started_at REAL, message_count INTEGER)"
    )
    conn.execute(
        "CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, "
        "role TEXT, content TEXT, timestamp REAL, tool_call_id TEXT, tool_calls TEXT, "
        "tool_name TEXT, active INTEGER DEFAULT 1, api_content TEXT)"
    )
    conn.execute(
        "INSERT INTO sessions (id, source, title, model, started_at, message_count) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (sid, "webui", "Sentinel", "test-model", 1000.0, len(rows)),
    )
    for row in rows:
        conn.execute(
            "INSERT INTO messages (session_id, role, content, timestamp, active) "
            "VALUES (?, ?, ?, ?, 1)",
            (sid, row["role"], row["content"], row["timestamp"]),
        )
    conn.commit()
    conn.close()


def _sentinel_session(tmp_path, monkeypatch):
    """A session whose first row carries sentinel-encoded multimodal content."""
    from api import models

    sid = "sentineltest"
    db = tmp_path / "state.db"
    _make_state_db(db, sid, [
        {"role": "user", "content": _sentinel(json.dumps(TEXT_AND_IMAGE)), "timestamp": 1000.0},
        {"role": "assistant", "content": "plain reply", "timestamp": 2000.0},
    ])
    monkeypatch.setattr(models, "_active_state_db_path", lambda: db, raising=False)
    return models, sid


def test_transcript_read_decodes_sentinel_rows(tmp_path, monkeypatch):
    """The canonical projection hands the frontend structured content."""
    models, sid = _sentinel_session(tmp_path, monkeypatch)
    messages = models.get_state_db_session_messages(sid)
    assert messages, "expected the fixture session to load"
    assert messages[0]["content"] == TEXT_AND_IMAGE
    # the base64 payload must not survive as literal transcript text
    assert not isinstance(messages[0]["content"], str)


def test_regeneration_prefix_and_tail_keys_agree_for_a_sentinel_row(tmp_path, monkeypatch):
    """The same row keyed as prefix and as tail must produce one identity.

    Decoding the projected tail while leaving prefix keys encoded would make
    `_bounded_tail_snapshot_if_safe` reject the bounded path and re-read the
    whole transcript, and could hide a genuine repeated recovered turn.
    """
    models, sid = _sentinel_session(tmp_path, monkeypatch)

    # floor above the sentinel row -> it is part of the prefix proof
    as_prefix = models.get_state_db_regeneration_tail_snapshot(sid, 1500.0)
    # floor below it -> the same row is part of the bounded tail
    as_tail = models.get_state_db_regeneration_tail_snapshot(sid, 500.0)
    assert as_prefix is not None and as_tail is not None

    assert as_prefix["prefix_keys"], "sentinel row should sit in the prefix"
    assert as_tail["tail_keys"], "sentinel row should sit in the tail"
    assert as_prefix["prefix_keys"][0] == as_tail["tail_keys"][0]


def test_bounded_prefix_reader_agrees_with_the_projected_tail(tmp_path, monkeypatch):
    """The standalone prefix-key reader shares the tail's representation."""
    models, sid = _sentinel_session(tmp_path, monkeypatch)

    prefix_keys = models.get_state_db_session_message_keys_before_timestamp(sid, 1500.0)
    tail = models.get_state_db_regeneration_tail_snapshot(sid, 500.0)
    assert prefix_keys, "expected a prefix key for the sentinel row"
    assert tail is not None and tail["tail_keys"]
    assert prefix_keys[0] == tail["tail_keys"][0]


def test_unsupported_sentinel_shape_survives_the_read_path_as_text(tmp_path, monkeypatch):
    """A shape the UI cannot render stays a string end-to-end, not silently dropped."""
    from api import models

    sid = "unsupported"
    db = tmp_path / "state.db"
    raw = _sentinel(json.dumps({"type": "text", "text": "dict root"}))
    _make_state_db(db, sid, [{"role": "user", "content": raw, "timestamp": 1000.0}])
    monkeypatch.setattr(models, "_active_state_db_path", lambda: db, raising=False)

    messages = models.get_state_db_session_messages(sid)
    assert messages
    assert messages[0]["content"] == raw
