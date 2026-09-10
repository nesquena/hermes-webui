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


# --- guard: every path that projects the content column must decode --------

def test_all_content_projecting_read_paths_decode():
    """The decode contract documented in docs/architecture/agent-api-contract.md.

    Keys derived on one read path are compared against keys derived on another,
    so a new raw projection of the ``content`` column would silently reintroduce
    the prefix/tail mismatch. Pin the known call sites.
    """
    import inspect
    import re

    from api import models

    expected = {
        "_project_state_db_message",
        "get_state_db_session_message_keys_before_timestamp",
        "get_state_db_regeneration_tail_snapshot",
    }
    source = inspect.getsource(models)
    found = set()
    current = None
    for line in source.splitlines():
        match = re.match(r"^def (\w+)", line)
        if match:
            current = match.group(1)
        if "_decode_state_db_content(" in line and "def " not in line and current:
            found.add(current)
    assert found == expected, f"decode call sites drifted: {found ^ expected}"
