import json
import re
import subprocess
import textwrap
from pathlib import Path


SESSIONS_JS = Path("static/sessions.js").read_text(encoding="utf-8")
UI_JS = Path("static/ui.js").read_text(encoding="utf-8")
BOOT_JS = Path("static/boot.js").read_text(encoding="utf-8")
PANELS_JS = Path("static/panels.js").read_text(encoding="utf-8")


def _function_body(src: str, name: str) -> str:
    # Extracted functions intentionally avoid braces in strings so this stays source-light.
    m = re.search(rf"(?:async\s+)?function\s+{re.escape(name)}\b", src)
    assert m, f"{name} function not found"
    sig_end = src.find(")", m.end())
    assert sig_end != -1, f"{name} function signature not terminated"
    brace_start = src.find("{", sig_end)
    assert brace_start != -1, f"{name} function body not found"
    depth = 0
    for idx in range(brace_start, len(src)):
        ch = src[idx]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return src[m.start():idx + 1]
    raise AssertionError(f"{name} function body not terminated")


def _run_node(script: str) -> dict:
    result = subprocess.run(["node", "-e", script], capture_output=True, text=True)
    if result.returncode != 0:
        raise AssertionError(f"node failed: {result.stderr}")
    return json.loads(result.stdout)


def test_load_session_supports_force_reload_for_external_refresh():
    assert "async function loadSession(sid)" in SESSIONS_JS
    assert "const opts = arguments[1] || {};" in SESSIONS_JS
    assert "const forceReload = !!opts.force" in SESSIONS_JS
    assert "if(currentSid===sid && !forceReload) return;" in SESSIONS_JS
    assert "loadSession(sid, {force:true" in SESSIONS_JS


def test_active_session_external_refresh_uses_metadata_then_force_reload():
    assert "function ensureActiveSessionExternalRefreshPoll()" in SESSIONS_JS
    assert "async function refreshActiveSessionIfExternallyUpdated(reason)" in SESSIONS_JS
    assert "messages=0&resolve_model=0" in SESSIONS_JS
    assert "if(remoteCount !== localCount)" in SESSIONS_JS
    assert "else if(remoteLast > localLast)" in SESSIONS_JS
    assert "if(S.busy || S.activeStreamId) return;" in SESSIONS_JS
    assert "document.hidden" in SESSIONS_JS
    assert "externalRefreshReason:reason||'poll'" in SESSIONS_JS


def test_active_session_external_refresh_skips_destructive_reload_on_metadata_only_bump():
    """A timestamp-only active-session update should not blank the transcript.

    Background skill/memory review can update session timestamps without adding
    chat messages. The old `remoteCount > localCount || remoteLast > localLast`
    condition called `loadSession(..., {force:true})` for that metadata-only
    bump; `loadSession(force)` clears S.messages before async message fetches,
    so the whole transcript visibly disappeared and reappeared with no new
    content. Only a message_count CHANGE should force-reload the transcript;
    timestamp-only bumps update local metadata and refresh the lightweight
    sidebar list.
    """
    assert "remoteCount > localCount || remoteLast > localLast" not in SESSIONS_JS
    assert "if(remoteCount !== localCount){" in SESSIONS_JS
    assert "await loadSession(sid, {force:true, externalRefreshReason:reason||'poll', keepStaleUntilLoaded:_keepStaleUntilLoaded});" in SESSIONS_JS
    assert "}else if(remoteLast > localLast){" in SESSIONS_JS
    assert "S.session.last_message_at = remoteLast" in SESSIONS_JS
    assert "if(data.session.updated_at) S.session.updated_at = data.session.updated_at;" in SESSIONS_JS


def test_active_session_external_refresh_force_reloads_on_count_decrease():
    """A LOWER remote message_count must still force-reload the transcript.

    Another tab/client can shrink the active transcript via /api/session/truncate,
    /retry, /undo, or regenerate — all reduce message_count while advancing
    updated_at. A `remoteCount > localCount` gate would treat that as a
    metadata-only bump and silently keep the stale (longer) transcript forever.
    The condition is `remoteCount !== localCount` precisely so a decrease also
    re-syncs.
    """
    # The force-reload branch must trigger on ANY count change, not just growth.
    assert "if(remoteCount !== localCount){" in SESSIONS_JS
    assert "if(remoteCount > localCount){" not in SESSIONS_JS
    # The metadata-only branch must be gated on an unchanged count (the else of
    # the count-change check), never reachable when the count differs.
    assert "}else if(remoteLast > localLast){" in SESSIONS_JS


