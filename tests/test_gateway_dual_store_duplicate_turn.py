"""Regression: one turn held by BOTH stores rendered twice in messaging sessions.

Reported shape (gateway-backed browser chat, reproduced on a live deployment
running exp-v0.52.264)
----------------------------------------------------------------------------
With ``HERMES_WEBUI_CHAT_BACKEND=gateway`` the agent executes browser turns, so
one logical turn is written twice:

* the agent store gets the run's own row — no stable ``id``, its own timestamp,
  provider payloads such as ``codex_message_items``/``codex_reasoning_items``;
* ``_run_gateway_chat_streaming`` independently writes the WebUI sidecar row —
  a stable ``id`` from ``_assign_stable_message_ids`` and a timestamp a few
  hundred microseconds later.

``GET /api/session`` served that assistant answer as TWO adjacent rows while the
sidecar JSON on disk held exactly one, so inspecting the session file made the
bug look like a pure DOM-rendering artifact.

Root cause
----------
For a messaging session whose agent store holds more rows than the sidecar,
``_merged_session_messages_for_display`` takes the chronological-union branch
and dedupes only on ``_session_message_merge_key``. That key has two shapes: a
row carrying ``id``/``message_id`` keys as ``("message_id", id)``, an
unidentified row keys as ``("legacy", role, content, timestamp, ...)``. Two key
SHAPES never compare equal, so the identified sidecar copy and the
unidentified agent copy of one turn both survived the union.

Contract
--------
Identified rows stay authoritative: two rows that both carry ids are distinct
messages even when their text matches, because a user really can send the same
prompt twice (``test_session_endpoint_preserves_distinct_messages_with_different_ids``).
Only an UNIDENTIFIED row is reconciled away, and only while an identified row
with the same visible identity is still unmatched.

Review follow-up (same day)
----------------------------
A maintainer review of the first version of this fix found the reconciliation
above was discarding real data along with the duplicate row, and could
mis-pair metadata when more than one occurrence of the same visible key
existed:

1. Dropping the unidentified agent-store row copied only the narrow
   ``_SESSION_MESSAGE_DISPLAY_METADATA_KEYS`` allowlist onto the survivor.
   That allowlist never included ``reasoning``, ``reasoning_content``,
   ``reasoning_details``, ``codex_reasoning_items``, or
   ``codex_message_items`` — exactly the fields the agent-store copy is the
   sole owner of in the reported production shape. Fixed by
   ``_adopt_agent_semantic_payload`` (api/models.py): a second, narrow,
   explicitly-named allowlist, copied only when the survivor lacks the field
   (never overwriting an existing sidecar value), kept deliberately separate
   from the display-metadata allowlist so this reconciliation's semantics
   don't leak into the other call sites that share it. ``api_content`` is
   excluded from both allowlists on purpose — its identity rules elsewhere in
   this module are stricter for good reason.
2. ``identified_by_visible_key.setdefault(visible_key, msg)`` pointed every
   unidentified match for a visible key at whichever identified row was seen
   FIRST, so two occurrences of the same visible key (e.g. two identical
   assistant answers with distinct ids and distinct reasoning) could
   cross-wire metadata onto the wrong survivor. Fixed by replacing the single
   survivor reference with a per-visible-key FIFO queue built in transcript
   order; each unidentified match pops the oldest still-unmatched entry, so
   repeats pair up one-to-one in the order they actually occurred.

Second review round
-------------------
A follow-up review found the FIFO fix above was still gated behind the
merge-key suppression that runs before it. ``_session_message_merge_key``
rounds timestamps to whole seconds, so two unidentified rows with the same
role/content inside one wall-clock second share a key: the first consumed a
queued survivor, the second hit ``seen_message_keys`` and returned early
without ever reaching the queue, so its twin kept none of the agent-only
payload — and where no survivor was waiting at all, the row vanished from the
transcript outright.

Fixed by splitting duplicate suppression by store for unidentified rows.
Cross-store matching now runs first; only a row that finds no survivor falls
through to dedup, where full-precision ``_session_message_dedup_key`` decides
same-store duplicates (one store's own clock needs no rounding tolerance) and
the coarse second-granularity key only collapses a row against a kept row from
the OPPOSITE store — which is the case that key was introduced for: a legacy
both-unidentified turn written to both stores with sub-second drift between
the two writes.

Third review round
------------------
The survivor queues were keyed by visible identity alone, so an unidentified
row could consume an identified row from its OWN store. Neither input list is
homogeneous: ``sidecar_messages`` is the output of
``_webui_sidecar_lineage_messages_for_display`` and can mix id-stamped rows
with legacy ones after lineage stitching or an upgrade, and the Agent store
can hold identified rows too. Because ``_session_message_visible_key`` carries
role, content and ``tool_calls`` but neither timestamp nor store, two same-text
turns arbitrarily far apart in ONE store matched each other and the later one
was dropped as though it were a cross-store twin.

Fixed by partitioning the queues by ``(visible_key, from_sidecar)`` and
allowing a row to consume only a survivor from the opposite store. The two
lanes are not symmetric: display metadata is sidecar-authored so it transfers
on both, but the semantic payload flows only Agent -> sidecar. A
sidecar-authored ``reasoning`` is the unreliable one (it can hold a verbatim
copy of the reply, NousResearch/hermes-agent#13007), so it must not ride into
an Agent survivor under a helper whose documented policy is Agent authority;
that lane keeps the survivor's own values.

Fourth round (self-review)
--------------------------
Three further defects, found by probing this branch rather than by review:

1. The coarse-key collapse tracked only a single "already kept a twin here"
   flag per key, so ONE kept row could absorb an unbounded number of
   opposite-store rows -- deleting every additional real turn that shared the
   second. It now keeps a consumable per-store pool and collapses exactly one
   twin per kept row, the same one-to-one discipline as the identified lane.
2. The pairing key omits ``tool_call_id``/``tool_name`` entirely (only
   ``tool_calls`` is in it), so two results from DIFFERENT tool calls printing
   the same text -- ``OK``, ``{}``, ``Command completed.`` -- shared a visible
   key and one was deleted along with its transcript card. Pairing now
   requires the tool identity to agree. The underlying key is shared with
   other merge paths and is deliberately left alone.
3. The reconciliation wrote onto the caller's own dicts. ``session.messages``
   rows are shared via the module-level session cache, and ``Session.save()``
   rewrites the whole array from that in-memory object, so a plain ``GET`` --
   or a metadata-only sidebar poll -- could seed a payload that any later
   unrelated save committed to the sidecar JSON. Payload now lands on a copy
   and this function mutates nothing it was handed.

Known remaining limitation, NOT fixed here: ``_session_message_key_with_sidecar``
appends ``api_content`` to every key shape including the visible key, so a
gateway turn whose Agent copy carries provider bytes the sidecar copy lacks
gets two different visible keys and still renders twice. Narrowing that is a
change to shared identity machinery with its own stricter rules, out of scope
for this fix.
"""

