import re
from pathlib import Path

SESSIONS_JS = Path(__file__).resolve().parent.parent / "static" / "sessions.js"


def _function_body(src: str, name: str) -> str:
    m = re.search(
        rf"(?:async\s+)?function\s+{re.escape(name)}\b.*?^}}",
        src,
        re.DOTALL | re.MULTILINE,
    )
    assert m, f"{name} function not found"
    return m.group(0)


def test_refreshActiveSessionIfExternallyUpdated_exists():
    src = SESSIONS_JS.read_text(encoding="utf-8")
    assert "async function refreshActiveSessionIfExternallyUpdated" in src


def test_poll_path_skips_non_external_sessions():
    src = SESSIONS_JS.read_text(encoding="utf-8")
    body = _function_body(src, "refreshActiveSessionIfExternallyUpdated")
    assert re.search(
        r"if\s*\(\s*\(\s*reason\s*\|\|\s*['\"]poll['\"]\s*\)\s*===\s*['\"]poll['\"]\s*&&\s*!\s*_isExternalSession\s*\(",
        body,
    ), (
        "the 30s poll fallback should early-return for WebUI-native sessions, "
        "but non-poll refresh triggers must still be allowed"
    )


def test_session_events_refresh_active_session():
    src = SESSIONS_JS.read_text(encoding="utf-8")
    body = _function_body(src, "_scheduleSessionEventsRefresh")
    assert "refreshSessionList(reason||'event', {refreshActive:true})" in body
