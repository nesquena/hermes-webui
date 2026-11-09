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

Fifth review round
-------------------
The prior round flagged that ``_session_message_key_with_sidecar`` appends
``api_content`` to every key shape including the visible key used for
pairing, so a gateway turn whose Agent copy carries provider bytes the
sidecar copy lacks got two different visible keys and rendered twice. Per the
reviewer's own guidance: do NOT widen the shared ``_session_message_visible_key``
globally (other merge paths rely on its stricter, ``api_content``-aware
identity there). Instead this reconciliation now builds a LOCAL candidate key,
``_cross_store_pairing_key``, that keeps role, normalized visible content and
``tool_calls`` but omits ``api_content`` -- tool identity (``tool_call_id``/
``tool_name``) is still enforced separately via ``_cross_store_pairable``, as
before. Once a twin is matched one-to-one on that narrower identity,
``api_content`` gets its own directional authority through the existing
``_copy_api_content_sidecar`` helper (Agent -> sidecar only, same lane as the
semantic payload) rather than a generic dict merge.

Fifth review round
------------------
Rounds 1-4 all reasoned about what a matched pair should EXCHANGE. This one
asked the prior question: what proves the two rows are the same turn at all?
Nothing did. ``_cross_store_pairing_key`` compares role, visible content and
``tool_calls``, and ``_cross_store_pairable`` adds tool identity -- so two
genuinely different turns that answer with the same text matched, and the
popped row was discarded with its own ``api_content`` and its Agent-owned
``reasoning``, silently. ``_message_private_identity_compatible`` is the
guard this module already uses for exactly that question (contradictory
stable id, state.db row alias, or ``api_content`` means "not one message"),
so ``_cross_store_pairable`` now consults it before any pairing.

This CHANGES the round-4 conflicting-``api_content`` contract: that case no
longer pairs-and-fails-closed, it does not pair at all and both turns
survive (``test_conflicting_api_content_rows_are_not_paired_at_all``). The
common production shape is untouched -- the sidecar copy has no
``api_content`` at all, so there is nothing to contradict and pairing
proceeds exactly as in round 4.

Sequence alignment, and why it was replaced
-------------------------------------------
The survivor queues were keyed by visible text and store, so a repeat
consumed the OLDEST compatible survivor anywhere in the transcript. With an
older identified "Done." in the sidecar and a later Agent-only
``user -> tool -> "Done."`` turn, the later reply vanished and its reasoning
landed on the old row -- a regression against the plain union, which keeps
both.

An intermediate version aligned the two stores by text (prompts first, then
the rows of each turn). Review showed that any pairing by text, order and the
two processes' clocks has counterexamples: a repeated prompt ("continue")
re-created the cross-turn pairing, missing or cross-clock timestamps blocked
or forced pairs, and the dynamic programme cost ~800 ms on 2000 turns.

Exact row identity
------------------
The two copies now share an identity. Before a Gateway run the worker
records the session's highest state.db row id; after it, the rows above that
floor are the run's own, and the sidecar user/assistant rows are stamped with
the row id of the single run row that matches each (the in-process path
already stamps its rows the same way). The display merge reads Agent rows
with their row ids and pairs one row id on one row per store. A row with an
ambiguous or missing match stays unstamped.

