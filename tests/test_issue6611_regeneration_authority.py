import json
import shutil
import subprocess
from pathlib import Path

import pytest

from api.helpers import redact_session_data
from api.models import Session
from api.session_ops import (
    RegenerationUnavailable,
    regeneration_authority,
    regeneration_revision_for,
    regeneration_state,
    resolve_regeneration_turn,
)
from api.streaming import _session_payload_with_full_messages
from tests.js_source_extract import extract_function


ROOT = Path(__file__).resolve().parents[1]


def _session():
    return Session(
        session_id="authority6611",
        messages=[{"role": "user", "content": "p", "_source": "webui"}, {"role": "assistant", "content": "a"}],
        context_messages=[{"role": "user", "content": "p"}, {"role": "assistant", "content": "a"}],
    )


def test_all_terminal_payloads_carry_fresh_revision():
    session = _session()
    payload = _session_payload_with_full_messages(session)
    assert payload["regeneration_revision"] == regeneration_authority(session, rows=session.messages)
    assert payload["regeneration_revision"] == regeneration_revision_for(session.messages, session=session, context=session.context_messages)


def test_get_revision_consumer_delegates_imported_ownership_to_shared_authority():
    from pathlib import Path

    source = (Path(__file__).parents[1] / "api" / "routes.py").read_text(encoding="utf-8")
    get_start = source.index("imported_turn_marker = any(")
    body = source[get_start:get_start + 900]
    assert "regeneration_authority(" in body
    assert "imported_turn_marker" in body


def test_private_active_turn_token_never_public():
    session = _session()
    session.messages[-2]["_active_turn_token"] = "private"
    public = redact_session_data(session.compact() | {"messages": session.messages})
    assert "_active_turn_token" not in str(public)


def test_private_active_turn_token_is_redacted_in_nested_context_and_journal():
    session = _session()
    session_dict = session.compact() | {
        "messages": session.messages,
        "context_messages": [
            {
                "role": "user",
                "content": "p",
                "_active_turn_token": "secret",
                "_fork_child_turn": "authority6611",
            }
        ],
        "runtime_journal_snapshot": {
            "context_messages": [
                {
                    "role": "user",
                    "content": "p",
                    "_active_turn_token": "secret",
                    "_fork_child_turn": "authority6611",
                }
            ]
        },
    }
    public = redact_session_data(session_dict)
    assert public["context_messages"][0].get("_active_turn_token") is None
    assert public["context_messages"][0].get("_fork_child_turn") is None
    assert public["runtime_journal_snapshot"]["context_messages"][0].get("_active_turn_token") is None
    assert public["runtime_journal_snapshot"]["context_messages"][0].get("_fork_child_turn") is None


def test_public_active_turn_marker_matches_only_the_active_user_row():
    from api.process_event_utils import build_active_turn_token

    session = _session()
    session.active_stream_id = "stream-active"
    session.pending_started_at = 123.0
    token = build_active_turn_token(session.active_stream_id, session.pending_started_at)
    session.messages[0]["_active_turn_token"] = token
    session.messages.insert(0, {"role": "user", "content": "older", "_active_turn_token": "other"})
    public = redact_session_data(
        session.compact()
        | {
            "active_stream_id": session.active_stream_id,
            "pending_started_at": session.pending_started_at,
            "messages": session.messages,
            "context_messages": [
                {"role": "user", "content": "older", "_active_turn_token": "other"},
                {"role": "user", "content": "p", "_active_turn_token": token},
            ],
        }
    )
    assert public["messages"][1]["_active_turn_user"] is True
    assert "_active_turn_user" not in public["messages"][0]
    assert public["context_messages"][1]["_active_turn_user"] is True
    assert "_active_turn_token" not in str(public)


