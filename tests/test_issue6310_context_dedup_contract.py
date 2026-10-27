"""#6310 — context-identity dedup contract (maintainer prescription, fail-first).

Prescription this module pins (nesquena-hermes):

* 2026-08-03: dedup-local handling prefers a valid non-negative integer message
  ID when available; adjacent replay copies still collapse; an ID-bearing row
  followed by an ID-less copy still collapses; partial/plain/settled/anchor
  ownership resolves to the authoritative settled owner in either order;
  replacement repairs ``seen`` bookkeeping; invalid, boolean, negative and
  non-integer IDs fail safely to the legacy path.
* 2026-08-14: one retained ownership set BEFORE ID minting; one stable ID
  across context and display projections; the same ordering in streaming
  settlement and synchronous chat. Required regressions: replay-cleaned
  context and display agree; stale-user repair keeps the same integer ID
  through a subsequent settlement; sync chat dedups before minting.

Every test drives the REAL components (``_deduplicate_context_messages``,
``_dedupe_replayed_context_messages``, ``_sanitize_messages_for_agent``,
``_strip_stale_user_merge_from_messages``, ``models.Session`` save/load) — no
reimplemented logic, so a green run is evidence about the shipping tree.
"""

from __future__ import annotations

import copy

import pytest

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


def _user(text, **extra):
    row = {"role": "user", "content": text}
    row.update(extra)
    return row


def _assistant(text, **extra):
    row = {"role": "assistant", "content": text}
    row.update(extra)
    return row


def _sanitize(messages, **kwargs):
    from api.streaming import _sanitize_messages_for_agent

    return _sanitize_messages_for_agent(copy.deepcopy(messages), **kwargs)


def _settle(previous_context, agent_returned, msg_text, projection):
    """Drive the real settle core + the real context dedupe."""
    from api.streaming import (
        _agent_replay_settle_projection,
        _dedupe_replayed_context_messages,
        _deduplicate_context_messages,
    )

    settled, protected = _dedupe_replayed_context_messages(
        list(previous_context),
        list(agent_returned),
        msg_text,
        projected_history=_agent_replay_settle_projection(projection),
    )
    return _deduplicate_context_messages(settled, protected)


def _save_and_reload(rows, session_id, tmp_path, monkeypatch):
    """Persist through the real Session writer and cold-reload from disk."""
    import api.models as models

    state_dir = tmp_path / "state"
    session_dir = state_dir / "sessions"
    session_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", state_dir / "session_index.json")
    session = models.Session(
        session_id=session_id,
        workspace=str(tmp_path),
        messages=list(rows),
        context_messages=list(rows),
        model="test-model",
        model_provider="test-provider",
    )
    session.save(touch_updated_at=False)
    reloaded = models.Session.load(session_id)
    assert reloaded is not None, "the cold reload must find the persisted session"
    return reloaded


def _build_next_turn_context(session, msg_text):
    """Drive the real pre-turn build: reconcile → new-turn → dedupe."""
    import api.models as models
    from api.streaming import (
        _deduplicate_context_messages,
        _new_turn_context_from_messages,
    )

    reconciled = models.reconciled_state_db_messages_for_session(
        session, prefer_context=True
    )
    return _deduplicate_context_messages(
        _new_turn_context_from_messages(reconciled, msg_text)
    )


def _contents(rows):
    return [r.get("content") for r in rows]


# ─────────────────────────── Part A: dedup contract ───────────────────────────


def test_distinct_nonadjacent_same_text_turns_survive_with_stable_ids():
    """The issue's literal repro at the dedup layer: repeated feedback.

    ``higher`` / ``higher`` / ``lower`` are three distinct committed turns that
    happen to share text with an earlier turn. Distinct valid stable ids prove
    independent ownership, so every occurrence must survive in chronological
    order — a content-only identity collapses the interior repeats and the
    model then answers from an earlier point in the interaction.
    """
    from api.streaming import _deduplicate_context_messages

    messages = [
        _user("let's play a guessing game", id=1, timestamp=1),
        _assistant("pick a number", id=2, timestamp=2),
        _user("higher", id=3, timestamp=3),
        _assistant("guess 50", id=4, timestamp=4),
        _user("higher", id=5, timestamp=5),
        _assistant("guess 75", id=6, timestamp=6),
        _user("lower", id=7, timestamp=7),
        _assistant("guess 60", id=8, timestamp=8),
    ]
    deduped = _deduplicate_context_messages(copy.deepcopy(messages))
    contents = _contents(deduped)
    assert contents.count("higher") == 2, (
        "both independently owned 'higher' turns must survive; a content-only "
        f"collapse deleted one; got {contents}"
    )
    assert contents == [
        "let's play a guessing game",
        "pick a number",
        "higher",
        "guess 50",
        "higher",
        "guess 75",
        "lower",
        "guess 60",
    ], f"chronological order must be preserved; got {contents}"


