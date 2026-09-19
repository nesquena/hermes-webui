"""Frontend capability guards for read-only native projects (issue #5763)."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SESSIONS_JS = ROOT / "static" / "sessions.js"
NODE = shutil.which("node")


def _run_node(driver: str, payload: dict | None = None) -> dict:
    assert NODE is not None
    result = subprocess.run(
        [NODE, "-e", driver, str(SESSIONS_JS), json.dumps(payload or {})],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, (
        f"node driver failed:\nSTDOUT={result.stdout}\nSTDERR={result.stderr}"
    )
    return json.loads(result.stdout.strip().splitlines()[-1])


_EXTRACTOR = r"""
function extractFunction(source, name) {
  for (const marker of [`async function ${name}(`, `function ${name}(`]) {
    const start = source.indexOf(marker);
    if (start < 0) continue;
    const brace = source.indexOf('{', source.indexOf(')', start));
    let depth = 0;
    for (let i = brace; i < source.length; i++) {
      if (source[i] === '{') depth++;
      else if (source[i] === '}') {
        depth--;
        if (depth === 0) return source.slice(start, i + 1);
      }
    }
  }
  throw new Error(name + ' not found');
}
"""


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_read_only_capability_fails_closed_for_native_and_malformed_flags():
    driver = (
        r"""
const fs = require('fs');
const [path] = process.argv.slice(-2);
const src = fs.readFileSync(path, 'utf8');
"""
        + _EXTRACTOR
        + r"""
eval(extractFunction(src, '_isReadOnlyProject'));
console.log(JSON.stringify({
  literalTrue: _isReadOnlyProject({read_only: true}),
  literalFalse: _isReadOnlyProject({read_only: false}),
  truthyString: _isReadOnlyProject({read_only: 'true'}),
  truthyNumber: _isReadOnlyProject({read_only: 1}),
  malformedObject: _isReadOnlyProject({read_only: {value: false}}),
  malformedNull: _isReadOnlyProject({read_only: null}),
  nativeMissing: _isReadOnlyProject({project_source: 'hermes-agent'}),
  nativeFalse: _isReadOnlyProject({project_source: 'hermes-agent', read_only: false}),
  missing: _isReadOnlyProject({}),
  nullRow: _isReadOnlyProject(null),
}));
"""
    )
    assert _run_node(driver) == {
        "literalTrue": True,
        "literalFalse": False,
        "truthyString": True,
        "truthyNumber": True,
        "malformedObject": True,
        "malformedNull": True,
        "nativeMissing": True,
        "nativeFalse": True,
        "missing": False,
        "nullRow": False,
    }


_NEW_SESSION_DRIVER = (
    r"""
const fs = require('fs');
const [path, payloadJson] = process.argv.slice(-2);
const args = JSON.parse(payloadJson);
const src = fs.readFileSync(path, 'utf8');
"""
    + _EXTRACTOR
    + r"""
globalThis.window = globalThis;
globalThis.document = {
  baseURI: 'http://example.test/',
  createElement: () => ({appendChild() {}, selectedOptions: [{dataset: {provider: ''}}]}),
};
globalThis.localStorage = {getItem: () => null, setItem: () => {}};
globalThis.history = {replaceState: () => {}};
globalThis.NO_PROJECT_FILTER = '__none__';
globalThis._activeProject = args.activeProject;
globalThis._allProjects = args.projects;
globalThis._sessionSourceFilter = 'webui';
globalThis._newSessionInFlight = null;
globalThis._messagesTruncated = false;
globalThis._oldestIdx = 0;
globalThis.INFLIGHT = {};
globalThis.S = {
  session: {session_id: 'previous', workspace: null},
  toolCalls: args.captureFailure ? ['keep-tool-call'] : [],
  messages: args.captureFailure ? ['keep-message'] : [],
  activeProfile: Object.prototype.hasOwnProperty.call(args, 'activeProfile')
    ? args.activeProfile
    : 'default',
  activeProfileIsDefault: Object.prototype.hasOwnProperty.call(args, 'activeProfileIsDefault')
    ? args.activeProfileIsDefault
    : false,
  _pendingSessionToolsets: null, _profileSwitchWorkspace: null,
  _profileDefaultWorkspace: null,
};
globalThis._allProjectsScope = Object.prototype.hasOwnProperty.call(args, 'projectScope')
  ? args.projectScope
  : {profile: S.activeProfile, allProfiles: false};
globalThis._profilesCache = {
  active: Object.prototype.hasOwnProperty.call(args, 'profilesCacheActive')
    ? args.profilesCacheActive
    : S.activeProfile,
  profiles: args.profiles || [],
};
globalThis._defaultModel = null;
globalThis._activeProvider = 'openai';
globalThis.$ = id => id === 'modelSelect'
  ? {value: 'gpt-4', selectedOptions: [{dataset: {provider: 'openai'}}]}
  : null;
