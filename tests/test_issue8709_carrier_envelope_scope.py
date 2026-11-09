"""#8709 — display projection scope requiring canonical compaction envelope.

Validates that project_compaction_carrier_for_display requires canonical envelope
evidence, enforces user/assistant role scope, distinguishes pure synthetic carriers
from authentic user turns, and projects structured message content without mutating
non-text blocks.
"""

from __future__ import annotations

import json
from io import BytesIO
from urllib.parse import urlparse

import api.models as models
import api.routes as routes
from api.compression_anchor import (
    COMPACTION_HANDOFF_DELIMITER_PREFIX,
    COMPACTION_HANDOFF_HEADER_PREFIX,
    project_compaction_carrier_for_display,
    project_compaction_carriers_for_display,
)

HEADER = "[PRIOR CONTEXT — for reference only; not a new message]"
DELIMITER = "[END OF PRIOR CONTEXT — COMPACTION SUMMARY BELOW]"
END_MARKER = (
    "--- END OF CONTEXT SUMMARY — respond to the message below, "
    "not the summary above ---"
)
RETAINED_STEP = "Let me check the failing assertion before editing the parser."
SUMMARY = (
    "[CONTEXT COMPACTION — REFERENCE ONLY] Earlier turns were compacted into the "
    "summary below.\n## Goal\nShip the carrier fix.\n## Active State\n"
    "- worktree wt-516-8709\n" + ("- padding detail line\n" * 40)
)
CARRIER_CONTENT = (
    HEADER + "\n" + RETAINED_STEP + "\n\n" + DELIMITER + "\n\n" + SUMMARY + "\n\n" + END_MARKER
)


class _Handler:
    def __init__(self):
        self.status = None
        self.response_headers = []
        self.wfile = BytesIO()

    def send_response(self, status):
        self.status = status

    def send_header(self, k, v):
        self.response_headers.append((k, v))

    def end_headers(self):
        pass

    def payload(self):
        return json.loads(self.wfile.getvalue().decode() or "{}")


def test_authored_header_without_delimiter_preserved_identity():
    # Case 1: [PRIOR CONTEXT] user-authored quote or notes (user role, no delimiter)
    msg = {"role": "user", "content": "[PRIOR CONTEXT] user-authored quote or notes"}
    projected = project_compaction_carrier_for_display(msg)
    assert projected is msg
    assert projected["content"] == "[PRIOR CONTEXT] user-authored quote or notes"


def test_authored_header_with_flag_and_no_delimiter_preserved():
    # Case 2: Same text with _compressed_summary: True and no delimiter => content preserved
    msg = {
        "role": "user",
        "content": "[PRIOR CONTEXT] user-authored quote or notes",
        "_compressed_summary": True,
    }
    projected = project_compaction_carrier_for_display(msg)
    assert projected is msg
    assert projected["content"] == "[PRIOR CONTEXT] user-authored quote or notes"
    assert projected["_compressed_summary"] is True


def test_bracket_form_and_mixed_case_header_preserved():
    # Case 3: Header with a bracket form and mixed case ([Prior context — my own note])
    msg = {"role": "assistant", "content": "[Prior context — my own note]"}
    projected = project_compaction_carrier_for_display(msg)
    assert projected is msg
    assert projected["content"] == "[Prior context — my own note]"


def test_tool_role_row_starting_with_header_prefix_preserved_untouched():
    # Case 4: Tool-role row whose content starts with [prior context and carries delimiter
    content = f"[prior context tool output] check log\n\n{DELIMITER}\n\n{SUMMARY}"
    msg = {"role": "tool", "content": content, "tool_call_id": "call_99"}
    projected = project_compaction_carrier_for_display(msg)
    assert projected is msg
    assert projected["content"] == content
    assert project_compaction_carriers_for_display([msg])[0] is msg


def test_truncated_envelope_header_authentic_head_no_delimiter_preserved():
    # Case 5: Truncated envelope: header, authentic head, no delimiter => preserved
    content = f"{HEADER}\nAuthentic user note without any delimiter"
    msg = {"role": "assistant", "content": content}
    projected = project_compaction_carrier_for_display(msg)
    assert projected is msg
    assert projected["content"] == content


