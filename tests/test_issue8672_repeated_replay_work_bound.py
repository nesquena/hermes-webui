"""PR #8672 review P2 (round 5): the repeated NULL-clock replay stays linear.

The maintainer's 2026-11-10 review reproduced a deterministic QUADRATIC work
bound in ``api/models.py:_clockfree_duplicate_candidate``.  Every NULL-clock
replay restarted the walk over ``by_identity.values()`` at the beginning, so
each incoming row re-visited every candidate whose replay capacity had already
been consumed.  Measured with a real stable-ID producer, a real SQLite reader
and the real merge (sidecar: one leading assistant plus N same-content,
different-clock user turns with a non-empty ``api_content``; state.db: those N
user rows replayed with NULL timestamps and real durable row ids):

    N=1001 -> 501501 candidate visits        (N(N+1)/2)
    N=2001 -> 2003001 candidate visits

The regression is counted WORK, not wall-clock noise and not data loss: the
merge kept the right rows in both cases.  The same replay WITH timestamps
resolves through the clock buckets in linear time, which is the control below.

Every case here is producer-composed -- the real ``_assign_stable_message_ids``
mints the stable ids, the real ``_stamp_missing_message_timestamps`` preserves
the legacy ``_ts`` clocks, a real SQLite database holds the replayed suffix and
the real reconciler consumes both stores.  No hand-built composite key is used
as an oracle.

The work count is taken by wrapping the two per-candidate predicates the
duplicate probes call for EVERY candidate they inspect
(``_visible_key_capacity_remaining`` / ``_visible_key_occurrence_compatible``):
the wrappers only count and delegate, so the merge's behaviour -- dict order,
values and returned candidates -- is untouched.  The O(1) index probes call
them a bounded number of times per incoming row, so the sum stays a faithful
upper bound on candidate visits.

The instrumentation also showed that the restart lives in a SECOND walk: below
the fuzzy bound (``_VISIBLE_DUPLICATE_FUZZY_MAX_KEYS``) the merge never reaches
the clock-free index probe at all and resolves the replay through the bounded
fuzzy loop over ``by_role[role]``, which restarted the same way.  Both walks
are therefore covered, above and below the bound.
"""

import sqlite3
from collections import OrderedDict

import pytest

pytestmark = pytest.mark.requires_agent_modules

PROLOGUE = "PROLOGUE"
PROMPT = "Q"
PROVIDER_BYTES = "provider-bytes"


def _linear_work_bound(n: int) -> int:
    """Linear candidate-visit budget for an N-row replay.

    The quadratic head needed ``N(N+1)/2`` visits (501501 at N=1001,
    2003001 at N=2001).  The repaired probe resolves every replay through the
    prebuilt index with a bounded number of candidate visits, so a generous
    linear multiple separates the two regimes by more than two orders of
    magnitude while leaving room for the O(1) index probes each row performs.
    """
    return 24 * n + 512


def _make_state_db(path, sid, rows):
    """Create a real state.db whose rows may pin their durable primary key."""
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, "
        "role TEXT, content TEXT, timestamp REAL, tool_call_id TEXT, tool_calls TEXT, "
        "tool_name TEXT, api_content TEXT)"
    )
    for row in rows:
        explicit_id = row.get("id")
        if explicit_id is None:
            conn.execute(
                "INSERT INTO messages (session_id, role, content, timestamp, tool_call_id, "
                "tool_calls, tool_name, api_content) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    sid,
                    row["role"],
                    row["content"],
                    row.get("timestamp"),
                    row.get("tool_call_id"),
                    row.get("tool_calls"),
                    row.get("tool_name"),
                    row.get("api_content"),
                ),
            )
        else:
            conn.execute(
                "INSERT INTO messages (id, session_id, role, content, timestamp, tool_call_id, "
                "tool_calls, tool_name, api_content) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    explicit_id,
                    sid,
                    row["role"],
                    row["content"],
                    row.get("timestamp"),
                    row.get("tool_call_id"),
                    row.get("tool_calls"),
                    row.get("tool_name"),
                    row.get("api_content"),
                ),
            )
    conn.commit()
    conn.close()


def _install_test_session(monkeypatch, tmp_path, sid, sidecar_messages):
    import api.config as config
    import api.models as models
    import api.profiles as profiles

    monkeypatch.setattr(config, "STATE_DIR", tmp_path, raising=False)
    session_dir = tmp_path / "sessions"
    monkeypatch.setattr(config, "SESSION_DIR", session_dir, raising=False)
    monkeypatch.setattr(
        config, "SESSION_INDEX_FILE", session_dir / "_index.json", raising=False
    )
    monkeypatch.setattr(models, "SESSION_DIR", session_dir, raising=False)
    monkeypatch.setattr(
        models, "SESSION_INDEX_FILE", session_dir / "_index.json", raising=False
    )
    monkeypatch.setattr(models, "SESSIONS", OrderedDict(), raising=False)
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path, raising=False)
    monkeypatch.setattr(
        models, "_active_state_db_path", lambda: tmp_path / "state.db", raising=False
    )
    session_dir.mkdir(parents=True, exist_ok=True)

    session = models.Session(
        session_id=sid,
        title="8672 repeated replay work bound",
        workspace=str(tmp_path),
        model="test-model",
        messages=sidecar_messages,
        created_at=1000.0,
        updated_at=1001.0,
    )
    session.save(touch_updated_at=False)
    return session


