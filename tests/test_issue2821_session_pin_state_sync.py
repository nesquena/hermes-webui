"""Regression checks for #2821 session pin/unpin state sync."""

from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
ROUTES_PY = (ROOT / "api" / "routes.py").read_text(encoding="utf-8")
SESSIONS_JS = (ROOT / "static" / "sessions.js").read_text(encoding="utf-8")


def _function_block(src: str, name: str) -> str:
    marker = f"function {name}"
    start = src.find(marker)
    assert start != -1, f"{name} not found"
    brace = src.find("{", start)
    assert brace != -1, f"{name} body not found"
    depth = 1
    i = brace + 1
    while i < len(src) and depth:
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
        i += 1
    assert depth == 0, f"{name} body did not close"
    return src[start:i]


def test_session_field_helper_reads_dicts_and_objects():
    from api.routes import _session_field

    class SessionLike:
        session_id = "obj-1"
        pinned = True
        archived = False

    assert _session_field({"session_id": "dict-1", "pinned": True}, "pinned", False) is True
    assert _session_field({"session_id": "dict-1"}, "archived", False) is False
    assert _session_field(SessionLike(), "session_id", None) == "obj-1"
    assert _session_field(SessionLike(), "missing", "fallback") == "fallback"


def test_pin_limit_snapshot_counts_index_dict_entries():
    assert "_session_field(existing, \"session_id\", None)" in ROUTES_PY
    assert "_session_field(existing, \"pinned\", False)" in ROUTES_PY
    assert "_session_field(existing, \"archived\", False)" in ROUTES_PY
    start = ROUTES_PY.find("persisted_pinned_ids = {")
    assert start != -1, "persisted pin snapshot not found"
    end = ROUTES_PY.find("with LOCK:", start)
    assert end != -1, "persisted pin snapshot should be computed before LOCK"
    persisted_snapshot = ROUTES_PY[start:end]
    assert 'getattr(existing, "pinned", False)' not in persisted_snapshot
    assert 'getattr(existing, "archived", False)' not in persisted_snapshot


def test_pin_action_does_not_short_circuit_on_stale_client_count():
    body = _function_block(SESSIONS_JS, "_openSessionActionMenu")
    assert "const pinLimitReached=" not in body
    assert "if(pinLimitReached)" not in body
    assert "_pinnedSessionCount()>=_getPinnedSessionsLimit()" not in body
    assert "await api('/api/session/pin'" in body


def test_pin_action_refreshes_session_list_after_pin_failure():
    body = _function_block(SESSIONS_JS, "_openSessionActionMenu")
    catch_idx = body.find("}catch(err){")
    assert catch_idx != -1, "Pin/unpin action must have an error path"
    catch_block = body[catch_idx:body.find("}", catch_idx + len("}catch(err){")) + 1]
    assert "showToast(t('session_pin_failed')+err.message)" in catch_block
    assert "await renderSessionList()" in catch_block