from __future__ import annotations

import copy
from types import SimpleNamespace

import api.routes as routes


def _gateway_turn_session():
    """Sidecar rows exactly as the gateway chat worker writes them."""
    return SimpleNamespace(
        messages=[
            {"id": 7, "role": "user", "content": "show tomorrow's weather", "timestamp": 100.5},
            {"id": 8, "role": "assistant", "content": "Tomorrow: sunny, 35C.", "timestamp": 130.542},
        ]
    )


def _gateway_agent_rows():
    """Agent-store rows for the same turn: no stable id, earlier timestamps."""
    return [
        {"role": "user", "content": "show tomorrow's weather", "timestamp": 100.4},
        {"role": "assistant", "content": "", "timestamp": 110.0, "tool_calls": [{"id": "c1"}]},
        {"role": "tool", "content": "{\"temp\": 35}", "timestamp": 120.0, "tool_call_id": "c1"},
        {
            "role": "assistant",
            "content": "Tomorrow: sunny, 35C.",
            "timestamp": 130.424,
            "reasoning": "**Composing a concise weather summary**",
            "codex_message_items": [{"type": "message"}],
        },
    ]


def test_gateway_turn_present_in_both_stores_renders_once():
    session = _gateway_turn_session()
    cli_messages = _gateway_agent_rows()
    # The branch under test is only reached when the agent store is longer.
    assert len(cli_messages) > len(session.messages)

    merged = routes._merged_session_messages_for_display(session, cli_messages)

    answers = [m for m in merged if m.get("content") == "Tomorrow: sunny, 35C."]
    assert len(answers) == 1, "one gateway turn must not render as two assistant rows"
    prompts = [m for m in merged if m.get("content") == "show tomorrow's weather"]
    assert len(prompts) == 1, "one gateway turn must not render as two user rows"


