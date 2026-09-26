"""Tests: GET /api/session full-transcript escape hatch + bounded recovery
calls — #7310 / #7625.

Bare ``/api/session`` reads (no ``msg_limit``) were issued by six frontend
recovery paths (offline/bfcache ``refreshSession``, stream-end settle, cancel
sync, /compress preflight, /retry, /undo). Each used to re-walk, re-redact and
re-serialize the ENTIRE transcript (4-15s / 28MB on a 5k-row session, measured
in #7625). Those call sites now request a bounded tail explicitly
(``msg_limit=30``), while the two paths that address rows by absolute
transcript index (outline jump, jump-to-start) opt in to the full transcript
via the explicit ``msg_limit=all`` escape hatch.

The bare no-limit HTTP shape deliberately keeps its historical full-transcript
contract: upstream contract tests pin tool-row preservation
(``test_state_db_reconciliation_preserves_sidecar_order_when_timestamps_collide``)
and the runtime-journal live snapshot
(``test_paginated_session_followup_does_not_repeat_runtime_snapshot``) on that
shape, so the fix is enforced at the frontend call sites rather than by
changing the server default.
"""
from __future__ import annotations

import re
from pathlib import Path

from api.routes import (
    _MAX_MSG_LIMIT,
    _parse_msg_limit,
    _resolve_effective_msg_limit,
)

_ROOT = Path(__file__).resolve().parents[1]


# ── helper: _resolve_effective_msg_limit ──


def test_bare_request_keeps_full_transcript_contract():
    """A bare reload (no msg_limit, no msg_before) keeps the historical
    full-transcript contract — server-side default windowing was rejected
    because contract tests pin tool-row preservation and the runtime-journal
    snapshot on this shape (#7310/#7625)."""
    limit, explicit_all = _resolve_effective_msg_limit(None)
    assert limit is None
    assert explicit_all is False


def test_empty_and_malformed_keep_contract():
    assert _resolve_effective_msg_limit("") == (None, False)
    assert _resolve_effective_msg_limit("not-a-number") == (None, False)


def test_explicit_all_resolves_to_full_transcript():
    """"msg_limit=all → (None, True): the explicit full-transcript escape
    hatch for outline jump / jump-to-start."""
    limit, explicit_all = _resolve_effective_msg_limit("all")
    assert limit is None
    assert explicit_all is True


def test_explicit_all_case_insensitive():
    assert _resolve_effective_msg_limit("ALL") == (None, True)
    assert _resolve_effective_msg_limit(" All ") == (None, True)


def test_numeric_limit_unchanged():
    """Explicit numeric limits keep the clamp behaviour — real pagination is
    unaffected."""
    assert _resolve_effective_msg_limit("30") == (30, False)
    assert _resolve_effective_msg_limit("9999") == (_MAX_MSG_LIMIT, False)
    assert _resolve_effective_msg_limit("0") == (1, False)


def test_parse_msg_limit_all_returns_none():
    """The parse helper returns None for 'all' — the handler distinguishes it
    from a bare request via _resolve_effective_msg_limit."""
    assert _parse_msg_limit("all") is None
    assert _parse_msg_limit("ALL") is None


# ── frontend call sites: bounded shapes (no bare GET /api/session left) ──

_UI_JS = (_ROOT / "static" / "ui.js").read_text(encoding="utf-8")
_MESSAGES_JS = (_ROOT / "static" / "messages.js").read_text(encoding="utf-8")
_SESSIONS_JS = (_ROOT / "static" / "sessions.js").read_text(encoding="utf-8")
_COMMANDS_JS = (_ROOT / "static" / "commands.js").read_text(encoding="utf-8")
_OUTLINE_JS = (_ROOT / "static" / "outline.js").read_text(encoding="utf-8")

_BOUNDED = "&messages=1&resolve_model=0&msg_limit=30&expand_renderable=1"
_ALL_QS = "&messages=1&resolve_model=0&msg_limit=all"


def test_refresh_session_uses_bounded_tail():
    """Offline/bfcache recovery refreshSession must not pull the full
    transcript (#7310/#7625)."""
    assert _BOUNDED in _UI_JS


def test_settle_and_cancel_paths_use_bounded_tail():
    """stream_end settle recovery AND cancel sync must not pull the full
    transcript."""
    assert _BOUNDED in _MESSAGES_JS
    assert _MESSAGES_JS.count(_BOUNDED) >= 2


def test_cancel_payload_restores_truncation_signal():
    """_applyCancelSessionPayload keeps the Load-earlier paging gate honest
    after a bounded cancel reload."""
    assert "!!sessionPayload._messages_truncated" in _MESSAGES_JS
    assert "sessionPayload._messages_offset||0" in _MESSAGES_JS


def test_settle_restores_truncation_signal():
    """_restoreSettledSession keeps the Load-earlier paging gate honest after
    a bounded settle reload."""
    assert "!!session._messages_truncated" in _MESSAGES_JS
    assert "session._messages_offset||0" in _MESSAGES_JS


def _restore_settled_session_body():
    start = _MESSAGES_JS.index("async function _restoreSettledSession")
    end = _MESSAGES_JS.index("function _handleStreamError", start)
    return _MESSAGES_JS[start:end]


