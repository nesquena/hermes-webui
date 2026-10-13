"""Durable reasoning identity at server hydration; regressions from PR #6304."""

import copy
import json
import pytest


def test_anchor_scene_preserves_identical_reasoning_with_distinct_durable_ids():
    from api import routes

    identityless_prefix = "legacy reasoning " + ("x" * 90)
    identityless_extension = f"{identityless_prefix} with more detail"
    messages = [
        {"role": "user", "content": "question"},
        {"role": "assistant", "content": "Final answer"},
    ]
    records = {
        "record": {
            "message_index": 1,
            "message_ref": routes._assistant_anchor_scene_message_ref(messages[1]),
            "stream_id": "stream-1",
            "scene": {
                "version": "activity_scene_v1",
                "mode": "compact_worklog",
                "final_answer": "Final answer",
                "activity_rows": [
                    {
                        "row_id": "reasoning-row-a",
                        "event_id": "reasoning-event-a",
                        "local_id": "reasoning-local-a",
                        "role": "thinking",
                        "kind": "reasoning",
                        "source_event_type": "reasoning",
                        "status": "completed",
                        "text": "same reasoning",
                        "identity": {
                            "event_id": "reasoning-event-a",
                            "row_id": "reasoning-row-a",
                            "local_id": "reasoning-local-a",
                        },
                    },
                    {
                        "row_id": "hydrated:stream-1:prose:1:0",
                        "event_id": None,
                        "local_id": None,
                        "role": "prose",
                        "kind": "process_prose",
                        "source_event_type": "token",
                        "status": "completed",
                        "text": "same reasoning",
                        "identity": {
                            "event_id": None,
                            "row_id": None,
                            "local_id": None,
                        },
                    },
                    {
                        "row_id": "reasoning-row-b",
                        "event_id": "reasoning-event-b",
                        "local_id": "reasoning-local-b",
                        "role": "thinking",
                        "kind": "reasoning",
                        "source_event_type": "reasoning",
                        "status": "completed",
                        "text": "same reasoning",
                        "identity": {
                            "event_id": "reasoning-event-b",
                            "row_id": "reasoning-row-b",
                            "local_id": "reasoning-local-b",
                        },
                    },
                    {
                        "row_id": "reasoning-row-a-redelivery",
                        "event_id": "reasoning-event-a",
                        "local_id": "reasoning-local-a-redelivery",
                        "role": "thinking",
                        "kind": "reasoning",
                        "source_event_type": "reasoning",
                        "status": "completed",
                        "text": "same reasoning",
                        "identity": {"event_id": "reasoning-event-a"},
                    },
                    {
                        "row_id": "hydrated:stream-1:thinking:1:0",
                        "event_id": None,
                        "local_id": None,
                        "role": "thinking",
                        "kind": "reasoning",
                        "source_event_type": "reasoning",
                        "status": "completed",
                        "text": identityless_prefix,
                        "identity": {
                            "event_id": None,
                            "row_id": None,
                            "local_id": None,
                        },
                    },
                    {
                        "row_id": "hydrated:stream-1:thinking:1:1",
                        "event_id": None,
                        "local_id": None,
                        "role": "thinking",
                        "kind": "reasoning",
                        "source_event_type": "reasoning",
                        "status": "completed",
                        "text": identityless_extension,
                        "identity": {
                            "event_id": None,
                            "row_id": None,
                            "local_id": None,
                        },
                    },
                    {
                        "row_id": "hydrated:stream-1:thinking:1:2",
                        "event_id": None,
                        "local_id": None,
                        "role": "thinking",
                        "kind": "reasoning",
                        "source_event_type": "reasoning",
                        "status": "completed",
                        "text": identityless_prefix,
                        "identity": {
                            "event_id": None,
                            "row_id": None,
                            "local_id": None,
                        },
                    },
                    {
                        "row_id": "done",
                        "role": "terminal",
                        "kind": "terminal_status",
                        "source_event_type": "done",
                    },
                ],
            },
        }
    }

    hydrated = routes._hydrate_anchor_activity_scenes(messages, records)
    thinking_rows = [
        row
        for row in hydrated[1]["_anchor_activity_scene"]["activity_rows"]
        if row.get("role") == "thinking"
    ]
    textual_rows = [
        row
        for row in hydrated[1]["_anchor_activity_scene"]["activity_rows"]
        if row.get("role") in ("prose", "thinking")
    ]

    assert [row.get("text") for row in thinking_rows] == [
        "same reasoning",
        "same reasoning",
        identityless_prefix,
        identityless_extension,
    ]
    assert [row.get("event_id") for row in thinking_rows] == [
        "reasoning-event-a",
        "reasoning-event-b",
        None,
        None,
    ]
    assert [row.get("row_id") for row in thinking_rows[:2]] == [
        "reasoning-row-a",
        "reasoning-row-b",
    ]
    assert [(row.get("role"), row.get("text")) for row in textual_rows] == [
        ("thinking", "same reasoning"),
        ("prose", "same reasoning"),
        ("thinking", "same reasoning"),
        ("thinking", identityless_prefix),
        ("thinking", identityless_extension),
    ]


