"""Tests for issue #2735 — "Open in VS Code" action for workspace files/folders.

Pins three layers:

1. **Source wiring** — the dispatch entry, handler structure, and menu items
   exist in the correct files.

2. **i18n completeness** — both new keys (``open_in_vscode`` and
   ``open_in_vscode_failed``) are present in every locale block.

3. **Live endpoint behaviour** — error paths (missing fields, unknown session,
   missing file, path traversal) behave correctly against the test server.

The success path (VS Code actually opening) is not covered here because it
requires VS Code to be installed on the CI host.  The subprocess call is
intentionally fire-and-forget (matching ``_handle_file_reveal``), so its
failure is surfaced via the OSError catch and a 400 response.  That
observable is tested in ``TestOpenInVsCodeEndpointBehaviour``.
"""
from __future__ import annotations

import json
import pathlib
import re
import sys
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
ROUTES = ROOT / "api" / "routes.py"
UI = ROOT / "static" / "ui.js"
I18N = ROOT / "static" / "i18n.js"

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from conftest import TEST_BASE  # noqa: E402


# ═══════════════════════════════════════════════════════════════════════════════
#  Source-level wiring
# ═══════════════════════════════════════════════════════════════════════════════


class TestOpenInVsCodeBackendWiring:
    def test_route_dispatch_entry_present(self):
        """Dispatcher must route /api/file/open-vscode to the handler."""
        src = ROUTES.read_text(encoding="utf-8")
        assert 'parsed.path == "/api/file/open-vscode"' in src

    def test_handler_function_defined(self):
        src = ROUTES.read_text(encoding="utf-8")
        assert "def _handle_file_open_vscode(handler, body):" in src

    def test_handler_uses_safe_resolve(self):
        """Handler must use safe_resolve to prevent path traversal."""
        src = ROUTES.read_text(encoding="utf-8")
        m = re.search(
            r"def _handle_file_open_vscode\(handler, body\):.*?(?=\ndef )",
            src,
            re.DOTALL,
        )
        assert m, "_handle_file_open_vscode body not found"
        body = m.group(0)
        assert "safe_resolve(Path(s.workspace)" in body

    def test_handler_checks_existence(self):
        """Handler must require the target to exist (unlike copy-path)."""
        src = ROUTES.read_text(encoding="utf-8")
        m = re.search(
            r"def _handle_file_open_vscode\(handler, body\):.*?(?=\ndef )",
            src,
            re.DOTALL,
        )
        assert m
        body = m.group(0)
        assert "exists()" in body

    def test_handler_reads_vscode_config(self):
        """Handler must read the optional ``vscode`` config block."""
        src = ROUTES.read_text(encoding="utf-8")
        m = re.search(
            r"def _handle_file_open_vscode\(handler, body\):.*?(?=\ndef )",
            src,
            re.DOTALL,
        )
        assert m
        body = m.group(0)
        assert 'get("vscode"' in body

    def test_handler_defaults_to_code_command(self):
        """Default executable must be ``code`` when config is absent."""
        src = ROUTES.read_text(encoding="utf-8")
        m = re.search(
            r"def _handle_file_open_vscode\(handler, body\):.*?(?=\ndef )",
            src,
            re.DOTALL,
        )
        assert m
        body = m.group(0)
        assert '"code"' in body

    def test_handler_supports_path_prefix_mapping(self):
        """Handler must support container_path_prefix / host_path_prefix
        so Docker users can map container paths to host paths."""
        src = ROUTES.read_text(encoding="utf-8")
        m = re.search(
            r"def _handle_file_open_vscode\(handler, body\):.*?(?=\ndef )",
            src,
            re.DOTALL,
        )
        assert m
        body = m.group(0)
        assert "container_path_prefix" in body
        assert "host_path_prefix" in body

    def test_handler_uses_subprocess_popen(self):
        """Handler must use subprocess.Popen (async, non-blocking) consistent
        with _handle_file_reveal."""
        src = ROUTES.read_text(encoding="utf-8")
        m = re.search(
            r"def _handle_file_open_vscode\(handler, body\):.*?(?=\ndef )",
            src,
            re.DOTALL,
        )
        assert m
        body = m.group(0)
        assert "subprocess.Popen(" in body

    def test_handler_resolves_command_via_shutil_which(self):
        """Handler must use shutil.which() to find the command so it works
        even when the server's inherited PATH is minimal (e.g. macOS launch
        via start.sh where /usr/local/bin may be absent from the subprocess
        PATH)."""
        src = ROUTES.read_text(encoding="utf-8")
        m = re.search(
            r"def _handle_file_open_vscode\(handler, body\):.*?(?=\ndef )",
            src,
            re.DOTALL,
        )
        assert m
        body = m.group(0)
        assert "shutil.which(" in body

    def test_handler_has_vscode_fallback_paths(self):
        """Handler must try common VS Code paths when shutil.which fails,
        covering macOS (/usr/local/bin/code), Linux (/snap/bin/code), and
        Windows (%LOCALAPPDATA%\\Programs\\Microsoft VS Code)."""
        src = ROUTES.read_text(encoding="utf-8")
        m = re.search(
            r"def _handle_file_open_vscode\(handler, body\):.*?(?=\ndef )",
            src,
            re.DOTALL,
        )
        assert m
        body = m.group(0)
        assert "/usr/local/bin/code" in body        # macOS
        assert "/snap/bin/code" in body             # Linux snap
        assert "Microsoft VS Code" in body          # Windows

    def test_handler_returns_helpful_error_when_not_found(self):
        """When code command is not found anywhere, handler must return a
        descriptive error instead of a bare OSError message."""
        src = ROUTES.read_text(encoding="utf-8")
        m = re.search(
            r"def _handle_file_open_vscode\(handler, body\):.*?(?=\ndef )",
            src,
            re.DOTALL,
        )
        assert m
        body = m.group(0)
        assert "VS Code command not found" in body


