"""Frontend regression coverage for Update Now apply failures (#1321)."""
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
UI_JS = ROOT / "static" / "ui.js"


def _ui_js() -> str:
    return UI_JS.read_text(encoding="utf-8")


def test_update_apply_network_error_has_recovery_message_not_raw_failed_to_fetch():
    """Network/interrupted update apply failures should not surface raw fetch text alone."""
    src = _ui_js()
    assert "function _formatUpdateApplyExceptionMessage" in src
    assert "could not reach the WebUI server" in src
    assert "restarted or the connection was interrupted" in src
    assert "wait a few seconds, reload the page, then check the server" in src
    assert "Update failed: '+e.message" not in src
    assert 'Update failed: "+e.message' not in src


def test_update_apply_structured_server_errors_still_use_json_message_path():
    """Server-reachable JSON errors must keep the existing targeted message path."""
    src = _ui_js()
    apply_start = src.index("async function applyUpdates()")
    show_error_call = src.index("_showUpdateError(target,res);", apply_start)
    reset_button = src.index("resetApplyButton(0);", show_error_call)
    assert show_error_call < reset_button
    assert "const msg='Update failed ('+target+'): '+(res.message||'unknown error');" in src


def test_update_apply_network_error_classifier_ignores_http_status_errors():
    """HTTP response errors should not be classified as interrupted transport failures."""
    src = _ui_js()
    fn_start = src.index("function _isUpdateApplyNetworkError(error)")
    fn_end = src.index("function _formatUpdateApplyExceptionMessage", fn_start)
    body = src[fn_start:fn_end]
    compact = re.sub(r"\s+", "", body)
    assert "if(error&&error.status)returnfalse;" in compact
    assert body.index("error.status") < body.index("/Failed to fetch|NetworkError|Load failed/i")
    assert "Failed to fetch|NetworkError|Load failed" in body


def test_update_apply_prevents_duplicate_apply_requests_while_in_flight():
    """Double-clicks should not send a second update apply request during restart race windows."""
    src = _ui_js()
    apply_start = src.index("async function applyUpdates()")
    next_fn = src.index("function _showUpdateError", apply_start)
    body = src[apply_start:next_fn]
    assert "window._updateApplyInFlight" in body
    assert "if(window._updateApplyInFlight) return;" in body
    assert "window._updateApplyInFlight=true;" in body
    assert "window._updateApplyInFlight=false;" in body
