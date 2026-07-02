"""Static regressions for mobile titlebar session title priority (#4520)."""
import pathlib

ROOT = pathlib.Path(__file__).parent.parent
HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
PANELS_JS = (ROOT / "static" / "panels.js").read_text(encoding="utf-8")


def test_titlebar_title_element_exists():
    assert 'id="appTitlebarTitle"' in HTML
    assert 'id="appTitlebarSub"' in HTML


def test_syncappTitlebar_compact_metadata():
    assert 'subText = String(vis.length)' in PANELS_JS
    assert "t('n_messages', vis.length)" not in PANELS_JS


def test_long_press_handler_wired():
    assert '_lpTimer' in PANELS_JS
    assert 'SESSION_LONG_PRESS_DELAY_MS' in PANELS_JS
    assert 'long-pressing' in PANELS_JS


def test_session_action_menu_callable_from_titlebar():
    assert '_openSessionActionMenu(S.session, titleEl)' in PANELS_JS