for (const name of [
  '_setNewSessionPending', 'updateQueueBadge', '_clearPendingSelections',
  'clearLiveToolCards', 'setComposerStatus', 'setStatus', 'updateSendBtn',
  'syncTopbar', 'renderMessages', 'startSessionStream', '_setSessionViewedCount',
  '_setActiveSessionUrl', '_rememberNewChatDraftSession', '_hydrateTodosFromSession',
  '_setLiveAssistantTps', '_syncCtxIndicator', 'showToast'
]) globalThis[name] = () => {};
globalThis.loadDir = async () => null;
globalThis._applyModelToDropdown = () => true;
globalThis._modelStateForSelect = () => ({model: 'gpt-4', model_provider: 'openai'});
globalThis._readEmptyComposerModelOverride = () => null;
globalThis._clearEmptyComposerModelOverride = () => {};
globalThis.getModelLabel = value => value || '';
const calls = [];
globalThis.api = async (_url, opts) => {
  calls.push(JSON.parse(opts.body));
  return {session: {
    session_id: 'new', messages: [], model: 'gpt-4', model_provider: 'openai',
    workspace: null, message_count: 0, last_usage: {},
  }};
};
eval([
  extractFunction(src, '_isReadOnlyProject'),
  extractFunction(src, '_isCanonicalProjectId'),
  extractFunction(src, '_projectForId'),
  extractFunction(src, '_projectAuthorizationActiveProfile'),
  extractFunction(src, '_projectListScopeMatchesActive'),
  extractFunction(src, '_projectProfileMatchesActive'),
  extractFunction(src, '_projectCanReceiveNewSession'),
  extractFunction(src, '_newSessionProjectSelection'),
  extractFunction(src, 'newSession'),
].join('\n'));
(async () => {
  let error = null;
  try {
    await newSession(false, args.options || {});
  } catch (caught) {
    error = String(caught && caught.message ? caught.message : caught);
  }
  if (args.captureFailure) {
    console.log(JSON.stringify({
      error,
      apiCalls: calls.length,
      sessionId: S.session && S.session.session_id,
      toolCalls: S.toolCalls,
      messages: S.messages,
    }));
    return;
  }
  if (error) throw new Error(error);
  const body = calls[0];
  console.log(JSON.stringify(args.includeState
    ? {body, activeProject: globalThis._activeProject, apiCalls: calls.length}
    : body));
})().catch(error => { console.error(error.stack || error); process.exit(1); });
"""
)


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_generic_new_session_omits_active_read_only_project():
    body = _run_node(
        _NEW_SESSION_DRIVER,
        {
            "activeProject": "native-1",
            "projects": [{"project_id": "native-1", "read_only": True}],
        },
    )
    assert "project_id" not in body


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
@pytest.mark.parametrize("project", [{"read_only": False}, {}])
def test_generic_new_session_keeps_active_writable_project(project):
    project = {"project_id": "legacy-1", **project}
    body = _run_node(
        _NEW_SESSION_DRIVER,
        {
            "activeProject": "legacy-1",
            "projects": [project],
        },
    )
    assert body["project_id"] == "legacy-1"


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_generic_new_session_omits_active_project_without_cached_metadata():
    body = _run_node(
        _NEW_SESSION_DRIVER,
        {
            "activeProject": "stale-project",
            "projects": [],
        },
    )
    assert "project_id" not in body


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_stale_implicit_project_scope_is_cleared_before_unassigned_request():
    result = _run_node(
        _NEW_SESSION_DRIVER,
        {
            "activeProfile": "team-b",
            "activeProject": "team-a-project",
            "projects": [
                {
                    "project_id": "team-a-project",
                    "profile": "team-b",
                    "read_only": False,
                }
            ],
            "projectScope": {"profile": "team-a", "allProfiles": False},
            "includeState": True,
        },
    )

    assert "project_id" not in result["body"]
    assert result["activeProject"] is None
    assert result["apiCalls"] == 1


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
@pytest.mark.parametrize("selection_path", ["implicit", "explicit"])
@pytest.mark.parametrize(
    ("scope", "expected_valid"),
    [
        pytest.param(
            {"profile": "team-a", "allProfiles": False},
            True,
            id="current-active-profile-scope",
        ),
        pytest.param(
            {"profile": "team-b", "allProfiles": False},
            False,
            id="stale-profile-scope",
        ),
        pytest.param(None, False, id="missing-scope"),
        pytest.param(
            {"profile": "team-a", "allProfiles": True},
            False,
            id="all-profiles-scope",
        ),
    ],
)
def test_new_session_requires_current_active_only_project_scope(
    selection_path, scope, expected_valid
):
    project_id = "team-a-project"
    payload = {
        "activeProfile": "team-a",
        "activeProject": project_id if selection_path == "implicit" else None,
        "projects": [
            {"project_id": project_id, "profile": "team-a", "read_only": False}
        ],
        "projectScope": scope,
        "options": {"project_id": project_id} if selection_path == "explicit" else {},
    }
    if not expected_valid:
        if selection_path == "explicit":
            payload["captureFailure"] = True
        else:
            payload["includeState"] = True

    result = _run_node(_NEW_SESSION_DRIVER, payload)

    if expected_valid:
        assert result["project_id"] == project_id
    elif selection_path == "explicit":
        assert "project" in result["error"].lower()
        assert result["apiCalls"] == 0
    else:
        assert "project_id" not in result["body"]
        assert result["activeProject"] is None
        assert result["apiCalls"] == 1


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_new_session_omits_stale_foreign_profile_active_project():
    body = _run_node(
        _NEW_SESSION_DRIVER,
        {
            "activeProfile": "default",
            "activeProfileIsDefault": True,
            "activeProject": "old-legacy",
            "projects": [
                {
                    "project_id": "old-legacy",
                    "profile": "old-profile",
                    "read_only": False,
                }
            ],
        },
    )
    assert "project_id" not in body


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
@pytest.mark.parametrize("selection_path", ["implicit", "explicit"])
def test_stale_profiles_cache_default_alias_cannot_authorize_project(selection_path):
    project_id = "stale-root-project"
    payload = {
        "activeProfile": "default",
        "activeProfileIsDefault": True,
        "activeProject": project_id if selection_path == "implicit" else None,
        "projects": [
            {
                "project_id": project_id,
                "profile": "former-root",
                "read_only": False,
            }
        ],
        "profilesCacheActive": "other-profile",
        "profiles": [{"name": "former-root", "is_default": True}],
        "options": {"project_id": project_id} if selection_path == "explicit" else {},
    }
    if selection_path == "explicit":
        payload["captureFailure"] = True
    else:
        payload["includeState"] = True

    result = _run_node(_NEW_SESSION_DRIVER, payload)

    if selection_path == "explicit":
        assert "project" in result["error"].lower()
        assert result["apiCalls"] == 0
    else:
        assert "project_id" not in result["body"]
        assert result["activeProject"] is None


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
@pytest.mark.parametrize("selection_path", ["implicit", "explicit"])
@pytest.mark.parametrize(
    "provenance",
    [
        pytest.param("padded-project-scope", id="padded-project-scope"),
        pytest.param("blank-profile-cache", id="blank-profile-cache"),
        pytest.param("padded-profile-cache", id="padded-profile-cache"),
    ],
)
def test_malformed_project_provenance_cannot_authorize_new_session_project(
    selection_path, provenance
):
    project_id = "candidate-project"
    payload = {
        "activeProfile": "default",
        "activeProfileIsDefault": True,
        "activeProject": project_id if selection_path == "implicit" else None,
        "projects": [
            {"project_id": project_id, "profile": "default", "read_only": False}
        ],
        "options": {"project_id": project_id} if selection_path == "explicit" else {},
    }

    if provenance == "padded-project-scope":
        payload["projectScope"] = {"profile": " default ", "allProfiles": False}
    else:
        payload["projects"][0]["profile"] = "former-root"
        payload["profilesCacheActive"] = (
            "   " if provenance == "blank-profile-cache" else " default "
        )
        payload["profiles"] = [{"name": "former-root", "is_default": True}]

    if selection_path == "explicit":
        payload["captureFailure"] = True
    else:
        payload["includeState"] = True

    result = _run_node(_NEW_SESSION_DRIVER, payload)

    if selection_path == "explicit":
        assert "project" in result["error"].lower()
        assert result["apiCalls"] == 0
    else:
        assert "project_id" not in result["body"]
        assert result["activeProject"] is None
        assert result["apiCalls"] == 1


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
@pytest.mark.parametrize("selection_path", ["implicit", "explicit"])
@pytest.mark.parametrize(
    "active_profile",
    [
        pytest.param(" default ", id="padded"),
        pytest.param("", id="empty"),
        pytest.param("   ", id="whitespace-only"),
        pytest.param(7, id="non-string"),
    ],
)
def test_malformed_active_profile_cannot_authorize_new_session_project(
    selection_path, active_profile
):
    project_id = "candidate-project"
    payload = {
        "activeProfile": active_profile,
        "activeProfileIsDefault": True,
        "activeProject": project_id if selection_path == "implicit" else None,
        "projects": [
            {"project_id": project_id, "profile": "default", "read_only": False}
        ],
        "projectScope": {"profile": "default", "allProfiles": False},
        "options": {"project_id": project_id} if selection_path == "explicit" else {},
    }
    if selection_path == "explicit":
        payload["captureFailure"] = True
    else:
        payload["includeState"] = True

    result = _run_node(_NEW_SESSION_DRIVER, payload)

    if selection_path == "explicit":
        assert "project" in result["error"].lower()
        assert result["apiCalls"] == 0
        assert result["sessionId"] == "previous"
        assert result["toolCalls"] == ["keep-tool-call"]
        assert result["messages"] == ["keep-message"]
    else:
        assert "project_id" not in result["body"]
        assert result["activeProject"] is None
        assert result["apiCalls"] == 1


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
@pytest.mark.parametrize("selection_path", ["implicit", "explicit"])
@pytest.mark.parametrize(
    "malformed_default_flag",
    [
        pytest.param("false", id="string-false"),
        pytest.param(1, id="number-one"),
        pytest.param({"value": False}, id="object"),
    ],
)
def test_malformed_active_default_flag_cannot_authorize_root_alias_project(
    selection_path, malformed_default_flag
):
    project_id = "stale-root-project"
    payload = {
        "activeProfile": "team-a",
        "activeProfileIsDefault": malformed_default_flag,
        "activeProject": project_id if selection_path == "implicit" else None,
        "projects": [
            {
                "project_id": project_id,
                "profile": "default",
                "read_only": False,
            }
        ],
        "projectScope": {"profile": "team-a", "allProfiles": False},
        "options": {"project_id": project_id} if selection_path == "explicit" else {},
    }
    if selection_path == "explicit":
        payload["captureFailure"] = True
    else:
        payload["includeState"] = True

    result = _run_node(_NEW_SESSION_DRIVER, payload)

    if selection_path == "explicit":
        assert "project" in result["error"].lower()
        assert result["apiCalls"] == 0
        assert result["sessionId"] == "previous"
        assert result["toolCalls"] == ["keep-tool-call"]
        assert result["messages"] == ["keep-message"]
    else:
        assert "project_id" not in result["body"]
        assert result["activeProject"] is None
        assert result["apiCalls"] == 1


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_new_session_keeps_explicit_known_writable_project_id():
    body = _run_node(
        _NEW_SESSION_DRIVER,
        {
            "activeProject": None,
            "projects": [{"project_id": "legacy-1", "read_only": False}],
            "options": {"project_id": "legacy-1"},
        },
    )
    assert body["project_id"] == "legacy-1"


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_new_session_rejects_explicit_foreign_profile_project_id():
    result = _run_node(
        _NEW_SESSION_DRIVER,
        {
            "activeProfile": "default",
            "activeProfileIsDefault": True,
            "activeProject": None,
            "projects": [
                {
                    "project_id": "foreign-project",
                    "profile": "other-profile",
                    "read_only": False,
                }
            ],
            "options": {"project_id": "foreign-project"},
            "captureFailure": True,
        },
    )
    assert "project" in result["error"].lower()
    assert result["apiCalls"] == 0


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_invalid_explicit_project_rejects_before_api_or_session_reset():
    result = _run_node(
        _NEW_SESSION_DRIVER,
        {
            "activeProfile": "team-b",
            "activeProject": None,
            "projects": [
                {
                    "project_id": "team-a-project",
                    "profile": "team-b",
                    "read_only": False,
                }
            ],
            "projectScope": {"profile": "team-a", "allProfiles": False},
            "options": {"project_id": "team-a-project"},
            "captureFailure": True,
        },
    )

    assert "project" in result["error"].lower()
    assert "try again" in result["error"].lower()
    assert result["apiCalls"] == 0
    assert result["sessionId"] == "previous"
    assert result["toolCalls"] == ["keep-tool-call"]
    assert result["messages"] == ["keep-message"]


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
@pytest.mark.parametrize("selection_path", ["active", "explicit"])
@pytest.mark.parametrize(
    ("project", "active_profile", "active_is_default", "profiles", "expected"),
    [
        pytest.param(
            {"profile": "team-a"}, "team-a", False, [], True, id="exact-named-profile"
        ),
        pytest.param(
            {"profile": "team-b"}, "team-a", False, [], False, id="foreign-named-profile"
        ),
        pytest.param({}, "default", True, [], True, id="missing-profile-under-default"),
        pytest.param(
            {"profile": "   "},
            "default",
            True,
            [],
            False,
            id="blank-profile-under-default",
        ),
        pytest.param({}, "team-a", False, [], False, id="missing-profile-under-named"),
        pytest.param(
            {"profile": "default"},
            "renamed-root",
            True,
            [],
            True,
            id="default-row-under-renamed-root",
        ),
        pytest.param(
            {"profile": "renamed-root"},
            "default",
            True,
            [{"name": "renamed-root", "is_default": True}],
            True,
            id="renamed-root-row-under-default",
        ),
        pytest.param(
            {"profile": "ordinary-profile"},
            "default",
            True,
            [{"name": "ordinary-profile", "is_default": False}],
            False,
            id="default-active-does-not-match-every-profile",
        ),
    ],
)
def test_new_session_project_profile_capability_matrix(
    selection_path, project, active_profile, active_is_default, profiles, expected
):
    project_id = "candidate-project"
    payload = {
        "activeProfile": active_profile,
        "activeProfileIsDefault": active_is_default,
        "activeProject": project_id if selection_path == "active" else None,
        "projects": [{"project_id": project_id, "read_only": False, **project}],
        "profiles": profiles,
        "options": {"project_id": project_id} if selection_path == "explicit" else {},
    }
    if not expected:
        if selection_path == "explicit":
            payload["captureFailure"] = True
        else:
            payload["includeState"] = True

    result = _run_node(_NEW_SESSION_DRIVER, payload)

    if expected:
        assert result["project_id"] == project_id
    elif selection_path == "explicit":
        assert "project" in result["error"].lower()
        assert result["apiCalls"] == 0
    else:
        assert "project_id" not in result["body"]
        assert result["activeProject"] is None


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_new_session_rejects_explicit_known_read_only_project_id():
    result = _run_node(
        _NEW_SESSION_DRIVER,
        {
            "activeProject": None,
            "projects": [{"project_id": "native-1", "read_only": True}],
            "options": {"project_id": "native-1"},
            "captureFailure": True,
        },
    )
    assert "project" in result["error"].lower()
    assert result["apiCalls"] == 0


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_new_session_rejects_explicit_unknown_project_id():
    result = _run_node(
        _NEW_SESSION_DRIVER,
        {
            "activeProject": None,
            "projects": [],
            "options": {"project_id": "unknown-project"},
            "captureFailure": True,
        },
    )
    assert "project" in result["error"].lower()
    assert result["apiCalls"] == 0


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_new_session_preserves_explicit_null_project_id():
    body = _run_node(
        _NEW_SESSION_DRIVER,
        {
            "activeProject": "native-1",
            "projects": [{"project_id": "native-1", "read_only": True}],
            "projectScope": None,
            "options": {"project_id": None},
        },
    )
    assert "project_id" in body
    assert body["project_id"] is None


_PROJECT_SCOPE_APPLY_DRIVER = (
    r"""
