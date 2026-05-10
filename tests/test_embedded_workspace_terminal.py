import os
import pathlib


REPO_ROOT = pathlib.Path(__file__).parent.parent.resolve()


def _read(path: str) -> str:
    return (REPO_ROOT / path).read_text(encoding="utf-8")


def test_terminal_is_opened_by_slash_command_not_permanent_composer_icon():
    html = _read("static/index.html")
    commands_js = _read("static/commands.js")
    sw = _read("static/sw.js")
    assert 'id="btnTerminalToggle"' not in html
    assert "name:'terminal'" in commands_js
    assert "fn:cmdTerminal" in commands_js
    assert "api('/api/workspaces')" in commands_js
    assert "await newSession()" in commands_js
    assert "toggleComposerTerminal(true)" in commands_js
    assert 'id="terminalViewport"' in html
    assert 'id="terminalSurface"' in html
    assert "static/terminal.js" in html
    assert "./static/terminal.js" in sw
    assert "xterm@5.3.0" in html


def test_terminal_surface_uses_composer_flyout_card_pattern():
    html = _read("static/index.html")
    style_css = _read("static/style.css")

    flyout = html.split('<div class="composer-flyout">', 1)[1].split(
        '<div class="queue-pill-outer">', 1
    )[0]
    assert 'id="composerTerminalPanel"' in flyout
    assert 'class="composer-terminal-inner"' in flyout
    assert 'id="composerTerminalDock"' in flyout
    assert 'id="terminalResizeHandle"' in flyout
    assert (
        'id="composerTerminalPanel"'
        not in html.split('<div class="queue-pill-outer">', 1)[1]
    )
    assert ".composer-terminal-panel{position:absolute" in style_css
    assert "bottom:-24px" in style_css
    assert "width:min(calc(100% - 64px),720px)" in style_css
    assert (
        ".composer-wrap.terminal-dock-visible .composer-flyout{z-index:4" in style_css
    )
    assert (
        ".composer-terminal-panel.is-collapsed{bottom:-2px;width:min(calc(100% - 112px),560px);overflow:visible;z-index:4"
        in style_css
    )
    assert (
        ".composer-terminal-panel.is-expanding-from-dock .composer-terminal-inner{transition:opacity .18s ease"
        in style_css
    )
    assert (
        ".messages.terminal-expanding-from-dock{transition:none!important" in style_css
    )
    assert ".composer-terminal-dock{min-height:42px" in style_css
    assert (
        ".composer-terminal-inner{height:var(--composer-terminal-height,260px)"
        in style_css
    )
    assert "transform:translateY(100%)" in style_css


def test_terminal_uses_controlled_desktop_resize_handle():
    html = _read("static/index.html")
    style_css = _read("static/style.css")
    terminal_js = _read("static/terminal.js")

    assert 'class="composer-terminal-resize-handle"' in html
    assert 'role="separator"' in html
    assert 'aria-orientation="horizontal"' in html
    terminal_inner_rule = style_css.split(".composer-terminal-inner{", 1)[1].split(
        "}", 1
    )[0]
    assert "resize:" not in terminal_inner_rule
    assert "cursor:ns-resize" in style_css
    assert "const TERMINAL_HEIGHT_DEFAULT=260" in terminal_js
    assert "const TERMINAL_HEIGHT_MIN=180" in terminal_js
    assert "const TERMINAL_HEIGHT_MAX=520" in terminal_js
    assert "max:Math.max(min,Math.min(hardMax,maxByViewport))" in terminal_js


def test_terminal_resize_path_refits_backend_and_transcript_space():
    terminal_js = _read("static/terminal.js")

    assert "function _applyTerminalHeight" in terminal_js
    apply_block = terminal_js.split("function _applyTerminalHeight", 1)[1].split(
        "function _resetTerminalHeightForViewport", 1
    )[0]
    assert "_fitTerminal();" in apply_block
    assert "_syncTerminalTranscriptSpace(true);" in apply_block
    assert "function _moveTerminalHeightResize" in terminal_js
    assert (
        "_applyTerminalHeight(TERMINAL_UI.resizeStartHeight+(TERMINAL_UI.resizeStartY-ev.clientY))"
        in terminal_js
    )
    assert (
        "handle.addEventListener('pointerdown',_startTerminalHeightResize)"
        in terminal_js
    )
    assert (
        "handle.addEventListener('pointermove',_moveTerminalHeightResize)"
        in terminal_js
    )
    assert "clearTimeout(TERMINAL_UI.resizeTimer)" in terminal_js
    assert "api('/api/terminal/resize'" in terminal_js


