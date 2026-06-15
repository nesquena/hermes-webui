import re
from pathlib import Path

SESSIONS_JS = Path(__file__).resolve().parent.parent / "static" / "sessions.js"


def test_refreshActiveSessionIfExternallyUpdated_exists():
    src = SESSIONS_JS.read_text(encoding="utf-8")
    assert "async function refreshActiveSessionIfExternallyUpdated" in src


def test_isExternalSession_guard_present():
    src = SESSIONS_JS.read_text(encoding="utf-8")
    m = re.search(
        r"async function refreshActiveSessionIfExternallyUpdated\b.*?^}",
        src,
        re.DOTALL | re.MULTILINE,
    )
    assert m, "refreshActiveSessionIfExternallyUpdated function not found"
    body = m.group(0)
    assert re.search(r"if\s*\(\s*!\s*_isExternalSession\s*\(", body), (
        "refreshActiveSessionIfExternallyUpdated must have an early-return "
        "guard: if(!_isExternalSession(...))"
    )
