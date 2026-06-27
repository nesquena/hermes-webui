"""Regression coverage for global screenshot paste UX.

Users expect Ctrl/Cmd+V screenshots to attach even when the textarea does not
currently have focus. The composer already supports paste on #msg; this guards the
document-level fallback that catches image clipboard data from the wider chat UI.
"""
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BOOT_JS = (ROOT / "static" / "boot.js").read_text(encoding="utf-8")


def _document_paste_handler_body() -> str:
    needle = "document.addEventListener('paste',e=>"
    start = BOOT_JS.index(needle)
    brace = BOOT_JS.index("{", start)
    depth = 0
    for i in range(brace, len(BOOT_JS)):
        c = BOOT_JS[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return BOOT_JS[brace : i + 1]
    raise AssertionError("document paste handler body not found")


def test_document_level_paste_handler_exists_for_screenshots():
    body = _document_paste_handler_body()
    assert "const imageItems=items.filter(i=>i.kind==='file'&&i.type.startsWith('image/'));" in body
    assert "addFiles(files);" in body
    assert "setStatus(t('image_pasted')" in body


def test_document_paste_does_not_double_handle_composer_paste():
    body = _document_paste_handler_body()
    assert "closest('#msg')" in body
    assert "return;" in body[body.index("closest('#msg')") : body.index("_isDocumentPasteEditableTarget")]


def test_document_paste_ignores_other_editable_fields():
    assert "function _isDocumentPasteEditableTarget" in BOOT_JS
    helper = BOOT_JS[BOOT_JS.index("function _isDocumentPasteEditableTarget") :]
    helper = helper[: helper.index("}\n", helper.index("function _isDocumentPasteEditableTarget")) + 2]
    assert "input, textarea, select" in helper
    assert "contenteditable" in helper
    assert "role=\"textbox\"" in helper
    body = BOOT_JS[BOOT_JS.index("document.addEventListener('paste',e=>") :]
    assert "if(_isDocumentPasteEditableTarget(target))return;" in body


def test_document_paste_focuses_composer_after_attaching_image():
    body = _document_paste_handler_body()
    assert "const msg=$('msg');" in body
    assert "msg.focus({preventScroll:true})" in body