def test_proven_pure_synthetic_carrier_both_shapes_project_to_empty():
    # Case 6: Proven pure synthetic carrier, both shapes => ""
    # Shape (a): header + summary + delimiter with an empty authentic head
    msg_a = {
        "role": "assistant",
        "content": f"{HEADER}\n\n{DELIMITER}\n\n{SUMMARY}\n\n{END_MARKER}",
        "_compressed_summary": True,
    }
    projected_a = project_compaction_carrier_for_display(msg_a)
    assert projected_a is not msg_a
    assert projected_a["content"] == ""
    assert "_compressed_summary" not in projected_a
    assert msg_a["_compressed_summary"] is True

    # Shape (b): header + closing ] + [context compaction ... summary text and no delimiter
    msg_b = {
        "role": "assistant",
        "content": f"{HEADER}\n\n{SUMMARY}\n\n{END_MARKER}",
        "_compressed_summary": True,
    }
    projected_b = project_compaction_carrier_for_display(msg_b)
    assert projected_b is not msg_b
    assert projected_b["content"] == ""
    assert "_compressed_summary" not in projected_b
    assert msg_b["_compressed_summary"] is True


def test_real_merged_carrier_removes_handoff_keeps_retained_text_and_tool_calls():
    # Case 7: Real merged carrier => handoff removed, authentic retained text and tool_calls kept
    tool_calls = [{"id": "call_1", "type": "function", "function": {"name": "read_file"}}]
    msg = {
        "role": "assistant",
        "content": CARRIER_CONTENT,
        "tool_calls": tool_calls,
        "timestamp": 4.0,
        "_compressed_summary": True,
    }
    projected = project_compaction_carrier_for_display(msg)
    assert projected is not msg
    assert projected["content"] == RETAINED_STEP
    assert projected["tool_calls"] == tool_calls
    assert projected["role"] == "assistant"
    assert projected["timestamp"] == 4.0
    assert "_compressed_summary" not in projected
    assert msg["_compressed_summary"] is True


def test_standalone_context_compaction_card_identical_object():
    # Case 8: Standalone [context compaction card => identical object
    card = {
        "role": "assistant",
        "content": "[context compaction — reference only] earlier turns were compacted.",
    }
    projected = project_compaction_carrier_for_display(card)
    assert projected is card
    assert projected["content"] == "[context compaction — reference only] earlier turns were compacted."


def test_content_none_int_and_list_without_text_block_identical_object():
    # Case 9: content that is None, an int, and a list with no text block => identical object
    msg_none = {"role": "assistant", "content": None}
    assert project_compaction_carrier_for_display(msg_none) is msg_none

    msg_int = {"role": "user", "content": 12345}
    assert project_compaction_carrier_for_display(msg_int) is msg_int

    msg_empty_list = {"role": "assistant", "content": []}
    assert project_compaction_carrier_for_display(msg_empty_list) is msg_empty_list

    msg_tool_block_only = {
        "role": "assistant",
        "content": [{"type": "tool_use", "id": "call_1", "name": "read_file", "input": {}}],
    }
    assert project_compaction_carrier_for_display(msg_tool_block_only) is msg_tool_block_only


def test_structured_merged_carrier_projects_text_block_preserves_non_text():
    # Case 10: Structured merged carrier: list of one text block with full envelope plus one tool_use block
    tool_use_block = {
        "type": "tool_use",
        "id": "call_1",
        "name": "read_file",
        "input": {"path": "api/compression_anchor.py"},
    }
    text_block = {"type": "text", "text": CARRIER_CONTENT}
    tool_calls = [{"id": "call_1", "type": "function", "function": {"name": "read_file"}}]
    msg = {
        "id": 42,
        "role": "assistant",
        "content": [text_block, tool_use_block],
        "tool_calls": tool_calls,
        "timestamp": 123.45,
        "_compressed_summary": True,
    }
    projected = project_compaction_carrier_for_display(msg)
    assert projected is not msg
    assert "_compressed_summary" not in projected
    assert msg["_compressed_summary"] is True
    assert projected["id"] == 42
    assert projected["role"] == "assistant"
    assert projected["timestamp"] == 123.45
    assert projected["tool_calls"] == tool_calls

    new_blocks = projected["content"]
    assert len(new_blocks) == 2
    # Summary removed from text block, authentic text retained
    assert new_blocks[0] == {"type": "text", "text": RETAINED_STEP}
    # tool_use block byte-identical
    assert new_blocks[1] is tool_use_block
    # Order kept
    assert new_blocks[0]["type"] == "text"
    assert new_blocks[1]["type"] == "tool_use"


def test_structured_proven_synthetic_legacy_shape_projects_to_empty():
    # Structured legacy form: header + summary text and no delimiter is still proven
    # synthetic, so the summary must not reach the payload; non-text blocks survive.
    tool_use_block = {"type": "tool_use", "id": "call_7", "name": "read_file", "input": {}}
    text_block = {"type": "text", "text": f"{HEADER}\n\n{SUMMARY}\n\n{END_MARKER}"}
    msg = {
        "role": "assistant",
        "content": [text_block, tool_use_block],
        "_compressed_summary": True,
    }
    projected = project_compaction_carrier_for_display(msg)
    assert projected is not msg
    assert "_compressed_summary" not in projected
    assert projected["content"][0] == {"type": "text", "text": ""}
    assert projected["content"][1] is tool_use_block


