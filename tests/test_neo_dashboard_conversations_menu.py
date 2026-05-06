"""Onda 9 — surface a Conversas entry on the Neo dashboard menu.

Without it, the dashboard-shell-mode left rail (.neo-dashboard-menu) had no
control that opened panelChat, so the historic session list — the same place
Wave 6/8 made sure all old conversations are listed — was unreachable from
the default landing page. The user reported this as "where do I find old
chats?" after seeing only the dashboard menu items.
"""
import pathlib

REPO_ROOT = pathlib.Path(__file__).parent.parent.resolve()
HTML = (REPO_ROOT / "static" / "index.html").read_text(encoding="utf-8")
I18N = (REPO_ROOT / "static" / "i18n.js").read_text(encoding="utf-8")


def test_conversations_menu_item_exists():
    menu_idx = HTML.find('class="neo-dashboard-menu"')
    end_idx = HTML.find("class=\"neo-dashboard-bottom\"", menu_idx)
    assert menu_idx != -1 and end_idx != -1, "neo-dashboard-menu block must exist"
    block = HTML[menu_idx:end_idx]
    assert 'data-panel="chat"' in block, (
        ".neo-dashboard-menu must include a data-panel=\"chat\" item so users "
        "can reach panelChat (the historic session list) from the dashboard"
    )
    assert "switchPanel('chat')" in block, (
        "the Conversas item must call switchPanel('chat')"
    )


def test_conversations_uses_dedicated_i18n_key():
    """Use a separate key from tab_chat so 'Conversas' (browse) is distinct
    from the 'Chat' label that may want a different translation surface."""
    menu_idx = HTML.find('class="neo-dashboard-menu"')
    end_idx = HTML.find("class=\"neo-dashboard-bottom\"", menu_idx)
    block = HTML[menu_idx:end_idx]
    assert 'data-i18n="tab_conversations"' in block


def test_pt_br_locale_has_conversas():
    """pt-BR is the runtime default (HERMES_WEBUI_LOCALE=pt-BR on the VPS).
    Anchor on tab_finance: 'Finanças' which is unique to the pt-BR block."""
    anchor = "tab_finance: 'Finanças'"
    idx = I18N.find(anchor)
    assert idx != -1, "pt-BR anchor missing — block may have been renamed"
    pt_block = I18N[max(0, idx - 600):idx + 600]
    assert "tab_conversations: 'Conversas'" in pt_block, (
        "pt-BR locale must define tab_conversations as 'Conversas'"
    )


def test_default_locale_has_conversations():
    """English is the fallback locale; entries missing here would render the
    bare key string instead of a label."""
    assert "tab_conversations: 'Conversations'" in I18N, (
        "English (default) locale must define tab_conversations"
    )


def test_conversations_item_inserted_after_dashboard():
    """Order matters for the menu — Conversas should sit right after
    Dashboard, before Projetos, mirroring the reading-flow used in the
    Telegram/WhatsApp gateways."""
    dash_idx = HTML.find('data-panel="dashboard" onclick="switchPanel(\'dashboard\')"')
    chat_idx = HTML.find('data-panel="chat" onclick="switchPanel(\'chat\')"')
    proj_idx = HTML.find('data-panel="projects" onclick="switchPanel(\'projects\')"')
    assert dash_idx != -1 and chat_idx != -1 and proj_idx != -1
    assert dash_idx < chat_idx < proj_idx, (
        "Conversas must sit between Dashboard and Projetos in the menu"
    )
