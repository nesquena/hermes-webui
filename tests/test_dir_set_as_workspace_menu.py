"""Static-source pins for the Files-tree "Set as New Workspace" flow.

Feature: right-click a directory row in the Files panel → "Set as New
Workspace". The item resolves the directory to its absolute on-disk path via
`/api/file/path`, registers it with `/api/workspaces/add` (idempotent — a
duplicate answers 400 "Workspace already in list"), then asks whether to switch
the *current session* to it; a confirmed switch is delegated to
`switchToWorkspace()` (panels.js) so the busy guard, the `/api/session/update`
call and the tree/memory refresh stay in one place. Declining the switch does
not roll the registration back.

These are source-level pins: the menu wiring, guard ordering and copy keys
cannot be exercised faithfully without a real browser, but the patterns worth
locking are the menu placement/visibility conditions, the call order inside
`_setDirectoryAsWorkspace`, the presence of every new i18n key in every locale
block, and the touch long-press handler + its CSS state.
"""

from __future__ import annotations

import pathlib
import re

REPO = pathlib.Path(__file__).parent.parent
UI_JS = (REPO / "static" / "ui.js").read_text(encoding="utf-8")
I18N_JS = (REPO / "static" / "i18n.js").read_text(encoding="utf-8")
STYLE_CSS = (REPO / "static" / "style.css").read_text(encoding="utf-8")

NEW_KEYS = [
    "dir_set_as_space_item",
    "dir_set_as_space_exists",
    "dir_set_as_space_is_current",
    "dir_set_as_space_profile_mismatch",
    "switch_session_ws_title",
    "switch_session_ws_msg",
    "switch_session_ws_btn",
    "read_only_session_ws_hint",
]

# en + the 14 shipped locales (see LOCALES in static/i18n.js).
EXPECTED_LOCALE_BLOCKS = 15


def _slice_function(src: str, name: str) -> str:
    """Return the body of a top-level `function name(...){...}` by brace balance."""
    match = re.search(rf"function\s+{re.escape(name)}\b[^{{]*\{{", src)
    assert match, f"could not find function {name}()"
    start = match.start()
    depth = 0
    for i, ch in enumerate(src[start:], start=start):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return src[start : i + 1]
    raise AssertionError(f"unbalanced braces while slicing {name}()")


# ── Menu entry (static/ui.js → _showFileContextMenu) ─────────────────────────


class TestMenuItem:
    def test_item_registered_after_copy_relative_path(self):
        body = _slice_function(UI_JS, "_showFileContextMenu")
        assert "t('dir_set_as_space_item')" in body
        # Placed after the Copy-relative-path row and before the (dir-only)
        # Download-folder row, inside its own visual group.
        assert body.index("copyRelPathItem") < body.index("t('dir_set_as_space_item')")
        assert body.index("t('dir_set_as_space_item')") < body.index("t('download_folder')")

    def test_item_is_directory_only(self):
        body = _slice_function(UI_JS, "_showFileContextMenu")
        guard = re.search(
            r"if\s*\(\s*isDirLike\s*&&\s*!\s*isExternalLink\s*\)\s*\{",
            body,
        )
        assert guard, "the new item must be gated on `isDirLike && !isExternalLink`"

    def test_external_symlink_rows_carry_no_item(self):
        body = _slice_function(UI_JS, "_showFileContextMenu")
        assert "item.target_outside_workspace" in body, (
            "isExternalLink must be derived from item.target_outside_workspace"
        )

    def test_item_dispatches_to_set_directory_as_workspace(self):
        body = _slice_function(UI_JS, "_showFileContextMenu")
        assert re.search(
            r"_workspaceContextMenuItem\(\s*t\('dir_set_as_space_item'\)",
            body,
        )
        assert "_setDirectoryAsWorkspace(item)" in body

    def test_workspace_root_menu_keeps_no_such_item(self):
        body = _slice_function(UI_JS, "_showWorkspaceRootContextMenu")
        assert "dir_set_as_space_item" not in body
        assert "_setDirectoryAsWorkspace" not in body


# ── _setDirectoryAsWorkspace: flow, guards, no rollback ──────────────────────


def _set_ws_body() -> str:
    return _slice_function(UI_JS, "_setDirectoryAsWorkspace")