const fs = require('fs');
const [path, payloadJson] = process.argv.slice(-2);
const args = JSON.parse(payloadJson);
const src = fs.readFileSync(path, 'utf8');
"""
    + _EXTRACTOR
    + r"""
globalThis.window = globalThis;
globalThis.S = {activeProfile: 'team-a'};
globalThis._otherProfileCount = 0;
globalThis._archivedWebuiCount = 0;
globalThis._archivedCliCount = 0;
globalThis._serverWebuiSessionCount = null;
globalThis._serverCliSessionCount = null;
globalThis._serverTimeDelta = 0;
globalThis._serverTz = null;
globalThis._optimisticallyRemovedSessionIds = new Set();
globalThis._allSessions = [];
globalThis._allSessionsScope = null;
globalThis._sidebarReferenceSessions = [];
globalThis._allProjects = [{project_id: 'old'}];
globalThis._allProjectsScope = {profile: 'old', allProfiles: false};
globalThis._activeProject = null;
globalThis._showAllProfiles = false;
globalThis._sessionSourceFilter = 'webui';
globalThis._sessionListLoadError = null;
globalThis._sessionListHasLoadedOnce = false;
globalThis._sessionListFirstRenderAnimated = true;
globalThis._sessionListSkeletonActive = false;
globalThis._sessionListRefreshAnimationPending = false;
globalThis._lastSessionListRenderSig = null;
globalThis._renamingSid = null;
globalThis._sessionActionMenu = null;
globalThis._cronPollGeneration = 0;
for (const [name, value] of Object.entries({
  _reconcileActiveSessionIdleStateFromList: () => {},
  _mergeOptimisticFirstTurnSessions: rows => rows,
  _requestedSessionSidebarSource: () => 'webui',
  _sessionListExcludeHiddenEnabled: () => false,
  _recordSessionProfileCount: () => {},
  _syncSessionAttentionSoundState: () => {},
  _pruneLineageReportCacheToVisibleSessions: () => {},
  _markPollingCompletionUnreadTransitions: () => {},
  _isSessionEffectivelyStreaming: () => false,
  startStreamingPoll: () => {}, stopStreamingPoll: () => {},
  ensureSessionTimeRefreshPoll: () => {},
  ensureActiveSessionExternalRefreshPoll: () => {},
  animateNextSessionListRefresh: () => {}, ensureSessionEventsSSE: () => {},
  _sessionListRenderSignature: () => 'scope-test',
  _purgeStaleInflightEntries: () => {}, renderSessionListFromCache: () => {},
})) globalThis[name] = value;
eval(extractFunction(src, '_applySessionListPayload'));
_applySessionListPayload(
  {sessions: [], active_profile: 'team-a'},
  args.projData,
  {requestAllProfiles: args.projData.all_profiles},
);
console.log(JSON.stringify({projects: _allProjects, scope: _allProjectsScope}));
"""
)


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
@pytest.mark.parametrize(
    ("proj_data", "expected_scope"),
    [
        pytest.param(
            {
                "projects": [{"project_id": "fresh"}],
                "active_profile": "team-a",
                "all_profiles": False,
            },
            {"profile": "team-a", "allProfiles": False},
            id="active-profile-only",
        ),
        pytest.param(
            {
                "projects": [{"project_id": "aggregate"}],
                "active_profile": "team-a",
                "all_profiles": True,
            },
            {"profile": "team-a", "allProfiles": True},
            id="all-profiles",
        ),
        pytest.param(
            {"projects": [{"project_id": "unknown"}], "all_profiles": False},
            None,
            id="missing-profile-fails-closed",
        ),
        pytest.param(
            {"projects": [{"project_id": "unknown"}], "active_profile": "team-a"},
            None,
            id="missing-all-profiles-fails-closed",
        ),
    ],
)
def test_apply_project_payload_records_server_scope(proj_data, expected_scope):
    result = _run_node(_PROJECT_SCOPE_APPLY_DRIVER, {"projData": proj_data})

    assert result["projects"] == proj_data["projects"]
    assert result["scope"] == expected_scope


_INGESTED_PROJECT_SCOPE_NEW_SESSION_DRIVER = (
    r"""