def test_webui_source_never_counts_as_external_session():
    assert "function _isWebUiSourceSession(session)" in SESSIONS_JS
    assert "if (!session || _isWebUiSourceSession(session)) return false;" in SESSIONS_JS
    external_start = SESSIONS_JS.index("function _isExternalSession(session)")
    external_body = SESSIONS_JS[external_start : external_start + 300]
    assert "_isWebUiSourceSession(session)" in external_body
    assert "session.is_cli_session || _isMessagingSession(session)" in external_body


def test_active_session_external_refresh_has_focus_and_visibility_hooks():
    assert "visibilitychange" in SESSIONS_JS
    assert "window.addEventListener('focus'" in SESSIONS_JS
    assert "ensureActiveSessionExternalRefreshPoll();" in SESSIONS_JS


def test_session_time_refresh_has_own_visibility_hook():
    """Skipped hidden-tab timestamp ticks must refresh immediately on return."""
    start = SESSIONS_JS.find("function ensureSessionTimeRefreshPoll()")
    assert start != -1
    block = SESSIONS_JS[start:start + 900]
    assert "_sessionTimeRefreshVisibilityHandler" in SESSIONS_JS
    assert "document.addEventListener('visibilitychange', _sessionTimeRefreshVisibilityHandler);" in block
    assert "if(!document.hidden) renderSessionListFromCache();" in block
    assert "startStreamingPoll re-renders the list" not in block


def test_session_list_external_refresh_uses_sse_invalidation_not_polling():
    """New sessions should refresh the sidebar from server invalidation events."""
    assert "async function refreshSessionList(reason='manual', opts={})" in SESSIONS_JS
    assert "let _sessionListRefreshPendingRequest = null;" in SESSIONS_JS
    assert "function _mergeSessionListRefreshOptions(prev, next)" in SESSIONS_JS
    assert "function _refreshSessionListAfterSidebarResume(reason)" in SESSIONS_JS
    assert "_sessionEventsNeedsRefreshOnOpen = false" in SESSIONS_JS
    assert "void refreshSessionList(reason, {force:true})" in SESSIONS_JS
    assert "function ensureSessionEventsSSE()" in SESSIONS_JS
    assert "new EventSource('api/sessions/events')" in SESSIONS_JS
    assert "addEventListener('sessions_changed'" in SESSIONS_JS
    assert "function _scheduleSessionEventsRefresh(reason, opts={})" in SESSIONS_JS
    assert "let _sessionEventsRefreshPendingRequest = null;" in SESSIONS_JS
    assert "_sessionEventsNeedsRefreshOnOpen = true" in SESSIONS_JS
    assert "void _refreshSessionListAfterSidebarResume('focus')" in SESSIONS_JS
    assert "void _refreshSessionListAfterSidebarResume('visible')" in SESSIONS_JS
    assert "void _refreshSessionListAfterSidebarResume('reconnect')" in SESSIONS_JS
    assert "renderSessionList({deferWhileInteracting:!force})" in SESSIONS_JS
    assert "const refreshActive = !!(opts && opts.refreshActive)" in SESSIONS_JS
    assert "if(refreshActive) await refreshActiveSessionIfExternallyUpdated(reason||'session-list')" in SESSIONS_JS
    assert "_sessionListRefreshPendingRequest = {" in SESSIONS_JS
    assert "_scheduleSessionEventsRefresh(pendingRequest.reason, pendingRequest.opts)" in SESSIONS_JS
    assert "ensureSessionEventsSSE();" in SESSIONS_JS
    assert "document._hermesSessionEventsVisibilityHook" in SESSIONS_JS
    ensure_fn = SESSIONS_JS[SESSIONS_JS.find("function ensureSessionEventsSSE()") :]
    # The visibility hook must be installed before the open-guard early-return.
    # #4151 replaced the `document.hidden) return` open guard with the focus-aware
    # `_sidebarSseBackgrounded()) return` predicate (which also covers PWA blur).
    assert ensure_fn.find("document._hermesSessionEventsVisibilityHook") < ensure_fn.find("_sidebarSseBackgrounded()) return")
    assert "_sessionListExternalRefreshMs" not in SESSIONS_JS
    assert "addEventListener('sessions_changed', (ev) => {" in ensure_fn
    assert "const activeProfile = S.activeProfile || 'default';" in ensure_fn
    assert "const payload = typeof ev?.data === 'string' ? JSON.parse(ev.data) : {};" in ensure_fn
    assert "const eventProfile = payload && typeof payload.profile === 'string' ? payload.profile : '';" in ensure_fn
    assert "if (!_sessionEventProfilesMatch(eventProfile, activeProfile)) {" in ensure_fn
    assert "function _sessionEventTargetsActiveSession(payload)" in SESSIONS_JS
    assert "typeof payload.session_id === 'string'" in SESSIONS_JS
    assert "eventTargetsActiveSession?'event-active-session':'event'" in ensure_fn
    assert "_scheduleSessionEventsRefresh(eventTargetsActiveSession?'event-active-session':'event', {force:true, refreshActive:true})" in ensure_fn


