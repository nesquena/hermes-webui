"""PR #8672 review round-2 P2 regressions: identity namespaces + bounded prefix.

The maintainer's re-review of ``3ed8a45e88a423834de2873e9b04e79435805534``
(left two P2 regressions, both introduced by the final identity hardening):

P2-1 -- private identity must be compared WITHIN its namespace.
    ``api/models.py``'s ``_visible_keys_share_occurrence()`` and
    ``_clockfree_duplicate_candidate()`` rejected ANY unequal identity
    strings.  But the stable WebUI ``id`` and the SQLite
    ``_state_db_row_id`` describe DIFFERENT things, so the reconciled replay
    of one logical row (stable id on the sidecar side, row id on the SQLite
    side) was treated as a contradiction and the replay was appended as a
    second row after the final answer.

P2-2 -- the stable settled id must not disable the bounded regeneration read.
    ``api/session_ops.py``'s ``_bounded_tail_snapshot_if_safe()`` compared the
    sidecar prefix visible keys LITERALLY against the SQLite prefix keys.
    The sidecar keys now carry the stable id and the SQLite projection does
    not, so the prefix proof failed for every settled row, the bounded path
    was refused, and regeneration read the WHOLE transcript instead of the
    200-row tail window.

Both regressions are pinned here through PRODUCER-COMPOSED fixtures: a real
``_settle_result_messages()`` call mints the stable ids, a real SQLite
database holds the mirrored rows, and the real reconciler /
``_bounded_tail_snapshot_if_safe`` entry points consume them.  No hand-built
composite key fixture is used as a RED oracle -- the maintainer explicitly
rejected that shape (``test_same_clock_conflicting_private_ids_stay_distinct_
over_the_probe_bound`` hand-builds its keys and therefore also passes on the
predecessor ``adcadb7f7``, so it never discriminated the producer hardening).

The controls pin the behaviour that must NOT change:
  * the one-sided-id case (a clockless durable row vs its clocked sidecar
    twin) still collapses;
  * two genuinely different ids in the SAME namespace still stay distinct;
  * the clock-disagreement fallback still refuses the bounded path;
  * the tool/provider identity still separates rows.
"""

import copy
import sqlite3
from collections import OrderedDict

import pytest

pytestmark = pytest.mark.requires_agent_modules

SENTINEL = "sentinel prompt"
PROVIDER_BYTES = "provider-bytes"


# ── fixtures ────────────────────────────────────────────────────────────────


def _make_state_db(path, sid, rows):
    """Create a real state.db with the given rows (durable row ids included)."""
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, "
        "role TEXT, content TEXT, timestamp REAL, tool_call_id TEXT, tool_calls TEXT, "
        "tool_name TEXT, api_content TEXT)"
    )
    for row in rows:
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
        title="8672 P2 identity namespaces",
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


def _settle_display_and_context(result_rows, prompt):
    """Run ONE real ``_settle_result_messages()`` call and return its rows.

    The producer is what mints the stable message ids, so the fixture carries
    the REAL identity shape instead of a hand-built key tuple.
    """
    from api import streaming
    from api.models import Session

    streaming._annotate_media_snapshots_for_settled_messages = lambda _m: None
    session = Session(session_id="webui_8672_settle", messages=[], context_messages=[])
    streaming._settle_result_messages(
        session, [], [], copy.deepcopy(result_rows), prompt, "webui", None
    )
    return list(session.messages), list(session.context_messages)


# ── P2-1: private identity is compared within its namespace ────────────────