A row id pairs only together with the same text, and nothing else pairs.
A text fallback for unstamped rows ("pair inside twin turns") was tried and
broke in review the same way: when the Agent copy of an old turn was gone,
a later Telegram turn with the same prompt became its unique "twin" and was
hidden. Rows without proof -- written before stamping, renumbered by a
transcript rewrite (/retry, /undo, /compress), or never stamped -- render as
on master. Stamping proves a twin only while the run's rows hold one turn,
and drops any id it cannot prove.
"""

from __future__ import annotations

import copy
import sqlite3
from types import SimpleNamespace

import api.models as models
import api.routes as routes


def _gateway_turn_session():
    """Sidecar rows exactly as the gateway chat worker writes them."""
    return SimpleNamespace(
        messages=[
            {"id": 7, "role": "user", "content": "show tomorrow's weather", "timestamp": 100.5, "_state_db_row_id": 1},
            {"id": 8, "role": "assistant", "content": "Tomorrow: sunny, 35C.", "timestamp": 130.542, "_state_db_row_id": 4},
        ]
    )


def _gateway_agent_rows():
    """Agent-store rows for the same turn: no stable id, earlier timestamps."""
    return [
        {"role": "user", "content": "show tomorrow's weather", "timestamp": 100.4, "_state_db_row_id": 1},
        {"role": "assistant", "content": "", "timestamp": 110.0, "tool_calls": [{"id": "c1"}], "_state_db_row_id": 2},
        {"role": "tool", "content": "{\"temp\": 35}", "timestamp": 120.0, "tool_call_id": "c1", "_state_db_row_id": 3},
        {
            "role": "assistant",
            "content": "Tomorrow: sunny, 35C.",
            "timestamp": 130.424,
            "_state_db_row_id": 4,
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


def test_gateway_reconciliation_pairs_across_differing_api_content():
    """The reported production shape: Agent copy carries provider bytes the
    sidecar copy lacks entirely.

    ``_session_message_visible_key`` appends ``api_content`` to its key via
    ``_session_message_key_with_sidecar``, so the two copies of one turn get
    DIFFERENT keys once one side has it and the other doesn't -- neither
    reconciles, and the turn renders twice. This reconciliation's own
    ``_cross_store_pairing_key`` excludes that field so the match still
    happens; `api_content` then gets adopted through the same directional
    (Agent -> sidecar) lane as the semantic payload.
    """
    session = SimpleNamespace(
        messages=[
            {"id": 7, "role": "user", "content": "q", "timestamp": 1.1, "_state_db_row_id": 1},
            {"id": 8, "role": "assistant", "content": "answer", "timestamp": 10.5, "_state_db_row_id": 3},
        ]
    )
    cli_messages = [
        {"role": "user", "content": "q", "timestamp": 1.0, "_state_db_row_id": 1},
        {"role": "tool", "content": "looked it up", "timestamp": 5.0, "tool_call_id": "c1", "_state_db_row_id": 2},
        {
            "role": "assistant",
            "content": "answer",
            "timestamp": 10.4,
            "_state_db_row_id": 3,
            "api_content": "PROVIDER BYTES",
            "reasoning": "AGENT REASONING",
        },
    ]

    merged = routes._merged_session_messages_for_display(session, cli_messages)

    answers = [m for m in merged if m.get("content") == "answer"]
    assert len(answers) == 1, "one gateway turn must not render as two answer rows"
    survivor = answers[0]
    assert survivor.get("id") == 8, "the identified sidecar row survives"
    assert survivor.get("api_content") == "PROVIDER BYTES"
    assert survivor.get("reasoning") == "AGENT REASONING"


def test_conflicting_api_content_rows_are_not_paired_at_all():
    """Two non-empty, DIFFERENT api_content values mean two turns, not one twin.

    CONTRACT CHANGE (review round 5). This case previously paired the rows and
    relied on ``_copy_api_content_sidecar`` being fill-only-if-absent to keep
    the survivor's own bytes -- which quietly assumed the two rows really were
    one turn. Nothing established that: ``_cross_store_pairing_key`` compares
    role, visible content and ``tool_calls`` only, so two genuinely different
    turns answering with the same text match it. Pairing them discarded the
    popped row outright, taking its ``api_content`` AND its Agent-owned
    ``reasoning`` with it, silently.

    ``_message_private_identity_compatible`` already treats contradictory
    non-empty ``api_content`` as proof two rows are not the same message, so
    pairing now fails closed on it and both turns survive intact. The failure
    mode this trades into -- a visible duplicate row when the two copies of
    ONE turn somehow disagree on provider bytes -- is the same trade this PR
    made deliberately in round 4: a visible duplicate is recoverable, silent
    deletion is not.
    """
    session = SimpleNamespace(
        messages=[
            {
                "id": 8,
                "role": "assistant",
                "content": "answer",
                "timestamp": 10.5,
                "_state_db_row_id": 2,
                "api_content": "SIDECAR'S OWN BYTES",
            }
        ]
    )
    cli_messages = [
        {"role": "user", "content": "q", "timestamp": 1.0},
        {
            "role": "assistant",
            "content": "answer",
            "timestamp": 10.4,
            "_state_db_row_id": 2,
            "api_content": "AGENT BYTES",
            "reasoning": "AGENT REASONING",
        },
    ]

    merged = routes._merged_session_messages_for_display(session, cli_messages)

    answers = [m for m in merged if m.get("content") == "answer"]
    assert len(answers) == 2, "contradictory provider bytes are not one turn"
    survivor = next(m for m in answers if m.get("id") == 8)
    assert survivor.get("api_content") == "SIDECAR'S OWN BYTES"
    assert "reasoning" not in survivor, "a non-twin's payload must not be adopted"
    other = next(m for m in answers if m.get("id") != 8)
    assert other.get("api_content") == "AGENT BYTES"
    assert other.get("reasoning") == "AGENT REASONING"


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
    The gateway writeback stamped each sidecar row with its Agent row id, so
    the repeat is no puzzle at all.
    """
    session = SimpleNamespace(
        messages=[
            {"id": "a1", "role": "assistant", "content": "same answer", "timestamp": 10.5,
             "_state_db_row_id": 11},
            {"id": "a2", "role": "assistant", "content": "same answer", "timestamp": 20.5,
             "_state_db_row_id": 22},
        ]
    )
    cli_messages = [
        # An agent-only row makes the agent store strictly longer than the
        # sidecar, which is what routes this scenario into the
        # chronological-union branch under test rather than the sibling
        # append-only branch (len(sidecar) >= len(cli) takes that one instead).
        {"role": "user", "content": "ask again", "timestamp": 5.0},
        {"role": "assistant", "content": "same answer", "timestamp": 10.4, "reasoning": "first reasoning",
         "_state_db_row_id": 11},
        {"role": "assistant", "content": "same answer", "timestamp": 20.4, "reasoning": "second reasoning",
         "_state_db_row_id": 22},
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
            {"id": "a1", "role": "assistant", "content": "same answer", "timestamp": 10.5,
             "_state_db_row_id": 11},
            {"id": "a2", "role": "assistant", "content": "same answer", "timestamp": 10.8,
             "_state_db_row_id": 12},
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
            "_state_db_row_id": 11,
        },
        {
            "role": "assistant",
            "content": "same answer",
            "timestamp": 10.7,
            "reasoning": "second reasoning",
            "codex_reasoning_items": [{"type": "reasoning", "n": 2}],
            "_state_db_row_id": 12,
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
            {"role": "user", "content": "the question", "timestamp": 1.0},
            {
                "role": "assistant",
                "content": "answer",
                "timestamp": 10.6,
                "_state_db_row_id": 3,
                "reasoning": "sidecar reasoning that must not travel",
                "_turnDuration": 1234,
            }
        ]
    )
    cli_messages = [
        {"role": "user", "content": "the question", "timestamp": 1.0},
        {"role": "tool", "content": "looked it up", "timestamp": 5.0, "tool_call_id": "c1"},
        {"id": "a1", "role": "assistant", "content": "answer", "timestamp": 10.4, "_state_db_row_id": 3},
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
                "_state_db_row_id": 2,
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
            "_state_db_row_id": 2,
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


def test_divergent_state_db_row_identity_rows_are_not_paired():
    """Same guard, the other identity leg: contradictory state.db provenance.

    ``_state_db_row_identity_details`` reads durable state.db aliases, which
    are independent of the WebUI-assigned ``id``/``message_id``. Two rows
    disagreeing there are provably different database rows even with no
    ``api_content`` on either side, so the Agent-owned ``reasoning`` of the
    second one must not be dropped as though it were a duplicate.
    """
    session = SimpleNamespace(
        messages=[
            {
                "id": 8,
                "role": "assistant",
                "content": "answer",
                "timestamp": 10.5,
                "_row_id": 501,
            }
        ]
    )
    cli_messages = [
        {"role": "user", "content": "q", "timestamp": 1.0},
        {
            "role": "assistant",
            "content": "answer",
            "timestamp": 10.4,
            "_row_id": 999,
            "reasoning": "the other turn's own reasoning",
        },
    ]

    merged = routes._merged_session_messages_for_display(session, cli_messages)

    answers = [m for m in merged if m.get("content") == "answer"]
    assert len(answers) == 2, "conflicting state.db row identity means two turns"
    survivor = next(m for m in answers if m.get("id") == 8)
    assert "reasoning" not in survivor, "a non-twin's payload must not be adopted"
    other = next(m for m in answers if m.get("id") != 8)
    assert other.get("_row_id") == 999
    assert other.get("reasoning") == "the other turn's own reasoning"


def test_row_identity_pairs_with_its_own_survivor_among_repeats():
    """A shared state.db alias decides WHICH survivor a row pairs with.

    The Agent row carries the alias of the SECOND survivor. Pairing it with
    the first, older one would cross-wire its reasoning onto the wrong turn.
    """
    session = SimpleNamespace(
        messages=[
            {"id": "s1", "role": "assistant", "content": "same", "timestamp": 10.5, "_row_id": 111},
            {"id": "s2", "role": "assistant", "content": "same", "timestamp": 20.5, "_row_id": 222},
        ]
    )
    cli_messages = [
        {"role": "user", "content": "q", "timestamp": 1.0},
        {"role": "tool", "content": "tool out", "timestamp": 2.0, "tool_call_id": "c1"},
        {
            "role": "assistant",
            "content": "same",
            "timestamp": 20.4,
            "_row_id": 222,
            "reasoning": "belongs to s2",
        },
    ]

    merged = routes._merged_session_messages_for_display(session, cli_messages)

    by_id = {m["id"]: m for m in merged if m.get("id")}
    assert set(by_id) == {"s1", "s2"}, "the twin collapses onto its own survivor"
    assert by_id["s2"].get("reasoning") == "belongs to s2"
    assert "reasoning" not in by_id["s1"], "the wrong survivor must not be written"


def test_matching_state_db_row_identity_still_pairs():
    """The guard must not break the case this reconciliation exists for.

    A real gateway twin carries the SAME state.db row alias on both copies
    (or none at all, the common shape), so the identity check has nothing to
    contradict and pairing proceeds exactly as before.
    """
    session = SimpleNamespace(
        messages=[
            {
                "id": 8,
                "role": "assistant",
                "content": "answer",
                "timestamp": 10.5,
                "_row_id": 501,
            }
        ]
    )
    cli_messages = [
        {"role": "user", "content": "q", "timestamp": 1.0},
        {
            "role": "assistant",
            "content": "answer",
            "timestamp": 10.4,
            "_row_id": 501,
            "api_content": "PROVIDER BYTES",
            "reasoning": "AGENT REASONING",
        },
    ]

    merged = routes._merged_session_messages_for_display(session, cli_messages)

    answers = [m for m in merged if m.get("content") == "answer"]
    assert len(answers) == 1, "a real twin must still render once"
    survivor = answers[0]
    assert survivor.get("id") == 8
    assert survivor.get("api_content") == "PROVIDER BYTES"
    assert survivor.get("reasoning") == "AGENT REASONING"


def test_later_agent_only_reply_is_not_consumed_by_an_older_turn():
    """The maintainer's repro: a repeat must not pair across a user turn.

    The sidecar holds an older identified "Done."; the Agent store holds a
    LATER turn the sidecar never saw (``user -> tool -> "Done."``). Visible-key
    FIFO handed that later reply the older survivor, so it vanished and its
    reasoning was grafted onto the old row. Neither row carries a row id here,
    so nothing proves a pair. ``master``'s plain union keeps both, and so must
    the reconciliation.
    """
    session = SimpleNamespace(
        messages=[{"id": "old", "role": "assistant", "content": "Done.", "timestamp": 10.0}]
    )
    cli_messages = [
        {"role": "user", "content": "now the other one", "timestamp": 50.0},
        {"role": "tool", "content": "ok", "timestamp": 51.0, "tool_call_id": "c1"},
        {"role": "assistant", "content": "Done.", "timestamp": 52.0, "reasoning": "later turn"},
    ]

    merged = routes._merged_session_messages_for_display(session, cli_messages)

    replies = [m for m in merged if m.get("content") == "Done."]
    assert [m.get("id") for m in replies] == ["old", None], "both replies render"
    assert "reasoning" not in replies[0], "the older turn must not adopt the later reasoning"
    assert replies[1]["reasoning"] == "later turn"


def test_repeat_pairs_with_the_occurrence_in_its_own_turn():
    """The Agent store holds both turns; the sidecar only the later one.

    The sidecar's "Done." is the later turn's twin. Pairing it with the oldest
    Agent "Done." instead would drop the earlier turn's reply and cross-wire
    both turns' reasoning.
    """
    session = SimpleNamespace(
        messages=[
            {"id": "u2", "role": "user", "content": "second task", "timestamp": 10.1, "_state_db_row_id": 3},
            {"id": "a2", "role": "assistant", "content": "Done.", "timestamp": 12.1, "_state_db_row_id": 4},
        ]
    )
    cli_messages = [
        {"role": "user", "content": "first task", "timestamp": 1.0, "_state_db_row_id": 1},
        {"role": "assistant", "content": "Done.", "timestamp": 2.0, "reasoning": "first", "_state_db_row_id": 2},
        {"role": "user", "content": "second task", "timestamp": 10.0, "_state_db_row_id": 3},
        {"role": "assistant", "content": "Done.", "timestamp": 12.0, "reasoning": "second", "_state_db_row_id": 4},
    ]

    merged = routes._merged_session_messages_for_display(session, cli_messages)

    assert [(m.get("id"), m.get("content")) for m in merged] == [
        (None, "first task"),
        (None, "Done."),
        ("u2", "second task"),
        ("a2", "Done."),
    ]
    assert merged[1]["reasoning"] == "first"
    assert merged[3]["reasoning"] == "second"


def test_unequal_repeats_inside_one_turn_keep_every_row():
    """Unequal repeat counts with nothing to break the tie stay unpaired.

    One identified "Done." in the sidecar, two unidentified ones in the Agent
    store, all in one turn: which Agent row is the twin cannot be told, and a
    wrong guess deletes a reply. A duplicate row is the cheaper failure.
    """
    session = SimpleNamespace(
        messages=[{"id": "s1", "role": "assistant", "content": "Done.", "timestamp": 20.5}]
    )
    cli_messages = [
        {"role": "user", "content": "do both", "timestamp": 1.0},
        {"role": "assistant", "content": "Done.", "timestamp": 10.0, "reasoning": "first"},
        {"role": "assistant", "content": "Done.", "timestamp": 20.4, "reasoning": "second"},
    ]

    merged = routes._merged_session_messages_for_display(session, cli_messages)

    replies = [m for m in merged if m.get("content") == "Done."]
    assert [m.get("reasoning") for m in replies] == ["first", "second", None]
    assert replies[2]["id"] == "s1"


def test_row_identity_breaks_an_unequal_repeat_tie():
    """A shared state.db row alias is the id that settles the ambiguity."""
    session = SimpleNamespace(
        messages=[
            {"id": "s1", "role": "assistant", "content": "Done.", "timestamp": 20.5, "_row_id": 22}
        ]
    )
    cli_messages = [
        {"role": "user", "content": "do both", "timestamp": 1.0},
        {"role": "assistant", "content": "Done.", "timestamp": 10.0, "_row_id": 11, "reasoning": "first"},
        {"role": "assistant", "content": "Done.", "timestamp": 20.4, "_row_id": 22, "reasoning": "second"},
    ]

    merged = routes._merged_session_messages_for_display(session, cli_messages)

    replies = [m for m in merged if m.get("content") == "Done."]
    assert [(m.get("id"), m.get("reasoning")) for m in replies] == [
        (None, "first"),
        ("s1", "second"),
    ]



def test_distinct_rows_sharing_a_dedup_key_each_pair_with_their_own_twin():
    """Two Agent rows with the same text and timestamp are still two rows.

    Their full-precision dedup keys collide, but each has its own identified
    sidecar twin by row id, so both pair and nothing renders twice.
    """
    session = SimpleNamespace(
        messages=[
            {"id": "a", "role": "assistant", "content": "Done.", "timestamp": 10.0, "_state_db_row_id": 2},
            {"id": "b", "role": "assistant", "content": "Done.", "timestamp": 10.0, "_state_db_row_id": 3},
        ]
    )
    cli_messages = [
        {"role": "user", "content": "go", "timestamp": 1.0, "_state_db_row_id": 1},
        {"role": "assistant", "content": "Done.", "timestamp": 10.0, "reasoning": "r1", "_state_db_row_id": 2},
        {"role": "assistant", "content": "Done.", "timestamp": 10.0, "reasoning": "r2", "_state_db_row_id": 3},
    ]

    merged = routes._merged_session_messages_for_display(session, cli_messages)

    replies = [m for m in merged if m.get("content") == "Done."]
    assert [(m.get("id"), m.get("reasoning")) for m in replies] == [("a", "r1"), ("b", "r2")]


def test_sidecar_rows_without_timestamps_still_pair_by_row_id():
    """Sidecar rows with no timestamp sort to the top of the union; the row
    id finds their twins wherever the chronological sort put them."""
    session = SimpleNamespace(
        messages=[
            {"id": "u1", "role": "user", "content": "q1", "_state_db_row_id": 2},
            {"id": "a1", "role": "assistant", "content": "A1", "_state_db_row_id": 3},
            {"id": "u2", "role": "user", "content": "q2", "_state_db_row_id": 4},
            {"id": "a2", "role": "assistant", "content": "A2", "_state_db_row_id": 5},
        ]
    )
    cli_messages = [
        {"role": "user", "content": "q0", "timestamp": 0.5, "_state_db_row_id": 1},
        {"role": "user", "content": "q1", "timestamp": 1.0, "_state_db_row_id": 2},
        {"role": "assistant", "content": "A1", "timestamp": 2.0, "reasoning": "r1", "_state_db_row_id": 3},
        {"role": "user", "content": "q2", "timestamp": 3.0, "_state_db_row_id": 4},
        {"role": "assistant", "content": "A2", "timestamp": 4.0, "reasoning": "r2", "_state_db_row_id": 5},
    ]

    merged = routes._merged_session_messages_for_display(session, cli_messages)

    assert sorted(m["content"] for m in merged) == ["A1", "A2", "q0", "q1", "q2"]
    by_id = {m["id"]: m for m in merged if m.get("id")}
    assert by_id["a1"]["reasoning"] == "r1"
    assert by_id["a2"]["reasoning"] == "r2"


def test_identified_and_unidentified_candidates_left_ambiguous_keep_every_row():
    """When an identified and an unidentified Agent row could both be the twin,
    nothing is guessed: the turn holds two candidates, so every row stays."""
    session = SimpleNamespace(
        messages=[{"id": "s1", "role": "assistant", "content": "Done.", "timestamp": 20.5}]
    )
    cli_messages = [
        {"role": "user", "content": "go", "timestamp": 1.0},
        {"id": "s1", "role": "assistant", "content": "Done.", "timestamp": 10.0},
        {"role": "assistant", "content": "Done.", "timestamp": 20.4, "reasoning": "r"},
    ]

    merged = routes._merged_session_messages_for_display(session, cli_messages)

    replies = [m for m in merged if m.get("content") == "Done."]
    assert [(m.get("id"), m.get("reasoning")) for m in replies] == [("s1", None), (None, "r")]


def test_vetoed_first_candidate_does_not_shift_later_repeats():
    """A veto on the first repeat must not slide the next twin into its place.

    The first sidecar and Agent answers contradict on ``api_content``. FIFO
    used to skip the vetoed survivor and hand the first Agent answer the
    SECOND sidecar row, putting its reasoning on the wrong turn.
    """
    session = SimpleNamespace(
        messages=[
            {"id": "u1", "role": "user", "content": "go", "timestamp": 1.1},
            {"id": "s1", "role": "assistant", "content": "same", "timestamp": 10.5, "api_content": "X"},
            {"id": "s2", "role": "assistant", "content": "same", "timestamp": 20.5},
        ]
    )
    cli_messages = [
        {"role": "user", "content": "go", "timestamp": 1.0},
        {"role": "tool", "content": "out", "timestamp": 2.0, "tool_call_id": "c1"},
        {"role": "assistant", "content": "same", "timestamp": 10.4, "api_content": "Y", "reasoning": "first"},
        {"role": "assistant", "content": "same", "timestamp": 20.4, "reasoning": "second"},
    ]

    merged = routes._merged_session_messages_for_display(session, cli_messages)

    same = [m for m in merged if m.get("content") == "same"]
    assert [(m.get("id"), m.get("reasoning")) for m in same] == [
        (None, "first"),
        ("s1", None),
        (None, "second"),
        ("s2", None),
    ], "an unpinned repeat keeps every row and moves no reasoning"

def test_tool_identity_vetoes_a_row_id_match():
    """A row id paired with a different tool call's result is no twin."""
    session = SimpleNamespace(
        messages=[
            {"id": "u1", "role": "user", "content": "go", "timestamp": 1.1, "_state_db_row_id": 1},
            {"id": "t1", "role": "tool", "content": "OK", "timestamp": 2.5, "tool_call_id": "c1", "_state_db_row_id": 3},
        ]
    )
    cli_messages = [
        {"role": "user", "content": "go", "timestamp": 1.0, "_state_db_row_id": 1},
        {"role": "tool", "content": "OK", "timestamp": 2.0, "tool_call_id": "c1", "_state_db_row_id": 2},
        {"role": "tool", "content": "OK", "timestamp": 3.0, "tool_call_id": "c2", "_state_db_row_id": 3},
    ]

    merged = routes._merged_session_messages_for_display(session, cli_messages)

    tools = [m for m in merged if m.get("role") == "tool"]
    assert [(m.get("id"), m["tool_call_id"]) for m in tools] == [(None, "c1"), ("t1", "c1"), (None, "c2")]

def test_structured_agent_prompt_pairs_with_its_sidecar_copy():
    """An image prompt keeps its structured identity on the Agent side too."""
    content = [
        {"type": "text", "text": "what is this?"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
    ]
    session = SimpleNamespace(
        messages=[
            {"id": "u1", "role": "user", "content": copy.deepcopy(content), "timestamp": 1.1, "_state_db_row_id": 1},
            {"id": "a1", "role": "assistant", "content": "A cat.", "timestamp": 5.1, "_state_db_row_id": 4},
        ]
    )
    cli_messages = [
        {"role": "user", "content": copy.deepcopy(content), "timestamp": 1.0, "_state_db_row_id": 1},
        {"role": "assistant", "content": "", "timestamp": 2.0, "tool_calls": [{"id": "c1"}], "_state_db_row_id": 2},
        {"role": "tool", "content": "vision", "timestamp": 3.0, "tool_call_id": "c1", "_state_db_row_id": 3},
        {"role": "assistant", "content": "A cat.", "timestamp": 5.0, "reasoning": "r", "_state_db_row_id": 4},
    ]

    merged = routes._merged_session_messages_for_display(session, cli_messages)

    assert [m.get("id") for m in merged if m.get("role") == "user"] == ["u1"]
    assert next(m for m in merged if m.get("id") == "a1")["reasoning"] == "r"


def test_stamped_twins_pair_by_row_id_across_a_repeated_prompt():
    """The maintainer's shape with a repeated prompt, once rows are stamped.

    The sidecar holds turn 1 (``continue -> Done.``); the Agent store holds
    turn 1 and a later Agent-only turn with the very same text. Text cannot
    tell the two turns apart; the row ids can.
    """
    session = SimpleNamespace(
        messages=[
            {"id": "u1", "role": "user", "content": "continue", "timestamp": 1.1, "_state_db_row_id": 1},
            {"id": "a1", "role": "assistant", "content": "Done.", "timestamp": 3.1, "_state_db_row_id": 3},
        ]
    )
    cli_messages = [
        {"role": "user", "content": "continue", "timestamp": 1.0, "_state_db_row_id": 1},
        {"role": "tool", "content": "ok", "timestamp": 2.0, "tool_call_id": "c1", "_state_db_row_id": 2},
        {"role": "assistant", "content": "Done.", "timestamp": 3.0, "reasoning": "turn 1", "_state_db_row_id": 3},
        {"role": "user", "content": "continue", "timestamp": 9.0, "_state_db_row_id": 7},
        {"role": "assistant", "content": "Done.", "timestamp": 9.5, "reasoning": "turn 2", "_state_db_row_id": 8},
    ]

    merged = routes._merged_session_messages_for_display(session, cli_messages)

    assert [(m.get("id"), m["content"]) for m in merged if m["role"] == "user"] == [
        ("u1", "continue"),
        (None, "continue"),
    ]
    replies = [m for m in merged if m.get("content") == "Done."]
    assert [(m.get("id"), m.get("reasoning")) for m in replies] == [("a1", "turn 1"), (None, "turn 2")]


def test_legacy_repeated_prompt_keeps_every_row():
    """Without row ids, a prompt repeated across turns pins nothing.

    Pairing the sidecar's ``continue`` turn with either Agent copy is a guess,
    and the wrong guess hides a reply, so every row renders.
    """
    session = SimpleNamespace(
        messages=[
            {"id": "u1", "role": "user", "content": "continue", "timestamp": 1.1},
            {"id": "a1", "role": "assistant", "content": "Done.", "timestamp": 3.1},
        ]
    )
    cli_messages = [
        {"role": "user", "content": "continue", "timestamp": 1.0},
        {"role": "assistant", "content": "Done.", "timestamp": 3.0, "reasoning": "turn 1"},
        {"role": "user", "content": "continue", "timestamp": 9.0},
        {"role": "assistant", "content": "Done.", "timestamp": 9.5, "reasoning": "turn 2"},
    ]

    merged = routes._merged_session_messages_for_display(session, cli_messages)

    replies = [m for m in merged if m.get("content") == "Done."]
    assert [(m.get("id"), m.get("reasoning")) for m in replies] == [
        (None, "turn 1"),
        ("a1", None),
        (None, "turn 2"),
    ]


def test_a_row_id_with_different_text_is_not_a_twin():
    """A stale or reused row id must not fold an unrelated Agent row away."""
    session = SimpleNamespace(
        messages=[{"id": "a1", "role": "assistant", "content": "hello", "timestamp": 2.1, "_state_db_row_id": 7}]
    )
    cli_messages = [
        {"role": "user", "content": "q", "timestamp": 1.0, "_state_db_row_id": 6},
        {"role": "assistant", "content": "something else", "timestamp": 2.0, "_state_db_row_id": 7},
    ]

    merged = routes._merged_session_messages_for_display(session, cli_messages)

    assert [m["content"] for m in merged if m["role"] == "assistant"] == ["something else", "hello"]


def test_in_process_stamps_pair_while_an_unstamped_gateway_turn_renders_twice():
    """A session that ran in-process (stamped) and then on an older gateway (unstamped)."""
    session = SimpleNamespace(
        messages=[
            {"id": "u1", "role": "user", "content": "local q", "timestamp": 1.1, "_state_db_row_id": 1},
            {"id": "a1", "role": "assistant", "content": "local a", "timestamp": 2.1, "_state_db_row_id": 2},
            {"id": "u2", "role": "user", "content": "gw q", "timestamp": 3.1},
            {"id": "a2", "role": "assistant", "content": "gw a", "timestamp": 5.1},
        ]
    )
    cli_messages = [
        {"role": "user", "content": "local q", "timestamp": 1.0, "_state_db_row_id": 1},
        {"role": "assistant", "content": "local a", "timestamp": 2.0, "_state_db_row_id": 2},
        {"role": "user", "content": "gw q", "timestamp": 3.0, "_state_db_row_id": 3},
        {"role": "tool", "content": "t", "timestamp": 4.0, "tool_call_id": "c1", "_state_db_row_id": 4},
        {"role": "assistant", "content": "gw a", "timestamp": 5.0, "reasoning": "r", "_state_db_row_id": 5},
    ]

    merged = routes._merged_session_messages_for_display(session, cli_messages)

    assert [(m.get("id"), m["content"]) for m in merged] == [
        ("u1", "local q"),
        ("a1", "local a"),
        (None, "gw q"),
        ("u2", "gw q"),
        (None, "t"),
        (None, "gw a"),
        ("a2", "gw a"),
    ]


def test_agent_only_repeat_of_a_legacy_prompt_keeps_every_row():
    """Unstamped turns pair with nothing, so a later Agent-only turn repeating
    a legacy prompt and reply keeps its own reasoning. Nothing is hidden."""
    session = SimpleNamespace(
        messages=[
            {"id": "u0", "role": "user", "content": "hi", "timestamp": 1.1},
            {"id": "a0", "role": "assistant", "content": "Done.", "timestamp": 2.1},
        ]
    )
    cli_messages = [
        {"role": "user", "content": "hi", "timestamp": 1.0},
        {"role": "assistant", "content": "Done.", "timestamp": 2.0, "reasoning": "old"},
        {"role": "user", "content": "hi", "timestamp": 8.0},
        {"role": "assistant", "content": "Done.", "timestamp": 9.0, "reasoning": "new"},
    ]

    merged = routes._merged_session_messages_for_display(session, cli_messages)

    assert [m.get("reasoning") for m in merged if m["content"] == "Done." and not m.get("id")] == ["old", "new"]


def test_reader_row_identity_stays_private_to_the_union_branch():
    """Callers read Agent rows with row ids for the exact lane only.

    Rows that leave the merge carry the default projection's shape, whichever
    branch produced them.
    """
    agent_only = {"role": "assistant", "content": "agent only", "timestamp": 9.0, "_state_db_row_id": 9}
    session = SimpleNamespace(
        messages=[{"id": "u1", "role": "user", "content": "q", "timestamp": 1.1, "_state_db_row_id": 1}]
    )
    union = routes._merged_session_messages_for_display(
        session,
        [{"role": "user", "content": "q", "timestamp": 1.0, "_state_db_row_id": 1}, agent_only],
    )
    assert [m.get("_state_db_row_id") for m in union] == [1, None]

    longer_sidecar = SimpleNamespace(
        messages=[
            {"id": "u1", "role": "user", "content": "q", "timestamp": 1.0},
            {"id": "a1", "role": "assistant", "content": "a", "timestamp": 2.0},
            {"id": "u2", "role": "user", "content": "q2", "timestamp": 3.0},
        ]
    )
    appended = routes._merged_session_messages_for_display(longer_sidecar, [agent_only])
    assert all("_state_db_row_id" not in m for m in appended)
    assert "_state_db_row_id" in agent_only, "the caller's row is not mutated"


def test_stamping_takes_only_an_unambiguous_run_row():
    run_rows = [
        {"role": "user", "content": "[Workspace: /w]\nfix it", "timestamp": 41.5, "_state_db_row_id": 41},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "c1"}], "timestamp": 42.5, "_state_db_row_id": 42},
        {"role": "tool", "content": "Done.", "tool_call_id": "c1", "timestamp": 43.5, "_state_db_row_id": 43},
        {"role": "assistant", "content": "Done.", "timestamp": 44.5, "_state_db_row_id": 44},
    ]
    user = {"id": "u", "role": "user", "content": "fix it"}
    assistant = {"id": "a", "role": "assistant", "content": "Done."}

    models.stamp_gateway_turn_row_ids(run_rows, (user, assistant))

    assert user["_state_db_row_id"] == 41, "the workspace prefix does not hide the prompt"
    assert user["_state_db_row_timestamp"] == 41.5, "the stamp pins the row's own state.db timestamp"
    assert assistant["_state_db_row_id"] == 44, "the tool row with the same text is not a candidate"

    steered = {"id": "u", "role": "user", "content": "go"}
    models.stamp_gateway_turn_row_ids(
        [
            {"role": "user", "content": "go", "_state_db_row_id": 1},
            {"role": "user", "content": "also X", "_state_db_row_id": 2},
            {"role": "assistant", "content": "Done.", "_state_db_row_id": 3},
        ],
        (steered,),
    )
    assert "_state_db_row_id" not in steered, "a window with two prompts (steer, concurrent turn, rewrite) stamps nothing"

    joined = {"id": "a", "role": "assistant", "content": "part one part two"}
    models.stamp_gateway_turn_row_ids(
        [
            {"role": "user", "content": "q", "_state_db_row_id": 0},
            {"role": "assistant", "content": "part one", "_state_db_row_id": 1},
            {"role": "assistant", "content": "part two", "_state_db_row_id": 2},
        ],
        (joined,),
    )
    assert "_state_db_row_id" not in joined, "a reply the stream joined matches no single row"

    regenerated = {"id": "a", "role": "assistant", "content": "Done.", "_state_db_row_id": 7}
    models.stamp_gateway_turn_row_ids(run_rows, (regenerated,))
    assert regenerated["_state_db_row_id"] == 44, "the superseded run's id gives way to this run's"


