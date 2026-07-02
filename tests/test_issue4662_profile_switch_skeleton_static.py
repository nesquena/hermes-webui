"""Static-source assertions for the #4662 profile-switch loading skeletons.

These don't execute JS — they assert the source wiring so the behaviour can't
silently regress:

  * switchToProfile() shows both skeletons up front (clears stale content),
    parallelizes the independent list+workspace refreshes, and restores real
    content on failure so a skeleton never strands.
  * renderSessionListFromCache() clears the skeleton-active flag on real render.
  * style.css defines the skeleton classes, the sheen + fade keyframes, the
    reduced-motion fallback, and dark-mode tokens.
"""
import re
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent.resolve()
PANELS = (REPO_ROOT / "static" / "panels.js").read_text(encoding="utf-8")
SESSIONS = (REPO_ROOT / "static" / "sessions.js").read_text(encoding="utf-8")
WORKSPACE = (REPO_ROOT / "static" / "workspace.js").read_text(encoding="utf-8")
CSS = (REPO_ROOT / "static" / "style.css").read_text(encoding="utf-8")


def _switch_body() -> str:
    start = PANELS.index("async function switchToProfile(")
    # grab a generous slice (the function is long); next top-level function after it
    end = PANELS.index("function openProfileCreate(", start)
    return PANELS[start:end]


def _show_session_skeleton_call_idx(body: str) -> int:
    match = re.search(
        r"(?:^|\n)\s*(?:if\s*\([^\n;]*showSessionListSkeleton[^\n;]*\)\s*)?showSessionListSkeleton\s*\(",
        body,
    )
    assert match, "switchToProfile() must call showSessionListSkeleton(...)"
    return match.start()