def _row(
    text,
    identity=None,
    *,
    field="event_id",
    location="row",
    role="thinking",
    status="completed",
):
    row = {
        "role": role,
        "kind": "reasoning" if role == "thinking" else "process_prose",
        "source_event_type": "reasoning" if role == "thinking" else "token",
        "text": text,
        "status": status,
        "stream_id": "stream-1",
    }
    if identity is not None:
        target = row if location == "row" else row.setdefault(location, {})
        target[field] = identity
    else:
        row["row_id"] = "hydrated:stream-1:thinking:1:0"
    return row


def _settle(rows, *, reasoning=None, messages=None, tool_calls=None):
    from api import routes

    messages = (
        copy.deepcopy(messages)
        if messages is not None
        else [
            {"role": "user", "content": "question"},
            {"role": "assistant", "content": "Final answer"},
        ]
    )
    if reasoning is not None:
        messages[-1]["reasoning_content"] = reasoning
    scene = {
        "version": "activity_scene_v1",
        "mode": "compact_worklog",
        "identity": {
            "session_id": "sid-1",
            "stream_id": "stream-1",
            "run_id": "stream-1",
        },
        "activity_rows": rows,
        "final_answer": "Final answer",
    }
    original = copy.deepcopy((messages, scene, tool_calls))
    record = {
        "message_index": len(messages) - 1,
        "stream_id": "stream-1",
        "scene": scene,
        "message_ref": routes._assistant_anchor_scene_message_ref(messages[-1]),
    }
    result = routes._hydrate_anchor_activity_scenes(
        messages, {record["message_ref"]: record}, tool_calls=tool_calls
    )[-1]["_anchor_activity_scene"]
    assert (messages, scene, tool_calls) == original, (
        "projection must not mutate the input transcript or scene"
    )
    return result


@pytest.mark.parametrize("field", ["event_id", "row_id", "local_id"])
@pytest.mark.parametrize("location", ["row", "identity", "payload"])
def test_distinct_ids_survive_equal_text_and_exact_redelivery(field, location):
    a = _row("Repeated reasoning", "segment-a", field=field, location=location)
    b = _row("Repeated reasoning", "segment-b", field=field, location=location)
    result = _settle([a, b, copy.deepcopy(a)])
    thoughts = [r for r in result["activity_rows"] if r["role"] == "thinking"]
    assert len(thoughts) == 2
    identities = [r if location == "row" else r[location] for r in thoughts]
    assert [r[field] for r in identities] == ["segment-a", "segment-b"]


@pytest.mark.parametrize("redeliver", [False, True])
def test_transcript_aggregate_keeps_live_segment_identities(redeliver):
    a = _row("Plan. ", "event-a", status="running")
    b = _row("Verify.", "event-b", status="running")
    result = _settle(
        [a, b] + ([copy.deepcopy(a)] if redeliver else []), reasoning="Plan. Verify."
    )
    thoughts = [r for r in result["activity_rows"] if r["role"] == "thinking"]
    assert [r.get("event_id") for r in thoughts] == ["event-a", "event-b"]
    assert [r["status"] for r in thoughts] == ["completed", "completed"]


def test_list_shaped_reasoning_reload_keeps_two_completed_saved_events():
    reasoning = [
        {"text": "Plan the change."},
        {"text": "Verify the result."},
    ]
    result = _settle(
        [
            _row("Plan the change.", "event-a", status="completed"),
            _row("Verify the result.", "event-b", status="completed"),
        ],
        reasoning=reasoning,
    )

    thoughts = [r for r in result["activity_rows"] if r["role"] == "thinking"]
    assert [(r.get("event_id"), r["text"], r["status"]) for r in thoughts] == [
        ("event-a", "Plan the change.", "completed"),
        ("event-b", "Verify the result.", "completed"),
    ]


