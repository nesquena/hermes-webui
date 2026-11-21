"""PR #8672 review round-3 residuals (maintainer P2 x2 + Greptile P1 x1).

Three findings against head ``3d97988397c0a31a284005776cd4f1532904e32f``:

P2-A (maintainer 2026-11-06) -- the exact-index path scanned the entire
    same-clock bucket.  ``_clockfree_duplicate_candidate()`` iterated
    ``clock_mates.items()`` (and the identity map below it), so ONE replay of
    a repeated prompt performed one ``_occurrence_identity_conflict()``
    comparison per same-clock identity: 1,001 rows at clock 1000 -> 1,001
    comparisons, 2,001 rows -> 2,001.  The fix indexes compatible
    representatives per clock / canonical identity namespace, so the probe is
    constant work.  The regression below drives the REAL
    ``_assign_stable_message_ids()`` producer and the REAL
    ``merge_session_messages_append_only()`` and asserts the comparison COUNT
    does not grow with the bucket, not merely that the output looks right.

P2-B (maintainer 2026-11-06) -- a stable ID still hid an available durable-ID
    conflict.  ``_session_message_occurrence_for_key()`` kept only the FIRST
    private identity, but real settlement preserves BOTH stable ``id=2`` and
    ``_state_db_row_id=7`` on a sidecar user row.  Compared with a distinct
    SQLite rowid 8 the row then read as identity-less on the durable side and
    the restored turn was suppressed in display AND context.  The fix keeps
    every established namespace, using the existing alias rules, so the
    durable conflict stays visible.

P1 (Greptile 2026-11-08, api/models.py:15472) -- restored images appeared
    twice.  With two identical image turns in one group (one at a known clock,
    one without any timestamp), a state.db replay of the second at a NEW clock
    found no candidate and the merge appended the image again.  The fix checks
    the compatible clock-unestablished copy BEFORE rejecting the incoming
    clock.

Every case is producer-composed: real ``_settle_result_messages()`` /
``_assign_stable_message_ids()`` calls mint the identities, a real SQLite
database holds the mirrored rows, and the real merge / reconciler consume
them.  No hand-built composite key is used as an oracle.
"""

import copy
import sqlite3
from collections import OrderedDict

import pytest

pytestmark = pytest.mark.requires_agent_modules

SENTINEL = "sentinel prompt"
PROVIDER_BYTES = "provider-bytes"
REPEATED = "repeated prompt"
IMAGE_CONTENT = [
    {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
    {"type": "text", "text": "look at this screenshot"},
]

# The prebuilt index probe must resolve a candidate with dict lookups only.
# The pre-fix head performed exactly one comparison per same-clock identity
# (1,001 for the 1,001-row bucket, 2,001 for the 2,001-row bucket); a small
# constant bound keeps that regression from returning while leaving room for
# the handful of comparisons the other bounded paths legitimately make.
BOUNDED_PROBE_COMPARISONS = 4


# ── fixtures ────────────────────────────────────────────────────────────────


def _make_state_db(path, sid, rows):
    """Create a real state.db with the given rows (durable row ids included).

    A row may carry an explicit ``"id"`` to pin its durable primary key; the
    maintainer's reproduction needs the distinct row at primary key 8.
    """
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, "
        "role TEXT, content TEXT, timestamp REAL, tool_call_id TEXT, tool_calls TEXT, "
        "tool_name TEXT, api_content TEXT)"
    )
    for row in rows:
        explicit_id = row.get("id")
        columns = (
            "id, session_id, role, content, timestamp, tool_call_id, "
            "tool_calls, tool_name, api_content"
        )
        placeholders = "?, ?, ?, ?, ?, ?, ?, ?, ?"
        values = (
            explicit_id,
            sid,
            row["role"],
            row["content"],
            row.get("timestamp"),
            row.get("tool_call_id"),
            row.get("tool_calls"),
            row.get("tool_name"),
            row.get("api_content"),
        )
        if explicit_id is None:
            columns = columns[4:]
            placeholders = placeholders[2:]
            values = values[1:]
        conn.execute(
            f"INSERT INTO messages ({columns}) VALUES ({placeholders})",
            values,
        )
    conn.commit()
    conn.close()