def _settled_sidecar(rows):
    """Run the REAL producers over saved transcript rows.

    ``_assign_stable_message_ids()`` is the WebUI stable-id producer and
    ``_stamp_missing_message_timestamps()`` is the real missing-timestamp
    stamper, which preserves the supported legacy ``_ts`` clock rows without
    adding a ``timestamp`` of its own.
    """
    from api.streaming import (
        _assign_stable_message_ids,
        _stamp_missing_message_timestamps,
    )

    rows = [dict(row) for row in rows]
    _assign_stable_message_ids(rows)
    stamped = _stamp_missing_message_timestamps(rows, now=9_000_000.0)
    return rows, stamped


def _replay_repeated_turns(monkeypatch, tmp_path, sid, *, clocked: bool, n: int):
    """Replay N same-content user turns through the real merge.

    The sidecar holds a leading assistant and then N user turns of identical
    content on distinct clocks, each carrying the same non-empty provider
    sidecar.  state.db replays those N turns; with ``clocked`` they keep their
    timestamps, otherwise their clocks are NULL and only the durable row ids
    (projected by the real reader because ``api_content`` is present) identify
    them.
    """
    import api.models as models

    sidecar, stamped = _settled_sidecar(
        [
            {"role": "assistant", "content": PROLOGUE, "timestamp": 500.0},
            *(
                {
                    "role": "user",
                    "content": PROMPT,
                    "_ts": 1000 + idx,
                    "api_content": PROVIDER_BYTES,
                }
                for idx in range(n)
            ),
        ]
    )
    assert stamped == 0, "the stamper added a timestamp to a supported _ts row"
    # The producer really minted one stable id per saved occurrence, so every
    # saved turn owns a private identity in the STABLE namespace.
    assert [row.get("id") for row in sidecar] == list(range(1, n + 2))

    _make_state_db(
        tmp_path / "state.db",
        sid,
        [
            {
                "role": "user",
                "content": PROMPT,
                "timestamp": (1000.0 + idx) if clocked else None,
                "api_content": PROVIDER_BYTES,
            }
            for idx in range(n)
        ],
    )
    session = _install_test_session(monkeypatch, tmp_path, sid, sidecar)

    counts = _count_candidate_visits(monkeypatch, models)
    display = models.reconciled_state_db_messages_for_session(session)
    return display, counts


def _count_candidate_visits(monkeypatch, models):
    """Count every candidate the duplicate probes inspect, changing nothing.

    Both predicates are called once per candidate the probes walk, so their
    call count is the work measure.  The wrappers only increment a counter and
    delegate, which keeps dict order, values and the chosen candidate intact.
    """
    counts = {"capacity": 0, "compatible": 0}
    real_capacity = models._visible_key_capacity_remaining
    real_compatible = models._visible_key_occurrence_compatible

    def counting_capacity(key, remaining):
        counts["capacity"] += 1
        return real_capacity(key, remaining)

    def counting_compatible(key, identities):
        counts["compatible"] += 1
        return real_compatible(key, identities)

    monkeypatch.setattr(models, "_visible_key_capacity_remaining", counting_capacity)
    monkeypatch.setattr(models, "_visible_key_occurrence_compatible", counting_compatible)
    return counts


@pytest.mark.parametrize("n", [601, 1001, 2001])
def test_null_clock_repeated_replay_stays_linear(monkeypatch, tmp_path, n):
    """N clockless replays of one repeated prompt must cost linear work.

    Every replayed row has to collapse onto its own saved occurrence (the
    transcript stays N+1 rows) AND the merge must not restart the candidate
    walk for each of them: the maintained cursor/index has to make the
    consumption amortized linear instead of the quadratic restart the review
    reproduced.  ``n=601`` stays below the fuzzy bound, so it exercises the
    fuzzy-loop walk; ``n=1001``/``n=2001`` are the review's own numbers above
    it, exercising the clock-free index probe.
    """
    sid = f"webui_8672_work_null_{n}"
    display, counts = _replay_repeated_turns(
        monkeypatch, tmp_path, sid, clocked=False, n=n
    )

    contents = [row.get("content") for row in display]
    assert contents == [PROLOGUE] + [PROMPT] * n, (
        f"the repeated-turn replay changed the merged rows: {len(contents)} rows"
    )

    visits = counts["capacity"] + counts["compatible"]
    bound = _linear_work_bound(n)
    assert visits <= bound, (
        f"the NULL-clock replay cost {visits} candidate visits for n={n} "
        f"(bound {bound}); the candidate walk restarted per incoming row"
    )


@pytest.mark.parametrize("n", [601, 1001, 2001])
def test_clocked_repeated_replay_stays_linear(monkeypatch, tmp_path, n):
    """Control: the same replay WITH timestamps stays linear.

    Each replayed row carries its clock, so the prebuilt clock buckets resolve
    it without walking the group's identity index.  The rows still have to
    collapse one-to-one, so the control proves the bound is not satisfied by
    dropping work the merge legitimately owes.
    """
    sid = f"webui_8672_work_clocked_{n}"
    display, counts = _replay_repeated_turns(
        monkeypatch, tmp_path, sid, clocked=True, n=n
    )

    contents = [row.get("content") for row in display]
    assert contents == [PROLOGUE] + [PROMPT] * n, (
        f"the clocked repeated-turn replay changed the merged rows: "
        f"{len(contents)} rows"
    )

    visits = counts["capacity"] + counts["compatible"]
    bound = _linear_work_bound(n)
    assert visits <= bound, (
        f"the clocked replay cost {visits} candidate visits for n={n} "
        f"(bound {bound})"
    )
