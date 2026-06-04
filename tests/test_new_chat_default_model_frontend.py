from pathlib import Path

SESSIONS_JS = Path("static/sessions.js").read_text(encoding="utf-8")
MESSAGES_JS = Path("static/messages.js").read_text(encoding="utf-8")
CHANGELOG = Path("CHANGELOG.md").read_text(encoding="utf-8")


def _extract_function(source: str, signature: str) -> str:
    start = source.index(signature)
    # Look for the function body's opening brace, not an object literal inside
    # a default argument such as `options={}`.
    brace = source.index("{\n", start)
    depth = 0
    for idx in range(brace, len(source)):
        ch = source[idx]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return source[start : idx + 1]
    raise AssertionError(f"Function body not closed for {signature}")


def _new_session_function() -> str:
    return _extract_function(SESSIONS_JS, "async function newSession")


def test_new_chat_syncs_model_picker_when_default_provider_changes_but_model_id_matches():
    fn = _new_session_function()
    assert "currentModelState" in fn
    assert "currentProvider" in fn
    assert "sessionProvider" in fn
    assert "sessionProvider !== currentProvider" in fn
    assert "_applyModelToDropdown(S.session.model,modelSel,sessionProvider)" in fn


def test_new_chat_inserts_session_model_when_static_picker_lacks_default():
    fn = _new_session_function()
    assert "sessionModelApplied" in fn
    assert "document.createElement('option')" in fn
    assert "opt.value=S.session.model" in fn
    assert "opt.dataset.provider=sessionProvider||''" in fn
    assert "modelSel.appendChild(opt)" in fn


def test_boot_model_hydration_prefers_active_session_over_persisted_model():
    boot_js = Path("static/boot.js").read_text(encoding="utf-8")
    marker = "const sessionModelState=S.session&&S.session.model"
    assert marker in boot_js
    session_branch = boot_js[boot_js.index(marker) : boot_js.index("if(S.session) syncTopbar();", boot_js.index(marker))]
    assert "_applyModelToDropdown(sessionModelState.model,$('modelSelect'),sessionModelState.model_provider||null)" in session_branch
    assert "savedState" in session_branch
    assert session_branch.index("sessionModelState") < session_branch.index("savedState"), (
        "active session model must be considered before localStorage so stale saved model preferences cannot override new chats"
    )


def test_hard_refresh_hydrates_saved_session_model_before_revealing_model_chip():
    boot_js = Path("static/boot.js").read_text(encoding="utf-8")
    load_marker = "await loadSession(saved);"
    assert load_marker in boot_js
    saved_restore = boot_js[boot_js.index(load_marker) : boot_js.index("await checkInflightOnBoot(saved);return;", boot_js.index(load_marker))]
    assert "await _startBootModelDropdown();" in saved_restore
    assert saved_restore.index("await _startBootModelDropdown();") > saved_restore.index(load_marker)
    assert saved_restore.index("await _startBootModelDropdown();") < saved_restore.index("S._bootReady=true;"), (
        "hard refresh must hydrate/re-apply the active session model before S._bootReady lets syncModelChip display stale static HTML defaults"
    )


def test_hard_refresh_injects_missing_active_session_model_option():
    boot_js = Path("static/boot.js").read_text(encoding="utf-8")
    marker = "if(!applied&&sessionModelState&&typeof _ensureModelOptionInDropdown==='function')"
    assert marker in boot_js
    branch = boot_js[boot_js.index(marker) : boot_js.index("else if(!applied&&!sessionModelState", boot_js.index(marker))]
    assert "_ensureModelOptionInDropdown(sessionModelState.model,$('modelSelect'),sessionModelState.model_provider||null)" in branch