class TestSwitchWiring:
    def test_shows_session_skeleton_up_front(self):
        body = _switch_body()
        skeleton_idx = _show_session_skeleton_call_idx(body)
        # ...and before the awaited /api/profile/switch POST, so stale rows clear immediately
        assert skeleton_idx < body.index("await api('/api/profile/switch'")

    def test_shows_workspace_skeleton_when_panel_open(self):
        body = _switch_body()
        assert "showWorkspaceTreeSkeleton()" in body
        assert "_workspaceVisibleAtStart" in body

    def test_workspace_refresh_after_switch_guard(self):
        # The workspace tree refresh (loadDir) must run AFTER the stale-switch
        # generation guard — loadDir paints the tree with only a session-id
        # check, and empty-session switches reuse the session id, so starting it
        # before the guard could let an older switch paint over a newer one
        # (Codex gate #4662). The skeletons shown up front already provide the
        # immediate cross-surface feedback.
        body = _switch_body()
        guard = "if (_switchGen !== _profileSwitchGeneration) return;"
        # In the non-sessionInProgress branch, the loadDir('.') call must come
        # after an occurrence of the guard.
        dir_idx = body.index("const dirLoad = loadDir('.');")
        guard_before = body.rfind(guard, 0, dir_idx)
        assert guard_before != -1, "loadDir('.') must be preceded by the switch-generation guard"

    def test_restores_real_content_on_failure(self):
        body = _switch_body()
        catch = body[body.index("} catch (e) {"):]
        # #4671: failure-restore clears the skeleton flag (asserted in
        # test_switch_failure_restore_clears_skeleton) then re-renders the real list.
        assert "renderSessionListFromCache()" in catch, "failed switch must restore real list"

    def test_failure_path_clears_workspace_skeleton_when_no_workspace(self):
        # The success path clears a stranded workspace skeleton when the profile
        # has no bound workspace; the failure path must do the same — otherwise a
        # switch failure while the workspace panel is open and the (still-current)
        # session has no workspace strands the up-front skeleton forever (#4662).
        body = _switch_body()
        catch = body[body.index("} catch (e) {"):]
        assert "clearWorkspaceTreeSkeleton()" in catch, (
            "failed switch must clear the workspace skeleton when there's no workspace to restore"
        )

    def test_noop_self_switch_early_returns(self):
        # Opus gate #4662: a switch to the already-active profile must bail before
        # showing a skeleton (activateCurrentProfile() doesn't pre-check), else it
        # flashes skeleton→restore.
        body = _switch_body()
        head = body[: _show_session_skeleton_call_idx(body)]
        assert "name === S.activeProfile" in head, "missing no-op self-switch early-return"
        assert "return;" in head

    def test_dismisses_rename_and_menu_before_skeleton(self):
        # Opus gate #4662: renderSessionListFromCache() early-returns while
        # _renamingSid / _sessionActionMenu is set — which would strand the
        # skeleton. switchToProfile must dismiss both before showing it.
        body = _switch_body()
        pre = body[: _show_session_skeleton_call_idx(body)]
        assert "_renamingSid = null" in pre, "must clear inline-rename state before skeleton"
        assert "closeSessionActionMenu()" in pre, "must close row action menu before skeleton"

    def test_clears_workspace_skeleton_when_no_workspace(self):
        # Opus gate #4662 (blocker): if the new profile has no bound workspace the
        # real loadDir is skipped, so the up-front workspace skeleton must be
        # explicitly cleared or it strands forever.
        body = _switch_body()
        assert body.count("clearWorkspaceTreeSkeleton()") >= 2, (
            "both switch branches must clear a stranded workspace skeleton"
        )

    def test_switch_post_suppresses_generic_timeout_toast(self):
        # @rodboev review #4662: the switch POST must pass timeoutToast:false so
        # api()'s generic "Request timed out" toast can't fire for a superseded or
        # transient-but-eventually-successful switch. Failures surface only through
        # the generation-guarded catch handler, which is the single source of truth.
        body = _switch_body()
        post_idx = body.index("await api('/api/profile/switch'")
        # The same api(...) call expression that targets /api/profile/switch must
        # carry timeoutToast: false. Scope to a small window around the call.
        window = body[post_idx: post_idx + 300]
        assert "timeoutToast: false" in window or "timeoutToast:false" in window.replace(" ", ""), (
            "the /api/profile/switch POST must suppress the generic timeout toast"
        )

    def test_in_progress_branch_reguards_after_list_render(self):
        # @rodboev/greptile review #4662: the sessionInProgress branch awaits
        # renderSessionList(); without a generation re-check after that await a
        # rapid second switch could have its workspace skeleton cleared / a stale
        # toast popped by the slower earlier switch. Mirror the no-messages guard.
        body = _switch_body()
        guard = "if (_switchGen !== _profileSwitchGeneration) return;"
        # Inside the sessionInProgress branch: locate its "await renderSessionList()"
        # then the profile_switched_new_conversation toast; a guard must sit between.
        new_toast_idx = body.index("profile_switched_new_conversation")
        list_render_before = body.rfind("await renderSessionList();", 0, new_toast_idx)
        assert list_render_before != -1
        guard_between = body.find(guard, list_render_before, new_toast_idx)
        assert guard_between != -1, (
            "the in-progress switch branch must re-check the switch generation "
            "after its awaited renderSessionList()"
        )


class TestSessionsWiring:
    def test_skeleton_flag_cleared_on_real_render(self):
        # The authoritative clear now happens in _applySessionListPayload()
        # immediately before the real render, while renderSessionListFromCache()
        # keeps the guard that blocks stale cached rows during the skeleton phase.
        render_idx = SESSIONS.index("function renderSessionListFromCache(")
        render_body = SESSIONS[render_idx: render_idx + 1200]
        apply_idx = SESSIONS.index("function _applySessionListPayload(")
        # Slice to the END of _applySessionListPayload()'s body (the next top-level
        # function), not a fixed char window — #4748 added lines inside this function
        # that pushed `_sessionListSkeletonActive = false;` past a fixed 4000-char
        # window. Anchoring on the next function def keeps the assertion robust.
        apply_end = SESSIONS.index("function _mergeRenderSessionListOptions(", apply_idx)
        apply_body = SESSIONS[apply_idx: apply_end]

        assert "if(_sessionListSkeletonActive)return;" in render_body.replace(" ", "")
        assert "_sessionListSkeletonActive = false;" in apply_body
        assert "renderSessionListFromCache();" in apply_body

    def test_builder_defines_groups_and_function(self):
        assert "const _SESSION_SKELETON_GROUPS" in SESSIONS
        assert "function showSessionListSkeleton(" in SESSIONS

    def test_skeleton_tears_down_virtual_scroll_state(self):
        # #4662 Codex gate: on long virtualized sidebars, leaving the
        # data-session-virtual-* window state + a queued scroll RAF active would
        # let _scheduleSessionVirtualizedRender() repaint the PREVIOUS profile's
        # cached rows over the skeleton. The builder must clear that state.
        idx = SESSIONS.index("function showSessionListSkeleton(")
        body = SESSIONS[idx: idx + 2000]
        assert "cancelAnimationFrame(_sessionVirtualScrollRaf)" in body
        assert "delete list.dataset.sessionVirtualTotal" in body
        assert "delete list.dataset.sessionVirtualStart" in body
        assert "delete list.dataset.sessionVirtualEnd" in body

    def test_virtual_render_guarded_by_skeleton_flag(self):
        # The virtual-scroll scheduler must bail while a skeleton is up.
        idx = SESSIONS.index("function _scheduleSessionVirtualizedRender(")
        body = SESSIONS[idx: idx + 600]
        assert "if(_sessionListSkeletonActive) return;" in body


