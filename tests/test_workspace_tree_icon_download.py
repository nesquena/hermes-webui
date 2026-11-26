"""Regression tests — workspace tree download lives in the context menu, not the icon.

History: the original PR (#7810) bound click/keydown download handlers to the
leading file icon for PDF/DOWNLOAD_EXTS rows. The 2026-10-28 design review
(nesquena-hermes, review at head 35184264) found the *placement* wrong, not the
code: no mainstream explorer makes the leading type icon a distinct download
control, at rest nothing distinguishes the icon, on touch the 14x14 icon sits
inside the row's tap target next to a name that previews, and a PDF-heavy tree
doubles the Tab stops per row. The icon's download glyph for .pdf was the root
mismatch (fileIcon drew "download" while a click previewed).

Rework contract (same review, steps 1/2/4; step 3's trailing hover button is
deliberately not added — a new hit area near the filename/adjacent rows was
already rejected in an earlier round):

  * fileIcon() renders .pdf as a document glyph (file-text) like the other
    previewable types; DOWNLOAD_EXTS keep the download glyph (their whole row
    downloads via openFile's DOWNLOAD_EXTS routing — the glyph is honest);
  * _showFileContextMenu() offers a Download item for files next to the
    existing Download Folder item for directories, reusing the media_download
    key and downloadFile(item.path), gated to non-dir rows and off the
    read-only escape path;
  * the icon carries NO click/keydown handlers, no role="button", no
    tabindex, no .file-icon-download class — icon clicks bubble to the row
    (preview for previewable types, download for DOWNLOAD_EXTS);
  * the file NAME click keeps the #1707 debounce contract — untouched;
  * the PDF preview panel's Download button (#btnDownloadFile) still covers
    one-click downloads from preview.

2026-10-29 review (head 211c502c): the Download item must also stay off
display-only external symlinks (target_outside_workspace) — downloadFile()
builds an ordinary /api/file/raw URL that the raw resolver 404s for
unauthorized outside-workspace targets, so the item would be a dead action.
The menu tests below EXECUTE _showFileContextMenu against a modeled DOM and
click the Download action (the review rejected source-only assertions).

These tests static-analyze static/ui.js and drive the icon block and the
context menu through Node VMs.
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
UI_JS_PATH = REPO_ROOT / "static" / "ui.js"
STYLE_CSS_PATH = REPO_ROOT / "static" / "style.css"
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


def _file_icon_fn() -> str:
    src = _read(UI_JS_PATH)
    m = re.search(r"function fileIcon\(name, type\)\{.*?\n\}", src, re.S)
    assert m, "fileIcon() not found in static/ui.js"
    return m.group(0)


def _context_menu_fn() -> str:
    src = _read(UI_JS_PATH)
    start = src.index("function _showFileContextMenu(e, item){")
    end = src.index("async function _inlineRenameFileItem(item){", start)
    # The menu clamps itself through the shared helper; run the real one.
    return _clamp_menu_fn() + "\n" + src[start:end]


def _clamp_menu_fn() -> str:
    src = _read(UI_JS_PATH)
    start = src.index("function _clampContextMenuToViewport(menu){")
    end = src.index("function _showWorkspaceRootContextMenu(e){", start)
    return src[start:end]


def _root_menu_fn() -> str:
    src = _read(UI_JS_PATH)
    start = src.index("function _showWorkspaceRootContextMenu(e){")
    end = src.index("// Track expanded directories for tree view", start)
    return src[start:end]


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


class TestIconStaysPlain:
    """The leading icon is a type glyph only — no separate download control."""

    def test_icon_block_binds_no_click_handler(self):
        block = _icon_block()
        assert "iconEl.onclick" not in block, (
            "the file icon must not bind its own click handler — icon clicks "
            "bubble to the row (2026-10-28 review: no explorer makes the "
            "leading type icon a distinct download control)"
        )

    def test_icon_block_binds_no_keydown_handler(self):
        block = _icon_block()
        assert "iconEl.onkeydown" not in block, (
            "the file icon must not bind keydown — a focusable icon doubles "
            "the Tab stops per row (review finding)"
        )

    def test_icon_block_sets_no_button_role_or_tabindex(self):
        block = _icon_block()
        for banned in ("setAttribute('role'", "setAttribute('tabindex'",
                       "file-icon-download", "aria-label"):
            assert banned not in block, (
                f"icon block must not carry {banned!r} — the icon is a plain "
                f"type glyph, not an interactive control"
            )

    def test_file_icon_download_class_absent_from_css(self):
        css = _read(STYLE_CSS_PATH)
        assert ".file-icon-download" not in css, (
            ".file-icon-download rules must be gone — the class was dropped "
            "with the icon handlers (any effective hit-area overlap near the "
            "filename was rejected in review)"
        )


class TestFileIconGlyphs:
    """fileIcon() must not promise download for previewable types."""

    def test_pdf_renders_document_glyph(self):
        fn = _file_icon_fn()
        assert ".pdf" in fn and "file-text" in fn, (
            "fileIcon() must map .pdf to a document glyph (file-text)"
        )
        pdf_line = next(line for line in fn.splitlines() if "'.pdf'" in line or '".pdf"' in line)
        assert "download" not in pdf_line, (
            f".pdf must not render the download glyph — that mismatch was the "
            f"root cause of #7810; got: {pdf_line.strip()}"
        )

    def test_download_exts_keep_download_glyph(self):
        fn = _file_icon_fn()
        assert "DOWNLOAD_EXTS.has(e)) return li('download'" in fn, (
            "DOWNLOAD_EXTS rows keep the download glyph — their whole row "
            "downloads (openFile routes DOWNLOAD_EXTS to downloadFile), so "
            "the glyph is honest"
        )


class TestContextMenuDownloadItem:
    """Files get a Download item in the context menu, next to Download Folder."""

    def test_file_download_item_present(self):
        menu = _context_menu_fn()
        assert "downloadFile(item.path)" in menu, (
            "file context menu must offer Download via downloadFile(item.path)"
        )
        assert "t('media_download')" in menu, (
            "file Download item must reuse the existing media_download label key"
        )

    def test_file_download_item_gated_to_non_dir_rows(self):
        menu = _context_menu_fn()
        assert "!isDirLike" in menu, (
            "the file Download item must not appear for directory rows — "
            "directories keep the existing Download Folder action"
        )

    def test_file_download_item_off_read_only_escape_path(self):
        menu = _context_menu_fn()
        assert re.search(
            r"if\(!isDirLike && !isReadOnlyEscape && !isDisplayOnlyExternalLink\)", menu
        ), (
            "the file Download item must stay off the read-only escape path "
            "(authorized external symlinks keep their gated navigation flow) "
            "and off display-only external links (2026-10-29 review P2)"
        )

    def test_file_download_item_excludes_display_only_external_links(self):
        menu = _context_menu_fn()
        assert "isDisplayOnlyExternalLink" in menu, (
            "the file Download gate must exclude display-only external symlinks "
            "(target_outside_workspace) — downloadFile() would build an ordinary "
            "/api/file/raw URL the raw resolver 404s for those targets"
        )

    def test_download_folder_item_unchanged(self):
        menu = _context_menu_fn()
        assert "t('download_folder')" in menu, (
            "the existing Download Folder action for directories must stay"
        )

    def test_downloadfile_guarded(self):
        menu = _context_menu_fn()
        assert "typeof downloadFile==='function'" in menu, (
            "the menu item must guard downloadFile — it lives in "
            "static/workspace.js, a sibling global script"
        )


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
    """Execute the icon block with mocked DOM/globals and click the icon.

    The row mock (el.onclick) reproduces the real row routing: openFile
    previews, except DOWNLOAD_EXTS which download (static/workspace.js).
    """
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
let openCalls = 0;
let dirToggles = 0;
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
const fileIcon = (name) => ['.zip'].includes(fileExt(name)) ? 'download' : 'file-text';
const t = (key) => ({media_download: 'Download'}[key] || key);
const downloadFile = (path) => { downloadCalls.push(path); };
// Row mock mirrors the real chain: row click → openFile (DOWNLOAD_EXTS
// download, everything else previews); directories toggle instead.
const el = {
  appendChild(child) { appendedTo = child; },
  onclick() {
    if (isDirLike) { dirToggles++; return; }
    if (DOWNLOAD_EXTS.has(fileExt(item.name))) downloadFile(item.path);
    else openCalls++;
  },
};
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
let stopped = false;
const event = {detail: 1, stopPropagation() { stopped = true; }};
// Icon click with no bound handler bubbles to the row's click handler,
// exactly like a real DOM click on a handler-less child.
if (typeof iconEl.onclick === 'function') iconEl.onclick(event);
if (!stopped) el.onclick(event);

console.log(JSON.stringify({
  iconHasHandler: !!(iconEl && typeof iconEl.onclick === 'function'),
  openCalls,
  dirToggles,
  downloadCalls,
  role: iconEl ? (iconEl.attributes.role ?? null) : null,
  tabindex: iconEl ? (iconEl.attributes.tabindex ?? null) : null,
  title: iconEl ? (iconEl.title ?? null) : null,
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


class TestIconClickBubblesToRow:
    def test_icon_is_plain_glyph_everywhere(self):
        """No file type binds an icon handler; the icon stays a type glyph."""
        for name in ('foo.pdf', 'analysis.md', 'main.py', 'config.json',
                     'data.csv', 'photo.png', 'archive.zip'):
            out = _run_node_vm('file', False, name)
            assert out["iconHasHandler"] is False, (name, out)
            assert out["role"] is None and out["tabindex"] is None, (name, out)
            assert out["title"] == "", (name, out)

    def test_pdf_icon_click_bubbles_to_preview(self):
        out = _run_node_vm('file', False, 'foo.pdf')
        assert out["openCalls"] == 1, (
            f"pdf icon click must bubble to the row's preview handler; got {out}"
        )
        assert out["downloadCalls"] == [], (
            f"pdf icon click must not download; got {out}"
        )

    def test_non_download_glyphs_bubble_to_preview(self):
        for name in ('analysis.md', 'main.py', 'config.json', 'data.csv', 'photo.png'):
            out = _run_node_vm('file', False, name)
            assert out["openCalls"] == 1, (name, out)
            assert out["downloadCalls"] == [], (name, out)

    def test_archive_row_still_downloads_via_row_routing(self):
        """DOWNLOAD_EXTS rows download through the row's openFile routing —
        unchanged by the rework; the download glyph stays honest."""
        out = _run_node_vm('file', False, 'archive.zip')
        assert out["downloadCalls"] == ['dir/archive.zip'], (
            f"zip row click (icon included) must still download; got {out}"
        )
        assert out["openCalls"] == 0

    def test_dir_icon_has_no_handler(self):
        out = _run_node_vm(item_type="dir", read_only_escape=False)
        assert out["iconHasHandler"] is False
        assert out["downloadCalls"] == [] and out["openCalls"] == 0, (
            f"directory icon click must neither download nor preview; got {out}"
        )
        assert out["dirToggles"] == 1, (
            f"directory icon click must still bubble to the row's toggle; got {out}"
        )

    def test_symlink_icon_keeps_original_navigation(self):
        out = _run_node_vm('symlink', False)
        assert out["iconHasHandler"] is False
        assert out["openCalls"] == 1
        assert out["downloadCalls"] == []

    def test_read_only_escape_icon_has_no_handler(self):
        out = _run_node_vm(item_type="file", read_only_escape=True)
        assert out["iconHasHandler"] is False
        assert out["downloadCalls"] == []


# ── Behavioral tests: execute _showFileContextMenu and click items ────────────


def _menu_vm(item: dict, escape_grant: bool = False, menu_rect: dict | None = None) -> dict:
    """Execute the real _showFileContextMenu() source against a modeled DOM.

    The mock captures the menu appended to document.body, and (optionally)
    clicks the file Download item so the action itself runs — the 2026-10-29
    review asked for execution of the new menu action, not source assertions.
    ``menu_rect`` models the menu's rendered bounds for the post-append
    viewport clamp (real layout lives in the browser gate).
    """
    payload = {"fnSource": _context_menu_fn(), "item": item, "escapeGrant": escape_grant}
    if menu_rect is not None:
        payload["menuRect"] = menu_rect
    js = (
        "const params = " + json.dumps(payload) + ";\n"
        + r"""
