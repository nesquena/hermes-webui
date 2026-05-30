"""Regression coverage for queued-turn restore persistence across later assistant replies.

Problem: the old restore path dropped any queued entry whose `_queued_at` was older
than the last assistant timestamp in the session. That sounds reasonable for one
stale draft, but it is wrong for real FIFO queues: if the user queued several
messages, the first queued turn can finish and append a newer assistant reply
while the remaining queued turns are still valid future work. Reloading the page
must keep those remaining queued entries.
"""
from pathlib import Path

REPO = Path(__file__).parent.parent
SESSIONS_JS = (REPO / "static" / "sessions.js").read_text(encoding="utf-8")


def test_queue_restore_no_longer_prunes_entries_by_last_assistant_timestamp():
    compact = SESSIONS_JS.replace(" ", "")
    assert "_fresh=_entries.filter(e=>!e._queued_at||e._queued_at>_lastAsst);" not in compact
    assert "const_lastMsg=S.messages.slice().reverse()" not in compact
    assert "const_lastAsst=_lastMsg?(_lastMsg.timestamp||_lastMsg._ts||0)*1000:0;" not in compact


def test_queue_restore_rehydrates_normalized_entries_and_repersists_them():
    assert "const _entries=_storedQ" in SESSIONS_JS
    assert ".filter(e=>e&&typeof e==='object')" in SESSIONS_JS
    assert "const _text=typeof e.text==='string'" in SESSIONS_JS
    assert "const _files=Array.isArray(e.files) ? e.files : [];" in SESSIONS_JS
    assert "return {...e,text:_text,files:_files};" in SESSIONS_JS
    assert "if(typeof _persistSessionQueueStorage==='function') _persistSessionQueueStorage(sid,_entries);" in SESSIONS_JS


def test_queue_restore_toast_uses_normalized_entry_count_after_reload():
    assert "(_entries.length>1?`${_entries.length} queued messages restored (showing first)`:'Queued message restored')" in SESSIONS_JS