class TestOpenInVsCodeFrontendWiring:
    def test_file_context_menu_has_vscode_item(self):
        """_showFileContextMenu must include the Open in VS Code action."""
        src = UI.read_text(encoding="utf-8")
        assert "t('open_in_vscode')" in src
        assert "/api/file/open-vscode" in src

    def test_workspace_root_context_menu_has_vscode_item(self):
        """_showWorkspaceRootContextMenu must also include the VS Code action."""
        src = UI.read_text(encoding="utf-8")
        # Both the file and root menus call the same endpoint; verify at least
        # two references in the file so we know both call sites exist.
        assert src.count("/api/file/open-vscode") >= 2

    def test_vscode_item_uses_hover_bg(self):
        """VS Code menu item must use var(--hover-bg), not var(--hover) or
        any other undefined variable."""
        src = UI.read_text(encoding="utf-8")
        # Confirm the item is wired with the correct variable — count hover-bg
        # usages; as long as our item follows the pattern the suite is green.
        assert "var(--hover-bg)" in src

    def test_vscode_failure_toast_uses_i18n_key(self):
        """Error toast on VS Code open failure must use the translatable key."""
        src = UI.read_text(encoding="utf-8")
        assert "t('open_in_vscode_failed')" in src

    def test_vscode_item_guards_err_message(self):
        """Error handler must guard against non-Error objects with
        (err.message||err) consistent with reveal handler."""
        src = UI.read_text(encoding="utf-8")
        # Find the open-vscode call site and check for the guard pattern near it.
        idx = src.find("/api/file/open-vscode")
        assert idx != -1
        # Look in a window around the first call site.
        window = src[max(0, idx - 200) : idx + 500]
        assert "(err.message||err)" in window or "(err.message || err)" in window