def test_terminal_open_reserves_transcript_space():
    style_css = _read("static/style.css")
    terminal_js = _read("static/terminal.js")

    assert (
        ".messages.terminal-open{padding-bottom:var(--terminal-card-height" in style_css
    )
    assert (
        ".messages.terminal-collapsed{padding-bottom:var(--terminal-dock-height"
        in style_css
    )
    assert "scroll-padding-bottom:var(--terminal-card-height" in style_css
    assert "classList.add('terminal-open')" in terminal_js
    assert "classList.add('terminal-collapsed')" in terminal_js
    assert "classList.remove('terminal-open')" in terminal_js
    assert "classList.remove('terminal-collapsed')" in terminal_js
    assert "messages.style.setProperty('--terminal-card-height'" in terminal_js
    assert "messages.style.setProperty('--terminal-dock-height'" in terminal_js
    assert "messages.style.removeProperty('--terminal-card-height')" in terminal_js
    assert "messages.style.removeProperty('--terminal-dock-height')" in terminal_js
    assert "function _terminalIsMessagesNearBottom" in terminal_js
    assert "scrollToBottom" in terminal_js


def test_terminal_initial_open_settles_transcript_space_before_reveal():
    terminal_js = _read("static/terminal.js")

    open_block = terminal_js.split("async function toggleComposerTerminal", 1)[1].split(
        "function collapseComposerTerminal", 1
    )[0]
    assert "messages.classList.add('terminal-expanding-from-dock')" in open_block
    assert "_syncTerminalTranscriptSpace(true,{immediate:true});" in open_block
    assert "void messages.offsetHeight;" in open_block
    assert "panel.classList.add('is-open')" in open_block
    assert "messages.classList.remove('terminal-expanding-from-dock')" in open_block
    assert open_block.index(
        "_syncTerminalTranscriptSpace(true,{immediate:true});"
    ) < open_block.index("panel.classList.add('is-open')")
    assert open_block.index("void messages.offsetHeight;") < open_block.index(
        "panel.classList.add('is-open')"
    )


def test_terminal_collapsed_state_preserves_pty_and_output_surface():
    html = _read("static/index.html")
    terminal_js = _read("static/terminal.js")

    assert 'id="btnTerminalCollapse"' in html
    assert 'onclick="collapseComposerTerminal()"' in html
    assert 'id="btnTerminalExpand"' in html
    assert 'onclick="expandComposerTerminal()"' in html
    assert 'id="btnTerminalDockClose"' in html
    assert 'onclick="closeComposerTerminal()"' in html
    assert "collapsed:false" in terminal_js
    collapse_block = terminal_js.split("function collapseComposerTerminal", 1)[1].split(
        "function expandComposerTerminal", 1
    )[0]
    assert "api('/api/terminal/close'" not in collapse_block
    assert "_disposeXterm" not in collapse_block
    assert "_setTerminalChromeState('collapsed')" in collapse_block
    assert (
        "composerWrap.classList.toggle('terminal-dock-visible',collapsed)"
        in terminal_js
    )
    expand_block = terminal_js.split("function expandComposerTerminal", 1)[1].split(
        "function _disposeXterm", 1
    )[0]
    assert "_setTerminalChromeState('expanded')" in expand_block
    assert "panel.classList.add('is-expanding-from-dock')" in expand_block
    assert "panel.classList.remove('is-expanding-from-dock')" in expand_block
    assert "messages.classList.add('terminal-expanding-from-dock')" in expand_block
    assert "messages.classList.remove('terminal-expanding-from-dock')" in expand_block
    assert "_syncTerminalTranscriptSpace(true,{immediate:true});" in expand_block
    assert "void messages.offsetHeight;" in expand_block
    assert expand_block.index(
        "_syncTerminalTranscriptSpace(true,{immediate:true});"
    ) < expand_block.index("_setTerminalChromeState('expanded')")
    assert expand_block.index("void messages.offsetHeight;") < expand_block.index(
        "_setTerminalChromeState('expanded')"
    )
    assert "_resetTerminalHeightForViewport();" in expand_block
    assert "focusComposerTerminalInput();" in expand_block
    close_block = terminal_js.split("async function closeComposerTerminal", 1)[1].split(
        "async function restartComposerTerminal", 1
    )[0]
    assert "api('/api/terminal/close'" in close_block
    assert "_disposeXterm();" in close_block


