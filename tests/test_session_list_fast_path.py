"""Slice C — fast first-paint sidebar payload: parity + cache wiring.

The fast payload (``api.routes._build_session_list_fast_payload``) is the cold
first-paint path for the default sidebar request shape. It must render the same
visible list as the full builder (``_build_session_list_cache_payload``) for the
same args, while never paying the unbounded pipeline costs: no messages JOIN
aggregates over the whole candidate window, no orphan-prune probes. The Claude
Code JSONL scan IS run whenever the request shape enables those sessions
(bounded at ``CLAUDE_CODE_MAX_FILES``, per-file parse cache): a JSONL-backed row
has no state.db row, so skipping it would drop valid sessions from the first
paint while the full builder returns them for the same shape.

Parity scope (plan v2 acceptance criterion 3): ids/order/title/updated_at/
message_count/source flags/project_id/pinned/archived/relationship_type/
parent_session_id, plus every payload count field.
"""
import json
import sqlite3
import threading
import time
from collections import OrderedDict
from pathlib import Path

import pytest

import api.config as config
import api.models as models
import api.routes as routes

pytestmark = pytest.mark.requires_agent_modules


# ── fixture ──────────────────────────────────────────────────────────────────

T = 1_700_000_000.0

_SESSION_COLUMNS = (
    "id", "source", "title", "model", "started_at", "message_count",
    "last_activity_at", "parent_session_id", "end_reason", "ended_at",
    "session_source", "user_id", "chat_id", "chat_type", "thread_id",
    "session_key", "origin_chat_id", "origin_user_id", "platform",
)

# (id, source, title, started_at, end_reason, ended_at, parent, message_count,
#  extra_columns, messages[(role, ts)])
_FIXTURE_SESSIONS = [
    # compression chain: root collapsed into the tip
    ("comp-root", "cli", "Compression root", T + 100, "compression", T + 200, None, 4, {}, [
        ("user", T + 110), ("assistant", T + 120), ("user", T + 190), ("assistant", T + 200)]),
    ("comp-tip", "cli", "Compression tip", T + 210, None, None, "comp-root", 3, {}, [
        ("user", T + 215), ("assistant", T + 230), ("user", T + 240)]),
    # cli_close chain: same continuation collapse
    ("close-root", "cli", "Close root", T + 300, "cli_close", T + 400, None, 2, {}, [
        ("user", T + 310), ("assistant", T + 400)]),
    ("close-tip", "cli", "Close tip", T + 410, None, None, "close-root", 2, {}, [
        ("user", T + 415), ("assistant", T + 430)]),
    # plain child: parent ended for another reason -> real child_session row
    ("plain-parent", "cli", "Plain parent", T + 500, "agent_close", T + 560, None, 3, {}, [
        ("user", T + 505), ("assistant", T + 510), ("user", T + 560)]),
    ("plain-child", "cli", "Plain child", T + 520, None, None, "plain-parent", 2, {}, [
        ("user", T + 530), ("assistant", T + 600)]),
    # messaging identity pair: newest wins under the per-source dedupe
    ("slack-old", "slack", "Slack thread old", T + 700, None, None, None, 2,
     {"chat_id": "C123", "user_id": "U9", "session_key": "slack:C123", "platform": "slack"}, [
        ("user", T + 705), ("assistant", T + 710)]),
    ("slack-new", "slack", "Slack thread new", T + 720, None, None, None, 2,
     {"chat_id": "C123", "user_id": "U9", "session_key": "slack:C123", "platform": "slack"}, [
        ("user", T + 725), ("assistant", T + 730)]),
    # subagent parent + child (the reader re-adds the parent when the child wins)
    ("sub-parent", "subagent", "Subagent orchestrator", T + 800, None, None, None, 2, {}, [
        ("user", T + 805), ("assistant", T + 810)]),
    ("sub-child", "subagent", "Subagent leaf", T + 900, None, None, "sub-parent", 3, {}, [
        ("user", T + 905), ("assistant", T + 910), ("user", T + 1000)]),
    # zero-message row: excluded by the fast SQL gate and dropped by the projection
    ("zero-msg", "cli", "Empty CLI", T + 50, None, None, None, 0, {}, []),
    # webui-source state.db row (mirrors a WebUI sidecar; must dedupe, not double-render)
    ("webui-state-row", "webui", "WebUI native", T + 1100, None, None, None, 3, {}, [
        ("user", T + 1105), ("assistant", T + 1110), ("user", T + 1115)]),
    # background chip rows
    ("cron_job1_20260101_000000", "cron", "Cron run", T + 950, None, None, None, 2, {}, [
        ("user", T + 955), ("assistant", T + 960)]),
    ("kanban-card-1", "kanban", "Kanban card", T + 960, None, None, None, 2, {}, [
        ("user", T + 965), ("assistant", T + 970)]),
    # default-titled CLI row with user turns: visible only when user counts are known
    ("cli-untitled", "cli", None, T + 1200, None, None, None, 2, {}, [
        ("user", T + 1205), ("assistant", T + 1210)]),
    # ACP row: visible only when its user turns are known
    ("acp-row", "acp", None, T + 1300, None, None, None, 2, {}, [
        ("user", T + 1305), ("assistant", T + 1310)]),
    # state.db claude-code row: reachable from the first pass, no JSONL scan
    ("claude-code-db-row", "claude-code", "Claude Code import", T + 1400, None, None, None, 2, {}, [
        ("user", T + 1405), ("assistant", T + 1410)]),
    # stale-counter branch: an EMPTY tip whose column lies (mc=3, no messages)
    # must not steal the compression-tip selection from the real tip
    ("stale-root", "cli", "Stale counter root", T + 1900, "compression", T + 1950, None, 2, {}, [
        ("user", T + 1905), ("assistant", T + 1950)]),
    ("stale-fresh-tip", "cli", "Stale counter fresh tip", T + 1960, None, None, "stale-root", 2, {}, [
        ("user", T + 1965), ("assistant", T + 1990)]),
    ("stale-empty-tip", "cli", "Stale counter empty tip", T + 2000, None, None, "stale-root", 3, {}, []),
    # zero-column row WITH persisted messages (mc=0, one user turn): the count
    # must fall back to the messages table, not hide the row
    ("cli-zero-col-active", "cli", None, T + 2100, None, None, None, 0, {}, [
        ("user", T + 2105)]),
]

# sidecars: (session_id, title, messages, extra)
_FIXTURE_SIDECARS = [
    ("webui-state-row", "WebUI native", [
        {"role": "user", "content": "webui turn", "timestamp": T + 1105},
        {"role": "assistant", "content": "webui answer", "timestamp": T + 1110},
        {"role": "user", "content": "webui follow", "timestamp": T + 1115},
    ], {}),
    ("webui-pure", "Pure WebUI session", [
        {"role": "user", "content": "pure webui", "timestamp": T + 1500},
        {"role": "assistant", "content": "answer", "timestamp": T + 1505},
    ], {}),
    ("arch-parent", "Archived parent", [
        {"role": "user", "content": "archived parent", "timestamp": T + 1600},
        {"role": "assistant", "content": "answer", "timestamp": T + 1605},
    ], {"archived": True}),
    ("arch-child", "Visible child of archived parent", [
        {"role": "user", "content": "child", "timestamp": T + 1700},
        {"role": "assistant", "content": "answer", "timestamp": T + 1705},
    ], {"parent_session_id": "arch-parent"}),
    ("other-profile-row", "Other profile session", [
        {"role": "user", "content": "other profile", "timestamp": T + 1800},
        {"role": "assistant", "content": "answer", "timestamp": T + 1805},
    ], {"profile": "work"}),
]


