"""#7681 review round 3 — the four findings the 02:27Z re-gate still reproduced.

Round 2 fixed the visibility escape, the rotation chains and the sidecar
counter. The 2026-10-08 re-gate found four more, three of them must-fix:

1. **[MUST-FIX] Compressed ACP chains disappear from the sidebar.** Nulling the
   collapsed row's displayed count also nulled the count the ACP visibility
   check reads, and that check runs *before* the lineage check. A real ACP
   chain with 8 user turns returned 0 sidebar rows.

2. **[MUST-FIX] The state.db counter still counts rows the Agent doesn't.**
   No ``display_kind`` filter; SQLite's ``TRIM()`` strips spaces only, so
   whitespace rows holding tabs/newlines counted; merged summary carriers the
   Agent *does* count were missed. The parity test passed only because its
   hand-made ``messages`` table has no ``display_kind`` column.

3. **[MUST-FIX] The default compaction mode still shows a wrong count.** The
   installed Agent compacts in place, so the session stays one segment and the
   label renders a total that includes rows already compacted away (and rows
   removed by ``/undo``).

4. **[SHOULD-FIX] The optimistic +1 compounds.** ``send()``'s three update
   passes took the count 41 → 42 → 43.
"""

from __future__ import annotations

import sqlite3
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    subprocess.run(["which", "node"], capture_output=True).returncode != 0,
    reason="node not on PATH",
)

REPO_ROOT = Path(__file__).resolve().parents[1]
AGENT_SESSIONS = (REPO_ROOT / "api" / "agent_sessions.py").read_text(encoding="utf-8")
COMPRESSION_ANCHOR = (REPO_ROOT / "api" / "compression_anchor.py").read_text(
    encoding="utf-8"
)
SESSIONS_JS = (REPO_ROOT / "static" / "sessions.js").read_text(encoding="utf-8")


# ── finding 1: an ACP chain with real turns stays visible ───────────────────


def test_the_collapsed_row_keeps_an_internal_count_for_visibility() -> None:
    """The displayed count and the visibility count must be different fields.

    ``_acp_row_is_visible`` runs ``_count_user_turns(row) > 0`` BEFORE the
    lineage check, and ``_count_user_turns`` falls back through
    ``actual_user_message_count`` → ``user_message_count`` → ``messages`` → 0.
    Nulling only the displayed count therefore hid real ACP chains.
    """
    assert "merged['lineage_user_message_count']" in AGENT_SESSIONS, (
        "the collapsed lineage row must preserve its count in a separate "
        "internal field before nulling the displayed one (#7681 finding 1)"
    )
    # And the visibility helper must read that field first.
    start = AGENT_SESSIONS.find("def _count_user_turns(")
    assert start >= 0
    body = AGENT_SESSIONS[start : AGENT_SESSIONS.find("\ndef ", start + 10)]
    assert 'row.get("lineage_user_message_count")' in body, (
        "_count_user_turns must read the internal lineage count first, or an "
        "ACP chain with real turns is hidden (#7681 finding 1)"
    )


def test_a_collapsed_acp_chain_with_user_turns_is_visible() -> None:
    """End-to-end on the real visibility gate: 8 real turns, 0 sidebar rows
    was the reported regression."""
    from api.agent_sessions import is_cli_session_row_visible

    row = {
        "is_cli_session": True,
        "source": "acp",
        "source_label": "acp",
        "source_tag": "acp",
        "raw_source": "acp",
        "title": "ACP session",
        # What the collapsed-lineage projection emits: the DISPLAYED count is
        # null, the internal one carries the truth.
        "actual_user_message_count": None,
        "lineage_user_message_count": 8,
        "user_message_count": None,
        "actual_message_count": 20,
        "message_count": 20,
        "messages": [],
    }
    assert is_cli_session_row_visible(row) is True, (
        "an ACP chain with 8 real user turns vanished from the sidebar "
        "(#7681 finding 1)"
    )


def test_a_collapsed_acp_chain_without_turns_stays_hidden() -> None:
    """The mirror guard: a genuinely empty ACP row must not become visible."""
    from api.agent_sessions import is_cli_session_row_visible

    row = {
        "is_cli_session": True,
        "source": "acp",
        "source_label": "acp",
        "source_tag": "acp",
        "raw_source": "acp",
        "title": "ACP session",
        "actual_user_message_count": None,
        "lineage_user_message_count": 0,
        "user_message_count": None,
        "actual_message_count": 4,
        "message_count": 4,
        "messages": [],
    }
    assert is_cli_session_row_visible(row) is False


