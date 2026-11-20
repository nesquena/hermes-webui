"""Regression coverage for the 2026-11-19 #7167 review findings.

1. ``api/models.py`` compared a recovered card's shortened ``args`` against
   the completion's longer ``args``. ``_truncate_journal_tool_args`` cuts every
   value at 120 characters while the legacy producer keeps content keys such as
   ``cmd`` at 4000. A command longer than 120 characters therefore could not
   match even its only card, and recovery dropped the result.
2. Two id-less starts with identical arguments both entered ``args_scoped``;
   the unique-match requirement rejected both completions and their results
   were lost. The legacy producer settles newest-first, so the newest
   unconsumed card must absorb each completion.
"""

from __future__ import annotations

import pytest

import api.models as models
import api.profiles as profiles
from api.models import Session
from api.run_journal import append_run_event


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    """Isolate HERMES_HOME + the WebUI state dir so journal writes sandbox."""
    home = tmp_path / "hermes_home"
    home.mkdir()
    (home / "sessions").mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_WEBUI_STATE_DIR", str(home))
    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", home)
    monkeypatch.setattr(models, "SESSION_DIR", home / "sessions")
    monkeypatch.setattr(
        models, "SESSION_INDEX_FILE", home / "sessions" / "_index.json"
    )
    return home


def _base_session(sid: str, stream_id: str) -> Session:
    return Session(
        session_id=sid,
        title=sid,
        messages=[
            {"role": "user", "content": "go", "timestamp": 900.5},
            {
                "role": "assistant",
                "content": "",
                "timestamp": 900.6,
                "_recovered_from_run_journal": True,
                "_recovered_stream_id": stream_id,
            },
        ],
    )


def test_long_content_key_command_still_matches_its_only_card(hermes_home):
    """A >120-char ``cmd`` must match via the producer-shaped snapshot.

    The display copy stays shortened, but ownership matching uses the full
    snapshot the completion carries.
    """
    sid = "round11_long_command"
    stream_id = "stream-round11-long-command"
    long_cmd = "echo " + ("x" * 400)

    append_run_event(
        sid, stream_id, "tool",
        {"name": "terminal", "args": {"cmd": long_cmd}, "preview": "running long"},
    )
    append_run_event(
        sid, stream_id, "tool_complete",
        {"name": "terminal", "args": {"cmd": long_cmd}, "preview": "long result"},
    )

    session = _base_session(sid, stream_id)
    from api.models import _append_journaled_partial_output

    appended, accounted = _append_journaled_partial_output(
        session, stream_id, dedupe_existing=True
    )
    assert accounted is True, "journal output must be accounted for"
    assert appended is True

    cards = session.tool_calls
    assert len(cards) == 1, [c.get("args") for c in cards]
    card = cards[0]
    assert card["done"] is True, "the only card must receive its completion"
    assert card["snippet"] == "long result"
    # The display copy stays shortened; the matching snapshot keeps the value.
    display_cmd = str((card.get("args") or {}).get("cmd") or "")
    assert display_cmd.endswith("...")
    assert len(display_cmd) <= 123
    assert (card.get("_journal_match_args") or {}).get("cmd") == long_cmd


def test_repeated_identical_commands_each_receive_a_result(hermes_home):
    """Two ``pwd`` starts + two completions must both land, newest-first."""
    sid = "round11_repeated_command"
    stream_id = "stream-round11-repeated-command"

    append_run_event(
        sid, stream_id, "tool",
        {"name": "terminal", "args": {"cmd": "pwd"}, "preview": "first pwd"},
    )
    append_run_event(
        sid, stream_id, "tool",
        {"name": "terminal", "args": {"cmd": "pwd"}, "preview": "second pwd"},
    )
    # Legacy producer settles newest-first.
    append_run_event(
        sid, stream_id, "tool_complete",
        {"name": "terminal", "args": {"cmd": "pwd"}, "preview": "result second"},
    )
    append_run_event(
        sid, stream_id, "tool_complete",
        {"name": "terminal", "args": {"cmd": "pwd"}, "preview": "result first"},
    )

    session = _base_session(sid, stream_id)
    from api.models import _append_journaled_partial_output

    appended, accounted = _append_journaled_partial_output(
        session, stream_id, dedupe_existing=True
    )
    assert accounted is True
    assert appended is True

    cards = session.tool_calls
    assert len(cards) == 2, [c.get("snippet") for c in cards]
    assert all(card["done"] is True for card in cards), cards
    assert [card["snippet"] for card in cards] == ["result first", "result second"]


def test_different_arguments_still_reject_a_mismatched_completion(hermes_home):
    """A completion whose args match no card must not retarget onto one."""
    sid = "round11_mismatched_args"
    stream_id = "stream-round11-mismatched-args"

    append_run_event(
        sid, stream_id, "tool",
        {"name": "terminal", "args": {"cmd": "echo A"}, "preview": "echo A"},
    )
    append_run_event(
        sid, stream_id, "tool_complete",
        {"name": "terminal", "args": {"cmd": "echo B"}, "preview": "result B"},
    )

    session = _base_session(sid, stream_id)
    from api.models import _append_journaled_partial_output

    _append_journaled_partial_output(session, stream_id, dedupe_existing=True)

    cards = session.tool_calls
    assert len(cards) == 1
    assert cards[0]["done"] is False, "a non-matching completion must not be applied"
    assert cards[0].get("snippet") == "echo A"