def _make_state_db(path: Path, sessions, *, last_activity_lag=None, last_activity_null=()):
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE sessions (%s)" % ", ".join(
            "id TEXT PRIMARY KEY" if col == "id" else f"{col} "
            + ("INTEGER" if col == "message_count" else "REAL" if col in ("started_at", "last_activity_at", "ended_at") else "TEXT")
            for col in _SESSION_COLUMNS
        )
    )
    conn.execute(
        "CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, "
        "role TEXT, content TEXT, timestamp REAL)"
    )
    conn.execute(
        "CREATE INDEX idx_sessions_effective_activity ON sessions("
        "COALESCE(last_activity_at, started_at) DESC, started_at DESC)"
    )
    conn.execute("CREATE INDEX idx_messages_session ON messages(session_id, timestamp)")
    for sid, source, title, started, end_reason, ended, parent, mc, extra, messages in sessions:
        last_message = max((ts for _role, ts in messages), default=None)
        lag = (last_activity_lag or {}).get(sid, 0)
        last_activity = None if sid in last_activity_null else (
            (last_message if last_message is not None else started) - lag
        )
        row = {
            "id": sid, "source": source, "title": title, "model": "test-model",
            "started_at": started, "message_count": mc, "last_activity_at": last_activity,
            "parent_session_id": parent, "end_reason": end_reason, "ended_at": ended,
            **extra,
        }
        conn.execute(
            "INSERT INTO sessions (%s) VALUES (%s)" % (
                ", ".join(_SESSION_COLUMNS), ", ".join("?" for _ in _SESSION_COLUMNS)),
            tuple(row.get(col) for col in _SESSION_COLUMNS),
        )
        for role, ts in messages:
            conn.execute(
                "INSERT INTO messages (session_id, role, content, timestamp) VALUES (?, ?, ?, ?)",
                (sid, role, f"{sid} {role}", ts),
            )
    conn.commit()
    conn.close()


def _install_fixture(monkeypatch, tmp_path, *, last_activity_lag=None, last_activity_null=(),
                     extra_sessions=(), extra_sidecars=()):
    import api.profiles as profiles

    state_dir = tmp_path / "webui"
    session_dir = state_dir / "sessions"
    session_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(config, "STATE_DIR", state_dir, raising=False)
    monkeypatch.setattr(config, "SESSION_DIR", session_dir, raising=False)
    monkeypatch.setattr(config, "SESSION_INDEX_FILE", session_dir / "_index.json", raising=False)
    monkeypatch.setattr(config, "SETTINGS_FILE", state_dir / "settings.json", raising=False)
    monkeypatch.setattr(models, "SESSION_DIR", session_dir, raising=False)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json", raising=False)
    monkeypatch.setattr(models, "SESSIONS", OrderedDict(), raising=False)
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path, raising=False)
    monkeypatch.setattr(profiles, "get_active_profile_name", lambda: "default", raising=False)
    monkeypatch.setattr(models, "_active_state_db_path", lambda: tmp_path / "state.db", raising=False)
    monkeypatch.setattr(routes, "_active_state_db_path", lambda: tmp_path / "state.db", raising=False)
    # Never mint projects / touch workspace state from a fixture build.
    monkeypatch.setattr(models, "ensure_cron_project", lambda **_kw: "cron-project", raising=False)
    monkeypatch.setattr(models, "ensure_webhook_project", lambda **_kw: "webhook-project", raising=False)
    monkeypatch.setattr(models, "_profile_has_user_projects", lambda *_a, **_kw: True, raising=False)
    monkeypatch.setattr(models, "get_last_workspace", lambda: "/tmp/fixture-workspace", raising=False)

    (state_dir / "settings.json").write_text(json.dumps({
        "show_cli_sessions": True,
        "show_previous_messaging_sessions": False,
        "show_cron_sessions": False,
        "show_claude_code_sessions": True,
        "show_webhook_sessions": False,
        "show_kanban_sessions": False,
        "api_redact_enabled": False,
    }), encoding="utf-8")

    _make_state_db(
        tmp_path / "state.db", [*_FIXTURE_SESSIONS, *extra_sessions],
        last_activity_lag=last_activity_lag, last_activity_null=last_activity_null,
    )

    index_entries = []
    for sid, title, messages, extra in [*_FIXTURE_SIDECARS, *extra_sidecars]:
        sidecar = {
            "session_id": sid,
            "title": title,
            "workspace": "/tmp/fixture-workspace",
            "model": "test-model",
            "messages": messages,
            "created_at": messages[0]["timestamp"],
            "updated_at": messages[-1]["timestamp"],
            "last_message_at": messages[-1]["timestamp"],
            "message_count": len(messages),
            "profile": extra.get("profile", "default"),
        }
        if extra.get("archived"):
            sidecar["archived"] = True
        if extra.get("parent_session_id"):
            sidecar["parent_session_id"] = extra["parent_session_id"]
        (session_dir / f"{sid}.json").write_text(json.dumps(sidecar), encoding="utf-8")
        index_entries.append(dict(sidecar))
    (session_dir / "_index.json").write_text(json.dumps(index_entries), encoding="utf-8")

    models.clear_sidecar_metadata_cache()
    models.clear_cli_sessions_cache()
    return state_dir


def _payload_args(**overrides):
    args = dict(
        active_profile="default",
        all_profiles=False,
        show_cli_sessions=True,
        show_previous_messaging_sessions=False,
        show_cron_sessions=False,
        show_claude_code_sessions=True,
        include_archived=False,
        exclude_hidden=False,
        visible_only=True,
        show_webhook_sessions=False,
        show_kanban_sessions=False,
        source_filter=None,
        sidebar_source=None,
        archived_limit=None,
        archived_offset=0,
    )
    args.update(overrides)
    return args


def _build_full(**overrides):
    return routes._build_session_list_cache_payload(**_payload_args(**overrides))


def _build_fast(**overrides):
    return routes._build_session_list_fast_payload(**_payload_args(**overrides))


_ROW_PARITY_FIELDS = (
    "session_id", "title", "display_title", "_state_db_title",
    "updated_at", "last_message_at", "message_count", "actual_message_count",
    "is_cli_session", "source_tag", "raw_source", "session_source", "source_label",
    "project_id", "pinned", "archived", "read_only", "profile",
    "relationship_type", "parent_session_id", "parent_title", "parent_source",
    "_parent_lineage_root_id", "_lineage_root_id", "_lineage_tip_id",
    "_compression_segment_count",
)

