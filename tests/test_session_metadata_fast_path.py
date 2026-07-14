import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_messages_zero_skips_effective_model_resolution():
    src = (ROOT / "api" / "routes.py").read_text(encoding="utf-8")

    assert re.search(
        r"effective_model\s*=\s*\(\s*"
        r"_resolve_effective_session_model_for_display\(s\)\s*"
        r"if resolve_model\s*else None\s*\)",
        src,
    ), "messages=0 metadata requests must not resolve the model catalog"
    assert 'resolve_model_default = "1" if load_messages else "0"' in src


def test_full_message_load_updates_viewed_count_after_metadata_fast_path():
    src = (ROOT / "static" / "sessions.js").read_text(encoding="utf-8")
    compact = re.sub(r"\s+", "", src)

    # The metadata-arrival viewed-count update now flows through
    # _acknowledgeSessionVisit(), which calls _setSessionViewedCount() internally
    # (and additionally syncs the polling snapshot + repaints so visiting a
    # session reliably clears a stale sidebar unread dot) (#4946).
    assert (
        "_acknowledgeSessionVisit(S.session.session_id,Number(data.session.message_count||0),"
        in compact
    )
    assert "_setSessionViewedCount(sid, Number(S.session.message_count || msgs.length));" in src


def test_lazy_message_load_skips_model_resolution():
    src = (ROOT / "static" / "sessions.js").read_text(encoding="utf-8")

    assert "messages=1&resolve_model=0" in src


def test_session_switch_defers_model_resolution_without_blocking():
    src = (ROOT / "static" / "sessions.js").read_text(encoding="utf-8")
    ui = (ROOT / "static" / "ui.js").read_text(encoding="utf-8")

    assert "messages=0&resolve_model=0" in src
    assert "function _resolveSessionModelForDisplaySoon" in src
    assert "messages=0&resolve_model=1" in src
    assert "_modelResolutionDeferred=true" in src
    assert "deferModelCorrection" in ui
    assert "if(fallback&&!deferModelCorrection)" in ui


def test_deferred_model_resolution_refreshes_context_metadata():
    src = (ROOT / "static" / "sessions.js").read_text(encoding="utf-8")
    start = src.index("function _resolveSessionModelForDisplaySoon")
    end = src.index("const _INITIAL_MSG_LIMIT", start)
    block = src[start:end]

    assert "S.session.context_length" in block, (
        "deferred model resolution must also hydrate context_length so a "
        "resumed high-context session does not keep the old model's limit"
    )
    assert "S.session.threshold_tokens" in block
    assert "_syncCtxIndicator" in block
    compact = block.replace(" ", "")
    assert "constresolvedContextLength=data.session.context_length||S.session.context_length||0;" in compact
    assert "context_length:resolvedContextLength||u.context_length||0" in compact


def test_boot_does_not_block_session_restore_on_model_catalog():
    src = (ROOT / "static" / "boot.js").read_text(encoding="utf-8")

    assert "if(s.default_model){" in src
    assert "window._defaultModel=s.default_model;" in src
    assert "const _hydrateModelDropdown=({redirectIfUnauth=null}={})=>populateModelDropdown({" in src
    assert "window._modelDropdownReady=null;" in src
    assert "window._startBootModelDropdown=_startBootModelDropdown;" in src
    assert "window._ensureModelDropdownReady=_startModelDropdown;" in src
    assert "await populateModelDropdown()" not in src


def test_boot_primes_model_catalog_without_awaiting_it():
    """The boot-time prime must NOT await the model-catalog hydration before
    rendering the session list. A later awaited hydration inside the saved-
    session restore path at ``if(S.session) await _startBootModelDropdown();``
    is intentional — that one re-applies the saved session's model after the
    live catalog hydrates so the chip never shows a stale static default
    (see comment in static/boot.js next to the saved-session restore).
    """
    src = (ROOT / "static" / "boot.js").read_text(encoding="utf-8")

    ensure_pos = src.index("window._ensureModelDropdownReady=_startModelDropdown;")
    prime_pos = src.index("Promise.resolve(_startBootModelDropdown()).catch(()=>{});", ensure_pos)
    session_restore_pos = src.index("await renderSessionList();", prime_pos)

    assert ensure_pos < prime_pos < session_restore_pos

    # No await on the boot-prime path itself: between ensure_pos and the first
    # session_restore await, the dropdown is fired-and-forgotten.
    boot_prelude = src[ensure_pos:session_restore_pos]
    assert "await _startBootModelDropdown()" not in boot_prelude, (
        "Boot prelude must not await _startBootModelDropdown — the prime is "
        "fire-and-forget so the sidebar can render before /api/models returns."
    )
    assert "await populateModelDropdown()" not in boot_prelude


def test_failed_boot_model_catalog_prime_is_retryable():
    src = (ROOT / "static" / "boot.js").read_text(encoding="utf-8")
    # #2726 parameterized the call: populateModelDropdown({preferProfileDefaultOnFreshBoot:true})
    # Match either signature shape — empty args (legacy) OR opts arg (post-#2726).
    needle = "const _hydrateModelDropdown=({redirectIfUnauth=null}={})=>populateModelDropdown({"
    start = src.index(needle)
    end = src.index("const _startBootModelDropdown=()=>", start)
    block = src[start:end]

    assert "window._modelDropdownReady=null;" in block
    assert "throw e;" in block


def test_boot_primes_visible_default_model_without_catalog_fetch():
    src = (ROOT / "static" / "boot.js").read_text(encoding="utf-8")
    default_block_start = src.index("if(s.default_model){")
    default_block = src[default_block_start:src.index("window._sessionJumpButtonsEnabled", default_block_start)]

    assert "if(s.default_model_provider) window._activeProvider=s.default_model_provider;" in src
    assert "const existingDefaultOpt=Array.from(sel.options).find(o=>o.value===s.default_model);" in default_block
    assert "existingDefaultOpt.dataset.provider=window._activeProvider;" in default_block
    assert "if(!existingDefaultOpt)" in default_block
    assert "opt.dataset.custom='1'" in default_block
    assert "opt.dataset.provider=window._activeProvider||''" in default_block
    assert "_applyModelToDropdown(s.default_model,sel,window._activeProvider||null)" in default_block
    assert "populateModelDropdown()" not in default_block


def test_settings_exposes_default_model_provider_for_lazy_boot_catalog():
    src = (ROOT / "api" / "config.py").read_text(encoding="utf-8")

    assert 'settings["default_model_provider"]' in src
    assert 'model_cfg = get_config().get("model", {})' in src


def test_boot_renders_session_list_before_workspace_and_onboarding_settle():
    src = (ROOT / "static" / "boot.js").read_text(encoding="utf-8")
    workspace_start = src.index("const _workspaceListReady=loadWorkspaceList();")
    onboarding_start = src.index("const _onboardingReady=_bootSettings.onboarding_completed?Promise.resolve(false):loadOnboardingWizard();")
    render_pos = src.index("await renderSessionList();", onboarding_start)
    workspace_await = src.index("await _workspaceListReady;", render_pos)
    onboarding_await = src.index("await _onboardingReady;", render_pos)

    assert workspace_start < render_pos < workspace_await
    assert onboarding_start < render_pos < onboarding_await
    assert "_bootSettings.onboarding_completed" in src