def test_adjacent_replay_copies_collapse():
    """Adjacent lifecycle-equivalent copies are replay, not distinct turns.

    The same persisted record surfacing twice (e.g. through two load paths)
    must still collapse — with or without stable ids — while the surviving
    copy keeps its position.
    """
    from api.streaming import _deduplicate_context_messages

    with_ids = [
        _user("replay me", id=4),
        _user("replay me", id=4),
        _assistant("ok", id=5),
    ]
    deduped = _deduplicate_context_messages(copy.deepcopy(with_ids))
    assert _contents(deduped) == ["replay me", "ok"], (
        f"adjacent same-id copies must collapse to one; got {_contents(deduped)}"
    )
    assert deduped[0].get("id") == 4, "the surviving copy keeps its stable id"

    without_ids = [
        _user("replay me"),
        _user("replay me"),
        _assistant("ok"),
    ]
    deduped_plain = _deduplicate_context_messages(copy.deepcopy(without_ids))
    assert _contents(deduped_plain) == ["replay me", "ok"], (
        "id-less adjacent copies stay on the legacy collapse path; got "
        f"{_contents(deduped_plain)}"
    )


def test_id_bearing_row_followed_by_idless_copy_collapses():
    """An ID-less copy cannot prove independent ownership.

    A settled row carrying a stable id, followed by an otherwise identical row
    with no id, must still collapse: the id-less row is the replay, and the
    survivor is the authoritative owned row.
    """
    from api.streaming import _deduplicate_context_messages

    messages = [
        _user("status?", id=11),
        _assistant("all green", id=12),
        _user("status?", id=13),
        _assistant("all green", id=14),
        _user("status?"),  # id-less replay of the last user row
    ]
    deduped = _deduplicate_context_messages(copy.deepcopy(messages))
    contents = _contents(deduped)
    assert contents.count("status?") == 2, (
        "the ID-bearing occurrences are distinct turns and survive, but the "
        f"trailing ID-less copy must collapse; got {contents}"
    )
    assert contents[-1] == "all green", (
        f"the trailing ID-less replay must not displace the tail; got {contents}"
    )
    assert [r.get("id") for r in deduped if r.get("role") == "user"] == [11, 13], (
        "the surviving user rows are the ID-bearing owners; got "
        f"{[r.get('id') for r in deduped if r.get('role') == 'user']}"
    )


def test_active_turn_admitted_row_registers_its_stable_id():
    """A current-turn row admitted beside a seen replay key registers its id.

    The active-turn append path admits a row whose exact key is new but whose
    replay key was already seen. Its durable id must join the per-key kept-id
    registry, or a later replay of that same record (timestamp shifted across
    a load) reaches the stable-id ownership path with a registry that never
    saw the id — and survives as a duplicate the model would see twice.
    """
    from api.streaming import _deduplicate_context_messages

    messages = [
        _user("q", id=1, timestamp=1),
        _user("q", id=2, timestamp=2, _active_turn_token="t2:2"),
        _user("q", id=2, timestamp=3),  # same record, replayed with a shifted ts
    ]
    deduped = _deduplicate_context_messages(copy.deepcopy(messages))
    contents = _contents(deduped)
    assert contents.count("q") == 2, (
        "the first two rows are independently owned (distinct ids); the third "
        f"is a replay of record id=2 and must collapse; got {contents}"
    )