_COUNT_FIELDS = (
    "cli_count", "archived_count", "archived_webui_count", "archived_cli_count",
    "webui_session_count", "cli_session_count", "other_profile_count",
    "all_profiles", "active_profile",
)


def _assert_payload_parity(full, fast):
    assert [r["session_id"] for r in fast["sessions"]] == [r["session_id"] for r in full["sessions"]]
    full_by_id = {r["session_id"]: r for r in full["sessions"]}
    for row in fast["sessions"]:
        ref = full_by_id[row["session_id"]]
        for field in _ROW_PARITY_FIELDS:
            assert row.get(field) == ref.get(field), (
                f"{row['session_id']}: {field}: fast={row.get(field)!r} full={ref.get(field)!r}"
            )
    assert [r["session_id"] for r in fast["sidebar_reference_sessions"]] == [
        r["session_id"] for r in full["sidebar_reference_sessions"]
    ]
    for field in _COUNT_FIELDS:
        assert fast.get(field) == full.get(field), f"{field}: fast={fast.get(field)!r} full={full.get(field)!r}"


# ── C3: parity ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("shape", [
    {},
    {"exclude_hidden": True},
    {"sidebar_source": "webui"},
    {"sidebar_source": "cli"},
    {"show_cli_sessions": False},
    {"show_previous_messaging_sessions": True},
    {"show_cron_sessions": True},
    {"show_kanban_sessions": True},
])
def test_fast_payload_parity_with_full_builder(monkeypatch, tmp_path, shape):
    _install_fixture(monkeypatch, tmp_path)
    full = _build_full(**shape)
    fast = _build_fast(**shape)
    _assert_payload_parity(full, fast)


def test_fast_payload_parity_under_candidate_key_drift(monkeypatch, tmp_path):
    """A lagging/NULL ``last_activity_at`` must not change the visible slice."""
    _install_fixture(
        monkeypatch, tmp_path,
        last_activity_lag={"plain-child": 30.0, "sub-child": 25.0},
        last_activity_null=("slack-new",),
    )
    full = _build_full()
    fast = _build_fast()
    _assert_payload_parity(full, fast)
    ids = [r["session_id"] for r in fast["sessions"]]
    # A collapsed chain renders as its TIP id (navigation points at the latest
    # importable segment) carrying the root's lineage identity.
    assert "comp-tip" in ids and "comp-root" not in ids
    assert "close-tip" in ids and "close-root" not in ids
    merged = next(r for r in fast["sessions"] if r["session_id"] == "comp-tip")
    assert merged["_lineage_root_id"] == "comp-root"
    assert merged["_lineage_tip_id"] == "comp-tip"
    assert merged["_compression_segment_count"] == 2
    assert merged["title"] == "Compression root"
    assert "plain-child" in ids  # real child row survives
    child = next(r for r in fast["sessions"] if r["session_id"] == "plain-child")
    assert child["relationship_type"] == "child_session"
    assert child["parent_session_id"] == "plain-parent"
    assert child["parent_title"] == "Plain parent"
    assert "slack-new" in ids and "slack-old" not in ids  # messaging dedupe
    assert "zero-msg" not in ids
    # stale-counter branch: the empty lying tip must not steal the collapse
    assert "stale-fresh-tip" in ids
    assert "stale-empty-tip" not in ids
    assert "stale-root" not in ids
    stale = next(r for r in fast["sessions"] if r["session_id"] == "stale-fresh-tip")
    assert stale["title"] == "Stale counter root"
    assert stale["_lineage_root_id"] == "stale-root"
    # mc=0 with a persisted message must stay visible (count from the table)
    assert "cli-zero-col-active" in ids
    zero_col = next(r for r in fast["sessions"] if r["session_id"] == "cli-zero-col-active")
    assert zero_col["message_count"] == 1
    assert "sub-parent" in ids and "sub-child" in ids  # subagent re-add
    assert "webui-state-row" in ids
    assert "webui-pure" in ids
    assert "arch-parent" not in ids
    assert "other-profile-row" not in ids
    assert any(
        r["session_id"] == "arch-parent" for r in fast["sidebar_reference_sessions"]
    )


def test_fast_payload_parity_binds_the_candidate_window(monkeypatch, tmp_path):
    """Resumed-old rows must survive the candidate window, and the window must
    stay oversampled.

    The base fixture (21 state.db candidates, 16 visible rows) cannot see a
    broken candidate ordering key or a collapsed window: every candidate fits
    either way. On real data a long-lived session resumed days later is exactly
    the row a ``started_at`` candidate key loses (its ``last_activity_at`` /
    exact ``MAX(m.timestamp)`` is recent while ``started_at`` is old), and the
    8x oversample is the headroom that keeps its exact-recency rank inside the
    window.

    Fixture: 200 fillers newer by ``started_at`` plus 3 resumed-old rows whose
    ``last_activity_at`` lags the exact key by ~65 min — candidate rank ~110 of
    203 (inside the 160-row window, outside any 1x window), exact recency
    top-3. A ``started_at`` candidate key drops the resumed rows from the
    window entirely; a ``limit * 1`` window excludes them from the candidate
    set. Both make parity fail (verified by mutation on a /tmp copy), so this
    test pins the window size and the key, not just the SQL text.
    """
    fillers = [
        (f"filler-{i:03d}", "cli", f"Filler {i:03d}", T + 1000 + i, None, None, None, 1, {},
         [("user", T + 1000 + i)])
        for i in range(200)
    ]
    resumed = []
    lag = {}
    for k in range(3):
        sid = f"resumed-old-{k}"
        resumed.append((sid, "cli", f"Resumed old {k}", T + 10 + k, None, None, None, 2, {},
                        [("user", T + 5000 + k), ("assistant", T + 5001 + k)]))
        # last_activity_at = (T + 5001 + k) - lag = T + 1100 for all three:
        # a recent-but-lagging candidate key with an exact recency far newer.
        lag[sid] = 3901.0 + k
    _install_fixture(
        monkeypatch, tmp_path, extra_sessions=fillers + resumed, last_activity_lag=lag,
    )
    full = _build_full()
    fast = _build_fast()
    _assert_payload_parity(full, fast)
    ids = [r["session_id"] for r in fast["sessions"]]
    # The resumed rows are in the visible slice (top-3 by exact recency); if the
    # fixture ever stops producing that shape this assertion goes red first.
    assert {"resumed-old-0", "resumed-old-1", "resumed-old-2"} <= set(ids)
    assert ids[:3] == ["resumed-old-2", "resumed-old-1", "resumed-old-0"]
    # The newest fillers fill the rest of the capped CLI slice in exact-recency
    # order; the oldest fillers never surface.
    assert ids.index("filler-199") < ids.index("filler-198")
    assert "filler-000" not in ids