def test_unmatched_completed_saved_reasoning_is_not_dropped():
    result = _settle(
        [_row("Saved completed reasoning", "event-a", status="completed")],
        reasoning="Different transcript reasoning",
    )

    thoughts = [r for r in result["activity_rows"] if r["role"] == "thinking"]
    assert any(
        r.get("event_id") == "event-a"
        and r["text"] == "Saved completed reasoning"
        and r["status"] == "completed"
        for r in thoughts
    )


@pytest.mark.parametrize("identity_first", [True, False])
def test_legacy_projection_dedupes_without_erasing_other_roles(identity_first):
    plain = _row("Same words")
    identified = _row("Same words", "event-a")
    rows = [identified, plain] if identity_first else [plain, identified]
    rows += [_row("Same words", role="prose"), _row("Same words", "event-b")]
    result = _settle(rows)
    assert [(r["role"], r.get("event_id")) for r in result["activity_rows"]] == [
        ("thinking", "event-a"),
        ("prose", None),
        ("thinking", "event-b"),
    ]


def test_repeated_settlement_and_json_reload_keep_distinct_ids():
    first = _settle([_row("Same", "event-a"), _row("Same", "event-b")])
    second = _settle(json.loads(json.dumps(first))["activity_rows"])
    assert [(r["text"], r.get("event_id")) for r in second["activity_rows"]] == [
        ("Same", "event-a"),
        ("Same", "event-b"),
    ]


@pytest.mark.parametrize("invalid_id", [{}, [], True])
def test_malformed_identity_does_not_merge_distinct_text(invalid_id):
    result = _settle(
        [_row("First observation", invalid_id), _row("Second observation", invalid_id)]
    )
    assert [r["text"] for r in result["activity_rows"]] == [
        "First observation",
        "Second observation",
    ]



def test_review_metadata_reasoning_wins_over_no_tool_thinking_content_part():
    messages = [
        {"role": "user", "content": "question"},
        {
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": "First thought"},
                {"type": "text", "text": "Final answer"},
            ],
            "reasoning_content": "Additional thought",
        },
    ]
    result = _settle(
        [_row("Additional thought", "event-metadata")],
        messages=messages,
    )
    thoughts = [r for r in result["activity_rows"] if r["role"] == "thinking"]
    assert [(r.get("event_id"), r["text"]) for r in thoughts] == [
        ("event-metadata", "Additional thought")
    ]



@pytest.mark.parametrize("legacy_first", [False, True])
@pytest.mark.parametrize("reasoning_shape", ["metadata", "content-parts"])
@pytest.mark.parametrize("status", ["running", "completed"])
def test_review_legacy_copy_does_not_invalidate_identified_reasoning(
    legacy_first, reasoning_shape, status
):
    identified = _row("Preserved reasoning", "event-a", status=status)
    legacy = _row("Preserved reasoning", status=status)
    rows = [legacy, identified] if legacy_first else [identified, legacy]
    messages = [
        {"role": "user", "content": "question"},
        {"role": "assistant", "content": "Final answer"},
    ]
    if reasoning_shape == "metadata":
        messages[-1]["reasoning_content"] = "Preserved reasoning"
    else:
        messages[-1]["content"] = [
            {"type": "thinking", "thinking": "Preserved reasoning"},
            {"type": "text", "text": "Final answer"},
        ]
    result = _settle(rows, messages=messages)
    assert [
        (r.get("event_id"), r["text"], r["status"])
        for r in result["activity_rows"]
        if r["role"] == "thinking"
    ] == [("event-a", "Preserved reasoning", "completed")]


@pytest.mark.parametrize("legacy_first", [False, True])
def test_review_legacy_copy_does_not_hide_two_equal_identified_events(legacy_first):
    rows = [_row("Repeat.", "event-a"), _row("Repeat.", "event-b")]
    rows.insert(0 if legacy_first else len(rows), _row("Repeat."))
    result = _settle(rows, reasoning="Repeat.Repeat.")
    assert [
        r.get("event_id") for r in result["activity_rows"] if r["role"] == "thinking"
    ] == ["event-a", "event-b"]


