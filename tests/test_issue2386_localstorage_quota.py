from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _script(path):
    return (ROOT / path).read_text()


def _assert_storage_setitem_guarded(src, needle):
    matches = [line.strip() for line in src.splitlines() if needle in line]
    assert matches, f"expected at least one {needle} write"
    for line in matches:
        assert line.startswith("try{localStorage.setItem("), (
            f"localStorage quota errors must not escape from {needle} writes: {line}"
        )
        assert "catch(_)" in line or "catch(e)" in line or "catch{}" in line


def test_active_session_localstorage_writes_ignore_quota_errors():
    """Session persistence writes are best-effort when the browser quota is full (#2386).

    The active-session write now goes through the tab-scoped
    _rememberActiveSession() helper (multi-tab isolation), so the quota guard
    lives in ui.js where that helper is defined; the call sites wrap it in
    try/catch exactly as before.
    """
    # ui.js owns the actual write; it must be individually try/catch-guarded.
    ui_js = _script("static/ui.js")
    write_lines = [
        line.strip() for line in ui_js.splitlines()
        if "localStorage.setItem(_activeSessionKey(), sid)" in line
        or "localStorage.setItem(ACTIVE_SESSION_KEY_LEGACY, sid)" in line
    ]
    assert len(write_lines) >= 2, "expected both the per-tab and legacy active-session writes"
    for line in write_lines:
        assert line.startswith("try{"), (
            f"localStorage quota errors must not escape the active-session write: {line}"
        )
        assert "catch(_)" in line or "catch(e)" in line or "catch{}" in line
    # Call sites must still swallow quota errors around the helper call.
    for path in ["static/sessions.js", "static/commands.js", "static/messages.js"]:
        script = _script(path)
        assert "try{_rememberActiveSession(" in script, (
            f"{path}: active-session write must stay wrapped in try/catch"
        )


def test_workspace_panel_localstorage_write_ignores_quota_errors():
    """Workspace panel state should not break UI toggles if localStorage throws (#2386)."""
    _assert_storage_setitem_guarded(
        _script("static/boot.js"),
        "localStorage.setItem('hermes-webui-workspace-panel'",
    )