let menus = [];
let created = 0;
const downloadCalls = [];
const document = {
  body: { appendChild(el) { menus.push(el); } },
  querySelectorAll() { return []; },
  createElement(tag) {
    // The menu is the first element _showFileContextMenu creates; its rect is
    // injectable so tests can model below-viewport geometry. The default
    // neutral rect keeps the post-append viewport clamp a no-op here (real
    // layout is covered by the browser gate).
    const el = {
      tagName: tag.toUpperCase(), className: '', textContent: '',
      style: {}, children: [], removed: false,
      __rect: (created++ === 0 && params.menuRect) ? params.menuRect
              : { top: 12, left: 12, right: 13, bottom: 13, width: 1, height: 1 },
      getBoundingClientRect() { return this.__rect; },
      appendChild(c) { this.children.push(c); },
      remove() { this.removed = true; },
      onclick: null, onmouseenter: null, onmouseleave: null,
    };
    return el;
  },
  addEventListener() {}, removeEventListener() {},
};
const window = { innerWidth: 1920, innerHeight: 1080 };
// Identity label map: menu items are matched by their t() key.
const t = (key) => ({ media_download: 'Download', download_folder: 'Download Folder' }[key] || key);
const _workspaceContextMenuItem = (label, fn) => ({ textContent: label, _action: fn });
const _workspaceParentDir = (p) => 'dir';
const _workspaceEscapeGrantForPath = () => (params.escapeGrant ? { path: params.item.path } : null);
const downloadFile = (path) => { downloadCalls.push(path); };
const setTimeout = () => {};  // do not wire the dismiss listener

