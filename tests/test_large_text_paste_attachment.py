"""Regression tests for large composer text paste attachment behavior."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import textwrap

import pytest

ROOT = Path(__file__).resolve().parents[1]
BOOT_JS = (ROOT / "static" / "boot.js").read_text(encoding="utf-8")
CHANGELOG = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
CONFIG_PY = (ROOT / "api" / "config.py").read_text(encoding="utf-8")
I18N_JS = (ROOT / "static" / "i18n.js").read_text(encoding="utf-8")
INDEX_HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
PANELS_JS = (ROOT / "static" / "panels.js").read_text(encoding="utf-8")
NODE = shutil.which("node")
UTC_2026_07_01_12_41_11_610 = 1782909671610


def _extract_function(source: str, name: str) -> str:
    marker = f"function {name}("
    start = source.index(marker)
    brace_start = source.index("{", start)
    depth = 0
    for idx in range(brace_start, len(source)):
        char = source[idx]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return source[start : idx + 1]
    raise AssertionError(f"could not extract function {name}")


def _run_large_text_filename(now_value: int, pending_files: list[dict[str, str]]) -> str:
    if NODE is None:
        raise AssertionError("node not on PATH")
    fn_source = _extract_function(BOOT_JS, "_largeTextPasteFileName")
    script = textwrap.dedent(
        f"""
        const fnSource = {json.dumps(fn_source)};
        globalThis.S = {{pendingFiles: {json.dumps(pending_files)}}};
        eval(fnSource);
        process.stdout.write(_largeTextPasteFileName({json.dumps(now_value)}));
        """
    )
    result = subprocess.run(
        [NODE, "-e", script],
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, "TZ": "Asia/Shanghai"},
    )
    return result.stdout.strip()


def test_large_text_paste_threshold_helpers_are_defined():
    assert "const LARGE_TEXT_PASTE_CHAR_THRESHOLD=4000" in BOOT_JS
    assert "const LARGE_TEXT_PASTE_LINE_THRESHOLD=100" in BOOT_JS
    assert "function _shouldAttachLargePastedText(text)" in BOOT_JS
    assert "value.length>=LARGE_TEXT_PASTE_CHAR_THRESHOLD" in BOOT_JS
    assert "_largeTextPasteLineCount(value)>=LARGE_TEXT_PASTE_LINE_THRESHOLD" in BOOT_JS


def test_large_text_line_count_does_not_overcount_trailing_newline():
    assert "const lines=value.split('\\n');" in BOOT_JS
    assert "return value.endsWith('\\n')?lines.length-1:lines.length;" in BOOT_JS


def test_large_text_paste_attachment_setting_is_server_backed_and_default_on():
    assert '"large_text_paste_as_attachment": True' in CONFIG_PY
    bool_keys_start = CONFIG_PY.index("_SETTINGS_BOOL_KEYS")
    assert '"large_text_paste_as_attachment"' in CONFIG_PY[bool_keys_start:]


def test_large_text_paste_attachment_setting_is_exposed_in_chat_settings():
    assert 'id="settingsLargeTextPasteAsAttachment"' in INDEX_HTML
    assert 'data-i18n="settings_label_large_text_paste_as_attachment"' in INDEX_HTML
    assert 'data-i18n="settings_desc_large_text_paste_as_attachment"' in INDEX_HTML
    assert "large_text_paste_as_attachment: !!($('settingsLargeTextPasteAsAttachment')||{}).checked" in PANELS_JS
    assert "largeTextPasteCb.checked=settings.large_text_paste_as_attachment!==false" in PANELS_JS
    assert "window._largeTextPasteAsAttachment=this.checked" in PANELS_JS


def test_large_text_paste_attachment_setting_hydrates_runtime_gate_default_on():
    assert "window._largeTextPasteAsAttachment=s.large_text_paste_as_attachment!==false" in BOOT_JS
    assert "if(window._largeTextPasteAsAttachment===false)return false;" in BOOT_JS


def test_line_threshold_is_one_hundred_lines():
    assert "const LARGE_TEXT_PASTE_LINE_THRESHOLD=100" in BOOT_JS
    assert "_largeTextPasteLineCount(value)>=LARGE_TEXT_PASTE_LINE_THRESHOLD" in BOOT_JS


def test_large_text_paste_creates_markdown_file_and_uses_existing_tray():
    assert "function _largeTextPasteFile(text,now)" in BOOT_JS
    assert "function _attachLargePastedText(file)" in BOOT_JS
    assert "pasted-text-${stamp}.md" in BOOT_JS
    assert "const existing=new Set((S.pendingFiles||[]).map(f=>f&&f.name).filter(Boolean))" in BOOT_JS
    assert "for(let i=2;existing.has(name);i++)name=`pasted-text-${stamp}-${i}.md`;" in BOOT_JS
    assert "new File([String(text||'')],name,{type:'text/markdown;charset=utf-8'})" in BOOT_JS
    assert "addFiles([file])" in BOOT_JS
    assert "setStatus(t('text_pasted')+file.name)" in BOOT_JS


def test_large_text_status_uses_i18n_key_available_to_all_locales():
    assert "text_pasted: 'Pasted text attached as '," in I18N_JS
    assert I18N_JS.count("text_pasted:") == I18N_JS.count("image_pasted:")
    assert "settings_label_large_text_paste_as_attachment" in I18N_JS
    assert I18N_JS.count("settings_label_large_text_paste_as_attachment") == I18N_JS.count("settings_label_workspace_panel_open")
    assert I18N_JS.count("settings_desc_large_text_paste_as_attachment") == I18N_JS.count("settings_desc_workspace_panel_open")


def test_paste_handler_keeps_image_paste_path_before_large_text_path():
    paste_idx = BOOT_JS.index("$('msg').addEventListener('paste',e=>{")
    image_idx = BOOT_JS.index("if(imageItems.length){", paste_idx)
    return_idx = BOOT_JS.index("return;", image_idx)
    text_idx = BOOT_JS.index("const plainText=e.clipboardData?.getData('text/plain')||'';", paste_idx)
    attach_idx = BOOT_JS.index("_attachLargePastedText(pastedTextFile);", text_idx)

    assert image_idx < return_idx < text_idx < attach_idx
    assert "if(!hasText)e.preventDefault();" in BOOT_JS[image_idx:return_idx]


def test_large_text_paste_prevents_default_textarea_insert_only_for_large_plain_text():
    text_idx = BOOT_JS.index("const plainText=e.clipboardData?.getData('text/plain')||'';")
    block = BOOT_JS[text_idx : BOOT_JS.index("});", text_idx)]
    assert "if(!_shouldAttachLargePastedText(plainText))return;" in block
    assert "const pastedTextFile=_largeTextPasteFile(plainText);" in block
    assert "if(!_largeTextPasteFitsUploadLimit(pastedTextFile))return;" in block
    assert "e.preventDefault();" in block
    assert "_attachLargePastedText(pastedTextFile);" in block
    assert block.index("if(!_shouldAttachLargePastedText(plainText))return;") < block.index("const pastedTextFile=_largeTextPasteFile(plainText);")
    assert block.index("if(!_largeTextPasteFitsUploadLimit(pastedTextFile))return;") < block.index("e.preventDefault();")


def test_oversize_large_text_paste_falls_back_to_native_paste_instead_of_being_dropped():
    assert "function _largeTextPasteFitsUploadLimit(file)" in BOOT_JS
    assert "typeof MAX_UPLOAD_BYTES==='number'&&file.size>MAX_UPLOAD_BYTES" in BOOT_JS
    text_idx = BOOT_JS.index("const plainText=e.clipboardData?.getData('text/plain')||'';")
    block = BOOT_JS[text_idx : BOOT_JS.index("});", text_idx)]
    fit_idx = block.index("if(!_largeTextPasteFitsUploadLimit(pastedTextFile))return;")
    prevent_idx = block.index("e.preventDefault();")
    attach_idx = block.index("_attachLargePastedText(pastedTextFile);")
    assert fit_idx < prevent_idx < attach_idx


def test_changelog_mentions_large_text_paste_attachment():
    assert "Large plain-text pastes in the composer now become `.md` attachments" in CHANGELOG


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_large_text_paste_filename_uses_local_time():
    assert _run_large_text_filename(
        now_value=UTC_2026_07_01_12_41_11_610,
        pending_files=[],
    ) == "pasted-text-2026-07-01_20-41-11-610.md"


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_large_text_paste_filename_duplicate_suffix_uses_local_stamp():
    assert _run_large_text_filename(
        now_value=UTC_2026_07_01_12_41_11_610,
        pending_files=[{"name": "pasted-text-2026-07-01_20-41-11-610.md"}],
    ) == "pasted-text-2026-07-01_20-41-11-610-2.md"


def test_large_text_paste_filename_no_longer_uses_toISOString():
    fn_source = _extract_function(BOOT_JS, "_largeTextPasteFileName")
    assert "toISOString" not in fn_source
    assert "getFullYear" in fn_source
    assert "getHours" in fn_source
    assert "getMilliseconds" in fn_source