def test_session_events_refresh_forces_hidden_sidebar_render_from_event_path():
    """The production sessions_changed path must force the hidden sidebar list render."""
    merge_body = (
        _function_body(SESSIONS_JS, "_mergeSessionListRefreshOptions")
        if "function _mergeSessionListRefreshOptions" in SESSIONS_JS
        else "function _mergeSessionListRefreshOptions(prev, next){ return {...(prev||{}), ...(next||{})}; }"
    )
    functions = "\n\n".join(
        [
            merge_body,
            _function_body(SESSIONS_JS, "refreshSessionList"),
            _function_body(SESSIONS_JS, "_scheduleSessionEventsRefresh"),
        ]
    )
    script = textwrap.dedent(
        f"""
        const record = [];
        global.document = {{hidden: true}};
        global.renderSessionList = async (opts) => record.push({{kind: 'render', opts}});
        global.refreshActiveSessionIfExternallyUpdated = async (reason) => record.push({{kind: 'active', reason}});
        global.setTimeout = (cb) => {{
          cb();
          return 1;
        }};
        let _sessionListRefreshInFlight = false;
        let _sessionListRefreshPendingRequest = null;
        let _sessionEventsRefreshTimer = 0;
        let _sessionEventsRefreshPendingRequest = null;
        {functions}
        (async() => {{
          _scheduleSessionEventsRefresh('event-active-session', {{force:true, refreshActive:true}});
          await Promise.resolve();
          process.stdout.write(JSON.stringify(record));
        }})().catch((err) => {{
          process.stderr.write(String(err.stack || err) + '\\n');
          process.exit(1);
        }});
        """
    )
    out = _run_node(script)
    assert out == [
        {"kind": "render", "opts": {"deferWhileInteracting": False}},
        {"kind": "active", "reason": "event-active-session"},
    ]


