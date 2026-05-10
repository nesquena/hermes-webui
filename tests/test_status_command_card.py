"""Regression tests for issue #463: WebUI /status info card.

/status should be a client-handled slash command that renders a safe,
ephemeral assistant-style card from already-loaded session/profile/model data.
It must not round-trip through the agent or a status endpoint just to draw the
card.
"""
import pathlib


REPO_ROOT = pathlib.Path(__file__).parent.parent
COMMANDS_JS = (REPO_ROOT / "static" / "commands.js").read_text(encoding="utf-8")
UI_JS = (REPO_ROOT / "static" / "ui.js").read_text(encoding="utf-8")
STYLE_CSS = (REPO_ROOT / "static" / "style.css").read_text(encoding="utf-8")
I18N_JS = (REPO_ROOT / "static" / "i18n.js").read_text(encoding="utf-8")
MESSAGES_JS = (REPO_ROOT / "static" / "messages.js").read_text(encoding="utf-8")


def _function_body(src: str, name: str) -> str:
    marker = f"function {name}"
    start = src.index(marker)
    brace = src.index("{", start)
    depth = 0
    for idx in range(brace, len(src)):
        if src[idx] == "{":
            depth += 1
        elif src[idx] == "}":
            depth -= 1
            if depth == 0:
                return src[start:idx + 1]
    raise AssertionError(f"Could not extract {name}()")


def test_status_command_is_registered_with_help_text():
    assert "{name:'status'" in COMMANDS_JS
    assert "desc:t('cmd_status')" in COMMANDS_JS
    assert "fn:cmdStatus" in COMMANDS_JS
    assert "cmd_status:'Show session info'" in I18N_JS


def test_status_command_uses_client_state_not_status_endpoint():
    body = _function_body(COMMANDS_JS, "cmdStatus")
    assert "/api/session/status" not in body
    assert "api(" not in body
    assert "S.session" in body
    assert "S.activeProfile" in COMMANDS_JS
    assert "model_provider" in COMMANDS_JS
    assert "last_usage" in COMMANDS_JS


def test_status_command_pushes_ephemeral_status_card_message():
    body = _function_body(COMMANDS_JS, "cmdStatus")
    assert "_statusCard" in body
    assert "_ephemeral:true" in body
    assert "renderMessages()" in body
    assert "_statusCardFromSession(S.session)" in body
    helper = _function_body(COMMANDS_JS, "_statusCardFromSession")
    assert "session_id" in helper
    assert "updated_at" in helper
    assert "message_count" in helper
    assert "active_stream_id" in helper


def test_status_card_renderer_escapes_all_dynamic_values_and_is_copyable():
    body = _function_body(UI_JS, "_statusCardHtml")
    assert "data-status-card" in body
    assert "data-copy-status-session" in body
    assert "onclick=\"copyStatusSessionId(this);event.stopPropagation()\"" in body
    assert "esc(card.title" in body
    assert "esc(card.subtitle" in body
    assert "esc(row.label" in body
    assert "esc(row.value" in body
    assert "esc(card.sessionId" in body
    assert "renderMd(" not in body, "Status card data should not be interpreted as markdown"


def test_render_messages_treats_status_card_as_visible_assistant_content():
    render_body = _function_body(UI_JS, "renderMessages")
    assert "m._statusCard" in render_body
    assert "_statusCardHtml(m._statusCard)" in render_body
    assert "statusHtml" in render_body


def test_status_card_styles_exist():
    assert ".status-card" in STYLE_CSS
    assert ".status-card-grid" in STYLE_CSS
    assert ".status-card-session-copy" in STYLE_CSS


def test_status_command_never_reaches_agent_send_path():
    send_body = _function_body(MESSAGES_JS, "send")
    branch_start = send_body.index("if(text.startsWith('/')")
    branch_end = send_body.index("if(_parsedCmd&&!_cmd)", branch_start)
    cmd_branch = send_body[branch_start:branch_end]
    assert "COMMANDS.find" in cmd_branch
    assert "return;" in cmd_branch
    assert "api('/api/chat/start'" not in cmd_branch
