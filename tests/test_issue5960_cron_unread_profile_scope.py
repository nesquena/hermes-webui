"""Regression coverage for cross-profile cron unread badges (#5960)."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
PANELS_JS = (ROOT / "static" / "panels.js").read_text(encoding="utf-8")
SESSIONS_JS = (ROOT / "static" / "sessions.js").read_text(encoding="utf-8")
ROUTES_PY = (ROOT / "api" / "routes.py").read_text(encoding="utf-8")
NODE = shutil.which("node")


def _extract_function(source: str, name: str) -> str:
    start = source.index(f"function {name}(")
    if source[max(0, start - 6) : start] == "async ":
        start -= 6
    brace = source.index("{", start)
    depth = 1
    pos = brace + 1
    while depth and pos < len(source):
        if source[pos] == "{":
            depth += 1
        elif source[pos] == "}":
            depth -= 1
        pos += 1
    assert depth == 0
    return source[start:pos]


def test_recent_handler_reuses_dispatcher_cron_context_without_nesting():
    dispatch_start = ROUTES_PY.index('if parsed.path == "/api/crons/recent":')
    dispatch_end = ROUTES_PY.index('if parsed.path == "/api/crons/status":', dispatch_start)
    dispatch = ROUTES_PY[dispatch_start:dispatch_end]
    handler_start = ROUTES_PY.index("def _handle_cron_recent(")
    handler_end = ROUTES_PY.index("\ndef ", handler_start + 1)
    handler = ROUTES_PY[handler_start:handler_end]

    assert "with cron_profile_context():" in dispatch
    assert "cron_profile_context_for_home" not in handler


def test_successful_profile_switch_resets_unread_cron_state():
    switch_start = PANELS_JS.index("async function switchToProfile(name) {")
    switch_end = PANELS_JS.index("// ── Cron completion alerts", switch_start)
    switch_body = PANELS_JS[switch_start:switch_end]

    state_update = switch_body.index("S.activeProfile = data.active || name;")
    reset_call = switch_body.index("_resetCronUnreadForProfileSwitch();")
    assert reset_call > state_update

    reset_start = PANELS_JS.index("function _resetCronUnreadForProfileSwitch(){")
    reset_end = PANELS_JS.index("\n}", reset_start)
    reset_body = PANELS_JS[reset_start:reset_end]
    assert "_cronPollGeneration++;" in reset_body
    assert "_cronNewJobIds.clear();" in reset_body
    assert "_cronPollSince=Date.now()/1000;" in reset_body
    assert "_clearCronSessionCompletionUnreadForInactiveProfiles" in reset_body
    assert "updateCronBadge();" in reset_body


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_session_load_profile_switch_resets_unread_cron_state():
    switch = _extract_function(SESSIONS_JS, "_switchProfileForSessionLoad")
    reset = _extract_function(PANELS_JS, "_resetCronUnreadForProfileSwitch")
    script = f"""
let _cronPollSince=10;
let _cronUnreadCount=1;
let _cronPollGeneration=0;
const _cronNewJobIds=new Set(['old-profile-job']);
global.S={{activeProfile:'default'}};
global.api=async()=>({{active:'alternate',is_default:false}});
global.localStorage={{removeItem(){{}}}};
global.updateCronBadge=()=>{{ _cronUnreadCount=_cronNewJobIds.size; }};
function _clearCronSessionCompletionUnreadForInactiveProfiles(){{}}
{reset}
{switch}
(async()=>{{
  await _switchProfileForSessionLoad('alternate');
  process.stdout.write(JSON.stringify({{
    profile:S.activeProfile,
    unread:Array.from(_cronNewJobIds),
    count:_cronUnreadCount,
    generation:_cronPollGeneration,
  }}));
}})().catch(error=>{{ console.error(error); process.exit(1); }});
"""
    result = subprocess.run(
        [NODE, "-e", script], check=True, capture_output=True, text=True, timeout=30
    )
    state = json.loads(result.stdout)
    assert state == {
        "profile": "alternate",
        "unread": [],
        "count": 0,
        "generation": 1,
    }


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_poll_started_before_switch_cannot_recreate_unread_state():
    polling = _extract_function(PANELS_JS, "startCronPolling")
    reset = _extract_function(PANELS_JS, "_resetCronUnreadForProfileSwitch")
    script = f"""
