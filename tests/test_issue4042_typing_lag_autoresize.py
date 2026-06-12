from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
MESSAGES_JS = (REPO / "static" / "messages.js").read_text(encoding="utf-8")
BOOT_JS = (REPO / "static" / "boot.js").read_text(encoding="utf-8")


def test_composer_autoresize_is_single_flight_per_frame():
    assert "let _composerAutoResizeRaf=0;" in MESSAGES_JS
    assert "function scheduleComposerAutoResize()" in MESSAGES_JS
    assert "if(typeof requestAnimationFrame!=='function'){autoResize();return;}" in MESSAGES_JS
    assert "if(_composerAutoResizeRaf) return;" in MESSAGES_JS
    assert "_composerAutoResizeRaf=requestAnimationFrame(()=>{" in MESSAGES_JS
    assert "_composerAutoResizeRaf=0;" in MESSAGES_JS
    assert "autoResize();" in MESSAGES_JS


def test_composer_input_listener_uses_scheduler_instead_of_direct_reflow():
    start = BOOT_JS.index("$('msg').addEventListener('input',()=>{")
    end = BOOT_JS.index("// Persist composer draft to server", start)
    listener = BOOT_JS[start:end]

    assert "scheduleComposerAutoResize();" in listener
    assert "autoResize();" not in listener
    assert "updateSendBtn();" not in listener