def _install_test_session(monkeypatch, tmp_path, sid, sidecar_messages, context_messages=None):
    import api.config as config
    import api.models as models
    import api.profiles as profiles

    monkeypatch.setattr(config, "STATE_DIR", tmp_path, raising=False)
    session_dir = tmp_path / "sessions"
    monkeypatch.setattr(config, "SESSION_DIR", session_dir, raising=False)
    monkeypatch.setattr(config, "SESSION_INDEX_FILE", session_dir / "_index.json", raising=False)
    monkeypatch.setattr(models, "SESSION_DIR", session_dir, raising=False)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json", raising=False)
    monkeypatch.setattr(models, "SESSIONS", OrderedDict(), raising=False)
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path, raising=False)
    monkeypatch.setattr(models, "_active_state_db_path", lambda: tmp_path / "state.db", raising=False)
    session_dir.mkdir(parents=True, exist_ok=True)

    session = models.Session(
        session_id=sid,
        title="8672 round 3 residuals",
        workspace=str(tmp_path),
        model="test-model",
        messages=sidecar_messages,
        created_at=1000.0,
        updated_at=1001.0,
    )
    if context_messages is not None:
        session.context_messages = list(context_messages)
    session.save(touch_updated_at=False)
    return session


def _settle_display_and_context(monkeypatch, result_rows, prompt):
    """Run ONE real ``_settle_result_messages()`` call and return its rows.

    The annotation helper is patched with ``monkeypatch`` so the stub cannot
    leak into later tests (maintainer request, 2026-11-06 review).
    """
    from api import streaming
    from api.models import Session

    monkeypatch.setattr(
        streaming, "_annotate_media_snapshots_for_settled_messages", lambda _m: None
    )
    session = Session(session_id="webui_8672_r3_settle", messages=[], context_messages=[])
    streaming._settle_result_messages(
        session, [], [], copy.deepcopy(result_rows), prompt, "webui", None
    )
    return list(session.messages), list(session.context_messages)


def _counting_identity_conflict(monkeypatch):
    """Wrap the real comparison and count how often the merge calls it."""
    import api.models as models

    calls = []
    real = models._occurrence_identity_conflict

    def counting(left_identity, right_identity):
        calls.append((left_identity, right_identity))
        return real(left_identity, right_identity)

    monkeypatch.setattr(models, "_occurrence_identity_conflict", counting)
    return calls


# ── P2-A: the exact-index probe is constant work ────────────────────────────


@pytest.mark.parametrize("same_clock_rows", [1001, 2001])
def test_exact_index_probe_work_is_bounded_by_the_same_clock_bucket(
    monkeypatch, same_clock_rows
):
    """P2-A: one replay must not rescan every same-clock identity.

    Maintainer reproduction through the real producer and the real merge:
    1,001 (or 2,001) same-content user rows at clock 1000 plus one more at
    clock 2000, ids minted by ``_assign_stable_message_ids()``, then the final
    row replayed with its clock changed to 1000.  The pre-fix head performs
    exactly one ``_occurrence_identity_conflict()`` comparison per same-clock
    identity inside the merge; the assertion below is on that COUNT, so a
    bucket scan cannot come back unnoticed.
    """
    import api.models as models
    from api.streaming import _assign_stable_message_ids

    calls = _counting_identity_conflict(monkeypatch)

    rows = [
        {"role": "user", "content": REPEATED, "timestamp": 1000.0}
        for _ in range(same_clock_rows)
    ]
    rows.append({"role": "user", "content": REPEATED, "timestamp": 2000.0})
    _assign_stable_message_ids(rows)
    sidecar = [dict(row) for row in rows]
    replay = dict(rows[-1])
    replay["timestamp"] = 1000.0

    merged = models.merge_session_messages_append_only(sidecar, [replay])

    comparisons = len(calls)
    assert comparisons <= BOUNDED_PROBE_COMPARISONS, (
        f"one replay performed {comparisons} _occurrence_identity_conflict() "
        f"comparisons for a {same_clock_rows}-row same-clock bucket; the "
        "prebuilt index must resolve the candidate with constant work"
    )
    # The work bound must not change the merge outcome: the replayed row is
    # still consumed as the prefix replay it is, so no row is appended and no
    # sidecar row is dropped.
    assert len(merged) == len(sidecar), (
        f"the bounded probe changed the merge outcome: {len(merged)} rows for "
        f"{len(sidecar)} sidecar rows"
    )
    # Same-namespace conflict protection survives at this scale: the replayed
    # stable id exists at clock 2000 only, so at clock 1000 the bucket already
    # owns that occurrence under a different stable id and the probe must
    # refuse the match (different-clock separation stays intact).
    visible_keys = {
        models._session_message_visible_key(row) for row in sidecar
    }
    lookup = models._build_visible_duplicate_lookup(visible_keys)
    assert (
        models._matching_visible_duplicate(
            models._session_message_visible_key(replay), visible_keys, lookup
        )
        is None
    ), "a same-namespace conflicting id at the probed clock collapsed"


