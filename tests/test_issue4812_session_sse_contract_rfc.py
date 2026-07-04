"""Docs/source contract tests for issue #4812 session SSE contract RFC.

These tests assert the invariants of docs/rfcs/session-sse-contract-v1.md
without requiring a running server or endpoint implementation.
"""

import pathlib

REPO = pathlib.Path(__file__).parent.parent
RFC = REPO / "docs" / "rfcs" / "session-sse-contract-v1.md"
RFC_README = REPO / "docs" / "rfcs" / "README.md"
CONTRACTS = REPO / "docs" / "CONTRACTS.md"


def _rfc() -> str:
    return RFC.read_text(encoding="utf-8")


def _readme() -> str:
    return RFC_README.read_text(encoding="utf-8")


def _contracts() -> str:
    return CONTRACTS.read_text(encoding="utf-8")


def _rfc_section(text: str, heading: str) -> str:
    marker = f"\n## {heading}\n"
    assert marker in text, f"RFC must include a '## {heading}' section"
    return text.split(marker, 1)[1].split("\n## ", 1)[0]


class TestRFCExists:
    def test_rfc_file_exists(self):
        assert RFC.exists(), f"RFC file not found: {RFC}"

    def test_rfc_has_status_proposed(self):
        assert "Status:** Proposed" in _rfc(), "RFC must have 'Status: Proposed' header"

    def test_rfc_has_author(self):
        assert "Author:** @" in _rfc(), "RFC must have 'Author: @...' header"

    def test_rfc_has_created_date(self):
        assert "Created:** " in _rfc(), "RFC must have 'Created: YYYY-MM-DD' header"

    def test_rfc_has_tracking_issue(self):
        assert "Tracking:** #4812" in _rfc(), "RFC must have 'Tracking: #4812' header"

    def test_rfc_refs_not_closes(self):
        text = _rfc()
        assert "Refs #4812" in text, "RFC must use 'Refs #4812', not 'Closes #4812'"
        assert "Closes #4812" not in text, "RFC must not use 'Closes #4812'"


class TestRFCIndexed:
    def test_readme_indexes_rfc(self):
        assert "session-sse-contract-v1.md" in _readme(), (
            "docs/rfcs/README.md must list session-sse-contract-v1.md"
        )

    def test_readme_indexes_rfc_with_issue(self):
        assert "#4812" in _readme(), (
            "docs/rfcs/README.md index entry must reference #4812"
        )

    def test_contracts_indexes_rfc(self):
        assert "session-sse-contract-v1.md" in _contracts(), (
            "docs/CONTRACTS.md must reference session-sse-contract-v1.md"
        )