def test_session_list_external_refresh_forced_resume_survives_hidden_inflight_refresh():
    """Queued resume refreshes must keep `force:true` even after a hidden invalidation."""
    functions = "\n\n".join(
        _function_body(SESSIONS_JS, name)
        for name in (
            "_mergeSessionListRefreshOptions",
            "refreshSessionList",
            "_scheduleSessionEventsRefresh",
            "_refreshSessionListAfterSidebarResume",
        )
    )
    script = textwrap.dedent(
        f"""
        const record = [];
        const state = {{hidden: false}};
        global.document = state;
        let renderCount = 0;
        global.renderSessionList = async (opts) => {{
          record.push({{kind: 'render', opts}});
          renderCount += 1;
          if (renderCount === 1) {{
            state.hidden = true;
            await refreshSessionList('focus', {{force:true}});
          }}
        }};
        global.refreshActiveSessionIfExternallyUpdated = async (reason) => {{
          record.push({{kind: 'active', reason}});
        }};
        global.setTimeout = (cb) => {{
          cb();
          return 1;
        }};
        global.clearTimeout = () => {{}};
        let _sessionListRefreshInFlight = false;
        let _sessionListRefreshPendingRequest = null;
        let _sessionEventsRefreshTimer = 0;
        let _sessionEventsRefreshPendingRequest = null;
        {functions}
        (async() => {{
          await refreshSessionList('event', {{refreshActive:true}});
          await refreshSessionList('manual');
          process.stdout.write(JSON.stringify({{
            record,
            resumeSrc: _refreshSessionListAfterSidebarResume.toString(),
          }}));
        }})().catch((err) => {{
          process.stderr.write(String(err.stack || err) + '\\n');
          process.exit(1);
        }});
        """
    )
    out = _run_node(script)
    assert out["record"] == [
        {"kind": "render", "opts": {"deferWhileInteracting": True}},
        {"kind": "active", "reason": "event"},
        {"kind": "render", "opts": {"deferWhileInteracting": False}},
    ]
    assert "refreshSessionList(reason, {force:true})" in out["resumeSrc"]
    assert "refreshActive:true" not in out["resumeSrc"]


def test_session_events_refresh_timer_merges_pending_force_and_active_options():
    """The debounce window must not drop force or active-refresh intent."""
    functions = "\n\n".join(
        _function_body(SESSIONS_JS, name)
        for name in (
            "_mergeSessionListRefreshOptions",
            "refreshSessionList",
            "_scheduleSessionEventsRefresh",
        )
    )
    script = textwrap.dedent(
        f"""
        const record = [];
        global.document = {{hidden: true}};
        global.renderSessionList = async (opts) => record.push({{kind: 'render', opts}});
        global.refreshActiveSessionIfExternallyUpdated = async (reason) => record.push({{kind: 'active', reason}});
        let timerCb = null;
        global.setTimeout = (cb) => {{ timerCb = cb; return 1; }};
        global.clearTimeout = () => {{}};
        let _sessionListRefreshInFlight = false;
        let _sessionListRefreshPendingRequest = null;
        let _sessionEventsRefreshTimer = 0;
        let _sessionEventsRefreshPendingRequest = null;
        {functions}
        (async() => {{
          _scheduleSessionEventsRefresh('focus', {{force:true}});
          _scheduleSessionEventsRefresh('event-active-session', {{refreshActive:true}});
          timerCb();
          await Promise.resolve();
          process.stdout.write(JSON.stringify(record));
        }})().catch((err) => {{
          process.stderr.write(String(err.stack || err) + '\\n');
          process.exit(1);
        }});
        """
    )
    assert _run_node(script) == [
        {"kind": "render", "opts": {"deferWhileInteracting": False}},
        {"kind": "active", "reason": "event-active-session"},
    ]


def test_session_event_profile_filter_tolerates_default_root_aliases():
    assert "function _profileMatchesActiveProfile(profile, activeProfile)" in SESSIONS_JS
    assert "return eventName === 'default' && !!S.activeProfileIsDefault;" in SESSIONS_JS
    assert "function _sessionEventProfilesMatch(eventProfile, activeProfile)" in SESSIONS_JS
    assert "if (!_profileMatchesActiveProfile(sessionProfile, activeProfile)) return false;" in SESSIONS_JS
    assert "activeProfileIsDefault:true" in UI_JS
    assert "const activeProfileState = await _resolveActiveProfileBootstrapState();" in BOOT_JS
    assert "S.activeProfileIsDefault = activeProfileState.isDefault;" in BOOT_JS
    assert "S.activeProfileIsDefault = !!data.is_default;" in PANELS_JS


def test_session_list_render_signature_serializes_full_rows_not_a_narrow_allowlist():
    """The render-skip signature must serialize the FULL applied rows (+ reference
    rows), not a curated field subset — a narrow allowlist silently false-skips
    when a rendered field it omits changes (Codex #5467: it dropped pending/running
    streaming state, attention dots, and the source/lineage cluster, so an
    approval/clarify transition or a row starting to stream could keep a stale
    sidebar). Serializing the whole row objects covers every field the render
    helpers read, present and future.
    """
    start = SESSIONS_JS.find("function _sessionListRenderSignature()")
    assert start != -1
    block = SESSIONS_JS[start:start + 900]
    # Full-object serialization, not a hand-picked allowlist.
    assert "const sessionKeys = [" not in block, \
        "signature must not use a narrow field allowlist (it false-skips on omitted fields)"
    assert "sessionKeys.forEach" not in block
    assert "_allSessions," in block, "signature must serialize the full applied rows"
    assert "_sidebarReferenceSessions," in block, \
        "signature must include the hidden reference/nesting rows the sidebar renders"
    # Fail-open on serialization failure (never skip on null).
    assert "catch(_){ return null; }" in block


