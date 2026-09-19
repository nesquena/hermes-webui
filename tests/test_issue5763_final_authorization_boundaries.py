"""Final frontend project-authorization provenance boundaries for issue #5763."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_BASE_PATH = Path(__file__).with_name("test_issue5763_native_project_ui.py")
_BASE_SPEC = importlib.util.spec_from_file_location("issue5763_native_project_ui_base", _BASE_PATH)
assert _BASE_SPEC is not None and _BASE_SPEC.loader is not None
base = importlib.util.module_from_spec(_BASE_SPEC)
_BASE_SPEC.loader.exec_module(base)


@pytest.mark.skipif(base.NODE is None, reason="node not on PATH")
@pytest.mark.parametrize(
    ("request_all_profiles", "response_all_profiles"),
    [(True, False), (False, True)],
)
def test_project_rows_remain_visible_but_scope_is_null_on_request_response_mismatch(
    request_all_profiles, response_all_profiles
):
    driver = base._PROJECT_SCOPE_APPLY_DRIVER.replace(
        "{requestAllProfiles: args.projData.all_profiles}",
        "{requestAllProfiles: args.requestAllProfiles}",
    )
    projects = [{"project_id": "visible-row"}]
    result = base._run_node(
        driver,
        {
            "requestAllProfiles": request_all_profiles,
            "projData": {
                "projects": projects,
                "active_profile": "team-a",
                "all_profiles": response_all_profiles,
            },
        },
    )

    assert result == {"projects": projects, "scope": None}


_PROJECT_REQUEST_SCOPE_RACE_DRIVER = (
    r"""
const fs = require('fs');
const [path, payloadJson] = process.argv.slice(-2);
const args = JSON.parse(payloadJson);
const src = fs.readFileSync(path, 'utf8');
"""
    + base._EXTRACTOR
    + r"""
