import json
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent
MESSAGES_JS = (ROOT / "static" / "messages.js").read_text(encoding="utf-8")
SESSIONS_JS = (ROOT / "static" / "sessions.js").read_text(encoding="utf-8")


def _function_body(src: str, name: str) -> str:
    marker = f"function {name}"
    start = src.index(marker)
    brace = src.index("{", start)
    depth = 0
    for idx in range(brace, len(src)):
        ch = src[idx]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return src[brace + 1 : idx]
    raise AssertionError(f"function {name} body not found")


def _extract_function(src: str, name: str) -> str:
    marker = f"function {name}"
    start = src.index(marker)
    body = _function_body(src, name)
    sig = src[start : src.index("{", start)]
    return f"{sig}{{{body}}}"


def _done_handler_body() -> str:
    marker = "source.addEventListener('done',e=>{"
    start = MESSAGES_JS.index(marker)
    brace = MESSAGES_JS.index("{", start)
    depth = 0
    for idx in range(brace, len(MESSAGES_JS)):
        ch = MESSAGES_JS[idx]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return MESSAGES_JS[brace + 1 : idx]
    raise AssertionError("done handler body not found")


def _has_pre_list_view_sync() -> bool:
    body = _done_handler_body()
    list_call = "_markSessionCompletedInList(completedSession, activeSid);"
    viewed_call = "_markSessionViewed(completedSid,"
    assert list_call in body, "done handler must update the cached sidebar row"
    assert viewed_call in body, "done handler must sync viewed state for active sessions"
    return body.index(viewed_call) < body.index(list_call)


def _completed_message_count_assignment() -> str:
    body = _done_handler_body()
    marker = "const completedMessageCount="
    start = body.index(marker)
    end = body.index(";\n", start) + 1
    return body[start:end]


def _run_done_compaction_harness(
    *,
    is_session_viewed: bool,
    include_message_count: bool = True,
    include_completed_messages: bool = True,
    session_sid_matches_completed: bool = True,
    session_message_count: int | None = 4,
    visible_message_count: int = 4,
) -> dict:
    helpers = "\n".join(
        [
            _extract_function(MESSAGES_JS, "_markSessionViewed"),
            _extract_function(SESSIONS_JS, "_getSessionViewedCounts"),
            _extract_function(SESSIONS_JS, "_saveSessionViewedCounts"),
            _extract_function(SESSIONS_JS, "_setSessionViewedCount"),
            _extract_function(SESSIONS_JS, "_getSessionCompletionUnread"),
            _extract_function(SESSIONS_JS, "_saveSessionCompletionUnread"),
            _extract_function(SESSIONS_JS, "_markSessionCompletionUnread"),
            _extract_function(SESSIONS_JS, "_clearSessionCompletionUnread"),
            _extract_function(SESSIONS_JS, "_hasSessionCompletionUnread"),
            _extract_function(SESSIONS_JS, "_hasUnreadForSession"),
            _extract_function(SESSIONS_JS, "_markSessionCompletedInList"),
        ]
    )
    has_pre_list_sync = _has_pre_list_view_sync()
    completed_message_count_assignment = textwrap.indent(
        _completed_message_count_assignment(),
        "        ",
    ).rstrip()
    harness = textwrap.dedent(
        f"""
        const SESSION_VIEWED_COUNTS_KEY='session-viewed-counts';
        const SESSION_COMPLETION_UNREAD_KEY='session-completion-unread';
        let _sessionViewedCounts=null;
        let _sessionCompletionUnread=null;
        const visibleMessages=Array.from({{length:{visible_message_count}}}, (_, idx)=>({{
          role:'assistant',
          content:`message-${{idx + 1}}`,
        }}));
        let _allSessions=[{{
          session_id:'active-before-compact',
          message_count:3,
          last_message_at:10,
          updated_at:10,
          is_streaming:true,
        }}];
        const _sessionStreamingById=new Map();
        const _sessionListSnapshotById=new Map();
        const _sessionListSourceById=new Map();
        const storage=new Map();
        const localStorage={{
          getItem(key){{ return storage.has(key) ? storage.get(key) : null; }},
          setItem(key, value){{ storage.set(key, String(value)); }},
          removeItem(key){{ storage.delete(key); }},
        }};
        function renderSessionListFromCache(){{}}
        function _forgetObservedStreamingSession(){{}}
        function _rememberSessionListSource(){{}}
        {helpers}
        _setSessionViewedCount('active-after-compact', 3);
        const activeSid='active-before-compact';
        const completedSid='active-after-compact';
        const S={{
          session:{{session_id:{json.dumps("active-after-compact" if session_sid_matches_completed else "different-session")}}},
          messages:visibleMessages,
        }};
        if ({json.dumps(session_message_count is not None)}) S.session.message_count = {json.dumps(session_message_count)};
        const completedSession={{
          session_id:completedSid,
          updated_at:20,
          last_message_at:20,
        }};
        if ({str(include_completed_messages).lower()}) completedSession.messages = visibleMessages.slice();
        if ({str(include_message_count).lower()}) completedSession.message_count = 4;
{completed_message_count_assignment}
        if ({str(is_session_viewed).lower()} && {json.dumps(has_pre_list_sync)}) {{
          _markSessionViewed(completedSid, completedMessageCount);
        }}
        if (!{str(is_session_viewed).lower()}) {{
          _markSessionCompletionUnread(completedSid, completedMessageCount);
        }}
        _markSessionCompletedInList(completedSession, activeSid);
        const cacheRow=_allSessions.find(s=>s&&s.session_id===completedSid);
        const unreadAfterCacheUpdate=_hasUnreadForSession(cacheRow);
        const viewedCountAfterCacheUpdate=_getSessionViewedCounts()[completedSid] ?? null;
        if ({str(is_session_viewed).lower()}) {{
          _markSessionViewed(completedSid, completedMessageCount);
        }}
        console.log(JSON.stringify({{
          hasPreListSync:{json.dumps(has_pre_list_sync)},
          unreadAfterCacheUpdate,
          viewedCountAfterCacheUpdate,
          unreadAfterActiveBranchSync:_hasUnreadForSession(cacheRow),
          hasCompletionUnread:_hasSessionCompletionUnread(completedSid),
          completionUnreadMessageCount:_getSessionCompletionUnread()[completedSid]?.message_count ?? null,
          viewedCount:_getSessionViewedCounts()[completedSid] ?? null,
          cacheRow,
        }}));
        """
    )
    result = subprocess.run(
        ["node", "-e", harness],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip())