def test_gateway_reconciliation_keeps_the_identified_sidecar_row():
    session = _gateway_turn_session()

    merged = routes._merged_session_messages_for_display(session, _gateway_agent_rows())

    # The WebUI-owned copy survives: the frontend slices fork/keep-counts
    # against this list and matches rows by their stable id.
    assert [m.get("id") for m in merged if m.get("id")] == [7, 8]


def test_gateway_reconciliation_preserves_agent_semantic_payload():
    """The discarded agent-store row is the sole owner of the real reasoning
    trace and Codex item lists in the reported production shape — dropping
    it must not silently drop that payload along with the duplicate row."""
    session = _gateway_turn_session()

    merged = routes._merged_session_messages_for_display(session, _gateway_agent_rows())

    survivor = next(m for m in merged if m.get("id") == 8)
    assert survivor["reasoning"] == "**Composing a concise weather summary**"
    assert survivor["codex_message_items"] == [{"type": "message"}]
    # api_content has its own stricter identity rules elsewhere; this
    # reconciliation must not fold it through the generic semantic-payload copy.
    assert "api_content" not in survivor


def test_gateway_reconciliation_keeps_agent_only_rows_and_order():
    session = _gateway_turn_session()

    merged = routes._merged_session_messages_for_display(session, _gateway_agent_rows())

    # Tool activity lives only in the agent store and must still merge through.
    assert [m.get("role") for m in merged] == ["user", "assistant", "tool", "assistant"]
    timestamps = [float(m.get("timestamp") or 0) for m in merged]
    assert timestamps == sorted(timestamps)


def test_identified_rows_with_matching_text_stay_distinct():
    """Two ids means two messages — a user can send the same prompt twice."""
    session = SimpleNamespace(
        messages=[{"id": "sidecar-retry", "role": "user", "content": "retry", "timestamp": 2.0}]
    )
    cli_messages = [
        {"role": "user", "content": "first", "timestamp": 1.0},
        {"id": "cli-retry", "role": "user", "content": "retry", "timestamp": 2.0},
    ]

    merged = routes._merged_session_messages_for_display(session, cli_messages)

    assert [m.get("id") for m in merged if m.get("content") == "retry"] == [
        "cli-retry",
        "sidecar-retry",
    ]


def test_repeated_identical_answers_pair_one_to_one_in_order():
    """Two occurrences of the same visible key must not cross-wire metadata.

    Two identical-text assistant answers, each with its own id and its own
    distinct agent-store reasoning, must each recover THEIR OWN reasoning —
    not both pointing at whichever identified row happened to be seen first.
    """
    session = SimpleNamespace(
        messages=[
            {"id": "a1", "role": "assistant", "content": "same answer", "timestamp": 10.5},
            {"id": "a2", "role": "assistant", "content": "same answer", "timestamp": 20.5},
        ]
    )
    cli_messages = [
        # An agent-only row makes the agent store strictly longer than the
        # sidecar, which is what routes this scenario into the
        # chronological-union branch under test rather than the sibling
        # append-only branch (len(sidecar) >= len(cli) takes that one instead).
        {"role": "user", "content": "ask again", "timestamp": 5.0},
        {"role": "assistant", "content": "same answer", "timestamp": 10.4, "reasoning": "first reasoning"},
        {"role": "assistant", "content": "same answer", "timestamp": 20.4, "reasoning": "second reasoning"},
    ]

    merged = routes._merged_session_messages_for_display(session, cli_messages)

    by_id = {m["id"]: m for m in merged if m.get("id")}
    assert set(by_id) == {"a1", "a2"}
    assert by_id["a1"]["reasoning"] == "first reasoning"
    assert by_id["a2"]["reasoning"] == "second reasoning"