let _cronPollSince=10;
let _cronPollTimer=null;
let _cronUnreadCount=0;
let _cronPollGeneration=0;
const _cronNewJobIds=new Set();
let intervalCallback=null;
let resolveApi;
global.document={{hidden:false}};
global.setInterval=(callback)=>{{ intervalCallback=callback; return 1; }};
global.api=()=>new Promise(resolve=>{{ resolveApi=resolve; }});
global.showToast=()=>{{}};
global.t=(key)=>key;
global.updateCronBadge=()=>{{ _cronUnreadCount=_cronNewJobIds.size; }};
function _clearCronSessionCompletionUnreadForInactiveProfiles(){{}}
{polling}
{reset}
startCronPolling();
(async()=>{{
  const stalePoll=intervalCallback();
  _resetCronUnreadForProfileSwitch();
  resolveApi({{completions:[{{job_id:'old-profile-job',completed_at:20}}]}});
  await stalePoll;
  process.stdout.write(JSON.stringify({{
    unread:Array.from(_cronNewJobIds),
    count:_cronUnreadCount,
    generation:_cronPollGeneration,
  }}));
}})().catch(error=>{{ console.error(error); process.exit(1); }});
"""
    result = subprocess.run(
        [NODE, "-e", script], check=True, capture_output=True, text=True, timeout=30
    )
    state = json.loads(result.stdout)
    assert state == {"unread": [], "count": 0, "generation": 1}


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_profile_switch_clears_persisted_old_profile_cron_markers_only():
    """Gate #5975: sticky sidebar must drop old-profile cron dots, keep non-cron."""
    mark = _extract_function(SESSIONS_JS, "_markSessionCompletionUnread")
    get_unread = _extract_function(SESSIONS_JS, "_getSessionCompletionUnread")
    save_unread = _extract_function(SESSIONS_JS, "_saveSessionCompletionUnread")
    clear_helpers = "\n".join(
        [
            _extract_function(SESSIONS_JS, "_isCronSessionForUnread"),
            _extract_function(SESSIONS_JS, "_sourceKeyForSession"),
            _extract_function(SESSIONS_JS, "_cronCompletionUnreadMetaForSession"),
            _extract_function(SESSIONS_JS, "_resolveCronCompletionMarkerOrigin"),
            _extract_function(SESSIONS_JS, "_cronMarkerProfileMatchesActive"),
            _extract_function(SESSIONS_JS, "_profileMatchesActiveProfile"),
            _extract_function(
                SESSIONS_JS, "_clearCronSessionCompletionUnreadForInactiveProfiles"
            ),
        ]
    )
    has_unread = _extract_function(SESSIONS_JS, "_hasUnreadForSession")
    has_marker = _extract_function(SESSIONS_JS, "_hasSessionCompletionUnread")
    reset = _extract_function(PANELS_JS, "_resetCronUnreadForProfileSwitch")
    switch = _extract_function(SESSIONS_JS, "_switchProfileForSessionLoad")
    script = f"""
const store={{'hermes-session-completion-unread':JSON.stringify({{}})}};
global.localStorage={{
  getItem:(key)=>Object.prototype.hasOwnProperty.call(store,key)?store[key]:null,
  setItem:(key,value)=>{{ store[key]=String(value); }},
  removeItem:(key)=>{{ delete store[key]; }},
}};
let _sessionCompletionUnread=null;
let _sessionViewedCounts={{}};
const SESSION_COMPLETION_UNREAD_KEY='hermes-session-completion-unread';
let _cronPollSince=10;
let _cronUnreadCount=0;
let _cronPollGeneration=0;
const _cronNewJobIds=new Set(['old-cron-job']);
let renders=0;
global.S={{activeProfile:'profile-a',activeProfileIsDefault:false}};
global._allSessions=[];
global.api=async()=>({{active:'profile-b',is_default:false}});
global.updateCronBadge=()=>{{ _cronUnreadCount=_cronNewJobIds.size; }};
global.renderSessionListFromCache=()=>{{ renders+=1; }};
function _getSessionViewedCounts(){{ return _sessionViewedCounts; }}
function _saveSessionViewedCounts(){{}}
function _setSessionViewedCount(sid, count){{
  _sessionViewedCounts[sid]=Number(count)||0;
}}
function _clearSessionCompletionUnread(sid){{
  const unread=_getSessionCompletionUnread();
  if(!Object.prototype.hasOwnProperty.call(unread, sid)) return;
  delete unread[sid];
  _saveSessionCompletionUnread();
}}
{get_unread}
{save_unread}
{mark}
{has_marker}
{has_unread}
{clear_helpers}
{reset}
{switch}
(async()=>{{
  _markSessionCompletionUnread('old-cron-session', 4, {{source:'cron', profile:'profile-a'}});
  _markSessionCompletionUnread('chat-session', 9);  // ordinary completion — keep
  _markSessionCompletionUnread('new-cron-session', 2, {{source:'cron', profile:'profile-b'}});
  const before={{
    oldCron:_hasUnreadForSession({{session_id:'old-cron-session'}}),
    chat:_hasUnreadForSession({{session_id:'chat-session'}}),
    newCron:_hasUnreadForSession({{session_id:'new-cron-session'}}),
  }};
  await _switchProfileForSessionLoad('profile-b');
  const after={{
    oldCron:_hasUnreadForSession({{session_id:'old-cron-session'}}),
    chat:_hasUnreadForSession({{session_id:'chat-session'}}),
    newCron:_hasUnreadForSession({{session_id:'new-cron-session'}}),
    badgeJobs:Array.from(_cronNewJobIds),
    badgeCount:_cronUnreadCount,
    generation:_cronPollGeneration,
    renders,
    persisted:JSON.parse(store['hermes-session-completion-unread']),
  }};
  process.stdout.write(JSON.stringify({{before, after}}));
}})().catch(error=>{{ console.error(error); process.exit(1); }});
"""
    result = subprocess.run(
        [NODE, "-e", script], check=True, capture_output=True, text=True, timeout=30
    )
    state = json.loads(result.stdout)
    assert state["before"] == {"oldCron": True, "chat": True, "newCron": True}
    assert state["after"]["oldCron"] is False
    assert state["after"]["chat"] is True
    assert state["after"]["newCron"] is True
    assert state["after"]["badgeJobs"] == []
    assert state["after"]["badgeCount"] == 0
    assert state["after"]["generation"] == 1
    assert state["after"]["renders"] >= 1
    assert "old-cron-session" not in state["after"]["persisted"]
    assert "chat-session" in state["after"]["persisted"]
    assert "new-cron-session" in state["after"]["persisted"]
    assert state["after"]["persisted"]["new-cron-session"]["source"] == "cron"
    assert state["after"]["persisted"]["new-cron-session"]["profile"] == "profile-b"


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_cron_poll_tags_persisted_markers_with_active_profile():
    polling = _extract_function(PANELS_JS, "startCronPolling")
    script = f"""
let _cronPollSince=10;
let _cronPollTimer=null;
let _cronUnreadCount=0;
let _cronPollGeneration=0;
const _cronNewJobIds=new Set();
const markCalls=[];
let intervalCallback=null;
global.document={{hidden:false}};
global.S={{activeProfile:'profile-a'}};
global.setInterval=(callback)=>{{ intervalCallback=callback; return 1; }};
global.api=async()=>({{
  completions:[{{
    job_id:'job-a',
    session_id:'cron-session-a',
    message_count:3,
    completed_at:20,
    toast_notifications:false,
  }}]
}});
global.showToast=()=>{{}};
global.t=(key)=>key;
global.updateCronBadge=()=>{{ _cronUnreadCount=_cronNewJobIds.size; }};
function _markSessionCompletionUnreadIfBackground(sid, count, meta){{
  markCalls.push([sid, count, meta]);
}}
{polling}
startCronPolling();
(async()=>{{
  await intervalCallback();
  process.stdout.write(JSON.stringify({{markCalls, unreadJobs:Array.from(_cronNewJobIds)}}));
}})().catch(error=>{{ console.error(error); process.exit(1); }});
"""
    result = subprocess.run(
        [NODE, "-e", script], check=True, capture_output=True, text=True, timeout=30
    )
    state = json.loads(result.stdout)
    assert state["unreadJobs"] == ["job-a"]
    assert state["markCalls"] == [
        ["cron-session-a", 3, {"source": "cron", "profile": "profile-a"}]
    ]


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_session_list_path_tags_cron_markers_with_source_and_profile():
    """Re-gate #5975: _markPollingCompletionUnreadTransitions must tag cron rows."""
    mark_poll = _extract_function(SESSIONS_JS, "_markPollingCompletionUnreadTransitions")
    is_cron = _extract_function(SESSIONS_JS, "_isCronSessionForUnread")
    meta_fn = _extract_function(SESSIONS_JS, "_cronCompletionUnreadMetaForSession")
    source_key = _extract_function(SESSIONS_JS, "_sourceKeyForSession")
    script = f"""
const markCalls=[];
global.S={{activeProfile:'profile-a',activeProfileIsDefault:false}};
global._allSessions=[];
global._sessionListSnapshotById=new Map();
global._sessionStreamingById=new Map();
global._sessionListSourceById=new Map();
global._allSessionsScope=null;
function _getSessionObservedStreaming(){{ return {{}}; }}
function _isSessionEffectivelyStreaming(){{ return false; }}
function _hasPendingUserMessageSignal(){{ return false; }}
function _isSessionActivelyViewedForList(){{ return false; }}
function _rememberSessionListSource(){{}}
function _rememberObservedStreamingSession(){{}}
function _forgetObservedStreamingSession(){{}}
function _setSessionViewedCount(){{}}
function _markSessionCompletionUnread(sid, count, meta){{
  markCalls.push([sid, count, meta||null]);
}}
{source_key}
{is_cron}
{meta_fn}
{mark_poll}
const sessions=[
  {{
    session_id:'cron-from-list',
    message_count:2,
    last_message_at:20,
    source_tag:'cron',
    profile:'profile-a',
    is_streaming:false,
  }},
  {{
    session_id:'chat-from-list',
    message_count:5,
    last_message_at:30,
    source_tag:'webui',
    profile:'profile-a',
    is_streaming:false,
  }},
];
// Pretend both previously streaming so completion transition fires.
_sessionStreamingById.set('cron-from-list', true);
_sessionStreamingById.set('chat-from-list', true);
_sessionListSnapshotById.set('cron-from-list', {{message_count:1, last_message_at:10}});
_sessionListSnapshotById.set('chat-from-list', {{message_count:4, last_message_at:10}});
_markPollingCompletionUnreadTransitions(sessions);
process.stdout.write(JSON.stringify({{markCalls}}));
"""
    result = subprocess.run(
        [NODE, "-e", script], check=True, capture_output=True, text=True, timeout=30
    )
    state = json.loads(result.stdout)
    by_sid = {row[0]: row for row in state["markCalls"]}
    assert "cron-from-list" in by_sid
    assert by_sid["cron-from-list"][2] == {"source": "cron", "profile": "profile-a"}
    assert "chat-from-list" in by_sid
    assert by_sid["chat-from-list"][2] is None


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_legacy_untagged_cron_marker_cleared_via_sidebar_metadata():
    """Re-gate #5975: untagged markers resolve from sidebar session source/profile."""
    helpers = "\n".join(
        [
            _extract_function(SESSIONS_JS, "_isCronSessionForUnread"),
            _extract_function(SESSIONS_JS, "_sourceKeyForSession"),
            _extract_function(SESSIONS_JS, "_cronCompletionUnreadMetaForSession"),
            _extract_function(SESSIONS_JS, "_resolveCronCompletionMarkerOrigin"),
            _extract_function(SESSIONS_JS, "_cronMarkerProfileMatchesActive"),
            _extract_function(SESSIONS_JS, "_profileMatchesActiveProfile"),
            _extract_function(SESSIONS_JS, "_getSessionCompletionUnread"),
            _extract_function(SESSIONS_JS, "_saveSessionCompletionUnread"),
            _extract_function(SESSIONS_JS, "_clearCronSessionCompletionUnreadForInactiveProfiles"),
            _extract_function(SESSIONS_JS, "_hasSessionCompletionUnread"),
            _extract_function(SESSIONS_JS, "_hasUnreadForSession"),
        ]
    )
    script = f"""
const store={{'hermes-session-completion-unread':JSON.stringify({{
  'legacy-cron':{{message_count:2, completed_at:1}},
  'chat':{{message_count:4, completed_at:1}},
}})}};
global.localStorage={{
  getItem:(key)=>Object.prototype.hasOwnProperty.call(store,key)?store[key]:null,
  setItem:(key,value)=>{{ store[key]=String(value); }},
  removeItem:(key)=>{{ delete store[key]; }},
}};
let _sessionCompletionUnread=null;
let _sessionViewedCounts={{}};
const SESSION_COMPLETION_UNREAD_KEY='hermes-session-completion-unread';
global.S={{activeProfile:'profile-b',activeProfileIsDefault:false}};
global._allSessions=[
  {{session_id:'legacy-cron', source_tag:'cron', profile:'profile-a', message_count:2}},
  {{session_id:'chat', source_tag:'webui', profile:'profile-a', message_count:4}},
];
global.renderSessionListFromCache=()=>{{}};
function _getSessionViewedCounts(){{ return _sessionViewedCounts; }}
function _setSessionViewedCount(){{}}
{helpers}
const before={{
  legacy:_hasUnreadForSession({{session_id:'legacy-cron'}}),
  chat:_hasUnreadForSession({{session_id:'chat'}}),
}};
_clearCronSessionCompletionUnreadForInactiveProfiles('profile-b');
const after={{
  legacy:_hasUnreadForSession({{session_id:'legacy-cron'}}),
  chat:_hasUnreadForSession({{session_id:'chat'}}),
  persisted:JSON.parse(store['hermes-session-completion-unread']),
}};
process.stdout.write(JSON.stringify({{before, after}}));
"""
    result = subprocess.run(
        [NODE, "-e", script], check=True, capture_output=True, text=True, timeout=30
    )
    state = json.loads(result.stdout)
    assert state["before"] == {"legacy": True, "chat": True}
    assert state["after"]["legacy"] is False
    assert state["after"]["chat"] is True
    assert "legacy-cron" not in state["after"]["persisted"]
    assert "chat" in state["after"]["persisted"]


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_root_alias_keeps_current_profile_cron_marker():
    """Re-gate #5975: default/renamed-root must not erase current-root cron dots."""
    helpers = "\n".join(
        [
            _extract_function(SESSIONS_JS, "_isCronSessionForUnread"),
            _extract_function(SESSIONS_JS, "_sourceKeyForSession"),
            _extract_function(SESSIONS_JS, "_cronCompletionUnreadMetaForSession"),
            _extract_function(SESSIONS_JS, "_resolveCronCompletionMarkerOrigin"),
            _extract_function(SESSIONS_JS, "_cronMarkerProfileMatchesActive"),
            _extract_function(SESSIONS_JS, "_profileMatchesActiveProfile"),
            _extract_function(SESSIONS_JS, "_getSessionCompletionUnread"),
            _extract_function(SESSIONS_JS, "_saveSessionCompletionUnread"),
            _extract_function(SESSIONS_JS, "_clearCronSessionCompletionUnreadForInactiveProfiles"),
            _extract_function(SESSIONS_JS, "_hasSessionCompletionUnread"),
            _extract_function(SESSIONS_JS, "_hasUnreadForSession"),
        ]
    )
    script = f"""
const store={{'hermes-session-completion-unread':JSON.stringify({{
  'root-cron':{{message_count:2, completed_at:1, source:'cron', profile:'default'}},
  'other-cron':{{message_count:1, completed_at:1, source:'cron', profile:'other'}},
}})}};
global.localStorage={{
  getItem:(key)=>Object.prototype.hasOwnProperty.call(store,key)?store[key]:null,
  setItem:(key,value)=>{{ store[key]=String(value); }},
  removeItem:(key)=>{{ delete store[key]; }},
}};
let _sessionCompletionUnread=null;
let _sessionViewedCounts={{}};
const SESSION_COMPLETION_UNREAD_KEY='hermes-session-completion-unread';
// Renamed root profile is active.
global.S={{activeProfile:'kinni',activeProfileIsDefault:true}};
global._allSessions=[];
global.renderSessionListFromCache=()=>{{}};
function _getSessionViewedCounts(){{ return _sessionViewedCounts; }}
function _setSessionViewedCount(){{}}
{helpers}
_clearCronSessionCompletionUnreadForInactiveProfiles('kinni');
const persisted=JSON.parse(store['hermes-session-completion-unread']);
process.stdout.write(JSON.stringify({{
  rootKept:_hasUnreadForSession({{session_id:'root-cron'}}),
  otherCleared:!_hasUnreadForSession({{session_id:'other-cron'}}),
  persisted,
}}));
"""
    result = subprocess.run(
        [NODE, "-e", script], check=True, capture_output=True, text=True, timeout=30
    )
    state = json.loads(result.stdout)
    assert state["rootKept"] is True
    assert state["otherCleared"] is True
    assert "root-cron" in state["persisted"]
    assert "other-cron" not in state["persisted"]