def test_session_list_render_signature_changes_on_pending_running_and_attention():
    """Regression for the Codex #5467 CORE/SILENT false-skips: because the
    signature serializes whole rows, a change to pending/running state or the
    attention dot changes the signature (so the sidebar repaints, not skips).
    This is a structural guarantee of full-row serialization — assert the
    signature does not strip those fields back out.
    """
    start = SESSIONS_JS.find("function _sessionListRenderSignature()")
    block = SESSIONS_JS[start:start + 900]
    # None of the previously-omitted, render-consumed fields may be filtered out.
    for stripped in (
        "delete out.active_stream_id",
        "delete out.attention",
        "delete out.has_pending_user_message",
    ):
        assert stripped not in block, f"signature must not strip {stripped!r}"
    # The whole-object arrays are passed straight to JSON.stringify.
    assert "JSON.stringify([" in block


def test_session_list_render_signature_does_not_skip_recovering_from_skeleton_or_error():
    """The render-skip must never fire when recovering from a skeleton or the
    'Could not load conversations' banner — both are rendered OUTSIDE the
    signature path, so an identical-signature match (empty/same-shaped profile,
    or a transient fetch failure healing with identical rows) would leave the
    skeleton/error DOM on screen instead of the real list. (Codex #5467 re-gate)
    """
    start = SESSIONS_JS.find("function _applySessionListPayload(")
    assert start != -1
    body = SESSIONS_JS[start:start + 12000]
    assert "const _hadSessionListSkeleton = _sessionListSkeletonActive;" in body
    assert "const _hadSessionListLoadError = !!_sessionListLoadError;" in body
    assert "const _mustForceRender = _hadSessionListSkeleton || _hadSessionListLoadError;" in body
    assert "!_mustForceRender" in body, "the force flag must gate the identical-signature skip"
    # captured BEFORE the respective clears
    assert body.index("const _hadSessionListSkeleton") < body.index("_sessionListSkeletonActive = false;")
    assert body.index("const _hadSessionListLoadError") < body.index("_sessionListLoadError = null;")


def test_pwa_pull_to_refresh_refreshes_session_list_not_page_when_available():
    assert "window.refreshSessionList('pull', {force:true, refreshActive:true})" in UI_JS
    assert "Promise.resolve(window.refreshSessionList('pull', {force:true, refreshActive:true})).catch(()=>{}).finally(_ptrReset)" in UI_JS


def test_force_reload_clears_stale_blocking_prompts_immediately():
    """External refresh should not leave old approval/clarify modals blocking the composer.

    hideApprovalCard() and hideClarifyCard() defer hiding for their minimum-visible
    timers unless force=true. That is correct for active streams, but when a
    same-session external state.db update triggers loadSession(..., {force:true}),
    the session has completed elsewhere and stale prompts should be removed now.
    """
    assert "hideApprovalCard(forceReload)" in SESSIONS_JS
    assert "hideClarifyCard(forceReload, forceReload?'external-refresh':'dismissed')" in SESSIONS_JS


def test_same_session_force_reload_preserves_non_empty_composer_input():
    """A slow same-session refresh must not roll back text typed meanwhile.

    The active-session refresh path can finish seconds after it started. If the
    user kept typing, restoring the server draft at the end of that load would
    replace newer local input with an older debounced draft.
    """
    assert "function _restoreComposerDraft(draft, targetSid, opts={})" in SESSIONS_JS
    assert "const preserveActiveInput = !!(opts && opts.preserveActiveInput);" in SESSIONS_JS
    assert "if (preserveActiveInput && current && current !== text) return;" in SESSIONS_JS
    assert "_restoreComposerDraft(_draft, sid, {preserveActiveInput:!!opts.preserveActiveInput || (currentSid===sid&&forceReload)});" in SESSIONS_JS


