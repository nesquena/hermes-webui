"""Tests for /api/folder/download — matches the static-inspection style used
elsewhere in the hermes-webui test suite (see tests/test_issue1867_upload_size_preflight.py).
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ROUTES_PY = ROOT / "api" / "routes.py"
UI_JS = ROOT / "static" / "ui.js"


def test_folder_download_handler_defined():
    src = ROUTES_PY.read_text(encoding="utf-8")
    assert "def _handle_folder_download(handler, parsed):" in src
    assert "/api/folder/download?session_id=" in src  # in handler docstring
    assert 'Content-Type", "application/zip"' in src
    assert "zipfile.ZipFile(handler.wfile" in src


def test_folder_download_dispatch_registered():
    src = ROUTES_PY.read_text(encoding="utf-8")
    assert 'parsed.path == "/api/folder/download"' in src
    assert "_handle_folder_download(handler, parsed)" in src


def test_folder_download_uses_safe_resolve():
    src = ROUTES_PY.read_text(encoding="utf-8")
    handler_idx = src.index("def _handle_folder_download")
    end_idx = src.index("\n\ndef ", handler_idx + 1)
    body = src[handler_idx:end_idx]
    assert "safe_resolve(Path(s.workspace), rel)" in body
    assert "ValueError" in body


def test_folder_download_skips_escaping_symlinks():
    src = ROUTES_PY.read_text(encoding="utf-8")
    collect_idx = src.index("def _folder_download_collect")
    end_idx = src.index("\n\ndef ", collect_idx + 1)
    body = src[collect_idx:end_idx]
    assert "followlinks=False" in body
    assert "is_symlink()" in body
    assert "is_relative_to(workspace_root)" in body


def test_folder_download_respects_max_files_env():
    src = ROUTES_PY.read_text(encoding="utf-8")
    assert 'HERMES_WEBUI_FOLDER_ZIP_MAX_FILES' in src
    assert '"too many files"' in src
    assert 'status=413' in src


def test_folder_download_respects_max_bytes_env():
    src = ROUTES_PY.read_text(encoding="utf-8")
    assert 'HERMES_WEBUI_FOLDER_ZIP_MAX_MB' in src
    assert '"folder too large"' in src
    assert 'limit_bytes' in src


def test_folder_download_preflights_before_streaming():
    """Pre-flight collect must run BEFORE send_response so 413 can return JSON."""
    src = ROUTES_PY.read_text(encoding="utf-8")
    handler_idx = src.index("def _handle_folder_download")
    end_idx = src.index("\n\n# ", handler_idx) if "\n\n# " in src[handler_idx:] else len(src)
    body = src[handler_idx:end_idx]
    collect_call = body.index("_folder_download_collect")
    send_response = body.index("handler.send_response(200)")
    limit_check = body.index('"too many files"')
    assert collect_call < limit_check < send_response


def test_folder_download_rejects_files():
    src = ROUTES_PY.read_text(encoding="utf-8")
    assert "path must be a directory" in src
    assert "/api/file/raw" in src  # error message guides user


def test_folder_download_streams_not_buffers():
    src = ROUTES_PY.read_text(encoding="utf-8")
    assert "zipfile.ZipFile(handler.wfile" in src
    assert "allowZip64=True" in src
    handler_idx = src.index("def _handle_folder_download")
    end_idx = src.index("\n\ndef ", handler_idx + 1)
    body = src[handler_idx:end_idx]
    assert "io.BytesIO" not in body, "must stream, not buffer in memory"


def test_ui_context_menu_has_download_folder():
    src = UI_JS.read_text(encoding="utf-8")
    assert "download_folder" in src
    download_idx = src.index("download_folder")
    snippet = src[max(0, download_idx - 200):download_idx]
    assert "item.type==='dir'" in snippet or "item.type === 'dir'" in snippet


def test_ui_download_folder_uses_endpoint():
    src = UI_JS.read_text(encoding="utf-8")
    download_idx = src.index("download_folder")
    snippet = src[download_idx:download_idx + 600]
    assert "/api/folder/download" in snippet
    assert "session_id=" in snippet
    assert "path=" in snippet
    assert "encodeURIComponent" in snippet
