"""#7754: a marked native-image state row that replays its sidecar row must consume that replay slot.

Before the fix, the row-id fast path for a marked native-image mirror row `continue`d without
advancing the replay index. The edited checkpoint then never read as consumed, and a rerun reply
that exists only in state.db was truncated. Master (no marker branch) keeps that reply.
"""

import api.models as models


def _merge(*, marked):
    field = models._WEBUI_UNMATCHED_NATIVE_IMAGE_MIRROR_FIELD
    image_row = {
        "role": "user",
        "content": "look",
        "timestamp": 860.0,
        "_state_db_row_id": 42,
        "api_content": "PAYLOAD-A",
    }
    sidecar = [
        {"role": "user", "content": "u1", "timestamp": 100.0},
        {"role": "assistant", "content": "a1", "timestamp": 101.0},
        dict(image_row),
        {"role": "assistant", "content": "a_img", "timestamp": 861.0},
        {"role": "user", "content": "uE", "timestamp": 900.0},
    ]
    state_image_row = dict(image_row)
    if marked:
        state_image_row[field] = True
    state = [
        {"role": "user", "content": "u1", "timestamp": 100.0},
        {"role": "assistant", "content": "a1", "timestamp": 101.0},
        state_image_row,
        {"role": "assistant", "content": "a_img", "timestamp": 861.0},
        {"role": "user", "content": "u_old", "timestamp": 870.0},
        {"role": "assistant", "content": "a_old", "timestamp": 871.0},
        {"role": "user", "content": "uE", "timestamp": 900.0},
        {"role": "assistant", "content": "aE", "timestamp": 901.0},
    ]
    return models.merge_session_messages_append_only(
        sidecar,
        state,
        truncation_watermark=900.0,
        truncation_boundary=861.0,
        incoming_provenance="state_db",
    )


def _contents(rows):
    return [row.get("content") for row in rows]


def test_marked_image_row_keeps_state_only_reply_after_edited_checkpoint():
    merged = _merge(marked=True)
    assert _contents(merged) == ["u1", "a1", "look", "a_img", "uE", "aE"]


def test_marked_and_unmarked_image_rows_merge_identically():
    assert _contents(_merge(marked=True)) == _contents(_merge(marked=False))


def test_removed_rows_between_boundary_and_watermark_stay_out():
    contents = _contents(_merge(marked=True))
    assert "u_old" not in contents
    assert "a_old" not in contents