def test_shifted_timestamp_replay_does_not_poison_the_exact_index():
    """A dropped replay must not publish an out-of-range exact-key index.

    Reviewer regression (PR #8519 round 1): the exact-key index used to be
    published with the PROSPECTIVE append slot before the stable-id branch
    decided whether the row survives. A replay dropped by that branch left
    the index pointing past the retained list, and the next same-key lookup
    dereferenced it (IndexError). The index must only ever name a retained
    row, and only after retention is decided.
    """
    from api.streaming import _deduplicate_context_messages

    messages = [
        _user("higher", id=1, timestamp=1),
        _assistant("guess 50", id=2, timestamp=1),
        _user("higher", id=1, timestamp=2),  # replay of record 1, shifted ts
        _user("higher", id=3, timestamp=2),  # independently owned turn
    ]
    deduped = _deduplicate_context_messages(copy.deepcopy(messages))
    contents = _contents(deduped)
    assert contents == ["higher", "guess 50", "higher"], (
        "the shifted-timestamp replay of record 1 must collapse while the "
        f"independently owned id-3 turn survives; got {contents}"
    )
    assert [r.get("id") for r in deduped if r.get("role") == "user"] == [1, 3], (
        "the surviving user rows are record 1 and the independent id-3 turn; "
        f"got {[r.get('id') for r in deduped if r.get('role') == 'user']}"
    )


def test_same_timestamp_replay_after_independent_owners_keeps_index_valid():
    """Same replay schedule inside one timestamp bucket (ids 1, 3, replay 1, 4).

    The independently owned id-3 row is admitted beside record 1, the replay
    of record 1 then collapses, and a further independent id-4 turn is
    admitted — every same-key lookup in that sequence must resolve to a
    retained row.
    """
    from api.streaming import _deduplicate_context_messages

    messages = [
        _user("higher", id=1, timestamp=1),
        _user("higher", id=3, timestamp=1),
        _user("higher", id=1, timestamp=1),  # replay of record 1
        _user("higher", id=4, timestamp=1),
    ]
    deduped = _deduplicate_context_messages(copy.deepcopy(messages))
    ids = [r.get("id") for r in deduped]
    assert ids == [1, 3, 4], (
        "records 1, 3 and 4 are independently owned and survive in order "
        f"while the replay of record 1 collapses; got {ids}"
    )


def test_replay_schedule_survives_cold_reconcile_and_next_turn_build(
    tmp_path, monkeypatch
):
    """The same schedule through the real cold path: save → load → reconcile
    → next-turn build → dedupe.

    A replay that the exact index must collapse can arrive through the
    reconciled state.db projection rather than a direct list, so the
    bookkeeping repair is pinned at the composition the production routes
    actually run.
    """
    import api.models as models

    settled = [
        _user("higher", id=1, timestamp=1),
        _assistant("guess 50", id=2, timestamp=1),
        _user("higher", id=1, timestamp=2),  # replay of record 1, shifted ts
        _user("higher", id=3, timestamp=2),  # independently owned turn
    ]
    reloaded = _save_and_reload(settled, "i6310_cold_replay", tmp_path, monkeypatch)
    reconciled = models.reconciled_state_db_messages_for_session(
        reloaded, prefer_context=True
    )
    reconciled_ids = [
        r.get("id") for r in reconciled if isinstance(r, dict) and r.get("role") == "user"
    ]
    assert 1 in reconciled_ids, (
        "precondition: the replayed record must reach the dedupe through the "
        f"reconciled projection; got {reconciled_ids}"
    )
    context = _build_next_turn_context(reloaded, "lower")
    user_ids = [r.get("id") for r in context if r.get("role") == "user"]
    assert user_ids.count(1) == 1, (
        "record 1 must appear exactly once after the cold reconcile and "
        f"next-turn build; got {user_ids}"
    )
    assert 3 in user_ids, (
        f"the independently owned id-3 turn must survive; got {user_ids}"
    )
    assert _contents(context) == ["higher", "guess 50", "higher"], (
        "the next-turn context is the reconciled history: the replay "
        f"collapsed and the independent turn survived; got {_contents(context)}"
    )


@pytest.mark.parametrize(
    "bad_id",
    [True, False, -1, -7, "abc", "  ", float("inf"), float("nan"), 3.5, [1], {"a": 1}],
)
@pytest.mark.parametrize("distinct_timestamps", [False, True], ids=["exact-index", "stable-path"])
def test_invalid_ids_fail_safely_to_legacy_path(bad_id, distinct_timestamps):
    """Invalid/boolean/negative/non-integer ids must never grant ownership.

    A row whose id cannot be canonicalised as a non-negative integer falls
    back to the legacy content-identity behaviour (collapse), so a malformed
    or hostile id can neither rescue a replay nor split a turn. Both entry
    shapes are pinned: the user exact-index collision (same timestamp bucket)
    and the stable-id ownership path (distinct timestamps).
    """
    from api.streaming import _deduplicate_context_messages

    ts = {"timestamp": 2} if distinct_timestamps else {}
    messages = [
        _user("same text", id=9),
        _user("same text", id=bad_id, **ts),
    ]
    deduped = _deduplicate_context_messages(copy.deepcopy(messages))
    assert _contents(deduped) == ["same text"], (
        f"an invalid id ({bad_id!r}) must fail safely to the legacy collapse; "
        f"got {_contents(deduped)}"
    )


