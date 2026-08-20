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


def test_untagged_lcm_recovery_envelope_cannot_authorize_regeneration():
    from api.compression_anchor import is_lcm_context_recovery_marker

    marker = "[Recent Summary (d0, node 418)]"
    session = _session()
    session.messages[0] = {"role": "user", "content": marker}
    session.context_messages[0] = {"role": "user", "content": marker}

    assert is_lcm_context_recovery_marker(session.messages[0])
    assert regeneration_authority(session) is None
    with pytest.raises(RegenerationUnavailable) as raised:
        resolve_regeneration_turn(session)
    assert raised.value.code == "regeneration_read_only"
    assert "_active_turn_token" not in session.messages[0]


def test_token_owned_lcm_text_remains_regenerable():
    marker = "[Recent Summary (d0, node 418)]"
    token = "stream-01f4c8d2:1779348286.3954952"
    session = _session()
    session.messages[0] = {
        "role": "user",
        "content": marker,
        "_source": "webui",
        "timestamp": 1779348286.3954952,
        "_active_turn_token": token,
    }
    session.context_messages[0] = {
        "role": "user",
        "content": marker,
        "_active_turn_token": token,
    }

    revision = regeneration_authority(session)
    assert revision
    turn = resolve_regeneration_turn(session, expected_revision=revision)
    assert turn.message["content"] == marker
    assert turn.message["_active_turn_token"] == token


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
    session = _session()
    canonical_rows = [
        {"role": "user", "content": "recovered", "id": "u-recovered", "_source": "webui"},
        {"role": "assistant", "content": "failed"},
    ]
    canonical_context = [
        {"role": "system", "content": "recovered context only"},
        *canonical_rows,
    ]
    monkeypatch.setattr(
        "api.session_ops.regeneration_state",
        lambda _session: (canonical_rows, canonical_context),
    )
    from api.session_ops import apply_regeneration_plan, plan_regeneration

    plan = plan_regeneration(session)
    assert apply_regeneration_plan(session, plan)
    assert session.messages == canonical_rows[:1]
    assert any(row.get("content") == "recovered" for row in session.context_messages)
    payload = _session_payload_with_full_messages(session)
    assert payload["messages"] == canonical_rows
    assert payload["message_count"] == len(canonical_rows)