const fs = require('fs');
const [path, payloadJson] = process.argv.slice(-2);
const args = JSON.parse(payloadJson);
const src = fs.readFileSync(path, 'utf8');
"""
    + _EXTRACTOR
    + r"""
globalThis.window = globalThis;
globalThis.document = {
  baseURI: 'http://example.test/',
  createElement: () => ({appendChild() {}, selectedOptions: [{dataset: {provider: ''}}]}),
};
globalThis.localStorage = {getItem: () => null, setItem: () => {}};
globalThis.history = {replaceState: () => {}};
globalThis.NO_PROJECT_FILTER = '__none__';
globalThis.S = {
  session: {session_id: 'previous', workspace: null},
  toolCalls: [], messages: [],
  activeProfile: args.activeProfile,
  activeProfileIsDefault: Object.prototype.hasOwnProperty.call(args, 'activeProfileIsDefault')
    ? args.activeProfileIsDefault
    : args.activeProfile === 'default',
  _pendingSessionToolsets: null, _profileSwitchWorkspace: null,
  _profileDefaultWorkspace: null,
};
globalThis._profilesCache = {
  active: args.activeProfile,
  profiles: args.profiles || [],
};
globalThis._activeProject = args.selectionPath === 'implicit' ? 'candidate-project' : null;
globalThis._allProjects = [{project_id: 'old'}];
globalThis._allProjectsScope = {profile: 'old', allProfiles: false};
globalThis._sessionSourceFilter = 'webui';
globalThis._newSessionInFlight = null;
globalThis._messagesTruncated = false;
globalThis._oldestIdx = 0;
globalThis.INFLIGHT = {};
globalThis._defaultModel = null;
globalThis._activeProvider = 'openai';
globalThis._otherProfileCount = 0;
globalThis._archivedWebuiCount = 0;
globalThis._archivedCliCount = 0;
globalThis._serverWebuiSessionCount = null;
globalThis._serverCliSessionCount = null;
globalThis._serverTimeDelta = 0;
globalThis._serverTz = null;
globalThis._optimisticallyRemovedSessionIds = new Set();
globalThis._allSessions = [];
globalThis._allSessionsScope = null;
globalThis._sidebarReferenceSessions = [];
globalThis._showAllProfiles = false;
globalThis._sessionListLoadError = null;
globalThis._sessionListHasLoadedOnce = false;
globalThis._sessionListFirstRenderAnimated = true;
globalThis._sessionListSkeletonActive = false;
globalThis._sessionListRefreshAnimationPending = false;
globalThis._lastSessionListRenderSig = null;
globalThis._renamingSid = null;
globalThis._sessionActionMenu = null;
globalThis._cronPollGeneration = 0;
globalThis.$ = id => id === 'modelSelect'
  ? {value: 'gpt-4', selectedOptions: [{dataset: {provider: 'openai'}}]}
  : null;
