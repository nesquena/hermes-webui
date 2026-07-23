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
        ACCURATE against current api/routes.py, verified by SYMBOL not by line
        number. The RFC cites the route string and handler function by name;
        this test confirms (a) each symbol still exists in api/routes.py and
        (b) the RFC names that symbol. It deliberately does NOT check line
        numbers: a routes.py line-shift must never break this test or the RFC
        (#5513 gate finding, chronic brittle failure #5542)."""
        text = _rfc()
        routes_src = (REPO / "api" / "routes.py").read_text(encoding="utf-8")

        # (RFC-cited symbol, existence probe in api/routes.py source)
        checks = [
            ("/api/sessions/events", "/api/sessions/events"),
            ("_handle_session_events_stream", "def _handle_session_events_stream"),
        ]
        for rfc_symbol, source_probe in checks:
            assert source_probe in routes_src, (
                f"api/routes.py must still define/route {source_probe!r}; the RFC "
                f"cites {rfc_symbol!r} as a stable source anchor"
            )
            assert rfc_symbol in text, (
                f"RFC must name the symbol {rfc_symbol!r} (symbol-based anchor, "
                f"not a line number)"
            )

    def test_rfc_run_journal_anchors_land_on_real_source(self):
        """Every named-symbol anchor the RFC cites in the run-journal inventory
        must be a REAL symbol in api/routes.py and be NAMED in the RFC prose.
        This is verified by symbol, never by line number, so a routes.py
        line-shift can't silently break it or the RFC (#5513 gate finding 2,
        chronic brittle failure #5542)."""
        text = _rfc()
        routes_src = (REPO / "api" / "routes.py").read_text(encoding="utf-8")

        # (RFC-cited symbol name, existence probe in api/routes.py source)
        checks = [
            ("_parse_run_journal_event_id", "def _parse_run_journal_event_id"),
            ("_parse_run_journal_after_seq", "def _parse_run_journal_after_seq"),
            ("_runner_event_id", "def _runner_event_id"),
            ("_replay_run_journal", "def _replay_run_journal"),
            ("_sse_with_id", "_sse_with_id"),
        ]
        for rfc_symbol, source_probe in checks:
            assert source_probe in routes_src, (
                f"api/routes.py must still define {source_probe!r}; the RFC cites "
                f"{rfc_symbol!r} as a stable source anchor"
            )
            assert rfc_symbol in text, (
                f"RFC must name the symbol {rfc_symbol!r} (symbol-based anchor, "
                f"not a line number)"
            )

    def test_rfc_uses_no_hardcoded_routes_line_numbers(self):
        """Guard against regression to line-number coupling: the RFC must not
        cite `api/routes.py:<line>` (or streaming.py:<line>) anchors. Symbol
        names are the durable anchor; absolute line numbers rot on any
        source-layout shift and caused chronic brittle failures (#5513, #5542)."""
        import re
        text = _rfc()
        stale = re.findall(r"\b\w+\.py:\d+(?:-\d+)?", text)
        assert not stale, (
            "RFC must not cite hardcoded source line numbers (they rot on any "
            f"line-shift); found: {stale!r}. Reference symbols by name instead."
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
