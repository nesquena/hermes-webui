import pathlib


ROOT = pathlib.Path(__file__).resolve().parent.parent
UI_JS = ROOT / "static" / "ui.js"


def _read_ui() -> str:
    return UI_JS.read_text(encoding="utf-8")


def test_select_model_custom_option_uses_friendly_label_helper():
    src = _read_ui()
    start = src.find("async function selectModelFromDropdown(value)")
    assert start != -1, "selectModelFromDropdown() not found"
    end = src.find("\nfunction toggleModelDropdown()", start)
    if end == -1:
        end = src.find("\nasync function toggleModelDropdown()", start)
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


def test_get_model_label_formats_bare_ollama_ids():
    src = _read_ui()
    assert (
        "const looksLikeOllamaTag = /^[a-z0-9][\\w.-]*:[\\w.-]+$/i.test(_last);" in src
    )
    # Tightened heuristic: only apply Ollama formatter to IDs with @ollama prefix or colon-tag format,
    # avoiding reformatting of bare provider model IDs like claude-sonnet-4-6 or gpt-4o.
    assert (
        "const looksLikeBareOllamaId = modelId.startsWith('@ollama') || looksLikeOllamaTag;"
        in src
    ), (
        "looksLikeBareOllamaId must be restricted to @ollama-prefixed or colon-tagged IDs "
        "to avoid reformatting generic bare model IDs."
    )
    assert "const ollamaLabel = _fmtOllamaLabel(_last);" in src
    assert (
        "if ((modelId.startsWith('ollama/') || modelId.startsWith('@ollama') || looksLikeOllamaTag || looksLikeBareOllamaId) && ollamaLabel !== _last) {"
        in src
    ), (
        "Ollama-tagged ids like 'kimi-k2.6:3b' should still pass through _fmtOllamaLabel() "
        "when the formatter produces a friendlier label."
    )


def test_fmt_ollama_label_preserves_dotted_acronyms():
    src = _read_ui()
    assert (
        "if (t.length <= 3 && /^[a-zA-Z.]+$/.test(t)) return t.toUpperCase();" in src
    ), "JS Ollama formatter should preserve dotted acronyms like 'a.b' -> 'A.B'."