def test_state_db_floor_and_run_rows_read_only_this_sessions_new_rows(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, "
            "role TEXT, content TEXT, timestamp REAL, tool_call_id TEXT, tool_calls TEXT, "
            "tool_name TEXT, api_content TEXT, active INTEGER DEFAULT 1)"
        )
        conn.executemany(
            "INSERT INTO messages (session_id, role, content, timestamp) VALUES (?, ?, ?, ?)",
            [("s", "user", "old", 1.0), ("other", "user", "x", 2.0), ("s", "assistant", "old", 3.0)],
        )
    monkeypatch.setattr(models, "_active_state_db_path", lambda: db_path)

    floor = models.state_db_message_id_floor("s")
    assert floor == 3
    assert models.state_db_message_id_floor("nobody") == 0

    with sqlite3.connect(db_path) as conn:
        conn.executemany(
            "INSERT INTO messages (session_id, role, content, timestamp) VALUES (?, ?, ?, ?)",
            [
                ("s", "user", "new", 4.0),
                ("other", "user", "y", 5.0),
                ("s", "tool", "big tool output", 5.5),
                ("s", "assistant", "reply", 6.0),
            ],
        )
        conn.execute(
            "INSERT INTO messages (session_id, role, content, timestamp, active) VALUES ('s', 'user', 'archived', 6.5, 0)"
        )

    rows = models.state_db_session_rows_after("s", floor)
    assert [(m["content"], m["_state_db_row_id"]) for m in rows] == [("new", 4), ("reply", 7)]

    monkeypatch.setattr(models, "_active_state_db_path", lambda: tmp_path / "missing.db")
    assert models.state_db_message_id_floor("s") == 0
    assert models.state_db_session_rows_after("s", 0) == []