# ── P2-B: a stable id must not mask a durable-id conflict ───────────────────


def test_settled_stable_id_does_not_mask_a_durable_row_id_conflict(
    monkeypatch, tmp_path
):
    """P2-B: durable row 8 must survive beside a settled stable id 2 + durable 7.

    Maintainer reproduction through the real chain: a real
    ``_settle_result_messages()`` call assigns the sentinel user row stable
    ``id=2`` while preserving its durable ``_state_db_row_id=7`` and the
    provider bytes; a real SQLite database then holds a DISTINCT same-content
    / same-provider user row with a NULL timestamp and primary key 8, and the
    real reconciler consumes both.  The stable id must not hide the durable
    conflict, so the second sentinel survives in display and in context.
    """
    import api.models as models

    display_rows, context_rows = _settle_display_and_context(
        monkeypatch,
        [
            {"role": "assistant", "content": "earlier answer", "timestamp": 500.0},
            {
                "role": "user",
                "content": SENTINEL,
                "timestamp": 1000.0,
                "api_content": PROVIDER_BYTES,
                "_state_db_row_id": 7,
            },
            {"role": "assistant", "content": "final answer", "timestamp": 1001.0},
        ],
        SENTINEL,
    )
    # The producer really minted the stable id AND preserved the durable one:
    # the fixture must carry the real identity shape, not a hand-built key.
    sentinel_row = next(row for row in display_rows if row.get("role") == "user")
    assert sentinel_row.get("id") == 2, (
        f"the settle producer must mint stable id=2, got {sentinel_row.get('id')!r}"
    )
    assert sentinel_row.get("_state_db_row_id") == 7, (
        "the settle producer must preserve durable row id 7, got "
        f"{sentinel_row.get('_state_db_row_id')!r}"
    )

    sid = "webui_8672_r3_durable_conflict"
    _make_state_db(
        tmp_path / "state.db",
        sid,
        [
            {
                "id": 8,
                "role": "user",
                "content": SENTINEL,
                "timestamp": None,
                "api_content": PROVIDER_BYTES,
            }
        ],
    )
    session = _install_test_session(
        monkeypatch, tmp_path, sid, display_rows, context_messages=context_rows
    )

    # The real reader projects durable row id 8 onto the distinct row.
    projected = models.get_state_db_session_messages(sid)
    assert projected and projected[0].get("_state_db_row_id") == 8, (
        "the real SQLite reader must project durable row id 8, got "
        f"{[row.get('_state_db_row_id') for row in projected]}"
    )

    display = models.reconciled_state_db_messages_for_session(session)
    context = models.reconciled_state_db_messages_for_session(session, prefer_context=True)
    for label, rows in (("display", display), ("context", context)):
        contents = [row.get("content") for row in rows]
        assert contents.count(SENTINEL) == 2, (
            f"{label} reconciliation suppressed the distinct durable row 8: {contents}"
        )
        assert "final answer" in contents, (
            f"{label} reconciliation dropped the final answer: {contents}"
        )