def test_repeated_answers_inside_one_second_pair_one_to_one():
    """Repeats within a single wall-clock second must still pair one-to-one.

    ``_session_message_merge_key`` rounds timestamps to whole seconds, so two
    agent rows with the same role/content landing in one second share a merge
    key even though their fractional timestamps and payloads differ. Suppressing
    the second one on that key alone would leave its sidecar twin without the
    reasoning trace it is the sole owner of.
    """
    session = SimpleNamespace(
        messages=[
            {"id": "a1", "role": "assistant", "content": "same answer", "timestamp": 10.5},
            {"id": "a2", "role": "assistant", "content": "same answer", "timestamp": 10.8},
        ]
    )
    cli_messages = [
        {"role": "user", "content": "ask again", "timestamp": 5.0},
        {
            "role": "assistant",
            "content": "same answer",
            "timestamp": 10.4,
            "reasoning": "first reasoning",
            "codex_reasoning_items": [{"type": "reasoning", "n": 1}],
        },
        {
            "role": "assistant",
            "content": "same answer",
            "timestamp": 10.7,
            "reasoning": "second reasoning",
            "codex_reasoning_items": [{"type": "reasoning", "n": 2}],
        },
    ]

    merged = routes._merged_session_messages_for_display(session, cli_messages)

    answers = [m for m in merged if m.get("content") == "same answer"]
    assert [m.get("id") for m in answers] == ["a1", "a2"], "no duplicate visible row"
    by_id = {m["id"]: m for m in answers}
    assert by_id["a1"]["reasoning"] == "first reasoning"
    assert by_id["a2"]["reasoning"] == "second reasoning"
    assert by_id["a1"]["codex_reasoning_items"] == [{"type": "reasoning", "n": 1}]
    assert by_id["a2"]["codex_reasoning_items"] == [{"type": "reasoning", "n": 2}]


def test_surplus_unidentified_repeats_in_one_second_all_survive():
    """Unpaired same-second repeats from one store are distinct messages.

    When the agent store holds more copies of a text than the sidecar has
    identified rows to pair them with, the surplus rows have no cross-store
    twin — they are genuinely separate turns. Collapsing them on the
    second-granularity merge key would delete a real message outright, not
    merely its metadata.
    """
    session = SimpleNamespace(
        messages=[{"id": "z1", "role": "assistant", "content": "unrelated", "timestamp": 1.0}]
    )
    cli_messages = [
        {"role": "assistant", "content": "same answer", "timestamp": 10.2, "reasoning": "first"},
        {"role": "assistant", "content": "same answer", "timestamp": 10.8, "reasoning": "second"},
    ]

    merged = routes._merged_session_messages_for_display(session, cli_messages)

    surplus = [m for m in merged if m.get("content") == "same answer"]
    assert [m.get("reasoning") for m in surplus] == ["first", "second"]


def test_unidentified_sidecar_row_cannot_consume_a_sidecar_survivor():
    """Reconciliation is cross-store only; a store cannot dedupe against itself.

    ``sidecar_messages`` is the output of
    ``_webui_sidecar_lineage_messages_for_display``, so it can mix id-stamped
    rows with legacy ones after lineage stitching or an upgrade. Because
    ``_session_message_visible_key`` carries role/content/tool identity but
    neither timestamp nor store, an unidentified sidecar row would otherwise
    match an identified sidecar row from a completely different turn and be
    dropped as though it were an Agent twin.
    """
    session = SimpleNamespace(
        messages=[
            {"id": "s1", "role": "assistant", "content": "same", "timestamp": 10.5},
            {"role": "assistant", "content": "same", "timestamp": 20.5},
        ]
    )
    cli_messages = [
        {"role": "user", "content": "q1", "timestamp": 1.0},
        {"role": "user", "content": "q2", "timestamp": 2.0},
        {"role": "user", "content": "q3", "timestamp": 3.0},
    ]

    merged = routes._merged_session_messages_for_display(session, cli_messages)

    same = [m for m in merged if m.get("content") == "same"]
    assert [m.get("timestamp") for m in same] == [10.5, 20.5], "both turns survive"
    assert same[0].get("id") == "s1"