class TestWorkspaceWiring:
    def test_builder_defined(self):
        assert "const _WS_SKELETON_ROWS" in WORKSPACE
        assert "function showWorkspaceTreeSkeleton(" in WORKSPACE

    def test_skeleton_clear_helper_defined(self):
        # The strand-clear helper must exist and only empty #fileTree when it
        # still holds a skeleton (so it can't clobber a real render).
        assert "function clearWorkspaceTreeSkeleton(" in WORKSPACE
        idx = WORKSPACE.index("function clearWorkspaceTreeSkeleton(")
        body = WORKSPACE[idx: idx + 400]
        assert ".skeleton-tree" in body, "clear helper must check for a skeleton before emptying"


class TestSkeletonCss:
    def test_core_classes_present(self):
        for cls in (".skeleton-list", ".skeleton-row", ".skeleton-bar",
                    ".skeleton-group-label", ".skeleton-tree", ".skeleton-tree-row",
                    ".skeleton-glyph"):
            assert cls in CSS, f"missing skeleton CSS class {cls}"

    def test_sheen_and_fade_keyframes(self):
        assert "@keyframes skeletonSheen" in CSS
        assert "@keyframes skeletonFadeIn" in CSS
        assert "animation:skeletonSheen" in CSS.replace(" ", "")

    def test_group_label_fade_settles_at_dim_opacity(self):
        # @rodboev/greptile review #4662: the shared skeletonFadeIn ends at
        # opacity:1 with fill-mode:both, which would override the group label's
        # intended resting opacity:.5 for the whole time the skeleton is up. Labels
        # must use a dedicated keyframe that ends at .5.
        compact = CSS.replace(" ", "")
        assert "@keyframesskeletonFadeInDim{from{opacity:0;}to{opacity:.5;}}" in compact, (
            "group labels need a dim fade-in that settles at opacity:.5"
        )
        assert "skeleton-group-label{animation:skeletonFadeInDim" in compact, (
            "group labels must use the dim fade-in, not the full-brightness skeletonFadeIn"
        )

    def test_reduced_motion_disables_animation(self):
        # There must be a prefers-reduced-motion block that turns the skeleton
        # sheen animation off (accessibility contract).
        compact = CSS.replace(" ", "")
        assert "prefers-reduced-motion" in compact
        # The reduced-motion rule names .skeleton-bar and sets animation:none.
        rm_blocks = [b for b in compact.split("@media(prefers-reduced-motion:reduce){")
                     if ".skeleton-bar" in b[:400]]
        assert rm_blocks, "no reduced-motion block scoping .skeleton-bar"
        assert "animation:none" in rm_blocks[0][:400], "reduced-motion must disable the sheen"

    def test_theme_tokens_defined_for_light_and_dark(self):
        compact = CSS.replace(" ", "")
        assert "--skeleton-base:" in compact
        assert "--skeleton-sheen:" in compact
        assert ":root.dark{--skeleton-base:" in compact


