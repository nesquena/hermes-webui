"""Round-10 review finding on #7167 (nesquena-hermes 2026-10-28 20:58).

SILENT — a completed command's result is assigned to a different card.

Same id-less completion class as round 9, one step further: the FIRST card
is already persisted as ``done`` before the replay reaches its completion.
The old completion pool filtered candidates by ``not done``, so completion 1
skipped its own (done) card and landed on the next unfinished card, and
completion 2 was then discarded.

Journal ``start 1, start 2, complete 1, complete 2`` (id-less completions),
card 1 persisted done, card 2 unfinished:

| card   | master              | pre-fix head                     |
|--------|---------------------|----------------------------------|
| card 1 | ``first result`` ✓  | ``first result`` ✓               |
| card 2 | still unfinished    | **``first result``** (wrong)     |

Fix: candidates are filtered by what THIS replay already consumed, never by
the persisted ``done`` flag. A matched done card is consumed without
rewriting its already-final result; exact tool-ID matching keeps precedence.
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


def test_done_first_unfinished_second_idless_completions_keep_their_results(
    hermes_home,
):
    """A completion whose card is already done must not be retargeted onto the
    next unfinished card, and the later completion must still be applied."""
    sid = "r10_done_first_unfinished_second"
    stream_id = "stream-r10-done-first"

    # Journal: start 1, start 2, complete 1, complete 2 — same tool name,
    # id-less completions. Journal start order is the only ordering signal.
    append_run_event(sid, stream_id, "tool", {"name": "terminal", "preview": "first command"})
    append_run_event(sid, stream_id, "tool", {"name": "terminal", "preview": "second command"})
    append_run_event(sid, stream_id, "tool_complete", {"name": "terminal", "preview": "first result"})
    append_run_event(sid, stream_id, "tool_complete", {"name": "terminal", "preview": "second result"})

    session = Session(
        session_id=sid,
        title="r10 done-first id-less completion",
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
    # Card 1 was completed by an earlier pass; card 2 is still unfinished.
    session.tool_calls = [
        {
            "name": "terminal",
            "preview": "first result",
            "snippet": "first result",
            "tid": "",
            "assistant_msg_idx": 1,
            "done": True,
            "_recovered_from_run_journal": True,
            "_recovered_stream_id": stream_id,
            "_recovered_event_id": f"{stream_id}:1",
        },
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
        },
    ]

    from api.models import _append_journaled_partial_output

    _appended, accounted = _append_journaled_partial_output(
        session, stream_id, dedupe_existing=True
    )
    assert accounted is True, "the journal's tool output must be accounted for"

    cards = session.tool_calls
    assert len(cards) == 2, (
        "both journal starts reuse their persisted card — no third card: "
        f"{[c.get('preview') for c in cards]!r}"
    )
    first_card = next(
        c for c in cards if c.get("_recovered_event_id") == f"{stream_id}:1"
    )
    second_card = next(
        c for c in cards if c.get("_recovered_event_id") == f"{stream_id}:2"
    )
    assert first_card.get("done") is True
    assert str(first_card.get("preview")) == "first result", (
        "the already-done card keeps its final result — the replayed "
        f"completion must not rewrite it; got {first_card.get('preview')!r}"
    )
    assert second_card.get("done") is True, (
        "the second completion must still complete the unfinished card; "
        f"got {second_card!r}"
    )
    assert str(second_card.get("preview")) == "second result", (
        "completion 1 must be consumed by the done card, not retargeted onto "
        f"card 2; got {second_card.get('preview')!r}"
    )

    # Idempotence: a second cold-replay pass over the same journal must be a
    # no-op (both cards are done and both completions are consumed again).
    _append_journaled_partial_output(session, stream_id, dedupe_existing=True)
    assert len(session.tool_calls) == 2, (
        "the second replay pass must not append a third card: "
        f"{[c.get('preview') for c in session.tool_calls]!r}"
    )
    assert str(first_card.get("preview")) == "first result"
    assert str(second_card.get("preview")) == "second result"

    # And the corrected pairing survives the persisted sidecar round-trip.
    session.save()
    loaded = Session.load(sid)
    assert loaded is not None
    loaded_cards = {
        c.get("_recovered_event_id"): c for c in (loaded.tool_calls or [])
    }
    assert set(loaded_cards) == {f"{stream_id}:1", f"{stream_id}:2"}, (
        f"both cards persist with their journal event identity: "
        f"{sorted(map(str, loaded_cards))!r}"
    )
    assert str(loaded_cards[f"{stream_id}:1"].get("preview")) == "first result"
    assert str(loaded_cards[f"{stream_id}:2"].get("preview")) == "second result"
    assert all(c.get("done") for c in loaded_cards.values())