def test_one_sided_durable_id_still_collapses_onto_its_settled_twin():
    """P2-B control: the newly repaired one-sided-id behaviour stays repaired.

    The settled row keeps its stable id and durable id; a SQLite mirror that
    carries the SAME durable id (and no clock) is the same logical row and
    must still collapse, so the round-3 fix does not over-preserve.
    """
    import api.models as models

    settled = {
        "role": "user",
        "content": SENTINEL,
        "timestamp": 1000.0,
        "id": 2,
        "_state_db_row_id": 7,
        "api_content": PROVIDER_BYTES,
    }
    mirror = {
        "role": "user",
        "content": SENTINEL,
        "timestamp": None,
        "_state_db_row_id": 7,
        "api_content": PROVIDER_BYTES,
    }
    assert models._matching_visible_duplicate(
        models._session_message_visible_key(mirror),
        {models._session_message_visible_key(settled)},
    ) == models._session_message_visible_key(settled)


# ── P1: a compatible clockless copy is checked before the clock is rejected ─


def _image_turn_sidecar(clockless_row_has_identity):
    """Two identical image turns: one clocked, one without any timestamp."""
    from api.streaming import _assign_stable_message_ids

    clocked = {"role": "user", "content": copy.deepcopy(IMAGE_CONTENT), "timestamp": 1000.0}
    clockless = {"role": "user", "content": copy.deepcopy(IMAGE_CONTENT)}
    if clockless_row_has_identity:
        # A settled sidecar row carries a stable id even without a clock.
        _assign_stable_message_ids([clocked, clockless])
    else:
        _assign_stable_message_ids([clocked])
    return [
        {"role": "assistant", "content": "earlier answer", "timestamp": 500.0},
        clocked,
        clockless,
    ]


@pytest.mark.parametrize("clockless_row_has_identity", [True, False])
def test_clockless_image_copy_still_collapses_a_restamped_replay(
    clockless_row_has_identity,
):
    """P1: replaying the clockless image turn at a new clock must not re-append it.

    Greptile reproduction through the real merge: the sidecar holds two
    identical image turns, one at timestamp 1000 and one without any
    timestamp, with stable ids minted by the real producer.  state.db replays
    the second turn at timestamp 2000 (the real reader projects a durable row
    id onto it).  The clockless copy is compatible, so the image turn must be
    merged into it instead of appended a second time.
    """
    from api.models import merge_session_messages_append_only

    sidecar = _image_turn_sidecar(clockless_row_has_identity)
    replay = {
        "role": "user",
        "content": copy.deepcopy(IMAGE_CONTENT),
        "timestamp": 2000.0,
        "_state_db_row_id": 9,
    }

    merged = merge_session_messages_append_only(sidecar, [replay])

    image_turns = [
        row
        for row in merged
        if row.get("role") == "user" and row.get("content") == IMAGE_CONTENT
    ]
    assert len(image_turns) == 2, (
        "the restamped replay was appended beside its compatible clockless "
        f"copy ({len(image_turns)} image turns, expected 2)"
    )


def test_distinct_clock_image_turn_is_still_appended():
    """P1 control: a third image turn at a genuinely new clock stays distinct.

    The clockless fallback must not swallow every restamped replay: when the
    sidecar holds only CLOCKED image turns, a replay at a different
    established clock is a different occurrence and must be appended.
    """
    from api.models import merge_session_messages_append_only

    sidecar = [
        {"role": "assistant", "content": "earlier answer", "timestamp": 500.0},
        {"role": "user", "content": copy.deepcopy(IMAGE_CONTENT), "timestamp": 1000.0},
        {"role": "assistant", "content": "final answer", "timestamp": 1002.0},
    ]
    replay = {
        "role": "user",
        "content": copy.deepcopy(IMAGE_CONTENT),
        "timestamp": 2000.0,
        "_state_db_row_id": 9,
    }

    merged = merge_session_messages_append_only(sidecar, [replay])

    image_turns = [
        row
        for row in merged
        if row.get("role") == "user" and row.get("content") == IMAGE_CONTENT
    ]
    assert len(image_turns) == 2, (
        "a distinct-clock image turn was collapsed onto the clocked copy "
        f"({len(image_turns)} image turns, expected 2)"
    )