const runner = new Function(
  'document', 'window', 't', '_workspaceContextMenuItem', '_workspaceParentDir',
  '_workspaceEscapeGrantForPath', 'downloadFile', 'setTimeout', 'event', 'item',
  params.fnSource + '\n_showFileContextMenu(event, item);'
);
runner(document, window, t, _workspaceContextMenuItem, _workspaceParentDir,
       _workspaceEscapeGrantForPath, downloadFile, setTimeout,
       { clientX: 100, clientY: 100 }, params.item);

const menu = menus[0];
if (!menu) throw new Error('no context menu appended to document.body');
const labels = menu.children.map((c) => c.textContent);
const dlItem = menu.children.find((c) => c.textContent === t('media_download')) || null;
let clickedDownload = false;
if (dlItem && typeof dlItem.onclick === 'function') { dlItem.onclick(); clickedDownload = true; }

console.log(JSON.stringify({
  labels,
  menuRemoved: menu.removed,
  downloadCalls,
  clickedDownload,
  style: {
    top: menu.style.top || '',
    left: menu.style.left || '',
    maxHeight: menu.style.maxHeight || '',
    overflowY: menu.style.overflowY || '',
  },
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


class TestContextMenuDownloadActionExecuted:
    """Behavioral contract from the 2026-10-29 review (PRR at head 211c502c):

    the file Download action must NOT be offered for display-only external
    symlinks (target outside the workspace — downloadFile() builds an ordinary
    /api/file/raw URL the raw resolver 404s for those targets), while ordinary
    files and internal symlinks keep a working Download item whose click really
    calls downloadFile(item.path).
    """

    EXTERNAL_LINK = {
        "name": "receipt-link.pdf", "path": "receipt-link.pdf",
        "type": "symlink", "is_dir": False, "target_outside_workspace": True,
    }
    INTERNAL_LINK = {
        "name": "notes-link.txt", "path": "notes-link.txt", "type": "symlink",
        "target": "/workspace/real/notes.txt", "is_dir": False,
        "target_outside_workspace": False,
    }
    ORDINARY_FILE = {
        "name": "report.pdf", "path": "report.pdf", "type": "file", "is_dir": False,
    }

    def test_external_symlink_row_offers_no_download(self):
        out = _menu_vm(self.EXTERNAL_LINK)
        assert "Download" not in out["labels"], (
            f"display-only external symlink must not get a Download item — "
            f"downloadFile() would hit a 404 raw URL; menu items: {out['labels']}"
        )
        assert out["downloadCalls"] == [], out
        # The row keeps its other (working) file actions.
        assert "rename_title" in out["labels"], (
            f"external-link row must keep its regular menu items; got {out['labels']}"
        )
        assert not out["clickedDownload"], out

    def test_external_symlink_with_escape_grant_stays_fully_gated(self):
        """Pre-existing contract: an authorized (read-only escape) row keeps the
        gated navigation flow — no Download, no destructive/authoring items."""
        out = _menu_vm(self.EXTERNAL_LINK, escape_grant=True)
        assert "Download" not in out["labels"], out
        assert "rename_title" not in out["labels"], (
            f"escape-granted row must keep the read-only menu shape; got {out['labels']}"
        )
        assert out["downloadCalls"] == [], out

    def test_ordinary_file_download_click_downloads(self):
        out = _menu_vm(self.ORDINARY_FILE)
        assert out["clickedDownload"] is True, (
            f"ordinary file must offer a clickable Download item; got {out}"
        )
        assert out["downloadCalls"] == ["report.pdf"], (
            f"clicking Download must call downloadFile(item.path); got {out}"
        )
        assert out["menuRemoved"] is True, (
            "the menu must close before the download action runs"
        )

    def test_internal_symlink_download_click_downloads(self):
        out = _menu_vm(self.INTERNAL_LINK)
        assert out["clickedDownload"] is True, (
            f"internal symlink must keep a clickable Download item; got {out}"
        )
        assert out["downloadCalls"] == ["notes-link.txt"], out

    def test_dir_row_keeps_download_folder_only(self):
        out = _menu_vm({"name": "assets", "path": "assets", "type": "dir", "is_dir": True})
        assert "Download Folder" in out["labels"], (
            f"directories keep the existing Download Folder action; got {out['labels']}"
        )
        assert "Download" not in out["labels"], (
            f"directory rows must not gain the file Download item; got {out['labels']}"
        )
        assert out["downloadCalls"] == [], out


class TestMenuClampsToViewport:
    """2026-11-20 gate RED: placement used a fixed pre-append height guess, so
    a pointer near the lower edge left the last rows (Delete) below the fold
    once Download made the menu taller. The rendered menu must be measured
    after insertion and clamped to the actual viewport, scrolling when it
    cannot fit. Real-pointer layout evidence lives in the browser gate
    (tests/browser_workspace_icon_download_geometry.py); here the modeled DOM
    executes the same production clamp with injected bounds."""

    def test_lower_edge_menu_is_measured_and_clamped(self):
        # Coherent rendered geometry: top 800 + height 390 → bottom 1190, past
        # 1072 (1080-8). The pre-append heuristic kept top at clientY=100
        # (100+100<1080), but the measured menu overflows, so the clamp moves
        # the top so the bottom lands at vh-8 (1080-8-390=682). Height still
        # fits the viewport → no scroll machinery.
        out = _menu_vm(
            {"name": "f.txt", "path": "f.txt", "type": "file", "is_dir": False},
            menu_rect={"top": 800, "left": 100, "right": 260, "bottom": 1190, "width": 160, "height": 390},
        )
        assert out["style"]["top"] == "682px", (
            f"menu must clamp its measured bottom inside the viewport "
            f"(1080-8-390=682); got top={out['style']['top']!r}"
        )
        assert out["style"]["maxHeight"] == "" and out["style"]["overflowY"] == "", (
            f"fitting menu must not gain scroll styling; got {out['style']}"
        )

    def test_viewport_taller_than_menu_pins_edge_and_scrolls(self):
        # Height 1200 > maxH 1064 (1080-2*8): pin to the top edge and scroll.
        out = _menu_vm(
            {"name": "f.txt", "path": "f.txt", "type": "file", "is_dir": False},
            menu_rect={"top": 100, "left": 100, "right": 260, "bottom": 1300, "width": 160, "height": 1200},
        )
        assert out["style"]["top"] == "8px", out["style"]
        assert out["style"]["maxHeight"] == "1064px", out["style"]
        assert out["style"]["overflowY"] == "auto", out["style"]


def _root_menu_vm(menu_rect: dict, vw: int, vh: int, client: tuple) -> dict:
    """Execute the real _showWorkspaceRootContextMenu() (and the shared clamp it
    calls) against a modeled DOM whose menu has the injected rendered bounds."""
    payload = {"fnSource": _clamp_menu_fn() + "\n" + _root_menu_fn(), "menuRect": menu_rect,
               "vw": vw, "vh": vh, "cx": client[0], "cy": client[1]}
    js = (
        "const params = " + json.dumps(payload) + ";\n"
        + r"""
let menus = [];
let created = 0;
const document = {
  body: { appendChild(el) { menus.push(el); } },
  querySelectorAll() { return []; },
  createElement(tag) {
    const el = {
      tagName: tag.toUpperCase(), className: '', style: {}, children: [],
      __rect: created++ === 0 ? params.menuRect : { top: 0, left: 0, right: 0, bottom: 0, width: 0, height: 0 },
      getBoundingClientRect() { return this.__rect; },
      appendChild(c) { this.children.push(c); },
      remove() {},
    };
    return el;
  },
  addEventListener() {}, removeEventListener() {},
};
const window = { innerWidth: params.vw, innerHeight: params.vh };
const t = (key) => key;
const _workspaceContextMenuItem = (label, fn) => ({ textContent: label, _action: fn });
const setTimeout = () => {};
const runner = new Function('document', 'window', 't', '_workspaceContextMenuItem', 'setTimeout', 'event',
  params.fnSource + '\n_showWorkspaceRootContextMenu(event);');
runner(document, window, t, _workspaceContextMenuItem, setTimeout, { clientX: params.cx, clientY: params.cy });
const menu = menus[0];
if (!menu) throw new Error('no root menu appended');
console.log(JSON.stringify({ top: menu.style.top || '', left: menu.style.left || '',
  maxHeight: menu.style.maxHeight || '', overflowY: menu.style.overflowY || '' }));
"""
    )
    r = subprocess.run([str(NODE), "-e", js], capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        raise RuntimeError(f"node failed: {r.stderr}")
    return json.loads(r.stdout.strip().splitlines()[-1])


class TestRootMenuClampsToViewport:
    """#7810 release gate: on touch screens the 44px rows make the workspace
    root menu taller (about 250px), and the root menu had no measured clamp,
    so on a short landscape phone its last action (Copy file path) sat off
    screen. It must share the file menu's clamp."""

    def test_root_menu_taller_than_viewport_pins_and_scrolls(self):
        # 480x215 viewport, 250px menu opened at y=150: the pre-append guess
        # puts the top at 70, so the bottom (320) is far below 215.
        out = _root_menu_vm({"top": 70, "left": 100, "right": 270, "bottom": 320, "width": 170, "height": 250},
                            480, 215, (100, 150))
        assert out["top"] == "8px" and out["maxHeight"] == "199px" and out["overflowY"] == "auto", (
            f"root menu taller than the viewport must pin to the edge and scroll; got {out}")

    def test_root_menu_near_lower_edge_moves_up(self):
        out = _root_menu_vm({"top": 700, "left": 100, "right": 270, "bottom": 950, "width": 170, "height": 250},
                            1366, 768, (100, 700))
        assert out["top"] == "510px" and out["maxHeight"] == "", (
            f"root menu must clamp its measured bottom inside the viewport (768-8-250=510); got {out}")