def test_matching_id_still_collapses():
    """The same durable id on both rows is one record, not two turns."""
    from api.streaming import _deduplicate_context_messages

    messages = [
        _user("same text", id=42),
        _user("same text", id="42"),
    ]
    deduped = _deduplicate_context_messages(copy.deepcopy(messages))
    assert _contents(deduped) == ["same text"], (
        f"a matching id stays conservative and collapses; got {_contents(deduped)}"
    )


# ─────────────────────── Part B: cross-path controls ───────────────────────


def test_repeated_feedback_survives_settle_later_turn_and_cold_reload(
    tmp_path, monkeypatch
):
    """The issue repro end-to-end: repeated feedback across real turns.

    Drives the real settle core, the real pre-turn build and a real cold
    reload. Every committed turn — including the interior repeats — must
    survive on BOTH surfaces (display ``messages`` and model-facing
    ``context_messages``), in order, through a later unrelated turn.
    """
    previous_context = [
        _user("let's play a guessing game", id=1, timestamp=1),
        _assistant("pick a number", id=2, timestamp=2),
    ]

    # ── Turn 2: first "higher".
    projection = _sanitize(previous_context, normalize_bare_assistant_adjacency=True)
    agent_returned = [
        *copy.deepcopy(projection),
        _user("higher", id=3, timestamp=3, _active_turn_token="t2:3"),
        _assistant("guess 50", id=4, timestamp=4),
    ]
    settled = _settle(previous_context, agent_returned, "higher", projection)
    assert _contents(settled).count("higher") == 1
    session = _save_and_reload(settled, "i6310_repeated_feedback", tmp_path, monkeypatch)

    # ── Turn 3: "higher" AGAIN — the interior repeat.
    turn3_context = _build_next_turn_context(session, "higher")
    projection3 = _sanitize(turn3_context, normalize_bare_assistant_adjacency=True)
    returned3 = [
        *copy.deepcopy(projection3),
        _user("higher", id=5, timestamp=5, _active_turn_token="t3:5"),
        _assistant("guess 75", id=6, timestamp=6),
    ]
    settled3 = _settle(turn3_context, returned3, "higher", projection3)
    assert _contents(settled3).count("higher") == 2, (
        "the second 'higher' is an independently owned turn and must survive "
        f"the settle; got {_contents(settled3)}"
    )
    session = _save_and_reload(settled3, "i6310_repeated_feedback", tmp_path, monkeypatch)

    # ── Turn 4: "lower" — an unrelated turn must not disturb the repeats.
    turn4_context = _build_next_turn_context(session, "lower")
    projection4 = _sanitize(turn4_context, normalize_bare_assistant_adjacency=True)
    returned4 = [
        *copy.deepcopy(projection4),
        _user("lower", id=7, timestamp=7, _active_turn_token="t4:7"),
        _assistant("guess 60", id=8, timestamp=8),
    ]
    settled4 = _settle(turn4_context, returned4, "lower", projection4)
    reloaded = _save_and_reload(
        settled4, "i6310_repeated_feedback", tmp_path, monkeypatch
    )

    for surface, rows in (
        ("messages", reloaded.messages),
        ("context_messages", reloaded.context_messages),
    ):
        contents = _contents(rows)
        assert contents.count("higher") == 2, (
            f"both 'higher' turns must survive on {surface} through the cold "
            f"reload; got {contents}"
        )
        assert contents.count("lower") == 1, (
            f"'lower' must survive exactly once on {surface}; got {contents}"
        )
        for answer in ("guess 50", "guess 75", "guess 60"):
            assert answer in contents, (
                f"{answer!r} must survive on {surface}; got {contents}"
            )
        assert contents.index("higher") < contents.index("lower"), (
            f"chronological order must hold on {surface}; got {contents}"
        )