def test_same_session_force_reload_keeps_loaded_transcript_width_hint():
    """Same-session force refresh must not collapse a long transcript to the tail."""
    assert "let _sameSessionForceReloadHint = null;" in SESSIONS_JS
    assert "function _captureSameSessionForceReloadHint(sid)" in SESSIONS_JS
    assert "if(!sid || _sameSessionForceReloadHint.session_id===sid) _sameSessionForceReloadHint=null;" in SESSIONS_JS
    assert "loaded_renderable_count:loadedRenderableCount" in SESSIONS_JS
    assert "message_count:knownMessageCount" in SESSIONS_JS
    assert "truncated:!!_messagesTruncated" in SESSIONS_JS
    assert "function _messageReloadLimitForSession(sid)" in SESSIONS_JS
    assert "if(!hint.truncated) return null;" in SESSIONS_JS
    assert "const appendedMessageCount=Math.max(0,currentMessageCount-previousMessageCount);" in SESSIONS_JS
    assert "return Math.max(_INITIAL_MSG_LIMIT,loadedRenderableCount,loadedMessageCount+appendedMessageCount);" in SESSIONS_JS
    assert "const reloadLimit = _messageReloadLimitForSession(sid);" in SESSIONS_JS
    # The width hint is applied only when it stays within the server msg_limit
    # ceiling; an over-ceiling hint would be clamped by the backend and could
    # silently shrink an already-loaded transcript, so it falls back to the bare
    # full-transcript path (#6152/#6154 ceiling; Codex gate silent row-loss fix).
    # #6177: the ceiling is now read from /api/session metadata into _msgLimitMax
    # (module-scope let, default _MSG_LIMIT_MAX) instead of the mirrored const.
    assert "const boundedReloadLimit = (reloadLimit && reloadLimit <= _msgLimitMax) ? reloadLimit : null;" in SESSIONS_JS
    assert "const reloadLimitParam = boundedReloadLimit ? `&msg_limit=${boundedReloadLimit}` : '';" in SESSIONS_JS
    assert "if (_ownsLoad()) _clearSameSessionForceReloadHint(sid);" in SESSIONS_JS

    load_start = SESSIONS_JS.index("async function loadSession(sid)")
    load_end = SESSIONS_JS.index("// ── Handoff hint logic", load_start)
    load_body = SESSIONS_JS[load_start:load_end]
    capture_pos = load_body.index("if (sameSessionForceReload) _captureSameSessionForceReloadHint(sid);")
    clear_pos = load_body.index("else _clearSameSessionForceReloadHint();", capture_pos)
    reset_pos = load_body.index("S.messages = [];", clear_pos)
    assert capture_pos < clear_pos < reset_pos
    assert "const sameSessionForceReload = forceReload && currentSid===sid;" in load_body
    assert "renderMessages(sameSessionForceReload?{preserveScroll:true}:undefined)" in load_body


def test_same_width_force_reload_invalidates_visible_message_cache():
    """Replacing a transcript with the same length must still refresh cached rows."""
    clear_start = UI_JS.index("function clearVisibleMessageRowCache()")
    clear_end = UI_JS.index("function _resetMessageRenderWindow", clear_start)
    clear_body = UI_JS[clear_start:clear_end]
    assert "_visWithIdxCache=null;" in clear_body
    assert "_visWithIdxCacheLen=0;" in clear_body
    assert "clearVisibleMessageRowCache();" in UI_JS[UI_JS.index("function clearMessageRenderCache()") :]

    ensure_start = SESSIONS_JS.index("async function _ensureMessagesLoaded(sid")
    ensure_end = SESSIONS_JS.index("function _messageComparableText", ensure_start)
    ensure_body = SESSIONS_JS[ensure_start:ensure_end]
    invalidate_pos = ensure_body.index("if(typeof clearVisibleMessageRowCache==='function') clearVisibleMessageRowCache();")
    replace_pos = ensure_body.index("S.messages = msgs;")
    assert invalidate_pos < replace_pos
