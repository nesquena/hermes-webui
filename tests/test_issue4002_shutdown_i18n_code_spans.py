from pathlib import Path


REPO = Path(__file__).resolve().parents[1]


def _extract_function(src: str, name: str) -> str:
    anchor = f"function {name}("
    start = src.find(anchor)
    assert start != -1, f"{name}() must exist"
    body_start = src.find("{", start)
    assert body_start != -1, f"{name}() must have a body"
    depth = 1
    idx = body_start + 1
    while depth and idx < len(src):
        if src[idx] == "{":
            depth += 1
        elif src[idx] == "}":
            depth -= 1
        idx += 1
    assert depth == 0, f"{name}() body must balance braces"
    return src[start:idx]


def test_shutdown_description_uses_split_i18n_spans_in_index_html():
    html = (REPO / "static" / "index.html").read_text(encoding="utf-8")
    start = html.find('<div class="settings-field" id="shutdownServerBlock"')
    assert start != -1, "Shutdown settings field must exist."
    button_idx = html.find('id="btnShutdownServer"', start)
    assert button_idx != -1, "Shutdown settings field must include the stop button."
    block = html[start:button_idx]
    assert 'data-i18n="settings_desc_shutdown_before_cmd"' in block
    assert 'data-i18n="settings_desc_shutdown_between_cmds"' in block
    assert 'data-i18n="settings_desc_shutdown_after_cmd"' in block
    assert block.count("<code>./ctl.sh start</code>") == 2
    assert 'data-i18n="settings_desc_shutdown"' not in block


def test_shutdown_locale_strings_no_longer_embed_code_tags():
    src = (REPO / "static" / "i18n.js").read_text(encoding="utf-8")
    locale_count = src.count("settings_label_shutdown:")
    assert locale_count > 0
    for key in (
        "settings_desc_shutdown_before_cmd",
        "settings_desc_shutdown_between_cmds",
        "settings_desc_shutdown_after_cmd",
    ):
        assert src.count(f"{key}:") == locale_count, f"{key} must exist in every locale block."
    assert "settings_desc_shutdown:" not in src
    for line in src.splitlines():
        if "settings_desc_shutdown_" in line:
            assert "<code>" not in line


def test_apply_locale_to_dom_stays_on_text_content():
    src = (REPO / "static" / "i18n.js").read_text(encoding="utf-8")
    apply_locale_body = _extract_function(src, "applyLocaleToDOM")
    assert "el.textContent = val;" in apply_locale_body
    assert "innerHTML = val" not in apply_locale_body
    assert "data-i18n-html" not in src