globalThis.window = globalThis;
globalThis._showAllProfiles = args.requestAllProfiles;
globalThis._sessionListHasLoadedOnce = true;
globalThis._SESSION_LIST_BOOT_TIMEOUT_MS = 90000;
globalThis._renderSessionListGen = 3;
globalThis._profileSwitchListEmbargo = false;
globalThis._pendingSessionListPayload = null;
globalThis._pendingSessionListApplyTimer = 0;
globalThis._allProjects = [];
globalThis._contentSearchResults = [];
globalThis._cronPollGeneration = 0;
globalThis.SESSION_LIST_INTERACTION_IDLE_MS = 1;
globalThis.$ = () => ({value: ''});
globalThis._sessionListQueryString = () => '/captured-session-query';
globalThis._isSessionListUserInteracting = (() => {
  let calls = 0;
  return () => args.deferred && calls++ === 0;
})();
globalThis.setTimeout = callback => { callback(); return 1; };
globalThis.clearTimeout = () => {};
globalThis._showSessionListLoadError = () => {};
globalThis._requestedSessionSidebarSource = () => 'webui';
globalThis._sessionListExcludeHiddenEnabled = () => false;
globalThis._clearSessionSourceTabCounts = () => {};
globalThis.renderSessionListFromCache = () => {};
const urls = [];
const applies = [];
globalThis.api = async url => {
  urls.push(url);
  if (url.startsWith('/api/projects')) return {
    projects: [{project_id: 'candidate'}], active_profile: 'team-a',
    all_profiles: args.requestAllProfiles,
  };
  if (url.startsWith('/api/sessions')) {
    _showAllProfiles = !args.requestAllProfiles;
    return {sessions: [], active_profile: 'team-a'};
  }
  throw new Error('unexpected API ' + url);
};
globalThis._applySessionListPayload = (_sessions, projects, opts) => {
  applies.push({projects, opts});
};
eval([
  extractFunction(src, '_schedulePendingSessionListApply'),
  extractFunction(src, '_loadSidebarSessionListPayload'),
  extractFunction(src, '_runRenderSessionListRefresh'),
].join('\n'));
(async () => {
  await _runRenderSessionListRefresh({deferWhileInteracting: args.deferred}, 3);
  console.log(JSON.stringify({urls, applies, pending: _pendingSessionListPayload}));
})().catch(error => { console.error(error.stack || error); process.exit(1); });
"""
)


@pytest.mark.skipif(base.NODE is None, reason="node not on PATH")
@pytest.mark.parametrize("request_all_profiles", [False, True])
@pytest.mark.parametrize("deferred", [False, True])
def test_sidebar_project_scope_stays_bound_to_captured_request_during_race(
    request_all_profiles, deferred
):
    result = base._run_node(
        _PROJECT_REQUEST_SCOPE_RACE_DRIVER,
        {"requestAllProfiles": request_all_profiles, "deferred": deferred},
    )

    expected_url = (
        "/api/projects?all_profiles=1" if request_all_profiles else "/api/projects"
    )
    assert expected_url in result["urls"]
    assert len(result["applies"]) == 1
    assert result["applies"][0]["opts"]["requestAllProfiles"] is request_all_profiles
    assert result["pending"] is None


@pytest.mark.skipif(base.NODE is None, reason="node not on PATH")
@pytest.mark.parametrize("selection_path", ["implicit", "explicit"])
@pytest.mark.parametrize(
    "malformed_id",
    [
        pytest.param(" padded-project ", id="padded"),
        pytest.param("", id="empty"),
        pytest.param("   ", id="whitespace-only"),
        pytest.param(7, id="non-string"),
    ],
)
def test_malformed_project_ids_cannot_authorize_or_persist_new_session(
    selection_path, malformed_id
):
    payload = {
        "activeProject": malformed_id if selection_path == "implicit" else None,
        "projects": [
            {"project_id": malformed_id, "profile": "default", "read_only": False}
        ],
        "options": {"project_id": malformed_id} if selection_path == "explicit" else {},
    }
    if selection_path == "explicit":
        payload["captureFailure"] = True
    else:
        payload["includeState"] = True

    result = base._run_node(base._NEW_SESSION_DRIVER, payload)

    if selection_path == "explicit":
        assert "project" in result["error"].lower()
        assert result["apiCalls"] == 0
        assert result["sessionId"] == "previous"
    else:
        assert "project_id" not in result["body"]
        assert result["activeProject"] is None
        assert result["apiCalls"] == 1


@pytest.mark.skipif(base.NODE is None, reason="node not on PATH")
@pytest.mark.parametrize("selection_path", ["implicit", "explicit"])
@pytest.mark.parametrize(
    "project_metadata",
    [
        pytest.param({"read_only": "false"}, id="string-flag"),
        pytest.param({"read_only": 0}, id="number-flag"),
        pytest.param({"read_only": {"value": False}}, id="object-flag"),
        pytest.param({"read_only": None}, id="null-flag"),
        pytest.param({"project_source": "hermes-agent"}, id="native-missing-flag"),
        pytest.param(
            {"project_source": "hermes-agent", "read_only": False},
            id="native-false-flag",
        ),
    ],
)
def test_malformed_or_native_capability_cannot_authorize_new_session(
    selection_path, project_metadata
):
    project_id = "candidate-project"
    payload = {
        "activeProject": project_id if selection_path == "implicit" else None,
        "projects": [{"project_id": project_id, **project_metadata}],
        "options": {"project_id": project_id} if selection_path == "explicit" else {},
    }
    if selection_path == "explicit":
        payload["captureFailure"] = True
    else:
        payload["includeState"] = True

    result = base._run_node(base._NEW_SESSION_DRIVER, payload)

    if selection_path == "explicit":
        assert "project" in result["error"].lower()
        assert result["apiCalls"] == 0
        assert result["sessionId"] == "previous"
    else:
        assert "project_id" not in result["body"]
        assert result["activeProject"] is None
        assert result["apiCalls"] == 1


@pytest.mark.skipif(base.NODE is None, reason="node not on PATH")
@pytest.mark.parametrize("project_id", ["legacy-1", "01J9NATIVE-PROJECT_abc"])
def test_canonical_project_ids_remain_authorizable(project_id):
    body = base._run_node(
        base._NEW_SESSION_DRIVER,
        {
            "activeProject": None,
            "projects": [{"project_id": project_id, "read_only": False}],
            "options": {"project_id": project_id},
        },
    )

    assert body["project_id"] == project_id