for (const [name, value] of Object.entries({
  _setNewSessionPending: () => {}, updateQueueBadge: () => {},
  _clearPendingSelections: () => {}, clearLiveToolCards: () => {},
  setComposerStatus: () => {}, setStatus: () => {}, updateSendBtn: () => {},
  syncTopbar: () => {}, renderMessages: () => {}, startSessionStream: () => {},
  _setSessionViewedCount: () => {}, _setActiveSessionUrl: () => {},
  _rememberNewChatDraftSession: () => {}, _hydrateTodosFromSession: () => {},
  _setLiveAssistantTps: () => {}, _syncCtxIndicator: () => {}, showToast: () => {},
  loadDir: async () => null,
  _applyModelToDropdown: () => true,
  _modelStateForSelect: () => ({model: 'gpt-4', model_provider: 'openai'}),
  _readEmptyComposerModelOverride: () => null,
  _clearEmptyComposerModelOverride: () => {}, getModelLabel: value => value || '',
  _reconcileActiveSessionIdleStateFromList: () => {},
  _mergeOptimisticFirstTurnSessions: rows => rows,
  _requestedSessionSidebarSource: () => 'webui',
  _sessionListExcludeHiddenEnabled: () => false,
  _recordSessionProfileCount: () => {},
  _syncSessionAttentionSoundState: () => {},
  _pruneLineageReportCacheToVisibleSessions: () => {},
  _markPollingCompletionUnreadTransitions: () => {},
  _isSessionEffectivelyStreaming: () => false,
  startStreamingPoll: () => {}, stopStreamingPoll: () => {},
  ensureSessionTimeRefreshPoll: () => {},
  ensureActiveSessionExternalRefreshPoll: () => {},
  animateNextSessionListRefresh: () => {}, ensureSessionEventsSSE: () => {},
  _sessionListRenderSignature: () => 'ingested-scope-test',
  _purgeStaleInflightEntries: () => {}, renderSessionListFromCache: () => {},
})) globalThis[name] = value;
const calls = [];
globalThis.api = async (_url, opts) => {
  calls.push(JSON.parse(opts.body));
  return {session: {
    session_id: 'new', messages: [], model: 'gpt-4', model_provider: 'openai',
    workspace: null, message_count: 0, last_usage: {},
  }};
};
eval([
  extractFunction(src, '_isReadOnlyProject'),
  extractFunction(src, '_isCanonicalProjectId'),
  extractFunction(src, '_projectForId'),
  extractFunction(src, '_projectAuthorizationActiveProfile'),
  extractFunction(src, '_projectListScopeMatchesActive'),
  extractFunction(src, '_projectProfileMatchesActive'),
  extractFunction(src, '_projectCanReceiveNewSession'),
  extractFunction(src, '_newSessionProjectSelection'),
  extractFunction(src, 'newSession'),
  extractFunction(src, '_applySessionListPayload'),
].join('\n'));
const project = {
  project_id: 'candidate-project', read_only: false,
};
if (args.rowProfilePresent !== false) {
  project.profile = Object.prototype.hasOwnProperty.call(args, 'rowProfile')
    ? args.rowProfile
    : args.activeProfile;
}
_applySessionListPayload(
  {sessions: [], active_profile: args.activeProfile},
  {projects: [project], active_profile: args.responseProfile, all_profiles: args.allProfiles},
  {requestAllProfiles: args.allProfiles},
);
(async () => {
  let error = null;
  const options = args.selectionPath === 'explicit'
    ? {project_id: 'candidate-project'}
    : {};
  try {
    await newSession(false, options);
  } catch (caught) {
    error = String(caught && caught.message ? caught.message : caught);
  }
  console.log(JSON.stringify({
    error,
    apiCalls: calls.length,
    body: calls[0] || null,
    activeProject: globalThis._activeProject,
    projects: globalThis._allProjects,
    scope: globalThis._allProjectsScope,
  }));
})().catch(error => { console.error(error.stack || error); process.exit(1); });
"""
)


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
@pytest.mark.parametrize("selection_path", ["implicit", "explicit"])
@pytest.mark.parametrize(
    ("response_profile", "active_profile", "all_profiles", "expected_scope"),
    [
        pytest.param("default", "default", False, {"profile": "default", "allProfiles": False}, id="exact-default"),
        pytest.param("team-a", "team-a", False, {"profile": "team-a", "allProfiles": False}, id="exact-named"),
        pytest.param("   ", "default", False, None, id="blank"),
        pytest.param(" default ", "default", False, None, id="padded-default"),
        pytest.param(" team-a", "team-a", False, None, id="leading-whitespace-named"),
        pytest.param("team-a ", "team-a", False, None, id="trailing-whitespace-named"),
        pytest.param("team-a", "team-a", True, {"profile": "team-a", "allProfiles": True}, id="all-profiles"),
    ],
)
def test_ingested_project_scope_authorizes_only_exact_active_profile_provenance(
    selection_path, response_profile, active_profile, all_profiles, expected_scope
):
    result = _run_node(
        _INGESTED_PROJECT_SCOPE_NEW_SESSION_DRIVER,
        {
            "selectionPath": selection_path,
            "responseProfile": response_profile,
            "activeProfile": active_profile,
            "allProfiles": all_profiles,
        },
    )

    assert result["projects"] == [
        {
            "project_id": "candidate-project",
            "profile": active_profile,
            "read_only": False,
        }
    ]
    assert result["scope"] == expected_scope

    authorized = expected_scope == {"profile": active_profile, "allProfiles": False}
    if authorized:
        assert result["error"] is None
        assert result["apiCalls"] == 1
        assert result["body"]["project_id"] == "candidate-project"
    elif selection_path == "explicit":
        assert "project" in result["error"].lower()
        assert result["apiCalls"] == 0
    else:
        assert result["error"] is None
        assert result["apiCalls"] == 1
        assert "project_id" not in result["body"]
        assert result["activeProject"] is None


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
@pytest.mark.parametrize("selection_path", ["implicit", "explicit"])
@pytest.mark.parametrize(
    (
        "row_profile_present",
        "row_profile",
        "active_profile",
        "active_is_default",
        "profiles",
        "expected_authorized",
    ),
    [
        pytest.param(True, "team-a", "team-a", False, [], True, id="canonical-named"),
        pytest.param(
            True,
            " team-a ",
            "team-a",
            False,
            [],
            False,
            id="padded-named",
        ),
        pytest.param(
            True,
            "team-a ",
            "team-a",
            False,
            [],
            False,
            id="trailing-space-named",
        ),
        pytest.param(
            True, "   ", "default", True, [], False, id="whitespace-only"
        ),
        pytest.param(True, 7, "default", True, [], False, id="nonstring"),
        pytest.param(
            False, None, "default", True, [], True, id="legacy-missing-default"
        ),
        pytest.param(
            True, None, "default", True, [], True, id="legacy-null-default"
        ),
        pytest.param(
            True, "", "default", True, [], True, id="legacy-empty-default"
        ),
        pytest.param(
            False, None, "team-a", False, [], False, id="missing-named"
        ),
        pytest.param(True, None, "team-a", False, [], False, id="null-named"),
        pytest.param(True, "", "team-a", False, [], False, id="empty-named"),
        pytest.param(
            True, "default", "default", True, [], True, id="canonical-default"
        ),
        pytest.param(
            True,
            "default",
            "renamed-root",
            True,
            [],
            True,
            id="default-row-under-renamed-root",
        ),
        pytest.param(
            True,
            "renamed-root",
            "default",
            True,
            [{"name": "renamed-root", "is_default": True}],
            True,
            id="renamed-root-row-under-default",
        ),
    ],
)
def test_ingested_project_row_profile_provenance_controls_new_session_authority(
    selection_path,
    row_profile_present,
    row_profile,
    active_profile,
    active_is_default,
    profiles,
    expected_authorized,
):
    result = _run_node(
        _INGESTED_PROJECT_SCOPE_NEW_SESSION_DRIVER,
        {
            "selectionPath": selection_path,
            "responseProfile": active_profile,
            "activeProfile": active_profile,
            "activeProfileIsDefault": active_is_default,
            "allProfiles": False,
            "rowProfilePresent": row_profile_present,
            "rowProfile": row_profile,
            "profiles": profiles,
        },
    )

    assert result["scope"] == {"profile": active_profile, "allProfiles": False}
    if expected_authorized:
        assert result["error"] is None
        assert result["apiCalls"] == 1
        assert result["body"]["project_id"] == "candidate-project"
    elif selection_path == "explicit":
        assert "project" in result["error"].lower()
        assert result["apiCalls"] == 0
    else:
        assert result["error"] is None
        assert result["apiCalls"] == 1
        assert "project_id" not in result["body"]
        assert result["activeProject"] is None


_GUARD_DRIVER = (
    r"""
