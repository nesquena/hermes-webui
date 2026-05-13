from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
UI_JS = ROOT / "static" / "ui.js"
I18N_JS = ROOT / "static" / "i18n.js"
CONFIG_PY = ROOT / "api" / "config.py"


def _function_body(src: str, name: str) -> str:
    marker = f"function {name}"
    start = src.index(marker)
    brace = src.index("{", start)
    depth = 0
    for idx in range(brace, len(src)):
        if src[idx] == "{":
            depth += 1
        elif src[idx] == "}":
            depth -= 1
            if depth == 0:
                return src[brace : idx + 1]
    raise AssertionError(f"{name} function body not found")


def test_upload_limit_constant_matches_server_limit():
    """The browser preflight limit must match api.config.MAX_UPLOAD_BYTES."""
    ui = UI_JS.read_text(encoding="utf-8")
    config = CONFIG_PY.read_text(encoding="utf-8")

    assert "const MAX_UPLOAD_BYTES=20*1024*1024;" in ui
    assert "MAX_UPLOAD_BYTES = 20 * 1024 * 1024" in config


def test_file_picker_rejects_oversize_files_before_queueing():
    """Selecting an oversized file should never add it to pending uploads."""
    src = UI_JS.read_text(encoding="utf-8")
    body = _function_body(src, "addFiles")

    size_gate = body.index("f&&f.size>MAX_UPLOAD_BYTES")
    status_notice = body.index("_showUploadTooLarge(f)")
    push_pending = body.index("S.pendingFiles.push(f)")

    assert size_gate < status_notice < push_pending
    assert "continue;" in body[size_gate:push_pending]


def test_pending_uploads_skip_fetch_for_oversize_files():
    """Restored or queued oversized files should fail locally before fetch()."""
    src = UI_JS.read_text(encoding="utf-8")
    body = _function_body(src, "uploadPendingFiles")

    size_gate = body.index("f&&f.size>MAX_UPLOAD_BYTES")
    form_data = body.index("const fd=new FormData()")
    upload_fetch = body.index("fetch(url")

    assert size_gate < form_data < upload_fetch
    assert "throw new Error(_uploadTooLargeMessage(f))" in body[size_gate:form_data]


def test_upload_too_large_has_user_facing_message():
    """The status toast should explain the 20 MB limit instead of a network reset."""
    i18n = I18N_JS.read_text(encoding="utf-8")
    ui = UI_JS.read_text(encoding="utf-8")

    assert "upload_too_large" in i18n
    assert "Maximum upload size is" in i18n
    assert "_uploadTooLargeMessage(file)" in ui