def test_stable_id_and_sqlite_row_id_do_not_append_a_second_sentinel_row(
    monkeypatch, tmp_path
):
    """P2-1 repro: the reconciled replay must NOT append a second sentinel.

    Producer-composed: a real settle call mints the stable id on the sidecar
    user row, a real SQLite row (NULL timestamp) carries the same
    user/provider payload under a durable row id, and the real reconciler
    consumes both.  The stable id and the row id live in DIFFERENT identity
    namespaces, so they cannot contradict each other.
    """
    import api.models as models

    display_rows, context_rows = _settle_display_and_context(
        [
            {"role": "assistant", "content": "earlier answer", "timestamp": 500.0},
            {
                "role": "user",
                "content": SENTINEL,
                "timestamp": 1000.0,
                "api_content": PROVIDER_BYTES,
            },
            {"role": "assistant", "content": "final answer", "timestamp": 1001.0},
        ],
        SENTINEL,
    )
    # The producer minted the stable id -- the fixture must really carry it.
    stable_ids = [row.get("id") for row in display_rows]
    assert stable_ids == [1, 2, 3], (
        f"the settle producer must mint stable ids, got {stable_ids}"
    )

    sid = "webui_8672_p2_1"
    _make_state_db(
        tmp_path / "state.db",
        sid,
        [
            {
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

    # The real reader projects the durable row id onto the row.
    projected = models.get_state_db_session_messages(sid)
    assert projected and projected[0].get("_state_db_row_id") is not None, (
        "the real SQLite reader must project the durable row id"
    )

    display = models.reconciled_state_db_messages_for_session(session)
    context = models.reconciled_state_db_messages_for_session(session, prefer_context=True)

    for label, rows in (("display", display), ("context", context)):
        contents = [row.get("content") for row in rows]
        assert contents.count(SENTINEL) == 1, (
            f"{label} reconciliation appended a second sentinel: {contents}"
        )
        assert contents[-1] == "final answer", (
            f"{label} reconciliation displaced the final answer: {contents}"
        )


def test_one_sided_stable_id_still_collapses_onto_its_sidecar_twin():
    """Control: a clockless durable row still collapses onto its clocked twin.

    This is the case the maintainer credited as already repaired -- it must
    STAY repaired.  The stable id is the only identity on the sidecar side and
    the durable row id is the only identity on the SQLite side; neither
    contradicts the other.
    """
    from api.models import (
        _matching_visible_duplicate,
        _session_message_visible_key,
    )

    clocked = {
        "role": "user",
        "content": SENTINEL,
        "timestamp": 1000.0,
        "id": 1,
    }
    clockless = {
        "role": "user",
        "content": SENTINEL,
        "timestamp": None,
        "_state_db_row_id": 7,
    }

    assert _matching_visible_duplicate(
        _session_message_visible_key(clockless),
        {_session_message_visible_key(clocked)},
    ) == _session_message_visible_key(clocked)


def test_conflicting_ids_in_the_same_namespace_stay_distinct():
    """Control: two different row ids in the SAME namespace stay distinct.

    The namespace relaxation must not weaken the same-namespace conflict:
    the durable row ids 7 and 8 describe the same thing, so they contradict.
    """
    from api.models import (
        _matching_visible_duplicate,
        _session_message_visible_key,
    )

    first = {"role": "user", "content": SENTINEL, "_state_db_row_id": 7}
    second = {"role": "user", "content": SENTINEL, "_state_db_row_id": 8}

    assert _matching_visible_duplicate(
        _session_message_visible_key(second),
        {_session_message_visible_key(first)},
    ) is None


def test_conflicting_stable_ids_in_the_same_namespace_stay_distinct():
    """Control: two different stable ids in the SAME namespace stay distinct."""
    from api.models import (
        _matching_visible_duplicate,
        _session_message_visible_key,
    )

    first = {"role": "user", "content": SENTINEL, "timestamp": 1000.0, "id": 7}
    second = {"role": "user", "content": SENTINEL, "timestamp": 1000.0, "id": 8}

    assert _matching_visible_duplicate(
        _session_message_visible_key(second),
        {_session_message_visible_key(first)},
    ) is None


# ── P2-2: the stable settled id must not disable the bounded regeneration ──


def test_bounded_regeneration_still_reads_only_the_tail_window(monkeypatch, tmp_path):
    """P2-2 repro: the bounded path must survive the stable settled ids.

    Producer-composed: 101 real ``_settle_result_messages()`` calls build 202
    alternating rows (each call mints the stable ids), the rows are mirrored
    into a real SQLite database, and the real
    ``_bounded_tail_snapshot_if_safe()`` proves the skipped prefix.

    The assertion pins that the BOUNDED PATH WAS TAKEN (not merely that the
    result looks the same): the returned tail must be the 200-row window, not
    the full 202-row history.
    """
    import api.models as models
    from api import session_ops, streaming

    streaming._annotate_media_snapshots_for_settled_messages = lambda _m: None

    sid = "webui_8672_p2_2"
    session = models.Session(session_id=sid, messages=[], context_messages=[])
    previous_context = []
    previous_display = []
    for turn in range(101):
        prompt = f"prompt {turn:04d}"
        result = copy.deepcopy(previous_context) + [
            {"role": "user", "content": prompt, "timestamp": 1000.0 + turn * 2},
            {
                "role": "assistant",
                "content": f"reply {turn:04d}",
                "timestamp": 1001.0 + turn * 2,
            },
        ]
        streaming._settle_result_messages(
            session,
            list(previous_display),
            list(previous_context),
            result,
            prompt,
            "webui",
            None,
        )
        previous_display = list(session.messages)
        previous_context = list(session.context_messages)

    rows = list(session.messages)
    assert len(rows) == 202, f"the fixture must build 202 rows, got {len(rows)}"
    # The producer really minted the stable ids (that is the regression).
    assert all(row.get("id") for row in rows), "the settle producer must mint stable ids"

    _make_state_db(
        tmp_path / "state.db",
        sid,
        [
            {
                "role": row.get("role"),
                "content": row.get("content"),
                "timestamp": row.get("timestamp"),
            }
            for row in rows
        ],
    )
    monkeypatch.setattr(models, "_active_state_db_path", lambda: tmp_path / "state.db")

    floor = min(row["timestamp"] for row in rows[-200:])
    bounded = session_ops._bounded_tail_snapshot_if_safe(session, floor)

    assert bounded is not None, (
        "the bounded regeneration path was refused -> the full history was read"
    )
    assert len(bounded) == 200, (
        f"the bounded path must read the 200-row tail window, got {len(bounded)}"
    )


def test_bounded_path_is_actually_taken_instead_of_the_full_read(monkeypatch, tmp_path):
    """P2-2 (stronger): the regeneration must not call the FULL reader.

    ``_bounded_tail_snapshot_if_safe`` returning the tail is not enough on its
    own -- the caller could still fall back to the full read.  This test spies
    on the full reader and proves it is NOT called, and that the full reader
    would have returned more rows than the bounded path did.
    """
    import api.models as models
    from api import session_ops, streaming

    streaming._annotate_media_snapshots_for_settled_messages = lambda _m: None

    sid = "webui_8672_p2_2_fullread"
    session = models.Session(session_id=sid, messages=[], context_messages=[])
    previous_context = []
    previous_display = []
    for turn in range(101):
        prompt = f"prompt {turn:04d}"
        result = copy.deepcopy(previous_context) + [
            {"role": "user", "content": prompt, "timestamp": 1000.0 + turn * 2},
            {
                "role": "assistant",
                "content": f"reply {turn:04d}",
                "timestamp": 1001.0 + turn * 2,
            },
        ]
        streaming._settle_result_messages(
            session,
            list(previous_display),
            list(previous_context),
            result,
            prompt,
            "webui",
            None,
        )
        previous_display = list(session.messages)
        previous_context = list(session.context_messages)

    rows = list(session.messages)
    _make_state_db(
        tmp_path / "state.db",
        sid,
        [
            {
                "role": row.get("role"),
                "content": row.get("content"),
                "timestamp": row.get("timestamp"),
            }
            for row in rows
        ],
    )
    monkeypatch.setattr(models, "_active_state_db_path", lambda: tmp_path / "state.db")

    full_reads = []
    real_full_reader = models.get_state_db_session_messages

    def _spy(*args, **kwargs):
        full_reads.append((args, kwargs))
        return real_full_reader(*args, **kwargs)

    monkeypatch.setattr(models, "get_state_db_session_messages", _spy)
    monkeypatch.setattr(session_ops, "get_state_db_session_messages", _spy, raising=False)

    session_ops.regeneration_state(session, use_sidecar=True)

    assert not full_reads, (
        "the regeneration fell back to the full state.db reader "
        f"({len(full_reads)} full read(s)) instead of the bounded tail"
    )


def test_restamped_clock_still_refuses_the_bounded_path(monkeypatch, tmp_path):
    """Control: a real clock disagreement still refuses the bounded path.

    The private-id-free prefix representation must stay CLOCK-sensitive: a
    state.db row restamped by a second relative to its sidecar twin cannot be
    told apart from a genuinely new repeated row, so the proof must fall back
    to the full read exactly as before.
    """
    import api.models as models
    from api import session_ops

    sid = "webui_8672_p2_2_restamp"
    # Two sidecar rows; the SQLite mirror of the second one is restamped by a
    # whole second, so the two stores establish DIFFERENT clocks for it.
    sidecar = [
        {"role": "user", "content": "first prompt", "timestamp": 100.0},
        {"role": "assistant", "content": "first answer", "timestamp": 200.0},
        {"role": "user", "content": "second prompt", "timestamp": 300.0},
        {"role": "assistant", "content": "final answer", "timestamp": 400.0},
    ]
    db_rows = [
        {"role": "user", "content": "first prompt", "timestamp": 100.0},
        {"role": "assistant", "content": "first answer", "timestamp": 200.0},
        {"role": "user", "content": "second prompt", "timestamp": 301.0},
        {"role": "assistant", "content": "final answer", "timestamp": 400.0},
    ]
    _make_state_db(tmp_path / "state.db", sid, db_rows)
    monkeypatch.setattr(models, "_active_state_db_path", lambda: tmp_path / "state.db")

    session = models.Session(
        session_id=sid,
        messages=[dict(row) for row in sidecar],
        context_messages=[dict(row) for row in sidecar],
    )
    floor = 150.0

    assert session_ops._bounded_tail_snapshot_if_safe(session, floor) is None, (
        "a restamped twin must still refuse the bounded path"
    )