@pytest.mark.parametrize("source", ["tool_calls", "_partial_tool_calls", "external"])
def test_review_compatible_reasoning_keeps_transcript_tool_body_authoritative(source):
    from api import routes

    call = {
        "id": "tool-1",
        "name": "read_file",
        "args": {"path": "note.txt"},
        "output": "The complete transcript-owned tool output",
        "assistant_msg_idx": 1,
    }
    stale = routes._anchor_scene_tool_row({**call, "output": "old"}, 0, 1, "stream-1")
    messages = [
        {"role": "user", "content": "question"},
        {
            "role": "assistant",
            "content": "Final answer",
            "reasoning_content": "Reason.",
        },
    ]
    external = [call] if source == "external" else None
    if source != "external":
        messages[-1][source] = [call]
    result = _settle(
        [stale, _row("Reason.", "event-a")], messages=messages, tool_calls=external
    )
    tools = [r for r in result["activity_rows"] if r["role"] == "tool"]
    assert len(tools) == 1
    assert tools[0]["tool"]["snippet"] == call["output"]
    assert tools[0]["tool"]["output"] == call["output"]
    assert tools[0]["payload"]["snippet"] == call["output"]


def test_review_saved_scene_cannot_jump_before_earlier_transcript_activity():
    from api import routes

    call = {"id": "tool-1", "name": "read_file", "output": "transcript output"}
    stale = routes._anchor_scene_tool_row({**call, "output": "old"}, 0, 1, "stream-1")
    messages = [
        {"role": "user", "content": "question"},
        {
            "role": "assistant",
            "content": "Earlier transcript activity",
            "tool_calls": [call],
        },
        {
            "role": "assistant",
            "content": "Final answer",
            "reasoning_content": "Reason.",
        },
    ]
    result = _settle([stale, _row("Reason.", "event-a")], messages=messages)
    assert [r["role"] for r in result["activity_rows"]] == ["prose", "tool", "thinking"]
    assert result["activity_rows"][0]["text"] == "Earlier transcript activity"


def test_review_segmented_reasoning_stays_at_its_transcript_positions():
    messages = [
        {"role": "user", "content": "question"},
        {
            "role": "assistant",
            "content": "",
            "reasoning_content": "Before.",
            "tool_calls": [{"id": "tool-1", "name": "read_file", "output": "output"}],
        },
        {"role": "assistant", "content": "Final answer", "reasoning_content": "After."},
    ]
    result = _settle(
        [_row("Before.", "event-a"), _row("After.", "event-b")], messages=messages
    )
    assert [(r["role"], r.get("event_id")) for r in result["activity_rows"]] == [
        ("thinking", "event-a"),
        ("tool", None),
        ("thinking", "event-b"),
    ]


def test_review_content_part_thinking_keeps_tool_interleaving_and_full_body():
    from api import routes

    call = {"id": "tool-1", "name": "read_file", "output": "complete output"}
    messages = [
        {"role": "user", "content": "question"},
        {
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": "Before."},
                {"type": "tool_use", "id": "tool-1", "name": "read_file", "input": {}},
                {"type": "thinking", "thinking": "After."},
                {"type": "text", "text": "Final answer"},
            ],
            "tool_calls": [call],
        },
    ]
    stale = routes._anchor_scene_tool_row({**call, "output": "old"}, 0, 1, "stream-1")
    rows = [_row("Before.", "event-a"), _row("After.", "event-b"), stale]
    result = _settle(rows, messages=messages)
    assert [(r["role"], r.get("event_id")) for r in result["activity_rows"]] == [
        ("thinking", "event-a"),
        ("tool", None),
        ("thinking", "event-b"),
    ]
    assert result["activity_rows"][1]["tool"]["snippet"] == call["output"]
    reloaded = _settle(
        json.loads(json.dumps(result))["activity_rows"], messages=messages
    )
    assert reloaded["activity_rows"] == result["activity_rows"]


