"""The Aider transcript window keeps the NEWEST messages (#674).

Aider chat history is APPEND-ONLY: the file only ever grows, and the newest
turns are appended at the end. The parser used to walk forward and STOP at
the message cap, so once a history passed 1,000 messages the transcript was
frozen on the same oldest turns forever -- later appends moved the sidebar
timestamp but the transcript never changed, and nothing said so.

The window must therefore keep the newest messages, and the title must stay
anchored to the FIRST user turn of the file (otherwise the sidebar title
would drift every time the window slid).
"""

from __future__ import annotations


def _write_history(path, body):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def test_window_keeps_the_newest_messages_not_the_oldest(tmp_path):
    """Past the cap, the transcript shows the newest turns."""
    import api.models as models

    body = ""
    for i in range(1, 13):
        body += f"\n#### user turn {i}\n\nassistant reply {i}\n"

    path = _write_history(tmp_path / "repo" / ".aider.chat.history.md", body)

    messages, _started_at, _title = models._parse_aider_chat_history(
        path, max_messages=10
    )

    contents = [m.get("content") for m in messages]
    assert len(messages) == 10, (
        f"the window must be bounded to max_messages; got {len(messages)}"
    )
    assert "user turn 12" in contents, (
        "the NEWEST user turn must be in the window; got "
        f"{contents!r}"
    )
    assert "user turn 1" not in contents, (
        "the oldest user turn must have been evicted once the window was "
        f"full; got {contents!r}"
    )


def test_title_stays_anchored_to_the_first_user_turn(tmp_path):
    """The title is the FIRST user turn, even when the window slid past it."""
    import api.models as models

    body = ""
    for i in range(1, 13):
        body += f"\n#### user turn {i}\n\nassistant reply {i}\n"

    path = _write_history(tmp_path / "repo" / ".aider.chat.history.md", body)

    messages, _started_at, title_hint = models._parse_aider_chat_history(
        path, max_messages=10
    )

    assert title_hint == "user turn 1", (
        "the title hint must be the FIRST user turn of the file; got "
        f"{title_hint!r}"
    )
    assert models._aider_title(messages, title_hint) == "user turn 1"
