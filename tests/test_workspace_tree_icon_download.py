"""Regression tests — workspace tree file icon click must download the file.

Bug: the icon rendered to the left of each file name (fileIcon()) had no click
handler of its own, so icon clicks bubbled to the row's onclick → openFile()
→ the preview pane. PDF rows render a *download* glyph
(fileIcon maps .pdf to li('download', 14)) and DOWNLOAD_EXTS rows download on
any click anyway, but preview-able types (PDF foremost) opened the preview —
the icon promised "download" and delivered "preview".

Fix contract (#user report: "Clicking the actual item (the name of it) opens
preview. Clicking the download icon to the left of the name actually downloads
that item."):

  * file-like rows bind iconEl.onclick → downloadFile(item.path) with
    e.stopPropagation() so the row's preview handler never fires;
  * the file NAME click keeps the #1707 debounce → el.onclick delegation
    (single click previews, double click renames) — untouched;
  * directory-like rows keep the expand/collapse toggle (no download handler);
  * read-only escape rows (authorized external symlinks) keep their gated
    navigation flow — no direct download bypass.

These tests static-analyze static/ui.js and drive the icon block through a
Node VM.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
UI_JS_PATH = REPO_ROOT / "static" / "ui.js"
I18N_JS_PATH = REPO_ROOT / "static" / "i18n.js"
NODE = shutil.which("node")


def _read(path: Path) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read()


def _icon_block() -> str:
    """Return the icon construction block of _renderTreeItems."""
    src = _read(UI_JS_PATH)
    start_marker = "const iconEl=document.createElement('span');"
    start = src.index(start_marker)
    end_marker = "el.appendChild(iconEl);"
    end = src.index(end_marker, start)
    return src[start:end + len(end_marker)]


def _name_handler_block() -> str:
    """Return the name element handler block (from the rename tooltip to the
    appendChild(nameEl) line) — the #1707 contract region."""
    src = _read(UI_JS_PATH)
    start_marker = "if(isLk && item.target)"
    start = src.find(start_marker)
    if start < 0:
        start_marker = "nameEl.title=t('double_click_rename');"
        start = src.find(start_marker)
    assert start >= 0, "nameEl rename tooltip not found in static/ui.js"
    end_marker = "el.appendChild(nameEl);"
    end = src.find(end_marker, start)
    assert end >= 0, "el.appendChild(nameEl) not found after rename tooltip"
    return src[start:end + len(end_marker)]


# ── Source-level regression locks ─────────────────────────────────────────────


class TestIconDownloadHandlerShape:
    def test_icon_binds_download_click_handler(self):
        block = _icon_block()
        assert "iconEl.onclick=" in block, (
            "the file icon must bind its own onclick handler — without it the "
            "click bubbles to the row's openFile (preview) handler"
        )

    def test_icon_handler_calls_downloadfile_with_item_path(self):
        block = _icon_block()
        assert "downloadFile(item.path)" in block, (
            "icon click must call downloadFile(item.path), not openFile"
        )

    def test_icon_handler_stops_propagation(self):
        block = _icon_block()
        assert "e.stopPropagation();" in block, (
            "icon click must stopPropagation so the row's preview handler "
            "does not also fire"
        )

    def test_icon_handler_gated_to_writable_file_rows(self):
        block = _icon_block()
        assert "if(isFileLike && !isReadOnlyEscape && hasDownloadGlyph){" in block, (
            "download handler must be gated to file-like rows that are not "
            "read-only escape rows (dirs toggle; escape rows keep their gate)"
        )

    def test_icon_has_download_tooltip(self):
        block = _icon_block()
        assert "t('media_download')" in block, (
            "icon must carry a Download tooltip (existing media_download key)"
        )

    def test_downloadfile_undefined_guard(self):
        block = _icon_block()
        assert "typeof downloadFile==='function'" in block, (
            "icon handler must guard downloadFile with typeof — downloadFile "
            "lives in static/workspace.js, a sibling global script"
        )

    def test_icon_still_appended_after_handler(self):
        """Ordering lock (#2554 family): handler wiring must not move the
        appendChild — icon construction still ends at el.appendChild(iconEl)."""
        src = _read(UI_JS_PATH)
        start = src.index("const iconEl=document.createElement('span');")
        end = src.index("el.appendChild(iconEl);", start)
        assert end > start