def test_fast_payload_parity_when_drift_exceeds_the_candidate_window(monkeypatch, tmp_path):
    """A resumed session whose denormalized activity key ranks it past the 8x
    window must still be in BOTH payloads.

    The window bounds the messages join, not the visible set: ordering it by the
    lagging ``last_activity_at`` made membership an oversample rather than a
    bound, so a row with a *missing* column (``NULL`` -> the old ``started_at``)
    — exact rank 1, approximate rank 201 of 201 — vanished from the fast AND the
    full projection. The window now orders by the exact key the display sorts
    by, so both builders keep the row.
    """
    fillers = [
        (f"drift-filler-{i:03d}", "cli", f"Drift filler {i:03d}", T + 1000 + i, None, None, None, 1, {},
         [("user", T + 1000 + i)])
        for i in range(200)
    ]
    resumed = [(
        "resumed-null", "cli", "Resumed null", T + 10, None, None, None, 2, {},
        [("user", T + 5000), ("assistant", T + 5001)],
    )]
    _install_fixture(
        monkeypatch, tmp_path,
        extra_sessions=fillers + resumed,
        last_activity_null=("resumed-null",),
    )
    full = _build_full()
    fast = _build_fast()
    _assert_payload_parity(full, fast)
    ids = [r["session_id"] for r in fast["sessions"]]
    # Present at all, and newest by exact recency, so it leads the slice.
    assert "resumed-null" in ids
    assert ids[0] == "resumed-null"


def test_fast_payload_shows_the_tier1_state_db_count_override(monkeypatch, tmp_path):
    """The fast first paint must apply the tier-1 state.db overlay: a webui
    row's own count (``sessions.message_count``) beats its sidecar.

    On the committed fixture the fast payload is byte-identical with the whole
    overlay skipped, so nothing pinned the tier-1 fetch; the settled background
    rebuild (which runs the tier-2 ``messages`` aggregation too) hides the gap
    in every other test. Here the state.db row has 5 messages while the sidecar
    has 2 with an older ``last_message_at``:

    * the fast payload must show the state.db count (5), not the sidecar's 2;
    * the fast builder must ask ``all_sessions`` for tier-1 only
      (``state_db_override_counts=False``) — no ``messages`` scan on the
      request thread;
    * the newer ``last_message_at`` stays a documented divergence: the fast
      paint carries the sidecar's T+2405, the settled full payload T+2600.
    """
    extra_sessions = [(
        "webui-desktop-appended", "webui", "Desktop appended", T + 2300, None, None, None, 5, {},
        [("user", T + 2300), ("assistant", T + 2400), ("user", T + 2450),
         ("assistant", T + 2550), ("user", T + 2600)],
    )]
    extra_sidecars = [(
        "webui-desktop-appended", "Desktop appended", [
            {"role": "user", "content": "desktop turn", "timestamp": T + 2400},
            {"role": "assistant", "content": "desktop answer", "timestamp": T + 2405},
        ], {},
    )]
    _install_fixture(
        monkeypatch, tmp_path,
        extra_sessions=extra_sessions, extra_sidecars=extra_sidecars,
    )

    override_calls = []
    real_all_sessions = routes.all_sessions

    def _spy(diag=None, *, include_lineage_metadata=True, state_db_override_counts=True):
        override_calls.append(state_db_override_counts)
        return real_all_sessions(
            diag=diag,
            include_lineage_metadata=include_lineage_metadata,
            state_db_override_counts=state_db_override_counts,
        )

    monkeypatch.setattr(routes, "all_sessions", _spy)
    fast = _build_fast()
    assert override_calls, "the fast builder must load webui rows through all_sessions"
    assert all(flag is False for flag in override_calls), (
        "the fast first paint must request tier-1 overrides only (no messages scan)"
    )

    row = next(r for r in fast["sessions"] if r["session_id"] == "webui-desktop-appended")
    assert row["message_count"] == 5, "state.db sessions count must beat the sidecar's 2"
    assert row["actual_message_count"] == 5
    assert row["last_message_at"] == T + 2405, (
        "documented divergence: the tier-2 last_message_at overlay waits for the rebuild"
    )

    full = _build_full()
    full_row = next(r for r in full["sessions"] if r["session_id"] == "webui-desktop-appended")
    assert full_row["message_count"] == 5
    assert full_row["last_message_at"] == T + 2600  # tier-2 aggregation, settled


def test_fast_payload_keeps_untitled_cli_and_acp_rows_visible(monkeypatch, tmp_path):
    """User-turn counts are unknown in the fast window; the bounded fallback
    query must still keep rows the full pipeline keeps."""
    _install_fixture(monkeypatch, tmp_path)
    fast = _build_fast()
    ids = {r["session_id"] for r in fast["sessions"]}
    assert "cli-untitled" in ids
    assert "acp-row" in ids
    full = _build_full()
    assert {r["session_id"] for r in full["sessions"]} == ids


def test_fast_payload_skips_claude_code_jsonl_scan_when_disabled(monkeypatch, tmp_path):
    """With Claude Code sessions disabled the fast payload must not pay for the
    JSONL scan (the shape does not ask for those rows) and no JSONL-backed row
    may leak into it."""
    _install_fixture(monkeypatch, tmp_path)

    def _boom():
        raise AssertionError("Claude Code JSONL scan must not run when disabled")

    monkeypatch.setattr(models, "get_claude_code_sessions", _boom)
    monkeypatch.setattr(routes, "get_claude_code_sessions", _boom, raising=False)
    fast = _build_fast(show_claude_code_sessions=False)
    ids = {r["session_id"] for r in fast["sessions"]}
    assert "claude-code-db-row" in ids  # state.db claude-code rows stay reachable
    assert not any(str(sid).startswith("claude_code_") for sid in ids)


def _record_sqlite_connect(monkeypatch):
    """Record every executed statement while delegating to the real sqlite3."""
    statements: list[str] = []
    real_connect = sqlite3.connect

    class _RecordingCursor:
        def __init__(self, cursor):
            self._cursor = cursor

        def execute(self, sql, *args):
            statements.append(" ".join(str(sql).split()))
            return self._cursor.execute(sql, *args)

        def __getattr__(self, name):
            return getattr(self._cursor, name)

    class _RecordingConnection:
        def __init__(self, conn):
            object.__setattr__(self, "_conn", conn)

        def __setattr__(self, name, value):
            # row_factory is set on the connection by the readers; forward it to
            # the real connection instead of shadowing it on the wrapper.
            setattr(self._conn, name, value)

        def __getattr__(self, name):
            return getattr(self._conn, name)

        def cursor(self):
            return _RecordingCursor(self._conn.cursor())

    monkeypatch.setattr(
        sqlite3, "connect",
        lambda *args, **kwargs: _RecordingConnection(real_connect(*args, **kwargs)),
    )
    return statements


