"""Behavioural checks for #5001 active-profile recovery."""

import json
import subprocess
import shutil
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent
BOOT_JS = ROOT / "static" / "boot.js"
NODE = shutil.which("node")
BOOT_MARKER_KEY = "hermes-webui-active-profile-bootstrap-401"


pytestmark = pytest.mark.skipif(
    NODE is None,
    reason="node is required to execute boot.js recovery-path behavior",
)


_BOOT_DRIVER = r"""
const fs = require('fs');
const bootSrc = fs.readFileSync(process.argv[2], 'utf8');
const scenario = JSON.parse(process.argv[3] || '{}');

function extractFunction(source, name) {
  const marker = `async function ${name}`;
  const start = source.indexOf(marker);
  if (start < 0) {
    throw new Error(`missing function: ${name}`);
  }
  const end = source.indexOf('\n  // Fetch active profile', start);
  if (end < 0) {
    throw new Error(`missing marker following function: ${name}`);
  }
  return source.slice(start, end);
}

function extractBlock(source, startMarker, endMarker) {
  const start = source.indexOf(startMarker);
  if (start < 0) {
    throw new Error(`missing block start: ${startMarker}`);
  }
  const end = source.indexOf(endMarker, start);
  if (end < 0) {
    throw new Error(`missing block end: ${endMarker}`);
  }
  return source.slice(start, end);
}

class FakeStorage {
  constructor(seed = {}) {
    this.store = { ...seed };
  }

  getItem(key) {
    return Object.prototype.hasOwnProperty.call(this.store, key)
      ? this.store[key]
      : null;
  }

  setItem(key, value) {
    this.store[key] = String(value);
  }

  removeItem(key) {
    delete this.store[key];
  }

  snapshot() {
    return { ...this.store };
  }
}

function makeAttempt(attempt) {
  return async function () {
    if (attempt.type === 'success') {
      const payload = attempt.payload || {name: 'default', is_default: true};
      return payload;
    }
    if (attempt.type === 'return') {
      return attempt.value;
    }
    if (attempt.type === 'undefined') {
      return undefined;
    }
    const error = new Error(attempt.message || 'active profile bootstrap failure');
    if (attempt.status !== undefined) error.status = attempt.status;
    throw error;
  };
}

function makeFetchResponse(attempt) {
  const status = attempt.status === undefined ? 200 : attempt.status;
  const payload = Object.prototype.hasOwnProperty.call(attempt, 'payload')
    ? attempt.payload
    : {name: 'default', is_default: true};
  const textBody = Object.prototype.hasOwnProperty.call(attempt, 'text')
    ? attempt.text
    : JSON.stringify(payload);
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: attempt.statusText || '',
    headers: {
      get(name) {
        return String(name).toLowerCase() === 'content-type'
          ? attempt.contentType || 'application/json'
          : '';
      },
    },
    json: async () => payload,
    text: async () => textBody,
  };
}

function makeApiAttempt(attempt) {
  return async function (_path, opts = {}) {
    if (!opts || opts.redirect401 !== false) {
      throw new Error('expected redirect401:false for active-profile bootstrap');
    }
    const response = makeFetchResponse(attempt);
    if (response.status === 401) {
      return undefined;
    }
    if (!response.ok) {
      const text = await response.text();
      let message = text;
      try {
        const parsed = JSON.parse(text);
        message = parsed.error || parsed.message || text;
      } catch (_) {}
      const error = new Error(message);
      error.status = response.status;
      error.statusText = response.statusText;
      error.body = text;
      throw error;
    }
    const ct = response.headers.get('content-type') || '';
    return ct.includes('application/json')
      ? await response.json()
      : await response.text();
  };
}

const budgetBlock = extractBlock(
  bootSrc,
  '  const _bootActiveProfileUnauthRedirectBudget=(()=>{',
  '  // Fetch active profile'
);
eval(budgetBlock.replace(
  '  const _bootActiveProfileUnauthRedirectBudget=(()=>{',
  '  globalThis._bootActiveProfileUnauthRedirectBudget=(()=>{'
));
eval(extractFunction(bootSrc, '_resolveActiveProfileBootstrapState'));
const bootActiveProfileBlock = extractBlock(
  bootSrc,
  'const activeProfileState = await _resolveActiveProfileBootstrapState();',
  '\n  // Update profile chip label immediately'
);
const runBootActiveProfileBlock = new Function(
  'resolveState',
  'S',
  'applyBotName',
  `
const _resolveActiveProfileBootstrapState = resolveState;
return (async () => {
  ${bootActiveProfileBlock}
  return {continued: true};
})();
`
);

(async () => {
  const attempts = Array.isArray(scenario.attempts) ? scenario.attempts : [];
  const markerKey =
    scenario.markerKey || 'hermes-webui-active-profile-bootstrap-401';
  const storage = new FakeStorage(scenario.initialStorage || {});
  const redirectUrls = [];
  const results = [];
  const storageHistory = [];

  for (const attempt of attempts) {
    const nextUrl = attempt.nextUrl || '/';
    const originalApi = globalThis.api;
    const originalWindow = globalThis.window;
    const originalLocation = globalThis.location;
    const originalDocument = globalThis.document;
    let state;

    try {
      if (scenario.useDefaultLoader) {
        globalThis.api = makeApiAttempt(attempt);
        state = await _resolveActiveProfileBootstrapState({
          markerStorage: storage,
          markerKey,
          getNextUrl: () => nextUrl,
          redirectToLogin: (value) => {
            const href = `login?next=${encodeURIComponent(value)}`;
            redirectUrls.push(href);
          },
        });
      } else {
        state = await _resolveActiveProfileBootstrapState({
          loadActiveProfile: makeAttempt(attempt),
          markerStorage: storage,
          markerKey,
          getNextUrl: () => nextUrl,
          redirectToLogin: (value) => {
            redirectUrls.push(`login?next=${encodeURIComponent(value)}`);
          },
        });
      }
    } finally {
      globalThis.api = originalApi;
      globalThis.window = originalWindow;
      globalThis.location = originalLocation;
      globalThis.document = originalDocument;
    }

    const bootState = {};
    let applyBotNameCalls = 0;
    const bootResult = await runBootActiveProfileBlock(
      async () => state,
      bootState,
      () => {
        applyBotNameCalls += 1;
      }
    );

    results.push({
      ...state,
      bootContinues: !!(bootResult && bootResult.continued === true),
      bootProfile: Object.prototype.hasOwnProperty.call(bootState, 'activeProfile')
        ? bootState.activeProfile
        : null,
      bootIsDefault: Object.prototype.hasOwnProperty.call(
        bootState,
        'activeProfileIsDefault'
      )
        ? bootState.activeProfileIsDefault
        : null,
      applyBotNameCalls,
    });
    storageHistory.push(storage.snapshot());
  }

  console.log(
    JSON.stringify({
      attempts: results,
      redirects: redirectUrls,
      storageHistory,
      storageSnapshot: storage.snapshot(),
      loadCalls: attempts.length,
    })
  );
})();
"""


