from pathlib import Path

REPO = Path(__file__).parent.parent
INDEX_HTML = (REPO / 'static' / 'index.html').read_text(encoding='utf-8')
STYLE_CSS = (REPO / 'static' / 'style.css').read_text(encoding='utf-8')
UI_JS = (REPO / 'static' / 'ui.js').read_text(encoding='utf-8')


def test_scroll_controls_are_overlays_outside_messages_scroller():
    shell = INDEX_HTML.index('<div class="messages-shell">')
    scroller = INDEX_HTML.index('<div class="messages" id="messages">')
    assert shell < INDEX_HTML.index('id="scrollToBottomBtn"') < scroller
    assert '.messages-shell{flex:1;min-height:0;position:relative;display:flex;flex-direction:column;}' in STYLE_CSS
    assert '.scroll-to-bottom-btn{position:absolute;' in STYLE_CSS
    assert '.session-jump-btn{position:absolute;' in STYLE_CSS


def test_bottom_button_has_dead_zone_and_no_platform_scroll_shim():
    scroll_listener = UI_JS[UI_JS.index("el.addEventListener('scroll'"):UI_JS.index('function _fmtTokens')]
    assert 'const showBottomButton=!_scrollPinned && el.scrollHeight-top-el.clientHeight>80;' in scroll_listener
    assert '_isIosStandalonePwa' not in UI_JS
    assert '_messagePanePreferredBottomScrollTop' not in UI_JS