def test_fast_payload_window_keeps_exact_counts_and_skips_user_turn_aggregation(monkeypatch, tmp_path):
    """The fast window keeps exact per-candidate counts/recency but must not
    aggregate user turns for the whole candidate set (deferred to a bounded
    fallback), and it orders by the exact activity key the display sorts by."""
    _install_fixture(monkeypatch, tmp_path)
    statements = _record_sqlite_connect(monkeypatch)
    fast = _build_fast()
    assert fast["sessions"]

    fast_sql = [s for s in statements if "FROM sessions s" in s and "candidates" in s.lower()]
    assert fast_sql, "the fast window query must run"
    for sql in fast_sql:
        assert "LOWER(m.role)" not in sql, f"fast window must not aggregate user turns: {sql[:200]}"
        assert "COUNT(CASE" not in sql
        # The window is the display order's prefix over the seeded candidate set
        # (not over all sessions): the candidates CTE's membership/order is the
        # exact key, applied only over the bounded pre-window union.
        assert (
            "COALESCE((SELECT MAX(mx.timestamp) FROM messages mx WHERE mx.session_id = s.id),"
            " s.started_at) DESC" in sql
        ), f"candidate window must order by the exact activity key: {sql[:200]}"
        assert "pre_activity AS" in sql and "s.id IN (" in sql, (
            f"the candidate set must be seeded by the bounded pre-window union: {sql[:200]}"
        )
        # The lagging denormalized column may seed ``pre_activity`` (a bounded
        # superset), but the candidates CTE — membership and order — must never
        # reference it.
        candidates_body = sql.split("candidates AS (", 1)[-1]
        assert "COALESCE(s.last_activity_at" not in candidates_body, (
            f"the lagging denormalized key must not decide window membership: {sql[:200]}"
        )
    assert any("COUNT(m.id) AS actual_message_count" in s for s in fast_sql), (
        "the fast window must keep the exact per-candidate message count"
    )


def _jsonl_fixture_row():
    """One Claude Code JSONL-scan row (only the scan can produce these)."""
    return {
        "session_id": "claude_code_deadbeefdeadbeefdeadbeef",
        "title": "JSONL transcript",
        "workspace": "/tmp/fixture-workspace",
        "model": "claude-code",
        "message_count": 4,
        "created_at": T + 3000,
        "updated_at": T + 3010,
        "pinned": False,
        "archived": False,
        "project_id": None,
        "profile": "default",
        "source_tag": "claude_code",
        "raw_source": "claude_code",
        "session_source": "external_agent",
        "source_label": "Claude Code",
        "is_cli_session": True,
        "read_only": True,
    }


def test_fast_payload_parity_includes_claude_code_jsonl_rows(monkeypatch, tmp_path):
    """With Claude Code sessions enabled, the JSONL-scan rows are part of the
    session set for the default shape — the fast first paint must return them.

    They exist only in the CLI list the JSONL scan produces (a JSONL-backed row
    has no state.db row), so a fast builder that skips the scan omits valid
    sessions from the first response while the full builder returns them for the
    same request shape. Same setting, same session set.
    """
    _install_fixture(monkeypatch, tmp_path)
    monkeypatch.setattr(
        models, "get_claude_code_sessions", lambda: [_jsonl_fixture_row()], raising=False,
    )

    full = _build_full()
    fast = _build_fast()

    _assert_payload_parity(full, fast)
    assert "claude_code_deadbeefdeadbeefdeadbeef" in {r["session_id"] for r in fast["sessions"]}


def test_fast_payload_counts_match_the_full_payload_when_claude_code_is_disabled(monkeypatch, tmp_path):
    """With Claude Code sessions disabled, both builders exclude the JSONL
    scan's rows from the CLI list and the count fields.

    The count fields describe the CLI list for the request shape; a fast window
    that ran the scan anyway (or a full builder that skipped it) would count
    rows the shape did not ask for.
    """
    _install_fixture(monkeypatch, tmp_path)
    monkeypatch.setattr(
        models, "get_claude_code_sessions", lambda: [_jsonl_fixture_row()], raising=False,
    )

    full = _build_full(show_claude_code_sessions=False)
    fast = _build_fast(show_claude_code_sessions=False)

    _assert_payload_parity(full, fast)
    assert not any(
        str(r["session_id"]).startswith("claude_code_") for r in fast["sessions"]
    )
    assert fast["cli_session_count"] == full["cli_session_count"] > 0


def test_fast_window_loader_honors_include_claude_code(monkeypatch, tmp_path):
    """The fast window runs the same JSONL scan as any other load when the
    caller asks for Claude Code sessions, and skips it when it does not.

    A JSONL-backed row has no state.db row, so skipping the scan on the fast
    window silently dropped valid sessions from the fast first paint while the
    full builder returned them for the same request shape.
    """
    _install_fixture(monkeypatch, tmp_path)
    calls = []

    def _scan():
        calls.append("scan")
        return [_jsonl_fixture_row()]

    monkeypatch.setattr(models, "get_claude_code_sessions", _scan, raising=False)
    rows = models.get_cli_sessions(include_claude_code=True, fast_window=True)
    assert calls == ["scan"]
    assert "claude_code_deadbeefdeadbeefdeadbeef" in {r["session_id"] for r in rows}
    assert rows  # the bounded state.db window still returns rows

    calls.clear()
    rows_disabled = models.get_cli_sessions(include_claude_code=False, fast_window=True)
    assert calls == []
    assert not any(str(r.get("session_id", "")).startswith("claude_code_") for r in rows_disabled)


def test_fast_reader_degrades_without_a_messages_table(monkeypatch, tmp_path):
    """Older/minimal schemas (no usable messages table) must mirror the full
    reader's degradation: denormalized counts, started_at recency."""
    import api.agent_sessions as agent_sessions

    db_path = tmp_path / "legacy.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, title TEXT, model TEXT, "
        "started_at REAL, message_count INTEGER, last_activity_at REAL, parent_session_id TEXT, "
        "end_reason TEXT, ended_at REAL)"
    )
    conn.execute(
        "INSERT INTO sessions (id, source, title, started_at, message_count, last_activity_at) "
        "VALUES ('legacy-cli', 'cli', 'Legacy session', ?, 3, ?)",
        (T + 10, T + 12),
    )
    conn.commit()
    conn.close()

    rows = agent_sessions.read_fast_sidebar_agent_rows(
        db_path, limit=20, exclude_sources=("cron", "webhook", "kanban"),
    )
    assert [row["id"] for row in rows] == ["legacy-cli"]
    assert rows[0]["actual_message_count"] == 3
    assert rows[0]["last_activity"] is None
    assert rows[0]["message_count"] == 3