@pytest.fixture(scope="module")
def driver_path(tmp_path_factory):
    path = tmp_path_factory.mktemp("boot-profile-driver") / "boot_profile_driver.js"
    path.write_text(_BOOT_DRIVER, encoding="utf-8")
    return str(path)


def _run_boot_profile_scenario(driver_path, scenario):
    process = subprocess.run(
        [NODE, driver_path, str(BOOT_JS), json.dumps(scenario)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if process.returncode != 0:
        raise RuntimeError(f"node profile driver failed: {process.stderr.strip()}")
    return json.loads(process.stdout)


def test_active_profile_boot_recovery_is_one_shot_and_bounded(driver_path):
    payload = _run_boot_profile_scenario(
        driver_path,
        {
            "markerKey": "test-5001-active-profile-recovery",
            "useDefaultLoader": True,
            "attempts": [
                {"status": 401, "payload": {"error": "Authentication required"}, "nextUrl": "/"},
                {"status": 401, "payload": {"error": "Authentication required"}, "nextUrl": "/"},
            ],
        },
    )

    assert payload["attempts"][0]["status"] == "recovery-redirect"
    assert payload["attempts"][0]["bootContinues"] is False
    assert payload["attempts"][0]["bootProfile"] is None
    assert payload["attempts"][0]["bootIsDefault"] is None
    assert payload["attempts"][0]["applyBotNameCalls"] == 0
    assert payload["attempts"][1]["status"] == "fallback"
    assert payload["attempts"][1]["bootContinues"] is True
    assert payload["attempts"][1]["profile"] == "default"
    assert payload["attempts"][1]["isDefault"] is True
    assert payload["attempts"][1]["bootProfile"] == "default"
    assert payload["attempts"][1]["bootIsDefault"] is True
    assert payload["attempts"][1]["applyBotNameCalls"] == 1
    assert payload["loadCalls"] == 2
    assert payload["redirects"] == ["login?next=%2F"]
    assert payload["storageHistory"][0].get(BOOT_MARKER_KEY) == "1"
    assert payload["storageHistory"][1].get(BOOT_MARKER_KEY) is None
    assert payload["storageSnapshot"] == {}


def test_active_profile_boot_recovery_handles_loader_thrown_401s(driver_path):
    payload = _run_boot_profile_scenario(
        driver_path,
        {
            "markerKey": "test-5001-active-profile-recovery-throws",
            "attempts": [
                {"type": "error", "status": 401, "nextUrl": "/"},
                {"type": "error", "status": 401, "nextUrl": "/"},
            ],
        },
    )

    assert payload["attempts"][0]["status"] == "recovery-redirect"
    assert payload["attempts"][0]["bootContinues"] is False
    assert payload["attempts"][0]["bootProfile"] is None
    assert payload["attempts"][0]["bootIsDefault"] is None
    assert payload["attempts"][0]["applyBotNameCalls"] == 0
    assert payload["attempts"][1]["status"] == "fallback"
    assert payload["attempts"][1]["bootContinues"] is True
    assert payload["attempts"][1]["profile"] == "default"
    assert payload["attempts"][1]["isDefault"] is True
    assert payload["attempts"][1]["bootProfile"] == "default"
    assert payload["attempts"][1]["bootIsDefault"] is True
    assert payload["attempts"][1]["applyBotNameCalls"] == 1
    assert payload["redirects"] == ["login?next=%2F"]
    assert payload["storageHistory"][0].get(BOOT_MARKER_KEY) == "1"
    assert payload["storageHistory"][1].get(BOOT_MARKER_KEY) is None
    assert payload["storageSnapshot"] == {}


def test_active_profile_boot_non_401_errors_fallback_without_redirect(driver_path):
    payload = _run_boot_profile_scenario(
        driver_path,
        {
            "markerKey": "test-5001-active-profile-recovery-non-401",
            "useDefaultLoader": True,
            "attempts": [
                {"status": 500, "payload": {"error": "server failure"}, "nextUrl": "/"},
            ],
        },
    )

    assert payload["attempts"][0]["status"] == "fallback"
    assert payload["attempts"][0]["bootContinues"] is True
    assert payload["attempts"][0]["profile"] == "default"
    assert payload["attempts"][0]["isDefault"] is True
    assert payload["attempts"][0]["bootProfile"] == "default"
    assert payload["attempts"][0]["bootIsDefault"] is True
    assert payload["attempts"][0]["applyBotNameCalls"] == 1
    assert payload["redirects"] == []
    assert payload["storageHistory"][0].get("test-5001-active-profile-recovery-non-401") is None
    assert payload["storageSnapshot"] == {}


def test_active_profile_boot_invalid_payload_falls_back_without_redirect(driver_path):
    payload = _run_boot_profile_scenario(
        driver_path,
        {
            "markerKey": "test-5001-active-profile-recovery-invalid-payload",
            "useDefaultLoader": True,
            "attempts": [
                {"status": 200, "payload": {"is_default": False}, "nextUrl": "/"},
            ],
        },
    )

    assert payload["attempts"][0]["status"] == "fallback"
    assert payload["attempts"][0]["bootContinues"] is True
    assert payload["attempts"][0]["profile"] == "default"
    assert payload["attempts"][0]["isDefault"] is True
    assert payload["attempts"][0]["bootProfile"] == "default"
    assert payload["attempts"][0]["bootIsDefault"] is True
    assert payload["attempts"][0]["applyBotNameCalls"] == 1
    assert payload["redirects"] == []
    assert payload["storageHistory"][0].get("test-5001-active-profile-recovery-invalid-payload") is None
    assert payload["storageSnapshot"] == {}


def test_active_profile_success_path_applies_boot_state_and_continues(driver_path):
    payload = _run_boot_profile_scenario(
        driver_path,
        {
            "markerKey": "test-5001-active-profile-recovery-success",
            "useDefaultLoader": True,
            "attempts": [
                {
                    "status": 200,
                    "payload": {"name": "team-profile", "is_default": False},
                }
            ],
        },
    )

    assert payload["attempts"][0]["status"] == "resolved"
    assert payload["attempts"][0]["bootContinues"] is True
    assert payload["attempts"][0]["profile"] == "team-profile"
    assert payload["attempts"][0]["isDefault"] is False
    assert payload["attempts"][0]["bootProfile"] == "team-profile"
    assert payload["attempts"][0]["bootIsDefault"] is False
    assert payload["attempts"][0]["applyBotNameCalls"] == 1
    assert payload["redirects"] == []
    assert payload["storageHistory"][0].get("test-5001-active-profile-recovery-success") is None
    assert payload["storageSnapshot"] == {}
