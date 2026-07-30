"""Regression coverage for panel-driven document.title ownership (#4039)."""

from __future__ import annotations


def _src(name: str) -> str:
    with open(f"static/{name}", encoding="utf-8") as f:
        return f.read()


def test_sync_app_titlebar_stamps_non_chat_document_title():
    src = _src("panels.js")

    assert "if (panel !== 'chat') {" in src
    assert "const bot = typeof assistantDisplayName === 'function' ? assistantDisplayName() : '';" in src
    assert "document.title = bot ? mainText + ' \\u2014 ' + bot : mainText;" in src


def test_switch_panel_restores_chat_title_via_sync_topbar():
    src = _src("panels.js")

    assert "if (nextPanel === 'chat' && typeof syncTopbar === 'function') syncTopbar();" in src
    assert "else syncAppTitlebar();" in src


def test_chat_title_format_stays_owned_by_sync_topbar():
    panels_src = _src("panels.js")
    ui_src = _src("ui.js")

    assert "document.title=sessionTitle+' \\u2014 '+assistantDisplayName();" in ui_src
    assert "document.title=sessionTitle+' \\u2014 '+assistantDisplayName();" not in panels_src
