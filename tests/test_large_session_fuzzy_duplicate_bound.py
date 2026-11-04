"""Regression coverage for large-session fuzzy duplicate matching."""

from __future__ import annotations

import pytest


def _visible_keys(count: int) -> set[tuple]:
    return {("assistant", f"message {idx}", "") for idx in range(count)}


def test_large_visible_set_keeps_exact_duplicate_matching():
    import api.models as models

    keys = _visible_keys(1001)
    exact = ("assistant", "message 1000", "")

    assert models._matching_visible_duplicate(exact, keys) == exact


def test_large_visible_set_skips_fuzzy_duplicate_scan(monkeypatch):
    import api.models as models

    keys = _visible_keys(1001)
    fuzzy_only = ("assistant", "message 1000 with suffix", "")

    def unexpected_lookup(_keys):
        raise AssertionError("large visible sets must not build the fuzzy lookup")

    monkeypatch.setattr(models, "_build_visible_duplicate_lookup", unexpected_lookup)

    assert models._matching_visible_duplicate(fuzzy_only, keys) is None


def test_threshold_sized_visible_set_preserves_fuzzy_recovery():
    import api.models as models

    keys = _visible_keys(1000)
    fuzzy_only = ("assistant", "message 999 with suffix", "")

    assert models._matching_visible_duplicate(fuzzy_only, keys) in keys


@pytest.mark.parametrize(
    "workspace_prefix",
    ("[Workspace::v1: /tmp/project]", "[Workspace: /tmp/project]"),
)
def test_large_merge_dedupes_same_clock_workspace_prefixed_middle_replay(workspace_prefix):
    """A TRUE replay (same clock) is still deduped on a large visible set."""
    import api.models as models

    sidecar = [
        {
            "role": "user" if idx % 2 == 0 else "assistant",
            "content": f"message {idx}",
            "timestamp": float(idx),
        }
        for idx in range(1000)
    ]
    sidecar.insert(
        500,
        {"role": "user", "content": "canonical prompt", "timestamp": 499.5},
    )
    sidecar.append(
        {"role": "assistant", "content": "final assistant", "timestamp": 1001.0}
    )
    # Same clock as the sidecar row it mirrors -> one durable record replayed.
    state_replay = [
        {
            "role": "user",
            "content": f"{workspace_prefix}\ncanonical prompt",
            "timestamp": 499.5,
        }
    ]

    merged = models.merge_session_messages_append_only(sidecar, state_replay)

    assert len(merged) == len(sidecar)
    assert merged[-1]["role"] == "assistant"
    assert merged[-1]["content"] == "final assistant"


@pytest.mark.parametrize(
    "workspace_prefix",
    ("[Workspace::v1: /tmp/project]", "[Workspace: /tmp/project]"),
)
def test_large_merge_preserves_restamped_occurrence_of_repeated_prompt(workspace_prefix):
    """#7587: a later state.db occurrence of a repeated prompt is a distinct turn.

    Before #7587 the visible key was content-only, so this state.db row
    collapsed onto the older same-content sidecar row and was dropped from the
    projection -- the exact data loss the issue reports.  Occurrence identity
    now separates them; under-dedup (a repeated row) is the accepted trade.
    """
    import api.models as models

    sidecar = [
        {
            "role": "user" if idx % 2 == 0 else "assistant",
            "content": f"message {idx}",
            "timestamp": float(idx),
        }
        for idx in range(1000)
    ]
    sidecar.insert(
        500,
        {"role": "user", "content": "canonical prompt", "timestamp": 499.5},
    )
    sidecar.append(
        {"role": "assistant", "content": "final assistant", "timestamp": 1001.0}
    )
    state_replay = [
        {
            "role": "user",
            "content": f"{workspace_prefix}\ncanonical prompt",
            "timestamp": 2000.0,
        }
    ]

    merged = models.merge_session_messages_append_only(sidecar, state_replay)

    assert len(merged) == len(sidecar) + 1
    assert merged[-1] is state_replay[0]
    assert sum(
        1
        for m in merged
        if m.get("role") == "user" and "canonical prompt" in str(m.get("content"))
    ) == 2


def test_large_merge_preserves_literal_workspace_prefix_as_distinct_turn():
    import api.models as models

    trailing_text = "same trailing text"
    literal_sidecar_text = f"[Workspace: user typed this literally]\n{trailing_text}"
    sidecar = [
        {
            "role": "user" if idx % 2 == 0 else "assistant",
            "content": f"message {idx}",
            "timestamp": float(idx),
        }
        for idx in range(1000)
    ]
    sidecar.insert(
        500,
        {"role": "user", "content": literal_sidecar_text, "timestamp": 499.5},
    )
    sidecar.append(
        {"role": "assistant", "content": "stale assistant tail", "timestamp": 1001.0}
    )
    distinct_state_turn = {
        "role": "user",
        "content": f"[Workspace::v1: /tmp/actual]\n{trailing_text}",
        "timestamp": 2000.0,
    }

    merged = models.merge_session_messages_append_only(
        sidecar,
        [distinct_state_turn],
    )

    assert len(merged) == len(sidecar) + 1
    assert any(message.get("content") == literal_sidecar_text for message in merged)
    assert merged[-1] is distinct_state_turn