def _apply_cancel_session_payload_body():
    start = _MESSAGES_JS.index("const _applyCancelSessionPayload=(sessionPayload)=>")
    end = _MESSAGES_JS.index("Prefer the canonical session snapshot", start)
    return _MESSAGES_JS[start:end]


def test_settle_refreshes_paging_before_anchor_persist():
    """Anchor persist reads _oldestIdx; a bounded tail must refresh it first (#7628)."""
    restore = _restore_settled_session_body()
    offset_idx = restore.index("_oldestIdx=session._messages_offset||0")
    attach_idx = restore.index("_attachProjectedAnchorSceneToLastAssistant(S.messages);")
    assert offset_idx < attach_idx


def test_cancel_refreshes_paging_before_anchor_persist():
    """Cancel recovery has the same persist-before-offset trap as settle (#7628)."""
    cancel = _apply_cancel_session_payload_body()
    offset_idx = cancel.index("_oldestIdx=sessionPayload._messages_offset||0")
    attach_idx = cancel.index("_attachProjectedAnchorSceneToLastAssistant(_nextMsgs3018);")
    assert offset_idx < attach_idx


def test_settle_preserves_terminal_marker_against_bounded_suffix():
    """Long-session bounded tails are suffixes of S.messages, not prefixes (#7628)."""
    restore = _restore_settled_session_body()
    assert "_stagedMatchesCurrentSuffix" in restore
    assert "(_truncatedRecovery?_stagedMatchesCurrentSuffix:_stagedMatchesCurrentPrefix)" in restore


def test_settle_truncation_signal_decides_match_strategy():
    """The server's truncation signal must decide prefix-vs-suffix, never an `||`:
    bounded settles get suffix-exclusive matching (identical repeated turns make
    BOTH comparisons succeed; the prefix branch then splices at the wrong offset
    and silently drops/duplicates rows) (#7628)."""
    restore = _restore_settled_session_body()
    assert "_truncatedRecovery=(typeof _messagesTruncated!=='undefined'&&!!_messagesTruncated)||(typeof _oldestIdx!=='undefined'&&!!(_oldestIdx>0))" in restore
    # The splice offset must follow the SAME truncation decision as the match
    # strategy — no fallthrough to prefix when both match.
    assert "_truncatedRecovery?_stagedSuffixStart+_stagedMessages.length:_stagedMessages.length" in restore


def test_apperror_embedded_session_refreshes_paging_before_anchor_persist():
    """The apperror embedded-session path has the same persist-before-offset trap (#7628)."""
    start = _MESSAGES_JS.index("source.addEventListener('apperror'")
    end = _MESSAGES_JS.index("source.addEventListener('warning'", start)
    block = _MESSAGES_JS[start:end]
    offset_idx = block.index("_oldestIdx=d.session._messages_offset||0")
    attach_idx = block.index("_attachProjectedAnchorSceneToLastAssistant(_nextMsgs3018);")
    assert offset_idx < attach_idx


def test_compress_preflight_uses_bounded_tail():
    """/compress preflight only needs session existence + the current tail."""
    assert _BOUNDED in _COMMANDS_JS


def test_retry_uses_bounded_tail():
    """/retry recovery must not pull the full transcript."""
    assert _COMMANDS_JS.count(_BOUNDED) >= 1


def test_undo_uses_bounded_tail():
    """/undo recovery must not pull the full transcript."""
    assert _COMMANDS_JS.count(_BOUNDED) >= 2


def test_recovery_paths_restore_truncation_signal():
    """preflight/retry/undo must NOT hardcode _messagesTruncated=false — they
    read the server's truncation signal instead."""
    assert "!!(live.session._messages_truncated)" in _COMMANDS_JS
    assert "_messagesTruncated=!!data.session._messages_truncated" in _COMMANDS_JS


def test_jump_to_start_requests_explicit_full_transcript():
    """_ensureAllMessagesLoaded genuinely needs everything (absolute-index
    addressing of the earliest rows) — it must opt in via msg_limit=all."""
    assert _ALL_QS in _SESSIONS_JS


def test_outline_jump_requests_explicit_full_transcript():
    """Outline jump addresses rows by absolute transcript index — it must opt
    in via msg_limit=all."""
    assert _ALL_QS in _OUTLINE_JS


def test_no_bare_session_fetch_remains_in_frontend():
    """Regression guard: NO static/ caller may issue a bare GET /api/session
    (no messages=/msg_limit= params). Use the bounded tail shape or the
    explicit msg_limit=all escape hatch."""
    bare_template = re.compile(
        r"api\(`/api/session\?session_id=\$\{encodeURIComponent\([^)]*\}\)`\)"
    )
    bare_concat = re.compile(
        r"api\('/api/session\?session_id='\s*\+\s*encodeURIComponent\([^)]*\)\)"
    )
    for name, src in (
        ("ui.js", _UI_JS),
        ("messages.js", _MESSAGES_JS),
        ("sessions.js", _SESSIONS_JS),
        ("commands.js", _COMMANDS_JS),
        ("outline.js", _OUTLINE_JS),
    ):
        assert not bare_template.findall(src), f"{name} still has a bare template-literal /api/session fetch"
        assert not bare_concat.findall(src), f"{name} still has a bare concatenated /api/session fetch"