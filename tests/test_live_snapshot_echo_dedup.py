"""Journal-reconstruction regression: a whitespace-stretched reasoning echo
must be de-duplicated exactly once.

``_run_journal_live_snapshot`` strips reasoning echoes of interim assistant
text so the restored transcript does not show the same content in both the
thinking block and the assistant output (run-state consistency invariant:
replay must not duplicate interim assistant text). The previous bounded echo
probe could miss a compact-equivalent suffix whose raw span exceeded its fold
window, leaving the duplicate in the snapshot; the backward walk has no
window.

Both directions are pinned: a stretched echo is stripped (appears once), and
genuine interim text that is NOT a reasoning echo is left alone.
"""

import json
import re

import api.routes as routes
from api import run_journal as RJ


def _compact(value):
    return re.sub(r"\s+", "", str(value or ""))


def _write_journal(root, session_id, run_id, events):
    session_root = root / RJ.RUN_JOURNAL_DIR_NAME / session_id
    session_root.mkdir(parents=True, exist_ok=True)
    path = session_root / f"{run_id}.jsonl"
    rows = []
    for seq, (event, payload) in enumerate(events, start=1):
        rows.append(
            json.dumps(
                {
                    "version": 1,
                    "event_id": f"{run_id}:{seq}",
                    "seq": seq,
                    "run_id": run_id,
                    "session_id": session_id,
                    "event": event,
                    "type": event,
                    "created_at": 1000 + seq,
                    "payload": payload,
                }
            )
        )
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    return path


def test_whitespace_stretched_reasoning_echo_appears_once(tmp_path, monkeypatch):
    gap = " " * 5000  # raw echo span far beyond any fixed fold window
    _write_journal(
        tmp_path,
        "session_1",
        "run_1",
        [
            ("reasoning", {"text": "prefix " + "alpha" + gap + "beta"}),
            ("interim_assistant", {"text": "alpha beta"}),
        ],
    )
    monkeypatch.setattr(RJ, "_default_session_dir", lambda: tmp_path)

    snapshot = routes._run_journal_live_snapshot("run_1")

    message = snapshot["messages"][-1]
    reasoning_text = message.get("reasoning") or ""
    content_text = message.get("content") or ""
    occurrences = _compact(reasoning_text).count("alphabeta") + _compact(
        content_text
    ).count("alphabeta")
    assert occurrences == 1, (
        f"the interim echo must appear once after reconstruction; "
        f"reasoning={reasoning_text[-60:]!r} content={content_text[-60:]!r}"
    )
    # The echo span is stripped from the reasoning copy; the prefix stays.
    assert "alphabeta" not in _compact(reasoning_text)
    assert "alpha beta" in content_text


def test_genuine_interim_text_is_not_treated_as_echo(tmp_path, monkeypatch):
    gap = " " * 5000
    _write_journal(
        tmp_path,
        "session_1",
        "run_1",
        [
            ("reasoning", {"text": "prefix " + "alpha" + gap + "beta"}),
            ("interim_assistant", {"text": "gamma delta"}),
        ],
    )
    monkeypatch.setattr(RJ, "_default_session_dir", lambda: tmp_path)

    snapshot = routes._run_journal_live_snapshot("run_1")

    message = snapshot["messages"][-1]
    reasoning_text = message.get("reasoning") or ""
    content_text = message.get("content") or ""
    # The genuine interim answer is preserved in full...
    assert "gamma delta" in content_text
    # ...and the reasoning transcript is not suppressed by the matcher.
    assert _compact(reasoning_text).count("alphabeta") == 1
    assert "prefix" in reasoning_text
