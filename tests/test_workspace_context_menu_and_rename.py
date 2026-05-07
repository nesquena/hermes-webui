"""
Workspace context-menu hover and rename-dialog pre-fill regressions.

Two distinct bugs that were both shipped at the same time and only caught when
a user dogfooded the workspace panel:

(a) Workspace + session-list right-click context menu items had no visible
    hover state because they wrote `style.background = 'var(--hover)'`. The
    custom property `--hover` is undefined anywhere in the codebase. An
    undefined `var()` falls back to the property's initial value (transparent
    for `background`), so the hover state silently no-op'd. The defined
    variable is `--hover-bg` (`rgba(255,255,255,.06)`), used by every other
    hover state in the app — there's a one-letter typo that ate every
    context-menu hover.

(b) Right-click → Rename did not pre-fill the input with the current filename.
    `_inlineRenameFileItem` passed `defaultValue: item.name` to
    `showPromptDialog`, but the dialog's input setter reads `opts.value` only.
    The `defaultValue` parameter was silently dropped; only the placeholder
    showed (the "ghost" name the user described).

Run: /root/hermes-agent/venv/bin/python -m pytest tests/test_workspace_context_menu_and_rename.py -v
"""

from __future__ import annotations

import os
import re
import unittest


BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UI_JS = os.path.join(BASE_DIR, "static", "ui.js")
SESSIONS_JS = os.path.join(BASE_DIR, "static", "sessions.js")


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as fh:
        return fh.read()


# ---------------------------------------------------------------------------
# (a) Context-menu hover background — `--hover` was undefined; must use --hover-bg
# ---------------------------------------------------------------------------
class ContextMenuHoverBackgroundTests(unittest.TestCase):
    """Pin: no JS code path may set `style.background = 'var(--hover)'`.

    The variable is undefined; the resolved value is `transparent`, which gives
    no visible hover feedback. Use `var(--hover-bg)` (the actual variable used
    by every other hover state in the codebase).
    """

    def test_no_var_hover_in_ui_js(self):
        src = _read(UI_JS)
        # Match `var(--hover)` but NOT `var(--hover-bg)` / `var(--hover-2)` etc.
        # Negative lookahead handles the `-` case; we also bar `_` and word chars.
        bad = re.findall(r"var\(--hover\)(?![\w-])", src)
        self.assertEqual(
            bad, [],
            f"Found {len(bad)} `var(--hover)` reference(s) in static/ui.js. "
            "The variable `--hover` is undefined; this resolves to `transparent` "
            "and breaks visible hover state. Use `var(--hover-bg)` instead.",
        )

    def test_no_var_hover_in_sessions_js(self):
        src = _read(SESSIONS_JS)
        bad = re.findall(r"var\(--hover\)(?![\w-])", src)
        self.assertEqual(
            bad, [],
            f"Found {len(bad)} `var(--hover)` reference(s) in static/sessions.js. "
            "Use `var(--hover-bg)` (the defined variable).",
        )

    def test_file_context_menu_uses_var_hover_bg(self):
        """Affirmative pin on the file context menu in ui.js — every menu item
        builder (Rename, Reveal, Copy path, Delete) must use `var(--hover-bg)`."""
        src = _read(UI_JS)
        fn_match = re.search(
            r"function\s+_showFileContextMenu\b[^{]*\{",
            src,
        )
        self.assertIsNotNone(fn_match, "Could not find _showFileContextMenu()")
        # Slice from start of function until the matching closing brace at
        # column 0 (next top-level function). Cheap brace-balance.
        start = fn_match.start()
        depth = 0
        end = start
        for i, ch in enumerate(src[start:], start=start):
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        body = src[start:end]
        # Expect at least 4 hover assignments (one per menu item).
        hits = re.findall(r"\.style\.background\s*=\s*['\"]var\(--hover-bg\)['\"]", body)
        self.assertGreaterEqual(
            len(hits), 4,
            f"Expected ≥4 menu items to set background to var(--hover-bg) "
            f"(Rename, Reveal, Copy path, Delete). Found {len(hits)}.",
        )

    def test_session_context_menu_uses_var_hover_bg(self):
        """Affirmative pin on the project chip context menu in sessions.js."""
        src = _read(SESSIONS_JS)
        fn_match = re.search(
            r"function\s+_showProjectContextMenu\b[^{]*\{",
            src,
        )
        self.assertIsNotNone(fn_match, "Could not find _showProjectContextMenu()")
        start = fn_match.start()
        depth = 0
        end = start
        for i, ch in enumerate(src[start:], start=start):
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        body = src[start:end]
        hits = re.findall(r"\.style\.background\s*=\s*['\"]var\(--hover-bg\)['\"]", body)
        self.assertGreaterEqual(
            len(hits), 2,
            f"Expected ≥2 menu items to set background to var(--hover-bg) "
            f"in _showProjectContextMenu. Found {len(hits)}.",
        )