class TestEndpointDistinction:
    def test_rfc_names_proposed_per_session_endpoint(self):
        assert "/api/sessions/{session_id}/events" in _rfc(), (
            "RFC must propose GET /api/sessions/{session_id}/events"
        )

    def test_rfc_distinguishes_global_sessions_events(self):
        text = _rfc()
        assert "/api/sessions/events" in text, (
            "RFC must mention existing GET /api/sessions/events"
        )
        assert "/api/sessions/{session_id}/events" in text, (
            "RFC must distinguish per-session endpoint from global endpoint"
        )

    def test_rfc_states_global_endpoint_is_different(self):
        text = _rfc()
        assert "path-distinct" in text or "different endpoint" in text, (
            "RFC must explicitly state the two endpoints are distinct"
        )

    def test_rfc_cites_current_global_endpoint_source(self):
        """The RFC's source anchors for the existing global stream must be
        ACCURATE against current api/routes.py — verify the cited lines actually
        contain what the RFC claims, rather than string-matching a fixed number
        (which silently rots and codifies wrong anchors, #5513 gate finding)."""
        import re
        text = _rfc()
        routes = (REPO / "api" / "routes.py").read_text(encoding="utf-8").splitlines()

        # Pull every `api/routes.py:<start>-<end>` or `api/routes.py:<line>`
        # anchor the RFC cites and confirm the referenced span exists.
        anchors = re.findall(r"api/routes\.py:(\d+)(?:-(\d+))?", text)
        assert anchors, "RFC must cite at least one api/routes.py source anchor"
        for start, end in anchors:
            lo = int(start)
            hi = int(end) if end else lo
            assert 1 <= lo <= len(routes), f"RFC cites api/routes.py:{start} beyond EOF ({len(routes)} lines)"
            assert 1 <= hi <= len(routes), f"RFC cites api/routes.py:{end} beyond EOF ({len(routes)} lines)"

        # The two load-bearing anchors must land on the real definitions.
        def _anchor_line(label):
            m = re.search(r"%s.*?api/routes\.py:(\d+)" % re.escape(label), text, re.DOTALL)
            assert m, f"RFC must cite a routes.py anchor near {label!r}"
            return int(m.group(1))

        route_line = _anchor_line("routed at")
        assert "/api/sessions/events" in routes[route_line - 1], (
            f"RFC's routed-at anchor api/routes.py:{route_line} must be the "
            f"/api/sessions/events route; got: {routes[route_line - 1].strip()!r}"
        )
        handler_line = _anchor_line("_handle_session_events_stream()")
        assert "def _handle_session_events_stream" in routes[handler_line - 1], (
            f"RFC's handler anchor api/routes.py:{handler_line} must be the "
            f"_handle_session_events_stream definition; got: {routes[handler_line - 1].strip()!r}"
        )

    def test_rfc_run_journal_anchors_land_on_real_source(self):
        """Every named-symbol / emission anchor the RFC cites in the run-journal
        inventory must land on the actual source token (not just be in-bounds),
        so a stale line number can't silently pass (#5513 gate finding 2)."""
        import re
        text = _rfc()
        routes = (REPO / "api" / "routes.py").read_text(encoding="utf-8").splitlines()

        def _first_anchor_after(label):
            m = re.search(r"%s.*?api/routes\.py:(\d+)" % re.escape(label), text, re.DOTALL)
            assert m, f"RFC must cite a routes.py anchor near {label!r}"
            return int(m.group(1))

        # (label in RFC prose, token that must appear on the cited line)
        checks = [
            ("_parse_run_journal_event_id()", "def _parse_run_journal_event_id"),
            ("_parse_run_journal_after_seq()", "def _parse_run_journal_after_seq"),
            ("_runner_event_id()", "def _runner_event_id"),
            ("_replay_run_journal()", "def _replay_run_journal"),
        ]
        for label, token in checks:
            line = _first_anchor_after(label)
            assert 1 <= line <= len(routes), f"{label} anchor api/routes.py:{line} beyond EOF"
            assert token in routes[line - 1], (
                f"RFC's {label} anchor api/routes.py:{line} must contain {token!r}; "
                f"got: {routes[line - 1].strip()!r}"
            )

        # The live-emission bullet must cite the real _sse_with_id call site.
        emit_line = _first_anchor_after("live `/api/chat/stream` path at")
        assert "_sse_with_id" in routes[emit_line - 1], (
            f"RFC's live-emission anchor api/routes.py:{emit_line} must be an "
            f"_sse_with_id() call; got: {routes[emit_line - 1].strip()!r}"
        )

    def test_contracts_distinguishes_both_endpoints(self):
        text = _contracts()
        assert "/api/sessions/{session_id}/events" in text, (
            "CONTRACTS.md must mention proposed per-session endpoint"
        )
        assert "/api/sessions/events" in text, (
            "CONTRACTS.md must mention existing global sessions/events endpoint"
        )