def test_public_active_turn_marker_is_consumed_by_browser_with_timestamp_drift():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required for the browser projection probe")

    from api.process_event_utils import build_active_turn_token

    stream_id = "projection-browser-stream"
    started_at = 123.5
    token = build_active_turn_token(stream_id, started_at)
    public = redact_session_data(
        {
            "active_stream_id": stream_id,
            "pending_started_at": started_at,
            "messages": [
                {
                    "role": "user",
                    "content": "same prompt",
                    "timestamp": 100.0,
                    "_active_turn_token": token,
                }
            ],
        }
    )
    projected = public["messages"][0]
    assert projected["_active_turn_user"] is True
    assert "_active_turn_token" not in projected

    ui_source = (ROOT / "static" / "ui.js").read_text(encoding="utf-8")
    functions = "\n".join(
        extract_function(ui_source, name)
        for name in (
            "_messageTimestampSeconds",
            "_activeTurnTokenMatches",
            "_pendingActiveTurnUserMessage",
        )
    )
    script = f"""
const _PENDING_ACTIVE_TURN_TS_EPSILON=1e-6;
{functions}
const messages={json.dumps([projected])};
const session={{active_stream_id:{json.dumps(stream_id)},pending_started_at:{started_at + 1}}};
const selected=_pendingActiveTurnUserMessage(messages,session);
if(selected!==messages[0]) throw new Error('projected active row was not selected');
process.stdout.write(JSON.stringify({{marker:selected._active_turn_user,token:selected._active_turn_token||null}}));
"""
    result = subprocess.run([node, "-e", script], text=True, capture_output=True, check=True)
    assert json.loads(result.stdout) == {"marker": True, "token": None}


def test_parent_or_foreign_source_refuses_authority():
    session = _session()
    session.messages[-2]["_source"] = "cron"
    assert regeneration_authority(session, rows=session.messages) is None
    try:
        resolve_regeneration_turn(session)
    except RegenerationUnavailable as exc:
        assert exc.code == "regeneration_read_only"
    else:
        raise AssertionError("foreign source was accepted")


def test_writable_imported_session_accepts_only_a_marked_final_user_turn(monkeypatch, tmp_path):
    from api.process_event_utils import build_active_turn_token
    from api import models as models_api

    session = _session()
    session.session_source = "cli"
    session.is_cli_session = True
    session.raw_source = "cli"
    session.source_tag = "cli"
    session.messages[-2]["_source"] = "cli"
    session.active_stream_id = "imported-stream"
    session.pending_started_at = 123.0
    token = build_active_turn_token(session.active_stream_id, session.pending_started_at)
    session.active_stream_id = None
    session.pending_started_at = None
    session.messages[-2]["_active_turn_token"] = token
    monkeypatch.setattr(models_api, "SESSION_DIR", tmp_path)
    monkeypatch.setattr(models_api, "SESSION_INDEX_FILE", tmp_path / "_index.json")
    session.save(touch_updated_at=False)
    session = Session.load(session.session_id)
    revision = regeneration_authority(session)
    assert revision
    assert resolve_regeneration_turn(session, expected_revision=revision).message["content"] == "p"
    assert _session_payload_with_full_messages(session)["regeneration_revision"] == revision