def test_structured_authored_header_without_envelope_is_preserved():
    # An authored structured row that merely starts with the header prefix must survive.
    text_block = {"type": "text", "text": "[PRIOR CONTEXT] user-authored quote or notes"}
    msg = {"role": "user", "content": [text_block], "timestamp": 9.0}
    projected = project_compaction_carrier_for_display(msg)
    assert projected is msg
    assert projected["content"][0] == text_block


def test_ledger_case_authored_row_survives_metadata_and_payload_paths(tmp_path):
    # Case 11: Ledger case: the same authored [PRIOR CONTEXT] row must survive both messages=0 and messages=1
    sess_dir = tmp_path / "sessions"
    sess_dir.mkdir()
    orig_dir, orig_index = models.SESSION_DIR, models.SESSION_INDEX_FILE
    models.SESSION_DIR = sess_dir
    models.SESSION_INDEX_FILE = sess_dir / "_index.json"
    models.SESSIONS.clear()

    saved = {
        "get_state_db_session_messages": getattr(routes, "get_state_db_session_messages", None),
        "_session_visible_to_active_profile": getattr(routes, "_session_visible_to_active_profile", None),
        "_clear_stale_stream_state": getattr(routes, "_clear_stale_stream_state", None),
        "_resolve_effective_session_model_for_display": getattr(routes, "_resolve_effective_session_model_for_display", None),
        "_resolve_effective_session_model_provider_for_display": getattr(routes, "_resolve_effective_session_model_provider_for_display", None),
    }

    sid = "ledger_test_sid"
    authored_row = {
        "role": "user",
        "content": "[PRIOR CONTEXT] user-authored quote or notes",
        "timestamp": 100.0,
    }
    try:
        s = models.Session(session_id=sid, messages=[authored_row])
        s.save()

        routes.get_state_db_session_messages = (
            lambda s_id, profile=None, since_timestamp=None, include_inactive=False, limit=None: [dict(authored_row)]
        )
        routes._session_visible_to_active_profile = lambda profile, handler: True
        routes._clear_stale_stream_state = lambda s: None
        routes._resolve_effective_session_model_for_display = lambda s: getattr(s, "model", None)
        routes._resolve_effective_session_model_provider_for_display = lambda s: getattr(s, "model_provider", None)

        # messages=0 metadata probe path
        h0 = _Handler()
        routes.handle_get(h0, urlparse(f"/api/session?session_id={sid}&messages=0&resolve_model=0"))
        assert h0.status == 200
        sess0 = h0.payload().get("session", {})
        assert sess0.get("message_count") == 1

        # messages=1 full payload path
        h1 = _Handler()
        routes.handle_get(h1, urlparse(f"/api/session?session_id={sid}&messages=1&resolve_model=0"))
        assert h1.status == 200
        msgs1 = h1.payload().get("session", {}).get("messages", [])
        assert len(msgs1) == 1
        assert msgs1[0]["content"] == "[PRIOR CONTEXT] user-authored quote or notes"
    finally:
        for name, val in saved.items():
            if val is not None:
                setattr(routes, name, val)
        models.SESSION_DIR, models.SESSION_INDEX_FILE = orig_dir, orig_index
        models.SESSIONS.clear()


