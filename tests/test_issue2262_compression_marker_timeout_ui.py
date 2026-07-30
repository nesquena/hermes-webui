from pathlib import Path


def _read(path: str) -> str:
    return Path(path).read_text(encoding="utf-8")


def test_preserved_task_list_marker_only_helper_is_strict():
    src = _read("static/ui.js")

    assert "function _isPreservedCompressionTaskListMarkerOnlyText" in src
    start = src.find("function _isPreservedCompressionTaskListMarkerOnlyText")
    end = src.find("function _isPreservedCompressionTaskListMessage", start)
    helper = src[start:end]

    assert "_isPreservedCompressionTaskListMarkerText(text)" in helper
    assert ".replace(/^\\s*\\[your active task list was preserved across context compression\\]" in helper
    assert ".trim()" in helper


def test_marker_only_assistant_message_renders_as_error_not_model_text():
    src = _read("static/ui.js")

    assert "function _isMarkerOnlyAssistantCompressionMessage" in src
    assert "m.role!=='assistant'" in src
    assert "_isPreservedCompressionTaskListMarkerOnlyText(text)" in src
    assert "if(!isUser&&_isMarkerOnlyAssistantCompressionMessage(m))" in src
    assert "content='**Error:** No response received after context compression. Please retry.'" in src


def test_done_and_restore_replace_marker_only_assistant_with_error_toast():
    src = _read("static/messages.js")

    assert "function _replaceMarkerOnlyAssistantWithStreamError(messages)" in src
    assert "_isMarkerOnlyAssistantMessage(msg)" in src
    assert "msg.content='**Error:** No response received after context compression. Please retry.'" in src
    assert "internal preserved-task-list compression marker" in src
    assert "_markerOnlyAssistantError=_replaceMarkerOnlyAssistantWithStreamError(S.messages)" in src
    assert "showToast('No response received after context compression. Please retry.',5000,'error')" in src