class TestSequenceAndReplaySemantics:
    def test_rfc_includes_last_event_id(self):
        assert "Last-Event-ID" in _rfc(), "RFC must include Last-Event-ID"

    def test_rfc_includes_event_id(self):
        assert "event_id" in _rfc(), "RFC must include event_id"

    def test_rfc_includes_stream_id(self):
        assert "stream_id" in _rfc(), "RFC must include stream_id"

    def test_rfc_includes_seq(self):
        assert '"seq"' in _rfc() or "`seq`" in _rfc(), "RFC must include seq field"

    def test_rfc_includes_session_snapshot(self):
        assert "session_snapshot" in _rfc(), "RFC must include session_snapshot event"

    def test_rfc_states_seq_is_stream_scoped(self):
        text = _rfc()
        assert "monotonic within a stream" in text or "stream/run-scoped" in text, (
            "RFC must state seq is monotonic within a stream/run, not session-global"
        )

    def test_rfc_does_not_promise_session_global_counter(self):
        text = _rfc()
        assert "does not claim a pre-existing session-global sequence" in text or (
            "not a session-global counter" in text
        ), (
            "RFC must explicitly state Phase 1 does not claim a session-global counter"
        )

    def test_rfc_states_event_id_is_opaque(self):
        assert "opaque" in _rfc(), "RFC must state event_id is opaque to clients"

    def test_rfc_gates_server_generated_event_identity(self):
        text = _rfc()
        assert "Server-generated event identity" in text, (
            "RFC must gate heartbeat and snapshot event identity before implementation"
        )
        assert "heartbeat" in text and "session_snapshot" in text, (
            "RFC must name heartbeat and session_snapshot in the event identity gate"
        )
        assert "event_id" in text and "stream_id" in text and "`seq`" in text, (
            "RFC must gate event_id, stream_id, and seq values for server events"
        )

    def test_rfc_names_run_journal_as_replay_source(self):
        text = _rfc()
        assert "run journal" in text.lower(), (
            "RFC must name the run journal as the replay source"
        )

    def test_rfc_defines_session_snapshot_as_fallback(self):
        text = _rfc()
        assert "session_snapshot" in text
        assert "fallback" in text.lower() or "snapshot fallback" in text.lower(), (
            "RFC must define session_snapshot as the stale-cursor fallback"
        )

    def test_rfc_snapshot_is_not_exact_replay(self):
        text = _rfc()
        assert "not proof of exact missed-event replay" in text or (
            "recovery boundary" in text
        ), (
            "RFC must state snapshot is a recovery boundary, not exact missed-event replay"
        )


class TestHeartbeat:
    def test_rfc_references_heartbeat_constant(self):
        assert "_SSE_HEARTBEAT_INTERVAL_SECONDS" in _rfc(), (
            "RFC must reference _SSE_HEARTBEAT_INTERVAL_SECONDS rather than inventing a new constant"
        )

    def test_rfc_does_not_add_new_heartbeat_knob(self):
        text = _rfc()
        heartbeat_section = _rfc_section(text, "Heartbeat")
        normalized = heartbeat_section.replace("*", "").lower()
        assert "new per-session configurable heartbeat knob is not added" in normalized, (
            "RFC must not add a new per-session heartbeat knob in Phase 1"
        )


class TestDocsOnlyScope:
    def test_rfc_states_no_endpoint_implementation(self):
        text = _rfc()
        assert "does **not** implement" in text or "does not implement" in text, (
            "RFC must state it does not implement the endpoint"
        )

    def test_rfc_has_non_goals_section(self):
        assert "Non-goals" in _rfc(), "RFC must have a Non-goals section"

    def test_rfc_lists_implementation_gates(self):
        text = _rfc()
        assert "implementation gate" in text.lower() or "open question" in text.lower(), (
            "RFC must list open implementation gates"
        )

    def test_contracts_preserves_no_implementation_warning(self):
        text = _contracts()
        assert "not authorize implementation" in text or (
            "Proposed RFCs are review guardrails, not implementation authorization" in text
        ), (
            "CONTRACTS.md must preserve warning that proposed RFCs do not authorize implementation"
        )