def _iter_indices(haystack: str, needle: str):
    """Yield every start index of needle in haystack."""
    i = haystack.find(needle)
    while i != -1:
        yield i
        i = haystack.find(needle, i + 1)


class TestSwitchRaceGuards:
    """#4671: the two render races the Codex gate caught on the rebased stage."""

    def test_session_skeleton_guard_blocks_non_switch_renders(self):
        # Race-2: while the profile-switch skeleton is up, renderSessionListFromCache()
        # must early-return, so a gateway-poll/SSE/timer/cache render can't repaint the
        # PREVIOUS profile's cached _allSessions over the skeleton in the window before
        # /api/sessions resolves.
        idx = SESSIONS.index("function renderSessionListFromCache(")
        head = SESSIONS[idx: idx + 800]
        # signature unchanged (many static tests anchor on the empty-paren marker)
        assert "function renderSessionListFromCache(){" in SESSIONS, (
            "renderSessionListFromCache signature must stay () — tests anchor on it"
        )
        assert "if(_sessionListSkeletonActive) return;" in head, (
            "renderSessionListFromCache must bail while the profile-switch skeleton is up"
        )

    def test_skeleton_flag_cleared_by_authoritative_render_when_data_is_fresh(self):
        # Race-2 (the other half): the skeleton flag must be cleared ONLY when fresh data
        # is in hand, so the bail above can't strand the skeleton. _applySessionListPayload
        # runs on the resolved /api/sessions payload (superseded responses already discarded
        # by the generation guard), so it clears the flag right before painting.
        apply_idx = SESSIONS.index("function _applySessionListPayload(")
        apply_body = SESSIONS[apply_idx: SESSIONS.index("\nfunction _mergeRenderSessionListOptions(", apply_idx)]
        clear_pos = apply_body.find("_sessionListSkeletonActive = false;")
        paint_pos = apply_body.find("renderSessionListFromCache();")
        assert clear_pos != -1, "_applySessionListPayload must clear the skeleton flag"
        assert paint_pos != -1 and clear_pos < paint_pos, (
            "the skeleton flag must be cleared right before the authoritative paint"
        )
        # And the fetch-failure path must clear it too (else a failed switch strands it):
        refresh = SESSIONS[SESSIONS.index("async function _runRenderSessionListRefresh("):]
        refresh = refresh[: refresh.index("\nasync function _drainRenderSessionListQueue(")]
        assert refresh.count("_sessionListSkeletonActive = false;") >= 1, (
            "the /api/sessions failure path must clear the skeleton flag so it can't strand"
        )

    def test_switch_failure_restore_clears_skeleton(self):
        # The switchToProfile catch (failure on the still-current previous profile) must
        # clear the skeleton flag before restoring the real list, or the skeleton strands.
        body = _switch_body()
        catch = body[body.index("} catch (e) {"):]
        clear_pos = catch.find("_sessionListSkeletonActive = false;")
        restore_pos = catch.find("renderSessionListFromCache()")
        assert clear_pos != -1, "switch failure path must clear the skeleton flag"
        assert restore_pos != -1 and clear_pos < restore_pos, (
            "skeleton flag must be cleared before the failure-restore render"
        )

    def test_switch_invalidates_inflight_renders_before_skeleton(self):
        # Race-2 (in-flight variant, Codex re-gate 2): a renderSessionList() already in
        # flight BEFORE the switch (old profile's /api/sessions) would resolve, pass the
        # generation guard (its _gen still current — the switch hasn't bumped it yet),
        # clear the skeleton flag, and paint stale rows. switchToProfile must invalidate
        # in-flight/queued renders at switch start, BEFORE showing the skeleton.
        assert "function _invalidateSessionListRenders(){" in SESSIONS, (
            "must have a helper that invalidates in-flight/queued session-list renders"
        )
        inv = SESSIONS[SESSIONS.index("function _invalidateSessionListRenders(){"):]
        inv = inv[: inv.index("\n}")]
        assert "_renderSessionListGen++" in inv, "invalidation must bump the render generation"
        assert "_pendingSessionListPayload = null" in inv, "invalidation must drop a deferred payload"
        assert "_renderSessionListQueuedRequest = null" in inv, "invalidation must drop the queued request"
        # ...and the switch must call it BEFORE showSessionListSkeleton(). Anchor on the
        # actual call guard (not the bare name, which also appears in nearby comments).
        body = _switch_body()
        inv_pos = body.find("_invalidateSessionListRenders === 'function')")
        skel_pos = body.find("showSessionListSkeleton === 'function')")
        assert inv_pos != -1, "switchToProfile must invalidate in-flight renders at switch start"
        assert skel_pos != -1 and inv_pos < skel_pos, (
            "the in-flight invalidation must run BEFORE the skeleton is shown"
        )

    def test_switch_embargoes_session_list_renders_during_switch_window(self):
        # Race-2 (mid-switch variant, Codex re-gate 3): a renderSessionList() that STARTS
        # after the skeleton shows but before /api/profile/switch sets the new-profile
        # cookie fetches the OLD profile's rows and would clobber the skeleton. An embargo
        # (set before the skeleton, lifted right before the switch-owned render and in the
        # finally) makes _runRenderSessionListRefresh drop ALL payloads during the window.
        assert "function _setProfileSwitchListEmbargo(" in SESSIONS, "missing embargo setter"
        # _runRenderSessionListRefresh must drop payloads while embargoed, after the gen guard
        refresh = SESSIONS[SESSIONS.index("async function _runRenderSessionListRefresh("):]
        refresh = refresh[: refresh.index("\nasync function _drainRenderSessionListQueue(")]
        assert "if (_profileSwitchListEmbargo) return;" in refresh, (
            "_runRenderSessionListRefresh must drop payloads while the profile-switch embargo is on"
        )
        body = _switch_body()
        # embargo set before the skeleton, and lifted (false) before the switch-owned render(s)
        set_pos = body.find("_setProfileSwitchListEmbargo === 'function') _setProfileSwitchListEmbargo(true)")
        skel_pos = body.find("showSessionListSkeleton === 'function')")
        assert set_pos != -1 and set_pos < skel_pos, "embargo must be set before the skeleton"
        assert body.count("_setProfileSwitchListEmbargo(false)") >= 2, (
            "embargo must be lifted before the switch-owned render(s) / on failure"
        )
        # and guaranteed-lifted in a finally so it can't freeze the sidebar on an early-return/throw
        assert "finally {" in body and "_setProfileSwitchListEmbargo(false)" in body[body.index("finally {"):], (
            "embargo must be lifted in the switch's finally as a safety net"
        )

    def test_workspace_tree_generation_token_guards_loaddir(self):
        # Race-1 (CORE): an empty-session profile switch reuses the same session_id, so
        # loadDir()'s session_id guard alone can't reject a stale pre-switch /api/list.
        # A _wsTreeGen generation token, bumped UNCONDITIONALLY at switch start (even when
        # the workspace panel is closed, since loadDir('.') still runs), gates loadDir().
        assert "_wsTreeGen" in WORKSPACE, "missing workspace-tree generation token"
        assert "function bumpWorkspaceTreeGen(" in WORKSPACE, "missing bumpWorkspaceTreeGen helper"
        # switchToProfile bumps it unconditionally (NOT only inside the panel-gated skeleton call)
        body = _switch_body()
        assert "bumpWorkspaceTreeGen()" in body, (
            "switchToProfile must bump the workspace-tree generation at switch start"
        )
        bump_idx = body.index("bumpWorkspaceTreeGen()")
        wsskel_idx = body.index("showWorkspaceTreeSkeleton()")
        assert bump_idx < wsskel_idx, (
            "the unconditional bump must precede the panel-gated showWorkspaceTreeSkeleton"
        )
        # loadDir captures + re-checks the generation after BOTH awaited /api/list points
        ld = WORKSPACE[WORKSPACE.index("async function loadDir("):]
        ld = ld[: ld.index("\nfunction refreshWorkspacePanel(")]
        assert "const treeGen=_wsTreeGen" in ld, "loadDir must capture the tree generation at call time"
        assert ld.count("treeGen!==_wsTreeGen") >= 2, (
            "loadDir must re-check the tree generation after BOTH awaited /api/list points "
            "(root render + expanded-dirs prefetch) and discard stale renders"
        )

