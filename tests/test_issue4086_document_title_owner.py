import re
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

    # #7611 wrapped the bare title write in a `!S.session{...}` block so the
    # installation-scoped instance label shares the same guard. The contract
    # this test locks is the GUARD, not the brace style, so normalise the
    # whitespace before matching and assert both halves explicitly:
    #   1. the !S.session guard exists;
    #   2. the bare write `document.title=name;` sits inside it; and
    #   3. NO document.title write survives outside it.
    compact = re.sub(r"\s+", "", body)
    guard = compact.find("if(!S.session){")
    assert guard != -1, (
        "applyBotName must wrap its document.title write in an explicit "
        "!S.session block so syncTopbar() stays the sole owner of the "
        "session title (#4086)"
    )

    depth = 0
    end = len(compact)
    for idx in range(guard, len(compact)):
        ch = compact[idx]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = idx + 1
                break
    guarded = compact[guard:end]
    unguarded = compact[:guard] + compact[end:]

    assert "document.title=name;" in guarded, (
        "the bare title write must live inside the !S.session guard"
    )
    assert "document.title=" not in unguarded, (
        "no document.title write may sit outside the !S.session guard — "
        "session titles belong to syncTopbar() while a chat session is "
        "active (#4086)"
    )


def test_sync_topbar_remains_session_document_title_owner():
    src = UI_JS.read_text()
    body = _extract_function(src, "function syncTopbar(){")

    assert "document.title=sessionTitle+' \\u2014 '+assistantDisplayName();" in body
