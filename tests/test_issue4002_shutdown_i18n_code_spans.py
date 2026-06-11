from pathlib import Path
import re


REPO = Path(__file__).resolve().parents[1]


def test_shutdown_description_uses_split_i18n_spans_in_index_html():
    html = (REPO / "static" / "index.html").read_text(encoding="utf-8")
    block_match = re.search(
        r'<div style="font-size:11px;color:var\(--muted\);margin-bottom:8px">(.*?)</div>',
        html,
        re.DOTALL,
    )
    assert block_match, "Shutdown settings description block must exist."
    block = block_match.group(1)
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
    assert "el.textContent = val;" in src
    assert "innerHTML = val" not in src
    assert "data-i18n-html" not in src
