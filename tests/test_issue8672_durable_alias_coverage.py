"""PR #8672 round 4: the durable provenance aliases are consumed completely.

The review (nesquena-hermes, 2026-11-08) found that
``_OCCURRENCE_IDENTITY_FIELDS`` (api/models.py) read only ``id``,
``message_id`` and ``_state_db_row_id``, while the owning helper
``_state_db_row_identity_details()`` accepts ``_row_id``, ``_db_row_id`` and
``state_db_row_id`` as well.

The reproduction: a real stable-ID producer assigns ``id=2``, ``durable7`` is
held under one of the omitted aliases, and a real SQLite reader projects a
DISTINCT same-content / same-provider ``row8`` with a NULL timestamp.  The
display and the ``prefer_context=True`` context reconcilers returned ONE
sentinel instead of two -- the durable conflict was invisible, so the distinct
durable row collapsed onto the row that owned the durable id.  The canonical
``_state_db_row_id`` alias returned two.

Every case below is producer-composed: a real ``_settle_result_messages()``
call mints the stable id, a real SQLite database holds the mirrored rows, and
the real reconcilers consume them.  No hand-built composite key is used as an
oracle.

Each alias gets BOTH controls:

* CONFLICT -- durable 7 under the alias against a distinct durable row 8:
  the two rows are provably different and must both survive.
* AGREEMENT -- durable 7 under the alias against durable row 7: the same
  durable row under two spellings must still collapse onto one row.
"""

import copy
import sqlite3
from collections import OrderedDict

import pytest

pytestmark = pytest.mark.requires_agent_modules

SENTINEL = "sentinel prompt"
PROVIDER_BYTES = "provider-bytes"

# Every alias the owning helper accepts.  The canonical one is the control the
# review used; the other three were the gap.
DURABLE_ALIASES = ("_state_db_row_id", "_row_id", "_db_row_id", "state_db_row_id")
OMITTED_ALIASES = ("_row_id", "_db_row_id", "state_db_row_id")


def _other_alias(alias):
    """Return a durable alias that is NOT ``alias``.

    The two-alias cases need two DIFFERENT spellings, so the comparison
    partner must not be the alias under test.
    """
    for candidate in DURABLE_ALIASES:
        if candidate != alias:
            return candidate
    raise AssertionError("no second durable alias available")


# ── fixtures ────────────────────────────────────────────────────────────────