class TestSetDirectoryAsWorkspace:
    def test_requires_a_session_and_a_directory(self):
        body = _set_ws_body()
        assert "if(!S.session||!S.session.session_id)return;" in body
        assert "item.type==='symlink'&&item.is_dir" in body

    def test_resolves_path_server_side_before_registering(self):
        body = _set_ws_body()
        assert "/api/file/path" in body
        assert body.index("/api/file/path") < body.index("/api/workspaces/add"), (
            "the absolute path must be resolved before the register call"
        )

    def test_registers_via_existing_endpoint_and_refreshes_list(self):
        body = _set_ws_body()
        assert "/api/workspaces/add" in body
        assert "_workspaceList" in body
        assert "renderWorkspacesPanel(" in body
        assert "t('workspace_added')" in body

    def test_duplicate_registration_is_treated_as_success(self):
        body = _set_ws_body()
        assert "already in list" in body, (
            "the 400 'Workspace already in list' answer must be treated as the "
            "already-registered state and still offer the switch"
        )
        assert "t('dir_set_as_space_exists')" in body

    def test_failed_registration_reports_and_stops(self):
        body = _set_ws_body()
        # The non-duplicate error branch must return before any switch prompt.
        err_branch = body[body.index("already in list") :]
        assert "setStatus((" in err_branch
        assert re.search(r"setStatus\(\(.*?\);\s*return;\s*\}", err_branch, re.S), (
            "a non-duplicate registration failure must report and return"
        )

    def test_guard_order_read_only_busy_is_current_profile(self):
        body = _set_ws_body()
        i_read_only = body.index("read_only_session_ws_hint")
        i_busy = body.index("workspace_busy_switch")
        i_current = body.index("dir_set_as_space_is_current")
        i_profile = body.index("dir_set_as_space_profile_mismatch")
        i_dialog = body.index("showConfirmDialog")
        assert i_read_only < i_busy < i_current < i_profile < i_dialog, (
            "guards must run (and short-circuit) before the switch prompt"
        )

    def test_profile_guard_compares_session_and_active_profile(self):
        body = _set_ws_body()
        assert "S.session.profile" in body and "S.activeProfile" in body

    def test_confirm_dialog_uses_fixed_copy_and_no_danger(self):
        body = _set_ws_body()
        assert "t('switch_session_ws_title')" in body
        assert "t('switch_session_ws_msg', name)" in body
        assert "t('switch_session_ws_btn')" in body
        assert "danger:true" not in body, "the switch prompt is non-destructive"

    def test_declined_switch_keeps_registration_and_skips_switch(self):
        body = _set_ws_body()
        assert re.search(r"if\(!switchNow\)\s*return;", body)
        assert body.index("if(!switchNow) return;") < body.index("switchToWorkspace(absPath")

    def test_switch_reuses_switch_to_workspace(self):
        body = _set_ws_body()
        assert "switchToWorkspace(absPath" in body, (
            "the switch must delegate to switchToWorkspace() (panels.js) instead "
            "of re-implementing session/update + refresh"
        )
        # No direct session-mutation or list-mutation calls from this function.
        assert "api('/api/session/update'" not in body
        assert "/api/workspaces/remove" not in body, (
            "registration must never be rolled back"
        )


# ── Touch long-press in _renderTreeItems ─────────────────────────────────────


class TestTreeLongPress:
    def test_long_press_handlers_wired_on_rows(self):
        body = _slice_function(UI_JS, "_renderTreeItems")
        for evt in ("touchstart", "touchmove", "touchend", "touchcancel"):
            assert f"addEventListener('{evt}'" in body, f"missing {evt} handler"
        assert re.search(r"\},500\)", body), "long-press threshold must be 500ms"
        assert "'long-pressing'" in body
        assert re.search(r"if\(_lpHandled\)\{te\.preventDefault\(\);te\.stopPropagation\(\);\}", body), (
            "touchend must swallow the synthetic click after the menu opened"
        )
        # The armed timer must live in the module-level slot (not a per-row
        # closure) so a tree re-render can cancel it before the row is detached.
        assert re.search(r"_treeRowLongPress=\{el,token,timer:setTimeout\(", body), (
            "the armed timer must be reachable by _cancelTreeRowLongPress()"
        )

    def test_native_contextmenu_cancels_pending_timer(self):
        body = _slice_function(UI_JS, "_renderTreeItems")
        ctx = body[body.index("el.oncontextmenu") : body.index("el.ondragstart")]
        assert "_cancelTreeRowLongPress()" in ctx, (
            "the native contextmenu path must cancel a pending long-press timer "
            "so one gesture never opens two menus (Windows touch)"
        )

    def test_long_press_style_present(self):
        assert ".file-item.long-pressing{" in STYLE_CSS
        # Two rules mention the class now: the declaration and the reduced-motion
        # override. Pin the declaration by its content, not by being first.
        rules, idx = [], 0
        while (idx := STYLE_CSS.find(".file-item.long-pressing{", idx)) != -1:
            rules.append(STYLE_CSS[idx:][: STYLE_CSS[idx:].index("}")])
            idx += 1
        decl = next((r for r in rules if "var(--hover-bg)" in r), None)
        assert decl is not None, "the .long-pressing declaration rule is missing"
        assert "animation:session-long-press" in decl
        motion = STYLE_CSS[STYLE_CSS.index("@media (prefers-reduced-motion:reduce)"):]
        motion = motion[: motion.index("\n  }")]
        assert ".file-item.long-pressing" in motion, (
            "reduced-motion must neutralise the row long-press animation"
        )


# ── i18n keys in every locale block ──────────────────────────────────────────


class TestI18nKeys:
    def test_every_key_in_every_locale_block(self):
        for key in NEW_KEYS:
            count = len(re.findall(rf"^\s*{re.escape(key)}\s*:", I18N_JS, re.MULTILINE))
            assert count == EXPECTED_LOCALE_BLOCKS, (
                f"i18n key {key!r} appears {count} times; expected "
                f"{EXPECTED_LOCALE_BLOCKS} (one per locale block)"
            )

    def test_placeholder_keys_keep_their_placeholders(self):
        for key in ("dir_set_as_space_profile_mismatch", "switch_session_ws_msg"):
            values = re.findall(rf"^\s*{key}:\s*'(.*)',\s*$", I18N_JS, re.MULTILINE)
            assert values, f"no single-quoted values found for {key}"
            assert all("{0}" in v for v in values), (
                f"every locale's {key!r} must keep the {{0}} placeholder"
            )

    def test_menu_key_copy_is_localised(self):
        assert "dir_set_as_space_item: 'Set as New Workspace'" in I18N_JS
        assert "dir_set_as_space_item: '设为新工作区'" in I18N_JS