def _run_gateway_turn(
    tmp_path, monkeypatch, *, rows_after, during_stream=None, checkpoint=None, break_display_merge=False,
):
    """Drive one gateway turn ("continue" -> "Done.") through the real worker.

    With *checkpoint*, the prompt was saved eagerly at send time with these
    extra keys, as the active turn's token-stamped display row.
    """
    import time
    from collections import OrderedDict

    from api.process_event_utils import build_active_turn_token, stamp_message_source

    import api.gateway_chat as gateway_chat
    import api.streaming as streaming
    from api.config import STREAMS, create_stream_channel

    session_dir = tmp_path / "sessions"
    session_dir.mkdir(parents=True)
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    monkeypatch.setattr(models, "SESSIONS", OrderedDict())

    s = models.new_session()

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def __iter__(self):
            yield b'data: {"choices":[{"delta":{"content":"Done."}}]}\n\n'
            if during_stream:
                during_stream(s.session_id)
            yield b'data: [DONE]\n\n'

    monkeypatch.setenv("HERMES_WEBUI_GATEWAY_BASE_URL", "http://gateway.local")
    monkeypatch.setattr(streaming, "_load_webui_prefill_context", lambda cfg: {
        "status": "not_configured", "source": "none", "label": "", "message_count": 0, "messages": [],
    })
    monkeypatch.setattr(streaming, "_prefill_messages_with_webui_context", lambda ctx, cfg: [])
    monkeypatch.setattr(gateway_chat.urllib.request, "urlopen", lambda req, timeout=0: FakeResponse())
    monkeypatch.setattr(gateway_chat, "state_db_message_id_floor", lambda sid, profile=None: 40)
    monkeypatch.setattr(gateway_chat, "state_db_session_rows_after", rows_after)
    if break_display_merge:
        def broken_merge(*_args, **_kwargs):
            raise RuntimeError("boom")

        monkeypatch.setattr(streaming, "_merge_display_messages_after_agent_result", broken_merge)

    stream_id = "stream-gateway-row-identity-test"
    started = time.time() - 5
    s.active_stream_id = stream_id
    s.pending_user_message = "continue"
    s.pending_attachments = []
    s.pending_started_at = started
    if checkpoint is not None:
        row = {"role": "user", "content": "continue", "timestamp": started, **checkpoint}
        stamp_message_source(row, "webui", active_turn_token=build_active_turn_token(stream_id, started))
        s.messages = [row]
    s.save()
    STREAMS[stream_id] = create_stream_channel()
    try:
        gateway_chat._run_gateway_chat_streaming(s.session_id, "continue", "test-model", str(tmp_path), stream_id, [])
    finally:
        STREAMS.pop(stream_id, None)
    return models.get_session(s.session_id)


