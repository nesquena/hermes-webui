"""Behavioral coverage for settled bare-session references and profile opens."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.js_source_extract import extract_function
from tests.test_cross_session_message_load_isolation import (
    ENSURE_MESSAGES_LOADED_SRC,
    INFLIGHT_HAS_VISIBLE_STATE_SRC,
    LOAD_SESSION_SRC,
    MERGE_PENDING_SESSION_MESSAGE_SRC,
    SELECT_LIVE_RECOVERY_INFLIGHT_SRC,
    _NODE_SCRIPT_TEMPLATE,
)


ROOT = Path(__file__).resolve().parent.parent
UI_SRC = (ROOT / "static/ui.js").read_text(encoding="utf-8")
SESSIONS_SRC = (ROOT / "static/sessions.js").read_text(encoding="utf-8")
PANELS_SRC = (ROOT / "static/panels.js").read_text(encoding="utf-8")
BOOT_SRC = (ROOT / "static/boot.js").read_text(encoding="utf-8")
MESSAGES_SRC = (ROOT / "static/messages.js").read_text(encoding="utf-8")
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")


def _run_node(source: str) -> str:
    result = subprocess.run(
        [NODE],
        input=source,
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode:
        raise RuntimeError(result.stderr)
    return result.stdout.strip()


def _extract(source: str, name: str, prefix: str = "function") -> str:
    return extract_function(source, name, prefix=prefix)


def _render_driver(request: str) -> str:
    session_helpers = "\n".join(
        f"eval({code!r});"
        for code in (
            _extract(SESSIONS_SRC, "_sessionReferenceProfileIsValid"),
            _extract(SESSIONS_SRC, "_sessionUrlForSid"),
        )
    )
    ui_helpers = "\n".join(
        f"eval({code!r});"
        for code in (
            _extract(UI_SRC, "_matchBacktickFenceLine"),
            _extract(UI_SRC, "_isBacktickFenceClose"),
            _extract(UI_SRC, "_renderUserFencedBlocks"),
            _extract(UI_SRC, "_stripXmlToolCallsDisplay"),
            _extract(UI_SRC, "renderMd"),
            _extract(UI_SRC, "_renderCacheKey"),
            _extract(UI_SRC, "_linkBareSessionReferences"),
            _extract(UI_SRC, "_getCachedRender"),
        )
    )
    return """
