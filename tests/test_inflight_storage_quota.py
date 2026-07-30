"""Regression coverage for browser in-flight localStorage quota handling."""
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PY = (REPO_ROOT / "api" / "config.py").read_text(encoding="utf-8")
BOOT_JS = (REPO_ROOT / "static" / "boot.js").read_text(encoding="utf-8")
UI_JS = (REPO_ROOT / "static" / "ui.js").read_text(encoding="utf-8")


def _function_body(src: str, name: str) -> str:
    marker = f"function {name}"
    start = src.index(marker)
    brace = src.index("{", start)
    depth = 1
    i = brace + 1
    while depth and i < len(src):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
        i += 1
    return src[brace + 1 : i - 1]


def test_inflight_state_is_compacted_before_localstorage_write():
    """Persisted recovery state must stay bounded instead of storing full long sessions."""
    save_body = _function_body(UI_JS, "saveInflightState")
    compact_body = _function_body(UI_JS, "_compactInflightState")

    assert "const entry={..._compactInflightState(state),updated_at:Date.now()};" in save_body
    assert "const limits=_getInflightStateLimits();" in compact_body
    assert ".slice(-limits.messages)" in compact_body
    assert ".slice(-limits.toolCalls)" in compact_body
    assert "limits.jsonChars" in UI_JS


def test_inflight_state_limits_are_configurable_from_settings():
    """Recovery snapshots should be bounded by settings, not hardcoded at 3 sessions / 8 messages."""
    assert '\"inflight_state_max_sessions\": 8' in CONFIG_PY
    assert '\"inflight_state_max_messages\": 24' in CONFIG_PY
    assert '\"inflight_state_max_tool_calls\": 48' in CONFIG_PY
    assert '\"inflight_state_max_string_chars\": 60000' in CONFIG_PY
    assert '\"inflight_state_max_json_chars\": 1500000' in CONFIG_PY
    assert '\"inflight_state_max_sessions\": (1, 25)' in CONFIG_PY
    assert '\"inflight_state_max_messages\": (1, 100)' in CONFIG_PY
    assert '\"inflight_state_max_tool_calls\": (1, 200)' in CONFIG_PY
    assert '\"inflight_state_max_string_chars\": (1000, 500000)' in CONFIG_PY
    assert '\"inflight_state_max_json_chars\": (100000, 4000000)' in CONFIG_PY
    assert "window._inflightStateLimits={" in BOOT_JS
    assert "maxSessions:parseInt(s.inflight_state_max_sessions||8,10)||8" in BOOT_JS
    assert "messages:parseInt(s.inflight_state_max_messages||24,10)||24" in BOOT_JS
    # The reader function MUST use a different name than the window-attached
    # config object — top-level `function foo(){}` in non-module scripts
    # attaches to `window`, so a collision causes boot.js to overwrite the
    # function with the config object and every later call throws
    # `_inflightStateLimits is not a function`. See #2771.
    assert "function _getInflightStateLimits()" in UI_JS
    assert "function _inflightStateLimits()" not in UI_JS, (
        "Function name must not collide with window._inflightStateLimits "
        "config object (#2771)."
    )
    assert "window._inflightStateLimits" in UI_JS
    assert "INFLIGHT_STATE_MAX_SESSIONS = 3" not in UI_JS
    assert "INFLIGHT_STATE_MAX_MESSAGES = 8" not in UI_JS


def test_inflight_marker_write_handles_quota_by_dropping_recovery_snapshots():
    """The tiny active-stream marker must not crash submit when recovery snapshots fill quota."""
    mark_body = _function_body(UI_JS, "markInflight")

    assert "try{" in mark_body
    assert "localStorage.setItem(INFLIGHT_KEY, payload);" in mark_body
    assert "_isStorageQuotaError(err)" in mark_body
    assert "localStorage.removeItem(INFLIGHT_STATE_KEY);" in mark_body
    assert mark_body.index("localStorage.removeItem(INFLIGHT_STATE_KEY);") < mark_body.rindex(
        "localStorage.setItem(INFLIGHT_KEY, payload);"
    )


def test_save_inflight_state_clears_snapshots_when_quota_retry_fails():
    """Quota failures should degrade recovery, not preserve a storage-filling blob."""
    save_body = _function_body(UI_JS, "saveInflightState")

    assert "catch(err)" in save_body
    assert "if(!_isStorageQuotaError(err)) return;" in save_body
    assert "localStorage.removeItem(INFLIGHT_STATE_KEY);" in save_body
    assert "_writeInflightStateMap({[sid]:entry});" in save_body