# ── finding 2: the state.db SQL must agree with the predicate ───────────────


def test_the_sql_filters_display_kind() -> None:
    """``display_kind`` scaffolding must be excluded in SQL as well as Python."""
    assert "display_kind_guard" in AGENT_SESSIONS, (
        "the state.db aggregate has no display_kind guard; async-delegation "
        "and hidden rows are counted as user turns (#7681 finding 2)"
    )
    assert "COALESCE(m.display_kind, '') IN ('', 'steer')" in AGENT_SESSIONS, (
        "the display_kind guard must allow only '' and 'steer', matching "
        "api/compression_anchor.is_user_originated_turn"
    )
    # It must be conditional on the column existing (legacy schemas lack it).
    assert "if 'display_kind' in message_cols" in AGENT_SESSIONS


def test_the_sql_trims_the_full_whitespace_set() -> None:
    """SQLite's ``TRIM()`` strips SPACES only."""
    assert "char(9)" in AGENT_SESSIONS, (
        "the content guard does not trim tabs; a whitespace-only row holding a "
        "tab counts as a user turn (#7681 finding 2)"
    )
    assert "char(10)" in AGENT_SESSIONS, "the content guard does not trim newlines"
    assert "char(13)" in AGENT_SESSIONS, "the content guard does not trim CR"


def _sql_user_count(db: Path) -> int | None:
    from api.agent_sessions import read_importable_agent_session_rows

    rows = read_importable_agent_session_rows(db)
    return rows[0]["actual_user_message_count"] if rows else None