def test_route_authored_user_and_tool_rows_reach_payload_intact(tmp_path):
    # Case 12: Route case: authored user row plus tool-role row reach /api/session with content intact
    sess_dir = tmp_path / "sessions"
    sess_dir.mkdir()
    orig_dir, orig_index = models.SESSION_DIR, models.SESSION_INDEX_FILE
    models.SESSION_DIR = sess_dir
    models.SESSION_INDEX_FILE = sess_dir / "_index.json"
    models.SESSIONS.clear()

    saved = {
        "get_state_db_session_messages": getattr(routes, "get_state_db_session_messages", None),
        "_session_visible_to_active_profile": getattr(routes, "_session_visible_to_active_profile", None),
        "_clear_stale_stream_state": getattr(routes, "_clear_stale_stream_state", None),
        "_resolve_effective_session_model_for_display": getattr(routes, "_resolve_effective_session_model_for_display", None),
        "_resolve_effective_session_model_provider_for_display": getattr(routes, "_resolve_effective_session_model_provider_for_display", None),
    }

    sid = "route_d1_d2_sid"
    authored_user = {
        "role": "user",
        "content": "[PRIOR CONTEXT] user-authored quote or notes",
        "timestamp": 100.0,
    }
    tool_row = {
        "role": "tool",
        "content": f"[prior context tool log]\n\n{DELIMITER}\n\n{SUMMARY}",
        "tool_call_id": "call_1",
        "timestamp": 101.0,
    }
    state_rows = [dict(authored_user), dict(tool_row)]

    try:
        s = models.Session(session_id=sid, messages=state_rows)
        s.save()

        routes.get_state_db_session_messages = (
            lambda s_id, profile=None, since_timestamp=None, include_inactive=False, limit=None: [dict(r) for r in state_rows]
        )
        routes._session_visible_to_active_profile = lambda profile, handler: True
        routes._clear_stale_stream_state = lambda s: None
        routes._resolve_effective_session_model_for_display = lambda s: getattr(s, "model", None)
        routes._resolve_effective_session_model_provider_for_display = lambda s: getattr(s, "model_provider", None)

        h = _Handler()
        routes.handle_get(h, urlparse(f"/api/session?session_id={sid}&messages=1&resolve_model=0"))
        assert h.status == 200
        msgs = h.payload().get("session", {}).get("messages", [])
        assert len(msgs) == 2
        assert msgs[0]["content"] == "[PRIOR CONTEXT] user-authored quote or notes"
        assert msgs[1]["content"] == f"[prior context tool log]\n\n{DELIMITER}\n\n{SUMMARY}"
    finally:
        for name, val in saved.items():
            if val is not None:
                setattr(routes, name, val)
        models.SESSION_DIR, models.SESSION_INDEX_FILE = orig_dir, orig_index
        models.SESSIONS.clear()


def test_route_positive_control_real_merged_carrier_loses_handoff(tmp_path):
    # Case 13: Route positive control: real merged carrier stored with delimiter loses handoff
    sess_dir = tmp_path / "sessions"
    sess_dir.mkdir()
    orig_dir, orig_index = models.SESSION_DIR, models.SESSION_INDEX_FILE
    models.SESSION_DIR = sess_dir
    models.SESSION_INDEX_FILE = sess_dir / "_index.json"
    models.SESSIONS.clear()

    saved = {
        "get_state_db_session_messages": getattr(routes, "get_state_db_session_messages", None),
        "_session_visible_to_active_profile": getattr(routes, "_session_visible_to_active_profile", None),
        "_clear_stale_stream_state": getattr(routes, "_clear_stale_stream_state", None),
        "_resolve_effective_session_model_for_display": getattr(routes, "_resolve_effective_session_model_for_display", None),
        "_resolve_effective_session_model_provider_for_display": getattr(routes, "_resolve_effective_session_model_provider_for_display", None),
    }

    sid = "route_control_sid"
    carrier_tool_calls = [{"id": "call_1", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}]
    carrier_row = {
        "role": "assistant",
        "content": CARRIER_CONTENT,
        "tool_calls": carrier_tool_calls,
        "timestamp": 102.0,
        "_compressed_summary": True,
    }

    try:
        s = models.Session(session_id=sid, messages=[carrier_row])
        s.save()

        routes.get_state_db_session_messages = (
            lambda s_id, profile=None, since_timestamp=None, include_inactive=False, limit=None: [dict(carrier_row)]
        )
        routes._session_visible_to_active_profile = lambda profile, handler: True
        routes._clear_stale_stream_state = lambda s: None
        routes._resolve_effective_session_model_for_display = lambda s: getattr(s, "model", None)
        routes._resolve_effective_session_model_provider_for_display = lambda s: getattr(s, "model_provider", None)

        h = _Handler()
        routes.handle_get(h, urlparse(f"/api/session?session_id={sid}&messages=1&resolve_model=0"))
        assert h.status == 200
        msgs = h.payload().get("session", {}).get("messages", [])
        assert len(msgs) == 1
        carrier = msgs[0]
        assert carrier["content"] == RETAINED_STEP
        assert carrier["tool_calls"] == carrier_tool_calls
        assert carrier["role"] == "assistant"
        assert "PRIOR CONTEXT" not in carrier["content"]
        assert "END OF PRIOR CONTEXT" not in carrier["content"]
        assert "_compressed_summary" not in carrier
    finally:
        for name, val in saved.items():
            if val is not None:
                setattr(routes, name, val)
        models.SESSION_DIR, models.SESSION_INDEX_FILE = orig_dir, orig_index
        models.SESSIONS.clear()
