"""Regression test for the state.db content sentinel leaking into the UI.

hermes_state stores list/dict message content as a NUL-sentinel JSON string.
When the WebUI projector failed to decode it, an uploaded image's base64 data
URI rendered as literal text -- one unbreakable ~65k-character run -- and the
browser spent minutes computing its min-content width.
"""
import json

from api.models import _decode_state_db_content, _project_state_db_message

PREFIX = "\x00json:"


def test_sentinel_encoded_list_content_is_decoded():
    parts = [
        {"type": "text", "text": "here is a screenshot"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
    ]
    assert _decode_state_db_content(PREFIX + json.dumps(parts)) == parts


def test_sentinel_encoded_dict_content_is_decoded():
    assert _decode_state_db_content(PREFIX + json.dumps({"a": 1})) == {"a": 1}


def test_plain_values_pass_through_unchanged():
    for value in ("hello", "", None, 42, ["already", "a", "list"], "see json: below"):
        assert _decode_state_db_content(value) == value


def test_malformed_sentinel_payload_returns_raw_string():
    broken = PREFIX + "{not valid json"
    assert _decode_state_db_content(broken) == broken


def test_projector_decodes_content():
    parts = [{"type": "text", "text": "hi"}]
    row = {
        "role": "user",
        "content": PREFIX + json.dumps(parts),
        "timestamp": 1.0,
        "id": 1,
    }
    msg = _project_state_db_message(row, available=set(), id_col=False, optional=())
    assert msg["content"] == parts