const fs = require('fs');
const [path, payloadJson] = process.argv.slice(-2);
const args = JSON.parse(payloadJson);
const src = fs.readFileSync(path, 'utf8');
"""
    + _EXTRACTOR
    + r"""
let documentTouches = 0;
globalThis.document = new Proxy({}, {
  get() { documentTouches++; throw new Error('document touched'); },
});
globalThis.api = async () => { throw new Error('api touched'); };
globalThis.showConfirmDialog = async () => { throw new Error('dialog touched'); };
globalThis._isReadOnlyProject = eval('(' + extractFunction(src, '_isReadOnlyProject') + ')');
const candidate = eval('(' + extractFunction(src, args.functionName) + ')');
(async () => {
  const project = {project_id: 'native-1', name: 'Native', read_only: true};
  if (args.functionName === '_startProjectRename') candidate(project, {});
  else if (args.functionName === '_showProjectContextMenu') candidate({}, project, {});
  else if (args.functionName === '_confirmDeleteProject') await candidate(project);
  else if (args.functionName === '_attachProjectQuickCreateButton') candidate({}, project);
  console.log(JSON.stringify({documentTouches}));
})().catch(error => { console.error(error.stack || error); process.exit(1); });
"""
)


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
@pytest.mark.parametrize(
    "function_name",
    [
        "_startProjectRename",
        "_showProjectContextMenu",
        "_confirmDeleteProject",
        "_attachProjectQuickCreateButton",
    ],
)
def test_read_only_project_mutation_helpers_return_before_side_effects(function_name):
    assert _run_node(_GUARD_DRIVER, {"functionName": function_name}) == {
        "documentTouches": 0
    }


_PICKER_DRIVER = (
    r"""
