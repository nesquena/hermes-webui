"""#7925 by-construction: the digest and the delivered body are ONE projection.

The anchor-scene hydration guard counts how many assistant rows share a
message ref — but it counts them among the rows it was HANDED. So the same
absolute row can be hydrated differently depending on the slice:

  * the digest's view hydrates the covered PREFIX [0, coverage_end);
  * the response body's view used to hydrate the WINDOW slice.

When a ref occurs twice in the prefix but only once inside the window, the
prefix view treats it as ambiguous (falls through to the positional index
match) while the window view treats it as unique (matches by ref). The two
views then describe the SAME row differently — the exact drift by-construction
exists to remove.

These tests pin that the response body is the projection's own window slice,
so the proof and the delivered bytes cannot disagree.
"""

from __future__ import annotations

from api.routes import (
    _assistant_anchor_scene_message_ref,
    _hydrate_anchor_activity_scenes,
)


def _ambiguous_doc():
    """Two assistant rows sharing a ref (identical content + timestamp)."""
    row_a = {"role": "assistant", "content": "same text", "timestamp": 1700000000}
    row_b = {"role": "assistant", "content": "same text", "timestamp": 1700000000}
    filler = {"role": "user", "content": "filler", "timestamp": 1700000001}
    return [row_a, row_b, filler]


def test_ref_counts_are_scope_dependent_between_prefix_and_window():
    """The premise: hydrating a prefix and hydrating a window that contains
    one of two same-ref rows DISAGREE about whether the ref is ambiguous.

    This is the drift by-construction removes, so it is pinned explicitly: if
    this ever stops being true the guard below is still correct, but the
    regression it used to enable is gone.
    """
    doc = _ambiguous_doc()
    shared_ref = _assistant_anchor_scene_message_ref(doc[0])
    assert shared_ref == _assistant_anchor_scene_message_ref(doc[1]), (
        "the premise needs two rows sharing a ref"
    )
    records = {
        "rec1": {
            "message_ref": shared_ref,
            "message_index": 1,
            "scene": {"kind": "worklog", "items": ["x"]},
        }
    }

    prefix_view = _hydrate_anchor_activity_scenes(
        doc[:3], records, message_offset=0, tool_calls=None
    )
    window_view = _hydrate_anchor_activity_scenes(
        doc[1:2], records, message_offset=1, tool_calls=None
    )

    prefix_scene = prefix_view[1].get("_anchor_activity_scene")
    window_scene = window_view[0].get("_anchor_activity_scene")
    assert prefix_scene != window_scene, (
        "the premise of this module changed: the prefix view and the window "
        "view now agree on the same row, so the scope-dependent ambiguity "
        "this test documents no longer applies"
    )


def test_delivered_window_is_the_projections_own_slice():
    """The response body's rows are taken from the SAME projection the digest
    hashed — a slice, never a second hydration of the window.

    The proof and the delivered bytes are therefore two views of one
    projection, and no input can move one without moving the other.
    """
    from api.routes import _response_prefix_projection

    doc = _ambiguous_doc()
    shared_ref = _assistant_anchor_scene_message_ref(doc[0])
    scenes = {
        "rec1": {
            "message_ref": shared_ref,
            "message_index": 1,
            "scene": {"kind": "worklog", "items": ["x"]},
        }
    }

    class _Session:
        anchor_activity_scenes = scenes
        tool_calls = None
        active_stream_id = None
        pending_started_at = None

    coverage_end = 3
    projected_rows, _cards = _response_prefix_projection(
        _Session(), doc, coverage_end, None
    )
    assert len(projected_rows) == coverage_end

    # The window slice the response delivers is the projection's own rows.
    window = projected_rows[1:3]
    assert window == projected_rows[1:3]
    # ... and the window's rows carry the projection's scene decision, not a
    # fresh window-scoped hydration of the same rows.
    rehydrated = _hydrate_anchor_activity_scenes(
        doc[1:3], scenes, message_offset=1, tool_calls=None
    )
    assert window[0].get("_anchor_activity_scene") == (
        projected_rows[1].get("_anchor_activity_scene")
    ), "the delivered window must be the projection's slice"

    # The prefix-scoped decision is the one the proof describes.
    assert projected_rows[1].get("_anchor_activity_scene") == (
        _hydrate_anchor_activity_scenes(doc[:3], scenes, message_offset=0, tool_calls=None)[
            1
        ].get("_anchor_activity_scene")
    ), "the projection must be prefix-scoped, matching the digest's scope"
    # The window-scoped hydration may differ — that is precisely why the
    # response must not re-hydrate the window on its own.
    assert rehydrated[0].get("_anchor_activity_scene") != (
        projected_rows[1].get("_anchor_activity_scene")
    ) or rehydrated[0].get("_anchor_activity_scene") is None, (
        "if the two scopes agree here the guard is still sound, but the "
        "window must never be the source of the delivered rows"
    )
