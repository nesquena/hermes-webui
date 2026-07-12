"""Tests for #1103 — reasoning chip visible on page load."""
import re


def test_boot_calls_fetchReasoningChip():
    """boot.js must call fetchReasoningChip() during boot initialization."""
    with open("static/boot.js") as f:
        src = f.read()
    assert "fetchReasoningChip" in src, "fetchReasoningChip must be referenced in boot.js"
    # Must be called (not just defined)
    assert re.search(r"fetchReasoningChip\s*\(\s*\)", src), \
        "fetchReasoningChip() must be called in boot.js"


def test_boot_call_before_session_load():
    """fetchReasoningChip() should be called before session load in boot sequence."""
    with open("static/boot.js") as f:
        src = f.read()
    boot_marker = "await loadSession(saved, {preserveActiveInput:true});"
    boot_pos = src.index(boot_marker)
    fetch_pos = src.index("fetchReasoningChip()", src.index("_profileQueryIntentFromLocation"))
    assert fetch_pos < boot_pos, \
        "fetchReasoningChip() should be called before saved session load in boot.js"


def test_boot_call_has_typeof_guard():
    """fetchReasoningChip() call in boot.js should have a typeof guard."""
    with open("static/boot.js") as f:
        src = f.read()
    assert "typeof fetchReasoningChip" in src, \
        "fetchReasoningChip call should be guarded with typeof check"


def test_reasoning_chip_html_starts_hidden():
    """The reasoning wrap must start hidden (display:none) in HTML."""
    with open("static/index.html") as f:
        src = f.read()
    assert 'id="composerReasoningWrap"' in src, "composerReasoningWrap must exist in HTML"
    # Extract the element and check for display:none
    m = re.search(
        r'<div[^>]*id="composerReasoningWrap"[^>]*style="display:none"[^>]*>',
        src
    )
    assert m, "composerReasoningWrap must start with style='display:none'"
    assert 'data-effort="max"' in src, (
        "composer reasoning dropdown must include Max option"
    )


def test_ui_js_passes_model_context_to_reasoning_api():
    with open("static/ui.js") as f:
        src = f.read()
    assert "_reasoningEffortQuery" in src, (
        "ui.js must pass the active session model/provider to /api/reasoning"
    )
    # The /api/reasoning GET must carry the model/provider query. #4650 captures
    # the query into a local `key` first (for the in-flight storm + stale-success
    # guards), so accept either the inlined form or the captured-key form — both
    # pass _reasoningEffortQuery()'s output to the endpoint.
    fetch_match = re.search(r"function fetchReasoningChip\([^)]*\)\{(.+?)\n\}", src, re.DOTALL)
    assert fetch_match, "fetchReasoningChip function must exist"
    fetch_body = fetch_match.group(1)
    inlined = "api('/api/reasoning'+_reasoningEffortQuery())" in src
    captured = (
        "_reasoningEffortQuery()" in fetch_body
        and "api('/api/reasoning'+key)" in fetch_body
    )
    assert inlined or captured, (
        "fetchReasoningChip must pass _reasoningEffortQuery() (model/provider context) "
        "to GET /api/reasoning, either inlined or via a captured key"
    )


def test_fetchReasoningChip_calls_apply():
    """fetchReasoningChip must call _applyReasoningChip on success."""
    with open("static/ui.js") as f:
        src = f.read()
    # Find fetchReasoningChip function
    func_match = re.search(r"function fetchReasoningChip\([^)]*\)\{(.+?)\}", src, re.DOTALL)
    assert func_match, "fetchReasoningChip function must exist"
    func_body = func_match.group(1)
    assert "_applyReasoningChip" in func_body, \
        "fetchReasoningChip must call _applyReasoningChip"


def test_syncReasoningChip_called_on_session_load():
    """syncReasoningChip must be called when a session is rendered."""
    with open("static/ui.js") as f:
        src = f.read()
    # Should be called in the session render flow
    assert "syncReasoningChip()" in src, \
        "syncReasoningChip() must be called somewhere in ui.js"


def test_syncReasoningChip_called_on_model_change():
    """Model picker changes must refresh reasoning chip with or without a session."""
    with open("static/boot.js") as f:
        boot_src = f.read()
    marker = "$('modelSelect').onchange=async()=>{"
    start = boot_src.index(marker)
    tail = boot_src[start:]
    assert "syncReasoningChip()" in tail, \
        "syncReasoningChip() must be called when modelSelect changes"
    no_session = tail[tail.index("if(!S.session){"):tail.index("if(typeof _rememberPendingSessionModel")]
    assert "syncReasoningChip()" in no_session, \
        "syncReasoningChip() must also run for pre-session picker changes"
    model_assign = tail.index("S.session.model=modelState.model")
    sync_call = tail.index("syncReasoningChip()", model_assign)
    assert model_assign < sync_call, \
        "syncReasoningChip() must run after S.session.model is updated"


def test_selectModelFromDropdown_defers_reasoning_sync_to_onchange():
    """Custom model dropdown must not fetch reasoning before session state updates."""
    with open("static/ui.js") as f:
        src = f.read()
    match = re.search(
        r"async function selectModelFromDropdown\(value(?:,\s*preferredProviderId)?\)\{(.*?)\n\}",
        src,
        re.DOTALL,
    )
    assert match, "selectModelFromDropdown must exist"
    body = match.group(1)
    assert "fetchReasoningChip()" not in body, \
        "selectModelFromDropdown must not call fetchReasoningChip before onchange"
    assert "sel.onchange" in body, \
        "selectModelFromDropdown must still trigger modelSelect.onchange"
    assert "_ensureModelOptionInDropdown" in body, \
        "selectModelFromDropdown must resolve provider-specific options"
    assert "preferredProviderId" in body, \
        "selectModelFromDropdown must accept an explicit provider id"


def test_model_dropdown_passes_provider_to_select():
    """Composer model rows must pass provider context through the shared picker callback."""
    with open("static/ui.js") as f:
        src = f.read()
    assert "selectModelFromDropdown(value,provider)" in src, (
        "composer default picker callback must still route to selectModelFromDropdown"
    )
    assert re.search(
        r"selectFromDropdown\(m\.value,\s*m\.providerId",
        src,
    ), "model dropdown rows must pass providerId through the shared callback"