def test_gateway_writeback_stamps_the_turn_with_its_state_db_row_ids(tmp_path, monkeypatch):
    """The worker reads the floor before the run and stamps both rows after it."""
    reads = []

    def rows_after(sid, floor, profile=None):
        reads.append((sid, floor))
        return [
            {"role": "user", "content": "continue", "timestamp": 41.5, "_state_db_row_id": 41},
            {"role": "assistant", "content": "Done.", "timestamp": 42.5, "_state_db_row_id": 42},
        ]

    saved = _run_gateway_turn(tmp_path, monkeypatch, rows_after=rows_after)

    assert reads == [(saved.session_id, 40)]
    assert [(m["content"], m.get("_state_db_row_id")) for m in saved.messages] == [
        ("continue", 41),
        ("Done.", 42),
    ]
    assert [m.get("_state_db_row_id") for m in saved.context_messages[-2:]] == [41, 42]


# ---------------------------------------------------------------------------
# Proof-only pairing. A row id plus the same text is the only evidence that
# two rows are one message; without it both rows render, as on master. Each
# test below is a shape that broke a text-based fallback in review.
# ---------------------------------------------------------------------------


def _replies(merged, text="Done."):
    return [(m.get("id"), m.get("reasoning")) for m in merged if m.get("content") == text]


