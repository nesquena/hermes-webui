import pathlib


REPO_ROOT = pathlib.Path(__file__).parent.parent.resolve()


def _read(path: str) -> str:
    return (REPO_ROOT / path).read_text(encoding="utf-8")


def test_terminal_nav_registers_page_view_and_locale_label():
    html = _read("static/index.html")
    panels_js = _read("static/panels.js")
    i18n_js = _read("static/i18n.js")
    style_css = _read("static/style.css")

    assert 'data-panel="terminal"' in html
    assert "switchPanel('terminal',{fromRailClick:true})" in html
    assert 'id="mainTerminal"' in html
    assert 'id="panelTerminal"' in html
    assert "terminal: 'tab_terminal'" in panels_js
    assert "terminal: 'tab_terminal'" not in html
    assert "tab_terminal:" in i18n_js
    assert "showing-terminal" in style_css
    assert "main.main.showing-terminal > #mainTerminal{display:flex;overflow:hidden;}" in style_css


def test_terminal_page_mode_reuses_the_existing_runtime():
    panels_js = _read("static/panels.js")
    terminal_js = _read("static/terminal.js")

    assert "MAIN_VIEW_PANELS = ['settings','skills','memory','tasks','kanban','workspaces','profiles','insights','terminal','logs','plugin'];" in panels_js
    assert "await toggleComposerTerminal(true, { mode: 'page' });" in panels_js
    assert "await toggleComposerTerminal(true, { mode: 'dock' });" in panels_js
    assert "const desiredMode=opts.mode==='page'?'page':'dock';" in terminal_js
    assert "_terminalSetPresentationMode(desiredMode)" in terminal_js
    assert terminal_js.count("new window.Terminal(") == 1
    assert terminal_js.count("new EventSource(") == 1


def test_terminal_page_layout_scopes_dock_only_ui():
    style_css = _read("static/style.css")
    terminal_js = _read("static/terminal.js")

    assert "#panelTerminal .composer-terminal-panel.is-page-mode" in style_css
    assert "#panelTerminal .composer-terminal-panel.is-page-mode .composer-terminal-dock{display:none;}" in style_css
    assert "#panelTerminal .composer-terminal-panel.is-page-mode .composer-terminal-resize-handle" in style_css
    assert "TERMINAL_UI.mode==='page'" in terminal_js
    assert "messages.classList.remove('terminal-open');" in terminal_js
    assert "messages.classList.remove('terminal-collapsed');" in terminal_js