@pytest.mark.parametrize("messages_ddl", [
    None,
    "CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, role TEXT, content TEXT)",
])
def test_fast_reader_degrades_without_messages_session_id_for_hidden_rows(tmp_path, messages_ddl):
    """Legacy schemas without a usable ``messages.session_id`` must not raise
    from the visibility fallback.

    ``_fill_fast_visibility_user_counts`` is the fast window's only post-filter
    query and it always reads ``messages.session_id``. On a schema without that
    column (or without the table), an initially hidden CLI/ACP row turns the
    whole read into an ``OperationalError`` — the bounded fast window is lost —
    instead of the full reader's degradation (denormalized ``s.message_count``
    for the user-turn count, ``use_messages_join`` keyed on ``session_id``
    alone). Both readers must agree here.
    """
    import api.agent_sessions as agent_sessions

    db_path = tmp_path / "legacy-hidden.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, title TEXT, model TEXT, "
        "started_at REAL, message_count INTEGER, last_activity_at REAL, parent_session_id TEXT, "
        "end_reason TEXT, ended_at REAL)"
    )
    if messages_ddl:
        conn.execute(messages_ddl)
    # Both rows are initially hidden (NULL user-turn count): a default-titled
    # CLI row needs >= CLI_MIN_UNTITLED_USER_MESSAGE_COUNT user turns, an ACP
    # row needs at least one.
    conn.execute(
        "INSERT INTO sessions (id, source, title, started_at, message_count, last_activity_at) "
        "VALUES ('legacy-untitled', 'cli', NULL, ?, 2, ?)",
        (T + 10, T + 12),
    )
    conn.execute(
        "INSERT INTO sessions (id, source, title, started_at, message_count, last_activity_at) "
        "VALUES ('legacy-acp', 'acp', NULL, ?, 2, ?)",
        (T + 20, T + 22),
    )
    conn.commit()
    conn.close()

    fast = agent_sessions.read_fast_sidebar_agent_rows(
        db_path, limit=20, exclude_sources=("cron", "webhook", "kanban"),
    )
    full = agent_sessions.read_importable_agent_session_rows(
        db_path, limit=20, exclude_sources=("cron", "webhook", "kanban"),
    )
    assert [row["id"] for row in fast] == [row["id"] for row in full]
    assert [row["id"] for row in fast] == ["legacy-acp", "legacy-untitled"]


def _make_edge_schema_without_message_timestamps(tmp_path):
    """state.db with ``messages(session_id, role, content)`` — no ``timestamp``.

    ``edge-hot`` is newest by ``started_at`` but oldest by ``last_activity_at``;
    20 fillers are the reverse. The full reader's degradation for this schema
    joins ``messages`` on ``session_id`` alone (``COUNT(m.id)``, ``last_activity
    = NULL``) and orders candidates by ``started_at``; the fast reader must
    mirror exactly that.
    """
    db_path = tmp_path / "edge.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, title TEXT, model TEXT, "
        "started_at REAL, message_count INTEGER, last_activity_at REAL, parent_session_id TEXT, "
        "end_reason TEXT, ended_at REAL)"
    )
    conn.execute(
        "CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, "
        "role TEXT, content TEXT)"
    )

    def _add(sid, title, started, mc, last_activity_at, roles):
        conn.execute(
            "INSERT INTO sessions (id, source, title, started_at, message_count, last_activity_at) "
            "VALUES (?, 'cli', ?, ?, ?, ?)",
            (sid, title, started, mc, last_activity_at),
        )
        for role in roles:
            conn.execute(
                "INSERT INTO messages (session_id, role, content) VALUES (?, ?, ?)",
                (sid, role, f"{sid} {role}"),
            )

    # stale counter (mc=5) over 2 real messages; candidate-last by last_activity_at
    _add("edge-hot", "Edge hot", T + 900, 5, T + 10, ("user", "assistant"))
    # mc=2 over 3 real messages
    _add("edge-b", "Edge b", T + 800, 2, T + 800, ("user", "assistant", "user"))
    for i in range(20):
        _add(f"edge-filler-{i:02d}", f"Edge filler {i:02d}", T + 100 + i, 2, T + 500 + i,
             ("user", "assistant"))
    conn.commit()
    conn.close()
    return db_path


def test_fast_reader_mirrors_full_reader_without_message_timestamps(tmp_path):
    """``messages`` without a ``timestamp`` column: the fast reader must join
    like the full reader (``session_id`` alone) and report ``COUNT(m.id)``, not
    the denormalized ``s.message_count``.

    Verifier repro on this schema: fast reported the stale counters (5/2) while
    the full reader reported the real counts (2/3) — the fast reader gated the
    JOIN on ``session_id AND timestamp`` while the full reader joins on
    ``session_id`` alone (``api/agent_sessions.py``).
    """
    import api.agent_sessions as agent_sessions

    db_path = _make_edge_schema_without_message_timestamps(tmp_path)
    fast = agent_sessions.read_fast_sidebar_agent_rows(
        db_path, limit=20, exclude_sources=("cron", "webhook", "kanban"),
    )
    full = agent_sessions.read_importable_agent_session_rows(
        db_path, limit=20, exclude_sources=("cron", "webhook", "kanban"),
    )
    assert [row["id"] for row in fast] == [row["id"] for row in full]
    assert [row["actual_message_count"] for row in fast] == [
        row["actual_message_count"] for row in full
    ]
    assert [row["last_activity"] for row in fast] == [None] * len(fast)
    by_id = {row["id"]: row for row in fast}
    assert by_id["edge-hot"]["actual_message_count"] == 2  # COUNT(m.id), not mc=5
    assert by_id["edge-b"]["actual_message_count"] == 3    # COUNT(m.id), not mc=2


def test_fast_reader_candidate_window_ignores_last_activity_without_timestamps(tmp_path):
    """Without a ``timestamp`` column the full reader's candidate window orders
    by ``started_at`` (its documented degradation), so the fast window must too.

    With a ``limit`` small enough that the 8x window binds, a
    ``COALESCE(last_activity_at, started_at)`` candidate key drops ``edge-hot``
    (newest by ``started_at``, oldest by ``last_activity_at``) from the window
    and the visible slice diverges from the full reader's.
    """
    import api.agent_sessions as agent_sessions

    db_path = _make_edge_schema_without_message_timestamps(tmp_path)
    fast = agent_sessions.read_fast_sidebar_agent_rows(
        db_path, limit=2, exclude_sources=("cron", "webhook", "kanban"),
    )
    full = agent_sessions.read_importable_agent_session_rows(
        db_path, limit=2, exclude_sources=("cron", "webhook", "kanban"),
    )
    assert [row["id"] for row in fast] == [row["id"] for row in full]
    assert [row["id"] for row in fast] == ["edge-hot", "edge-b"]


# ── no-index store: the fast CLI window must degrade, never disappear ────────

def _drop_sessions_indexes(db_path: Path) -> None:
    """Drop the two indexes the union's session-row seeds require."""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("DROP INDEX IF EXISTS idx_sessions_effective_activity")
        conn.execute("DROP INDEX IF EXISTS idx_sessions_started")
        conn.commit()
    finally:
        conn.close()


def test_fast_payload_cli_rows_match_the_full_builder_without_sessions_indexes(monkeypatch, tmp_path):
    """A store with ``messages.timestamp`` but no standard sessions indexes:
    the fast payload's CLI/agent rows ARE the full builder's.

    Regression (round-2 defect A-D1): the documented no-index fallback (the
    plain exact-key window) raised ``sqlite3.ProgrammingError: Incorrect number
    of bindings supplied`` because it reused the union's parameter list, and
    ``get_cli_sessions(fast_window=True)`` swallowed the error into ``[]`` — the
    fast first paint silently dropped every CLI/agent row (and the
    cron/webhook/kanban chips) on such a store, with no exception for the route
    to fall back from.
    """
    _install_fixture(monkeypatch, tmp_path)
    _drop_sessions_indexes(tmp_path / "state.db")

    full = _build_full()
    fast = _build_fast()
    _assert_payload_parity(full, fast)
    assert fast["cli_session_count"] == full["cli_session_count"] > 0


