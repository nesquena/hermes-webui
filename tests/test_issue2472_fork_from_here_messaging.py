"""Regression coverage for #2472 fork-from-here on messaging sessions."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import api.routes as routes

REPO = Path(__file__).resolve().parents[1]
COMMANDS_JS = (REPO / "static" / "commands.js").read_text(encoding="utf-8")
ROUTES_PY = (REPO / "api" / "routes.py").read_text(encoding="utf-8")


def _function_body(src: str, name: str) -> str:
    start = src.index(f"async function {name}")
    brace = src.index("{", start)
    depth = 0
    for i in range(brace, len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[start : i + 1]
    raise AssertionError(f"function {name!r} body not found")


def test_messaging_merge_helper_matches_session_get_coordinate_space():
    session = SimpleNamespace(
        messages=[
            {"role": "user", "content": "sidecar only", "timestamp": 2},
            {"role": "assistant", "content": "shared", "timestamp": 3},
        ]
    )
    cli_messages = [
        {"role": "user", "content": "cli earlier", "timestamp": 1},
        {"role": "assistant", "content": "shared", "timestamp": 3},
        {"role": "assistant", "content": "cli later", "timestamp": 4},
    ]

    merged = routes._merged_session_messages_for_display(session, cli_messages)

    assert [m["content"] for m in merged] == [
        "cli earlier",
        "sidecar only",
        "shared",
        "cli later",
    ]


def test_branch_handler_uses_merged_messaging_messages_for_keep_count():
    branch_idx = ROUTES_PY.index('parsed.path == "/api/session/branch":')
    block = ROUTES_PY[branch_idx : branch_idx + 2600]

    assert "_merged_session_messages_for_display(source, cli_messages)" in block
    assert "get_cli_session_messages(source.session_id)" in block
    assert "source_messages = source.messages or []" not in block


def test_branch_handler_best_effort_saves_source_before_fork_slice():
    branch_idx = ROUTES_PY.index('parsed.path == "/api/session/branch":')
    block = ROUTES_PY[branch_idx : branch_idx + 2600]

    assert "source.save()" in block
    assert block.index("source.save()") < block.index("source_messages =")


def test_fork_from_message_snapshots_session_id_across_async_load():
    body = _function_body(COMMANDS_JS, "forkFromMessage")

    assert "const initialSid = S.session.session_id;" in body
    assert "S.session.session_id !== initialSid" in body
    assert "session_id:initialSid" in body
    assert "session_id:S.session.session_id" not in body


def test_fork_loads_full_fork_transcript_after_branch():
    body = _function_body(COMMANDS_JS, "forkFromMessage")

    load_idx = body.index("await loadSession(data.session_id)")
    after_load = body[load_idx:]
    assert "await _ensureAllMessagesLoaded()" in after_load