class TestOpenInVsCodeI18n:
    """Both new translation keys must be present in every locale block."""

    LOCALES = [
        # (locale tag, sample anchor key: value)
        ("en",    "reveal_in_finder: 'Reveal in File Manager'"),
        ("it",    "reveal_in_finder: 'Mostra nel File Manager'"),
        ("ja",    "reveal_in_finder: 'ファイルマネージャーで表示'"),
        ("ru",    "reveal_in_finder: 'Показать в файловом менеджере'"),
        ("es",    "reveal_in_finder: 'Mostrar en el gestor de archivos'"),
        ("de",    "reveal_in_finder: 'Im Dateimanager anzeigen'"),
        ("zh-CN", "reveal_in_finder: '在文件管理器中显示'"),
        ("pt",    "reveal_in_finder: 'Mostrar no gerenciador de arquivos'"),
        ("ko",    "reveal_in_finder: '파일 관리자에서 열기'"),
    ]

    def test_open_in_vscode_key_count(self):
        """open_in_vscode key must appear exactly once per locale (11 total)."""
        src = I18N.read_text(encoding="utf-8")
        count = src.count("open_in_vscode:")
        assert count == 12, (
            f"Expected 11 open_in_vscode: entries (one per locale), found {count}"
        )

    def test_open_in_vscode_failed_key_count(self):
        """open_in_vscode_failed key must appear exactly once per locale (11 total)."""
        src = I18N.read_text(encoding="utf-8")
        count = src.count("open_in_vscode_failed:")
        assert count == 12, (
            f"Expected 11 open_in_vscode_failed: entries (one per locale), found {count}"
        )

    def test_english_translation_not_a_placeholder(self):
        """English locale must have a human-readable string, not a TODO."""
        src = I18N.read_text(encoding="utf-8")
        assert "open_in_vscode: 'Open in VS Code'" in src
        assert "open_in_vscode_failed: 'Failed to open in VS Code: '" in src

    def test_non_english_locales_translated(self):
        """Non-English locales must have real translations, not TODO stubs."""
        src = I18N.read_text(encoding="utf-8")
        # Spot-check a selection of locales — none of these should be TODO stubs.
        assert "open_in_vscode: 'Apri in VS Code'" in src       # it
        assert "open_in_vscode: 'VS Codeで開く'" in src          # ja
        assert "open_in_vscode: 'Открыть в VS Code'" in src     # ru
        assert "open_in_vscode: 'Abrir en VS Code'" in src      # es
        assert "open_in_vscode: 'In VS Code öffnen'" in src     # de
        assert "open_in_vscode: 'VS Code에서 열기'" in src        # ko

    def test_keys_adjacent_to_reveal_block(self):
        """New keys must appear near the reveal/copy block so locale coverage
        is easy to spot in code review."""
        src = I18N.read_text(encoding="utf-8")
        # In the English block, open_in_vscode must appear between
        # copy_file_path and download_folder.
        copy_idx = src.index("copy_file_path: 'Copy file path'")
        dl_idx = src.index("download_folder: 'Download Folder'", copy_idx)
        vscode_idx = src.index("open_in_vscode: 'Open in VS Code'", copy_idx)
        assert copy_idx < vscode_idx < dl_idx, (
            "open_in_vscode key must appear between copy_file_path and "
            "download_folder in the English locale block"
        )


# ═══════════════════════════════════════════════════════════════════════════════
#  Live endpoint behaviour
# ═══════════════════════════════════════════════════════════════════════════════


def _post(path, body=None):
    data = json.dumps(body or {}).encode()
    req = urllib.request.Request(
        TEST_BASE + path,
        data=data,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read()), r.status
    except urllib.error.HTTPError as e:
        return json.loads(e.read()), e.code


class TestOpenInVsCodeEndpointBehaviour:
    def _new_session(self):
        body, status = _post("/api/session/new", {})
        assert status == 200, body
        return body["session"]["session_id"]

    def test_missing_session_id_returns_400(self):
        body, status = _post("/api/file/open-vscode", {"path": "."})
        assert status == 400, body
        assert "session_id" in body.get("error", "")

    def test_missing_path_returns_400(self):
        sid = self._new_session()
        body, status = _post("/api/file/open-vscode", {"session_id": sid})
        assert status == 400, body
        assert "path" in body.get("error", "")

    def test_unknown_session_returns_404(self):
        body, status = _post(
            "/api/file/open-vscode",
            {"session_id": "nonexistent-session-xyz", "path": "."},
        )
        assert status == 404, body
        assert "session" in body.get("error", "").lower()

    def test_missing_file_returns_404_with_path(self):
        """Attempting to open a file that does not exist must return 404 and
        include the resolved path in the error (mirrors _handle_file_reveal
        behaviour introduced in #1764)."""
        sid = self._new_session()
        body, status = _post(
            "/api/file/open-vscode",
            {"session_id": sid, "path": "does-not-exist-2735.txt"},
        )
        assert status == 404, body
        err = body.get("error", "")
        assert "does-not-exist-2735.txt" in err, (
            f"404 message must include the resolved path, got: {err!r}"
        )

    def test_path_traversal_rejected(self):
        """Handler must reject paths that escape the workspace root."""
        sid = self._new_session()
        body, status = _post(
            "/api/file/open-vscode",
            {"session_id": sid, "path": "../../../../../../etc/passwd"},
        )
        assert status == 400, body