def test_unidentified_agent_row_cannot_consume_an_agent_survivor():
    """The symmetric case: the Agent store can also hold identified rows."""
    session = SimpleNamespace(
        messages=[{"id": "x9", "role": "user", "content": "unrelated", "timestamp": 0.5}]
    )
    cli_messages = [
        {"id": "a1", "role": "assistant", "content": "same", "timestamp": 10.5},
        {"role": "assistant", "content": "same", "timestamp": 20.5},
        {"role": "user", "content": "filler", "timestamp": 30.0},
    ]

    merged = routes._merged_session_messages_for_display(session, cli_messages)

    same = [m for m in merged if m.get("content") == "same"]
    assert [m.get("timestamp") for m in same] == [10.5, 20.5], "both turns survive"
    assert same[0].get("id") == "a1"


def test_semantic_payload_travels_only_from_agent_to_sidecar():
    """The reverse lane keeps the Agent survivor's own semantic values.

    When the identified survivor is the AGENT row and the row being dropped is
    its unidentified SIDECAR twin, display metadata still transfers (the
    sidecar authors it) but the semantic payload must not: a sidecar-authored
    ``reasoning`` is the unreliable one, so it cannot ride into an Agent
    survivor under a helper documenting Agent authority.
    """
    session = SimpleNamespace(
        messages=[
            {
                "role": "assistant",
                "content": "answer",
                "timestamp": 10.6,
                "reasoning": "sidecar reasoning that must not travel",
                "_turnDuration": 1234,
            }
        ]
    )
    cli_messages = [
        {"role": "user", "content": "the question", "timestamp": 1.0},
        {"id": "a1", "role": "assistant", "content": "answer", "timestamp": 10.4},
    ]

    merged = routes._merged_session_messages_for_display(session, cli_messages)

    answers = [m for m in merged if m.get("content") == "answer"]
    assert len(answers) == 1, "the cross-store twin still collapses"
    survivor = answers[0]
    assert survivor.get("id") == "a1", "the identified row survives"
    assert "reasoning" not in survivor, "semantic payload must not flow sidecar->agent"
    assert survivor.get("_turnDuration") == 1234, "display metadata still transfers"


def test_unidentified_cross_store_twin_still_collapses_within_one_second():
    """The coarse merge key's original job must survive this fix.

    Sessions predating stable-id stamping hold the same turn in both stores
    with neither copy identified and a few hundred microseconds of clock drift
    between the two writes. Second-level rounding is what collapses those into
    one row, so it still has to apply across stores — just not within one.
    """
    session = SimpleNamespace(
        messages=[{"role": "assistant", "content": "legacy answer", "timestamp": 10.9}]
    )
    cli_messages = [
        {"role": "user", "content": "the question", "timestamp": 1.0},
        {"role": "assistant", "content": "legacy answer", "timestamp": 10.4},
    ]

    merged = routes._merged_session_messages_for_display(session, cli_messages)

    assert [m.get("content") for m in merged].count("legacy answer") == 1


def test_conflicting_semantic_payload_keeps_survivors_own_value():
    """When BOTH sides already carry a non-empty value, the identified
    survivor's own value wins — agent-store data must not silently
    overwrite a value the sidecar copy already authored."""
    session = SimpleNamespace(
        messages=[
            {
                "id": 8,
                "role": "assistant",
                "content": "answer",
                "timestamp": 10.5,
                "reasoning": "sidecar's own reasoning",
            }
        ]
    )
    cli_messages = [
        # Agent-only filler row so len(cli) > len(sidecar) — routes this
        # scenario into the chronological-union branch under test rather than
        # the sibling append-only branch (len(sidecar) >= len(cli) takes that
        # one instead, which has its own, already-tested, conflict handling).
        {"role": "user", "content": "the question", "timestamp": 5.0},
        {
            "role": "assistant",
            "content": "answer",
            "timestamp": 10.4,
            "reasoning": "agent-store reasoning",
        },
    ]

    merged = routes._merged_session_messages_for_display(session, cli_messages)

    survivor = next(m for m in merged if m.get("id") == 8)
    assert survivor["reasoning"] == "sidecar's own reasoning"


