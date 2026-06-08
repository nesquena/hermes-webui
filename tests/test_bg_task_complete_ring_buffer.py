"""Structural assertions for the bg_task_complete dedupe ring buffer.

Per P-bc §2.5: the WebUI consumer dedupe graduates from an unbounded ``Set``
keyed by ``task_id`` to a bounded ``Map``-backed ring buffer keyed by
``(session_id, event_id)``. The new structure carries:

  * A 60-second TTL (``_BG_TASK_COMPLETE_TTL_MS = 60000``).
  * A 256-entry soft cap (``_BG_TASK_COMPLETE_CAP = 256``).
  * Lazy purge: every insert walks the Map in insertion order and drops
    entries whose expiry has passed.
  * Insertion-order eviction on overflow (oldest entry dropped first).
  * A helper ``_bgTaskCompleteRingBufferAdd(sid, event_id)`` returning
    ``true`` on duplicate, ``false`` on first-seen.

We can't exercise JS at runtime from pytest (the repo intentionally avoids a
node/jsdom dep per AGENTS.md), so this file does structural / string-grep
assertions on ``static/messages.js`` — the same convention every other
WEBUI-SUB test uses. The grep targets are intentionally precise so a
behavioural regression (e.g. silently switching the dedupe key back to
``task_id`` or removing lazy purge) shows up as a hard test failure.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _read_messages_js() -> str:
    return (REPO_ROOT / "static" / "messages.js").read_text()


# ---------------------------------------------------------------------------
# Module-scope declarations
# ---------------------------------------------------------------------------


def test_ring_buffer_constants_declared_module_scope():
    """TTL and cap constants live at module scope, not buried inside a
    function body, so the dedupe state is shared across every EventSource
    (in-turn STREAMS + per-session SSE channel)."""
    js = _read_messages_js()
    assert "const _BG_TASK_COMPLETE_TTL_MS = 60000;" in js
    assert "const _BG_TASK_COMPLETE_CAP = 256;" in js
    # Map (not Set) carries the (key -> expiry) entries.
    assert "const _bgTaskCompleteSeenIds = new Map();" in js
    # Set form must be gone — D-b-2 replaced it.
    assert "_bgTaskCompleteSeenIds = new Set()" not in js
    assert "_seenProcessCompleteIds" not in js


def test_ring_buffer_helper_declared():
    """The add-and-dedupe helper exists and lives at module scope."""
    js = _read_messages_js()
    assert "function _bgTaskCompleteRingBufferAdd(sid, evt_id)" in js


# ---------------------------------------------------------------------------
# Helper-body internals
# ---------------------------------------------------------------------------


def _helper_source(js: str) -> str:
    ix = js.index("function _bgTaskCompleteRingBufferAdd(sid, evt_id)")
    # Helper body is short (~25-35 LOC); 2000 chars is comfortable headroom
    # without bleeding into the next function.
    return js[ix : ix + 2000]


def test_helper_ignores_missing_event_id():
    """Events without an event_id are ignored — server contract guarantees one.

    Missing key returns ``true`` (treat as seen/skip) rather than ``false``
    (proceed): if a future call site forgets the caller-side ``if (!evt_id)``
    guard, an un-keyable completion is dropped instead of processed without a
    dedupe key.
    """
    body = _helper_source(_read_messages_js())
    assert "if (!sid || !evt_id) return true;" in body


def test_helper_key_construction_uses_event_id_not_task_id():
    """Dedupe key is (session_id, event_id), the canonical contract surface."""
    body = _helper_source(_read_messages_js())
    # The composite key uses evt_id, not pid / task_id / process_id.
    assert "const key = sid + '|' + evt_id;" in body
    # Negative guards on the previous keying schemes.
    assert "sid + '|' + pid" not in body
    assert "sid + '|' + task_id" not in body
    assert "process_id" not in body  # legacy payload key, gone


def test_helper_lazy_purges_expired_entries():
    """Each add walks the Map in insertion order and drops expired entries.
    Without lazy purge the soft cap eviction would silently drop live entries
    while expired ones squat in the Map indefinitely."""
    body = _helper_source(_read_messages_js())
    assert "const now = Date.now();" in body
    assert "for (const [k, exp] of _bgTaskCompleteSeenIds)" in body
    assert "if (exp <= now)" in body
    assert "_bgTaskCompleteSeenIds.delete(k);" in body


def test_helper_returns_true_on_duplicate():
    """Duplicate detection short-circuits — returns true without inserting."""
    body = _helper_source(_read_messages_js())
    assert "if (_bgTaskCompleteSeenIds.has(key)) return true;" in body


def test_helper_inserts_with_expiry_in_future():
    """First-seen entries are stamped with now + TTL_MS."""
    body = _helper_source(_read_messages_js())
    assert "_bgTaskCompleteSeenIds.set(key, now + _BG_TASK_COMPLETE_TTL_MS);" in body


def test_helper_soft_cap_evicts_oldest():
    """Soft cap enforcement uses insertion-order eviction (Map.keys().next())."""
    body = _helper_source(_read_messages_js())
    assert "while (_bgTaskCompleteSeenIds.size > _BG_TASK_COMPLETE_CAP)" in body
    assert "_bgTaskCompleteSeenIds.keys().next().value" in body
    assert "_bgTaskCompleteSeenIds.delete(firstKey);" in body


def test_helper_returns_false_on_first_seen():
    """First-seen path returns false so the caller proceeds to surface/ack."""
    body = _helper_source(_read_messages_js())
    # The trailing `return false;` of the helper.
    assert re.search(r"return false;\s*\n\}", body), (
        "helper must end with `return false;` after the soft-cap loop"
    )


# ---------------------------------------------------------------------------
# Call-site integration in _handleBgTaskCompleteEvent
# ---------------------------------------------------------------------------


def _handler_source(js: str) -> str:
    ix = js.index("function _handleBgTaskCompleteEvent")
    return js[ix : ix + 2200]


def test_handler_calls_ring_buffer_helper():
    """The shared handler calls the helper with (sid, event_id) — not (sid, task_id)."""
    body = _handler_source(_read_messages_js())
    assert "_bgTaskCompleteRingBufferAdd(sid, evt_id)" in body
    # Old call shape must be gone.
    assert "_bgTaskCompleteSeenIds.has(dedupeKey)" not in body
    assert "_bgTaskCompleteSeenIds.add(dedupeKey)" not in body


def test_handler_extracts_event_id_from_payload():
    """The handler pulls event_id out of the parsed payload."""
    body = _handler_source(_read_messages_js())
    assert "d.event_id" in body
    # Missing event_id short-circuits before the dedupe / ack.
    assert "if (!evt_id) return;" in body


def test_handler_dedup_runs_before_ack_post():
    """Dedupe gate must precede the fire-and-forget ack POST so duplicates
    don't generate a flood of ack traffic."""
    body = _handler_source(_read_messages_js())
    dedupe_ix = body.index("_bgTaskCompleteRingBufferAdd(sid, evt_id)")
    ack_ix = body.index("api/bg-task-complete-ack")
    assert dedupe_ix < ack_ix, (
        "ring-buffer dedupe must execute before the ack POST"
    )
