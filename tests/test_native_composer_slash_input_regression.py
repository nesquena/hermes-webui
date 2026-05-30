from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
BOOT_JS = (REPO / "static" / "boot.js").read_text(encoding="utf-8")


def test_native_composer_restores_slash_and_question_mark_insert_paths():
    assert "function _insertComposerTextAtCursor(input,text){" in BOOT_JS
    assert "input.dispatchEvent(new Event('input',{bubbles:true}));" in BOOT_JS
    assert "$('msg').addEventListener('beforeinput',e=>{" in BOOT_JS
    assert "if((text==='/'||text==='?')&&e.inputType==='insertText'){" in BOOT_JS
    assert "_insertComposerTextAtCursor($('msg'),text);" in BOOT_JS


def test_native_composer_keydown_restores_slash_when_browser_skips_beforeinput():
    start = BOOT_JS.index("$('msg').addEventListener('keydown',e=>{")
    end = BOOT_JS.index("  // Autocomplete navigation when dropdown is open", start)
    block = BOOT_JS[start:end]
    assert "const slashPressed=" in block
    assert "(!e.shiftKey&&e.key==='/')" in block
    assert "(e.key==='Unidentified'||e.key==='')&&e.code==='Slash'" in block
    assert "e.code==='NumpadDivide'" in block
    assert "e.preventDefault();" in block
    assert "_insertComposerTextAtCursor($('msg'),'/');" in block