class TestNameClickContractUntouched:
    """The #1707 contract (single click on name → preview after debounce,
    double click → rename) must remain exactly as it was."""

    def test_name_click_still_delegates_to_row_handler(self):
        block = _name_handler_block()
        assert "el.onclick(" in block, (
            "nameEl.onclick must still delegate to el.onclick after the "
            "300ms debounce — the icon change must not touch the name path"
        )

    def test_name_click_still_uses_debounce(self):
        block = _name_handler_block()
        assert "setTimeout" in block and "clearTimeout" in block, (
            "nameEl debounce machinery must remain"
        )


class TestI18nKey:
    def test_media_download_key_exists_in_locales(self):
        i18n = _read(I18N_JS_PATH)
        count = i18n.count("media_download:")
        assert count >= 10, (
            f"media_download key must exist across locale blocks; found {count}"
        )


# ── Behavioral tests via Node VM ──────────────────────────────────────────────


pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")


def _run_node_vm(item_type: str, read_only_escape: bool, name: str = 'foo.pdf') -> dict:
    """Execute the icon block with mocked DOM/globals and click the icon."""
    block = _icon_block()
    payload = {
        "block": block,
        "itemType": item_type,
        "readOnlyEscape": read_only_escape,
        "name": name,
    }
    js = (
        "const params = " + json.dumps(payload) + ";\n"
        + r"""
const block = params.block;
const itemType = params.itemType;
const readOnlyEscape = params.readOnlyEscape;

let downloadCalls = [];
let stopPropagationCalls = 0;
let openCalls = 0;
let appendedTo = null;

const document = {
  createElement: (tag) => ({
    tagName: tag.toUpperCase(),
    className: '', classList: {add() {}}, textContent: '', title: '', innerHTML: '',
    onclick: null, attributes: {}, setAttribute(key, val) { this.attributes[key] = val; },
    style: {},
    appendChild(child) {},
  }),
};
const li = () => '';
const fileExt = (name) => name.slice(name.lastIndexOf('.')).toLowerCase();
const IMAGE_EXTS = new Set(['.png']);
const MD_EXTS = new Set(['.md']);
const DOWNLOAD_EXTS = new Set(['.zip']);
const fileIcon = (name) => ['.pdf', '.zip'].includes(fileExt(name)) ? 'download' : 'file-text';
const t = (key) => ({media_download: 'Download'}[key] || key);
const downloadFile = (path) => { downloadCalls.push(path); };
const el = { appendChild(child) { appendedTo = child; }, onclick() { openCalls++; } };
const item = { type: itemType, name: params.name, path: 'dir/' + params.name };

const isLk = itemType === 'symlink';
const isExternalLink = false;
const isDirLike = itemType === 'dir' || (isLk && itemType === 'dir');
const isFileLike = !isDirLike;
const isReadOnlyEscape = readOnlyEscape;

const runner = new Function(
  'document', 'li', 'fileIcon', 'fileExt', 'IMAGE_EXTS', 'MD_EXTS', 'DOWNLOAD_EXTS',
  't', 'downloadFile', 'el', 'item',
  'isLk', 'isExternalLink', 'isDirLike', 'isFileLike', 'isReadOnlyEscape',
  '(()=>{' + block + '})();'
);
runner(document, li, fileIcon, fileExt, IMAGE_EXTS, MD_EXTS, DOWNLOAD_EXTS,
       t, downloadFile, el, item,
       isLk, isExternalLink, isDirLike, isFileLike, isReadOnlyEscape);

const iconEl = appendedTo;
let clicked = false;
function click(detail) {
  let stopped = false;
  const event = {detail, stopPropagation() { stopped = true; stopPropagationCalls++; }};
  if (typeof iconEl.onclick === 'function') iconEl.onclick(event);
  if (!stopped) el.onclick(event);
}
click(1);
clicked = true;
if (params.name === 'double.pdf') click(2);
if (params.name === 'keyboard.pdf') {
  for (const key of ['Enter', ' ']) {
    iconEl.onkeydown({key, preventDefault() {}, stopPropagation() {}});
  }
}

console.log(JSON.stringify({
  iconHasHandler: !!(iconEl && typeof iconEl.onclick === 'function'),
  clicked, openCalls,
  downloadCalls,
  stopPropagationCalls,
  role: iconEl ? iconEl.attributes.role : null,
  tabindex: iconEl ? iconEl.attributes.tabindex : null,
  label: iconEl ? iconEl.attributes['aria-label'] : null,
  title: iconEl ? iconEl.title : null,
}));
"""
    )
    r = subprocess.run(
        [str(NODE), "-e", js],
        capture_output=True, text=True, timeout=30,
    )
    if r.returncode != 0:
        raise RuntimeError(f"node failed: {r.stderr}")
    return json.loads(r.stdout.strip().splitlines()[-1])


