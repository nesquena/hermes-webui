import pathlib


ROOT = pathlib.Path(__file__).resolve().parent.parent
UI_JS = ROOT / "static" / "ui.js"


def test_select_model_custom_option_uses_friendly_label_helper():
    src = UI_JS.read_text(encoding="utf-8")
    start = src.find("async function selectModelFromDropdown(value)")
    assert start != -1, "selectModelFromDropdown() not found"
    end = src.find("\nfunction toggleModelDropdown()", start)
    assert end != -1, "toggleModelDropdown() boundary not found"
    body = src[start:end]

    assert "opt.textContent=getModelLabel(value);" in body, (
        "Temporary model options should use getModelLabel(value) so the chip shows a "
        "friendly label instead of a raw slug when the value is not already in the "
        "native <select> options."
    )
    assert "opt.textContent=value.split('/').pop()||value;" not in body, (
        "Raw slug fallback in selectModelFromDropdown() regresses the model chip for "
        "Ollama-tag style model IDs."
    )
