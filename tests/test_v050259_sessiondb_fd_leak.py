"""Regression tests for SessionDB FD-leak fixes (PR #1421) plus the
subagent shared-handle race (close-under-live-subagents).

History
-------
PR #1421: `_run_agent_streaming` created a new `SessionDB` per request and
replaced the cached agent's `_session_db` without closing the old one.
After ~73 messages on a long-lived agent, leaked FDs exhausted the 256 FD
default limit causing `EMFILE` crashes. Fix: close the previous handle
when it is safe to replace it.

Follow-up (this change): always-close-before-replace is *not* safe when
background subagents still hold a reference to the same SessionDB object
(delegate_tool copies ``parent._session_db`` by ref). A server-side wakeup
/ new turn for the parent session was closing the shared handle mid-child-
run, producing:

    Session DB append_message failed: 'NoneType' object has no attribute 'execute'

Policy now (``_adopt_session_db_for_cached_agent``):
- existing handle still open → keep it, close the unused *new* handle
- existing handle missing/closed → adopt the new handle (close dead one)
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


# ── 1: source-level pin: cached-agent reuse uses the adopt helper ──────────


def test_cached_agent_reuse_uses_adopt_helper():
    """Cached-agent reuse must go through `_adopt_session_db_for_cached_agent`
    so a still-open SessionDB is reused (subagent-safe) and only a dead handle
    is closed+replaced (still EMFILE-safe)."""
    src = (REPO / "api" / "streaming.py").read_text(encoding="utf-8")

    reuse_idx = src.find("Refresh per-turn callbacks")
    assert reuse_idx != -1, "cached-agent reuse block missing"
    block = src[reuse_idx : reuse_idx + 2500]

    assert "_adopt_session_db_for_cached_agent" in block, (
        "cached-agent reuse path must call _adopt_session_db_for_cached_agent "
        "instead of unconditionally closing agent._session_db. Unconditional "
        "close breaks background subagents that share the handle by reference."
    )
    assert "agent._session_db = _session_db" in block, (
        "reuse path must still assign the adopted SessionDB onto the agent"
    )
    # The old unconditional-close pattern must not remain in the reuse block.
    assert "agent._session_db.close()" not in block, (
        "unconditional agent._session_db.close() in the reuse path is the "
        "subagent race; close is now owned by _adopt_session_db_for_cached_agent"
    )


def test_adopt_and_is_open_helpers_exist():
    src = (REPO / "api" / "streaming.py").read_text(encoding="utf-8")
    assert "def _session_db_is_open(" in src
    assert "def _adopt_session_db_for_cached_agent(" in src
    # self-heal path must also refuse to close a still-open handle
    replace_idx = src.find("def _replace_session_db_in_kwargs")
    assert replace_idx != -1
    block = src[replace_idx : replace_idx + 1200]
    assert "_session_db_is_open" in block, (
        "_replace_session_db_in_kwargs must guard on _session_db_is_open so "
        "credential self-heal cannot close a handle live subagents share"
    )
    # adopt helper must log failed closes (not bare `pass`) so EMFILE pressure
    # from a failed close is diagnosable — matches _replace_session_db_in_kwargs.
    adopt_idx = src.find("def _adopt_session_db_for_cached_agent")
    assert adopt_idx != -1
    adopt_block = src[adopt_idx : adopt_idx + 1800]
    assert 'Failed to close unused session_db handle in adopt helper' in adopt_block
    assert "logger.debug" in adopt_block


def test_cached_agent_reuse_sets_current_agent_for_fallback_warning_metadata():
    """The cached-agent reuse path must publish the reused agent to the
    per-request status callback holder before refreshing status_callback.

    `_agent_status_callback()` enriches fallback warning SSE payloads with the
    live agent's model/provider via `_current_agent[0]`. `_run_agent_streaming`
    is per-request, so a cache-hit turn starts with `_current_agent = [None]`;
    without assigning the reused cached agent in this block, mid-conversation
    fallback notices still render but lose the model/provider badge.
    """
    src = (REPO / "api" / "streaming.py").read_text(encoding="utf-8")

    reuse_idx = src.find("Refresh per-turn callbacks")
    assert reuse_idx != -1, "cached-agent reuse block missing"
    block = src[reuse_idx : reuse_idx + 1200]

    current_agent_idx = block.find("_current_agent[0] = agent")
    status_callback_idx = block.find("agent.status_callback = _agent_kwargs.get('status_callback')")
    assert current_agent_idx != -1, (
        "cached-agent reuse must set _current_agent[0] so fallback warning "
        "events include to_model/to_provider on normal multi-turn cache hits."
    )
    assert status_callback_idx != -1, "cached-agent reuse must refresh status_callback"
    assert current_agent_idx < status_callback_idx, (
        "_current_agent[0] must be set before the refreshed status_callback can "
        "emit fallback warning metadata for the reused agent."
    )


def test_fallback_notice_persisted_on_assistant_message_before_save():
    """Fallback notices must be session-persisted as structured metadata on
    the turn's final assistant message, not inserted as an orphan DOM node.

    The gate certification on PR #5755 found that `appendFallbackNotice()`
    inserted a DOM node that was wiped by the next `renderMessages()` /
    session-switch / stream-completion — so the "persistent" notice was not
    actually persistent. The fix stores `_fallbackNotice` on the message
    before `s.save()`, and `renderMessages()` renders it from `S.messages`
    like `provider_details` / `_statusCard`.
    """
    src = (REPO / "api" / "streaming.py").read_text(encoding="utf-8")

    # 1. _pending_fallback_notices holder is declared alongside _current_agent
    assert "_pending_fallback_notices = []" in src, (
        "_pending_fallback_notices holder must be initialised so the status "
        "callback can accumulate fallback data for session persistence."
    )

    # 2. The callback captures fallback data into the holder
    capture_idx = src.find("_pending_fallback_notices.append(")
    assert capture_idx != -1, (
        "_agent_status_callback must append to _pending_fallback_notices so "
        "the fallback metadata is available for persistence before s.save()."
    )

    # 3. The metadata is stamped on the last assistant message before s.save()
    stamp_idx = src.find("_dm['_fallbackNotice']")
    assert stamp_idx != -1, (
        "The turn's final assistant message must receive _fallbackNotice "
        "metadata before s.save() so it survives renderMessages() rebuilds."
    )

    # 4. The stamping must happen in the pre-save metadata block (near _turnDuration)
    turn_duration_idx = src.find("_dm['_turnDuration']")
    assert turn_duration_idx != -1, "_turnDuration stamping block not found"
    assert stamp_idx > turn_duration_idx and stamp_idx < turn_duration_idx + 1200, (
        "_fallbackNotice must be stamped in the same pre-save metadata block "
        "as _turnDuration/_turnTps, before s.save()."
    )

    # 5. The orphan DOM insertion function must not exist in ui.js
    # (greptile flagged the original assertion checked streaming.py — a Python
    # file — for a JavaScript function name, so it was always true and provided
    # no actual regression coverage.)
    ui_src = (REPO / "static" / "ui.js").read_text(encoding="utf-8")
    assert "function appendFallbackNotice(" not in ui_src, (
        "appendFallbackNotice() was the orphan-DOM approach that failed "
        "persistence — it should be removed from ui.js."
    )

    # 6. The heal-success save path must ALSO flush _pending_fallback_notices.
    # The except-path credential self-heal retries the conversation through its
    # own session-save block (s.save() + early return) that runs BEFORE the
    # normal pre-save metadata block above. Without a flush there, a fallback
    # notice emitted during the heal's run_conversation() is lost on the next
    # session switch / page reload (greptile P1: heal notices not saved).
    heal_save_idx = src.find("self-heal (except path): retry succeeded")
    assert heal_save_idx != -1, "heal-success save path marker not found"
    heal_block = src[heal_save_idx - 4000:heal_save_idx]
    assert "_dm['_fallbackNotice']" in heal_block, (
        "The heal-success save path must flush _pending_fallback_notices onto "
        "the final assistant message before its own s.save(), because it "
        "returns before the normal pre-save metadata block runs."
    )


def test_error_save_paths_flush_fallback_notices():
    """Every s.save() path that finalizes an assistant turn must flush
    _pending_fallback_notices, including the ERROR save paths.

    The greptile P1 "error saves drop notices" finding identified two error
    save sites that append a final assistant error message and call s.save()
    without stamping _fallbackNotice: the compression-continuation error path
    and the main except-path error save. When a fallback warning was captured
    during streaming and the stream later errored, the saved message had no
    _fallbackNotice, so the notice vanished on reload/session switch.
    """
    src = (REPO / "api" / "streaming.py").read_text(encoding="utf-8")

    # The compression-continuation error path builds _error_message, stamps
    # _compressionRecovery, then appends+saves. The _fallbackNotice flush must
    # appear between the _compressionRecovery stamping and the append.
    comp_err_idx = src.find("_error_message['_compressionRecovery'] = _recovery")
    assert comp_err_idx != -1, "compression-continuation error path not found"
    # Find the s.messages.append(_error_message) that follows this stamping.
    append_after_comp = src.find("s.messages.append(_error_message)", comp_err_idx)
    assert append_after_comp != -1, "append after compression error stamping not found"
    comp_block = src[comp_err_idx:append_after_comp]
    assert "_error_message['_fallbackNotice']" in comp_block, (
        "The compression-continuation error save path must flush "
        "_pending_fallback_notices onto _error_message before s.save()."
    )

    # The main error path: find the LAST occurrence of the 'Interruption details'
    # label stamping (the main path uses _exc_type, not _err_type), then verify
    # the flush appears before the append+save.
    main_err_label = src.rfind("_error_message['provider_details_label'] = 'Interruption details'")
    assert main_err_label != -1, "main error path label stamping not found"
    append_after_main = src.find("s.messages.append(_error_message)", main_err_label)
    assert append_after_main != -1, "append after main error label stamping not found"
    main_block = src[main_err_label:append_after_main]
    assert "_error_message['_fallbackNotice']" in main_block, (
        "The main error save path must flush _pending_fallback_notices onto "
        "_error_message before s.save() so fallback notices survive reload."
    )


def test_fallback_classifier_matches_confirmed_switch_only():
    """The lifecycle classifier must match the CONFIRMED post-switch notice
    ("switched to fallback"), NOT transient pre-switch messages ("switching
    to fallback", "rate limited", etc.).

    The Agent has two distinct fallback emission paths:
    - Transient (pre-switch): _buffer_status("... switching to fallback: ...")
      — buffered, only flushed on terminal FAILURE, carries OLD model.
    - Confirmed (post-switch): _emit_status("Switched to fallback model: ...")
      — emitted ONLY on SUCCESS, AFTER agent.model/provider have changed.

    Matching transient strings inverts the contract: successful fallbacks
    produce no persistent notice, while failed attempts get false positives.
    (Gate-certifier blocking finding #1.)
    """
    src = (REPO / "api" / "streaming.py").read_text(encoding="utf-8")

    func_idx = src.find("def _is_fallback_lifecycle_message(")
    assert func_idx != -1, "_is_fallback_lifecycle_message not found"
    # Read the function body (up to the next def at column 0)
    next_def = src.find("\ndef ", func_idx + 1)
    func_body = src[func_idx:next_def] if next_def != -1 else src[func_idx:func_idx + 800]

    # Must match the confirmed post-switch string in the RETURN statement
    # (not just the docstring — the actual code must use it)
    return_idx = func_body.find("return (")
    assert return_idx != -1, "return statement not found in classifier"
    return_body = func_body[return_idx:]
    assert "switched to fallback" in return_body, (
        "Classifier return must match 'switched to fallback' (the confirmed post-switch "
        "notice emitted by _emit_pending_fallback_notice on success)."
    )
    # Must NOT match transient pre-switch strings in the RETURN statement
    assert "switching to fallback" not in return_body, (
        "Classifier must NOT match 'switching to fallback' — that's the transient "
        "pre-switch buffer line that fires before the model has changed and may "
        "never succeed. Matching it inverts the fallback contract."
    )
    assert "rate limited" not in return_body, (
        "Classifier must NOT match 'rate limited' — rate-limit retries are "
        "transient and may not result in a model switch."
    )
    assert "falling back" not in return_body, (
        "Classifier must NOT match 'falling back' — transient pre-switch."
    )
    assert "trying fallback" not in return_body, (
        "Classifier must NOT match 'trying fallback' — transient pre-switch."
    )


def test_stream_scoped_fallback_notices_dict_exists():
    """A module-level _STREAM_FALLBACK_NOTICES dict must exist so cancel_stream()
    — which runs outside _run_agent_streaming's closure — can read the latest
    confirmed fallback notice and stamp it before its own s.save().

    Without this, a user who clicks Stop after a real fallback sees the live
    SSE warning but loses the persistent _fallbackNotice after reload.
    (Gate-certifier blocking finding #2.)
    """
    src = (REPO / "api" / "streaming.py").read_text(encoding="utf-8")

    # 1. Module-level dict declaration
    assert "_STREAM_FALLBACK_NOTICES" in src, (
        "_STREAM_FALLBACK_NOTICES module-level dict must be declared so "
        "cancel_stream() can access fallback notices outside the streaming closure."
    )
    decl_idx = src.find("_STREAM_FALLBACK_NOTICES: dict = {}")
    assert decl_idx != -1, (
        "_STREAM_FALLBACK_NOTICES must be declared as a module-level dict."
    )

    # 2. The callback writes to it
    callback_write_idx = src.find("_STREAM_FALLBACK_NOTICES[stream_id] = _pending_fallback_notices[-1]")
    assert callback_write_idx != -1, (
        "_agent_status_callback must mirror the latest notice to "
        "_STREAM_FALLBACK_NOTICES[stream_id] so cancel_stream() can read it."
    )

    # 3. cancel_stream() reads and stamps it before _cs.save()
    cancel_stamping = src.find("_cancel_fb_notice = _STREAM_FALLBACK_NOTICES.get(stream_id)")
    assert cancel_stamping != -1, (
        "cancel_stream() must read _STREAM_FALLBACK_NOTICES before its s.save() "
        "so a mid-stream cancel after a real fallback still persists the notice."
    )
    # Verify the stamping skips the _error cancel marker
    stamping_block = src[cancel_stamping:cancel_stamping + 500]
    assert "not _dm.get('_error')" in stamping_block, (
        "cancel_stream() must skip the _error cancel marker when stamping "
        "_fallbackNotice — stamp the partial/prior assistant message instead."
    )

    # 4. The finally cleanup pops it
    cleanup_idx = src.find("_STREAM_FALLBACK_NOTICES.pop(stream_id, None)")
    assert cleanup_idx != -1, (
        "_run_agent_streaming's finally block must pop _STREAM_FALLBACK_NOTICES "
        "to prevent unbounded growth across streams."
    )


def test_textless_turn_render_includes_fallback_notice():
    """The ordered-parts render path must insert the fallback notice even when
    lastTextPartIdx === -1 (tool-only / textless assistant turns).

    Previously, the notice was only inserted inside the forEach loop when
    isLastTextPart was true — which never fires for a turn with no text parts.
    (Gate-certifier blocking finding #3.)
    """
    src = (REPO / "static" / "ui.js").read_text(encoding="utf-8")

    # Find the post-loop stamping for the textless case
    textless_stamp = src.find("lastTextPartIdx === -1 && fallbackNoticeHtml")
    assert textless_stamp != -1, (
        "The ordered-parts render path must handle lastTextPartIdx === -1 by "
        "stamping fallbackNoticeHtml on firstSeg after the loop, so tool-only "
        "turns still show the fallback notice."
    )


# ── 2: source-level pin: LRU eviction path also closes _session_db ──────────


def test_lru_eviction_closes_evicted_agent_session_db():
    """SAME LEAK SHAPE on the LRU eviction path: when SESSION_AGENT_CACHE
    grows beyond SESSION_AGENT_CACHE_MAX (default 25), the LRU agent gets popped
    via `popitem(last=False)`. Without explicit close, its `_session_db` waits
    on GC finalization which may never run on a long-lived server.

    Fix: capture the evicted entry, close its agent's `_session_db` before
    dropping the reference. (Eviction is a true session boundary — no live
    subagents are expected to still be writing into that agent.)
    """
    src = (REPO / "api" / "streaming.py").read_text(encoding="utf-8")

    eviction_idx = src.find("Evicted LRU agent from cache")
    assert eviction_idx != -1, "LRU eviction debug log missing"
    block = src[max(0, eviction_idx - 1500) : eviction_idx + 200]

    assert "evicted_sid, _ = SESSION_AGENT_CACHE.popitem" not in block, (
        "LRU eviction must capture the evicted entry so the agent's "
        "_session_db can be closed. The `evicted_sid, _ = ...` discard form "
        "is the original bug shape."
    )

    assert "_close_evicted_agent_at_session_boundary(_evicted_sid, _evicted_agent)" in block, (
        "LRU eviction must route the evicted agent through the session-boundary "
        "close helper."
    )
    helper_start = src.index("def _close_evicted_agent_at_session_boundary")
    helper_end = src.index("\ndef _refresh_cached_agent_runtime", helper_start)
    helper_block = src[helper_start:helper_end]
    assert "session_db.close()" in helper_block, (
        "LRU eviction helper must close the evicted agent's _session_db. "
        "(Opus pre-release follow-up to PR #1421.)"
    )


# ── 3: behavioral: SessionDB.close() is idempotent + safe ──────────────────


def test_session_db_close_is_idempotent():
    """`SessionDB.close()` must be safe to call multiple times."""
    import importlib.util
    if importlib.util.find_spec("hermes_state") is None:
        pytest.skip("hermes_state not on import path (CI-only — agent repo not present)")
    from hermes_state import SessionDB  # type: ignore
    import tempfile

    with tempfile.TemporaryDirectory() as tmpd:
        db_path = Path(tmpd) / "test.db"
        db = SessionDB(db_path=db_path)
        with db._lock:
            db._conn.execute("SELECT 1")
        db.close()
        assert db._conn is None
        db.close()
        assert db._conn is None
        db.close()


# ── 4: behavioral: adopt helper keeps open handles, closes dead ones ───────


class _MockSessionDB:
    def __init__(self, name, open_=True):
        self.name = name
        self.close_calls = 0
        # Mirror SessionDB: open → _conn is truthy; closed → _conn is None.
        self._conn = object() if open_ else None

    def close(self):
        self.close_calls += 1
        self._conn = None


class _MockAgent:
    def __init__(self, db):
        self._session_db = db
        self.stream_delta_callback = None
        self.tool_progress_callback = None
        self._api_call_count = 0
        self._interrupted = False
        self._interrupt_message = None


def _import_adopt_helpers():
    """Import the production helpers.

    No source-slicing / exec fallback: that path breaks as soon as the helpers
    reference module-level names (e.g. ``logger.debug``) or another def is
    inserted between the markers. Prefer a real import; skip the behavioral
    suite when the package cannot be imported (CI without full deps).
    Source-level pins above still catch reverts without importing streaming.
    """
    try:
        from api.streaming import (  # type: ignore
            _adopt_session_db_for_cached_agent,
            _session_db_is_open,
        )
    except Exception as exc:
        pytest.skip(f"api.streaming helpers not importable: {exc}")
    return _session_db_is_open, _adopt_session_db_for_cached_agent


def test_adopt_reuses_open_session_db_and_closes_new():
    """Live (open) existing handle must be kept; unused new handle closed.

    This is the subagent-safe path: children hold a reference to `old_db`.
    """
    _is_open, adopt = _import_adopt_helpers()
    old_db = _MockSessionDB("old", open_=True)
    new_db = _MockSessionDB("new", open_=True)
    agent = _MockAgent(old_db)

    result = adopt(agent, new_db)

    assert result is old_db
    assert agent._session_db is old_db
    assert old_db.close_calls == 0, "must not close the live shared handle"
    assert new_db.close_calls == 1, "unused per-request handle must be closed (FD leak)"
    assert _is_open(old_db) is True
    assert _is_open(new_db) is False


def test_adopt_replaces_closed_session_db():
    """Dead existing handle is closed (idempotent) and replaced with the new one."""
    _is_open, adopt = _import_adopt_helpers()
    old_db = _MockSessionDB("old", open_=False)
    new_db = _MockSessionDB("new", open_=True)
    agent = _MockAgent(old_db)

    result = adopt(agent, new_db)

    assert result is new_db
    assert agent._session_db is new_db
    assert old_db.close_calls == 1
    assert new_db.close_calls == 0


def test_adopt_handles_missing_existing():
    _is_open, adopt = _import_adopt_helpers()
    new_db = _MockSessionDB("new", open_=True)
    agent = _MockAgent(None)
    agent._session_db = None

    result = adopt(agent, new_db)

    assert result is new_db
    assert agent._session_db is new_db
    assert new_db.close_calls == 0


def test_cached_agent_reuse_calls_adopt_semantics():
    """End-to-end mirror of the production reuse block using the real helper."""
    _is_open, adopt = _import_adopt_helpers()
    old_db = _MockSessionDB("old", open_=True)
    new_db = _MockSessionDB("new", open_=True)
    agent = _MockAgent(old_db)
    _session_db = new_db

    # Mirror production:
    if _session_db is not None:
        _session_db = adopt(agent, _session_db)
        agent._session_db = _session_db

    assert agent._session_db is old_db
    assert old_db.close_calls == 0
    assert new_db.close_calls == 1


# ── 5: behavioral: LRU eviction with mock agents ────────────────────────────


def test_lru_eviction_closes_evicted_session_db():
    """End-to-end: simulate LRU eviction and verify the evicted agent's
    SessionDB.close() is called."""
    import collections

    cache = collections.OrderedDict()
    db1, db2, db3 = _MockSessionDB("a"), _MockSessionDB("b"), _MockSessionDB("c")
    cache["sid-a"] = (_MockAgent(db1), "sig1")
    cache["sid-b"] = (_MockAgent(db2), "sig2")
    cache["sid-c"] = (_MockAgent(db3), "sig3")

    MAX = 2
    while len(cache) > MAX:
        evicted_sid, evicted_entry = cache.popitem(last=False)
        try:
            _evicted_agent = evicted_entry[0] if isinstance(evicted_entry, tuple) else None
            if _evicted_agent is not None and getattr(_evicted_agent, "_session_db", None) is not None:
                _evicted_agent._session_db.close()
        except Exception:
            pass

    assert "sid-a" not in cache
    assert db1.close_calls == 1, "evicted agent's SessionDB must be closed exactly once"
    assert db2.close_calls == 0, "remaining agents' SessionDBs must not be touched"
    assert db3.close_calls == 0


# ── 6: self-heal path must not reuse a CLOSED handle when the rebuild fails ──


def _import_replace_helper():
    """Import the real credential-self-heal SessionDB replacer."""
    try:
        from api.streaming import _replace_session_db_in_kwargs  # type: ignore
    except Exception as exc:
        pytest.skip(f"api.streaming not importable: {exc}")
    return _replace_session_db_in_kwargs


def test_replace_degrades_to_none_when_rebuild_fails_and_old_is_closed(monkeypatch):
    """Credential self-heal regression (Codex gate finding on PR #6143).

    When ``_build_session_db_for_stream`` returns None (rebuild failed) AND the
    prior handle is already CLOSED, ``_replace_session_db_in_kwargs`` must leave
    ``agent_kwargs['session_db'] = None`` — as master did — so the rebuilt agent
    lazily reinitialises. Retaining the closed handle (the pre-fix behaviour)
    makes every persist/search fail with
    ``'NoneType' object has no attribute 'execute'`` while the chat continues.
    """
    import api.streaming as streaming

    _replace = _import_replace_helper()
    monkeypatch.setattr(streaming, "_build_session_db_for_stream", lambda _p: None)

    old_db = _MockSessionDB("old", open_=False)  # already closed
    kwargs = {"session_db": old_db}
    result = _replace(kwargs, "/tmp/does-not-matter.db")

    assert result is None, "must not hand back a closed handle when rebuild fails"
    assert kwargs["session_db"] is None, "kwargs must degrade to None (clean lazy reinit)"


def test_replace_keeps_open_handle_when_rebuild_fails(monkeypatch):
    """Inverse: a still-OPEN prior handle (held by live subagents) is retained
    when the rebuild fails — do not orphan a live shared connection."""
    import api.streaming as streaming

    _replace = _import_replace_helper()
    monkeypatch.setattr(streaming, "_build_session_db_for_stream", lambda _p: None)

    old_db = _MockSessionDB("old", open_=True)  # still live (subagents hold it)
    kwargs = {"session_db": old_db}
    result = _replace(kwargs, "/tmp/does-not-matter.db")

    assert result is old_db, "a live handle must be kept when the rebuild fails"
    assert kwargs["session_db"] is old_db
    assert old_db.close_calls == 0, "must not close a live shared handle"
