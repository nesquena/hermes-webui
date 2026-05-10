"""Tests for #1710 — file-tree tooltip says "Double-click to rename" on folders too,
but folders don't rename on double-click; they navigate via loadDir(). The tooltip
is therefore misleading on directory rows.

Fix: gate the tooltip on `item.type !== 'dir'` so it appears only on files.
Folder rename is still reachable via the right-click context menu.
"""
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
UI_JS_PATH = REPO_ROOT / "static" / "ui.js"


def _read_ui_js() -> str:
    with open(UI_JS_PATH, encoding="utf-8") as f:
        return f.read()


def _name_block() -> str:
    """Source slice covering the file-tree row's name span construction."""
    src = _read_ui_js()
    start = src.find("// Name\n    const nameEl=document.createElement('span');")
    assert start >= 0, "name span construction marker not found in static/ui.js"
    end = src.find("el.appendChild(nameEl);", start)
    assert end >= 0, "el.appendChild(nameEl) not found after name span"
    return src[start:end]


class TestFolderTooltipGated:
    """The 'Double-click to rename' tooltip must only attach to files, not dirs."""

    def test_tooltip_assignment_is_guarded_by_item_type(self):
        block = _name_block()
        # We expect the tooltip line to be wrapped in an `if(item.type!=='dir')` guard.
        # The pre-fix shape was `nameEl.title=t('double_click_rename');` unconditionally.
        # Find every line that assigns nameEl.title and confirm at least one is gated.
        gated = "if(item.type!=='dir')nameEl.title=t('double_click_rename')"
        unguarded = "    nameEl.className='file-name';nameEl.textContent=item.name;nameEl.title=t('double_click_rename');"
        assert gated in block, (
            "tooltip assignment must be guarded by `if(item.type!=='dir')` so directories "
            "do not show the misleading 'Double-click to rename' hint (#1701)"
        )
        assert unguarded not in block, (
            "the pre-fix unguarded tooltip assignment is still present — folders will "
            "still show the misleading hint"
        )

    def test_dir_dblclick_still_navigates_not_renames(self):
        """Sanity: directory dblclick path is unchanged — must still call loadDir()."""
        block = _name_block()
        assert "if(item.type==='dir'){loadDir(item.path);return;}" in block, (
            "directory dblclick must still navigate (call loadDir); the rename-only "
            "tooltip gating depends on this contract being unchanged"
        )

    def test_files_still_get_tooltip(self):
        """Sanity: the tooltip text is still defined for files via the i18n key."""
        block = _name_block()
        assert "t('double_click_rename')" in block, (
            "tooltip i18n key must still be referenced — the gate hides it for dirs, "
            "not for files"
        )

    def test_i18n_key_still_defined_in_all_locales(self):
        """The i18n key must remain defined in every locale block in static/i18n.js."""
        i18n = (REPO_ROOT / "static" / "i18n.js").read_text(encoding="utf-8")
        # i18n.js has 9 locale blocks with the same key. Lock that the key still exists
        # at least 5 times (en, plus a quorum of locales) — exact count is i18n maintenance.
        count = i18n.count("double_click_rename:")
        assert count >= 5, (
            f"i18n key 'double_click_rename' should be defined in multiple locales; "
            f"found {count} occurrences — did this PR accidentally drop translations?"
        )
