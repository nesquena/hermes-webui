"""Issue #5671: workspace switcher and New Chat workspace announcements."""
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
INDEX_HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
PANELS_JS = (ROOT / "static" / "panels.js").read_text(encoding="utf-8")
SESSIONS_JS = (ROOT / "static" / "sessions.js").read_text(encoding="utf-8")
STYLE_CSS = (ROOT / "static" / "style.css").read_text(encoding="utf-8")
I18N_JS = (ROOT / "static" / "i18n.js").read_text(encoding="utf-8")


def _block(source: str, start_marker: str, end_marker: str) -> str:
    start = source.find(start_marker)
    assert start != -1, f"{start_marker!r} not found"
    end = source.find(end_marker, start)
    assert end != -1, f"{end_marker!r} not found after {start_marker!r}"
    return source[start:end]


def test_composer_workspace_switchers_expose_action_and_popup_state():
    assert 'id="composerWorkspaceChip"' in INDEX_HTML
    assert 'id="composerMobileWorkspaceAction"' in INDEX_HTML
    assert 'aria-label="Switch workspace"' in INDEX_HTML
    assert 'aria-haspopup="true"' in INDEX_HTML
    assert 'aria-expanded="false"' in INDEX_HTML
    assert 'aria-controls="composerWsDropdown"' in INDEX_HTML
    assert INDEX_HTML.count('aria-controls="composerWsDropdown"') == 2

    sync = _block(PANELS_JS, "function syncWorkspaceDisplays", "async function loadWorkspaceList")
    assert "const composerExpanded=!!(composerDropdown&&composerDropdown.classList.contains('open'))" in sync
    assert "composerChip.setAttribute('aria-label',hasWorkspace?t('workspace_switcher_aria',label):t('no_workspace'))" in sync
    assert "composerChip.setAttribute('aria-expanded',composerExpanded?'true':'false')" in sync
    assert "composerChip.classList.toggle('active',composerExpanded)" in sync
    assert "mobileAction.setAttribute('aria-label',hasWorkspace?t('workspace_switcher_aria',label):t('no_workspace'))" in sync
    assert "mobileAction.setAttribute('aria-expanded',composerExpanded?'true':'false')" in sync
    assert "mobileAction.classList.toggle('active',composerExpanded)" in sync


def test_composer_workspace_dropdown_keeps_aria_expanded_in_sync():
    toggle = _block(PANELS_JS, "function toggleComposerWsDropdown", "function closeWsDropdown")
    close = _block(PANELS_JS, "function closeWsDropdown", "document.addEventListener('click'")

    assert "chip.setAttribute('aria-expanded','true')" in toggle
    assert "mobileAction.setAttribute('aria-expanded','true')" in toggle
    assert "composerChip.setAttribute('aria-expanded','false')" in close
    assert "mobileAction.setAttribute('aria-expanded','false')" in close


def test_workspace_switcher_i18n_key_exists_in_english_locale():
    assert "workspace_switcher_aria: 'Switch workspace. Current workspace: {0}.'" in I18N_JS