def test_both_unidentified_twin_carries_payload_either_way():
    """A legacy cross-store twin must not lose the loser's payload.

    When neither copy has an id, which one survives the sort is decided by
    sub-second drift between the two stores' writes. Before this was handled,
    the outcome depended on that drift: the sidecar copy winning silently
    discarded the Agent copy's reasoning/Codex trace, and the Agent copy
    winning silently discarded the sidecar's display metadata. Both
    orderings must now yield the same content.
    """
    def merged_twin(sidecar_ts, agent_ts):
        session = SimpleNamespace(
            messages=[
                {
                    "role": "assistant",
                    "content": "legacy",
                    "timestamp": sidecar_ts,
                    "_turnDuration": 999,
                }
            ]
        )
        cli_messages = [
            {"role": "user", "content": "q", "timestamp": 1.0},
            {
                "role": "assistant",
                "content": "legacy",
                "timestamp": agent_ts,
                "reasoning": "real agent reasoning",
                "codex_message_items": [{"type": "message"}],
            },
        ]
        out = routes._merged_session_messages_for_display(session, cli_messages)
        rows = [m for m in out if m.get("content") == "legacy"]
        assert len(rows) == 1, "the twin still collapses to one row"
        return rows[0]

    for label, sidecar_ts, agent_ts in (
        ("sidecar wins the sort", 10.1, 10.4),
        ("agent wins the sort", 10.9, 10.4),
    ):
        row = merged_twin(sidecar_ts, agent_ts)
        assert row.get("reasoning") == "real agent reasoning", label
        assert row.get("codex_message_items") == [{"type": "message"}], label
        assert row.get("_turnDuration") == 999, label


def test_same_second_twin_collapse_is_one_to_one():
    """One kept row absorbs at most ONE opposite-store twin.

    The coarse key groups every same-second repeat of a text, so a single
    "already kept a twin here" flag lets one row swallow an unbounded number
    of opposite-store rows, deleting every additional real turn in that
    second. Which rows die depends only on sub-second write drift, so both
    orderings are checked.
    """
    def merged_reasonings(sidecar_ts):
        session = SimpleNamespace(
            messages=[
                {
                    "role": "assistant",
                    "content": "same",
                    "timestamp": sidecar_ts,
                    "_turnDuration": 7,
                }
            ]
        )
        cli_messages = [
            {"role": "user", "content": "q", "timestamp": 1.0},
            {"role": "assistant", "content": "same", "timestamp": 10.3, "reasoning": "r1"},
            {"role": "assistant", "content": "same", "timestamp": 10.6, "reasoning": "r2"},
        ]
        out = routes._merged_session_messages_for_display(session, cli_messages)
        return [m.get("reasoning") for m in out if m.get("content") == "same"]

    # Two real Agent turns plus one sidecar twin => two rows either way.
    assert merged_reasonings(10.1) == ["r1", "r2"], "sidecar twin sorts first"
    assert merged_reasonings(10.9) == ["r1", "r2"], "sidecar twin sorts last"


def test_surplus_same_second_twins_keep_every_real_turn():
    """Three Agent turns in one second, one sidecar twin => three rows."""
    session = SimpleNamespace(
        messages=[{"role": "assistant", "content": "same", "timestamp": 10.1}]
    )
    cli_messages = [
        {"role": "user", "content": "q", "timestamp": 1.0},
        {"role": "assistant", "content": "same", "timestamp": 10.3, "reasoning": "r1"},
        {"role": "assistant", "content": "same", "timestamp": 10.5, "reasoning": "r2"},
        {"role": "assistant", "content": "same", "timestamp": 10.7, "reasoning": "r3"},
    ]

    merged = routes._merged_session_messages_for_display(session, cli_messages)

    assert [
        m.get("reasoning") for m in merged if m.get("content") == "same"
    ] == ["r1", "r2", "r3"]


