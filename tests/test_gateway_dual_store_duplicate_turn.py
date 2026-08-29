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
"""

from __future__ import annotations

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
