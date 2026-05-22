"""Regression coverage for send/start optimistic INFLIGHT races."""
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
MESSAGES_JS = (REPO_ROOT / "static" / "messages.js").read_text(encoding="utf-8")
SESSIONS_JS = (REPO_ROOT / "static" / "sessions.js").read_text(encoding="utf-8")


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


def test_send_preserves_optimistic_messages_across_chat_start_await():
    """send() must not dereference INFLIGHT[activeSid] after await without a fallback."""
    body = _function_body(MESSAGES_JS, "send")
    setup_idx = body.index("const optimisticMessages=[...S.messages];")
    inflight_idx = body.index("INFLIGHT[activeSid]={messages:optimisticMessages")
    await_idx = body.index("const startData=await api('/api/chat/start'")
    save_idx = body.index("saveInflightState(activeSid,{streamId", await_idx)

    assert setup_idx < inflight_idx < await_idx < save_idx
    post_await = body[await_idx:save_idx]
    assert "if(!INFLIGHT[activeSid])" in post_await, (
        "send() should recreate the INFLIGHT entry if a session-list refresh pruned it"
    )
    assert "messages:INFLIGHT[activeSid].messages" not in body[save_idx : save_idx + 220], (
        "saveInflightState() should use a guarded local/current inflight object, not a blind nested read"
    )


def test_stale_inflight_purge_preserves_current_send_before_stream_id_exists():
    """Sidebar cleanup must not delete the active send before /api/chat/start responds."""
    body = _function_body(SESSIONS_JS, "_purgeStaleInflightEntries")

    assert "_sendInProgress" in body and "_sendInProgressSid" in body, (
        "_purgeStaleInflightEntries() should skip the current send while start is in progress"
    )
    skip_idx = body.index("_sendInProgress")
    delete_idx = body.index("delete INFLIGHT[sid];")
    assert skip_idx < delete_idx, "the current-send skip must run before any purge deletion"
