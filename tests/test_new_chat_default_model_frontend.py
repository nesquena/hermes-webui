from pathlib import Path

SESSIONS_JS = Path("static/sessions.js").read_text(encoding="utf-8")
MESSAGES_JS = Path("static/messages.js").read_text(encoding="utf-8")
CHANGELOG = Path("CHANGELOG.md").read_text(encoding="utf-8")


def _extract_function(source: str, signature: str) -> str:
    start = source.index(signature)
    # Look for the function body's opening brace, not an object literal inside
    # a default argument such as `options={}`.
    brace = source.index("{\n", start)
    depth = 0
    for idx in range(brace, len(source)):
        ch = source[idx]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return source[start : idx + 1]
    raise AssertionError(f"Function body not closed for {signature}")


def _new_session_function() -> str:
    return _extract_function(SESSIONS_JS, "async function newSession")


def test_new_chat_syncs_model_picker_when_default_provider_changes_but_model_id_matches():
    fn = _new_session_function()
    assert "currentModelState" in fn
    assert "currentProvider" in fn
    assert "sessionProvider" in fn
    assert "sessionProvider !== currentProvider" in fn
    assert "_applyModelToDropdown(S.session.model,modelSel,sessionProvider)" in fn


def test_new_chat_does_not_send_stale_dropdown_model_when_session_has_default_model():
    assert "model:S.session.model||$('modelSelect').value" in MESSAGES_JS
    assert "model_provider:S.session.model_provider||null" in MESSAGES_JS


def test_changelog_mentions_new_chat_default_model_provider_sync():
    unreleased = CHANGELOG.split("## [v0.51.103]", 1)[0]
    assert "New conversations now resync" in unreleased
    assert "default model provider" in unreleased