const fs = require('fs');
const [path, payloadJson] = process.argv.slice(-2);
const args = JSON.parse(payloadJson);
const src = fs.readFileSync(path, 'utf8');
"""
    + _EXTRACTOR
    + r"""
function node(tag) {
  return {
    tagName: String(tag || '').toUpperCase(), className: '', textContent: '',
    children: [], style: {}, dataset: {},
    appendChild(child) { this.children.push(child); return child; },
    remove() {}, contains() { return false; }, querySelectorAll() { return []; },
    getBoundingClientRect() { return {top: 10, bottom: 20, right: 100}; },
    setAttribute() {}, addEventListener() {},
  };
}
const body = node('body');
globalThis.window = {innerHeight: 800};
globalThis.document = {
  body, createElement: node, querySelectorAll: () => [],
  addEventListener() {}, removeEventListener() {},
};
globalThis.setTimeout = () => 0;
globalThis._allProjects = [
  {project_id: 'legacy', name: 'Legacy', profile: 'default'},
  {project_id: 'native', name: 'Native', profile: 'default', read_only: true},
];
globalThis._isReadOnlyProject = eval('(' + extractFunction(src, '_isReadOnlyProject') + ')');
globalThis.PROJECT_COLORS = ['#fff'];
globalThis.t = value => value;
globalThis.api = async () => {};
globalThis.showToast = () => {};
globalThis.showPromptDialog = async () => null;
globalThis.renderSessionList = async () => {};
globalThis.renderSessionListFromCache = () => {};
globalThis._allSessions = [];
let picker;
if (args.kind === 'batch') {
  const bar = node('div');
  globalThis._selectedSessions = new Set(['s-1']);
  globalThis.$ = () => bar;
  eval('(' + extractFunction(src, '_showBatchProjectPicker') + ')')();
  picker = bar.children[0];
} else {
  eval('(' + extractFunction(src, '_showProjectPicker') + ')')(
    {session_id: 's-1', profile: 'default'}, node('button')
  );
  picker = body.children[0];
}
function label(item) {
  return item.textContent || item.children.map(child => child.textContent || '').join('');
}
console.log(JSON.stringify({labels: picker.children.map(label)}));
"""
)


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
@pytest.mark.parametrize("kind", ["single", "batch"])
def test_move_pickers_exclude_read_only_projects(kind):
    labels = _run_node(_PICKER_DRIVER, {"kind": kind})["labels"]
    assert "Legacy" in labels
    assert "Native" not in labels


_RENDER_DRIVER = (
    r"""