def test_replay_cleaned_context_and_display_agree(tmp_path, monkeypatch):
    """Replay-cleaned context and the display projection must agree.

    When the Agent replays the sent history verbatim, the settle collapses the
    replay out of the model-facing context; the visible transcript must show
    the SAME occurrences afterwards (no display/context divergence), including
    independently owned repeats.
    """
    previous_context = [
        _user("question", id=1, timestamp=1),
        _assistant("answer", id=2, timestamp=2),
        _user("question", id=3, timestamp=3),
        _assistant("answer", id=4, timestamp=4),
    ]
    projection = _sanitize(previous_context, normalize_bare_assistant_adjacency=True)
    # The Agent echoes the full projection back and appends one new exchange.
    agent_returned = [
        *copy.deepcopy(projection),
        _user("one more", id=5, timestamp=5, _active_turn_token="t2:5"),
        _assistant("done", id=6, timestamp=6),
    ]
    settled = _settle(previous_context, agent_returned, "one more", projection)
    reloaded = _save_and_reload(settled, "i6310_display_context_agree", tmp_path, monkeypatch)

    display_counts = {}
    context_counts = {}
    for label, rows in (
        ("display", reloaded.messages),
        ("context", reloaded.context_messages),
    ):
        counts = {"question": 0, "answer": 0}
        for row in rows:
            if row.get("content") in counts:
                counts[row["content"]] += 1
        (display_counts if label == "display" else context_counts).update(counts)
    assert display_counts == context_counts, (
        "the display surface and the model-facing context must report the same "
        f"occurrence counts after the replay-cleaned settle; display="
        f"{display_counts} context={context_counts}"
    )
    assert context_counts == {"question": 2, "answer": 2}, (
        "both independently owned repeats must survive on both surfaces; got "
        f"{context_counts}"
    )


def test_stale_user_repair_keeps_same_id_through_settlement(tmp_path, monkeypatch):
    """Stale-user repair retains the result-owned row's integer id.

    The boundary/full-prefix stale-user repair replaces a polluted merged row
    with a clean copy of the current turn. The repaired row must carry the
    SAME integer id on both the display and the model-facing projections (a
    deep-copied ``id=None`` repair row would desynchronise the two surfaces
    and let a later settle re-admit the pollution as a distinct turn).
    """
    from api.streaming import _strip_stale_user_merge_from_messages

    prior_tail = "please use the larger context model"
    current_turn = "can you summarize the release blockers?"
    polluted = f"{prior_tail}\n\n{current_turn}"

    result_rows = [
        _user(prior_tail, id=20, timestamp=1),
        _user(polluted, id=21, timestamp=2),  # polluted boundary row (owned)
        _assistant("Sure, checking that now.", id=22, timestamp=3),
    ]
    cleaned = _strip_stale_user_merge_from_messages(
        copy.deepcopy(result_rows), current_turn, prior_tail
    )
    repaired = cleaned[1]
    assert repaired["content"] == current_turn, (
        f"the repair must restore the clean current turn; got {repaired}"
    )
    assert isinstance(repaired.get("id"), int) and not isinstance(
        repaired.get("id"), bool
    ), (
        "the repaired row must retain the result-owned integer id so both "
        f"surfaces share it; got id={repaired.get('id')!r}"
    )

    # A subsequent settle over the repaired context must keep that id.
    previous_context = [
        _user("earlier", id=10, timestamp=0),
        repaired,
    ]
    projection = _sanitize(previous_context, normalize_bare_assistant_adjacency=True)
    agent_returned = [
        *copy.deepcopy(projection),
        _user("next", id=30, timestamp=4, _active_turn_token="t2:30"),
        _assistant("ack", id=31, timestamp=5),
    ]
    settled = _settle(previous_context, agent_returned, "next", projection)
    reloaded = _save_and_reload(settled, "i6310_stale_user_id", tmp_path, monkeypatch)
    for surface, rows in (
        ("messages", reloaded.messages),
        ("context_messages", reloaded.context_messages),
    ):
        ids = [
            r.get("id")
            for r in rows
            if isinstance(r, dict) and r.get("content") == current_turn
        ]
        assert ids == [21], (
            f"the repaired row must keep id 21 on {surface} through the "
            f"settlement and cold reload; got {ids}"
        )