class TestIconDownloadBehavior:
    def test_file_icon_click_downloads(self):
        """Clicking a file row's icon calls downloadFile with the item path."""
        out = _run_node_vm(item_type="file", read_only_escape=False)
        assert out["iconHasHandler"] is True, (
            f"file row icon must have a click handler; got {out}"
        )
        assert out["downloadCalls"] == ["dir/foo.pdf"], (
            f"icon click must call downloadFile('dir/foo.pdf'); got {out}"
        )
        assert out["stopPropagationCalls"] == 1, (
            f"icon click must stopPropagation exactly once; got {out}"
        )
        assert out["title"] == "Download", (
            f"icon must carry the Download tooltip; got {out}"
        )

    def test_dir_icon_has_no_download_handler(self):
        """Directory rows keep the expand/collapse toggle — no download."""
        out = _run_node_vm(item_type="dir", read_only_escape=False)
        assert out["iconHasHandler"] is False, (
            f"directory icon must not bind a download handler; got {out}"
        )
        assert out["downloadCalls"] == [], (
            f"directory icon click must not download; got {out}"
        )

    def test_non_download_glyph_keeps_preview(self):
        for name in ('analysis.md', 'main.py', 'config.json', 'data.csv', 'photo.png'):
            out = _run_node_vm('file', False, name)
            assert out['iconHasHandler'] is False, (name, out)
            assert out['downloadCalls'] == [], (name, out)
            assert out['openCalls'] == 1, (name, out)

    def test_archive_glyph_downloads_without_preview(self):
        out = _run_node_vm('file', False, 'archive.zip')
        assert out['downloadCalls'] == ['dir/archive.zip']
        assert out['openCalls'] == 0

    def test_pdf_click_does_not_also_preview(self):
        out = _run_node_vm('file', False)
        assert out['openCalls'] == 0

    def test_double_click_only_downloads_once(self):
        out = _run_node_vm('file', False, 'double.pdf')
        assert out['downloadCalls'] == ['dir/double.pdf']
        assert out['openCalls'] == 0

    def test_keyboard_activation_has_accessible_label(self):
        out = _run_node_vm('file', False, 'keyboard.pdf')
        assert out['role'] == 'button'
        assert out['tabindex'] == '0'
        assert out['label'] == 'Download keyboard.pdf'
        assert out['downloadCalls'] == ['dir/keyboard.pdf'] * 3
        assert out['openCalls'] == 0

    def test_symlink_icon_keeps_original_navigation(self):
        out = _run_node_vm('symlink', False)
        assert out['iconHasHandler'] is False
        assert out['openCalls'] == 1
        assert out['downloadCalls'] == []

    def test_read_only_escape_icon_has_no_download_handler(self):
        """Read-only escape rows keep their gated navigation flow."""
        out = _run_node_vm(item_type="file", read_only_escape=True)
        assert out["iconHasHandler"] is False, (
            f"read-only escape icon must not bind a download handler; got {out}"
        )
        assert out["downloadCalls"] == [], (
            f"read-only escape icon click must not download (bypasses the "
            f"escape gate); got {out}"
        )