def test_terminal_slash_command_expands_existing_collapsed_terminal():
    commands_js = _read("static/commands.js")
    terminal_js = _read("static/terminal.js")

    assert "await toggleComposerTerminal(true)" in commands_js
    toggle_block = terminal_js.split("async function toggleComposerTerminal", 1)[
        1
    ].split("function collapseComposerTerminal", 1)[0]
    assert "if(TERMINAL_UI.open)" in toggle_block
    assert "if(TERMINAL_UI.collapsed)expandComposerTerminal();" in toggle_block
    assert "else focusComposerTerminalInput();" in toggle_block


def test_terminal_v1_does_not_expose_send_to_chat_action():
    html = _read("static/index.html")
    terminal_js = _read("static/terminal.js")
    combined = html + terminal_js
    assert "Send latest result to chat" not in combined
    assert "send latest result" not in combined.lower()
    assert "Send to chat" not in combined


def test_terminal_ui_handles_shell_close_commands():
    terminal_js = _read("static/terminal.js")

    assert "function _isTerminalCloseCommand" in terminal_js
    for command in ("exit", "quit", "logout", "close"):
        assert f"'{command}'" in terminal_js
    assert "closeComposerTerminal();" in terminal_js


def test_terminal_restart_ignores_stale_sse_events():
    terminal_js = _read("static/terminal.js")

    assert "if(TERMINAL_UI.source!==source)return;" in terminal_js
    assert "async function restartComposerTerminal" in terminal_js
    restart_block = terminal_js.split("async function restartComposerTerminal", 1)[
        1
    ].split("function clearComposerTerminal", 1)[0]
    assert "TERMINAL_UI.source.close()" in restart_block
    assert "TERMINAL_UI.source=null" in restart_block


def test_terminal_routes_are_registered():
    routes = _read("api/routes.py")
    for path in (
        "/api/terminal/start",
        "/api/terminal/input",
        "/api/terminal/output",
        "/api/terminal/resize",
        "/api/terminal/close",
    ):
        assert path in routes


def test_terminal_process_does_not_mutate_global_terminal_cwd(tmp_path, monkeypatch):
    from api.terminal import close_terminal, start_terminal

    monkeypatch.delenv("TERMINAL_CWD", raising=False)
    sid = "test-terminal-env"
    term = start_terminal(sid, tmp_path, rows=8, cols=40, restart=True)
    try:
        assert term.workspace == str(tmp_path.resolve())
        assert os.environ.get("TERMINAL_CWD") is None
    finally:
        close_terminal(sid)


def test_terminal_output_preserves_control_sequences_for_xterm():
    import codecs
    from api.terminal import _decode_terminal_output

    raw = "\x1b[?2004h$ \x1b[32mhello\x1b[0m\n"
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    assert _decode_terminal_output(decoder, raw.encode()) == raw


def test_terminal_xterm_theme_follows_appearance_tokens():
    terminal_js = _read("static/terminal.js")
    style_css = _read("static/style.css")

    assert "function _terminalTheme" in terminal_js
    assert "_terminalCssVar('--code-bg'" in terminal_js
    assert "_terminalCssVar('--pre-text'" in terminal_js
    assert "syncComposerTerminalTheme" in terminal_js
    assert "attributeFilter:['class','data-skin']" in terminal_js
    assert "background:var(--code-bg)" in style_css
    assert "color:var(--pre-text)" in style_css
