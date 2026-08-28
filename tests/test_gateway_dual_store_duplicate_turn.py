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
