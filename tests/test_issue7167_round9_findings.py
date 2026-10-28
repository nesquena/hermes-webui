"""Round-9 review finding on #7167 (nesquena-hermes 2026-10-27 22:07).

SILENT — journal recovery attaches tool results to the wrong command.

Repro (Codex, confirmed by the reviewer through cold ``get_session()``
recovery with a real run journal): the journal holds ``start command 1``,
``start command 2``, ``complete 1``, ``complete 2`` and the session has
only command 2's unfinished card persisted. After two cold reloads:

| card               | master            | pre-fix head         |
|--------------------|-------------------|----------------------|
| ``first command``  | ``first result``  | **``second result``** |
| ``second command`` | still unfinished  | **``first result``**  |

Cause: the ``tool_complete`` pool put every REUSED card before the newly
recovered ones whatever the journal start order, so an id-less completion
was matched to the wrong card — the later-started reused tool consumed the
earlier tool's result.

Fix: one per-pass list of reused and newly created cards in journal start
order, built as each tool start is replayed; completions match against
that list, with exact tool-ID matching still ahead of the fallback.
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
    # Session.load() resolves SESSION_DIR at import time; redirect the module
    # global so the fresh-file reload reads OUR sandbox (round-8 pattern).
    monkeypatch.setattr(models, "SESSION_DIR", home / "sessions")
    monkeypatch.setattr(
        models, "SESSION_INDEX_FILE", home / "sessions" / "_index.json"
    )
    return home


def test_mixed_reused_and_fresh_tool_cards_match_completions_in_start_order(
    hermes_home,
):
    """Four journal events + one persisted unfinished card: each tool's
    completion must land on the card the journal's start order implies."""
    sid = "r9_mixed_card_completion_order"
    stream_id = "stream-r9-mixed-cards"

    # Journal: start 1, start 2, complete 1, complete 2. Both commands use
    # the SAME tool name and the completions are id-less (no tid) — the
    # journal's start order is then the only ordering signal there is.
    append_run_event(sid, stream_id, "tool", {"name": "terminal", "preview": "first command"})
    append_run_event(sid, stream_id, "tool", {"name": "terminal", "preview": "second command"})
    append_run_event(sid, stream_id, "tool_complete", {"name": "terminal", "preview": "first result"})
    append_run_event(sid, stream_id, "tool_complete", {"name": "terminal", "preview": "second result"})

    # Persisted start state: only command 2's unfinished card (an earlier
    # interrupted pass recovered it), anchored at that pass's recovered
    # assistant row.
    session = Session(
        session_id=sid,
        title="r9 mixed reused + fresh tool cards",
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
    session.tool_calls = [
        {
            "name": "terminal",
            "preview": "second command",
            "snippet": "second command",
            "tid": "",
            "assistant_msg_idx": 1,
            "done": False,
            "_recovered_from_run_journal": True,
            "_recovered_stream_id": stream_id,
            "_recovered_event_id": f"{stream_id}:2",
        }
    ]

    from api.models import _append_journaled_partial_output

    _appended, accounted = _append_journaled_partial_output(
        session, stream_id, dedupe_existing=True
    )
    assert accounted is True, "the journal's tool output must be accounted for"

    cards = session.tool_calls
    assert len(cards) == 2, (
        "exactly one reused card + one freshly recovered card, "
        f"got {len(cards)}: {[c.get('preview') for c in cards]!r}"
    )
    first_card = next(
        c for c in cards if c.get("_recovered_event_id") == f"{stream_id}:1"
    )
    second_card = next(
        c for c in cards if c.get("_recovered_event_id") == f"{stream_id}:2"
    )
    assert first_card.get("done") is True and second_card.get("done") is True, (
        "both completions must land on a card: "
        f"first={first_card!r} second={second_card!r}"
    )
    assert str(first_card.get("preview")) == "first result", (
        "the first-started tool must carry the FIRST completion's result "
        "(the journal's start order is the only id-less ordering signal); "
        f"got {first_card.get('preview')!r}"
    )
    assert str(second_card.get("preview")) == "second result", (
        "the later-started reused card must carry the SECOND completion's "
        f"result; got {second_card.get('preview')!r}"
    )

    # The tool-first replay reuses the persisted recovered anchor instead
    # of allocating an orphan empty assistant row.
    assert len(session.messages) == 2, (
        "the replay must reuse the persisted recovered anchor, "
        f"messages grew to {len(session.messages)}"
    )

    # Second pass over the same journal (the cold-reload / lazy-retry
    # shape): both starts dedupe by event id and both cards are already
    # done, so the pass must be a no-op — no third card, no rewrite.
    _append_journaled_partial_output(session, stream_id, dedupe_existing=True)
    assert len(session.tool_calls) == 2, (
        "the second replay pass must not append a third card: "
        f"{[c.get('preview') for c in session.tool_calls]!r}"
    )
    assert str(first_card.get("preview")) == "first result"
    assert str(second_card.get("preview")) == "second result"

    # The corrected pairing survives the persisted sidecar round-trip.
    session.save()
    loaded = Session.load(sid)
    assert loaded is not None
    loaded_cards = {
        c.get("_recovered_event_id"): c for c in (loaded.tool_calls or [])
    }
    assert set(loaded_cards) == {f"{stream_id}:1", f"{stream_id}:2"}, (
        "both cards persist with their journal event identity: "
        f"{sorted(map(str, loaded_cards))!r}"
    )
    assert str(loaded_cards[f"{stream_id}:1"].get("preview")) == "first result"
    assert str(loaded_cards[f"{stream_id}:2"].get("preview")) == "second result"
    assert all(c.get("done") for c in loaded_cards.values())