def test_small_visible_set_preserves_fuzzy_recovery():
    import api.models as models

    keys = {("assistant", "canonical answer", "")}
    fuzzy_only = ("assistant", "canonical answer with suffix", "")

    assert models._matching_visible_duplicate(fuzzy_only, keys) in keys

def _missing_clock_visible_keys(count: int, established_occurrence: str = "500") -> set[tuple]:
    """Large visible set with a canonical prompt row that carries a clock."""
    keys = {("assistant", f"message {idx}", "") for idx in range(count)}
    keys.add(("user", "canonical prompt", "", established_occurrence))
    return keys


def test_over_bound_missing_clock_replay_collapses_exactly():
    """#8672 gate r2: over the fuzzy bound, a row whose clock is missing must
    still collapse onto an exact identity replay that carries a clock.

    The #7587 occurrence discriminator puts the visible key of the clock-less
    row at "" and the state.db mirror at its timestamp, so set-membership
    fails; before the clock-optional replay probe the fuzzy fallback
    short-circuited at the >1000-key work bound and the merge appended the
    replay as a duplicate row.
    """
    import api.models as models

    keys = _missing_clock_visible_keys(1001)
    lookup = models._build_visible_duplicate_lookup(keys)

    replay = ("user", "canonical prompt", "", "")  # sidecar row: missing clock
    matched = models._matching_visible_duplicate(replay, keys, lookup)
    assert matched is not None
    assert matched[:3] == ("user", "canonical prompt", "")
    assert matched[3] == "500"  # collapses onto the clocked mirror, not a new row


def test_over_bound_established_distinct_occurrences_stay_separate():
    """#8672 gate r2: the clock-optional replay probe must NEVER merge two
    established different occurrences -- the exact #7587 data-loss fix."""
    import api.models as models

    keys = _missing_clock_visible_keys(1001, established_occurrence="500")
    lookup = models._build_visible_duplicate_lookup(keys)

    other_occurrence = ("user", "canonical prompt", "", "501")
    assert models._matching_visible_duplicate(other_occurrence, keys, lookup) is None


def test_over_bound_lookupless_probe_keeps_fuzzy_bound(monkeypatch):
    """#8672 gate r2: without a prebuilt lookup the fuzzy work bound is never
    silently lifted -- a lookup-less over-bound probe still returns None and
    never builds the fuzzy index."""
    import api.models as models

    keys = _missing_clock_visible_keys(1001)
    # A distinct absent occurrence: set-membership misses, and without a prebuilt
    # lookup the over-bound probe must return None without building the index.
    with_clock = ("user", "canonical prompt", "", "999")

    def unexpected_lookup(_keys):
        raise AssertionError("large visible sets must not build the fuzzy lookup")

    monkeypatch.setattr(models, "_build_visible_duplicate_lookup", unexpected_lookup)
    assert models._matching_visible_duplicate(with_clock, keys) is None