const fs = require('fs');
const [path] = process.argv.slice(-2);
const src = fs.readFileSync(path, 'utf8');
"""
    + _EXTRACTOR
    + r"""
const STOP = new Error('project bar rendered');
function node(tag) {
  return {
    tagName: String(tag || '').toUpperCase(), className: '', textContent: '', title: '',
    children: [], style: {}, dataset: {}, attributes: {}, listeners: [],
    appendChild(child) {
      this.children.push(child);
      if (this.id === 'sessionList' && child.className === 'project-bar') throw STOP;
      return child;
    },
    setAttribute(name, value) { this.attributes[name] = String(value); },
    addEventListener(type) { this.listeners.push(type); },
    querySelectorAll() { return []; },
  };
}
const list = node('div'); list.id = 'sessionList'; list.scrollTop = 0;
const elements = {sessionList: list, sessionSearch: {value: ''}};
globalThis.window = globalThis;
globalThis.document = {createElement: node};
globalThis.$ = id => elements[id] || null;
globalThis._sessionListSkeletonActive = false;
globalThis._renamingSid = null;
globalThis._sessionActionMenu = null;
globalThis._allSessions = [];
globalThis._allProjects = [{
  project_id: 'native', name: 'Native', read_only: true,
  project_source: 'hermes-agent',
}];
globalThis._activeProject = null;
globalThis._contentSearchResults = [];
globalThis._sessionSourceFilter = 'webui';
globalThis._serverWebuiSessionCount = 0;
globalThis._serverCliSessionCount = 0;
globalThis._sessionListRefreshAnimationPending = false;
globalThis._sessionListEnterAllAnimationPending = false;
globalThis._sessionSelectMode = false;
globalThis._selectedSessions = new Set();
globalThis._sessionListLoadError = null;
globalThis._otherProfileCount = 0;
globalThis._showAllProfiles = false;
globalThis.NO_PROJECT_FILTER = '__none__';
globalThis._purgeStaleInflightEntries = () => {};
globalThis._activeSessionIdForSidebar = () => null;
globalThis._sessionRowsWithActiveEphemeralSession = rows => rows;
globalThis._sessionSearchMergeMatches = rows => rows;
globalThis._ensureActiveSessionRowPresent = rows => rows;
globalThis._partitionSidebarSessionRows = () => ({
  cliSessionCount: 0, profileFiltered: [], sessionsRaw: [], archivedCount: 0,
  webuiReferenceRaw: [], cliReferenceRaw: [], webuiSessionsRaw: [], cliSessionsRaw: [],
});
globalThis._scopedSidebarReferenceRows = () => [];
globalThis._renderSidebarRowsFromRawSessions = () => [];
globalThis._sessionSourceTabCount = () => 0;
globalThis._syncSidebarExpansionForActiveSession = () => {};
globalThis._captureSessionReflowPositions = () => null;
globalThis._sessionPrefersReducedMotion = () => true;
globalThis.SESSION_SWIPE_DURATION_MS = 0;
globalThis.SESSION_SWIPE_REFLOW_LEAD_MS = 0;
globalThis.closeSessionActionMenu = () => {};
globalThis.t = value => value;
globalThis._setActiveProjectFilter = id => { globalThis.clickedProject = id; };
globalThis.setTimeout = callback => { callback(); return 1; };
globalThis.clearTimeout = () => {};
globalThis._isReadOnlyProject = eval('(' + extractFunction(src, '_isReadOnlyProject') + ')');
globalThis._attachProjectQuickCreateButton = () => { throw new Error('quick create attached'); };
globalThis._projectQuickCreate = true;
const render = eval('(' + extractFunction(src, 'renderSessionListFromCache') + ')');
try { render(); } catch (error) { if (error !== STOP) throw error; }
const bar = list.children.find(child => child.className === 'project-bar');
const chip = bar.children.find(child =>
  child.children.some(grandchild => grandchild.textContent === 'Native')
);
chip.onclick({});
console.log(JSON.stringify({
  visible: Boolean(chip), clickedProject: globalThis.clickedProject,
  title: chip.title, ariaLabel: chip.attributes['aria-label'],
  hasDoubleClick: typeof chip.ondblclick === 'function',
  hasContextMenu: typeof chip.oncontextmenu === 'function',
  touchListeners: chip.listeners,
  quickCreateChildren: chip.children.filter(
    child => child.className === 'project-chip-quick-create'
  ).length,
}));
"""
)


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_read_only_chip_remains_filterable_without_mutation_affordances():
    assert _run_node(_RENDER_DRIVER) == {
        "visible": True,
        "clickedProject": "native",
        "title": "Managed by Hermes Agent (read-only here)",
        "ariaLabel": "Native — managed by Hermes Agent, read-only here",
        "hasDoubleClick": False,
        "hasContextMenu": False,
        "touchListeners": [],
        "quickCreateChildren": 0,
    }