def test_old_sidecar_turn_is_not_paired_with_a_later_agent_only_turn():
    """The Agent copy of an old turn is gone (archived, undone); a later
    Telegram turn repeats its prompt and reply. Each prompt is unique in its
    store, which is no proof that they are one turn."""
    for stamp in ({}, {"_state_db_row_id": 1}):
        session = SimpleNamespace(
            messages=[
                {"id": "u1", "role": "user", "content": "status", "timestamp": 1.1, **stamp},
                {"id": "a1", "role": "assistant", "content": "Done.", "timestamp": 2.1},
            ]
        )
        cli_messages = [
            {"role": "user", "content": "status", "timestamp": 9.0, "_state_db_row_id": 9},
            {"role": "tool", "content": "ok", "timestamp": 9.2, "tool_call_id": "c9", "_state_db_row_id": 10},
            {"role": "assistant", "content": "Done.", "timestamp": 9.5, "reasoning": "telegram", "_state_db_row_id": 11},
        ]

        merged = routes._merged_session_messages_for_display(session, cli_messages)

        assert [(m.get("id"), m["content"]) for m in merged] == [
            ("u1", "status"),
            ("a1", "Done."),
            (None, "status"),
            (None, "ok"),
            (None, "Done."),
        ]
        assert _replies(merged) == [("a1", None), (None, "telegram")]