def test_unmatched_reasoning_slot_does_not_borrow_a_later_event_identity():
    messages = [
        {"role": "user", "content": "question"},
        {"role": "assistant", "content": "", "reasoning_content": "First."},
        {
            "role": "assistant",
            "content": "Final answer",
            "reasoning_content": "Second.",
        },
    ]
    # One saved event spans two distinct transcript slots. Do not move its
    # identity onto either transcript slot. A completed saved event that cannot
    # be matched stays visible as its own row instead of being discarded.
    result = _settle([_row("First.Second.", "spanning-event")], messages=messages)
    assert [
        (r.get("event_id"), r["text"], r["status"])
        for r in result["activity_rows"]
        if r["role"] == "thinking"
    ] == [
        (None, "First.", "completed"),
        (None, "Second.", "completed"),
        ("spanning-event", "First.Second.", "completed"),
    ]


@pytest.mark.parametrize("filtered_kind", ["visible-prose", "final-answer"])
@pytest.mark.parametrize("shape", ["metadata", "content-parts"])
def test_filtered_transcript_reasoning_does_not_consume_distinct_saved_event(filtered_kind, shape):
    repeated = "Visible progress" if filtered_kind == "visible-prose" else "Final answer"
    assistant = {"role": "assistant", "content": "Visible progress", "reasoning_content": repeated}
    if shape == "content-parts":
        assistant["content"] = [
            {"type": "thinking", "thinking": repeated},
            {"type": "text", "text": "Visible progress"},
        ]
    messages = [
        {"role": "user", "content": "question"},
        assistant,
        {"role": "assistant", "content": "Final answer"},
    ]
    result = _settle([_row("Distinct saved thought", "saved-event", status="running")], messages=messages)
    thinking = [r for r in result["activity_rows"] if r["role"] == "thinking"]
    assert [(r.get("event_id"), r["text"], r["status"]) for r in thinking] == [
        ("saved-event", "Distinct saved thought", "completed"),
    ]
    reloaded = _settle(json.loads(json.dumps(result))["activity_rows"], messages=messages)
    assert reloaded["activity_rows"] == result["activity_rows"]


def test_visible_content_part_reasoning_with_tools_keeps_its_saved_identity():
    messages = [
        {"role": "user", "content": "question"},
        {"role": "assistant", "content": [
            {"type": "thinking", "thinking": "Visible progress"},
            {"type": "text", "text": "Visible progress"},
            {"type": "tool_use", "id": "call-1", "name": "terminal", "input": {}},
        ]},
        {"role": "assistant", "content": "Final answer"},
    ]
    result = _settle([_row("Visible progress", "saved-event", status="running")], messages=messages)
    thinking = [row for row in result["activity_rows"] if row["role"] == "thinking"]
    assert [(row.get("event_id"), row["text"]) for row in thinking] == [("saved-event", "Visible progress")]


@pytest.mark.parametrize("metadata", [
    {},
    {"reasoning": "", "_reasoning": None, "reasoning_content": "", "thinking": ""},
    {"reasoning_content": []},
    {"_reasoning": {}},
    {"thinking": " \n "},
    {"reasoning": [{"text": ""}]},
], ids=["missing", "empty-scalars", "empty-list", "empty-object", "whitespace", "empty-part"])
@pytest.mark.parametrize("part_type", ["thinking", "reasoning"])
@pytest.mark.parametrize("tool_source", [None, "tool_calls", "_partial_tool_calls", "external"])
def test_no_tool_content_without_reasoning_metadata_preserves_distinct_saved_event(
    metadata, part_type, tool_source
):
    assistant = {
        "role": "assistant",
        "content": [
            {"type": part_type, part_type: "Transcript thought"},
            {"type": "text", "text": "Final answer"},
        ],
        **metadata,
    }
    call = {"id": "call-1", "name": "read_file", "output": "Transcript output", "assistant_msg_idx": 1}
    external = [call] if tool_source == "external" else None
    if tool_source in ("tool_calls", "_partial_tool_calls"):
        assistant[tool_source] = [call]
    messages = [{"role": "user", "content": "question"}, assistant]
    saved = _row("Distinct saved thought", "saved-event", status="running")
    saved["row_id"] = "saved-row"
    result = _settle([saved], messages=messages, tool_calls=external)
    thinking = [row for row in result["activity_rows"] if row["role"] == "thinking"]
    assert [(row.get("row_id"), row.get("event_id"), row["text"], row["status"]) for row in thinking] == [
        ("saved-row", "saved-event", "Distinct saved thought", "completed"),
    ]
    assert result["final_answer"].strip() == "Final answer"
    tools = [row for row in result["activity_rows"] if row["role"] == "tool"]
    assert [row["tool"]["output"] for row in tools] == (["Transcript output"] if tool_source else [])
    reloaded = _settle(json.loads(json.dumps(result))["activity_rows"], messages=messages, tool_calls=external)
    assert reloaded["activity_rows"] == result["activity_rows"]