def test_get_and_terminal_consumers_emit_imported_marked_revision(monkeypatch):
    from types import SimpleNamespace
    from urllib.parse import urlparse
    from api import models as models_api
    from api import routes
    from api.process_event_utils import build_active_turn_token

    session = _session()
    session.session_source = "desktop"
    session.is_cli_session = True
    session.raw_source = "desktop"
    session.source_tag = "desktop"
    session.messages[-2]["_source"] = "desktop"
    session.messages[-2]["_active_turn_token"] = build_active_turn_token("imported-stream", 123.0)
    revision = regeneration_authority(session)
    captured = {}

    def capture(_handler, data, status=200, **_kwargs):
        captured["data"] = data
        captured["status"] = status

    monkeypatch.setattr(routes, "get_session", lambda *_args, **_kwargs: session)
    monkeypatch.setattr(routes, "_session_visible_to_active_profile", lambda *_args: True)
    monkeypatch.setattr(routes, "_clear_stale_stream_state", lambda *_args: None)
    monkeypatch.setattr(routes, "_session_requires_cli_metadata_lookup", lambda *_args: False)
    monkeypatch.setattr(routes, "get_state_db_session_messages", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(models_api, "get_state_db_session_messages", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(routes, "_resolve_effective_session_model_for_display", lambda *_args: None)
    monkeypatch.setattr(routes, "_resolve_effective_session_model_provider_for_display", lambda *_args: None)
    monkeypatch.setattr(routes, "j", capture)

    routes.handle_get(
        SimpleNamespace(_safe_webui_print=lambda *_args: None),
        urlparse(f"/api/session?session_id={session.session_id}&messages=1&resolve_model=0"),
    )

    assert captured["status"] == 200
    assert captured["data"]["session"]["regeneration_revision"] == revision
    assert _session_payload_with_full_messages(session)["regeneration_revision"] == revision


def test_get_and_terminal_consumers_omit_imported_unowned_revision(monkeypatch):
    from types import SimpleNamespace
    from urllib.parse import urlparse
    from api import models as models_api
    from api import routes

    session = _session()
    session.session_source = "desktop"
    session.is_cli_session = True
    session.raw_source = "desktop"
    session.source_tag = "desktop"
    session.messages[-2]["_source"] = "desktop"
    captured = {}

    def capture(_handler, data, status=200, **_kwargs):
        captured["data"] = data
        captured["status"] = status

    monkeypatch.setattr(routes, "get_session", lambda *_args, **_kwargs: session)
    monkeypatch.setattr(routes, "_session_visible_to_active_profile", lambda *_args: True)
    monkeypatch.setattr(routes, "_clear_stale_stream_state", lambda *_args: None)
    monkeypatch.setattr(routes, "_session_requires_cli_metadata_lookup", lambda *_args: False)
    monkeypatch.setattr(routes, "get_state_db_session_messages", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(models_api, "get_state_db_session_messages", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(routes, "_resolve_effective_session_model_for_display", lambda *_args: None)
    monkeypatch.setattr(routes, "_resolve_effective_session_model_provider_for_display", lambda *_args: None)
    monkeypatch.setattr(routes, "j", capture)

    routes.handle_get(
        SimpleNamespace(_safe_webui_print=lambda *_args: None),
        urlparse(f"/api/session?session_id={session.session_id}&messages=1&resolve_model=0"),
    )

    assert captured["status"] == 200
    assert "regeneration_revision" not in captured["data"]["session"]
    assert "regeneration_revision" not in _session_payload_with_full_messages(session)


@pytest.mark.parametrize(
    "marker,read_only,expected",
    [(None, False, None), ("bad-token", False, None), ("valid-earlier", False, None), ("valid-final", True, None)],
)
def test_imported_turn_matrix_rejects_unowned_or_read_only_rows(marker, read_only, expected):
    from api.process_event_utils import build_active_turn_token

    session = _session()
    session.session_source = "desktop"
    session.is_cli_session = True
    session.read_only = read_only
    valid = build_active_turn_token("imported-stream", 123.0)
    if marker == "valid-earlier":
        session.messages[-2]["_active_turn_token"] = valid
        session.messages.append({"role": "user", "content": "foreign", "_source": "desktop"})
        session.messages.append({"role": "assistant", "content": "foreign answer"})
    elif marker == "valid-final":
        session.messages[-2]["_active_turn_token"] = valid
    elif marker:
        session.messages[-2]["_active_turn_token"] = marker
    assert regeneration_authority(session) is expected
    assert "regeneration_revision" not in _session_payload_with_full_messages(session)


def test_fork_child_has_regeneration_authority():
    session = _session()
    session.session_source = "fork"
    session.parent_session_id = "parent-6611"
    session.messages[-2]["_fork_child_turn"] = session.session_id
    revision = regeneration_authority(session)
    assert revision
    assert resolve_regeneration_turn(session, expected_revision=revision).source == "webui"


def test_fork_of_fork_copied_parent_marker_refuses_authority():
    session = _session()
    session.session_source = "fork"
    session.parent_session_id = "parent-6611"
    session.messages[-2]["_fork_child_turn"] = "parent-6611"
    assert regeneration_authority(session) is None
    try:
        resolve_regeneration_turn(session)
    except RegenerationUnavailable as exc:
        assert exc.code == "regeneration_read_only"
    else:
        raise AssertionError("parent fork marker was accepted by child")


def test_current_fork_child_materialization_binds_and_accepts_its_new_turn(monkeypatch):
    from api import routes

    session = Session(
        session_id="fork-child-current-6611",
        messages=[
            {"role": "user", "content": "parent prompt", "_fork_child_turn": "parent-6611"},
            {"role": "assistant", "content": "parent answer"},
        ],
        context_messages=[
            {"role": "user", "content": "parent prompt"},
            {"role": "assistant", "content": "parent answer"},
        ],
        session_source="fork",
        parent_session_id="parent-6611",
    )
    monkeypatch.setattr(routes, "register_session_writeback_owner", lambda *_args: None)
    monkeypatch.setattr(routes, "get_webui_session_save_mode", lambda: "eager")
    routes._prepare_chat_start_session_for_stream(
        session,
        msg="current child prompt",
        attachments=[],
        workspace="C:/workspace",
        model="model",
        model_provider="provider",
        stream_id="fork-current-stream",
        started_at=123.0,
        defer_save=True,
    )
    assert session.messages[-1]["_fork_child_turn"] == session.session_id
    session.messages.append({"role": "assistant", "content": "current answer"})
    session.active_stream_id = None
    session.pending_user_message = None
    session.pending_attachments = []
    session.pending_started_at = None
    session.pending_user_source = None
    revision = regeneration_authority(session)
    assert revision
    assert resolve_regeneration_turn(session, expected_revision=revision).message["content"] == "current child prompt"


def test_fork_without_child_lineage_refuses_authority():
    session = _session()
    session.session_source = "fork"
    session.parent_session_id = None
    assert regeneration_authority(session) is None
    try:
        resolve_regeneration_turn(session)
    except RegenerationUnavailable as exc:
        assert exc.code == "regeneration_read_only"
    else:
        raise AssertionError("parent-only fork state was accepted")


def test_authority_withholds_a_noncanonical_display_projection():
    session = _session()
    canonical_rows, canonical_context = regeneration_state(session)
    projected_rows = canonical_rows + [{"role": "tool", "content": "stitched parent"}]
    assert regeneration_authority(
        session,
        rows=projected_rows,
        context=canonical_context,
    ) is None


def test_terminal_payload_embeds_the_rows_it_hashes(monkeypatch):
    session = _session()
    canonical_rows = [
        {"role": "user", "content": "recovered", "_source": "webui"},
        {"role": "assistant", "content": "answer"},
    ]
    canonical_context = list(canonical_rows)
    monkeypatch.setattr(
        "api.session_ops.regeneration_state",
        lambda _session: (canonical_rows, canonical_context),
    )
    payload = _session_payload_with_full_messages(session)
    assert payload["messages"] == canonical_rows
    assert payload["message_count"] == len(canonical_rows)
    assert payload["regeneration_revision"] == regeneration_authority(
        session,
        rows=canonical_rows,
        context=canonical_context,
    )


def test_recovered_display_context_pair_survives_local_and_gateway_apply(monkeypatch):
    """#6611: the recovered display/context pair survives apply on BOTH paths.

    The state.db authority path (empty sidecar, recovery) and the #6826
    sidecar-anchored path (pair already loaded in memory) must both plan the
    same canonical pair, and the recovered pair must survive local and
    gateway apply on each path.
    """
    from api import models as models_api

    canonical_rows = [
        {"role": "user", "content": "recovered", "id": "u-recovered", "_source": "webui", "timestamp": 100.0},
        {"role": "assistant", "content": "failed", "timestamp": 101.0},
    ]
    canonical_context = [
        {"role": "system", "content": "recovered context only", "timestamp": 99.0},
        *canonical_rows,
    ]
    monkeypatch.setattr(
        models_api,
        "get_state_db_session_messages",
        lambda *_args, **_kwargs: [dict(row) for row in canonical_rows],
    )
    from api.session_ops import apply_regeneration_plan, plan_regeneration

    # --- state.db authority path: empty sidecar (recovery), the authority
    # reconciles the state.db snapshot into the canonical pair.
    session = _session()
    session.messages = []
    session.context_messages = []
    plan = plan_regeneration(session)
    assert plan.canonical_rows == canonical_rows
    assert plan.canonical_context == canonical_rows
    payload = _session_payload_with_full_messages(session)
    assert payload["messages"] == canonical_rows
    assert payload["message_count"] == len(canonical_rows)
    assert apply_regeneration_plan(session, plan)
    assert session.messages == canonical_rows[:1]
    assert any(row.get("content") == "recovered" for row in session.context_messages)

    # --- sidecar-anchored path: the recovered pair is already loaded in
    # memory; the bounded state.db tail read must not lose it, and the same
    # canonical pair must survive local + gateway apply.
    session = _session()
    session.messages = [dict(row) for row in canonical_rows]
    session.context_messages = [dict(row) for row in canonical_context]
    plan = plan_regeneration(session)
    assert plan.canonical_rows == canonical_rows
    assert plan.canonical_context == canonical_context
    payload = _session_payload_with_full_messages(session)
    assert payload["messages"] == canonical_rows
    assert payload["message_count"] == len(canonical_rows)
    assert apply_regeneration_plan(session, plan)
    assert session.messages == canonical_rows[:1]
    assert any(row.get("content") == "recovered" for row in session.context_messages)


def test_regeneration_sidecar_path_avoids_full_transcript_refetch(monkeypatch):
    """#6826: regenerate reads a bounded state.db tail, not the full transcript.

    The full authority read materializes the entire state.db transcript (the
    >1min stall on large sessions).  The sidecar-anchored regeneration path
    must pass ``since_timestamp`` so the SQL scan is bounded, while still
    reconciling through ``reconciled_state_db_messages_for_session`` — a
    state.db-only gateway tail row must be picked up identically on both
    paths (authority preserved).
    """
    from api import models as models_api
    from api.session_ops import regeneration_state

    session = _session()
    session.messages = [
        {"role": "user", "content": f"m{index}", "timestamp": float(index)}
        for index in range(500)
    ]
    session.context_messages = [dict(row) for row in session.messages]
    # state.db has the same transcript plus a gateway-applied tail the sidecar
    # has not seen yet.
    state_rows = [dict(row) for row in session.messages]
    state_rows.append(
        {"role": "assistant", "content": "gateway applied turn", "timestamp": 500.0}
    )

    calls = []

    def fake_get_state_db_session_messages(*_args, **_kwargs):
        calls.append(dict(_kwargs))
        since = _kwargs.get("since_timestamp")
        if since is None:
            return [dict(row) for row in state_rows]
        return [
            dict(row)
            for row in state_rows
            if (row.get("timestamp") or 0) >= since
        ]

    monkeypatch.setattr(
        models_api,
        "get_state_db_session_messages",
        fake_get_state_db_session_messages,
    )

    # The bounded guard must prove the skipped prefix (rows < floor) is
    # identical in the sidecar and return the tail from ONE snapshot. The fake
    # state.db carries the same 300 prefix rows as the sidecar.
    from api.models import _session_message_visible_key

    floor = min(row["timestamp"] for row in session.messages[-200:])
    prefix_rows = [r for r in session.messages if (r.get("timestamp") or 0) < floor]
    tail_rows = [r for r in state_rows if (r.get("timestamp") or 0) >= floor]
    monkeypatch.setattr(
        models_api,
        "get_state_db_regeneration_tail_snapshot",
        lambda *_a, **_k: {
            "prefix": {"count": len(prefix_rows), "null_timestamp_count": 0},
            "prefix_keys": [_session_message_visible_key(r) for r in prefix_rows],
            "tail": [dict(r) for r in tail_rows],
            "tail_keys": [_session_message_visible_key(r) for r in tail_rows],
        },
    )

    full_rows, full_context = regeneration_state(session)
    assert calls[-1].get("since_timestamp") is None  # full read

    # provably effective: the fast path does NOT consult the full reader — the
    # bounded tail comes from the single snapshot
    calls_before = len(calls)
    fast_rows, fast_context = regeneration_state(session, use_sidecar=True)
    assert len(calls) == calls_before, "bounded path must not touch the full reader"

    # authority preserved: identical reconciled transcript on both paths,
    # including the state.db-only gateway tail row.
    assert fast_rows == full_rows
    assert fast_context == full_context
    assert fast_rows[-1]["content"] == "gateway applied turn"


# ── #6826 r3: bounded tail-read guard ────────────────────────────────────────

def _sidecar_session(rows=3, *, anchor_key=None, anchor_ts=None):
    """Session whose sidecar carries `rows` timestamped display messages."""
    messages = [
        {"role": "user" if i % 2 == 0 else "assistant", "content": f"m{i}", "timestamp": 100.0 + i * 100.0}
        for i in range(rows)
    ]
    s = Session(
        session_id="sidecar-guard-6826",
        messages=messages,
        context_messages=[dict(m) for m in messages],
    )
    if anchor_key:
        s.compression_anchor_message_key = anchor_key
        if anchor_ts is not None:
            s._anchor_ts = anchor_ts
    return s


def _snap(prefix_count=0, prefix_keys=None, tail=None, tail_keys=None):
    """Factory for a fake get_state_db_regeneration_tail_snapshot result."""
    tail = tail if tail is not None else []
    return {
        "prefix": {"count": prefix_count, "null_timestamp_count": 0},
        "prefix_keys": prefix_keys or [],
        "tail": tail,
        "tail_keys": tail_keys if tail_keys is not None else [],
    }


def test_bounded_guard_empty_prefix_allows_tail_read(monkeypatch):
    from api import session_ops

    session = _sidecar_session(3)
    monkeypatch.setattr(
        "api.models.get_state_db_regeneration_tail_snapshot",
        lambda *_a, **_k: _snap(tail=[{"role": "user", "content": "t", "timestamp": 300.0}]),
    )
    seen = {}
    monkeypatch.setattr(
        "api.models.get_state_db_session_messages",
        lambda *_a, **_k: _capture(seen, _k),
    )
    session_ops.regeneration_state(session, use_sidecar=True)
    # bounded path: full reader NOT consulted (tail comes from the snapshot)
    assert seen == {}, "empty skipped prefix must take the bounded tail from the snapshot"


def test_bounded_guard_unrepresented_prefix_row_falls_back(monkeypatch):
    from api import session_ops

    session = _sidecar_session(3)
    # state.db has one row older than the floor that the sidecar does NOT carry
    monkeypatch.setattr(
        "api.models.get_state_db_regeneration_tail_snapshot",
        lambda *_a, **_k: _snap(
            prefix_count=1,
            prefix_keys=[("user", "recoverable-only-in-db")],
        ),
    )
    seen = {}
    monkeypatch.setattr(
        "api.models.get_state_db_session_messages",
        lambda *_a, **_k: _capture(seen, _k),
    )
    session_ops.regeneration_state(session, use_sidecar=True)
    assert seen != {}, "unrepresented prefix row must force the full read (full reader consulted)"


def test_bounded_guard_repeated_tail_key_falls_back(monkeypatch):
    """#6826 r3 #1: a bounded-tail key that ALSO occurs in the skipped prefix
    must force the full read (occurrence-count collision would drop the
    repeated tail row)."""
    from api import session_ops

    session = _sidecar_session(3)
    repeated_key = ("user", "m0")  # m0 lives below the floor AND repeats in the tail
    monkeypatch.setattr(
        "api.models.get_state_db_regeneration_tail_snapshot",
        lambda *_a, **_k: _snap(
            prefix_count=1,
            prefix_keys=[repeated_key],
            tail=[{"role": "user", "content": "m0", "timestamp": 300.0}],
            tail_keys=[repeated_key],
        ),
    )
    seen = {}
    monkeypatch.setattr(
        "api.models.get_state_db_session_messages",
        lambda *_a, **_k: _capture(seen, _k),
    )
    session_ops.regeneration_state(session, use_sidecar=True)
    assert seen != {}, "repeated tail key must force the full read"


def test_bounded_guard_compression_anchor_below_floor_falls_back(monkeypatch):
    from api import session_ops

    session = _sidecar_session(3)
    # anchor timestamp predates the floor → bounded read cannot cover it
    session.compression_anchor_message_key = {"role": "user", "text": "m0", "ts": 100.0}
    monkeypatch.setattr(
        "api.models.get_state_db_regeneration_tail_snapshot",
        lambda *_a, **_k: _snap(),
    )
    # shrink the anchor budget window so the floor (300.0) sits above the anchor
    monkeypatch.setattr(session_ops, "_REGENERATION_SIDECAR_ANCHOR_BUDGET", 1)
    seen = {}
    monkeypatch.setattr(
        "api.models.get_state_db_session_messages",
        lambda *_a, **_k: _capture(seen, _k),
    )
    session_ops.regeneration_state(session, use_sidecar=True)
    assert seen != {}, "compression anchor below floor must force the full read"


def test_bounded_guard_compression_anchor_covered_allows(monkeypatch):
    from api import session_ops

    session = _sidecar_session(3)
    session.compression_anchor_message_key = {"role": "user", "text": "m0", "ts": 100.0}
    # budget 200 covers all 3 rows → floor = min(all) = 100 → anchor at floor is covered
    monkeypatch.setattr(
        "api.models.get_state_db_regeneration_tail_snapshot",
        lambda *_a, **_k: _snap(tail=[{"role": "user", "content": "t", "timestamp": 300.0}]),
    )
    seen = {}
    monkeypatch.setattr(
        "api.models.get_state_db_session_messages",
        lambda *_a, **_k: _capture(seen, _k),
    )
    session_ops.regeneration_state(session, use_sidecar=True)
    assert seen == {}, "anchor covered by floor must keep the bounded tail read"


def test_bounded_guard_missing_snapshot_falls_back(monkeypatch):
    """#6826 r3 #2 (TOCTOU): if a stable single-connection snapshot cannot be
    obtained, the guard must refuse the bounded path entirely."""
    from api import session_ops

    session = _sidecar_session(3)
    monkeypatch.setattr(
        "api.models.get_state_db_regeneration_tail_snapshot",
        lambda *_a, **_k: None,
    )
    seen = {}
    monkeypatch.setattr(
        "api.models.get_state_db_session_messages",
        lambda *_a, **_k: _capture(seen, _k),
    )
    session_ops.regeneration_state(session, use_sidecar=True)
    assert seen != {}, "missing snapshot must force the full read (no TOCTOU window)"


def _capture(seen, kwargs):
    seen.update(kwargs)
    return []


def test_regeneration_tail_snapshot_reads_real_sqlite(monkeypatch, tmp_path):
    """Real-SQLite: the single-connection snapshot returns prefix proof + tail
    from one read, including the repeated-prompt collision shape from the
    round-3 review (a prompt that lives both below and above the floor)."""
    import sqlite3

    from api import models

    db = tmp_path / "state.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, "
        "content TEXT, timestamp REAL, tool_calls TEXT, active INTEGER)"
    )
    rows = [
        ("s1", "user", "prompt", 100.0),
        ("s1", "assistant", "a1", 200.0),
        ("s1", "user", "prompt", 300.0),   # repeated prompt above the floor
        ("s1", "assistant", "a2", 400.0),
    ]
    for sid, role, content, ts in rows:
        conn.execute(
            "INSERT INTO messages (session_id, role, content, timestamp, active) VALUES (?,?,?,?,1)",
            (sid, role, content, ts),
        )
    conn.commit()
    conn.close()

    monkeypatch.setattr(models, "_active_state_db_path", lambda: db)
    snap = models.get_state_db_regeneration_tail_snapshot("s1", 250.0)
    assert snap is not None
    assert snap["prefix"]["count"] == 2, "rows < floor must be counted"
    assert len(snap["prefix_keys"]) == 2
    assert len(snap["tail"]) == 2, "rows >= floor must be the bounded tail"
    # the repeated prompt key appears in BOTH the skipped prefix and the tail —
    # exactly the occurrence-count collision the guard must refuse
    assert snap["prefix_keys"][0] == snap["tail_keys"][0]


def _make_state_db(tmp_path, rows, with_id=True):
    """Create a real state.db with the given rows (list of dicts)."""
    import sqlite3

    db = tmp_path / "state.db"
    conn = sqlite3.connect(db)
    cols = ["session_id TEXT", "role TEXT", "content TEXT", "timestamp REAL",
            "tool_calls TEXT", "tool_name TEXT", "reasoning TEXT", "active INTEGER"]
    if with_id:
        cols.append("id INTEGER PRIMARY KEY")
    conn.execute(f"CREATE TABLE messages ({', '.join(cols)})")
    for i, r in enumerate(rows):
        conn.execute(
            "INSERT INTO messages (session_id, role, content, timestamp, tool_calls, tool_name, reasoning, active) "
            "VALUES (?,?,?,?,?,?,?,1)",
            ("s1", r.get("role"), r.get("content"), r.get("timestamp"),
             r.get("tool_calls"), r.get("tool_name"), r.get("reasoning")),
        )
    conn.commit()
    conn.close()
    return db


def test_snapshot_tail_uses_canonical_projection(monkeypatch, tmp_path):
    """#6826 r4 #1: the snapshot tail must use the SAME projection as the
    canonical reader — JSON tool_calls decoded, tool_name → name, no raw
    id/active exposure (tool-call/gateway-row sessions)."""
    from api import models

    db = _make_state_db(tmp_path, [
        {"role": "user", "content": "p", "timestamp": 100.0},
        {"role": "tool", "content": "result", "timestamp": 200.0,
         "tool_calls": '[{"name": "x", "arguments": {"a": 1}}]',
         "tool_name": "read_file", "reasoning": "r1"},
        {"role": "assistant", "content": "a", "timestamp": 300.0},
    ])
    monkeypatch.setattr(models, "_active_state_db_path", lambda: db)
    snap = models.get_state_db_regeneration_tail_snapshot("s1", 50.0)
    assert snap is not None
    tool = [m for m in snap["tail"] if m.get("role") == "tool"][0]
    assert isinstance(tool["tool_calls"], list), "tool_calls must be JSON-decoded"
    assert tool["name"] == "read_file", "tool_name → name must be applied"
    assert "id" not in tool and "active" not in tool, "raw id/active must not leak"
    assert "reasoning" in tool and tool["reasoning"] == "r1"


def test_snapshot_uses_durable_id_order(monkeypatch, tmp_path):
    """#6826 r4 #2: durable id ASC order (not timestamp) — a later-id user at
    ts=600 followed by its assistant at ts=550 must stay user → assistant."""
    from api import models

    db = _make_state_db(tmp_path, [
        {"role": "user", "content": "later user", "timestamp": 600.0},
        {"role": "assistant", "content": "assistant 550", "timestamp": 550.0},
    ])
    monkeypatch.setattr(models, "_active_state_db_path", lambda: db)
    snap = models.get_state_db_regeneration_tail_snapshot("s1", 50.0)
    assert snap is not None
    roles = [m.get("role") for m in snap["tail"]]
    assert roles == ["user", "assistant"], f"durable id order violated: {roles}"


class _FakeCursor:
    def __init__(self, conn):
        self._c = conn._real.cursor()
        self._conn = conn
        self._dv = None

    def execute(self, sql, *args):
        s = sql.strip()
        if s.startswith("PRAGMA data_version"):
            self._conn._dv_calls += 1
            self._dv = 42 if self._conn._dv_calls == 1 else 43  # changed on 2nd read
            return self
        return self._c.execute(sql, *args)

    def fetchone(self):
        if self._dv is not None:
            v, self._dv = self._dv, None
            return (v,)
        return self._c.fetchone()

    def fetchall(self):
        return self._c.fetchall()


class _FakeConn:
    def __init__(self, real):
        self._real = real
        self._dv_calls = 0

    @property
    def row_factory(self):
        return self._real.row_factory

    @row_factory.setter
    def row_factory(self, value):
        # the real cursor must see the row_factory, or the interleaving path
        # dies on tuple indexing instead of exercising data_version
        self._real.row_factory = value

    def cursor(self):
        return _FakeCursor(self)

    def close(self):
        self._real.close()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


def test_snapshot_refuses_when_wal_data_version_changes(monkeypatch, tmp_path):
    """#6826 r4 #3 (WAL TOCTOU): if PRAGMA data_version changes during the
    proof (a concurrent WAL commit), the snapshot must return None so the
    caller falls back to the full read."""
    import sqlite3

    from api import models

    db = _make_state_db(tmp_path, [
        {"role": "user", "content": "p", "timestamp": 100.0},
        {"role": "assistant", "content": "a", "timestamp": 200.0},
    ])
    real_conn = sqlite3.connect(db)
    fake = _FakeConn(real_conn)
    monkeypatch.setattr(models, "open_state_db_readonly", lambda _p: fake)
    snap = models.get_state_db_regeneration_tail_snapshot("s1", 50.0)
    assert snap is None, "changed data_version must refuse the bounded snapshot"


def test_in_tail_duplicate_guard_refuses_bounded_and_full_revision_accepted(monkeypatch, tmp_path):
    """#6826 r5: a repeated message wholly INSIDE the bounded tail must refuse
    the fast path (full-read fallback), so the minted full-read revision is
    accepted by plan_regeneration (no 409 stale_regeneration_revision)."""
    from api import models
    from api.session_ops import (
        plan_regeneration,
        regeneration_revision_for,
        regeneration_state,
    )

    # 212 rows (above the 200-row anchor budget); the user row at index 210
    # repeats the visible key of the user row at index 100 — a duplicate that
    # lives entirely within the tail. The final assistant row (211) answers
    # the current turn so the session is regenerable.
    rows = []
    for i in range(212):
        if i == 211:
            rows.append({"role": "assistant", "content": "final answer", "timestamp": 100.0 + i})
            continue
        role = "user" if i % 2 == 0 else "assistant"
        content = "m100" if i == 210 else f"m{i}"
        rows.append({"role": role, "content": content, "timestamp": 100.0 + i})
    db = _make_state_db(tmp_path, rows)
    monkeypatch.setattr(models, "_active_state_db_path", lambda: db)

    session = Session(
        session_id="s1",
        messages=[dict(r) for r in rows],
        context_messages=[dict(r) for r in rows],
    )
    # guard must refuse the bounded path (in-tail duplicate)
    floor = min(m["timestamp"] for m in session.messages[-200:])
    snap = models.get_state_db_regeneration_tail_snapshot("s1", floor)
    assert snap is not None
    assert len(snap["tail_keys"]) > len(set(snap["tail_keys"])), "fixture must repeat a key inside the tail"
    from api import session_ops
    assert session_ops._bounded_tail_snapshot_if_safe(session, floor) is None, \
        "in-tail duplicate must refuse the bounded path"

    # mint → validate: the full-read revision must be accepted
    full_rows, full_context = regeneration_state(session)
    full_rev = regeneration_revision_for(full_rows, session=session, context=full_context)
    assert full_rev
    plan = plan_regeneration(session, expected_revision=full_rev, lock_held=True)
    assert plan is not None and plan.revision == full_rev
