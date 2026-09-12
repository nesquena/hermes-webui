"""Executable regression coverage for loadSession() metadata restore state."""
from __future__ import annotations

import json
import shutil
import subprocess
import textwrap
from functools import lru_cache
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parent.parent
SESSIONS_JS = REPO / "static" / "sessions.js"
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node is required for executable coverage")


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _extract_js_block(source: str, marker: str) -> str:
    start = source.index(marker)
    brace_start = source.index("{", start)
    depth = 0
    string = None
    escaped = False
    line_comment = False
    block_comment = False
    for index in range(brace_start, len(source)):
        char = source[index]
        next_char = source[index + 1] if index + 1 < len(source) else ""
        if line_comment:
            if char == "\n":
                line_comment = False
            continue
        if block_comment:
            if char == "*" and next_char == "/":
                block_comment = False
            continue
        if string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == string:
                string = None
            continue
        if char == "/" and next_char == "/":
            line_comment = True
            continue
        if char == "/" and next_char == "*":
            block_comment = True
            continue
        if char in "'\"`":
            string = char
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return source[start : index + 1]
    raise AssertionError(f"JavaScript block did not close: {marker}")


def _extract_optional(source: str, marker: str) -> str:
    return _extract_js_block(source, marker) if marker in source else ""


@lru_cache(maxsize=1)
def _run_frontend_scenarios() -> dict:
    sessions_js = _read(SESSIONS_JS)
    scenarios = {
        "http500": {
            "sid": "boot-session",
            "savedSid": "boot-session",
            "routeSid": "boot-session",
            "metadataFailure": {"status": 500},
            "profileSwitchFailure": {"status": 500},
        },
        "network": {
            "sid": "boot-session",
            "savedSid": "boot-session",
            "routeSid": "boot-session",
            "metadataFailure": {"status": None, "message": "network down"},
            "profileSwitchFailure": {"status": 500},
        },
        "http404": {
            "sid": "dead-session",
            "savedSid": "dead-session",
            "routeSid": "dead-session",
            "metadataFailure": {"status": 404},
            "profileSwitchFailure": {"status": 500},
        },
        "urlOnly404": {
            "sid": "url-only-dead-session",
            "savedSid": None,
            "routeSid": "url-only-dead-session",
            "metadataFailure": {"status": 404},
            "profileSwitchFailure": {"status": 500},
        },
        "localStorageOnly404": {
            "sid": "saved-only-dead-session",
            "savedSid": "saved-only-dead-session",
            "routeSid": None,
            "metadataFailure": {"status": 404},
            "profileSwitchFailure": {"status": 500},
        },
        "conflictingState404": {
            "sid": "session-B",
            "savedSid": "session-A",
            "routeSid": "session-B",
            "metadataFailure": {"status": 404},
            "profileSwitchFailure": {"status": 500},
        },
        "liveActiveConflict404": {
            "sid": "session-A",
            "savedSid": "session-B",
            "routeSid": "session-B",
            "activeSid": "session-A",
            "force": True,
            "metadataFailure": {"status": 404},
            "profileSwitchFailure": {"status": 500},
        },
        "liveActiveChanges404": {
            "sid": "session-A",
            "savedSid": "session-B",
            "routeSid": "session-B",
            "activeSid": "session-A",
            "activeSidBeforeReject": "session-B",
            "deferMetadata": True,
            "force": True,
            "metadataFailure": {"status": 404},
            "profileSwitchFailure": {"status": 500},
        },
        "storageReadFailure404": {
            "sid": "route-owned-dead-session",
            "savedSid": "saved-live-session",
            "routeSid": "route-owned-dead-session",
            "storageReadFailure": True,
            "metadataFailure": {"status": 404},
            "profileSwitchFailure": {"status": 500},
        },
        "routeReadFailure404": {
            "sid": "saved-owned-dead-session",
            "savedSid": "saved-owned-dead-session",
            "routeSid": "route-live-session",
            "routeReadFailure": True,
            "metadataFailure": {"status": 404},
            "profileSwitchFailure": {"status": 500},
        },
        "profileSwitch404": {
            "sid": "profile-session",
            "savedSid": "profile-session",
            "routeSid": "profile-session",
            "metadataFailure": {
                "status": 409,
                "body": json.dumps(
                    {
                        "code": "session_profile_mismatch",
                        "profile": "other",
                        "session_id": "profile-session",
                    }
                ),
            },
            "profileSwitchFailure": {"status": 404},
        },
        "deadBWithSavedA": {
            "sid": "session-B",
            "savedSid": "session-A",
            "routeSid": "session-A",
            "metadataFailure": {"status": 404},
            "profileSwitchFailure": {"status": 500},
            "concurrent": True,
        },
    }
    script = textwrap.dedent(
        """
        const vm = require('vm');
        const helperSource = %s;
        const profileMismatchSource = %s;
        const loadSessionSource = %s;
        const scenarios = %s;

        function makeError(spec) {
          const error = new Error(spec.message || 'HTTP failure');
          if (spec.status !== null && spec.status !== undefined) error.status = spec.status;
          if (spec.body !== undefined) error.body = spec.body;
          return error;
        }

        async function runScenario(config) {
          const values = new Map([['hermes-webui-session', config.savedSid]]);
          const historyCalls = [];
          const apiCalls = [];
          let pendingFirstLoadReject;
          let pendingMetadataReject;
          let storageReadFailed = false;
          const location = { href: config.routeSid === null
            ? 'https://hermes.test/'
            : `https://hermes.test/session/${encodeURIComponent(config.routeSid)}` };
          const message = { innerHTML: '' };
          const streamStarts = [];
          const context = {
            console,
            window: {},
            S: {
              session: config.activeSid ? {session_id: config.activeSid} : null,
              messages: [], toolCalls: [], busy: false, activeStreamId: null,
            },
            INFLIGHT: {},
            _loadingSessionId: null,
            _loadSessionGeneration: 0,
            _messagesTruncated: false,
            _oldestIdx: 0,
            _loadingOlder: false,
            _yoloEnabled: false,
            localStorage: {
              getItem(key) {
                if (config.storageReadFailure && !storageReadFailed) {
                  storageReadFailed = true;
                  throw new Error('storage read failed');
                }
                return values.has(key) ? values.get(key) : null;
              },
              setItem(key, value) { values.set(key, String(value)); },
              removeItem(key) { values.delete(key); },
            },
            history: {
              replaceState(...args) {
                historyCalls.push(args);
                location.href = args[2];
              },
            },
            location,
            $: id => id === 'msgInner' ? message : null,
            _appRootPath: () => '/',
            _sessionIdFromLocation: () => {
              if (config.routeReadFailure) throw new Error('route read failed');
              return config.routeSid;
            },
            _rearmActiveSessionStream: () => {},
            _updateYoloPill: () => {},
            stopApprovalPolling: () => {},
            hideApprovalCard: () => {},
            stopSessionStream: () => {},
            _clearSameSessionForceReloadHint: () => {},
            _captureSameSessionForceReloadHint: () => {},
            startSessionStream: sid => streamStarts.push(sid),
            api(path) {
              apiCalls.push(String(path));
              if (config.concurrent && path.includes('session_id=session-A')) {
                return new Promise((resolve, reject) => { pendingFirstLoadReject = reject; });
              }
              if (config.deferMetadata && path.startsWith('/api/session?')) {
                return new Promise((resolve, reject) => { pendingMetadataReject = reject; });
              }
              const failure = path === '/api/profile/switch'
                ? config.profileSwitchFailure
                : config.metadataFailure;
              return Promise.reject(makeError(failure));
            },
            showToast: () => {},
          };
          context._switchProfileForSessionLoad = () => context.api('/api/profile/switch');
          vm.createContext(context);
          vm.runInContext(
            helperSource + profileMismatchSource + loadSessionSource,
            context,
            {filename: 'sessions.js'},
          );
          let firstLoad;
          if (config.concurrent) {
            firstLoad = vm.runInContext(
              "loadSession('session-A', {skipLineageResolve:true, skipExtHooks:true})",
              context,
            );
            await Promise.resolve();
          }
          const loadOptions = config.force
            ? {skipLineageResolve:true, skipExtHooks:true, force:true}
            : {skipLineageResolve:true, skipExtHooks:true};
          const loadExpression = `loadSession(${JSON.stringify(config.sid)}, ${JSON.stringify(loadOptions)})`;
          let pendingLoad;
          if (config.deferMetadata) {
            pendingLoad = vm.runInContext(loadExpression, context);
            await Promise.resolve();
            context.S.session = {session_id: config.activeSidBeforeReject};
            pendingMetadataReject(makeError(config.metadataFailure));
          }
          let rejectedStatus = null;
          try {
            await (pendingLoad || vm.runInContext(loadExpression, context));
          } catch (error) {
            rejectedStatus = error && error.status || null;
          }
          if (firstLoad) {
            pendingFirstLoadReject(makeError({status: 404}));
            await firstLoad;
          }
          return {
            remainingPointer: context.localStorage.getItem('hermes-webui-session'),
            historyCalls,
            url: context.location.href,
            rejectedStatus,
            apiCalls,
            loadingSessionId: context._loadingSessionId,
            streamStarts,
          };
        }

        (async () => {
          const result = {};
          for (const [name, config] of Object.entries(scenarios)) {
            result[name] = await runScenario(config);
          }
          process.stdout.write(JSON.stringify(result));
        })().catch(error => {
          console.error(error && error.stack || error);
          process.exit(1);
        });
        """
        % (
            json.dumps(_extract_optional(sessions_js, "function _clearStuckSessionOnBoot(")),
            json.dumps(_extract_js_block(sessions_js, "function _sessionProfileMismatchFromError(")),
            json.dumps(_extract_js_block(sessions_js, "async function loadSession(")),
            json.dumps(scenarios),
        )
    )
    result = subprocess.run(
        [NODE, "-e", script],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, f"node failed:\n{result.stderr}\n{result.stdout}"
    return json.loads(result.stdout)


def test_non_404_metadata_failures_preserve_saved_session_state():
    results = _run_frontend_scenarios()
    for name in ("http500", "network"):
        result = results[name]
        assert result["remainingPointer"] == "boot-session", result
        assert result["historyCalls"] == [], result
        assert result["url"] == "https://hermes.test/session/boot-session", result
        assert result["loadingSessionId"] is None, result


def test_direct_metadata_404_clears_owned_saved_session_state():
    result = _run_frontend_scenarios()["http404"]
    assert result["remainingPointer"] is None, result
    assert result["historyCalls"] == [[None, "", "/"]], result
    assert result["url"] == "/", result
    assert result["rejectedStatus"] == 404, result
    assert result["loadingSessionId"] is None, result


def test_url_only_metadata_404_clears_owned_route():
    result = _run_frontend_scenarios()["urlOnly404"]
    assert result["remainingPointer"] is None, result
    assert result["historyCalls"] == [[None, "", "/"]], result
    assert result["url"] == "/", result
    assert result["rejectedStatus"] == 404, result
    assert result["loadingSessionId"] is None, result


def test_localstorage_only_metadata_404_clears_only_saved_pointer():
    result = _run_frontend_scenarios()["localStorageOnly404"]
    assert result["remainingPointer"] is None, result
    assert result["historyCalls"] == [], result
    assert result["url"] == "https://hermes.test/", result
    assert result["rejectedStatus"] == 404, result
    assert result["loadingSessionId"] is None, result


def test_conflicting_metadata_404_clears_only_matching_route():
    result = _run_frontend_scenarios()["conflictingState404"]
    assert result["remainingPointer"] == "session-A", result
    assert result["historyCalls"] == [[None, "", "/"]], result
    assert result["url"] == "/", result
    assert result["rejectedStatus"] == 404, result
    assert result["loadingSessionId"] is None, result


def test_live_active_metadata_404_preserves_conflicting_recovery_state():
    result = _run_frontend_scenarios()["liveActiveConflict404"]
    assert result["remainingPointer"] == "session-B", result
    assert result["historyCalls"] == [], result
    assert result["url"] == "https://hermes.test/session/session-B", result
    assert result["rejectedStatus"] is None, result
    assert result["loadingSessionId"] is None, result


def test_live_active_transition_rearms_the_live_session_stream():
    result = _run_frontend_scenarios()["liveActiveChanges404"]
    assert result["remainingPointer"] == "session-B", result
    assert result["historyCalls"] == [], result
    assert result["url"] == "https://hermes.test/session/session-B", result
    assert result["rejectedStatus"] is None, result
    assert result["loadingSessionId"] is None, result
    assert result["streamStarts"] == ["session-B"], result


def test_metadata_404_component_reads_fail_independently():
    storage_failure = _run_frontend_scenarios()["storageReadFailure404"]
    assert storage_failure["remainingPointer"] == "saved-live-session", storage_failure
    assert storage_failure["historyCalls"] == [[None, "", "/"]], storage_failure
    assert storage_failure["rejectedStatus"] == 404, storage_failure
    route_failure = _run_frontend_scenarios()["routeReadFailure404"]
    assert route_failure["remainingPointer"] is None, route_failure
    assert route_failure["historyCalls"] == [], route_failure
    assert route_failure["url"] == "https://hermes.test/session/route-live-session", route_failure
    assert route_failure["rejectedStatus"] == 404, route_failure


def test_profile_switch_404_does_not_reclassify_metadata_failure():
    result = _run_frontend_scenarios()["profileSwitch404"]
    assert result["remainingPointer"] == "profile-session", result
    assert result["historyCalls"] == [], result
    assert result["url"] == "https://hermes.test/session/profile-session", result
    assert result["rejectedStatus"] is None, result
    assert result["apiCalls"][1] == "/api/profile/switch", result


def test_unowned_boot_404_does_not_clear_saved_session_a_state():
    result = _run_frontend_scenarios()["deadBWithSavedA"]
    assert result["remainingPointer"] == "session-A", result
    assert result["historyCalls"] == [], result
    assert result["url"] == "https://hermes.test/session/session-A", result
    assert result["rejectedStatus"] is None, result
    assert result["loadingSessionId"] is None, result
    assert "session_id=session-A" in result["apiCalls"][0], result
    assert "session_id=session-B" in result["apiCalls"][1], result