def test_display_kind_and_whitespace_rows_are_not_counted(tmp_path) -> None:
    """A real SessionDB-shaped table, driven through the real reader.

    The old parity test built a ``messages`` table WITHOUT ``display_kind``, so
    the new guard was silently skipped and the test stayed green while the
    counter still over-reported.
    """
    from api.agent_sessions import read_importable_agent_session_rows

    db = tmp_path / "state.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY, source TEXT, session_source TEXT,
            title TEXT, model TEXT, started_at REAL NOT NULL,
            message_count INTEGER DEFAULT 0, parent_session_id TEXT,
            ended_at REAL, end_reason TEXT
        );
        CREATE TABLE messages (
            id TEXT PRIMARY KEY, session_id TEXT, role TEXT, content TEXT,
            timestamp REAL, _compressed_summary INTEGER NOT NULL DEFAULT 0,
            display_kind TEXT
        );
        CREATE INDEX idx_messages_session ON messages(session_id, timestamp);
        """
    )
    conn.execute(
        "INSERT INTO sessions (id, source, session_source, title, model,"
        " started_at, message_count, parent_session_id, ended_at, end_reason)"
        " VALUES ('dk', 'tui', 'tui', 'DK', 'm', 10.0, 8, NULL, NULL, NULL)"
    )
    rows = [
        ("m1", "user", "real human ask one", 0, None, True),
        # async-delegation scaffolding: role='user' but not human input.
        ("m2", "user", "delegate this to a subagent", 0, "async_delegation", False),
        # hidden operational notice.
        ("m3", "user", "background process finished", 0, "hidden", False),
        # steer IS human input.
        ("m4", "user", "actually, stop and do X", 0, "steer", True),
        # whitespace-only rows: TRIM() alone would count the tab/newline ones.
        ("m5", "user", "   ", 0, None, False),
        ("m6", "user", "\t", 0, None, False),
        ("m7", "user", "\n", 0, None, False),
        ("m8", "user", "real human ask two", 0, None, True),
    ]
    for mid, role, content, flag, dk, _counts in rows:
        conn.execute(
            "INSERT INTO messages (id, session_id, role, content, timestamp,"
            " _compressed_summary, display_kind) VALUES (?,?,?,?,?,?,?)",
            (mid, "dk", role, content, 100.0, flag, dk),
        )
    conn.commit()
    conn.close()

    by_id = {r["id"]: r for r in read_importable_agent_session_rows(db)}
    assert "dk" in by_id
    assert by_id["dk"]["actual_user_message_count"] == 3, (
        "only the 3 genuine human turns (2 plain + 1 steer) may be counted; "
        f"got {by_id['dk']['actual_user_message_count']!r} — display_kind "
        "scaffolding and whitespace-only rows are still being counted "
        "(#7681 finding 2)"
    )


def test_a_merged_summary_carrier_with_a_live_ask_is_counted() -> None:
    """The Agent counts a live ask embedded in a summary carrier.

    ``split_user_originated_turn`` strips the handoff and keeps the human
    payload; the WebUI mirror used to reject every ``_compressed_summary`` row
    outright.
    """
    from api.compression_anchor import is_user_originated_turn

    carrier = {
        "role": "user",
        "display_kind": "hidden",
        "_compressed_summary": True,
        # The agent's own end-marker shape: the live ask follows the marker.
        "content": (
            "[CONTEXT SUMMARY]: prior turns\n"
            "--- END OF CONTEXT SUMMARY — respond to the message below, "
            "not the summary above ---\n"
            "and now please also check the deployment logs"
        ),
    }
    assert is_user_originated_turn(carrier) is True, (
        "a merged summary carrier that still embeds a live human ask must be "
        "counted — the Agent counts it (#7681 finding 2)"
    )

    pure_handoff = {
        "role": "user",
        "display_kind": "hidden",
        "_compressed_summary": True,
        "content": (
            "[CONTEXT SUMMARY]: prior turns\n"
            "--- END OF CONTEXT SUMMARY — respond to the message below, "
            "not the summary above ---\n"
        ),
    }
    assert is_user_originated_turn(pure_handoff) is False, (
        "a handoff with no live ask is still scaffolding"
    )


# ── finding 3: in-place compaction must not render a stale total ────────────


def test_the_sql_reports_inactive_user_rows() -> None:
    """The backend must say "this session has compacted-away user rows"."""
    assert "inactive_user_rows_expr" in AGENT_SESSIONS, (
        "the aggregate does not report inactive (compacted/rewound) user rows, "
        "so the client renders a total that includes them (#7681 finding 3)"
    )
    assert "AS has_inactive_user_rows" in AGENT_SESSIONS, (
        "the inactive-rows flag is not selected into the row payload"
    )


def test_a_session_with_compacted_rows_omits_the_label() -> None:
    """The client's render gate must refuse a session with inactive rows."""
    gate = _extract_js(SESSIONS_JS, "function _sidebarUserTurnCountRenderOK(")
    assert "has_inactive_user_rows" in gate, (
        "the render gate does not look at has_inactive_user_rows; an in-place "
        "compacted session still renders its stale total (#7681 finding 3)"
    )


def _extract_js(source: str, marker: str) -> str:
    start = source.find(marker)
    assert start >= 0, f"marker not found: {marker!r}"
    i = source.find("{", start)
    depth = 0
    while i < len(source):
        if source[i] == "{":
            depth += 1
        elif source[i] == "}":
            depth -= 1
            if depth == 0:
                return source[start : i + 1]
        i += 1
    raise AssertionError(f"unbalanced braces from {marker!r}")


# ── finding 4: the optimistic +1 is idempotent per pending turn ─────────────


def test_the_optimistic_bump_is_idempotent() -> None:
    """``send()`` runs three update passes; the +1 must land once."""
    start = SESSIONS_JS.find("const optimisticUserTurns=")
    assert start >= 0
    body = SESSIONS_JS[start : start + 2500]
    assert "_optimisticTurnBumpSid" in body, (
        "the optimistic +1 has no per-session marker; three send passes "
        "compound 41 → 42 → 43 (#7686 finding 4)".replace("7686", "7681")
    )
    assert "_optimisticTurnBumpBase" in body, (
        "the marker must also pin the base count, so a second pass against the "
        "same server total is recognised as already bumped"
    )
    assert "alreadyBumped" in body


def test_the_bump_marker_is_cleared_when_no_turn_is_in_flight() -> None:
    """The next send must be able to bump again."""
    start = SESSIONS_JS.find("const optimisticUserTurns=")
    body = SESSIONS_JS[start : start + 2500]
    assert "_optimisticTurnBumpSid=null" in body, (
        "the bump marker is never cleared, so a later send in the same session "
        "can never advance the count again"
    )