# ---------------------------------------------------------------------------
# (b) showPromptDialog pre-fill: must accept both `value` and `defaultValue`
# ---------------------------------------------------------------------------
class ShowPromptDialogPrefillTests(unittest.TestCase):
    """The rename dialog must pre-fill with the current filename (matches
    every native file manager) AND the dialog must accept `defaultValue` as an
    alias for `value` — the typo that caused the original bug is too easy to
    repeat with no API alias.
    """

    def setUp(self):
        self.src = _read(UI_JS)

    def _slice_show_prompt_dialog(self) -> str:
        """Return the body of `showPromptDialog(opts={}){ ... }` as a string."""
        # Anchor: the `function showPromptDialog` keyword. Skip past the
        # parameter list (which contains `opts={}` — its `{}` would fool a naive
        # brace counter), then balance braces from the function-body opener.
        kw = re.search(r"function\s+showPromptDialog\b", self.src)
        self.assertIsNotNone(kw, "Could not find showPromptDialog()")
        # Find the parameter-list parens — skip over them by parens balance.
        i = kw.end()
        # advance to the opening '('
        while i < len(self.src) and self.src[i] != "(":
            i += 1
        self.assertLess(i, len(self.src), "showPromptDialog: no opening paren")
        depth = 0
        while i < len(self.src):
            ch = self.src[i]
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    i += 1
                    break
            i += 1
        # Now skip whitespace to the function-body '{'.
        while i < len(self.src) and self.src[i] not in "{":
            i += 1
        self.assertLess(i, len(self.src), "showPromptDialog: no function-body brace")
        start = i
        depth = 0
        end = start
        for j, ch in enumerate(self.src[start:], start=start):
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end = j + 1
                    break
        return self.src[start:end]

    def test_show_prompt_dialog_accepts_default_value_alias(self):
        body = self._slice_show_prompt_dialog()
        # Must reference `opts.defaultValue` somewhere — the alias was the
        # backward-compatibility fix so future typos don't cause silent drops.
        self.assertIn(
            "opts.defaultValue", body,
            "showPromptDialog must accept `defaultValue` as an alias for "
            "`value` so callers using the standard HTMLInputElement.defaultValue "
            "param name pre-fill correctly (regression protection).",
        )
        # Must still reference `opts.value` — the canonical param.
        self.assertIn("opts.value", body)

    def test_show_prompt_dialog_supports_select_stem(self):
        """Stem selection (everything before the last '.') is what makes
        rename-with-pre-fill actually fast — user can immediately type the new
        basename without losing the extension. Without this, pre-fill plus a
        full-string select would force the user to type the extension every
        time."""
        body = self._slice_show_prompt_dialog()
        self.assertIn(
            "selectStem", body,
            "showPromptDialog should support `selectStem:true` to select the "
            "filename portion before the last '.' on focus (Finder-style "
            "rename UX).",
        )
        # Pin the actual stem-selection mechanic — must use lastIndexOf('.')
        # and setSelectionRange. Anything else is the wrong selection rule.
        self.assertRegex(
            body, r"lastIndexOf\(\s*['\"]\.['\"]\s*\)",
            "selectStem must use lastIndexOf('.') so 'a.b.c.d' selects 'a.b.c'.",
        )
        self.assertRegex(
            body, r"setSelectionRange\s*\(\s*0\s*,",
            "selectStem must use setSelectionRange(0, dot) to select the stem.",
        )

    def test_inline_rename_uses_value_and_select_stem(self):
        """The rename caller must (a) pre-fill the current name via `value:`
        and (b) ask for `selectStem:true` on files (so the extension survives)
        — these are the two legs of the user-visible fix."""
        m = re.search(
            r"async\s+function\s+_inlineRenameFileItem\b[^{]*\{",
            self.src,
        )
        self.assertIsNotNone(m, "Could not find _inlineRenameFileItem()")
        start = m.start()
        depth = 0
        end = start
        for i, ch in enumerate(self.src[start:], start=start):
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        body = self.src[start:end]
        # Must pass value:item.name (not defaultValue:item.name — the original bug).
        self.assertRegex(
            body,
            r"value\s*:\s*item\.name",
            "_inlineRenameFileItem must pass `value:item.name` to pre-fill "
            "the dialog input. (The original `defaultValue:item.name` was "
            "silently dropped because the dialog reads `opts.value`.)",
        )
        # Must opt into selectStem for files (not directories).
        self.assertIn(
            "selectStem", body,
            "_inlineRenameFileItem must pass selectStem:... so renaming "
            "'report.txt' selects 'report' and the user can immediately type "
            "the new basename while preserving the extension.",
        )


if __name__ == "__main__":
    unittest.main()