def test_unpaired_repeat_of_a_stamped_prompt_is_not_paired_by_text():
    """The exact lane pairs the stamped copies of ``continue``; an older
    sidecar-only ``continue`` and a later Telegram ``continue`` are then the
    only unpaired copies in their stores, and still not one turn."""
    session = SimpleNamespace(
        messages=[
            {"id": "u0", "role": "user", "content": "continue", "timestamp": 0.1},
            {"id": "a0", "role": "assistant", "content": "Done.", "timestamp": 0.2},
            {"id": "u1", "role": "user", "content": "continue", "timestamp": 1.1, "_state_db_row_id": 1},
            {"id": "a1", "role": "assistant", "content": "Done.", "timestamp": 3.1, "_state_db_row_id": 3},
        ]
    )
    cli_messages = [
        {"role": "user", "content": "continue", "timestamp": 1.0, "_state_db_row_id": 1},
        {"role": "tool", "content": "ok", "timestamp": 2.0, "tool_call_id": "c1", "_state_db_row_id": 2},
        {"role": "assistant", "content": "Done.", "timestamp": 3.0, "reasoning": "turn 1", "_state_db_row_id": 3},
        {"role": "user", "content": "continue", "timestamp": 9.0, "_state_db_row_id": 7},
        {"role": "tool", "content": "ok", "timestamp": 9.2, "tool_call_id": "c7", "_state_db_row_id": 8},
        {"role": "assistant", "content": "Done.", "timestamp": 9.5, "reasoning": "telegram", "_state_db_row_id": 9},
    ]

    merged = routes._merged_session_messages_for_display(session, cli_messages)

    assert [m.get("id") for m in merged if m["role"] == "user"] == ["u0", "u1", None]
    assert _replies(merged) == [("a0", None), ("a1", "turn 1"), (None, "telegram")]


def test_rows_before_the_first_prompt_are_not_paired_by_text():
    session = SimpleNamespace(
        messages=[
            {"id": "a0", "role": "assistant", "content": "Hi!", "timestamp": 0.5},
            {"id": "u1", "role": "user", "content": "q", "timestamp": 1.1, "_state_db_row_id": 3},
        ]
    )
    cli_messages = [
        {"role": "assistant", "content": "Hi!", "timestamp": 0.1, "reasoning": "agent", "_state_db_row_id": 1},
        {"role": "tool", "content": "ok", "timestamp": 0.2, "tool_call_id": "c0", "_state_db_row_id": 2},
        {"role": "user", "content": "q", "timestamp": 1.0, "_state_db_row_id": 3},
    ]

    merged = routes._merged_session_messages_for_display(session, cli_messages)

    assert _replies(merged, "Hi!") == [(None, "agent"), ("a0", None)]
    assert [m.get("id") for m in merged if m["role"] == "user"] == ["u1"]


def test_unstamped_gateway_turn_renders_both_copies_as_on_master():
    """A turn written before stamping existed, or whose stamping found no
    single match, has no proof of identity: a duplicate, never a guess."""
    session = SimpleNamespace(
        messages=[
            {"id": "u1", "role": "user", "content": "q1", "timestamp": 1.1},
            {"id": "a1", "role": "assistant", "content": "A1", "timestamp": 3.1},
        ]
    )
    cli_messages = [
        {"role": "user", "content": "q1", "timestamp": 1.0},
        {"role": "tool", "content": "t", "timestamp": 2.0, "tool_call_id": "c1"},
        {"role": "assistant", "content": "A1", "timestamp": 3.0, "reasoning": "r1"},
    ]

    merged = routes._merged_session_messages_for_display(session, cli_messages)

    assert [(m.get("id"), m["content"]) for m in merged] == [
        (None, "q1"),
        ("u1", "q1"),
        (None, "t"),
        (None, "A1"),
        ("a1", "A1"),
    ]
    assert "reasoning" not in merged[-1], "nothing travels without a proven pair"


def test_renumbered_agent_rows_render_both_copies():
    """/retry, /undo and /compress rewrite the Agent transcript with new row
    ids; the sidecar's stamps then prove nothing."""
    session = SimpleNamespace(
        messages=[
            {"id": "u1", "role": "user", "content": "q1", "timestamp": 1.1, "_state_db_row_id": 1},
            {"id": "a1", "role": "assistant", "content": "A1", "timestamp": 3.1, "_state_db_row_id": 3},
        ]
    )
    cli_messages = [
        {"role": "user", "content": "q1", "timestamp": 1.0, "_state_db_row_id": 101},
        {"role": "tool", "content": "t", "timestamp": 2.0, "tool_call_id": "c1", "_state_db_row_id": 102},
        {"role": "assistant", "content": "A1", "timestamp": 3.0, "reasoning": "r1", "_state_db_row_id": 103},
    ]

    merged = routes._merged_session_messages_for_display(session, cli_messages)

    assert [(m.get("id"), m["content"]) for m in merged] == [
        (None, "q1"),
        ("u1", "q1"),
        (None, "t"),
        (None, "A1"),
        ("a1", "A1"),
    ]
    assert all("_state_db_row_id" not in m for m in merged if not m.get("id"))


def test_requested_row_ids_do_not_change_the_legacy_same_second_collapse():
    """Rows with no stable id on either side collapse on the coarse merge key
    as on master. The row id the union branch requests from the reader must
    not veto that: a stale sidecar id against a renumbered Agent row would
    otherwise render each twin twice."""
    session = SimpleNamespace(
        messages=[
            {"role": "user", "content": "q", "timestamp": 1.2, "_state_db_row_id": 1},
            {"role": "assistant", "content": "A", "timestamp": 3.2, "_state_db_row_id": 3},
        ]
    )
    cli_messages = [
        {"role": "user", "content": "q", "timestamp": 1.0, "_state_db_row_id": 11},
        {"role": "tool", "content": "t", "timestamp": 2.0, "tool_call_id": "c1", "_state_db_row_id": 12},
        {"role": "assistant", "content": "A", "timestamp": 3.0, "_state_db_row_id": 13},
    ]

    merged = routes._merged_session_messages_for_display(session, cli_messages)

    assert [m["content"] for m in merged] == ["q", "t", "A"]