@pytest.mark.skipif(shutil.which("node") is None, reason="node required for behavioral test")
def test_actively_viewed_done_completion_stays_read_after_sidebar_cache_update():
    result = _run_done_compaction_harness(is_session_viewed=True)

    assert result["cacheRow"]["session_id"] == "active-after-compact"
    assert result["cacheRow"]["message_count"] == 4
    assert result["unreadAfterCacheUpdate"] is False, (
        "actively viewed done settle must sync viewed_count before the sidebar cache "
        "re-render sees the higher compacted message_count"
    )
    assert result["viewedCountAfterCacheUpdate"] == 4
    assert result["unreadAfterActiveBranchSync"] is False
    assert result["hasCompletionUnread"] is False
    assert result["viewedCount"] == 4


@pytest.mark.skipif(shutil.which("node") is None, reason="node required for behavioral test")
def test_actively_viewed_done_completion_uses_messages_fallback_when_message_count_missing():
    result = _run_done_compaction_harness(
        is_session_viewed=True,
        include_message_count=False,
    )

    assert result["cacheRow"]["session_id"] == "active-after-compact"
    assert result["cacheRow"]["message_count"] == 4
    assert result["unreadAfterCacheUpdate"] is False
    assert result["viewedCountAfterCacheUpdate"] == 4
    assert result["unreadAfterActiveBranchSync"] is False
    assert result["hasCompletionUnread"] is False
    assert result["viewedCount"] == 4


@pytest.mark.skipif(shutil.which("node") is None, reason="node required for behavioral test")
def test_actively_viewed_done_completion_uses_session_fallback_when_payload_counts_are_missing():
    result = _run_done_compaction_harness(
        is_session_viewed=True,
        include_message_count=False,
        include_completed_messages=False,
    )

    assert result["cacheRow"]["session_id"] == "active-after-compact"
    assert result["cacheRow"]["message_count"] == 3
    assert result["unreadAfterCacheUpdate"] is False
    assert result["viewedCountAfterCacheUpdate"] == 4
    assert result["unreadAfterActiveBranchSync"] is False
    assert result["hasCompletionUnread"] is False
    assert result["viewedCount"] == 4


@pytest.mark.skipif(shutil.which("node") is None, reason="node required for behavioral test")
def test_actively_viewed_done_completion_uses_visible_messages_fallback_when_session_count_is_missing():
    result = _run_done_compaction_harness(
        is_session_viewed=True,
        include_message_count=False,
        include_completed_messages=False,
        session_sid_matches_completed=False,
        session_message_count=None,
    )

    assert result["cacheRow"]["session_id"] == "active-after-compact"
    assert result["cacheRow"]["message_count"] == 3
    assert result["unreadAfterCacheUpdate"] is False
    assert result["viewedCountAfterCacheUpdate"] == 4
    assert result["unreadAfterActiveBranchSync"] is False
    assert result["hasCompletionUnread"] is False
    assert result["viewedCount"] == 4


@pytest.mark.skipif(shutil.which("node") is None, reason="node required for behavioral test")
def test_background_done_completion_stays_unread_after_sidebar_cache_update():
    result = _run_done_compaction_harness(is_session_viewed=False)

    assert result["cacheRow"]["session_id"] == "active-after-compact"
    assert result["cacheRow"]["message_count"] == 4
    assert result["unreadAfterCacheUpdate"] is True
    assert result["viewedCountAfterCacheUpdate"] == 3
    assert result["unreadAfterActiveBranchSync"] is True
    assert result["hasCompletionUnread"] is True
    assert result["completionUnreadMessageCount"] == 4
    assert result["viewedCount"] == 3
