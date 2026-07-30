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
    """Session persistence writes are best-effort when the browser quota is full (#2386)."""
    for path in ["static/sessions.js", "static/commands.js", "static/messages.js"]:
        _assert_storage_setitem_guarded(
            _script(path),
            "localStorage.setItem('hermes-webui-session'",
        )


def test_workspace_panel_localstorage_write_ignores_quota_errors():
    """Workspace panel state should not break UI toggles if localStorage throws (#2386)."""
    _assert_storage_setitem_guarded(
        _script("static/boot.js"),
        "localStorage.setItem('hermes-webui-workspace-panel'",
    )