def test_sync_topbar_preserves_missing_session_model_as_dropdown_option():
    ui_js = Path("static/ui.js").read_text(encoding="utf-8")
    assert "function _ensureModelOptionInDropdown" in ui_js
    sync_topbar = _extract_function(ui_js, "function syncTopbar")
    branch_start = sync_topbar.index("const applied=_applyModelToDropdown(currentModel,modelSel,S.session.model_provider||null);")
    session_model_branch = sync_topbar[branch_start:]
    assert "_ensureModelOptionInDropdown(currentModel,modelSel,S.session.model_provider||null)" in session_model_branch
    assert "const fallback=_applySessionModelFallback(modelSel);" in session_model_branch
    assert session_model_branch.index("_ensureModelOptionInDropdown(currentModel,modelSel,S.session.model_provider||null)") < session_model_branch.index("const fallback=_applySessionModelFallback(modelSel);"), (
        "active session models missing from the current catalog must be injected before fallback can select the static/default model"
    )


def test_new_chat_does_not_send_stale_dropdown_model_when_session_has_default_model():
    assert "model:S.session.model||$('modelSelect').value" in MESSAGES_JS
    assert "model_provider:S.session.model_provider||null" in MESSAGES_JS


def test_new_session_posts_picker_model_before_server_default():
    fn = _new_session_function()
    # Behavior contract: the picker model goes into reqBody so /api/session/new
    # uses the user's selection before falling back to the server default
    # (#872). The previous literal-string assertion
    # "reqBody.model_provider=newModelState.model_provider||null" became a
    # change-detector once the #2518 follow-up added a fallback chain; the
    # contract that newModelState.model_provider is the FIRST source of
    # reqBody.model_provider is now verified by substring + ordering.
    assert "reqBody.model=newModelState.model" in fn
    assert "newModelState.model_provider" in fn
    assert "window._activeProvider" in fn, (
        "Cold-start fallback must consult window._activeProvider so "
        "/api/session/new hits the resolve fast path (follow-up from #2518)."
    )
    assert "S.session&&S.session.model_provider" in fn, (
        "Unhydrated-dropdown fallback must consult S.session.model_provider "
        "before sending model_provider=null (follow-up from #2518)."
    )
    provider_assignment = fn[fn.index("reqBody.model_provider="):].split(";", 1)[0]
    # The assignment sources from the explicit picker value first, then the
    # bare-model fallback (_fallbackProvider, wired above from _activeProvider /
    # prev-session), gated so a family-mismatched bare model defers to the
    # server slow path. Anchor on the real `reqBody.model_provider=` assignment
    # (not a comment) and verify the fallback wiring exists in the function body.
    assert "newModelState.model_provider" in provider_assignment
    assert "_fallbackProvider" in provider_assignment
    assert "window._activeProvider" in fn
    assert "S.session&&S.session.model_provider" in fn
    # Ordering in the body: explicit picker value referenced before the
    # _activeProvider fallback, which is referenced before prev-session.
    pos_explicit = fn.index("newModelState.model_provider")
    pos_active = fn.index("window._activeProvider")
    pos_prev = fn.index("S.session&&S.session.model_provider")
    assert pos_explicit < pos_active < pos_prev, (
        "Fallback chain must be: explicit > _activeProvider > prev-session."
    )
    # Family-mismatch guard (Codex #3410-followup finding): a bare known-family
    # model whose family differs from the fallback provider must NOT fast-path.
    assert "_familyMismatch" in fn
    assert "_readPersistedModelState" in fn


def test_model_picker_persists_without_active_session():
    boot_js = Path("static/boot.js").read_text(encoding="utf-8")
    body = boot_js[boot_js.index("$('modelSelect').onchange=async()=>") : boot_js.index("$('msg').addEventListener", boot_js.index("$('modelSelect').onchange=async()=>"))]
    assert "_writePersistedModelState(modelState.model,modelState.model_provider)" in body
    assert "if(!S.session){" in body
    assert body.index("if(!S.session){") < body.index("await api('/api/session/update'")


def test_changelog_mentions_new_chat_default_model_provider_sync():
    unreleased = CHANGELOG.split("## [v0.51.103]", 1)[0]
    assert "New conversations now resync" in unreleased
    assert "default model provider" in unreleased