def test_fast_window_cli_read_failure_is_not_swallowed_into_zero_rows(monkeypatch, tmp_path):
    """A raising fast CLI read must not become an empty CLI list.

    ``get_cli_sessions(fast_window=True)`` is the fast first paint's CLI/agent
    read; swallowing a failure into ``[]`` silently drops every CLI/agent row
    (chips included) from the first paint while the full builder returns them.
    The exception propagates so the route's documented fast-build-failure
    fallback (``_get_cached_session_list_payload`` → the synchronous full build)
    serves the request instead — the fast path degrades, it never omits.
    """
    _install_fixture(monkeypatch, tmp_path)

    def _boom(*_args, **_kwargs):
        raise sqlite3.ProgrammingError("Incorrect number of bindings supplied")

    monkeypatch.setattr(models, "read_fast_sidebar_agent_rows", _boom)
    with pytest.raises(sqlite3.ProgrammingError):
        models.get_cli_sessions(include_claude_code=False, fast_window=True)


def test_failing_fast_cli_read_serves_the_full_payload_through_the_route(monkeypatch, tmp_path):
    """The documented fallback, end to end: a failing fast CLI read makes the
    route serve the unchanged full payload, not a fast one missing every CLI row.

    ``_build_session_list_fast_payload`` does not swallow a fast CLI read
    failure, so ``_get_cached_session_list_payload`` catches it on the
    ``fast_builder()`` call and falls through to the synchronous full build —
    the documented behavior the fast-window propagation exists to reach.
    """
    _install_fixture(monkeypatch, tmp_path)
    monkeypatch.setattr(routes, "_session_list_cache_source_stamp", lambda _key: ("stable",))
    full = _build_full()
    real_get_cli_sessions = routes.get_cli_sessions

    def _failing_get_cli_sessions(*args, **kwargs):
        if kwargs.get("fast_window"):
            raise sqlite3.ProgrammingError("Incorrect number of bindings supplied")
        return real_get_cli_sessions(*args, **kwargs)

    monkeypatch.setattr(routes, "get_cli_sessions", _failing_get_cli_sessions)
    served = routes._get_cached_session_list_payload(
        key=_cache_key(),
        builder=_build_full,
        fast_builder=lambda: _build_fast(),
    )
    assert [r["session_id"] for r in served["sessions"]] == [
        r["session_id"] for r in full["sessions"]
    ]
    assert served["cli_session_count"] == full["cli_session_count"] > 0


def test_fast_payload_is_bounded_and_fills_user_counts_lazily(monkeypatch, tmp_path):
    """The user-turn fallback query must only cover rows dropped by the
    visibility filter, never the whole candidate window."""
    _install_fixture(monkeypatch, tmp_path)
    statements = _record_sqlite_connect(monkeypatch)
    fast = _build_fast()
    assert fast["sessions"]
    user_count_sql = [s for s in statements if "LOWER(role)" in s]
    assert user_count_sql, "the bounded user-count fallback must run for dropped rows"
    for sql in user_count_sql:
        assert "session_id IN" in sql, f"user-count fallback must be id-bounded: {sql[:200]}"
        assert "GROUP BY" in sql


# ── C2: cache wiring ─────────────────────────────────────────────────────────

def _cache_payload(marker, **extra):
    payload = {"sessions": [{"session_id": marker}], "cli_count": 0, "active_profile": None}
    payload.update(extra)
    return payload


def _cache_key():
    return routes._session_list_cache_key(
        active_profile="default",
        all_profiles=False,
        show_cli_sessions=True,
        show_previous_messaging_sessions=False,
        show_cron_sessions=False,
    )


@pytest.fixture(autouse=True)
def _isolated_session_list_cache_state():
    routes._session_list_cache_clear()
    with routes._SESSIONS_CACHE_LOCK:
        routes._SESSIONS_CACHE_INFLIGHT.clear()
    yield
    routes._session_list_cache_clear()
    with routes._SESSIONS_CACHE_LOCK:
        routes._SESSIONS_CACHE_INFLIGHT.clear()


def test_cold_owner_serves_fast_payload_and_rebuilds_full_in_background(monkeypatch):
    monkeypatch.setattr(routes, "_session_list_cache_source_stamp", lambda _key: ("stable",))
    key = _cache_key()
    started = threading.Event()
    release = threading.Event()
    calls = {"full": 0, "fast": 0}

    def _full_builder():
        calls["full"] += 1
        started.set()
        release.wait(5.0)
        return _cache_payload("full")

    def _fast_builder():
        calls["fast"] += 1
        return _cache_payload("fast")

    result = routes._get_cached_session_list_payload(
        key=key, builder=_full_builder, fast_builder=_fast_builder,
    )
    assert result == _cache_payload("fast")
    assert calls == {"full": 1, "fast": 1}
    assert started.wait(1.0), "the full rebuild must run in the background"

    # The fast payload is NOT stored and the rebuild event is still pending, so
    # waiters cannot be released with no fresh payload.
    assert routes._session_list_cache_get(key, allow_stale=True) == (None, False)
    with routes._SESSIONS_CACHE_LOCK:
        event = routes._SESSIONS_CACHE_INFLIGHT.get(key)
    assert event is not None and not event.is_set()

    release.set()
    deadline = time.monotonic() + 5.0
    stored = None
    while time.monotonic() < deadline:
        stored, _fresh = routes._session_list_cache_get(key, allow_stale=True)
        if stored is not None:
            break
        time.sleep(0.02)
    assert stored == _cache_payload("full")
    assert calls == {"full": 1, "fast": 1}


def test_cold_follower_serves_fast_payload_without_waiting_for_owner(monkeypatch):
    monkeypatch.setattr(routes, "_session_list_cache_source_stamp", lambda _key: ("stable",))
    key = _cache_key()
    started = threading.Event()
    release = threading.Event()

    def _full_builder():
        started.set()
        release.wait(5.0)
        return _cache_payload("full")

    owner_result = {}

    def _owner():
        owner_result["payload"] = routes._get_cached_session_list_payload(
            key=key,
            builder=_full_builder,
            fast_builder=lambda: _cache_payload("fast"),
        )

    owner = threading.Thread(target=_owner)
    owner.start()
    try:
        assert started.wait(1.0)
        # The owner's full rebuild is inflight and blocked. A concurrent cold
        # request must return the fast payload instead of waiting 0.25 s and
        # rebuilding synchronously.
        follower = routes._get_cached_session_list_payload(
            key=key,
            builder=_full_builder,
            fast_builder=lambda: _cache_payload("fast"),
        )
        assert follower == _cache_payload("fast")
        assert not release.is_set(), "follower must not block on the owner's rebuild"
    finally:
        release.set()
        owner.join(5.0)
    assert owner_result["payload"] == _cache_payload("fast")


def test_cold_call_without_fast_builder_keeps_synchronous_rebuild(monkeypatch):
    monkeypatch.setattr(routes, "_session_list_cache_source_stamp", lambda _key: ("stable",))
    key = _cache_key()
    calls = []

    def _builder():
        calls.append("full")
        return _cache_payload("full")

    assert routes._get_cached_session_list_payload(key=key, builder=_builder) == _cache_payload("full")
    assert calls == ["full"]