def test_stamping_drops_an_id_it_cannot_prove():
    """A regenerated row inherits the superseded run's id; when this run has
    no single matching row, the stale id goes rather than pairing the row
    with the old run's copy."""
    run_rows = [
        {"role": "user", "content": "continue", "timestamp": 43.5, "_state_db_row_id": 43},
        {"role": "assistant", "content": "Done.", "timestamp": 44.5, "_state_db_row_id": 44},
    ]

    inherited = {"id": "a", "role": "assistant", "content": "unmatched", "_state_db_row_id": 7}
    models.stamp_gateway_turn_row_ids(run_rows, (inherited,))
    assert "_state_db_row_id" not in inherited

    unread = {"id": "a", "role": "assistant", "content": "Done.", "_state_db_row_id": 7}
    models.stamp_gateway_turn_row_ids((), (unread,))
    assert "_state_db_row_id" not in unread, "no run rows to check against proves nothing either"

    aliased = {
        "id": "a", "role": "assistant", "content": "Done.", "_row_id": 5, "state_db_row_id": 6,
        "_state_db_row_timestamp": 1.0,
    }
    models.stamp_gateway_turn_row_ids(run_rows, (aliased,))
    assert {k: v for k, v in aliased.items() if k.startswith(("_row", "_state", "state"))} == {
        "_state_db_row_id": 44,
        "_state_db_row_timestamp": 44.5,
    }


def test_state_db_run_rows_keep_rows_whose_active_flag_is_null(tmp_path, monkeypatch):
    """The same filter as the display reader: only ``active = 0`` is archived."""
    db_path = tmp_path / "state.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, "
            "role TEXT, content TEXT, timestamp REAL, active INTEGER)"
        )
        conn.executemany(
            "INSERT INTO messages (session_id, role, content, timestamp, active) VALUES (?, ?, ?, ?, ?)",
            [("s", "user", "q", 1.0, None), ("s", "assistant", "gone", 2.0, 0), ("s", "assistant", "a", 3.0, 1)],
        )
    monkeypatch.setattr(models, "_active_state_db_path", lambda: db_path)

    rows = models.state_db_session_rows_after("s", 0)

    assert [(m["content"], m["_state_db_row_id"]) for m in rows] == [("q", 1), ("a", 3)]


def test_failed_stamping_does_not_lose_the_reply(tmp_path, monkeypatch):
    import api.gateway_chat as gateway_chat

    def broken(*_args, **_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(gateway_chat, "stamp_gateway_turn_row_ids", broken)

    saved = _run_gateway_turn(
        tmp_path,
        monkeypatch,
        rows_after=lambda sid, floor, profile=None: [{"role": "assistant", "content": "Done.", "_state_db_row_id": 42}],
    )

    assert [m["content"] for m in saved.messages] == ["continue", "Done."]


def test_superseded_stream_does_not_read_the_run_rows(tmp_path, monkeypatch):
    """The run rows are read only once the writeback is known to proceed."""
    reads = []

    def supersede(sid):
        models.get_session(sid).active_stream_id = "a-newer-stream"

    def rows_after(sid, floor, profile=None):
        reads.append(sid)
        return []

    _run_gateway_turn(tmp_path, monkeypatch, rows_after=rows_after, during_stream=supersede)

    assert reads == []


def test_a_reissued_row_id_is_not_a_twin():
    """A state.db restored from a backup, or recreated, hands row ids out
    again. The stamp pins the row's own state.db timestamp, which a later row
    with the same id and text does not share."""
    session = SimpleNamespace(
        messages=[
            {"id": "u1", "role": "user", "content": "go", "timestamp": 100.1,
             "_state_db_row_id": 499, "_state_db_row_timestamp": 100.0},
            {"id": "a1", "role": "assistant", "content": "Done.", "timestamp": 130.1,
             "_state_db_row_id": 500, "_state_db_row_timestamp": 130.0},
        ]
    )
    cli_messages = [
        {"role": "user", "content": "go", "timestamp": 900.0, "_state_db_row_id": 499},
        {"role": "assistant", "content": "Done.", "timestamp": 930.0, "reasoning": "telegram", "_state_db_row_id": 500},
        {"role": "tool", "content": "ok", "timestamp": 940.0, "tool_call_id": "c1", "_state_db_row_id": 501},
    ]

    merged = routes._merged_session_messages_for_display(session, cli_messages)

    assert _replies(merged) == [("a1", None), (None, "telegram")]

    cli_messages[0]["timestamp"], cli_messages[1]["timestamp"] = 100.0, 130.0
    merged = routes._merged_session_messages_for_display(session, cli_messages)

    assert [m.get("id") for m in merged if m["role"] != "tool"] == ["u1", "a1"]
    assert _replies(merged) == [("a1", "telegram")]


def test_one_row_id_on_two_sidecar_rows_pairs_neither():
    session = SimpleNamespace(
        messages=[
            {"id": "u1", "role": "user", "content": "go", "timestamp": 1.1, "_state_db_row_id": 1},
            {"id": "u1-copy", "role": "user", "content": "go", "timestamp": 1.2, "_state_db_row_id": 1},
        ]
    )
    cli_messages = [
        {"role": "user", "content": "go", "timestamp": 1.0, "_state_db_row_id": 1},
        {"role": "tool", "content": "ok", "timestamp": 2.0, "tool_call_id": "c1", "_state_db_row_id": 2},
        {"role": "assistant", "content": "Done.", "timestamp": 3.0, "_state_db_row_id": 3},
    ]

    merged = routes._merged_session_messages_for_display(session, cli_messages)

    assert [m.get("id") for m in merged if m["role"] == "user"] == [None, "u1", "u1-copy"]


def _stamped_run_rows(sid, floor, profile=None):
    return [
        {"role": "user", "content": "continue", "timestamp": 41.5, "_state_db_row_id": 41},
        {"role": "assistant", "content": "Done.", "timestamp": 42.5, "_state_db_row_id": 42},
    ]


def test_the_eager_display_row_takes_the_prompts_stamp(tmp_path, monkeypatch):
    """The prompt saved at send time is the display row; it gets this run's
    stamp, and loses the one it inherited."""
    for broken in (False, True):
        saved = _run_gateway_turn(
            tmp_path / str(broken),
            monkeypatch,
            rows_after=_stamped_run_rows,
            checkpoint={"_state_db_row_id": 7, "_row_id": 7},
            break_display_merge=broken,
        )

        stamps = [
            (m["content"], m.get("_state_db_row_id"), m.get("_state_db_row_timestamp"), m.get("_row_id"))
            for m in saved.messages
        ]
        assert stamps == [("continue", 41, 41.5, None), ("Done.", 42, 42.5, None)], broken
        assert [m.get("_state_db_row_id") for m in saved.context_messages[-2:]] == [41, 42]


def test_a_display_row_without_a_proven_stamp_loses_its_inherited_one(tmp_path, monkeypatch):
    saved = _run_gateway_turn(
        tmp_path,
        monkeypatch,
        rows_after=lambda sid, floor, profile=None: [],
        checkpoint={"_state_db_row_id": 7},
    )

    assert [m.get("_state_db_row_id") for m in saved.messages] == [None, None]