global.window = {
  _renderUserMarkdown: true,
  location: { href: 'https://example.test/app/', pathname: '/app/', search: '', hash: '' },
};
global.document = { baseURI: 'https://example.test/app/' };
global.localStorage = { getItem(){ return null; }, setItem(){}, removeItem(){} };
global.history = { replaceState(){}, pushState(){} };
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => (
  {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const _dynamicModelLabels = {};
function _isSafeDataImageUri() { return false; }
function _inlineMediaHtmlForRef(ref) {
  return '<img class="msg-media-img" src="api/media?path=' + encodeURIComponent(String(ref || '')) + '" alt="image">';
}
const _renderCache = new Map();
const _renderCacheMax = 300;
""" + session_helpers + "\n" + ui_helpers + f"""
const request = {request};
window._renderUserMarkdown = request.userMarkdown !== false;
process.stdout.write(_getCachedRender(String(request.text || ''), !!request.user,
  {{linkSessionReferences: request.linkSessionReferences !== false}}));
"""


def _render(
    text: str,
    *,
    user: bool = False,
    user_markdown: bool = True,
    link_session_references: bool = True,
) -> str:
    request = json.dumps({
        "text": text,
        "user": user,
        "userMarkdown": user_markdown,
        "linkSessionReferences": link_session_references,
    })
    return _run_node(_render_driver(request))


def test_settled_assistant_and_user_rendering_link_valid_wrappers_and_profiles():
    text = (
        "_@session:abc123_ __@session:def456__ &quot;@session:quoted123&quot; "
        "«@session:guillemet123» 「@session:cjk123」 @session:p/abc123 @session:a_b "
        "__@session:tail___"
    )
    for user, user_markdown in ((False, True), (True, True), (True, False)):
        out = _render(text, user=user, user_markdown=user_markdown)
        assert out.count('data-session-ref="1"') == 8
        assert 'data-session-id="a_b"' in out
        assert 'data-session-id="tail_"' in out
        assert 'data-session-profile="p"' in out
        assert 'href="/app/session/abc123' in out
        assert 'href="/app/session/abc123?profile=p' in out
        assert out.count('data-session-id="abc123"') == 2


def test_settled_user_plain_renderer_links_bare_text_but_not_fenced_code():
    out = _render(
        "plain @session:user123\n\n```text\n@session:code123\n```",
        user=True,
        user_markdown=False,
    )
    assert out.count('data-session-ref="1"') == 1
    assert '@session:code123' in out
    assert 'data-session-id="code123"' not in out


@pytest.mark.parametrize(
    "text",
    [
        "`@session:inline123`",
        "[existing](https://example.test/session/existing?ref=@session:link123)",
        "![image](https://example.test/@session:image123.png)",
        "https://example.test/path/@session:url123?q=@session:query123",
        "@session:",
        "@session:p/",
        "@session:p//abc123",
        "@session:p/abc123/extra",
        "@session:abc.def",
    ],
)
def test_settled_rendering_protects_code_links_images_urls_and_malformed_source(text):
    out = _render(text)
    assert 'data-session-ref="1"' not in out
    if text.startswith("@session"):
        assert out.count("@session") == 1


@pytest.mark.parametrize(
    "text",
    [
        "<code></a>@session:x</code>",
        "<pre></code>@session:x</pre>",
        '<a href="/target"></code>@session:x</a>',
    ],
)
def test_settled_rendering_keeps_references_in_malformed_protected_markup(text):
    out = _render(text)
    assert 'data-session-ref="1"' not in out
    assert "@session:x" in out


def test_valid_reference_stops_before_punctuation_without_prefix_link():
    out = _render("@session:p/not-valid!")
    assert out.count('data-session-ref="1"') == 1
    assert 'data-session-id="not-valid"' in out
    assert out.endswith('!</p>')


@pytest.mark.parametrize("punctuation", [".", "?", ":"])
def test_terminal_prose_punctuation_is_not_treated_as_a_url_suffix(punctuation):
    out = _render(f"See @session:abc123{punctuation}")
    assert out.count('data-session-ref="1"') == 1
    assert f"@session:abc123</a>{punctuation}" in out


def test_escaped_reference_remains_literal():
    out = _render(r"\@session:escaped123")
    assert 'data-session-ref="1"' not in out
    assert "@session:escaped123" in out


def test_live_render_cache_path_does_not_link_and_does_not_reuse_settled_html():
    text = "@session:live123"
    live = _render(text, link_session_references=False)
    settled = _render(text)
    assert 'data-session-ref="1"' not in live
    assert settled.count('data-session-ref="1"') == 1
    assert "_getCachedRender(displayContent, isUser, {linkSessionReferences:!m._live})" in UI_SRC
    assert "_getCachedRender(partDisplayText,false,{linkSessionReferences:!m._live})" in UI_SRC


def test_final_html_tokenizer_does_not_split_on_quoted_tag_delimiters():
    linker = _extract(UI_SRC, "_linkBareSessionReferences")
    source = f"""
const esc = value => String(value);
const _sessionUrlForSid = sid => '/session/' + sid;
eval({linker!r});
const html = '<span title="x > @session:attribute123">safe</span>';
process.stdout.write(_linkBareSessionReferences(html));
"""
    assert _run_node(source) == '<span title="x > @session:attribute123">safe</span>'


@pytest.mark.parametrize(
    "source",
    [
        '<span title="x > @session:attribute123">safe</span>',
        '<img alt="x > @session:imageattr123" src="x">',
    ],
)
def test_rendered_raw_html_attribute_residue_is_not_linked_as_prose(source):
    out = _render(source)
    assert 'data-session-ref="1"' not in out


def test_finalized_linker_handles_many_references_and_hostile_unfinished_candidate():
    references = " ".join(f"@session:s{i}" for i in range(1500))
    linked = _render(references)
    assert linked.count('data-session-ref="1"') == 1500

    unfinished = "@session:" + ("a" * 100_000)
    preserved = _render(unfinished)
    assert 'data-session-ref="1"' not in preserved
    assert preserved.count("@session:") == 1
    assert preserved.count("a") == 100_000


def test_streaming_incremental_writes_keep_bare_references_as_text():
    smd_write = _extract(MESSAGES_SRC, "_smdWrite")
    source = f"""
let writes = [];
let _smdParser = {{}};
let _smdWrittenLen = 0;
let _smdWrittenText = '';
const assistantBody = {{ textContent: '', innerHTML: '' }};
const window = {{ smd: {{ parser_write(_parser, text) {{ writes.push(String(text)); }} }} }};
function _scheduleStreamingKatex() {{}}
eval({smd_write!r});
_smdWrite('@session:smd123');
_smdWrittenLen = 0;
_smdWrittenText = '';
_smdParser = {{}};
_smdWrite('@session:fade123', true);
process.stdout.write(JSON.stringify(writes));
"""
    writes = json.loads(_run_node(source))
    assert writes == ["@session:smd123", "@session:fade123"]
    assert "<a" not in "".join(writes)


def test_profile_query_intent_rejects_duplicates_and_invalid_values():
    profile_intent = _extract(SESSIONS_SRC, "_profileQueryIntentFromLocation")
    source = f"""
global.window = {{ location: {{ search: '?profile=ops&profile=other' }} }};
eval({profile_intent!r});
const duplicate = _profileQueryIntentFromLocation();
window.location.search = '?profile=../bad';
const invalid = _profileQueryIntentFromLocation();
process.stdout.write(JSON.stringify({{duplicate, invalid}}));
"""
    payload = json.loads(_run_node(source))
    assert payload["duplicate"]["hasParam"] is True
    assert payload["duplicate"]["valid"] is False
    assert payload["invalid"]["valid"] is False


def _navigation_base() -> str:
    functions = "\n".join(
        f"globalThis.{name}=(0,eval)('('+{code!r}+')');"
        for name, code in (
            ("_profileMatchesActiveProfile", _extract(SESSIONS_SRC, "_profileMatchesActiveProfile")),
            ("_profileMatchesProfileState", _extract(SESSIONS_SRC, "_profileMatchesProfileState")),
            ("_sessionReferenceIdIsValid", _extract(SESSIONS_SRC, "_sessionReferenceIdIsValid")),
            ("_sessionReferenceProfileIsValid", _extract(SESSIONS_SRC, "_sessionReferenceProfileIsValid")),
            ("_parseSessionReference", _extract(SESSIONS_SRC, "_parseSessionReference")),
            ("_sessionProfilesMatch", _extract(SESSIONS_SRC, "_sessionProfilesMatch")),
            ("_sessionPayloadProfileForExpected", _extract(SESSIONS_SRC, "_sessionPayloadProfileForExpected")),
            ("_quarantineExplicitInflight", _extract(SESSIONS_SRC, "_quarantineExplicitInflight")),
            ("_profileQueryIntentFromLocation", _extract(SESSIONS_SRC, "_profileQueryIntentFromLocation")),
            ("_sessionProfileMismatchFromError", _extract(SESSIONS_SRC, "_sessionProfileMismatchFromError")),
            ("_preflightSessionReference", _extract(SESSIONS_SRC, "_preflightSessionReference", "async function")),
            ("_restoreSessionReference", _extract(SESSIONS_SRC, "_restoreSessionReference", "async function")),
            ("_sessionUrlForSid", _extract(SESSIONS_SRC, "_sessionUrlForSid")),
            ("_setActiveSessionUrl", _extract(SESSIONS_SRC, "_setActiveSessionUrl")),
            ("_openSessionReference", _extract(SESSIONS_SRC, "_openSessionReference", "async function")),
        )
    )
    return """
var _sessionNavigationGeneration = 0;
var S = {
  activeProfile: 'default', activeProfileIsDefault: true,
  session: { session_id: 'old', profile: 'default' },
  messages: ['old-message'], toolCalls: ['old-tool'], activeStreamId: 'old-stream', busy: true,
};
var apiMode = 'mismatch';
var loadMode = 'success';
var calls = [];
var profileSwitchCalls = [];
var INFLIGHT = {};
var clearedInflight = [];
var _loadingOlder=false;
var _messagesTruncated=false;
var _oldestIdx=7;
var _messageRenderWindowSize=80;
var _pendingCarryForwardSnapshot={owner:'old'};
var _messageUserUnpinned=true;
var _scrollPinned=false;
const storage = {
  'hermes-webui-session': 'old',
  'hermes-webui-model': 'old-model',
  'hermes-webui-model-state': 'old-model-state',
};
global.localStorage = {
  getItem(key){ return Object.prototype.hasOwnProperty.call(storage, key) ? storage[key] : null; },
  setItem(key, value){ storage[key] = String(value); },
  removeItem(key){ delete storage[key]; },
};
global.window = {
  location: { href: 'https://example.test/app/session/old?keep=1#h', pathname: '/app/session/old', search: '?keep=1', hash: '#h' },
};
global.document = { baseURI: 'https://example.test/app/' };
function setUrl(value){
  const next = new URL(value, window.location.href);
  window.location.href = next.href;
  window.location.pathname = next.pathname;
  window.location.search = next.search;
  window.location.hash = next.hash;
}
global.history = { replaceState(_s, _t, value){ setUrl(value); }, pushState(_s, _t, value){ setUrl(value); } };
window.history = global.history;
function _cronProfileNameIsRootAlias(name){ return name === 'root'; }
function clearInflightState(sid){ clearedInflight.push(sid); }
global.api = async (url, options = {}) => {
  if (options.method === 'POST') {
    const body = JSON.parse(options.body || '{}');
    S.activeProfile = body.name;
    S.activeProfileIsDefault = body.name === 'default' || body.name === 'root';
    return { active: body.name, is_default: S.activeProfileIsDefault };
  }
  if (apiMode === 'wrong-mismatch') {
    const error = new Error('mismatch');
    error.status = 409;
    error.body = JSON.stringify({ code: 'session_profile_mismatch', session_id: 'other', profile: 'ops' });
    throw error;
  }
  if (apiMode === 'mismatch') {
    const error = new Error('mismatch');
    error.status = 409;
    error.body = JSON.stringify({ code: 'session_profile_mismatch', session_id: 'target', profile: 'ops' });
    throw error;
  }
  return { session: { session_id: 'target', profile: 'ops' } };
};
global.switchToProfile = async (profile, options = {}) => {
  profileSwitchCalls.push({profile, options});
  S.activeProfile = profile;
  S.activeProfileIsDefault = profile === 'default' || profile === 'root';
  return true;
};
global.loadSession = async (sid, options) => {
  calls.push({ sid, options, inflightAtLoad: INFLIGHT[sid] || null });
  if (sid === 'old') {
    S.session = { session_id: 'old', profile: 'default' };
    S.messages = ['old-message'];
    S.toolCalls = ['old-tool'];
    S.busy = true;
    _loadingOlder=false;
    _messagesTruncated=false;
    _oldestIdx=7;
    _messageRenderWindowSize=80;
    _pendingCarryForwardSnapshot={owner:'old'};
    _messageUserUnpinned=true;
    _scrollPinned=false;
    localStorage.setItem('hermes-webui-session', 'old');
    return true;
  }
  S.session = { session_id: loadMode === 'wrong-sid' ? 'other' : sid, profile: loadMode === 'wrong-profile' ? 'other' : 'ops' };
  S.messages = ['target-message'];
  S.toolCalls = ['target-tool'];
  S.busy = false;
  _loadingOlder=true;
  _messagesTruncated=true;
  _oldestIdx=999;
  _messageRenderWindowSize=1;
  _pendingCarryForwardSnapshot={owner:'target'};
  _messageUserUnpinned=false;
  _scrollPinned=true;
  localStorage.setItem('hermes-webui-session', S.session.session_id);
  if (loadMode !== 'success') history.pushState(null, '', '/app/session/' + encodeURIComponent(S.session.session_id));
  return loadMode === 'fail' ? false : true;
};
""" + functions + "\n"


def _navigation_run(body: str) -> dict:
    return json.loads(_run_node(_navigation_base() + body))


def test_explicit_reference_success_switches_owner_and_keeps_query():
    payload = _navigation_run(
        """
(async () => {
  const result = await _openSessionReference('target', 'ops');
  process.stdout.write(JSON.stringify({
    result, active: S.activeProfile, session: S.session.session_id,
    href: window.location.pathname + window.location.search + window.location.hash,
        persisted: localStorage.getItem('hermes-webui-session'),
        options: calls[0] && calls[0].options,
        profileSwitch: profileSwitchCalls[0],
  }));
})();
"""
    )
    assert payload["result"] is True
    assert payload["active"] == "ops"
    assert payload["session"] == "target"
    assert payload["href"] == "/app/session/target?keep=1&profile=ops#h"
    assert payload["persisted"] == "target"
    assert payload["options"]["skipProfileResolve"] is True
    assert payload["options"]["_ignorePersistedInflight"] is True
    assert payload["profileSwitch"]["profile"] == "ops"
    assert payload["profileSwitch"]["options"]["openExistingSession"] is True


def test_explicit_preload_hook_keeps_extension_payload_shape():
    payload = _navigation_run(
        """
var hookArgs = null;
global._hermesNotifySessionOpen = (...args) => { hookArgs = args; return {}; };
(async () => {
  const result = await _openSessionReference('target', 'ops');
  process.stdout.write(JSON.stringify({result, hookArgs}));
})();
"""
    )
    assert payload["result"] is True
    assert payload["hookArgs"][0] == "target"
    assert payload["hookArgs"][1] is None
    assert payload["hookArgs"][2]["preload"] is True
    assert payload["hookArgs"][2]["opts"]["explicitProfile"] == "ops"


def test_same_sid_explicit_profile_forces_real_load_and_promotes_owner_query():
    payload = _navigation_run(
        """
S.session = {session_id: 'target', profile: 'default'};
S.messages = ['same-session-old'];
(async () => {
  const result = await _openSessionReference('target', 'ops');
  process.stdout.write(JSON.stringify({
    result, active: S.activeProfile, session: S.session,
    href: window.location.pathname + window.location.search + window.location.hash,
    options: calls[0] && calls[0].options,
  }));
})();
"""
    )
    assert payload["result"] is True
    assert payload["active"] == "ops"
    assert payload["session"] == {"session_id": "target", "profile": "ops"}
    assert payload["href"] == "/app/session/target?keep=1&profile=ops#h"
    assert payload["options"]["force"] is True


@pytest.mark.parametrize("load_mode", ["wrong-sid", "wrong-profile", "fail"])
def test_explicit_reference_wrong_response_or_message_failure_rolls_back(load_mode):
    payload = _navigation_run(
        f"""
loadMode = {load_mode!r};
(async () => {{
  const result = await _openSessionReference('target', 'ops');
  process.stdout.write(JSON.stringify({{
    result, active: S.activeProfile, session: S.session.session_id,
    messages: S.messages, toolCalls: S.toolCalls, busy: S.busy,
    href: window.location.pathname + window.location.search + window.location.hash,
    persisted: localStorage.getItem('hermes-webui-session'),
    model: localStorage.getItem('hermes-webui-model'),
    modelState: localStorage.getItem('hermes-webui-model-state'),
    loadState: {{_loadingOlder,_messagesTruncated,_oldestIdx,_messageRenderWindowSize,
      _pendingCarryForwardSnapshot,_messageUserUnpinned,_scrollPinned}},
  }}));
}})();
"""
    )
    assert payload["result"] is False
    assert payload["active"] == "default"
    assert payload["session"] == "old"
    assert payload["messages"] == ["old-message"]
    assert payload["toolCalls"] == ["old-tool"]
    assert payload["href"] == "/app/session/old?keep=1#h"
    assert payload["persisted"] == "old"
    assert payload["model"] == "old-model"
    assert payload["modelState"] == "old-model-state"
    assert payload["loadState"] == {
        "_loadingOlder": False,
        "_messagesTruncated": False,
        "_oldestIdx": 7,
        "_messageRenderWindowSize": 80,
        "_pendingCarryForwardSnapshot": {"owner": "old"},
        "_messageUserUnpinned": True,
        "_scrollPinned": False,
    }


def test_explicit_history_failure_restores_previous_url_and_preflight_does_not_load():
    payload = _navigation_run(
        """
(async () => {
  setUrl('/app/session/target?profile=ops#h');
  loadMode = 'fail';
  const failed = await _openSessionReference('target', 'ops', {rollbackUrl:'/app/session/old?keep=1#h'});
  const afterLoadFailure = window.location.pathname + window.location.search + window.location.hash;
  apiMode = 'wrong-mismatch';
  setUrl('/app/session/target?profile=ops#h');
  const rejected = await _openSessionReference('target', 'ops', {rollbackUrl:'/app/session/old?keep=1#h'});
  process.stdout.write(JSON.stringify({
    failed, rejected, afterLoadFailure,
    afterPreflightFailure: window.location.pathname + window.location.search + window.location.hash,
    calls,
  }));
})();
"""
    )
    assert payload["failed"] is False
    assert payload["rejected"] is False
    assert payload["afterLoadFailure"] == "/app/session/old?keep=1#h"
    assert payload["afterPreflightFailure"] == "/app/session/old?keep=1#h"
    assert [call["sid"] for call in payload["calls"]] == ["target", "old"]


def test_explicit_reference_rejects_wrong_mismatch_owner_or_sid_without_discovery():
    payload = _navigation_run(
        """
(async () => {
  apiMode = 'wrong-mismatch';
  const wrongSid = await _openSessionReference('target', 'ops');
  const wrongOwner = await _openSessionReference('target', 'other');
  process.stdout.write(JSON.stringify({ wrongSid, wrongOwner, calls }));
})();
"""
    )
    assert payload["wrongSid"] is False
    assert payload["wrongOwner"] is False
    assert payload["calls"] == []


def test_profile_matching_uses_root_alias_without_mutating_active_state():
    source = f"""
var S = {{ activeProfileIsDefault: false }};
function _cronProfileNameIsRootAlias(name) {{ return name === 'root'; }}
globalThis._profileMatchesActiveProfile = (0,eval)('('+{_extract(SESSIONS_SRC, '_profileMatchesActiveProfile')!r}+')');
globalThis._profileMatchesProfileState = (0,eval)('('+{_extract(SESSIONS_SRC, '_profileMatchesProfileState')!r}+')');
globalThis._sessionProfilesMatch = (0,eval)('('+{_extract(SESSIONS_SRC, '_sessionProfilesMatch')!r}+')');
const alias = _sessionProfilesMatch('default', 'root');
const unrelated = _sessionProfilesMatch('ops', 'other');
process.stdout.write(JSON.stringify({{alias, unrelated, activeDefault: S.activeProfileIsDefault}}));
"""
    payload = json.loads(_run_node(source))
    assert payload == {"alias": True, "unrelated": False, "activeDefault": False}


def test_profile_matching_uses_authoritative_active_default_before_roster_cache():
    source = f"""
var S = {{activeProfile:'main', activeProfileIsDefault:true}};
function _cronProfileNameIsRootAlias() {{ return false; }}
globalThis._profileMatchesProfileState = (0,eval)('('+{_extract(SESSIONS_SRC, '_profileMatchesProfileState')!r}+')');
globalThis._sessionProfilesMatch = (0,eval)('('+{_extract(SESSIONS_SRC, '_sessionProfilesMatch')!r}+')');
process.stdout.write(JSON.stringify({{
  forward:_sessionProfilesMatch('default','main'),
  reverse:_sessionProfilesMatch('main','default'),
}}));
"""
    assert json.loads(_run_node(source)) == {"forward": True, "reverse": True}


def test_profileless_legacy_payload_is_accepted_only_when_active_owner_proves_it():
    preflight = _extract(SESSIONS_SRC, "_preflightSessionReference", "async function")
    helper_names = (
        "_sessionReferenceProfileIsValid",
        "_profileMatchesProfileState",
        "_sessionProfilesMatch",
        "_sessionPayloadProfileForExpected",
        "_sessionProfileMismatchFromError",
    )
    helpers = "\n".join(
        f"globalThis.{name}=(0,eval)('('+{_extract(SESSIONS_SRC, name)!r}+')');"
        for name in helper_names
    )
    source = f"""
var S={{activeProfile:'main',activeProfileIsDefault:true}};
function _cronProfileNameIsRootAlias(){{return false;}}
global.api=async()=>({{session:{{session_id:'legacy',profile:null}}}});
{helpers}
globalThis._preflightSessionReference=(0,eval)('('+{preflight!r}+')');
(async()=>{{
  const root=await _preflightSessionReference('legacy','default');
  const unrelated=await _preflightSessionReference('legacy','ops');
  process.stdout.write(JSON.stringify({{root,unrelated}}));
}})();
"""
    assert json.loads(_run_node(source)) == {"root": {"profile": "main"}, "unrelated": None}


def test_explicit_reference_does_not_use_unscoped_sidebar_lineage_resolution():
    payload = _navigation_run(
        """
global._resolveSessionIdFromSidebarLineage = () => 'other-profile-tip';
(async () => {
  const result = await _openSessionReference('target', 'ops');
  process.stdout.write(JSON.stringify({result, calledSid:calls[0] && calls[0].sid}));
})();
"""
    )
    assert payload == {"result": True, "calledSid": "target"}


def test_cross_profile_same_sid_inflight_is_quarantined_on_failure():
    payload = _navigation_run(
        """
INFLIGHT.target={streamId:'default-stream',messages:['default-live']};
loadMode='fail';
(async () => {
  const result=await _openSessionReference('target','ops');
  process.stdout.write(JSON.stringify({
    result,
    inflightAtLoad:calls[0]&&calls[0].inflightAtLoad,
    restored:INFLIGHT.target,
    ignored:calls[0]&&calls[0].options._ignorePersistedInflight,
  }));
})();
"""
    )
    assert payload["result"] is False
    assert payload["inflightAtLoad"] is None
    assert "restored" not in payload
    assert payload["ignored"] is True


def _canonical_switch_source() -> str:
    return _extract(PANELS_SRC, "switchToProfile", "async function")


def _canonical_switch_harness(race: str) -> dict:
    source = f"""
var S={{activeProfile:'default',activeProfileIsDefault:true,
  session:{{session_id:'same',profile:'default'}},messages:['visible']}};
var _profileSwitchGeneration=0;
var _profileSwitchOpeningExistingSession=false;
var _sessionNavigationGeneration=1;
var _sessionListSkeletonActive=false;
var _workspacePanelMode='closed';
var posts=[];
var renderStarted=false;
var renderCount=0;
var lifecycle=[];
global.window={{}};
global.localStorage={{getItem(){{return null;}},removeItem(){{}},setItem(){{}}}};
global.$=()=>null;
global.t=()=>'';
global._invalidateSessionListRenders=()=>lifecycle.push('invalidate-list');
global._setProfileSwitchListEmbargo=(on)=>lifecycle.push(on?'embargo-on':'embargo-off');
global.showSessionListSkeleton=(name)=>lifecycle.push('skeleton:'+name);
global.bumpWorkspaceTreeGen=()=>lifecycle.push('workspace-generation');
global.startGatewaySSE=()=>lifecycle.push('gateway-sse');
global.applyBotName=()=>lifecycle.push('bot-name');
global._clearPersistedModelState=()=>lifecycle.push('clear-model');
global.refreshProfileTransitionReasoningChip=()=>lifecycle.push('reasoning');
global._resetCronUnreadForProfileSwitch=()=>lifecycle.push('cron-reset');
global.animateNextSessionListRefresh=()=>{{}};
global.closeSessionActionMenu=()=>{{}};
global.syncTopbar=()=>{{}};
global.showToast=()=>{{}};
global._profileSwitchPanelLoad=async()=>{{}};
global._refreshProfileSwitchBackground=()=>{{}};
global.api=async(url,options)=>{{
  const name=JSON.parse(options.body).name;
  posts.push(name);
  {race}
  return {{active:name,is_default:name==='default'}};
}};
global.renderSessionList=async()=>{{
  renderStarted=true;
  renderCount+=1;
  if(renderCount===1) _sessionNavigationGeneration=2;
}};
eval({_canonical_switch_source()!r});
(async()=>{{
  const result=await switchToProfile('ops',{{openExistingSession:true,navigationGeneration:1}});
  process.stdout.write(JSON.stringify({{result,posts,active:S.activeProfile,activeDefault:S.activeProfileIsDefault,
    session:S.session,renderStarted,lifecycle}}));
}})();
"""
    return json.loads(_run_node(source))


def test_canonical_switch_compensates_exact_post_render_same_sid_supersession():
    payload = _canonical_switch_harness("")
    assert payload["result"] is False
    assert payload["posts"] == ["ops", "default"]
    assert payload["active"] == "default"
    assert payload["activeDefault"] is True
    assert payload["session"] == {"session_id": "same", "profile": "default"}
    assert "workspace-generation" in payload["lifecycle"]
    assert "gateway-sse" in payload["lifecycle"]


def test_canonical_switch_compensates_when_navigation_supersedes_profile_post():
    payload = _canonical_switch_harness(
        "if(posts.length===1) { _sessionNavigationGeneration=2; }"
    )
    assert payload["result"] is False
    assert payload["posts"] == ["ops", "default"]
    assert payload["active"] == "default"


def _canonical_switch_model_workspace_harness() -> dict:
    source = f"""
var S={{activeProfile:'default',activeProfileIsDefault:true,
  session:{{session_id:'same',profile:'default'}},messages:['visible'],
  _profileDefaultWorkspace:'/default-workspace',_profileSwitchWorkspace:'/default-pending-workspace',
  _pendingProfileModel:'default-model',_pendingProfileModelProvider:'default-provider'}};
var _profileSwitchGeneration=0;
var _profileSwitchOpeningExistingSession=false;
var _sessionNavigationGeneration=1;
var _sessionListSkeletonActive=false;
var _workspacePanelMode='closed';
var _workspaceList=null;
var _skillsData=null;
var renderCount=0;
var posts=[];
var lifecycle=[];
var storage={{'hermes-webui-model':'default-model'}};
global.window={{_defaultModel:'default-model',_activeProvider:'default-provider'}};
global.localStorage={{
  getItem(key){{return Object.prototype.hasOwnProperty.call(storage,key)?storage[key]:null;}},
  setItem(key,value){{storage[key]=String(value);}},
  removeItem(key){{delete storage[key];}},
}};
global.$=()=>null;
global.t=()=>' ';
global._invalidateSessionListRenders=()=>lifecycle.push('invalidate-list');
global._setProfileSwitchListEmbargo=(on)=>lifecycle.push(on?'embargo-on':'embargo-off');
global.showSessionListSkeleton=(name)=>lifecycle.push('skeleton:'+name);
global.bumpWorkspaceTreeGen=()=>lifecycle.push('workspace-generation');
global.startGatewaySSE=()=>lifecycle.push('gateway-sse');
global.applyBotName=()=>lifecycle.push('bot-name');
global._clearPersistedModelState=()=>{{
  lifecycle.push('clear-model');
  localStorage.removeItem('hermes-webui-model');
  localStorage.removeItem('hermes-webui-model-state');
}};
global._applyModelToDropdown=(model)=>model;
global._modelStateForSelect=()=>({{model:'ops-model',model_provider:'ops-provider'}});
global.refreshProfileTransitionReasoningChip=()=>lifecycle.push('reasoning');
global._resetCronUnreadForProfileSwitch=()=>lifecycle.push('cron-reset');
global.animateNextSessionListRefresh=()=>{{}};
global.closeSessionActionMenu=()=>{{}};
global.syncTopbar=()=>{{}};
global.showToast=()=>{{}};
global._profileSwitchPanelLoad=async()=>{{}};
global._refreshProfileSwitchBackground=()=>{{}};
global.api=async(url,options)=>{{
  const name=JSON.parse(options.body).name;
  posts.push(name);
  if(name==='ops') return {{active:'ops',is_default:false,default_model:'ops-model',default_model_provider:'ops-provider',default_workspace:'/ops-workspace'}};
  return {{active:'default',is_default:true}};
}};
global.renderSessionList=async()=>{{
  renderCount+=1;
  if(renderCount===1) _sessionNavigationGeneration=2;
}};
eval({_canonical_switch_source()!r});
(async()=>{{
  const result=await switchToProfile('ops',{{openExistingSession:true,navigationGeneration:1}});
  const state=(key)=>Object.prototype.hasOwnProperty.call(storage,key)
    ? {{present:true,value:storage[key]}} : {{present:false}};
  process.stdout.write(JSON.stringify({{result,posts,active:S.activeProfile,activeDefault:S.activeProfileIsDefault,
    session:S.session,model:window._defaultModel,provider:window._activeProvider,
    profileDefaultWorkspace:S._profileDefaultWorkspace,profileSwitchWorkspace:S._profileSwitchWorkspace,
    pendingModel:S._pendingProfileModel,pendingProvider:S._pendingProfileModelProvider,
    persistedModel:state('hermes-webui-model'),persistedModelState:state('hermes-webui-model-state'),lifecycle}}));
}})();
"""
    return json.loads(_run_node(source))


def test_superseded_profile_switch_restores_pre_navigation_model_workspace_and_storage():
    payload = _canonical_switch_model_workspace_harness()
    assert payload["result"] is False
    assert payload["posts"] == ["ops", "default"]
    assert payload["active"] == "default"
    assert payload["activeDefault"] is True
    assert payload["session"] == {"session_id": "same", "profile": "default"}
    assert payload["model"] == "default-model"
    assert payload["provider"] == "default-provider"
    assert payload["profileDefaultWorkspace"] == "/default-workspace"
    assert payload["profileSwitchWorkspace"] == "/default-pending-workspace"
    assert payload["pendingModel"] == "default-model"
    assert payload["pendingProvider"] == "default-provider"
    assert payload["persistedModel"] == {"present": True, "value": "default-model"}
    assert payload["persistedModelState"] == {"present": False}
    assert "clear-model" not in payload["lifecycle"]


def test_running_inflight_fallback_is_successful_for_ordinary_loads_but_not_explicit_owners():
    result = _run_actual_load(
        """
createEnvironment();
S.activeProfile='default'; S.activeProfileIsDefault=true;
globalThis._sessionNavigationGeneration=1;
const saved={};
globalThis.localStorage={getItem:(k)=>saved[k]||null,setItem:(k,v)=>{saved[k]=String(v);},removeItem:(k)=>{delete saved[k];}};
const apiHost=makeHarness(); globalThis.apiHost=apiHost; globalThis.api=apiHost.api;
const meta=apiHost.enqueue('/api/session?session_id=sid-ordinary&messages=0&resolve_model=0');
const msgs=apiHost.enqueue('/api/session?session_id=sid-ordinary&messages=1&resolve_model=0&msg_limit=2&expand_renderable=1');
INFLIGHT['sid-ordinary']={streamId:'stream-ordinary',messages:[{role:'assistant',content:'ordinary live tail'}],toolCalls:[]};
(async()=>{
  const pending=loadSession('sid-ordinary',{skipProfileResolve:true,skipLineageResolve:true,
    _navigationGeneration:1,force:true});
  await Promise.resolve();
  meta._resolve({session:{session_id:'sid-ordinary',profile:'default',active_stream_id:'stream-ordinary',message_count:2}});
  while(!apiHost.pending.some((entry)=>entry.url===msgs.url)) await Promise.resolve();
  msgs._reject(new Error('transcript unavailable'));
  const result=await pending;
  process.stdout.write(JSON.stringify({result,saved:saved['hermes-webui-session'],sid:S.session&&S.session.session_id,messages:S.messages}));
})();
"""
    )
    assert result["result"] is True
    assert result["saved"] == "sid-ordinary"
    assert result["sid"] == "sid-ordinary"
    assert result["messages"][0]["content"] == "ordinary live tail"


def _actual_load_script(scenario: str) -> str:
    helpers = "\n".join(
        f"globalThis.{name}=(0,eval)('('+{_extract(SESSIONS_SRC, name)!r}+')');"
        for name in (
            "_sessionReferenceIdIsValid",
            "_sessionReferenceProfileIsValid",
            "_parseSessionReference",
            "_profileMatchesActiveProfile",
            "_profileMatchesProfileState",
            "_sessionProfilesMatch",
            "_sessionPayloadProfileForExpected",
            "_quarantineExplicitInflight",
            "_serverLiveSnapshotToolId",
            "_serverLiveSnapshotInflight",
        )
    )
    source = _NODE_SCRIPT_TEMPLATE
    for marker, value in (
        ("__INFLIGHT_HAS_VISIBLE_STATE_SRC__", INFLIGHT_HAS_VISIBLE_STATE_SRC),
        ("__SELECT_LIVE_RECOVERY_INFLIGHT_SRC__", SELECT_LIVE_RECOVERY_INFLIGHT_SRC),
        ("__MERGE_PENDING_SESSION_MESSAGE_SRC__", MERGE_PENDING_SESSION_MESSAGE_SRC),
        ("__LOAD_SESSION_SRC__", LOAD_SESSION_SRC),
        ("__ENSURE_MESSAGES_LOADED_SRC__", ENSURE_MESSAGES_LOADED_SRC),
    ):
        source = source.replace(marker, value)
    source = source.split("async function waitForQueued", 1)[0]
    return source.replace(
        "globalThis._resolveSessionIdFromSidebarLineage = (sid) => sid;",
        "globalThis._resolveSessionIdFromSidebarLineage = (sid) => sid;\n"
        "globalThis._cronProfileNameIsRootAlias = () => false;\n"
        "globalThis._messageComparableText = (m) => String(m && m.content || '');\n"
        "globalThis._ensureInflightLiveAssistantMessage = () => {};\n"
        "globalThis._projectInflightMessagesForActivityBursts = (inflight) => Array.isArray(inflight && inflight.messages) ? inflight.messages : [];\n"
        "globalThis._prepareRunningLiveTail = () => false;\n"
        "globalThis._dropCurrentTurnAssistantMessages = (messages) => messages;\n"
        "globalThis._mergeInflightTailMessages = (messages, tail) => messages.concat(tail || []);\n"
        "globalThis._renderRuntimeJournalAnchorActivityScene = () => false;\n"
        "globalThis.appendThinking = () => {};\n"
        "globalThis.placeLiveToolCardsHost = () => {};\n"
        "globalThis.ensureLiveWorklogShell = () => {};\n"
        "globalThis.ensureRunActivityForCurrentTurn = () => {};\n"
        "globalThis.appendLiveToolCard = () => {};\n"
        "globalThis.document = {getElementById: () => null};\n"
        + helpers,
    ) + scenario


def _run_actual_load(scenario: str) -> dict:
    return json.loads(_run_node(_actual_load_script(scenario)))


def _actual_open_script(scenario: str) -> str:
    open_helpers = "\n".join(
        _extract(SESSIONS_SRC, name, prefix)
        for name, prefix in (
            ("_preflightSessionReference", "async function"),
            ("_restoreSessionReference", "async function"),
            ("_openSessionReference", "async function"),
        )
    )
    return _actual_load_script("") + "\n" + open_helpers + scenario


@pytest.mark.parametrize("payload", [{"session": {"session_id": "other", "profile": "ops"}}, {"session": {"session_id": "wanted", "profile": "other"}}, {}])
def test_messages_payload_identity_fails_before_transcript_mutation(payload):
    payload_json = json.dumps(payload)
    result = json.loads(_run_node(
        f"""
{_extract(SESSIONS_SRC, '_sessionReferenceProfileIsValid')}
{_extract(SESSIONS_SRC, '_profileMatchesProfileState')}
{_extract(SESSIONS_SRC, '_sessionProfilesMatch')}
{_extract(SESSIONS_SRC, '_sessionPayloadProfileForExpected')}
{ENSURE_MESSAGES_LOADED_SRC}
var _loadingSessionId='wanted', _loadSessionGeneration=1, _msgLimitMax=500;
var _messagesTruncated=false, _oldestIdx=0, _messageRenderWindowSize=20;
var _pendingCarryForwardSnapshot=null, _sameSessionForceReloadHint=null;
var MESSAGE_RENDER_WINDOW_DEFAULT=50, INFLIGHT={{}};
var S={{session:{{session_id:'wanted',profile:'ops'}},messages:[{{role:'assistant',content:'old'}}],toolCalls:['old-tool'],busy:false,activeStreamId:null}};
var syncCalls=0, clearCalls=0, cacheCalls=0;
global._cronProfileNameIsRootAlias=()=>false;
global._messageReloadLimitForSession=()=>2;
global._clearSameSessionForceReloadHint=()=>{{}};
global._syncToolCallsForLoadedMessages=()=>{{syncCalls++;}};
global.clearLiveToolCards=()=>{{clearCalls++;}};
global.clearVisibleMessageRowCache=()=>{{cacheCalls++;}};
global._currentMessageRenderWindowSize=()=>20;
global._messageRenderableMessageCount=()=>1;
global._isSessionActivelyViewedForList=()=>true;
global._setSessionViewedCount=()=>{{}};
global.syncTopbar=()=>{{}};
global.window={{}};
global.api=async()=>({payload_json});
(async()=>{{
  let error=null;
      try{{await _ensureMessagesLoaded('wanted',{{force:true,loadGeneration:1,expectedSessionId:'wanted',expectedProfile:'ops'}});}}
  catch(e){{error={{code:e.code||'',message:String(e.message||e)}};}}
  process.stdout.write(JSON.stringify({{error,messages:S.messages,toolCalls:S.toolCalls,syncCalls,clearCalls,cacheCalls}}));
}})();
""",
    ))
    assert result["error"]["code"] == "session_payload_identity"
    assert result["messages"] == [{"role": "assistant", "content": "old"}]
    assert result["toolCalls"] == ["old-tool"]
    assert result["syncCalls"] == 0
    assert result["clearCalls"] == 0
    assert result["cacheCalls"] == 0


def test_explicit_live_recovery_survives_failed_transcript_hydration_and_is_saved():
    result = _run_actual_load(
        """
createEnvironment();
S.activeProfile='ops';
S.activeProfileIsDefault=false;
globalThis._sessionNavigationGeneration=1;
const saved={};
globalThis.localStorage={getItem:(k)=>saved[k]||null,setItem:(k,v)=>{saved[k]=String(v);},removeItem:(k)=>{delete saved[k];}};
const apiHost=makeHarness();
globalThis.apiHost=apiHost; globalThis.api=apiHost.api;
const meta=apiHost.enqueue('/api/session?session_id=sid-live&messages=0&resolve_model=0');
const msgs=apiHost.enqueue('/api/session?session_id=sid-live&messages=1&resolve_model=0&msg_limit=2&expand_renderable=1', null, 'reject');
INFLIGHT['sid-live']={streamId:'stream-live',messages:[{role:'assistant',content:'live tail'}],toolCalls:[{name:'live-tool'}]};
(async()=>{
  const pending=loadSession('sid-live',{_explicitPrepared:true,_preloadNotified:true,skipProfileResolve:true,skipLineageResolve:true,
    expectedSessionId:'sid-live',expectedProfile:'ops',_navigationGeneration:1,force:true});
  await Promise.resolve();
  meta._resolve({session:{session_id:'sid-live',profile:'ops',active_stream_id:'stream-live',message_count:4}});
  while(!apiHost.pending.some((entry)=>entry.url===msgs.url)) await Promise.resolve();
  msgs._reject(new Error('transcript unavailable'));
  const result=await pending;
  process.stdout.write(JSON.stringify({result,saved:saved['hermes-webui-session'],sid:S.session&&S.session.session_id,messages:S.messages,active:S.activeStreamId}));
})();
"""
    )
    assert result["result"] is True
    assert result["saved"] == "sid-live"
    assert result["sid"] == "sid-live"
    assert result["active"] == "stream-live"
    assert result["messages"][0]["content"] == "live tail"


def test_explicit_wrong_stream_recovery_does_not_survive_failed_transcript_hydration():
    result = _run_actual_load(
        """
createEnvironment();
S.activeProfile='ops';
S.activeProfileIsDefault=false;
globalThis._sessionNavigationGeneration=1;
const apiHost=makeHarness();
globalThis.apiHost=apiHost; globalThis.api=apiHost.api;
const meta=apiHost.enqueue('/api/session?session_id=sid-live&messages=0&resolve_model=0');
const msgs=apiHost.enqueue('/api/session?session_id=sid-live&messages=1&resolve_model=0&msg_limit=2&expand_renderable=1', null, 'reject');
INFLIGHT['sid-live']={streamId:'stale-stream',messages:[{role:'assistant',content:'stale live tail'}],toolCalls:[]};
(async()=>{
  const pending=loadSession('sid-live',{_explicitPrepared:true,_preloadNotified:true,skipProfileResolve:true,skipLineageResolve:true,
    expectedSessionId:'sid-live',expectedProfile:'ops',_navigationGeneration:1,force:true});
  await Promise.resolve();
  meta._resolve({session:{session_id:'sid-live',profile:'ops',active_stream_id:'current-stream',message_count:4}});
  while(!apiHost.pending.some((entry)=>entry.url===msgs.url)) await Promise.resolve();
  msgs._reject(new Error('transcript unavailable'));
  const result=await pending;
  process.stdout.write(JSON.stringify({result,messages:S.messages,active:S.activeStreamId,inflight:INFLIGHT['sid-live']||null}));
})();
"""
    )
    assert result["result"] is False
    assert result["messages"] != [{"role": "assistant", "content": "stale live tail"}]
    assert result["active"] is None
    assert result["inflight"] is None


@pytest.mark.parametrize(
    "snapshot",
    [
        {"stream_id": "stale-stream", "messages": [{"role": "assistant", "content": "stale server tail"}]},
        {"stream_id": "", "messages": [{"role": "assistant", "content": "empty-id server tail"}]},
        {"messages": [{"role": "assistant", "content": "missing-id server tail"}]},
    ],
    ids=["stale-id", "empty-id", "missing-id"],
)
def test_explicit_invalid_server_snapshot_rolls_back_after_transcript_hydration_failure(snapshot):
    result = json.loads(_run_node(_actual_open_script(
        """
createEnvironment();
S.activeProfile='ops'; S.activeProfileIsDefault=false; S.session=null; S.messages=[];
globalThis._sessionNavigationGeneration=0;
globalThis.switchToProfile=async()=>true;
const saved={};
globalThis.localStorage={getItem:(k)=>saved[k]||null,setItem:(k,v)=>{saved[k]=String(v);},removeItem:(k)=>{delete saved[k];}};
const apiHost=makeHarness(); globalThis.apiHost=apiHost; globalThis.api=apiHost.api;
const preflight=apiHost.enqueue('/api/session?session_id=sid-live&messages=0&resolve_model=0');
const metadata=apiHost.enqueue('/api/session?session_id=sid-live&messages=0&resolve_model=0');
const msgs=apiHost.enqueue('/api/session?session_id=sid-live&messages=1&resolve_model=0&msg_limit=2&expand_renderable=1', null, 'reject');
(async()=>{
  const pending=_openSessionReference('sid-live','ops');
  await Promise.resolve();
  preflight._resolve({session:{session_id:'sid-live',profile:'ops'}});
  while(!apiHost.pending.some((entry)=>entry.url===metadata.url)) await Promise.resolve();
  metadata._resolve({session:{session_id:'sid-live',profile:'ops',active_stream_id:'current-stream',runtime_journal_snapshot:__SNAPSHOT__}});
  while(!apiHost.pending.some((entry)=>entry.url===msgs.url)) await Promise.resolve();
  msgs._reject(new Error('transcript unavailable'));
  const result=await pending;
  process.stdout.write(JSON.stringify({result,sid:S.session&&S.session.session_id,messages:S.messages,active:S.activeStreamId,
    inflight:INFLIGHT['sid-live']||null,saved:saved['hermes-webui-session']||null}));
})();
"""
        .replace("__SNAPSHOT__", json.dumps(snapshot))
    )))
    assert result["result"] is False
    assert result["sid"] is None
    assert result["messages"] == []
    assert result["active"] is None
    assert result["inflight"] is None
    assert result["saved"] is None


def test_explicit_hydration_failure_without_live_recovery_rolls_back_to_empty_boot():
    result = json.loads(_run_node(_actual_open_script(
        """
createEnvironment();
S.activeProfile='ops'; S.activeProfileIsDefault=false; S.session=null; S.messages=[];
globalThis._sessionNavigationGeneration=0;
globalThis.switchToProfile=async()=>true;
const saved={};
globalThis.localStorage={getItem:(k)=>saved[k]||null,setItem:(k,v)=>{saved[k]=String(v);},removeItem:(k)=>{delete saved[k];}};
const apiHost=makeHarness(); globalThis.apiHost=apiHost; globalThis.api=apiHost.api;
const preflight=apiHost.enqueue('/api/session?session_id=sid-no-live&messages=0&resolve_model=0');
const metadata=apiHost.enqueue('/api/session?session_id=sid-no-live&messages=0&resolve_model=0');
const msgs=apiHost.enqueue('/api/session?session_id=sid-no-live&messages=1&resolve_model=0&msg_limit=2&expand_renderable=1');
(async()=>{
  const pending=_openSessionReference('sid-no-live','ops');
  await Promise.resolve();
  preflight._resolve({session:{session_id:'sid-no-live',profile:'ops'}});
  while(!apiHost.pending.some((entry)=>entry.url===metadata.url)) await Promise.resolve();
  metadata._resolve({session:{session_id:'sid-no-live',profile:'ops',active_stream_id:null}});
  while(!apiHost.pending.some((entry)=>entry.url===msgs.url)) await Promise.resolve();
  msgs._reject(new Error('transcript unavailable'));
  const result=await pending;
  process.stdout.write(JSON.stringify({result,active:S.activeProfile,sid:S.session,messages:S.messages,saved:saved['hermes-webui-session']||null}));
})();
"""
    )))
    assert result == {
        "result": False,
        "active": "ops",
        "sid": None,
        "messages": [],
        "saved": None,
    }


def test_continuation_cross_profile_inflight_is_quarantined_and_accepted_sid_cleared():
    result = _run_actual_load(
        """
createEnvironment();
S.activeProfile='ops'; S.activeProfileIsDefault=false;
globalThis._sessionNavigationGeneration=1;
const persisted={continuation:{streamId:'stream-live',messages:[{role:'assistant',content:'wrong-owner'}]}};
const cleared=[];
globalThis.localStorage={getItem:(k)=>k==='hermes-webui-inflight-state'?JSON.stringify(persisted):null,setItem:()=>{},removeItem:()=>{}};
globalThis.clearInflightState=(sid)=>{cleared.push(sid); delete persisted[sid];};
let loadStateCalls=0;
globalThis.loadInflightState=(sid)=>{loadStateCalls++; return persisted[sid]||null;};
const apiHost=makeHarness(); globalThis.apiHost=apiHost; globalThis.api=apiHost.api;
const parentMeta=apiHost.enqueue('/api/session?session_id=parent&messages=0&resolve_model=0');
const childMeta=apiHost.enqueue('/api/session?session_id=continuation&messages=0&resolve_model=0');
const childMsgs=apiHost.enqueue('/api/session?session_id=continuation&messages=1&resolve_model=0&msg_limit=2&expand_renderable=1');
INFLIGHT.continuation={streamId:'stream-live',messages:[{role:'assistant',content:'wrong-owner'}],toolCalls:[{name:'wrong'}]};
INFLIGHT.parent={streamId:'stream-live',messages:[{role:'assistant',content:'wrong-parent-owner'}]};
INFLIGHT.unrelated={streamId:'other',messages:[{role:'assistant',content:'keep'}]};
(async()=>{
  const pending=loadSession('parent',{_explicitPrepared:true,_preloadNotified:true,skipProfileResolve:true,skipLineageResolve:true,
    expectedSessionId:'parent',expectedProfile:'ops',_navigationGeneration:1,_ignorePersistedInflight:true,force:true});
  parentMeta._resolve({session:{session_id:'parent',profile:'ops',active_stream_id:'stream-live',continuation_session_id:'continuation'}});
  while(!apiHost.pending.some((entry)=>entry.url===childMeta.url)) await Promise.resolve();
  childMeta._resolve({session:{session_id:'continuation',profile:'ops',active_stream_id:null,messages:[]}});
  while(!apiHost.pending.some((entry)=>entry.url===childMsgs.url)) await Promise.resolve();
  childMsgs._resolve({session:{session_id:'continuation',profile:'ops',messages:[{role:'assistant',content:'accepted'}],_messages_truncated:false}});
  const result=await pending;
  process.stdout.write(JSON.stringify({result,sid:S.session&&S.session.session_id,inflight:Object.keys(INFLIGHT),cleared,loadStateCalls}));
})();
"""
    )
    assert result["result"] is True
    assert result["sid"] == "continuation"
    assert "parent" not in result["inflight"]
    assert "continuation" not in result["inflight"]
    assert "unrelated" in result["inflight"]
    assert result["loadStateCalls"] == 0
    assert "continuation" in result["cleared"]


def test_load_session_rejects_wrong_sid_and_profile_before_assigning_payload():
    load_session = _extract(SESSIONS_SRC, "loadSession", "async function")
    validators = "\n".join(
        f"globalThis.{name}=(0,eval)('('+{_extract(SESSIONS_SRC, name)!r}+')');"
        for name in ("_sessionReferenceIdIsValid", "_sessionReferenceProfileIsValid", "_profileMatchesActiveProfile", "_profileMatchesProfileState", "_sessionProfilesMatch", "_sessionPayloadProfileForExpected", "_quarantineExplicitInflight")
    )
    source = f"""
var _loadingSessionId = null;
var _loadSessionGeneration = 0;
var _sessionNavigationGeneration = 0;
var _verifiedSessionProfileIntent = null;
var _yoloEnabled = false;
var INFLIGHT = {{}};
var S = {{ activeProfile: 'ops', activeProfileIsDefault: false, session: {{session_id:'old',profile:'ops'}}, messages:['old'], toolCalls:[], busy:false, activeStreamId:null, pendingFiles:[] }};
global.window = {{ location: {{ href:'https://example.test/app/session/old', pathname:'/app/session/old', search:'', hash:'' }} }};
global.document = {{ baseURI:'https://example.test/app/' }};
global.localStorage = {{ getItem(){{return 'old';}}, setItem(){{}}, removeItem(){{}} }};
global.history = {{ replaceState(){{}}, pushState(){{}} }};
function $(id) {{ return id === 'msg' ? {{value:''}} : {{innerHTML:''}}; }}
function stopApprovalPolling(){{}} function hideApprovalCard(){{}} function _updateYoloPill(){{}}
function _rearmActiveSessionStream(){{}} async function _saveComposerDraftNow(){{}}
function _clearSameSessionForceReloadHint(){{}}
global.api = async () => ({{ session: {{ session_id: 'other', profile: 'ops' }} }});
{validators}
globalThis.loadSession = (0,eval)('('+{load_session!r}+')');
(async () => {{
  const wrongSid = await loadSession('wanted', {{_explicitPrepared:true, skipProfileResolve:true, expectedSessionId:'wanted', expectedProfile:'ops', force:true, _preloadNotified:true}});
  global.api = async () => ({{ session: {{ session_id: 'wanted', profile: 'other' }} }});
  const wrongProfile = await loadSession('wanted', {{_explicitPrepared:true, skipProfileResolve:true, expectedSessionId:'wanted', expectedProfile:'ops', force:true, _preloadNotified:true}});
  process.stdout.write(JSON.stringify({{wrongSid, wrongProfile, session:S.session.session_id, profile:S.session.profile}}));
}})();
"""
    payload = json.loads(_run_node(source))
    assert payload == {"wrongSid": False, "wrongProfile": False, "session": "old", "profile": "ops"}


def test_session_url_helper_preserves_unrelated_query_and_hash_with_explicit_clear():
    url_helper = _extract(SESSIONS_SRC, "_sessionUrlForSid")
    profile_validator = _extract(SESSIONS_SRC, "_sessionReferenceProfileIsValid")
    source = f"""
global.window = {{
  location: {{ href: 'https://example.test/app/?keep=1&profile=old#h', search: '?keep=1&profile=old', hash: '#h' }},
}};
global.document = {{ baseURI: 'https://example.test/app/' }};
globalThis._sessionReferenceProfileIsValid = (0,eval)('('+{profile_validator!r}+')');
eval({url_helper!r});
process.stdout.write(JSON.stringify({{
  explicit: _sessionUrlForSid('abc123', 'ops'),
  cleared: _sessionUrlForSid('abc123', null),
}}));
"""
    payload = json.loads(_run_node(source))
    assert payload["explicit"] == "/app/session/abc123?keep=1&profile=ops#h"
    assert payload["cleared"] == "/app/session/abc123?keep=1#h"


def test_central_url_promotion_retains_verified_profile_and_clears_stale_unverified_query():
    payload = _navigation_run(
        """
S.activeProfile='ops';
S.activeProfileIsDefault=false;
S.session={session_id:'continuation',profile:'ops'};
S._verifiedSessionProfileIntent={sid:'parent',profile:'ops'};
setUrl('/app/session/parent?keep=1&profile=ops#h');
_setActiveSessionUrl('continuation');
const retained=window.location.pathname+window.location.search+window.location.hash;
const verified=S._verifiedSessionProfileIntent;
S.activeProfile='default';
S.activeProfileIsDefault=true;
S.session={session_id:'plain',profile:'default'};
S._verifiedSessionProfileIntent=null;
setUrl('/app/session/continuation?keep=1&profile=ops#h');
_setActiveSessionUrl('plain');
const cleared=window.location.pathname+window.location.search+window.location.hash;
process.stdout.write(JSON.stringify({retained,verified,cleared}));
"""
    )
    assert payload == {
        "retained": "/app/session/continuation?keep=1&profile=ops#h",
        "verified": {"sid": "continuation", "profile": "ops"},
        "cleared": "/app/session/plain?keep=1#h",
    }


def test_popstate_routes_same_sid_different_profile_and_clears_profileless_intent():
    popstate = _extract(SESSIONS_SRC, "_handleSessionPopstate")
    sid_from_location = _extract(SESSIONS_SRC, "_sessionIdFromLocation")
    profile_intent = _extract(SESSIONS_SRC, "_profileQueryIntentFromLocation")
    profile_state = _extract(SESSIONS_SRC, "_profileMatchesProfileState")
    profile_matches = _extract(SESSIONS_SRC, "_sessionProfilesMatch")
    source = f"""
var S = {{activeProfile:'ops', activeProfileIsDefault:false, session:{{session_id:'same', profile:'ops'}}, busy:false, _verifiedSessionProfileIntent:{{sid:'same', profile:'ops'}}}};
global.window = {{location:{{pathname:'/app/session/same', search:'?profile=other'}}}};
global._openSessionReference = async (...args) => {{ calls.push(['open', ...args]); return true; }};
global.loadSession = async (...args) => {{ calls.push(['load', ...args]); return true; }};
var calls = [];
function _cronProfileNameIsRootAlias(name) {{ return name === 'root'; }}
eval({sid_from_location!r});
eval({profile_intent!r});
eval({profile_state!r});
eval({profile_matches!r});
eval({popstate!r});
(async () => {{
  await _handleSessionPopstate();
  window.location.search = '';
  await _handleSessionPopstate();
  process.stdout.write(JSON.stringify(calls));
}})();
"""
    calls = json.loads(_run_node(source))
    assert calls == [
        ["open", "same", "other"],
        ["load", "same", {"clearProfileIntent": True}],
    ]


def test_boot_restore_helper_fails_closed_for_invalid_or_duplicate_explicit_intent():
    restore = _extract(BOOT_SRC, "_restoreBootSession", "async function")
    intent = _extract(SESSIONS_SRC, "_profileQueryIntentFromLocation")
    source = f"""
var calls = [];
global.window = {{location:{{search:'?profile=ops&profile=other'}}}};
global._openSessionReference = async (...args) => {{ calls.push(['open', ...args]); return true; }};
global.loadSession = async (...args) => {{ calls.push(['load', ...args]); return true; }};
eval({intent!r});
eval({restore!r});
(async () => {{
  const duplicate = _profileQueryIntentFromLocation();
  const duplicateResult = await _restoreBootSession('sid', duplicate, 'saved');
  window.location.search = '?profile=../bad';
  const invalid = _profileQueryIntentFromLocation();
  const invalidResult = await _restoreBootSession('sid', invalid, 'saved');
  process.stdout.write(JSON.stringify({{duplicateResult, invalidResult, calls}}));
}})();
"""
    payload = json.loads(_run_node(source))
    assert payload == {"duplicateResult": False, "invalidResult": False, "calls": []}


def test_boot_restore_helper_uses_explicit_production_open_and_returns_boolean():
    restore = _extract(BOOT_SRC, "_restoreBootSession", "async function")
    source = f"""
var calls = [];
global._openSessionReference = async (...args) => {{ calls.push(['open', ...args]); return true; }};
global.loadSession = async (...args) => {{ calls.push(['load', ...args]); return true; }};
eval({restore!r});
(async () => {{
  const explicit = await _restoreBootSession('sid', {{hasParam:true, valid:true, name:'ops'}}, 'saved');
  const ordinary = await _restoreBootSession(null, null, 'saved');
  process.stdout.write(JSON.stringify({{explicit, ordinary, calls}}));
}})();
"""
    payload = json.loads(_run_node(source))
    assert payload["explicit"] is True
    assert payload["ordinary"] is True
    assert payload["calls"][0] == ["open", "sid", "ops"]
    assert payload["calls"][1][0] == "load"