def test_intermediate_no_tool_content_does_not_consume_later_metadata_reasoning_slot():
    messages = [
        {"role": "user", "content": "question"},
        {"role": "assistant", "content": [
            {"type": "thinking", "thinking": "Transcript thought"},
            {"type": "text", "text": "Visible progress"},
        ]},
        {"role": "assistant", "content": "Final answer", "reasoning_content": "Distinct saved thought"},
    ]
    result = _settle([_row("Distinct saved thought", "saved-event", status="running")], messages=messages)
    assert [(row["role"], row.get("event_id"), row["text"].strip()) for row in result["activity_rows"]] == [
        ("prose", None, "Visible progress"),
        ("thinking", "saved-event", "Distinct saved thought"),
    ]


@pytest.mark.parametrize("transcript_text,saved_text", [
    ("```python\nprint(42)\n```", "```python print(42) ```"),
    ("    print(42)\n    print(43)", "print(42)\n    print(43)"),
    ("- a\n- b\n\n1. c", "- a - b 1. c"),
], ids=["fenced-code", "indented-code", "lists"])
@pytest.mark.parametrize("role", ["prose", "thinking"])
@pytest.mark.parametrize("field,location", [
    ("event_id", "row"), ("event_id", "identity"),
    ("row_id", "row"), ("local_id", "payload"),
])
@pytest.mark.parametrize("tool_source", ["tool_calls", "content-tool", "external"])
def test_transcript_prose_owns_exact_markdown_when_inheriting_saved_identity(
    transcript_text, saved_text, role, field, location, tool_source
):
    from api import routes

    call = {"id": "call-1", "name": "read_file", "output": "Full transcript tool output", "assistant_msg_idx": 1}
    assistant = {"role": "assistant", "content": transcript_text if role == "prose" else "Inspecting the code"}
    if role == "thinking":
        assistant["reasoning_content"] = transcript_text
    if tool_source == "content-tool":
        assistant["content"] = [
            {"type": "text", "text": assistant["content"]},
            {"type": "tool_use", "id": "call-1", "name": "read_file", "input": {}, "output": call["output"]},
        ]
    elif tool_source == "tool_calls":
        assistant["tool_calls"] = [call]
    messages = [
        {"role": "user", "content": "question"}, assistant,
        {"role": "assistant", "content": "Final answer"},
    ]
    saved = _row(saved_text, "saved-identity", field=field, location=location, role=role, status="running")
    saved.setdefault("payload", {}).update({"text": saved_text, "saved_only": True})
    stale_tool = routes._anchor_scene_tool_row({**call, "output": "Stale preview"}, 0, 1, "stream-1")
    external = [call] if tool_source == "external" else None
    result = _settle([saved, stale_tool, copy.deepcopy(saved)], messages=messages, tool_calls=external)
    matched = [row for row in result["activity_rows"] if row["role"] == role]
    assert len(matched) == 1
    row = matched[0]
    assert row["text"] == (transcript_text if role == "prose" else saved_text)
    identity = row if location == "row" else row[location]
    assert identity[field] == "saved-identity"
    if role == "prose":
        assert row["payload"]["text"] == transcript_text
        assert "saved_only" not in row["payload"]
        assert row["source_event_type"] == "settled_message"
        assert row["group"]["assistant_msg_idx"] == 1
    assert row["status"] == "completed"
    tools = [row for row in result["activity_rows"] if row["role"] == "tool"]
    assert len(tools) == 1
    assert tools[0]["tool"]["output"] == call["output"]
    roles = [row["role"] for row in result["activity_rows"]]
    if role == "thinking" and tool_source == "content-tool":
        assert roles.index(role) > roles.index("tool")
    else:
        assert roles.index(role) < roles.index("tool")
    reloaded = _settle(json.loads(json.dumps(result))["activity_rows"], messages=messages, tool_calls=external)
    assert reloaded["activity_rows"] == result["activity_rows"]