def test_sqlite_active_suffix_missing_clock_replay_not_duplicated(tmp_path, monkeypatch):
    """#8672 gate r2 regression (active-suffix / missing-clock case).

    A large live SQLite transcript whose first 500 rows are INACTIVE feeds a
    502-row active projection.  The sentinel user row (sidecar index 500) has
    NO timestamp in the sidecar but carries timestamp 500 in state.db, and the
    default projection does not export a private row ID.  The reconciled
    display transcript must contain exactly one copy of that row -- before the
    clock-optional replay probe, the >1000-key fuzzy bound short-circuited
    before the missing-clock compatibility rule and the in-window user path
    appended a duplicate (1003 rows).
    """
    import sqlite3

    import api.models as models
    from api.models import (
        Session,
        get_state_db_session_messages,
        reconciled_state_db_messages_for_session,
    )

    db = tmp_path / "state.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE messages (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          session_id TEXT NOT NULL,
          role TEXT, content TEXT, timestamp REAL, active INTEGER,
          tool_calls TEXT, api_content TEXT
        )
        """
    )
    n = 1002
    rows = []
    for i in range(n):
        role = "user" if i % 2 == 0 else "assistant"
        rows.append(("sid", role, f"message {i}", float(i), 1 if i >= 500 else 0, None, None))
    conn.executemany(
        "INSERT INTO messages (session_id, role, content, timestamp, active, tool_calls, api_content) VALUES (?,?,?,?,?,?,?)",
        rows,
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(models, "_active_state_db_path", lambda: db)

    # Production active-row reader: first 500 rows inactive -> 502 rows.
    state_read = get_state_db_session_messages("sid")
    assert len(state_read) == 502
    sentinel = [m for m in state_read if m.get("content") == "message 500"][0]
    assert sentinel.get("timestamp") == 500.0
    assert not any(k.startswith("_state_db") for k in sentinel), "no private row id"

    # Sidecar transcript: index-500 user row has no timestamp at all.
    sidecar = []
    for i in range(n):
        role = "user" if i % 2 == 0 else "assistant"
        row = {"role": role, "content": f"message {i}"}
        if not (i == 500 and role == "user"):
            row["timestamp"] = float(i)
        sidecar.append(row)

    session = Session(session_id="sid", messages=list(sidecar), context_messages=list(sidecar))
    reconciled = reconciled_state_db_messages_for_session(session, prefer_context=False)

    assert len(reconciled) == n, f"reconciled={len(reconciled)} (expected {n})"
    copies = [m for m in reconciled if m.get("content") == "message 500" and m.get("role") == "user"]
    assert len(copies) == 1, f"sentinel user row duplicated: {len(copies)} copies"



def test_over_bound_missing_clock_structured_replay_collapses_exactly():
    """#8672 gate r2: structured (non-string) content is exact-identity only in
    this probe, and a missing-clock structured replay still collapses onto its
    clocked exact twin over the fuzzy bound.  Established distinct occurrences
    stay separate."""
    import api.models as models

    keys = {("assistant", f"message {idx}", "") for idx in range(1001)}
    structured = models._content_identity_for_key(["structured", "payload"])
    keys.add(("user", structured, "", "500"))
    lookup = models._build_visible_duplicate_lookup(keys)

    missing_clock = ("user", structured, "", "")
    assert models._matching_visible_duplicate(missing_clock, keys, lookup) is not None
    distinct = ("user", structured, "", "501")
    assert models._matching_visible_duplicate(distinct, keys, lookup) is None



def test_small_transcript_missing_clock_structured_replay_collapses_exactly():
    """Greptile finding #1: image turns appear twice.

    A structured (image) row whose sidecar copy carries a clock and whose
    state.db copy does not must still collapse on a SMALL transcript.  Below
    the fuzzy bound the structured branch used to return before any
    clock-optional probe ran, so the merge appended the same image turn again.
    """
    import api.models as models

    structured = models._content_identity_for_key(["image", "payload"])
    keys = {("assistant", "hello", "", "")}  # small: far below the fuzzy bound
    keys.add(("user", structured, "", "500"))
    lookup = models._build_visible_duplicate_lookup(keys)

    missing_clock = ("user", structured, "", "")
    matched = models._matching_visible_duplicate(missing_clock, keys, lookup)
    assert matched is not None
    assert matched[3] == "500", "must collapse onto the clocked image twin"

    # A genuinely different established occurrence still stays separate.
    distinct = ("user", structured, "", "501")
    assert models._matching_visible_duplicate(distinct, keys, lookup) is None


def test_repeated_prompt_group_probe_is_constant_work(monkeypatch):
    """Greptile finding #2: repeated prompts slow loading.

    The clock-free probe used to scan every occurrence of the same content for
    each incoming row, bypassing the fuzzy work bound.  Thousands of repeated
    prompts at distinct timestamps made one row cost O(occurrences).  The
    prebuilt group must answer in constant work.
    """
    import api.models as models

    repeats = 400
    keys = {("user", "are you done yet", "", str(1000 + idx)) for idx in range(repeats)}
    keys |= {
        ("assistant", f"filler {idx}", "", str(5000 + idx))
        for idx in range(models._VISIBLE_DUPLICATE_FUZZY_MAX_KEYS + 1 - repeats)
    }
    assert len(keys) > models._VISIBLE_DUPLICATE_FUZZY_MAX_KEYS
    lookup = models._build_visible_duplicate_lookup(keys)

    calls = {"n": 0}
    original_share = models._visible_keys_share_occurrence

    def counting_share(left, right):
        calls["n"] += 1
        return original_share(left, right)

    monkeypatch.setattr(models, "_visible_keys_share_occurrence", counting_share)

    fresh = ("user", "are you done yet", "", "9999")  # a new, genuinely distinct occurrence
    assert models._matching_visible_duplicate(fresh, keys, lookup) is None
    assert calls["n"] == 0, (
        "the group probe must not scan occurrences per incoming row "
        f"(saw {calls['n']} comparisons for {repeats} repeats)"
    )

    # The clock-less wildcard still collapses onto the group's representative.
    missing_clock = ("user", "are you done yet", "", "")
    matched = models._matching_visible_duplicate(missing_clock, keys, lookup)
    assert matched is not None
