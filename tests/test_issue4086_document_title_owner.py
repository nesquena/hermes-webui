from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BOOT_JS = ROOT / "static" / "boot.js"
UI_JS = ROOT / "static" / "ui.js"


def _extract_function(src: str, signature: str) -> str:
    start = src.find(signature)
    assert start != -1, f"{signature} not found"
    depth = 0
    for idx in range(start, len(src)):
        ch = src[idx]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return src[start : idx + 1]
    raise AssertionError(f"{signature} body did not terminate")


def test_apply_bot_name_does_not_overwrite_active_session_document_title():
    """Session titles belong to syncTopbar() while a chat session is active."""
    src = BOOT_JS.read_text()
    body = _extract_function(src, "function applyBotName(){")

    assert "if(!S.session) document.title=name;" in body
    assert "document.title=name;" not in body.replace(
        "if(!S.session) document.title=name;",
        "",
    )


def test_sync_topbar_remains_session_document_title_owner():
    src = UI_JS.read_text()
    body = _extract_function(src, "function syncTopbar(){")

    assert "document.title=sessionTitle+' \\u2014 '+assistantDisplayName();" in body