def test_fast_builder_failure_falls_back_to_the_synchronous_full_build(monkeypatch):
    monkeypatch.setattr(routes, "_session_list_cache_source_stamp", lambda _key: ("stable",))
    key = _cache_key()
    calls = []

    def _boom():
        raise RuntimeError("fast build failed")

    def _builder():
        calls.append("full")
        return _cache_payload("full")

    result = routes._get_cached_session_list_payload(
        key=key, builder=_builder, fast_builder=_boom,
    )
    assert result == _cache_payload("full")
    assert calls == ["full"]
    assert routes._session_list_cache_get(key, allow_stale=True)[0] == _cache_payload("full")


def test_background_rebuild_stores_under_the_pre_build_source_stamp(monkeypatch):
    """A payload built while the source changed must not be stamped fresh.

    The store-time stamp re-read marks a payload built from an obsolete row set
    as a cache hit, so the next request would serve it without rebuilding —
    defeating the commit-47d8ac94 invariant for up to the TTL. The background
    rebuild therefore stores the payload with the stamp it was BUILT from; the
    next request classifies the entry correctly (structural → synchronous
    rebuild, volatile → stale-while-revalidate)."""
    routes._session_list_cache_clear()
    stamps = [("s0", "v0")]
    monkeypatch.setattr(routes, "_session_list_cache_source_stamp", lambda _key: stamps[0])
    key = _cache_key()
    started = threading.Event()
    release = threading.Event()

    def _builder():
        started.set()
        release.wait(5.0)
        stamps[0] = ("s1", "v1")  # the source changes mid-build
        return _cache_payload("full")

    owner_result = {}

    def _owner():
        owner_result["payload"] = routes._get_cached_session_list_payload(
            key=key,
            builder=_builder,
            fast_builder=lambda: _cache_payload("fast"),
        )

    thread = threading.Thread(target=_owner)
    thread.start()
    try:
        assert started.wait(1.0)
    finally:
        release.set()
        thread.join(5.0)

    deadline = time.monotonic() + 5.0
    entry = None
    while time.monotonic() < deadline:
        with routes._SESSIONS_CACHE_LOCK:
            entry = routes._SESSIONS_CACHE.get(key)
        if entry is not None:
            break
        time.sleep(0.01)
    assert entry is not None, "the background rebuild must still store the payload"
    _ts, stored_stamp, payload = entry
    assert stored_stamp == ("s0", "v0"), (
        "the background store must keep the pre-build stamp, not the store-time one"
    )
    assert payload == _cache_payload("full")
    cached, is_fresh = routes._session_list_cache_get(key, allow_stale=True)
    assert cached == _cache_payload("full")
    assert is_fresh is False
    assert routes._session_list_cache_stale_reason(key) == "source"


@pytest.mark.parametrize("overrides,eligible", [
    ({}, True),
    ({"sidebar_source": "webui"}, True),
    ({"sidebar_source": "cli"}, True),
    ({"all_profiles": True}, False),
    ({"include_archived": True}, False),
    ({"archived_limit": 50}, False),
    ({"source_filter": "cron"}, False),
    ({"visible_only": False}, False),
    ({"sidebar_source": "cron"}, False),
])
def test_fast_shape_gate(overrides, eligible):
    """The gate is about the request SHAPE only: exclude_hidden and the
    show_* settings flags do not change eligibility (parity is asserted for
    those shapes in the payload parity test)."""
    args = dict(
        all_profiles=False,
        visible_only=True,
        include_archived=False,
        archived_limit=None,
        source_filter=None,
        sidebar_source=None,
    )
    args.update(overrides)
    assert routes._session_list_fast_shape_eligible(**args) is eligible


class _GetHandler:
    """Minimal GET-handler double for ``/api/sessions`` route tests."""

    def __init__(self, path):
        self.path = path
        self.headers = {}
        self.client_address = ("127.0.0.1", 12345)
        self.status = None
        from io import BytesIO

        self.wfile = BytesIO()

    def send_response(self, status):
        self.status = status

    def send_header(self, key, value):
        pass

    def end_headers(self):
        pass

    @property
    def response_json(self):
        return json.loads(self.wfile.getvalue().decode("utf-8"))


def test_sessions_route_serves_fast_payload_only_for_default_shape(monkeypatch, tmp_path):
    from urllib.parse import urlparse

    _install_fixture(monkeypatch, tmp_path)
    monkeypatch.setattr(routes, "load_settings", lambda: json.loads(
        (tmp_path / "webui" / "settings.json").read_text(encoding="utf-8")
    ))
    monkeypatch.setattr(routes, "_session_list_cache_source_stamp", lambda _key: ("stable",))

    fast_calls = []

    def _fast_builder(**kwargs):
        fast_calls.append(kwargs)
        return _cache_payload("fast", webui_session_count=1, cli_session_count=0)

    monkeypatch.setattr(routes, "_build_session_list_fast_payload", _fast_builder)

    default = _GetHandler("/api/sessions?sidebar_source=webui&exclude_hidden=1")
    routes.handle_get(default, urlparse(default.path))
    assert default.status == 200
    assert [r["session_id"] for r in default.response_json["sessions"]] == ["fast"]
    assert len(fast_calls) == 1

    archive = _GetHandler("/api/sessions?sidebar_source=webui&include_archived=1&archived_limit=50")
    routes.handle_get(archive, urlparse(archive.path))
    assert archive.status == 200
    assert [r["session_id"] for r in archive.response_json["sessions"]] != ["fast"]
    assert len(fast_calls) == 1, "archive/paged shapes must keep the full builder"


def test_cold_route_with_claude_code_enabled_returns_the_full_builder_rows(monkeypatch, tmp_path):
    """Cold first paint, Claude Code sessions enabled: the served rows ARE the
    full builder's rows for the same shape.

    JSONL-backed rows exist only in the scan's output (a JSONL-backed session has
    no state.db row), so a fast builder that skipped the scan served a short
    first response — valid sessions missing until the background rebuild landed,
    and missing for good if the client never refetched. The route-level contract
    is the reviewer's: same shape, same session set, scan included.
    """
    from urllib.parse import urlparse

    _install_fixture(monkeypatch, tmp_path)
    monkeypatch.setattr(
        models, "get_claude_code_sessions", lambda: [_jsonl_fixture_row()], raising=False,
    )
    monkeypatch.setattr(routes, "load_settings", lambda: json.loads(
        (tmp_path / "webui" / "settings.json").read_text(encoding="utf-8")
    ))
    monkeypatch.setattr(routes, "_session_list_cache_source_stamp", lambda _key: ("stable",))

    handler = _GetHandler("/api/sessions?exclude_hidden=1")
    routes.handle_get(handler, urlparse(handler.path))

    assert handler.status == 200
    served = [r["session_id"] for r in handler.response_json["sessions"]]
    assert "claude_code_deadbeefdeadbeefdeadbeef" in served, (
        "the cold first paint must include the JSONL-backed Claude Code session"
    )
    full = _build_full(exclude_hidden=True)
    assert served == [r["session_id"] for r in full["sessions"]]