def test_same_second_twins_pair_metadata_one_to_one():
    """Two real turns, each with its own twin, keep their own metadata."""
    session = SimpleNamespace(
        messages=[
            {"role": "assistant", "content": "same", "timestamp": 10.1, "_turnDuration": 111},
            {"role": "assistant", "content": "same", "timestamp": 10.2, "_turnDuration": 222},
        ]
    )
    cli_messages = [
        {"role": "user", "content": "q", "timestamp": 1.0},
        {"role": "assistant", "content": "same", "timestamp": 10.3, "reasoning": "r1"},
        {"role": "assistant", "content": "same", "timestamp": 10.4, "reasoning": "r2"},
    ]

    merged = routes._merged_session_messages_for_display(session, cli_messages)

    rows = [m for m in merged if m.get("content") == "same"]
    assert [(m.get("_turnDuration"), m.get("reasoning")) for m in rows] == [
        (111, "r1"),
        (222, "r2"),
    ]


def test_different_tool_calls_with_identical_output_stay_distinct():
    """The pairing key omits tool identity, so guard it explicitly.

    ``_session_message_visible_key`` carries role, content and ``tool_calls``
    but NOT ``tool_call_id``/``tool_name``. Two results from different tool
    calls that print the same text -- ``OK``, ``{}``, ``Command completed.``
    -- therefore share a visible key, and reconciling them deletes a real
    tool result along with its card in the transcript.
    """
    session = SimpleNamespace(
        messages=[
            {
                "id": "s-A",
                "role": "tool",
                "content": '{"ok":true}',
                "timestamp": 10.5,
                "tool_call_id": "callA",
                "tool_name": "read_file",
            }
        ]
    )
    cli_messages = [
        {"role": "user", "content": "q", "timestamp": 1.0},
        {
            "role": "tool",
            "content": '{"ok":true}',
            "timestamp": 20.4,
            "tool_call_id": "callB",
            "tool_name": "write_file",
        },
    ]

    merged = routes._merged_session_messages_for_display(session, cli_messages)

    tool_rows = [m for m in merged if m.get("role") == "tool"]
    assert [m.get("tool_call_id") for m in tool_rows] == ["callA", "callB"]


def test_display_merge_does_not_mutate_its_inputs():
    """Reconciliation must not durably edit the caller's transcript rows.

    ``session.messages`` rows are shared -- they live in the module-level
    session cache, and ``Session.save()`` rewrites the whole array from that
    in-memory object. Mutating here would let a plain ``GET`` (or a
    metadata-only sidebar poll) seed a payload that any later unrelated save
    commits to the sidecar JSON on disk.
    """
    session = _gateway_turn_session()
    cli_messages = _gateway_agent_rows()
    sidecar_before = copy.deepcopy(session.messages)
    cli_before = copy.deepcopy(cli_messages)

    merged = routes._merged_session_messages_for_display(session, cli_messages)

    assert session.messages == sidecar_before, "sidecar rows must be untouched"
    assert cli_messages == cli_before, "agent rows must be untouched"
    # The payload still reaches the returned transcript -- on a copy.
    survivor = next(m for m in merged if m.get("id") == 8)
    assert survivor["reasoning"] == "**Composing a concise weather summary**"
    assert survivor is not session.messages[1]


def test_display_merge_is_idempotent():
    """Re-running the merge on the same session must not change the result.

    The reconciliation mutates the surviving row in place, so a second call
    sees fields the first call adopted. Both helpers are fill-only-if-absent,
    which is what makes that safe -- pin it.
    """
    session = _gateway_turn_session()
    cli_messages = _gateway_agent_rows()

    first = routes._merged_session_messages_for_display(session, cli_messages)
    snapshot = [dict(m) for m in first]
    second = routes._merged_session_messages_for_display(session, cli_messages)

    assert [dict(m) for m in second] == snapshot


def test_unidentified_repeats_within_one_store_are_not_collapsed():
    """Without any id there is no cross-store twin to reconcile against."""
    session = SimpleNamespace(
        messages=[{"role": "user", "content": "ping", "timestamp": 5.0}]
    )
    cli_messages = [
        {"role": "user", "content": "ping", "timestamp": 1.0},
        {"role": "assistant", "content": "pong", "timestamp": 2.0},
        {"role": "user", "content": "ping", "timestamp": 3.0},
    ]

    merged = routes._merged_session_messages_for_display(session, cli_messages)

    assert [m.get("content") for m in merged].count("ping") == 3