def _make_state_db(path, sid, rows):
    """Create a real state.db with the given rows (durable row ids included).

    A row may carry an explicit ``"id"`` to pin its durable primary key; the
    reproduction needs the distinct row at primary key 8.
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
        placeholders = ", ".join(["?"] * 9)
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
            placeholders = ", ".join(["?"] * 7)
            values = values[1:]
        conn.execute(
            f"INSERT INTO messages ({columns}) VALUES ({placeholders})", values
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
        title="8672 durable alias coverage",
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
    session = Session(session_id="webui_8672_r4_settle", messages=[], context_messages=[])
    streaming._settle_result_messages(
        session, [], [], copy.deepcopy(result_rows), prompt, "webui", None
    )
    return list(session.messages), list(session.context_messages)


def _settled_sentinel(monkeypatch, alias, durable_value):
    """Settle a real turn whose sentinel row carries ``durable_value``.

    The durable id is held under ``alias`` so the occurrence discriminator has
    to read that spelling to see the conflict.
    """
    sentinel_row = {
        "role": "user",
        "content": SENTINEL,
        "timestamp": 1000.0,
        "api_content": PROVIDER_BYTES,
    }
    sentinel_row[alias] = durable_value

    display_rows, context_rows = _settle_display_and_context(
        monkeypatch,
        [
            {"role": "assistant", "content": "earlier answer", "timestamp": 500.0},
            sentinel_row,
            {"role": "assistant", "content": "final answer", "timestamp": 1001.0},
        ],
        SENTINEL,
    )
    settled = next(row for row in display_rows if row.get("role") == "user")
    # The producer really minted the stable id AND preserved the durable one:
    # the fixture must carry the real identity shape, not a hand-built key.
    assert settled.get("id") == 2, (
        f"the settle producer must mint stable id=2, got {settled.get('id')!r}"
    )
    assert settled.get(alias) == durable_value, (
        f"the settle producer must preserve {alias}={durable_value!r}, got "
        f"{settled.get(alias)!r}"
    )
    return display_rows, context_rows


def _reconcile(monkeypatch, tmp_path, alias, durable_value, sqlite_row_id):
    """Reconcile a settled sidecar against a real SQLite row.

    ``sqlite_row_id`` is the durable primary key of the SQLite row.  It is the
    row's ``_state_db_row_id`` on the SQLite side, which is exactly what the
    real reader projects.
    """
    import api.models as models

    display_rows, context_rows = _settled_sentinel(monkeypatch, alias, durable_value)

    sid = f"webui_8672_r4_{alias.strip('_')}_{sqlite_row_id}"
    _make_state_db(
        tmp_path / "state.db",
        sid,
        [
            {
                "id": sqlite_row_id,
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

    # The real reader projects the durable row id onto the distinct row.
    projected = models.get_state_db_session_messages(sid)
    assert projected and projected[0].get("_state_db_row_id") == sqlite_row_id, (
        "the real SQLite reader must project durable row id "
        f"{sqlite_row_id}, got "
        f"{[row.get('_state_db_row_id') for row in projected]}"
    )

    display = models.reconciled_state_db_messages_for_session(session)
    context = models.reconciled_state_db_messages_for_session(session, prefer_context=True)
    return display, context


def _sentinel_count(rows):
    return [row.get("content") for row in rows].count(SENTINEL)


# ── CONFLICT: durable 7 under the alias vs a distinct durable row 8 ────────


@pytest.mark.parametrize("alias", DURABLE_ALIASES)
def test_durable_alias_conflict_keeps_the_distinct_sqlite_row(
    monkeypatch, tmp_path, alias
):
    """A durable conflict under ANY alias must keep both rows.

    The durable identity is held under ``alias`` on the sidecar side and the
    distinct durable row 8 arrives from the real SQLite reader.  The two rows
    are provably different, so neither the display nor the context projection
    may suppress the restored turn.
    """
    display, context = _reconcile(monkeypatch, tmp_path, alias, 7, 8)

    for label, rows in (("display", display), ("context", context)):
        contents = [row.get("content") for row in rows]
        assert _sentinel_count(rows) == 2, (
            f"{label} reconciliation suppressed the distinct durable row 8 "
            f"under {alias}: {contents}"
        )
        assert "final answer" in contents, (
            f"{label} reconciliation dropped the final answer: {contents}"
        )


# ── AGREEMENT: durable 7 under the alias vs the SAME durable row 7 ─────────


@pytest.mark.parametrize("alias", DURABLE_ALIASES)
def test_durable_alias_agreement_collapses_the_same_sqlite_row(
    monkeypatch, tmp_path, alias
):
    """The SAME durable row under a different alias spelling must collapse.

    The alias names one namespace, so the sidecar's ``durable 7`` and the
    SQLite projection's durable row 7 are ONE logical row.  Reading a subset
    of the aliases would either hide the durable identity (losing the
    conflict) or split one namespace into two spellings that read as a
    conflict -- the row would be duplicated in the transcript.
    """
    display, context = _reconcile(monkeypatch, tmp_path, alias, 7, 7)

    for label, rows in (("display", display), ("context", context)):
        contents = [row.get("content") for row in rows]
        assert _sentinel_count(rows) == 1, (
            f"{label} reconciliation duplicated one durable row under {alias}: "
            f"{contents}"
        )
        assert "final answer" in contents, (
            f"{label} reconciliation dropped the final answer: {contents}"
        )


# ── one-sided compatibility: a durable id on ONE side only ─────────────────


@pytest.mark.parametrize("alias", DURABLE_ALIASES)
def test_durable_alias_one_sided_id_is_not_a_contradiction(alias):
    """A durable id on one side only must stay compatible.

    The real SQLite reader can return the same payload with a NULL timestamp
    while the sidecar copy carries the clock, and the durable id can be
    one-sided too.  A one-sided identity is NOT a second clock: it cannot
    establish difference, so the two rows must still collapse.  Reading more
    aliases must not turn that one-sided identity into a contradiction.
    """
    import api.models as models

    settled = {
        "role": "user",
        "content": SENTINEL,
        "timestamp": 1000.0,
        "id": 2,
        "api_content": PROVIDER_BYTES,
    }
    settled[alias] = 7
    # The mirror carries NO durable id at all -- the identity is one-sided.
    mirror = {
        "role": "user",
        "content": SENTINEL,
        "timestamp": None,
        "api_content": PROVIDER_BYTES,
    }
    assert models._matching_visible_duplicate(
        models._session_message_visible_key(mirror),
        {models._session_message_visible_key(settled)},
    ) == models._session_message_visible_key(settled)


# ── broken durable provenance must fail CLOSED, not open ───────────────────


@pytest.mark.parametrize(
    "broken",
    [
        {"_row_id": 7, "_state_db_row_id": 8},
        {"_state_db_row_id": True},
        {"_row_id": -4},
        {"_row_id": "abc"},
    ],
    ids=["contradictory", "bool", "negative", "non-numeric"],
)
def test_broken_durable_provenance_never_drops_the_distinct_row(
    monkeypatch, tmp_path, broken
):
    """Broken durable provenance must REFUSE to collapse, not read as "none".

    The owning helper reports such a row as invalid, so it names no durable
    row.  Reading that as "this row has no durable identity" would let it
    collapse onto a DIFFERENT durable row and silently drop a real turn --
    the failure mode #7587 exists to prevent.  The row must stay distinct
    from the distinct durable row 8.
    """
    import api.models as models

    sentinel_row = {
        "role": "user",
        "content": SENTINEL,
        "timestamp": 1000.0,
        "api_content": PROVIDER_BYTES,
    }
    sentinel_row.update(broken)

    display_rows, context_rows = _settle_display_and_context(
        monkeypatch,
        [
            {"role": "assistant", "content": "earlier answer", "timestamp": 500.0},
            sentinel_row,
            {"role": "assistant", "content": "final answer", "timestamp": 1001.0},
        ],
        SENTINEL,
    )

    sid = "webui_8672_r4_broken_provenance"
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

    display = models.reconciled_state_db_messages_for_session(session)
    context = models.reconciled_state_db_messages_for_session(session, prefer_context=True)
    for label, rows in (("display", display), ("context", context)):
        contents = [row.get("content") for row in rows]
        assert _sentinel_count(rows) == 2, (
            f"{label} reconciliation dropped the distinct durable row 8 "
            f"because the sidecar's durable provenance is broken {broken}: "
            f"{contents}"
        )


# ── one-sided compatibility: the durable id on one side only ───────────────


@pytest.mark.parametrize("alias", OMITTED_ALIASES)
def test_durable_alias_spelling_of_one_row_still_collapses_onto_its_twin(alias):
    """Two spellings of ONE durable row must still collapse onto each other.

    The settled row owns durable 7 under ``alias``; the SQLite mirror carries
    the SAME durable 7 under the canonical spelling and no clock.  All four
    aliases name one namespace and one row, so reading more aliases must not
    turn one row into two.
    """
    import api.models as models

    settled = {
        "role": "user",
        "content": SENTINEL,
        "timestamp": 1000.0,
        "id": 2,
        "api_content": PROVIDER_BYTES,
    }
    settled[alias] = 7
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


# ── the owning helper's canonical value rule applies to every alias ────────


@pytest.mark.parametrize("alias", DURABLE_ALIASES)
@pytest.mark.parametrize("durable_value", [7, 7.0, "7", "007"])
def test_durable_alias_value_canonicalises_to_one_row(
    alias, durable_value
):
    """``7``, ``7.0``, ``"7"`` and ``"007"`` name the SAME durable row.

    The owning helper canonicalises the value before comparing it, so the
    occurrence discriminator must see the same row id for every spelling --
    otherwise two spellings of one durable row read as a conflict.
    """
    import api.models as models

    identities = models._established_occurrence_identities(
        {"id": 2, alias: durable_value}
    )
    assert identities == ("id:id:2", "id:_state_db_row_id:7"), (
        f"{alias}={durable_value!r} must canonicalise to durable row 7, got "
        f"{identities}"
    )


@pytest.mark.parametrize("alias", DURABLE_ALIASES)
@pytest.mark.parametrize(
    "durable_value", [True, -4, "abc", 7.5]
)
def test_durable_alias_invalid_value_refuses_to_name_a_row(alias, durable_value):
    """A value that is not a row id must not NAME a durable row.

    The owning helper reports contradictory provenance for it, so the
    occurrence discriminator must not compare it as if it named a row.  It
    still refuses to collapse onto a row, though: reading "no durable
    identity" would let the row collapse onto a DIFFERENT durable row and
    silently drop a real turn (#7587 fails closed).
    """
    import api.models as models

    identities = models._established_occurrence_identities(
        {"id": 2, alias: durable_value}
    )
    assert identities[0] == "id:id:2", (
        f"the stable identity must survive, got {identities}"
    )
    durable = [i for i in identities if i != "id:id:2"]
    assert len(durable) == 1, (
        f"{alias}={durable_value!r} must contribute exactly one durable "
        f"identity, got {identities}"
    )
    value = models._occurrence_identity_value(durable[0])
    assert not value.isdigit(), (
        f"{alias}={durable_value!r} is not a row id, so its durable identity "
        f"must not be a canonical row id, got {value!r}"
    )
    # The refusal must be STABLE: two rows carrying the same broken
    # provenance still agree with each other.
    assert models._established_occurrence_identities(
        {"id": 2, alias: durable_value}
    ) == identities


@pytest.mark.parametrize("alias", DURABLE_ALIASES)
def test_durable_alias_contradictory_values_refuse_to_name_a_row(alias):
    """Two durable aliases that disagree must not NAME a durable row.

    Contradictory provenance must not silently fall back to one of the two
    values: that would turn an identity conflict into a guessed payload.  The
    owning helper refuses the row, so the occurrence discriminator refuses it
    too -- and it keeps refusing to collapse rather than reading as "no
    durable identity at all".
    """
    import api.models as models

    identities = models._established_occurrence_identities(
        {"id": 2, alias: 7, _other_alias(alias): 8}
    )
    assert identities[0] == "id:id:2", (
        f"the stable identity must survive, got {identities}"
    )
    durable = [i for i in identities if i != "id:id:2"]
    assert len(durable) == 1, (
        "contradictory durable aliases must contribute exactly one durable "
        f"identity, got {identities}"
    )
    value = models._occurrence_identity_value(durable[0])
    assert value not in ("7", "8"), (
        "contradictory durable aliases must not name either row, got "
        f"{value!r}"
    )
    # The refusal must be STABLE: the same contradiction agrees with itself.
    assert models._established_occurrence_identities(
        {"id": 2, alias: 7, _other_alias(alias): 8}
    ) == identities


@pytest.mark.parametrize("alias", DURABLE_ALIASES)
def test_durable_alias_same_value_under_two_spellings_is_one_identity(alias):
    """Two aliases carrying the SAME value are a single identity.

    They name one namespace and one row, so they must not read as a conflict
    against each other.
    """
    import api.models as models

    identities = models._established_occurrence_identities(
        {"id": 2, alias: 7, _other_alias(alias): 7}
    )
    assert identities == ("id:id:2", "id:_state_db_row_id:7"), (
        f"two spellings of durable row 7 are one identity, got {identities}"
    )


# ── the bound: reading more aliases must not add comparison work ───────────


def test_durable_alias_coverage_keeps_the_probe_work_bounded(monkeypatch):
    """The wider alias coverage must not re-introduce a bucket scan.

    The prebuilt index resolves a candidate with dict lookups; the comparison
    COUNT is asserted, so a per-bucket scan cannot come back unnoticed.
    """
    import api.models as models

    calls = []
    real = models._occurrence_identity_conflict

    def counting(left_identity, right_identity):
        calls.append((left_identity, right_identity))
        return real(left_identity, right_identity)

    monkeypatch.setattr(models, "_occurrence_identity_conflict", counting)

    rows = [
        {"role": "user", "content": "repeated prompt", "timestamp": 1000.0}
        for _ in range(1001)
    ]
    rows.append({"role": "user", "content": "repeated prompt", "timestamp": 2000.0})
    from api.streaming import _assign_stable_message_ids

    _assign_stable_message_ids(rows)
    sidecar = [dict(row) for row in rows]
    replay = dict(rows[-1])
    replay["timestamp"] = 1000.0

    merged = models.merge_session_messages_append_only(sidecar, [replay])

    assert len(calls) <= 4, (
        f"one replay performed {len(calls)} _occurrence_identity_conflict() "
        "comparisons for a 1001-row same-clock bucket; the prebuilt index must "
        "resolve the candidate with constant work"
    )
    assert len(merged) == len(sidecar), (
        f"the bounded probe changed the merge outcome: {len(merged)} rows for "
        f"{len(sidecar)} sidecar rows"
    )
